"""Abstract LLM provider interface implemented by both Mock and Hy3 providers."""

from __future__ import annotations

from abc import ABC, abstractmethod

from tracejudge_hy3.schemas.evaluation import ProcessAssessment
from tracejudge_hy3.schemas.execution import ExecutionSummary, StaticEvidence
from tracejudge_hy3.schemas.problem import ProblemSpec
from tracejudge_hy3.schemas.solution import SolutionTrace


class LLMProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def generate_solution(self, problem: ProblemSpec) -> SolutionTrace: ...

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
