"""Deterministic Mock provider.

Ships real, schema-valid SolutionTrace fixtures (not placeholder "success"
strings) for the built-in sample problems, plus a generic fallback for any
other problem in a dataset so `run`/`batch --provider mock` never crashes on
an unrecognised problem_id.

evaluate_process() implements a small heuristic that stands in for what a real
Hy3 judge call would return. It is intentionally simple (keyword + static-
evidence checks) and is meant to be cross-checked against the independent,
purely rule-based evaluator in evaluator/rule_based.py -- exactly the
cross-validation the design doc calls for to reduce reliance on a single
LLM-as-judge opinion.
"""

from __future__ import annotations

import json
from pathlib import Path

from tracejudge_hy3.dataset.loader import load_problem_by_id
from tracejudge_hy3.evaluator.claims import (
    claims_empty_input_handling,
    claims_explicit_empty_input_branch,
)
from tracejudge_hy3.evaluator.code_location import function_code_span
from tracejudge_hy3.exceptions import ConfigurationError, ProviderResponseError, TraceJudgeError
from tracejudge_hy3.prompts.solver import solver_public_payload
from tracejudge_hy3.providers.base import (
    LLMProvider,
    SolutionGeneration,
    validate_solution_for_problem,
)
from tracejudge_hy3.resources import data_path
from tracejudge_hy3.schemas.evaluation import ErrorType, ProcessAssessment
from tracejudge_hy3.schemas.execution import ExecutionSummary, StaticEvidence
from tracejudge_hy3.schemas.problem import ProblemSpec
from tracejudge_hy3.schemas.solution import ImplementationStep, SolutionTrace

_SET_KEYWORDS = ("集合", "set", "哈希表", "hash set")


def _claims_constant_time(complexity: str | None) -> bool:
    if not complexity:
        return False
    normalized = complexity.lower().replace(" ", "")
    return any(marker in normalized for marker in ("o(1)", "常数时间", "constanttime"))


def _find_mock_responses_dir() -> Path:
    return data_path("mock_responses")


def _fixture_path(name: str) -> Path:
    return _find_mock_responses_dir() / f"{name}.json"


def _load_fixture(name: str) -> SolutionTrace:
    fixture_path = _fixture_path(name)
    if not fixture_path.exists():
        raise ConfigurationError(f"mock fixture not found: {fixture_path}")
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    return SolutionTrace.model_validate(payload)


