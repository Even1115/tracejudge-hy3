"""Real Hy3 backend for the frozen judge-only prompt.

Wraps the project's OpenAI-compatible Hy3 settings with a minimal
text-completion surface.  All knobs are frozen for the experiment and exposed
via :meth:`public_configuration` so the manifest can bind them: temperature
0.0 (deterministic judging, matching the CJ-Eval protocol), timeout and
provider-error retries from Settings.  Parse repair is owned by the runner
(``MAX_PARSE_REPAIRS``), never by this provider.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import openai

from tracejudge_hy3.config import Settings, get_settings
from tracejudge_hy3.exceptions import (
    ProviderAuthError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from tracejudge_hy3.logging_config import get_logger

logger = get_logger(__name__)

JUDGE_ONLY_TEMPERATURE = 0.0


class Hy3JudgeOnlyProvider:
    """``JudgeOnlyProvider`` implementation backed by the real Hy3 endpoint."""

    name = "hy3"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        temperature: float = JUDGE_ONLY_TEMPERATURE,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        if not self.settings.hy3_configured():
            raise ProviderAuthError(
                "Hy3 judge-only provider requires HY3_BASE_URL, HY3_API_KEY and HY3_MODEL"
            )
        self.temperature = temperature
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else self.settings.hy3_timeout_seconds
        )
        self.max_retries = max_retries if max_retries is not None else self.settings.hy3_max_retries
        self._client = openai.AsyncOpenAI(
            base_url=self.settings.hy3_base_url,
            api_key=self.settings.hy3_api_key,
            max_retries=0,
        )
        # Audit counters: one "call" is one complete() invocation; "attempts"
        # counts every underlying API attempt including retries.
        self._call_count = 0
        self._attempt_count = 0

    def audit_counters(self) -> dict[str, int]:
        """Aggregate provider audit counts for the completion receipt."""

        return {
            "calls": self._call_count,
            "attempts": self._attempt_count,
            "retries": self._attempt_count - self._call_count,
        }

    def public_configuration(self) -> dict[str, Any]:
        """Non-sensitive frozen knobs for the experiment manifest."""

        secret = self.settings.hy3_api_key or ""
        model = self.settings.hy3_model or ""
        if secret:
            model = model.replace(secret, "<redacted>")
        return {
            "provider": self.name,
            "model": model,
            "temperature": self.temperature,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "reasoning_effort": (
                self.settings.hy3_reasoning_effort
                if self.settings.hy3_enable_reasoning_effort
                else None
            ),
            "endpoint_sha256": self._endpoint_fingerprint(),
        }

    def _endpoint_fingerprint(self) -> str:
        try:
            parsed = urlsplit(self.settings.hy3_base_url or "")
            hostname = (parsed.hostname or "").lower()
            if ":" in hostname and not hostname.startswith("["):
                hostname = f"[{hostname}]"
            port = parsed.port
            netloc = f"{hostname}:{port}" if port is not None else hostname
            path = parsed.path.rstrip("/") or "/"
            canonical = urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))
        except (TypeError, ValueError):
            canonical = "unparseable-endpoint"
        return hashlib.sha256(canonical.encode("utf-8", errors="replace")).hexdigest()

    async def _call_once(self, *, system_prompt: str, user_prompt: str) -> str:
        extra_body: dict[str, Any] = {}
        if self.settings.hy3_enable_reasoning_effort:
            extra_body["reasoning_effort"] = self.settings.hy3_reasoning_effort
        start = time.perf_counter()
        try:
            response = await self._client.chat.completions.create(
                model=self.settings.hy3_model,  # type: ignore[arg-type]
                messages=[  # type: ignore[list-item]
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=self.temperature,
                timeout=self.timeout_seconds,
                extra_body=extra_body or None,
            )
        except openai.AuthenticationError as exc:
            raise ProviderAuthError(f"Hy3 authentication failed ({type(exc).__name__})") from exc
        except (openai.APITimeoutError, TimeoutError) as exc:
            raise ProviderTimeoutError(f"Hy3 call timed out ({type(exc).__name__})") from exc
        except (openai.APIConnectionError, openai.RateLimitError, openai.APIStatusError) as exc:
            raise ProviderResponseError(f"Hy3 API request failed ({type(exc).__name__})") from exc
        finally:
            logger.info("Hy3 judge-only call took %.2fs", time.perf_counter() - start)
        if not response.choices:
            raise ProviderResponseError("Hy3 response contained no choices")
        content = response.choices[0].message.content
        if not isinstance(content, str) or not content.strip():
            raise ProviderResponseError("Hy3 response contained no text message content")
        return content

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        """One logical completion with frozen provider-error retries."""

        self._call_count += 1
        attempts = self.max_retries + 1
        last_error: ProviderTimeoutError | ProviderResponseError | None = None
        for attempt in range(1, attempts + 1):
            self._attempt_count += 1
            try:
                return await self._call_once(system_prompt=system_prompt, user_prompt=user_prompt)
            except ProviderAuthError:
                raise
            except (ProviderTimeoutError, ProviderResponseError) as exc:
                last_error = exc
                logger.warning(
                    "Hy3 judge-only attempt %d/%d failed (%s)",
                    attempt,
                    attempts,
                    type(exc).__name__,
                )
        assert last_error is not None
        raise type(last_error)(
            f"Hy3 judge-only call failed after {attempts} attempt(s): {last_error}"
        ) from last_error

    async def aclose(self) -> None:
        await self._client.close()
