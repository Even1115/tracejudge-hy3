"""Abstract LLM provider interface implemented by both Mock and Hy3 providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal

from tracejudge_hy3.exceptions import ParsingError, ProviderError
from tracejudge_hy3.schemas.evaluation import ProcessAssessment
from tracejudge_hy3.schemas.execution import ExecutionSummary, StaticEvidence
from tracejudge_hy3.schemas.problem import ProblemSpec
from tracejudge_hy3.schemas.solution import SolutionTrace

GenerationStatus = Literal["success", "parse_error", "provider_error"]


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
    error: Exception | None = None
    raw_output_attempt: int | None = None
    parse_attempted: bool = False

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
            validate_solution_for_problem(problem, solution)
        except (ParsingError, ValueError) as exc:
            return SolutionGeneration(
                status="parse_error",
                raw_output=None,
                solution=None,
                attempt_count=1,
                error=exc,
                parse_attempted=True,
            )
        except ProviderError as exc:
            return SolutionGeneration(
                status="provider_error",
                raw_output=None,
                solution=None,
                attempt_count=1,
                error=exc,
            )
        return SolutionGeneration(
            status="success",
            raw_output=solution.model_dump_json(),
            solution=solution,
            attempt_count=1,
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
