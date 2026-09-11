"""Shared frozen-solution evaluator used by the product and new experiments."""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict, Field

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
from tracejudge_hy3.sandbox.trusted_local import TrustedLocalSandbox
from tracejudge_hy3.schemas.evaluation import Counterexample, ErrorCertificate, ProcessAssessment
from tracejudge_hy3.schemas.execution import ExecutionSummary, StaticEvidence
from tracejudge_hy3.schemas.problem import ProblemSpec
from tracejudge_hy3.schemas.solution import SolutionTrace
from tracejudge_hy3.static_analysis.ast_analyzer import analyze_code


def model_sha256(value: BaseModel) -> str:
    """Hash the validated model using the project's canonical JSON encoding."""
    import json

    raw = json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class EvaluationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    use_judge: bool = True
    search_counterexamples: bool = True


class BoundExecutionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    problem_sha256: str
    solution_sha256: str
    execution: ExecutionSummary

    def validate_binding(self, problem: ProblemSpec, solution: SolutionTrace) -> None:
        if (
            self.problem_sha256 != model_sha256(problem)
            or self.solution_sha256 != model_sha256(solution)
            or self.execution.problem_id != problem.problem_id
            or self.execution.function_name != problem.function_name
        ):
            raise ProviderResponseError(
                "cached execution does not match the frozen problem/solution"
            )
        if self.execution.runtime_status == "completed":
            expected = {case.case_id: case for case in problem.all_test_cases()}
            observed = [case.case_id for case in self.execution.results]
            if len(observed) != len(set(observed)) or set(observed) != set(expected):
                raise ProviderResponseError(
                    "cached execution has incomplete or duplicate test coverage"
                )
            for result in self.execution.results:
                case = expected[result.case_id]
                if (
                    result.category != case.category
                    or result.related_requirements != case.related_requirements
                    or result.expected_output != case.expected
                ):
                    raise ProviderResponseError(
                        "cached execution test identity does not match problem"
                    )


class PipelineResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    evaluator_version: str = "shared-evaluator-v3"
    policy: EvaluationPolicy = Field(default_factory=EvaluationPolicy)
    solution_sha256: str | None = None
    execution_reused: bool = False
    problem: ProblemSpec
    solution: SolutionTrace
    static_evidence: StaticEvidence
    execution_result: ExecutionSummary
    llm_assessment: ProcessAssessment | None
    process_assessment: ProcessAssessment
    counterexample: Counterexample | None
    error_certificate: ErrorCertificate | None


def _visible_test_values(problem: ProblemSpec) -> list[object]:
    values: list[object] = []
    for tc in problem.visible_test_cases:
        values.extend(tc.args)
        values.append(tc.expected)
    return values


async def evaluate_solution(
    problem: ProblemSpec,
    solution: SolutionTrace,
    provider: LLMProvider | None,
    backend: SandboxBackend,
    *,
    policy: EvaluationPolicy | None = None,
    execution_evidence: BoundExecutionEvidence | None = None,
) -> PipelineResult:
    """Evaluate a frozen candidate; never calls generate_solution.

    Cached execution must bind the exact problem (including tests) and solution.
    Supply only evidence from a trusted execution artifact. Hash binding checks
    identity, not authenticity. Counterexample search may run additional tests.
    """
    policy = policy or EvaluationPolicy()
    if policy.use_judge and provider is None:
        raise ConfigurationError("a Judge provider is required by the evaluation policy")
    # Isolate callers' frozen objects from providers/backends that mutate inputs.
    problem = problem.model_copy(deep=True)
    solution = solution.model_copy(deep=True)
    if execution_evidence is not None:
        execution_evidence.validate_binding(problem, solution)
    if execution_evidence is None or policy.search_counterexamples:
        available, reason = backend.is_available()
        if not available:
            raise SandboxUnavailableError(f"{backend.name} sandbox unavailable: {reason}")
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
        and (execution_evidence is None or policy.search_counterexamples)
        and (provider is None or not provider.is_trusted_local_solution(problem, solution))
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

    execution_result = (
        execution_evidence.execution.model_copy(deep=True)
        if execution_evidence is not None
        else backend.run(solution.code, problem.function_name, problem.all_test_cases())
    )
    if execution_result.runtime_status == "backend_error":
        raise SandboxError(
            f"{backend.name} sandbox failed before producing candidate results: "
            f"{execution_result.setup_error or 'unknown backend error'}"
        )
    execution_result = execution_result.model_copy(update={"problem_id": problem.problem_id})

    rule_assessment = evaluate_alignment_rules(problem, solution, static_evidence, execution_result)
    llm_assessment = None
    if policy.use_judge:
        llm_assessment = await get_llm_assessment(
            provider,
            problem.model_copy(deep=True),
            solution.model_copy(deep=True),
            static_evidence.model_copy(deep=True),
            execution_result.model_copy(deep=True),
        )
    for assessment in (rule_assessment, llm_assessment):
        if assessment is not None:
            try:
                assessment.validate_location_against(solution)
            except ValueError as exc:
                raise ProviderResponseError(
                    "assessment location does not bind to frozen solution"
                ) from exc
    process_assessment = combine_assessment(
        problem,
        solution,
        static_evidence,
        execution_result,
        llm_assessment,
        rule_assessment=rule_assessment,
    )

    counterexample = None
    if policy.search_counterexamples and process_assessment.error_type is not None:
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
        policy=policy,
        solution_sha256=model_sha256(solution),
        execution_reused=execution_evidence is not None,
        problem=problem,
        solution=solution,
        static_evidence=static_evidence,
        execution_result=execution_result,
        llm_assessment=llm_assessment,
        process_assessment=process_assessment,
        counterexample=counterexample,
        error_certificate=error_certificate,
    )
