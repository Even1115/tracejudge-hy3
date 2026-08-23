"""End-to-end pipeline: generate -> parse -> static analysis -> execute ->
four-layer evaluate -> counterexample search -> error certificate.

This is the fixed Generate -> Parse -> Execute -> Evaluate -> Verify ->
Aggregate workflow described in the design doc -- deliberately not a
multi-agent orchestration, so each stage stays independently testable.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from tracejudge_hy3.config import Settings
from tracejudge_hy3.counterexample.generator import find_counterexample
from tracejudge_hy3.evaluator.alignment import combine_assessment
from tracejudge_hy3.evaluator.evidence import build_error_certificate
from tracejudge_hy3.evaluator.hy3_judge import get_llm_assessment
from tracejudge_hy3.evaluator.rule_based import evaluate_alignment_rules
from tracejudge_hy3.exceptions import (
    ConfigurationError,
    ProviderResponseError,
    SandboxError,
    SandboxUnavailableError,
    UnsafeExecutionError,
)
from tracejudge_hy3.providers.base import LLMProvider
from tracejudge_hy3.sandbox.base import SandboxBackend
from tracejudge_hy3.sandbox.docker_backend import DockerSandbox
from tracejudge_hy3.sandbox.trusted_local import TrustedLocalSandbox
from tracejudge_hy3.schemas.evaluation import Counterexample, ErrorCertificate, ProcessAssessment
from tracejudge_hy3.schemas.execution import ExecutionSummary, StaticEvidence
from tracejudge_hy3.schemas.problem import ProblemSpec
from tracejudge_hy3.schemas.solution import SolutionTrace
from tracejudge_hy3.static_analysis.ast_analyzer import analyze_code


class PipelineResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    problem: ProblemSpec
    solution: SolutionTrace
    static_evidence: StaticEvidence
    execution_result: ExecutionSummary
    llm_assessment: ProcessAssessment | None
    process_assessment: ProcessAssessment
    counterexample: Counterexample | None
    error_certificate: ErrorCertificate | None


def select_backend(
    *,
    provider_name: str,
    sandbox_choice: str,
    allow_unsafe_local_exec: bool,
    settings: Settings,
) -> SandboxBackend:
    """Enforce: real (non-mock) provider code may not run via trusted-local
    unless the caller explicitly opts in with --allow-unsafe-local-exec."""

    if sandbox_choice == "trusted-local":
        if provider_name != "mock" and not allow_unsafe_local_exec:
            raise UnsafeExecutionError(
                "trusted-local sandbox refuses to run non-mock provider output "
                "without --allow-unsafe-local-exec. Use --sandbox docker instead, "
                "or pass --allow-unsafe-local-exec if you understand the risk."
            )
        return TrustedLocalSandbox(
            per_test_timeout_seconds=settings.tracejudge_test_timeout_seconds,
            allow_untrusted_code=allow_unsafe_local_exec,
        )

    if sandbox_choice == "docker":
        return DockerSandbox(
            image=settings.tracejudge_docker_image,
            memory_limit=settings.tracejudge_memory_limit,
            cpu_limit=settings.tracejudge_cpu_limit,
            per_test_timeout_seconds=settings.tracejudge_test_timeout_seconds,
        )

    raise ConfigurationError(
        f"unknown sandbox {sandbox_choice!r}; expected 'docker' or 'trusted-local'"
    )


def _visible_test_values(problem: ProblemSpec) -> list[object]:
    values: list[object] = []
    for tc in problem.visible_test_cases:
        values.extend(tc.args)
        values.append(tc.expected)
    return values


async def run_pipeline(
    problem: ProblemSpec,
    provider: LLMProvider,
    backend: SandboxBackend,
) -> PipelineResult:
    available, unavailable_reason = backend.is_available()
    if not available:
        raise SandboxUnavailableError(
            f"{backend.name} sandbox unavailable: {unavailable_reason or 'unknown reason'}"
        )

    solution = await provider.generate_solution(problem)

    if solution.problem_id != problem.problem_id:
        raise ProviderResponseError(
            f"solution problem_id mismatch: expected {problem.problem_id!r}, "
            f"got {solution.problem_id!r}"
        )

    valid_requirement_ids = {item.requirement_id for item in problem.requirements}
    for step in solution.implementation_steps:
        unknown_ids = set(step.related_requirements) - valid_requirement_ids
        if unknown_ids:
            raise ProviderResponseError(
                f"solution step {step.step_id!r} references unknown requirement IDs: "
                f"{sorted(unknown_ids)}"
            )

    if (
        isinstance(backend, TrustedLocalSandbox)
        and not backend.allow_untrusted_code
        and not provider.is_trusted_local_solution(problem, solution)
    ):
        raise UnsafeExecutionError(
            "trusted-local execution is limited to repository-owned Mock fixtures. "
            "This solution is not a trusted fixture; use Docker or explicitly pass "
            "--allow-unsafe-local-exec if you understand the risk."
        )

    static_evidence = analyze_code(
        solution.code,
        function_name=problem.function_name,
        visible_test_values=_visible_test_values(problem),
    )

    execution_result = backend.run(solution.code, problem.function_name, problem.all_test_cases())
    if execution_result.runtime_status == "backend_error":
        raise SandboxError(
            f"{backend.name} sandbox failed before producing candidate results: "
            f"{execution_result.setup_error or 'unknown backend error'}"
        )
    execution_result = execution_result.model_copy(update={"problem_id": problem.problem_id})

    rule_assessment = evaluate_alignment_rules(problem, solution, static_evidence, execution_result)
    llm_assessment = await get_llm_assessment(
        provider, problem, solution, static_evidence, execution_result
    )
    process_assessment = combine_assessment(
        problem,
        solution,
        static_evidence,
        execution_result,
        llm_assessment,
        rule_assessment=rule_assessment,
    )

    counterexample = None
    if process_assessment.error_type is not None:
        counterexample = find_counterexample(
            problem,
            solution,
            execution_result,
            backend,
            violated_requirement=process_assessment.violated_requirement,
        )

    error_certificate = build_error_certificate(
        process_assessment,
        execution_result,
        counterexample,
        rule_fired=rule_assessment is not None,
    )

    return PipelineResult(
        problem=problem,
        solution=solution,
        static_evidence=static_evidence,
        execution_result=execution_result,
        llm_assessment=llm_assessment,
        process_assessment=process_assessment,
        counterexample=counterexample,
        error_certificate=error_certificate,
    )
