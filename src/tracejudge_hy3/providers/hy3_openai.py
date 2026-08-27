"""Hy3 provider: a configurable OpenAI-compatible chat-completions client.

No endpoint, model name, or API key is hardcoded here -- everything comes from
Settings (environment variables / .env). All parsed output is validated with
Pydantic; if validation keeps failing after the configured retries, this
raises ProviderResponseError rather than returning an empty/placeholder
result.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import openai

from tracejudge_hy3.config import Settings, get_settings
from tracejudge_hy3.exceptions import (
    ParsingError,
    ProviderAuthError,
    ProviderParseError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from tracejudge_hy3.logging_config import get_logger, redact_secret
from tracejudge_hy3.parsing.structured_output import parse_structured_output
from tracejudge_hy3.prompts.evaluator import (
    EVALUATOR_SYSTEM_PROMPT,
    build_evaluator_json_schema,
    build_evaluator_user_prompt,
)
from tracejudge_hy3.prompts.solver import (
    SOLVER_SYSTEM_PROMPT,
    build_solver_json_schema,
    build_solver_user_prompt,
)
from tracejudge_hy3.providers.base import (
    AttemptOutcome,
    GenerationStatus,
    LLMProvider,
    SolutionGeneration,
    validate_solution_for_problem,
)
from tracejudge_hy3.redaction import redact_sensitive_text
from tracejudge_hy3.schemas.evaluation import ProcessAssessment
from tracejudge_hy3.schemas.execution import ExecutionSummary, StaticEvidence
from tracejudge_hy3.schemas.problem import ProblemSpec
from tracejudge_hy3.schemas.solution import SolutionTrace

logger = get_logger(__name__)


class Hy3OpenAIProvider(LLMProvider):
    name = "hy3"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.hy3_configured():
            raise ProviderAuthError(
                "Hy3 provider requires HY3_BASE_URL, HY3_API_KEY and HY3_MODEL to be set "
                "(see .env.example). Use --provider mock to run without real credentials."
            )
        logger.info(
            "Hy3OpenAIProvider configured: base_url=<configured> model=%s api_key=%s",
            self._redact_configured_secret(self.settings.hy3_model),
            redact_secret(self.settings.hy3_api_key),
        )
        self._client = openai.AsyncOpenAI(
            base_url=self.settings.hy3_base_url,
            api_key=self.settings.hy3_api_key,
            max_retries=0,
        )

    def public_generation_config(self) -> dict[str, Any]:
        """Return only the non-sensitive knobs needed to reproduce generation."""

        return {
            "provider": self.name,
            "model": self._redact_configured_secret(self.settings.hy3_model),
            "reasoning_effort": (
                self._redact_configured_secret(self.settings.hy3_reasoning_effort)
                if self.settings.hy3_enable_reasoning_effort
                else None
            ),
            "reasoning_effort_enabled": self.settings.hy3_enable_reasoning_effort,
            "timeout_seconds": self.settings.hy3_timeout_seconds,
            "max_retries": self.settings.hy3_max_retries,
            "max_parse_repairs": self.settings.hy3_max_parse_repairs,
            # Keep endpoint identity reproducible without persisting a URL that
            # may contain private hostnames, userinfo, or query credentials.
            "endpoint_sha256": self._endpoint_fingerprint(),
        }

    def _endpoint_fingerprint(self) -> str:
        """Fingerprint endpoint identity without URL credentials or query data."""

        try:
            parsed = urlsplit(self.settings.hy3_base_url)
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

    def _redact_configured_secret(self, value: str) -> str:
        """Remove the configured API key from any provider-controlled text."""

        secret = self.settings.hy3_api_key
        if secret:
            return value.replace(secret, "<redacted>")
        return value

    def _artifact_safe_model_text(self, value: str) -> str:
        """Scrub credentials before text is persisted or sent back for repair."""

        return redact_sensitive_text(value, known_secrets=(self.settings.hy3_api_key,))

    async def _call_model(self, messages: list[dict[str, str]]) -> str:
        extra_body: dict[str, Any] = {}
        if self.settings.hy3_enable_reasoning_effort:
            extra_body["reasoning_effort"] = self.settings.hy3_reasoning_effort

        start = time.perf_counter()
        try:
            response = await self._client.chat.completions.create(
                model=self.settings.hy3_model,  # type: ignore[arg-type]
                messages=messages,  # type: ignore[arg-type]
                timeout=self.settings.hy3_timeout_seconds,
                extra_body=extra_body or None,
            )
        except openai.AuthenticationError as exc:
            raise ProviderAuthError(f"Hy3 authentication failed ({type(exc).__name__})") from exc
        except (openai.APITimeoutError, TimeoutError) as exc:
            raise ProviderTimeoutError(f"Hy3 call timed out ({type(exc).__name__})") from exc
        except (openai.APIConnectionError, openai.RateLimitError, openai.APIStatusError) as exc:
            raise ProviderResponseError(f"Hy3 API request failed ({type(exc).__name__})") from exc
        finally:
            elapsed = time.perf_counter() - start
            logger.info("Hy3 call took %.2fs", elapsed)

        if not response.choices:
            raise ProviderResponseError("Hy3 response contained no choices")
        content = response.choices[0].message.content
        if not isinstance(content, str) or not content.strip():
            raise ProviderResponseError("Hy3 response contained no text message content")
        return content

    async def _call_with_retries(
        self,
        system_prompt: str,
        user_prompt: str,
        model_cls: type,
        extra_check: Any = None,
    ) -> Any:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        attempts = self.settings.hy3_max_retries + 1
        last_error: Exception | None = None
        last_was_timeout = False

        for attempt in range(1, attempts + 1):
            try:
                raw = await self._call_model(messages)
            except ProviderTimeoutError as exc:
                last_error = exc
                last_was_timeout = True
                logger.warning("Hy3 call attempt %d/%d timed out", attempt, attempts)
                continue
            except ProviderAuthError:
                raise
            except ProviderResponseError as exc:
                last_error = exc
                last_was_timeout = False
                logger.warning(
                    "Hy3 call attempt %d/%d failed (%s)",
                    attempt,
                    attempts,
                    type(exc).__name__,
                )
                continue

            parse_raw = self._redact_configured_secret(raw)
            artifact_raw = self._artifact_safe_model_text(parse_raw)
            try:
                parsed = parse_structured_output(parse_raw, model_cls)
                if extra_check is not None:
                    extra_check(parsed)
                return parsed
            except (ParsingError, ValueError) as exc:
                safe_error = self._artifact_safe_model_text(str(exc))
                last_error = ParsingError(safe_error)
                last_was_timeout = False
                logger.warning(
                    "Hy3 call attempt %d/%d failed schema validation (%s)",
                    attempt,
                    attempts,
                    type(exc).__name__,
                )
                messages = [
                    *messages,
                    {"role": "assistant", "content": artifact_raw},
                    {
                        "role": "user",
                        "content": (
                            "你上一次的输出未通过 JSON Schema 校验，错误信息如下：\n"
                            f"{safe_error}\n"
                            "请修正后重新输出一个完整、严格符合要求的 JSON 对象，"
                            "不要输出 Markdown 代码围栏或 JSON 之外的任何文字。"
                        ),
                    },
                ]

        if last_was_timeout:
            raise ProviderTimeoutError(
                f"Hy3 call did not complete within {attempts} attempt(s): {last_error}"
            )
        raise ProviderResponseError(
            f"Hy3 response failed schema validation after {attempts} attempt(s): {last_error}"
        )

    def _solver_messages(self, problem: ProblemSpec) -> list[dict[str, str]]:
        user_prompt = build_solver_user_prompt(problem)
        schema = build_solver_json_schema()
        system_prompt = (
            f"{SOLVER_SYSTEM_PROMPT}\n\nJSON Schema:\n"
            f"{json.dumps(schema, ensure_ascii=False, indent=2)}"
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    async def generate_solution_with_details(self, problem: ProblemSpec) -> SolutionGeneration:
        """Generate once with finite retries and a separate JSON repair budget.

        ``HY3_MAX_RETRIES`` limits the total number of additional model calls.
        ``HY3_MAX_PARSE_REPAIRS`` limits how many times a JSON repair prompt may
        be appended after a ``parse_error``.  A provider-level failure never
        resets the parse-repair budget, and the repair budget can never be used
        to construct a second repair prompt.
        """

        messages = self._solver_messages(problem)
        max_calls = self.settings.hy3_max_retries + 1
        max_repairs = self.settings.hy3_max_parse_repairs
        last_raw: str | None = None
        last_raw_attempt: int | None = None
        last_error: Exception | None = None
        last_status: GenerationStatus = "provider_error"
        attempt_outcomes: list[AttemptOutcome] = []
        repairs_used = 0

        for attempt in range(1, max_calls + 1):
            try:
                raw = await self._call_model(messages)
            except ProviderAuthError as exc:
                attempt_outcomes.append("provider_error")
                safe_error = ProviderAuthError(self._redact_configured_secret(str(exc)))
                return SolutionGeneration(
                    status="provider_error",
                    raw_output=last_raw,
                    solution=None,
                    attempt_count=attempt,
                    attempt_outcomes=tuple(attempt_outcomes),
                    error=safe_error,
                    raw_output_attempt=last_raw_attempt,
                    parse_attempted=last_raw is not None,
                )
            except (ProviderTimeoutError, ProviderResponseError) as exc:
                attempt_outcomes.append("provider_error")
                safe_message = self._redact_configured_secret(str(exc))
                last_error = type(exc)(safe_message)
                last_status = "provider_error"
                logger.warning(
                    "Hy3 Solver attempt %d/%d failed (%s)",
                    attempt,
                    max_calls,
                    type(exc).__name__,
                )
                continue

            parse_raw = self._redact_configured_secret(raw)
            artifact_raw = self._artifact_safe_model_text(parse_raw)
            last_raw = artifact_raw
            last_raw_attempt = attempt
            try:
                solution = parse_structured_output(parse_raw, SolutionTrace)
                validate_solution_for_problem(problem, solution)
            except (ParsingError, ValueError) as exc:
                attempt_outcomes.append("parse_error")
                safe_error = self._artifact_safe_model_text(str(exc))
                last_error = ParsingError(safe_error)
                last_status = "parse_error"
                logger.warning(
                    "Hy3 Solver attempt %d/%d failed schema/context validation (%s)",
                    attempt,
                    max_calls,
                    type(exc).__name__,
                )
                if repairs_used < max_repairs and attempt < max_calls:
                    repairs_used += 1
                    messages = [
                        *messages,
                        {"role": "assistant", "content": artifact_raw},
                        {
                            "role": "user",
                            "content": (
                                "你上一次的输出未通过 JSON Schema 校验或公开上下文校验，错误信息如下：\n"
                                f"{safe_error}\n"
                                "请修正后重新输出一个完整、严格符合要求的 JSON 对象，"
                                "不要输出 Markdown 代码围栏或 JSON 之外的任何文字。"
                            ),
                        },
                    ]
                    continue
                # Parse-repair budget is exhausted or no calls remain: this is a
                # terminal parse_error.
                break

            attempt_outcomes.append("success")
            return SolutionGeneration(
                status="success",
                raw_output=artifact_raw,
                solution=solution,
                attempt_count=attempt,
                attempt_outcomes=tuple(attempt_outcomes),
                raw_output_attempt=attempt,
                parse_attempted=True,
            )

        assert last_error is not None
        assert attempt_outcomes
        if last_status == "parse_error":
            error: Exception = ProviderParseError(
                f"Hy3 response failed schema/context validation after {len(attempt_outcomes)} "
                f"attempt(s) and {repairs_used} repair attempt(s): {last_error}"
            )
        elif isinstance(last_error, ProviderTimeoutError):
            error = ProviderTimeoutError(
                f"Hy3 call did not complete within {len(attempt_outcomes)} attempt(s): {last_error}"
            )
        else:
            error = ProviderResponseError(
                f"Hy3 API request failed after {len(attempt_outcomes)} attempt(s): {last_error}"
            )
        return SolutionGeneration(
            status=last_status,
            raw_output=last_raw,
            solution=None,
            attempt_count=len(attempt_outcomes),
            attempt_outcomes=tuple(attempt_outcomes),
            error=error,
            raw_output_attempt=last_raw_attempt,
            parse_attempted=last_raw is not None,
        )

    async def generate_solution(self, problem: ProblemSpec) -> SolutionTrace:
        generation = await self.generate_solution_with_details(problem)
        if generation.status == "success":
            assert generation.solution is not None
            return generation.solution
        assert generation.error is not None
        raise generation.error

    async def evaluate_process(
        self,
        problem: ProblemSpec,
        solution: SolutionTrace,
        static_evidence: StaticEvidence,
        execution_result: ExecutionSummary,
    ) -> ProcessAssessment:
        user_prompt = build_evaluator_user_prompt(
            problem, solution, static_evidence, execution_result
        )
        schema = build_evaluator_json_schema()
        system_prompt = (
            f"{EVALUATOR_SYSTEM_PROMPT}\n\nJSON Schema:\n"
            f"{json.dumps(schema, ensure_ascii=False, indent=2)}"
        )

        valid_step_ids = {step.step_id for step in solution.implementation_steps}
        valid_requirement_ids = {requirement.requirement_id for requirement in problem.requirements}

        def _check_context_references(assessment: ProcessAssessment) -> None:
            referenced_step_ids = set(assessment.affected_steps)
            if assessment.first_faulty_step is not None:
                referenced_step_ids.add(assessment.first_faulty_step)
            unknown_step_ids = referenced_step_ids - valid_step_ids
            if unknown_step_ids:
                raise ValueError(
                    "assessment references unknown implementation step IDs: "
                    f"{sorted(unknown_step_ids)}"
                )

            violated_requirement = assessment.violated_requirement
            if (
                violated_requirement is not None
                and violated_requirement not in valid_requirement_ids
            ):
                raise ValueError(
                    f"assessment references unknown requirement ID: {violated_requirement!r}"
                )

        return await self._call_with_retries(
            system_prompt,
            user_prompt,
            ProcessAssessment,
            extra_check=_check_context_references,
        )

    async def aclose(self) -> None:
        await self._client.close()
