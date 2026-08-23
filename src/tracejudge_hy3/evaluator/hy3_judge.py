"""Thin wrapper around LLMProvider.evaluate_process.

Isolated in its own module so evaluator/alignment.py can treat "no LLM signal
available" (e.g. a transient Hy3 API failure) as a degraded-but-recoverable
state -- the combined assessment then falls back to rule_based evidence alone
instead of crashing the whole pipeline run.
"""

from __future__ import annotations

from tracejudge_hy3.exceptions import ProviderError
from tracejudge_hy3.logging_config import get_logger
from tracejudge_hy3.providers.base import LLMProvider
from tracejudge_hy3.schemas.evaluation import ProcessAssessment
from tracejudge_hy3.schemas.execution import ExecutionSummary, StaticEvidence
from tracejudge_hy3.schemas.problem import ProblemSpec
from tracejudge_hy3.schemas.solution import SolutionTrace

logger = get_logger(__name__)


async def get_llm_assessment(
    provider: LLMProvider,
    problem: ProblemSpec,
    solution: SolutionTrace,
    static_evidence: StaticEvidence,
    execution_result: ExecutionSummary,
) -> ProcessAssessment | None:
    try:
        return await provider.evaluate_process(problem, solution, static_evidence, execution_result)
    except ProviderError as exc:
        logger.warning(
            "LLM process-evaluation call failed, continuing with rule-based evidence only: %s", exc
        )
        return None
