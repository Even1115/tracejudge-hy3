from __future__ import annotations

from tracejudge_hy3.evaluator.evidence import build_error_certificate
from tracejudge_hy3.schemas.evaluation import Counterexample, ErrorType, ProcessAssessment
from tracejudge_hy3.schemas.execution import ExecutionSummary
from tracejudge_hy3.schemas.execution import TestExecutionResult as ExecutionCaseResult


def _assessment(**overrides) -> ProcessAssessment:
    base = dict(
        reasoning_correct=True,
        plan_code_aligned=False,
        functional_correct=False,
        process_correct=False,
        first_faulty_layer="alignment",
        first_faulty_step="S1",
        affected_steps=["S1"],
        violated_requirement="R1",
        code_span=None,
        error_type=ErrorType.A01_PLAN_CODE_MISMATCH,
        explanation="test",
        confidence=0.8,
    )
    base.update(overrides)
    return ProcessAssessment(**base)


def _execution(results: list[ExecutionCaseResult]) -> ExecutionSummary:
    return ExecutionSummary(
        problem_id="p",
        function_name="f",
        sandbox_backend="trusted-local",
        results=results,
        runtime_status="completed",
    )


def test_no_error_type_yields_no_certificate():
    assessment = _assessment(error_type=None, functional_correct=True, process_correct=True)
    execution = _execution([])
    cert = build_error_certificate(assessment, execution, counterexample=None, rule_fired=False)
    assert cert is None


def test_clean_follow_up_clears_a_previous_certificate():
    previous = build_error_certificate(
        _assessment(),
        _execution([]),
        counterexample=None,
        rule_fired=True,
    )
    assert previous is not None
    passing = ExecutionCaseResult(
        case_id="h1",
        category="hidden",
        passed=True,
        actual_output=0.0,
        expected_output=0.0,
        related_requirements=["R1"],
    )
    clean_assessment = _assessment(
        functional_correct=True,
        process_correct=True,
        plan_code_aligned=True,
        first_faulty_layer=None,
        first_faulty_step=None,
        affected_steps=[],
        violated_requirement=None,
        error_type=None,
        explanation="修复后复核无异常。",
    )

    cleared = build_error_certificate(
        clean_assessment,
        _execution([passing]),
        counterexample=None,
        rule_fired=False,
        previous_certificate=previous,
    )

    assert cleared is not None
    assert cleared.verdict == "cleared"
    assert cleared.error_type == previous.error_type
    assert cleared.counterexample is None


def test_previous_certificate_is_not_cleared_without_passing_recheck():
    previous = build_error_certificate(
        _assessment(),
        _execution([]),
        counterexample=None,
        rule_fired=True,
    )
    assert previous is not None
    clean_assessment = _assessment(
        functional_correct=True,
        process_correct=True,
        error_type=None,
    )

    cleared = build_error_certificate(
        clean_assessment,
        _execution([]),
        counterexample=None,
        rule_fired=False,
        previous_certificate=previous,
    )

    assert cleared is None


def test_counterexample_present_yields_confirmed_bug():
    assessment = _assessment()
    execution = _execution([])
    ce = Counterexample(
        args=[[]],
        expected=0.0,
        candidate_exception="ZeroDivisionError",
        source="boundary_candidate",
    )
    cert = build_error_certificate(assessment, execution, counterexample=ce, rule_fired=True)
    assert cert is not None
    assert cert.verdict == "confirmed_bug"
    assert cert.counterexample is ce


def test_failing_hidden_test_without_counterexample_yields_confirmed_bug():
    assessment = _assessment()
    failing = ExecutionCaseResult(
        case_id="h1",
        category="hidden",
        passed=False,
        actual_output=None,
        expected_output=0.0,
        exception_type="ZeroDivisionError",
        exception_message="division by zero",
        related_requirements=["R1"],
    )
    execution = _execution([failing])
    cert = build_error_certificate(assessment, execution, counterexample=None, rule_fired=True)
    assert cert is not None
    assert cert.verdict == "confirmed_bug"
    assert cert.counterexample is None


def test_unrelated_hidden_failure_does_not_confirm_requirement_assessment():
    assessment = _assessment(violated_requirement="R1")
    unrelated_failure = ExecutionCaseResult(
        case_id="h-r2",
        category="hidden",
        passed=False,
        actual_output=1,
        expected_output=2,
        related_requirements=["R2"],
    )
    execution = _execution([unrelated_failure])

    cert = build_error_certificate(
        assessment,
        execution,
        counterexample=None,
        rule_fired=True,
    )

    assert cert is not None
    assert cert.verdict == "strongly_supported"
    assert cert.counterexample is None


def test_unrelated_reused_test_counterexample_is_not_confirming_evidence():
    assessment = _assessment(violated_requirement="R1")
    unrelated_failure = ExecutionCaseResult(
        case_id="c-r2",
        category="challenge",
        passed=False,
        actual_output=1,
        expected_output=2,
        related_requirements=["R2"],
    )
    execution = _execution([unrelated_failure])
    unrelated_counterexample = Counterexample(
        args=[1],
        expected=2,
        candidate_output=1,
        source="challenge_test",
    )

    cert = build_error_certificate(
        assessment,
        execution,
        counterexample=unrelated_counterexample,
        rule_fired=True,
    )

    assert cert is not None
    assert cert.verdict == "strongly_supported"
    assert cert.counterexample is None


def test_requirement_free_assessment_preserves_generic_failure_behavior():
    assessment = _assessment(violated_requirement=None)
    untagged_failure = ExecutionCaseResult(
        case_id="h1",
        category="hidden",
        passed=False,
        actual_output=1,
        expected_output=2,
    )
    execution = _execution([untagged_failure])

    cert = build_error_certificate(
        assessment,
        execution,
        counterexample=None,
        rule_fired=True,
    )

    assert cert is not None
    assert cert.verdict == "confirmed_bug"


def test_rule_fired_without_dynamic_evidence_is_strongly_supported():
    assessment = _assessment()
    execution = _execution([])
    cert = build_error_certificate(assessment, execution, counterexample=None, rule_fired=True)
    assert cert is not None
    assert cert.verdict == "strongly_supported"


def test_llm_only_without_rule_or_counterexample_is_unverified_suspicion():
    assessment = _assessment()
    execution = _execution([])
    cert = build_error_certificate(assessment, execution, counterexample=None, rule_fired=False)
    assert cert is not None
    assert cert.verdict == "unverified_suspicion"


def test_failing_visible_test_alone_does_not_count_as_confirmed():
    assessment = _assessment()
    failing_visible = ExecutionCaseResult(
        case_id="v1", category="visible", passed=False, actual_output=1, expected_output=2
    )
    execution = _execution([failing_visible])
    cert = build_error_certificate(assessment, execution, counterexample=None, rule_fired=True)
    assert cert is not None
    assert cert.verdict == "strongly_supported"
