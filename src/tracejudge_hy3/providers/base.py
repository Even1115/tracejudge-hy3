"""Abstract LLM provider interface implemented by both Mock and Hy3 providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal

from tracejudge_hy3.exceptions import ParsingError, ProviderError, ProviderResponseError
from tracejudge_hy3.schemas.evaluation import ProcessAssessment
from tracejudge_hy3.schemas.execution import ExecutionSummary, StaticEvidence
from tracejudge_hy3.schemas.problem import ProblemSpec
from tracejudge_hy3.schemas.solution import SolutionTrace

GenerationStatus = Literal["success", "parse_error", "provider_error"]
AttemptOutcome = GenerationStatus
_ATTEMPT_OUTCOMES = frozenset({"success", "parse_error", "provider_error"})


@dataclass(frozen=True, slots=True)
class SolutionGeneration:
    """Observable result of one finite Solver generation sequence.

    ``raw_output`` and the parsed ``solution`` deliberately remain separate so
    phase-one experiments can audit exactly what the provider returned.  The
    exception object is for in-process classification only and must never be
    serialized directly into an artifact.
    """

    status: GenerationStatus
    raw_output: str | None
    solution: SolutionTrace | None
    attempt_count: int
    attempt_outcomes: tuple[AttemptOutcome, ...]
    error: Exception | None = None
    raw_output_attempt: int | None = None
    parse_attempted: bool = False

    def __post_init__(self) -> None:
        """Reject ambiguous or internally inconsistent attempt histories."""

        if isinstance(self.attempt_count, bool) or not isinstance(self.attempt_count, int):
            raise TypeError("attempt_count must be an integer")
        if self.attempt_count < 1:
            raise ValueError("a generation sequence must contain at least one attempt")
        if not isinstance(self.attempt_outcomes, tuple):
            raise TypeError("attempt_outcomes must be a tuple")
        if self.attempt_count != len(self.attempt_outcomes):
            raise ValueError("attempt_count must equal len(attempt_outcomes)")
        if not isinstance(self.status, str) or self.status not in _ATTEMPT_OUTCOMES:
            raise ValueError(f"unsupported generation status: {self.status!r}")
        if any(
            not isinstance(outcome, str) or outcome not in _ATTEMPT_OUTCOMES
            for outcome in self.attempt_outcomes
        ):
            raise ValueError("attempt_outcomes contains an unsupported outcome")
        if self.attempt_outcomes[-1] != self.status:
            raise ValueError("generation status must equal the final attempt outcome")
        if "success" in self.attempt_outcomes[:-1]:
            raise ValueError("a generation sequence cannot continue after success")
        expected_parse_attempted = any(
            outcome in {"parse_error", "success"} for outcome in self.attempt_outcomes
        )
        if self.parse_attempted is not expected_parse_attempted:
            raise ValueError("parse_attempted must match the observable attempt outcomes")
        if self.raw_output is not None and not isinstance(self.raw_output, str):
            raise TypeError("raw_output must be text or None")
        if self.raw_output_attempt is not None and (
            isinstance(self.raw_output_attempt, bool)
            or not isinstance(self.raw_output_attempt, int)
        ):
            raise TypeError("raw_output_attempt must be an integer or None")
        if expected_parse_attempted and (
            self.raw_output is None or self.raw_output_attempt is None
        ):
            raise ValueError("a parse attempt must preserve raw output and its attempt number")
        if (self.raw_output is None) is not (self.raw_output_attempt is None):
            raise ValueError("raw_output and raw_output_attempt must be present together")
        if self.raw_output_attempt is not None:
            if not 1 <= self.raw_output_attempt <= self.attempt_count:
                raise ValueError("raw_output_attempt is outside the attempt sequence")
            if self.attempt_outcomes[self.raw_output_attempt - 1] == "provider_error":
                raise ValueError("raw_output_attempt cannot reference a provider_error")

    @property
    def retry_count(self) -> int:
        return max(0, self.attempt_count - 1)


def validate_solution_for_problem(problem: ProblemSpec, solution: SolutionTrace) -> None:
    """Validate public-context links and the auditable phase-one payload."""

    if solution.problem_id != problem.problem_id:
        raise ValueError(
            f"problem_id mismatch: expected {problem.problem_id!r}, got {solution.problem_id!r}"
        )

    required_text = {
        "requirement_understanding": solution.requirement_understanding,
        "design_summary": solution.design_summary,
        "code": solution.code,
    }
    empty_fields = [name for name, value in required_text.items() if not value.strip()]
    if empty_fields:
        raise ValueError(f"solution fields must be non-empty: {sorted(empty_fields)}")
    if not solution.implementation_steps:
        raise ValueError("solution must include at least one auditable implementation step")
    empty_steps = [
        step.step_id for step in solution.implementation_steps if not step.content.strip()
    ]
    if empty_steps:
        raise ValueError(f"solution contains empty implementation steps: {sorted(empty_steps)}")

    valid_requirement_ids = {item.requirement_id for item in problem.requirements}
    for step in solution.implementation_steps:
        unknown_ids = set(step.related_requirements) - valid_requirement_ids
        if unknown_ids:
            raise ValueError(
                f"solution step {step.step_id!r} references unknown requirement IDs: "
                f"{sorted(unknown_ids)}"
            )


class LLMProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def generate_solution(self, problem: ProblemSpec) -> SolutionTrace: ...

    async def generate_solution_with_details(self, problem: ProblemSpec) -> SolutionGeneration:
        """Generate a solution while retaining raw text and failure category.

        Providers with a native text response should override this method.
        The default keeps existing providers compatible and serializes their
        already-parsed result as canonical JSON for the raw-output field.
        """

        try:
            solution = await self.generate_solution(problem)
        except (ParsingError, ValueError) as exc:
            return SolutionGeneration(
                status="provider_error",
                raw_output=None,
                solution=None,
                attempt_count=1,
                attempt_outcomes=("provider_error",),
                error=ProviderResponseError(
                    "provider failed before exposing auditable raw model output "
                    f"({type(exc).__name__})"
                ),
            )
        except ProviderError as exc:
            return SolutionGeneration(
                status="provider_error",
                raw_output=None,
                solution=None,
                attempt_count=1,
                attempt_outcomes=("provider_error",),
                error=exc,
            )

        raw_output = solution.model_dump_json()
        try:
            validate_solution_for_problem(problem, solution)
        except ValueError as exc:
            return SolutionGeneration(
                status="parse_error",
                raw_output=raw_output,
                solution=None,
                attempt_count=1,
                attempt_outcomes=("parse_error",),
                error=exc,
                raw_output_attempt=1,
                parse_attempted=True,
            )
        return SolutionGeneration(
            status="success",
            raw_output=raw_output,
            solution=solution,
            attempt_count=1,
            attempt_outcomes=("success",),
            raw_output_attempt=1,
            parse_attempted=True,
        )

    def public_generation_config(self) -> dict[str, Any]:
        """Return an explicit, non-sensitive config allowlist for manifests."""

        return {
            "provider": self.name,
            "model": self.name,
            "reasoning_effort": None,
            "reasoning_effort_enabled": False,
            "timeout_seconds": None,
            "max_retries": 0,
            "endpoint_sha256": None,
        }

    @abstractmethod
    async def evaluate_process(
        self,
        problem: ProblemSpec,
        solution: SolutionTrace,
        static_evidence: StaticEvidence,
        execution_result: ExecutionSummary,
    ) -> ProcessAssessment: ...

    def is_trusted_local_solution(
        self,
        problem: ProblemSpec,
        solution: SolutionTrace,
    ) -> bool:
        """Whether this exact solution is a repository-owned fixture.

        Providers are untrusted by default. The pipeline uses this provenance
        check before allowing execution in TrustedLocalSandbox without an
        explicit unsafe opt-in.
        """

        return False

    async def aclose(self) -> None:
        """Release provider resources; no-op for providers without a client."""

        return None
