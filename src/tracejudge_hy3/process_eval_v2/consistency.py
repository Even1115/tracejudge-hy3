"""Validate public-process projections before leaving the provider retry boundary."""

from tracejudge_hy3.process_eval_v2.contracts import public_process_correct
from tracejudge_hy3.schemas.evaluation import ProcessAssessment

CONSISTENCY_VERSION = "public-process-consistency-v2"


def public_assessment(assessment: ProcessAssessment) -> ProcessAssessment:
    """Project function-independent signals, retaining and revalidating all evidence."""
    values = assessment.model_dump(mode="json")
    values.update(
        functional_correct=None,
        process_correct=public_process_correct(
            assessment.reasoning_correct, assessment.plan_code_aligned
        ),
    )
    projected = ProcessAssessment.model_validate(values)
    if projected.process_correct is True and any(
        (
            projected.error_type is not None,
            projected.first_faulty_layer is not None,
            projected.first_faulty_step is not None,
            projected.first_faulty_location is not None,
            bool(projected.secondary_error_types),
        )
    ):
        raise ValueError("public process is true but error classification/localization is present")
    return projected


def check_public_consistency(assessment: ProcessAssessment) -> None:
    expected = public_process_correct(assessment.reasoning_correct, assessment.plan_code_aligned)
    if assessment.process_correct is not None and assessment.process_correct is not expected:
        raise ValueError(
            "process_correct conflicts with reasoning_correct AND plan_code_aligned; "
            "reconsider the judgment and evidence, do not just remove the location"
        )
    public_assessment(assessment)
