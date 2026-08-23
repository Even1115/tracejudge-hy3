from __future__ import annotations

import pytest
from pydantic import ValidationError

from tracejudge_hy3.schemas.evaluation import ErrorCertificate, ErrorType, ProcessAssessment
from tracejudge_hy3.schemas.problem import ProblemSpec, RequirementItem
from tracejudge_hy3.schemas.problem import TestCase as CaseSpec
from tracejudge_hy3.schemas.solution import ImplementationStep, SolutionTrace


def _minimal_problem(**overrides) -> ProblemSpec:
    payload = dict(
        problem_id="p1",
        title="t",
        requirement="r",
        function_signature="def f(x):",
        requirements=[],
        visible_test_cases=[],
        hidden_test_cases=[],
        challenge_test_cases=[],
        reference_code="def f(x):\n    return x\n",
        difficulty="easy",
        source="self_constructed_mvp_fixture",
        tags=[],
    )
    payload.update(overrides)
    return ProblemSpec.model_validate(payload)


def test_problem_spec_valid_roundtrip():
    problem = _minimal_problem(
        requirements=[RequirementItem(requirement_id="R1", content="c")],
        visible_test_cases=[CaseSpec(case_id="v1", args=[1], expected=1, category="visible")],
    )
    assert problem.function_name == "f"
    dumped = problem.model_dump_json()
    reloaded = ProblemSpec.model_validate_json(dumped)
    assert reloaded == problem


def test_problem_spec_rejects_bad_difficulty():
    with pytest.raises(ValidationError):
        _minimal_problem(difficulty="impossible")


def test_test_case_default_factories_are_independent():
    t1 = CaseSpec(case_id="a", expected=1, category="visible")
    t2 = CaseSpec(case_id="b", expected=2, category="visible")
    t1.args.append(99)
    assert t2.args == []
    assert t1.kwargs is not t2.kwargs


def test_function_signature_name_extraction():
    problem = _minimal_problem(
        function_signature="def deduplicate_preserve_order(items: list) -> list:"
    )
    assert problem.function_name == "deduplicate_preserve_order"


def test_test_case_requires_expected_even_when_none_is_allowed():
    with pytest.raises(ValidationError):
        CaseSpec(case_id="missing", category="visible")
    assert CaseSpec(case_id="none", expected=None, category="visible").expected is None


def test_problem_rejects_duplicate_requirement_and_case_ids():
    with pytest.raises(ValidationError, match="requirement_id values must be unique"):
        _minimal_problem(
            requirements=[
                RequirementItem(requirement_id="R1", content="a"),
                RequirementItem(requirement_id="R1", content="b"),
            ]
        )

    duplicate = CaseSpec(case_id="same", expected=1, category="visible")
    with pytest.raises(ValidationError, match="duplicate test case_id"):
        _minimal_problem(visible_test_cases=[duplicate, duplicate])


def test_problem_rejects_misfiled_category_and_unknown_requirement_link():
    with pytest.raises(ValidationError, match="declares category 'hidden'"):
        _minimal_problem(
            visible_test_cases=[CaseSpec(case_id="wrong", expected=1, category="hidden")]
        )

    with pytest.raises(ValidationError, match="unknown requirement IDs"):
        _minimal_problem(
            visible_test_cases=[
                CaseSpec(
                    case_id="unknown",
                    expected=1,
                    category="visible",
                    related_requirements=["R404"],
                )
            ]
        )


def test_solution_trace_requires_code():
    with pytest.raises(ValidationError):
        SolutionTrace(
            problem_id="p1",
            requirement_understanding="u",
            design_summary="d",
            edge_cases_considered=[],
            implementation_steps=[ImplementationStep(step_id="S1", content="c")],
            declared_time_complexity=None,
            declared_space_complexity=None,
        )


def test_solution_trace_rejects_duplicate_step_ids():
    with pytest.raises(ValidationError, match="step_id values must be unique"):
        SolutionTrace(
            problem_id="p1",
            requirement_understanding="u",
            design_summary="d",
            implementation_steps=[
                ImplementationStep(step_id="S1", content="a"),
                ImplementationStep(step_id="S1", content="b"),
            ],
            code="def f():\n    return None\n",
        )


def test_process_assessment_error_type_enum():
    assessment = ProcessAssessment(
        functional_correct=False,
        error_type="A01_PLAN_CODE_MISMATCH",
        explanation="x",
    )
    assert assessment.error_type == ErrorType.A01_PLAN_CODE_MISMATCH


def test_error_certificate_requires_valid_verdict():
    with pytest.raises(ValidationError):
        ErrorCertificate(verdict="maybe")
