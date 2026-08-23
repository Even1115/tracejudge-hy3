"""Combine rule-based evidence and the LLM judge's opinion into one ProcessAssessment.

Priority: functional_correct always comes from execution results (ground
truth, never from either judge's opinion). Where a deterministic rule fires,
it takes priority over the LLM's opinion for layer/step/error-type -- this is
the concrete mechanism by which the project avoids treating a single LLM
judgement as authoritative. Any disagreement from the LLM is preserved in the
explanation/secondary_error_types rather than silently dropped.
"""

from __future__ import annotations

from tracejudge_hy3.evaluator.rule_based import evaluate_alignment_rules
from tracejudge_hy3.schemas.evaluation import ProcessAssessment
from tracejudge_hy3.schemas.execution import ExecutionSummary, StaticEvidence
from tracejudge_hy3.schemas.problem import ProblemSpec
from tracejudge_hy3.schemas.solution import SolutionTrace


def _functional_correct(execution_result: ExecutionSummary) -> bool:
    if execution_result.runtime_status != "completed":
        return False
    return execution_result.all_passed()


def combine_assessment(
    problem: ProblemSpec,
    solution: SolutionTrace,
    static_evidence: StaticEvidence,
    execution_result: ExecutionSummary,
    llm_assessment: ProcessAssessment | None,
    rule_assessment: ProcessAssessment | None = None,
) -> ProcessAssessment:
    if rule_assessment is None:
        rule_assessment = evaluate_alignment_rules(
            problem, solution, static_evidence, execution_result
        )
    functional_correct = _functional_correct(execution_result)

    primary = rule_assessment if rule_assessment is not None else llm_assessment
    source = (
        "rule" if rule_assessment is not None else ("llm" if llm_assessment is not None else "none")
    )

    if primary is None:
        explanation = "无可用的规则或 LLM 过程判断信号；"
        explanation += (
            "全部测试通过，但不能仅据此推断 reasoning 或计划—代码对齐正确。"
            if functional_correct
            else "存在未通过的测试，且过程层结论不可计算。"
        )
        return ProcessAssessment(
            reasoning_correct=None,
            plan_code_aligned=None,
            functional_correct=functional_correct,
            process_correct=None,
            first_faulty_layer=None,
            first_faulty_step=None,
            affected_steps=[],
            violated_requirement=None,
            code_span=None,
            error_type=None,
            secondary_error_types=[],
            explanation=explanation,
            confidence=0.5,
        )

    reasoning_correct = primary.reasoning_correct
    plan_code_aligned = primary.plan_code_aligned
    if source == "rule" and llm_assessment is not None:
        if reasoning_correct is None:
            reasoning_correct = llm_assessment.reasoning_correct
        if plan_code_aligned is None:
            plan_code_aligned = llm_assessment.plan_code_aligned

    secondary = list(primary.secondary_error_types)
    explanation = primary.explanation
    confidence = primary.confidence

    if source == "rule" and llm_assessment is not None:
        if (
            llm_assessment.error_type is not None
            and llm_assessment.error_type != primary.error_type
        ):
            explanation += f" [Hy3 判断存在差异：{llm_assessment.explanation}]"
            if llm_assessment.error_type not in secondary:
                secondary.append(llm_assessment.error_type)
        elif llm_assessment.error_type == primary.error_type and primary.error_type is not None:
            explanation += " [与 Hy3 判断一致]"
            confidence = round(min(1.0, (confidence or 0.5) + 0.05), 2)

    process_correct: bool | None
    if reasoning_correct is not None and plan_code_aligned is not None:
        process_correct = bool(reasoning_correct and plan_code_aligned and functional_correct)
    elif not functional_correct:
        process_correct = False
    else:
        process_correct = None

    return ProcessAssessment(
        reasoning_correct=reasoning_correct,
        plan_code_aligned=plan_code_aligned,
        functional_correct=functional_correct,
        process_correct=process_correct,
        first_faulty_layer=primary.first_faulty_layer,
        first_faulty_step=primary.first_faulty_step,
        affected_steps=primary.affected_steps,
        violated_requirement=primary.violated_requirement,
        code_span=primary.code_span,
        error_type=primary.error_type,
        secondary_error_types=secondary,
        explanation=explanation,
        confidence=confidence,
    )