class MockProvider(LLMProvider):
    name = "mock"

    def __init__(self, case: str | None = None) -> None:
        """case only affects the built-in safe_mean demo problem: 'correct' | 'faulty'."""

        self.case = case

    def _fixture_name(self, problem: ProblemSpec) -> str | None:
        if problem.problem_id == "safe_mean":
            variant = self.case or "correct"
            if variant not in ("correct", "faulty"):
                raise ConfigurationError(f"unknown mock case '{variant}' for safe_mean")
            return f"safe_mean_{variant}"
        if problem.problem_id in {"deduplicate_preserve_order", "clamp"}:
            return f"{problem.problem_id}_correct"
        return None

    async def generate_solution(self, problem: ProblemSpec) -> SolutionTrace:
        fixture_name = self._fixture_name(problem)
        if fixture_name is not None:
            return _load_fixture(fixture_name)
        return self._fallback_solution(problem)

    async def generate_solution_with_details(self, problem: ProblemSpec) -> SolutionGeneration:
        """Return deterministic fixture text without using reference-code fallback.

        The fallback remains available to the legacy full-pipeline Mock mode,
        but it is intentionally forbidden for a baseline experiment because it
        reads ``reference_code`` and therefore is not a valid Solver sample.
        """

        try:
            fixture_name = self._fixture_name(problem)
        except ConfigurationError as exc:
            return SolutionGeneration(
                status="provider_error",
                raw_output=None,
                solution=None,
                attempt_count=1,
                attempt_outcomes=("provider_error",),
                error=exc,
            )
        if fixture_name is None:
            error = ProviderResponseError(
                "no public-data-only baseline Mock fixture exists for "
                f"problem_id {problem.problem_id!r}"
            )
            return SolutionGeneration(
                status="provider_error",
                raw_output=None,
                solution=None,
                attempt_count=1,
                attempt_outcomes=("provider_error",),
                error=error,
            )

        try:
            built_in_problem = load_problem_by_id(
                data_path("sample_problems.jsonl"),
                problem.problem_id,
            )
        except TraceJudgeError as exc:
            return SolutionGeneration(
                status="provider_error",
                raw_output=None,
                solution=None,
                attempt_count=1,
                attempt_outcomes=("provider_error",),
                error=exc,
            )
        if solver_public_payload(problem) != solver_public_payload(built_in_problem):
            error = ProviderResponseError(
                "baseline Mock fixtures are restricted to the bundled problem's public prompt "
                f"for problem_id {problem.problem_id!r}"
            )
            return SolutionGeneration(
                status="provider_error",
                raw_output=None,
                solution=None,
                attempt_count=1,
                attempt_outcomes=("provider_error",),
                error=error,
            )

        fixture_path = _fixture_path(fixture_name)
        try:
            raw_output = fixture_path.read_text(encoding="utf-8")
        except OSError as exc:
            return SolutionGeneration(
                status="provider_error",
                raw_output=None,
                solution=None,
                attempt_count=1,
                attempt_outcomes=("provider_error",),
                error=exc,
            )
        try:
            solution = SolutionTrace.model_validate_json(raw_output)
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

    def public_generation_config(self) -> dict[str, object]:
        return {
            "provider": self.name,
            "model": "deterministic-mock-fixtures",
            "reasoning_effort": None,
            "reasoning_effort_enabled": False,
            "timeout_seconds": None,
            "max_retries": 0,
            "endpoint_sha256": None,
        }

    def is_trusted_local_solution(
        self,
        problem: ProblemSpec,
        solution: SolutionTrace,
    ) -> bool:
        fixture_name = self._fixture_name(problem)

        if fixture_name is None:
            return False
        try:
            built_in_problem = load_problem_by_id(
                data_path("sample_problems.jsonl"),
                problem.problem_id,
            )
        except TraceJudgeError:
            return False
        return problem == built_in_problem and solution == _load_fixture(fixture_name)

    def _fallback_solution(self, problem: ProblemSpec) -> SolutionTrace:
        return SolutionTrace(
            problem_id=problem.problem_id,
            requirement_understanding=(f"[mock fallback] 题目要求：{problem.requirement}"),
            design_summary=(
                "[mock fallback] 未为该题目预置示例解答，直接复用题目自带的参考实现，"
                "仅用于打通链路，不代表真实模型输出。"
            ),
            edge_cases_considered=[],
            implementation_steps=[
                ImplementationStep(
                    step_id="S1",
                    content="直接采用题目提供的参考实现",
                    related_requirements=[r.requirement_id for r in problem.requirements],
                    expected_code_behavior=None,
                )
            ],
            declared_time_complexity=None,
            declared_space_complexity=None,
            code=problem.reference_code,
        )

    async def evaluate_process(
        self,
        problem: ProblemSpec,
        solution: SolutionTrace,
        static_evidence: StaticEvidence,
        execution_result: ExecutionSummary,
    ) -> ProcessAssessment:
        functional_correct = execution_result.runtime_status == "completed" and (
            not execution_result.results or execution_result.all_passed()
        )

        empty_req_ids = [
            r.requirement_id for r in problem.requirements if claims_empty_input_handling(r.content)
        ]
        empty_steps = [
            step
            for step in solution.implementation_steps
            if claims_explicit_empty_input_branch(step.content)
        ]

        if empty_steps and not static_evidence.has_empty_input_check:
            step = empty_steps[0]
            violated = (
                step.related_requirements[0]
                if step.related_requirements
                else (empty_req_ids[0] if empty_req_ids else None)
            )
            failing = [
                r
                for r in execution_result.failures()
                if violated is None or violated in r.related_requirements
            ]
            secondary = [ErrorType.C01_BOUNDARY_ERROR] if failing else []
            return ProcessAssessment(
                reasoning_correct=True,
                plan_code_aligned=False,
                functional_correct=functional_correct,
                process_correct=False,
                first_faulty_layer="alignment",
                first_faulty_step=step.step_id,
                affected_steps=[
                    s.step_id
                    for s in solution.implementation_steps[
                        solution.implementation_steps.index(step) :
                    ]
                ],
                violated_requirement=violated,
                code_span=function_code_span(static_evidence),
                error_type=ErrorType.A01_PLAN_CODE_MISMATCH,
                secondary_error_types=secondary,
                explanation=(
                    f"步骤 {step.step_id} 声称处理空输入（“{step.content}”），"
                    "但静态分析未在代码中找到对应的空输入判断分支（如 `if not x` 或 "
                    "`len(x) == 0`）。这是一处计划—代码不一致。"
                ),
                confidence=0.85,
            )

        set_steps = [
            step
            for step in solution.implementation_steps
            if any(k in step.content for k in _SET_KEYWORDS)
        ]
        if set_steps and "set" not in static_evidence.data_structures_used:
            step = set_steps[0]
            violated = step.related_requirements[0] if step.related_requirements else None
            return ProcessAssessment(
                reasoning_correct=True,
                plan_code_aligned=False,
                functional_correct=functional_correct,
                process_correct=False,
                first_faulty_layer="alignment",
                first_faulty_step=step.step_id,
                affected_steps=[step.step_id],
                violated_requirement=violated,
                code_span=function_code_span(static_evidence),
                error_type=ErrorType.A01_PLAN_CODE_MISMATCH,
                secondary_error_types=[],
                explanation=(
                    f"步骤 {step.step_id} 声称使用集合（set）实现，"
                    "但静态分析未在代码中检测到 set 的使用。"
                ),
                confidence=0.7,
            )

        if (
            _claims_constant_time(solution.declared_time_complexity)
            and static_evidence.input_dependent_loop_count > 0
        ):
            return ProcessAssessment(
                reasoning_correct=False,
                plan_code_aligned=False,
                functional_correct=functional_correct,
                process_correct=False,
                first_faulty_layer="reasoning",
                first_faulty_step=None,
                affected_steps=[],
                violated_requirement=None,
                code_span=function_code_span(static_evidence),
                error_type=ErrorType.P03_COMPLEXITY_MISMATCH,
                secondary_error_types=[],
                explanation=(
                    f"声明的时间复杂度为 {solution.declared_time_complexity}，"
                    f"但代码中检测到 {static_evidence.input_dependent_loop_count} 个输入相关循环，"
                    "与声明的复杂度明显不符。"
                ),
                confidence=0.6,
            )

        if not functional_correct:
            failing = execution_result.failures()
            first_fail = failing[0] if failing else None
            if first_fail and first_fail.timed_out:
                error_type = ErrorType.E02_TIMEOUT_OR_RESOURCE_ERROR
            elif first_fail and first_fail.exception_type:
                error_type = ErrorType.E01_RUNTIME_EXCEPTION
            else:
                error_type = ErrorType.E03_WRONG_OUTPUT
            return ProcessAssessment(
                reasoning_correct=True,
                plan_code_aligned=True,
                functional_correct=False,
                process_correct=False,
                first_faulty_layer="execution",
                first_faulty_step=None,
                affected_steps=[],
                violated_requirement=(
                    first_fail.related_requirements[0]
                    if first_fail and first_fail.related_requirements
                    else None
                ),
                code_span=function_code_span(static_evidence),
                error_type=error_type,
                secondary_error_types=[],
                explanation=(
                    "未在 reasoning 与代码之间发现明显的声明—实现不一致，"
                    "但执行测试未能全部通过，判定为实现/执行层缺陷。"
                ),
                confidence=0.5,
            )

        return ProcessAssessment(
            reasoning_correct=True,
            plan_code_aligned=True,
            functional_correct=True,
            process_correct=True,
            first_faulty_layer=None,
            first_faulty_step=None,
            affected_steps=[],
            violated_requirement=None,
            code_span=None,
            error_type=None,
            secondary_error_types=[],
            explanation="需求条款均被 reasoning 覆盖，声明的关键步骤在代码中均可找到对应实现，且全部测试通过。",
            confidence=0.9,
        )
