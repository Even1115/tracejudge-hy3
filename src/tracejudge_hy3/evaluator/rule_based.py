"""Deterministic, LLM-independent alignment checks.

These rules only fire on concrete static/execution evidence -- they never rely
on an LLM's opinion -- and exist specifically to cross-check (and, where they
disagree, take priority over) the LLM judge output, per the design doc's goal
of reducing pure LLM-as-judge false positives. A rule returning None means "no
rule-based signal", not "everything is fine": the caller (evaluator/
alignment.py) falls back to the LLM judgement in that case.
"""

from __future__ import annotations

from typing import Any

from tracejudge_hy3.evaluator.claims import claims_explicit_empty_input_branch
from tracejudge_hy3.evaluator.code_location import best_available_code_span, function_code_span
from tracejudge_hy3.schemas.evaluation import ErrorType, ProcessAssessment
from tracejudge_hy3.schemas.execution import ExecutionSummary, StaticEvidence, TestExecutionResult
from tracejudge_hy3.schemas.problem import ProblemSpec, TestCase
from tracejudge_hy3.schemas.solution import SolutionTrace

_SET_KEYWORDS = ("集合", "使用 set", "使用set", "hash set", "哈希集合")
_SINGLE_PASS_KEYWORDS = ("单次遍历", "一次遍历", "一遍遍历", "single pass", "one pass")
_BOUNDARY_EXCEPTIONS = {"ZeroDivisionError", "IndexError", "KeyError", "StopIteration"}


def _is_empty_like(value: Any) -> bool:
    return isinstance(value, list | str | dict | tuple) and len(value) == 0


def _test_case_by_id(problem: ProblemSpec) -> dict[str, TestCase]:
    return {tc.case_id: tc for tc in problem.all_test_cases()}


def check_empty_input_claim(
    solution: SolutionTrace, static_evidence: StaticEvidence
) -> ProcessAssessment | None:
    if not static_evidence.ast_parse_ok:
        return None
    empty_steps = [
        step
        for step in solution.implementation_steps
        if claims_explicit_empty_input_branch(step.content)
    ]
    if not empty_steps or static_evidence.has_empty_input_check:
        return None

    step = empty_steps[0]
    idx = solution.implementation_steps.index(step)
    violated = step.related_requirements[0] if step.related_requirements else None
    return ProcessAssessment(
        reasoning_correct=None,
        plan_code_aligned=False,
        functional_correct=False,
        process_correct=False,
        first_faulty_layer="alignment",
        first_faulty_step=step.step_id,
        affected_steps=[s.step_id for s in solution.implementation_steps[idx:]],
        violated_requirement=violated,
        code_span=function_code_span(static_evidence),
        error_type=ErrorType.A01_PLAN_CODE_MISMATCH,
        secondary_error_types=[],
        explanation=(
            f"[rule] 步骤 {step.step_id}（“{step.content}”）声称处理空输入，"
            "但 AST 静态分析未发现空输入判断分支（如 `if not x` / `len(x) == 0`）。"
        ),
        confidence=0.9,
    )


def check_set_usage_claim(
    solution: SolutionTrace, static_evidence: StaticEvidence
) -> ProcessAssessment | None:
    if not static_evidence.ast_parse_ok:
        return None
    set_steps = [
        step
        for step in solution.implementation_steps
        if any(k in step.content for k in _SET_KEYWORDS)
    ]
    if not set_steps or "set" in static_evidence.data_structures_used:
        return None

    step = set_steps[0]
    violated = step.related_requirements[0] if step.related_requirements else None
    return ProcessAssessment(
        reasoning_correct=None,
        plan_code_aligned=False,
        functional_correct=False,
        process_correct=False,
        first_faulty_layer="alignment",
        first_faulty_step=step.step_id,
        affected_steps=[step.step_id],
        violated_requirement=violated,
        code_span=function_code_span(static_evidence),
        error_type=ErrorType.A01_PLAN_CODE_MISMATCH,
        secondary_error_types=[],
        explanation=(
            f"[rule] 步骤 {step.step_id} 声称使用集合（set），"
            "但静态分析在代码中未检测到 set 字面量或 set()/frozenset() 调用。"
        ),
        confidence=0.75,
    )


