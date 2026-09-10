"""Aggregate functional-evidence adapter for the full-system pilot method.

The pilot's official MBPP evidence is an aggregate base/plus outcome bound to
the frozen candidate by content hash -- deliberately NOT a per-test
ExecutionSummary. This adapter lets the full-system method reuse the shared
rule/combination machinery without fabricating per-case test records:

- ``aggregate_execution_summary`` carries an explicit ``results=[]`` summary
  whose backend name marks it as aggregate; rule checks that require per-case
  failures therefore find nothing and stay silent.
- ``apply_aggregate_functional`` is the single, explicit place where the
  aggregate pass/fail verdict enters the combined assessment, using the same
  three-valued joint logic as evaluator/alignment.py.

The per-case evidence path (BoundExecutionEvidence in pipeline/evaluate_solution)
is untouched and keeps its own binding validation.
"""

from __future__ import annotations

from tracejudge_hy3.process_eval_v2.contracts import FunctionalEvidence, PilotInput
from tracejudge_hy3.schemas.evaluation import ProcessAssessment
from tracejudge_hy3.schemas.execution import ExecutionSummary
from tracejudge_hy3.schemas.problem import ProblemSpec, RequirementItem

#: Explicit, greppable marker: this summary is aggregate-only, never per-case.
AGGREGATE_BACKEND = "evalplus-official-aggregate-no-per-case"


def public_function_name(item: PilotInput) -> str:
    """Best-effort 'def foo(...):' -> 'foo', mirroring ProblemSpec.function_name."""

    signature = item.problem.function_signature.strip()
    if signature.startswith("def "):
        signature = signature[len("def ") :]
    return signature.split("(", 1)[0].strip()


def build_scaffold_problem(item: PilotInput) -> ProblemSpec:
    """Internal scaffold for the rule layer; never shown to the Judge.

    No test cases and no reference code exist in the pilot materials, so both
    stay empty -- the Judge prompt is built separately and never receives this
    object's ``reference_code``.
    """

    return ProblemSpec(
        problem_id=item.item_id,
        title=item.problem.title,
        requirement=item.problem.requirement,
        function_signature=item.problem.function_signature,
        requirements=[
            RequirementItem(requirement_id=r.requirement_id, content=r.content)
            for r in item.problem.requirements
        ],
        reference_code="",
        difficulty="unknown",
        source="pilot-frozen-candidate",
    )


def aggregate_execution_summary(item: PilotInput) -> ExecutionSummary:
    """Aggregate-only execution view: zero test records, verdict applied later."""

    return ExecutionSummary(
        problem_id=item.item_id,
        function_name=public_function_name(item),
        sandbox_backend=AGGREGATE_BACKEND,
        results=[],
        runtime_status="completed",
    )


def apply_aggregate_functional(
    assessment: ProcessAssessment, evidence: FunctionalEvidence
) -> ProcessAssessment:
    """Set functional_correct from the bound aggregate verdict and recompute the joint flag.

    Uses the same three-valued rule as combine_assessment: any False makes the
    joint conclusion False, all True makes it True, otherwise it stays None.
    """

    functional = evidence.functional_correct
    values = (assessment.reasoning_correct, assessment.plan_code_aligned, functional)
    if any(value is False for value in values):
        joint: bool | None = False
    elif all(value is True for value in values):
        joint = True
    else:
        joint = None
    return ProcessAssessment.model_validate(
        assessment.model_dump(mode="json")
        | {"functional_correct": functional, "process_correct": joint}
    )
