"""Hy3 provider: a configurable OpenAI-compatible chat-completions client.

No endpoint, model name, or API key is hardcoded here -- everything comes from
Settings (environment variables / .env). All parsed output is validated with
Pydantic; if validation keeps failing after the configured retries, this
raises ProviderResponseError rather than returning an empty/placeholder
result.
"""

from __future__ import annotations

import json
import time
from typing import Any

import openai

from tracejudge_hy3.config import Settings, get_settings
from tracejudge_hy3.exceptions import (
    ParsingError,
    ProviderAuthError,
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
from tracejudge_hy3.providers.base import LLMProvider
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
            "Hy3OpenAIProvider configured: base_url=%s model=%s api_key=%s",
            self.settings.hy3_base_url,
            self.settings.hy3_model,
            redact_secret(self.settings.hy3_api_key),
        )
        self._client = openai.AsyncOpenAI(
            base_url=self.settings.hy3_base_url,
            api_key=self.settings.hy3_api_key,
            max_retries=0,
        )

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
            raise ProviderAuthError(f"Hy3 authentication failed: {exc}") from exc
        except (openai.APITimeoutError, TimeoutError) as exc:
            raise ProviderTimeoutError(f"Hy3 call timed out: {exc}") from exc
        except (openai.APIConnectionError, openai.RateLimitError, openai.APIStatusError) as exc:
            raise ProviderResponseError(
                f"Hy3 API request failed ({type(exc).__name__}): {exc}"
            ) from exc
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
                logger.warning("Hy3 call attempt %d/%d failed: %s", attempt, attempts, exc)
                continue

            try:
                parsed = parse_structured_output(raw, model_cls)
                if extra_check is not None:
                    extra_check(parsed)
                return parsed
            except (ParsingError, ValueError) as exc:
                last_error = exc
                last_was_timeout = False
                logger.warning(
                    "Hy3 call attempt %d/%d failed schema validation: %s", attempt, attempts, exc
                )
                messages = [
                    *messages,
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": (
                            "你上一次的输出未通过 JSON Schema 校验，错误信息如下：\n"
                            f"{exc}\n"
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

    async def generate_solution(self, problem: ProblemSpec) -> SolutionTrace:
        user_prompt = build_solver_user_prompt(problem)
        schema = build_solver_json_schema()
        system_prompt = (
            f"{SOLVER_SYSTEM_PROMPT}\n\nJSON Schema:\n"
            f"{json.dumps(schema, ensure_ascii=False, indent=2)}"
        )

        def _check_problem_id(solution: SolutionTrace) -> None:
            if solution.problem_id != problem.problem_id:
                raise ValueError(
                    f"problem_id mismatch: expected '{problem.problem_id}', "
                    f"got '{solution.problem_id}'"
                )

        return await self._call_with_retries(
            system_prompt, user_prompt, SolutionTrace, extra_check=_check_problem_id
        )

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