def check_single_pass_claim(
    solution: SolutionTrace, static_evidence: StaticEvidence
) -> ProcessAssessment | None:
    if not static_evidence.ast_parse_ok:
        return None
    claim_steps = [
        step
        for step in [*solution.implementation_steps]
        if any(k in step.content for k in _SINGLE_PASS_KEYWORDS)
    ]
    claims_in_summary = any(k in solution.design_summary for k in _SINGLE_PASS_KEYWORDS)
    if (not claim_steps and not claims_in_summary) or static_evidence.max_loop_nesting_depth < 2:
        return None

    step_id = claim_steps[0].step_id if claim_steps else None
    violated = (
        claim_steps[0].related_requirements[0]
        if claim_steps and claim_steps[0].related_requirements
        else None
    )
    return ProcessAssessment(
        reasoning_correct=None,
        plan_code_aligned=False,
        functional_correct=False,
        process_correct=False,
        first_faulty_layer="alignment",
        first_faulty_step=step_id,
        affected_steps=[step_id] if step_id else [],
        violated_requirement=violated,
        code_span=function_code_span(static_evidence),
        error_type=ErrorType.A01_PLAN_CODE_MISMATCH,
        secondary_error_types=[],
        explanation=(
            "[rule] reasoning 声称采用单次遍历（O(n)）方法，"
            f"但静态分析检测到 {static_evidence.max_loop_nesting_depth} 层嵌套循环。"
        ),
        confidence=0.7,
    )


def check_complexity_declaration(
    solution: SolutionTrace, static_evidence: StaticEvidence
) -> ProcessAssessment | None:
    if not static_evidence.ast_parse_ok:
        return None
    declared = solution.declared_time_complexity
    if not declared:
        return None
    declared_lower = declared.lower().replace(" ", "")
    claims_constant_time = any(
        marker in declared_lower for marker in ("o(1)", "常数时间", "constanttime")
    )
    if not claims_constant_time or static_evidence.input_dependent_loop_count == 0:
        return None
    return ProcessAssessment(
        reasoning_correct=False,
        plan_code_aligned=None,
        functional_correct=False,
        process_correct=False,
        first_faulty_layer="reasoning",
        first_faulty_step=None,
        affected_steps=[],
        violated_requirement=None,
        code_span=function_code_span(static_evidence),
        error_type=ErrorType.P03_COMPLEXITY_MISMATCH,
        secondary_error_types=[],
        explanation=(
            f"[rule] 声明时间复杂度为 {declared}，"
            f"但代码中检测到 {static_evidence.input_dependent_loop_count} 个输入相关循环，"
            "与声明的复杂度不一致。"
        ),
        confidence=0.65,
    )


def _classify_execution_failure(
    failure: TestExecutionResult, test_case: TestCase | None
) -> ErrorType:
    if failure.timed_out:
        return ErrorType.E02_TIMEOUT_OR_RESOURCE_ERROR
    if failure.exception_type in _BOUNDARY_EXCEPTIONS:
        return ErrorType.C01_BOUNDARY_ERROR
    if test_case is not None and any(_is_empty_like(a) for a in test_case.args):
        return ErrorType.C01_BOUNDARY_ERROR
    if failure.exception_type:
        return ErrorType.E01_RUNTIME_EXCEPTION
    return ErrorType.E03_WRONG_OUTPUT


