"""Build the final ErrorCertificate from a ProcessAssessment + counterexample search result.

Verdict rules (see docs/architecture.md for the full rationale):
- confirmed_bug: an executable counterexample was found, OR an already-executed
  hidden/challenge test independently reproduces the violation.
- strongly_supported: a deterministic rule fired (static + execution evidence),
  but no independent counterexample was found.
- unverified_suspicion: only the LLM judge raised a concern; no rule fired and
  no counterexample was found.
- cleared: a later clean assessment and complete passing execution explicitly
  re-check a previous certificate and show that it no longer applies.  A clean
  first run still produces no certificate.
"""

from __future__ import annotations

from tracejudge_hy3.schemas.evaluation import Counterexample, ErrorCertificate, ProcessAssessment
from tracejudge_hy3.schemas.execution import ExecutionSummary, TestExecutionResult

_TEST_SOURCE_CATEGORIES = {
    "hidden_test": "hidden",
    "challenge_test": "challenge",
}


def _failure_matches_requirement(
    failure: TestExecutionResult,
    violated_requirement: str | None,
) -> bool:
    if violated_requirement is None:
        return True
    return violated_requirement in failure.related_requirements


def _test_counterexample_matches_requirement(
    counterexample: Counterexample,
    execution_result: ExecutionSummary,
    violated_requirement: str | None,
) -> bool:
    """Correlate reused hidden/challenge evidence with the assessed requirement.

    Boundary and differential counterexamples are independently executed probes,
    so this guard only applies to counterexamples that reuse a dataset test.
    """

    category = _TEST_SOURCE_CATEGORIES.get(counterexample.source)
    if category is None or violated_requirement is None:
        return True
    return any(
        not failure.passed
        and failure.category == category
        and _failure_matches_requirement(failure, violated_requirement)
        for failure in execution_result.results
    )


def build_error_certificate(
    assessment: ProcessAssessment,
    execution_result: ExecutionSummary,
    counterexample: Counterexample | None,
    rule_fired: bool,
    previous_certificate: ErrorCertificate | None = None,
) -> ErrorCertificate | None:
    if assessment.error_type is None:
        if (
            previous_certificate is not None
            and previous_certificate.verdict != "cleared"
            and assessment.process_correct is True
            and execution_result.runtime_status == "completed"
            and execution_result.all_passed()
        ):
            return ErrorCertificate(
                verdict="cleared",
                violated_requirement=previous_certificate.violated_requirement,
                first_faulty_step=previous_certificate.first_faulty_step,
                first_faulty_layer=previous_certificate.first_faulty_layer,
                code_span=previous_certificate.code_span,
                error_type=previous_certificate.error_type,
                counterexample=None,
                supporting_evidence=[
                    *previous_certificate.supporting_evidence,
                    assessment.explanation,
                    "后续复核的过程判断无错误，且可见、隐藏与挑战测试全部通过。",
                ],
            )
        return None

    supporting_evidence: list[str] = [assessment.explanation]

    if counterexample is not None and _test_counterexample_matches_requirement(
        counterexample,
        execution_result,
        assessment.violated_requirement,
    ):
        verdict = "confirmed_bug"
        supporting_evidence.append(
            "反例已通过沙盒复现："
            f"args={counterexample.args} kwargs={counterexample.kwargs} "
            f"expected={counterexample.expected!r} "
            f"candidate_output={counterexample.candidate_output!r} "
            f"candidate_exception={counterexample.candidate_exception} "
            f"(source={counterexample.source}, minimized={counterexample.minimized})"
        )
        return ErrorCertificate(
            verdict=verdict,
            violated_requirement=assessment.violated_requirement,
            first_faulty_step=assessment.first_faulty_step,
            first_faulty_layer=assessment.first_faulty_layer,
            code_span=assessment.code_span,
            error_type=assessment.error_type,
            counterexample=counterexample,
            supporting_evidence=supporting_evidence,
        )

    relevant_failures = [
        failure
        for failure in execution_result.failures()
        if failure.category in ("hidden", "challenge")
        and _failure_matches_requirement(failure, assessment.violated_requirement)
    ]
    if relevant_failures:
        f = relevant_failures[0]
        supporting_evidence.append(
            f"{f.category} 测试 {f.case_id} 已独立执行并失败："
            f"期望 {f.expected_output!r}，实际 {f.actual_output!r}"
            + (f"，异常 {f.exception_type}: {f.exception_message}" if f.exception_type else "")
        )
        return ErrorCertificate(
            verdict="confirmed_bug",
            violated_requirement=assessment.violated_requirement,
            first_faulty_step=assessment.first_faulty_step,
            first_faulty_layer=assessment.first_faulty_layer,
            code_span=assessment.code_span,
            error_type=assessment.error_type,
            counterexample=None,
            supporting_evidence=supporting_evidence,
        )

    if rule_fired:
        verdict = "strongly_supported"
    else:
        verdict = "unverified_suspicion"

    return ErrorCertificate(
        verdict=verdict,
        violated_requirement=assessment.violated_requirement,
        first_faulty_step=assessment.first_faulty_step,
        first_faulty_layer=assessment.first_faulty_layer,
        code_span=assessment.code_span,
        error_type=assessment.error_type,
        counterexample=None,
        supporting_evidence=supporting_evidence,
    )