def check_execution_evidence(
    problem: ProblemSpec,
    execution_result: ExecutionSummary,
    static_evidence: StaticEvidence,
) -> ProcessAssessment | None:
    if execution_result.runtime_status == "backend_error":
        return None
    if execution_result.runtime_status != "completed":
        setup_error = execution_result.setup_error or ""
        if execution_result.runtime_status == "syntax_error" or (
            execution_result.runtime_status == "import_error"
            and "not found in candidate code" in setup_error
        ):
            error_type = ErrorType.C04_INTERFACE_OR_FORMAT_ERROR
        else:
            error_type = ErrorType.E01_RUNTIME_EXCEPTION
        return ProcessAssessment(
            reasoning_correct=None,
            plan_code_aligned=None,
            functional_correct=False,
            process_correct=False,
            first_faulty_layer="implementation",
            first_faulty_step=None,
            affected_steps=[],
            violated_requirement=None,
            code_span=best_available_code_span(static_evidence, execution_result),
            error_type=error_type,
            secondary_error_types=[],
            explanation=f"[rule] 沙盒执行未成功完成：{execution_result.runtime_status} - {execution_result.setup_error}",
            confidence=0.95,
        )

    failures = execution_result.failures()
    if not failures:
        return None

    by_id = _test_case_by_id(problem)
    first_fail = failures[0]
    test_case = by_id.get(first_fail.case_id)
    error_type = _classify_execution_failure(first_fail, test_case)
    violated = first_fail.related_requirements[0] if first_fail.related_requirements else None

    return ProcessAssessment(
        reasoning_correct=None,
        plan_code_aligned=None,
        functional_correct=False,
        process_correct=False,
        first_faulty_layer="implementation",
        first_faulty_step=None,
        affected_steps=[],
        violated_requirement=violated,
        code_span=best_available_code_span(static_evidence, execution_result),
        error_type=error_type,
        secondary_error_types=[],
        explanation=(
            f"[rule] 测试 {first_fail.case_id}（{first_fail.category}）失败："
            f"期望 {first_fail.expected_output!r}，实际 {first_fail.actual_output!r}"
            + (
                f"，异常 {first_fail.exception_type}: {first_fail.exception_message}"
                if first_fail.exception_type
                else ""
            )
        ),
        confidence=0.8,
    )


def evaluate_alignment_rules(
    problem: ProblemSpec,
    solution: SolutionTrace,
    static_evidence: StaticEvidence,
    execution_result: ExecutionSummary,
) -> ProcessAssessment | None:
    """Run rules in priority order; the first alignment-layer rule that fires wins.

    Alignment-layer mismatches (reasoning claims vs. code) are checked before
    falling back to raw execution-failure classification, since they carry a
    more specific, causally-earlier explanation of the same underlying defect.
    """

    findings: list[ProcessAssessment] = []
    for check in (
        check_empty_input_claim,
        check_set_usage_claim,
        check_single_pass_claim,
    ):
        result = check(solution, static_evidence)
        if result is not None:
            findings.append(result)

    complexity_result = check_complexity_declaration(solution, static_evidence)
    if complexity_result is not None:
        findings.append(complexity_result)

    execution_result_finding = check_execution_evidence(problem, execution_result, static_evidence)
    if execution_result_finding is not None:
        findings.append(execution_result_finding)

    if not findings:
        return None

    layer_order = {
        "requirement": 0,
        "reasoning": 1,
        "alignment": 2,
        "implementation": 3,
        "execution": 4,
    }
    step_order = {step.step_id: index for index, step in enumerate(solution.implementation_steps)}
    findings.sort(
        key=lambda item: (
            layer_order.get(item.first_faulty_layer or "execution", 99),
            step_order.get(item.first_faulty_step or "", 10**9),
        )
    )
    primary = findings[0]
    for additional in findings[1:]:
        if additional.error_type and additional.error_type != primary.error_type:
            if additional.error_type not in primary.secondary_error_types:
                primary.secondary_error_types.append(additional.error_type)
        primary.explanation += f" [次级规则证据：{additional.explanation}]"
    if static_evidence.suspicious_hardcoding:
        if ErrorType.C05_HARDCODED_SHORTCUT not in primary.secondary_error_types:
            primary.secondary_error_types.append(ErrorType.C05_HARDCODED_SHORTCUT)
        primary.explanation += (
            f" [硬编码启发式证据：{static_evidence.suspicious_hardcoding_reason}]"
        )
    return primary
