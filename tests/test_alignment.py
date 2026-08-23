from __future__ import annotations

from pathlib import Path

import pytest

from tracejudge_hy3.dataset.loader import load_problem_by_id
from tracejudge_hy3.evaluator.alignment import combine_assessment
from tracejudge_hy3.evaluator.evidence import build_error_certificate
from tracejudge_hy3.evaluator.rule_based import (
    check_complexity_declaration,
    check_empty_input_claim,
    check_set_usage_claim,
    evaluate_alignment_rules,
)
from tracejudge_hy3.sandbox.trusted_local import TrustedLocalSandbox
from tracejudge_hy3.schemas.evaluation import ErrorType
from tracejudge_hy3.schemas.solution import ImplementationStep, SolutionTrace
from tracejudge_hy3.static_analysis.ast_analyzer import analyze_code

DATASET = Path(__file__).resolve().parents[1] / "data" / "sample_problems.jsonl"

FAULTY_CODE = "def safe_mean(nums):\n    return sum(nums) / len(nums)\n"
CORRECT_CODE = (
    "def safe_mean(nums):\n    if not nums:\n        return 0.0\n    return sum(nums) / len(nums)\n"
)


def _faulty_solution() -> SolutionTrace:
    return SolutionTrace(
        problem_id="safe_mean",
        requirement_understanding="u",
        design_summary="d",
        edge_cases_considered=["空列表"],
        implementation_steps=[
            ImplementationStep(
                step_id="S1",
                content="检查 nums 是否为空列表，如果为空直接返回 0.0",
                related_requirements=["R1"],
            ),
            ImplementationStep(
                step_id="S2",
                content="计算 sum(nums) / len(nums)",
                related_requirements=["R2"],
            ),
        ],
        declared_time_complexity="O(n)",
        declared_space_complexity="O(1)",
        code=FAULTY_CODE,
    )


def _correct_solution() -> SolutionTrace:
    sol = _faulty_solution()
    return sol.model_copy(update={"code": CORRECT_CODE})


@pytest.fixture
def problem():
    return load_problem_by_id(DATASET, "safe_mean")


def test_rule_flags_empty_input_mismatch():
    solution = _faulty_solution()
    evidence = analyze_code(solution.code, "safe_mean")
    result = check_empty_input_claim(solution, evidence)
    assert result is not None
    assert result.error_type == ErrorType.A01_PLAN_CODE_MISMATCH
    assert result.first_faulty_step == "S1"
    assert result.violated_requirement == "R1"
    assert result.code_span == "L1-L2"


def test_rule_does_not_flag_correct_implementation():
    solution = _correct_solution()
    evidence = analyze_code(solution.code, "safe_mean")
    assert check_empty_input_claim(solution, evidence) is None


def test_empty_result_list_initialization_is_not_an_empty_input_claim():
    solution = SolutionTrace(
        problem_id="dedupe",
        requirement_understanding="u",
        design_summary="d",
        edge_cases_considered=[],
        implementation_steps=[
            ImplementationStep(
                step_id="S1",
                content="初始化一个空集合 seen 和一个空列表 result",
                related_requirements=["R1"],
            )
        ],
        code="def dedupe(items):\n    result = []\n    return result\n",
    )
    evidence = analyze_code(solution.code, "dedupe")
    assert check_empty_input_claim(solution, evidence) is None


def test_branch_free_empty_behavior_is_not_forced_to_have_an_if():
    solution = SolutionTrace(
        problem_id="copy",
        requirement_understanding="u",
        design_summary="d",
        implementation_steps=[
            ImplementationStep(
                step_id="S1",
                content="空列表输入自然返回空列表",
                related_requirements=["R1"],
            )
        ],
        code="def copy(items):\n    return list(items)\n",
    )
    evidence = analyze_code(solution.code, "copy")
    assert check_empty_input_claim(solution, evidence) is None


def test_multivariate_complexity_is_not_misread_as_quadratic_mismatch():
    solution = SolutionTrace(
        problem_id="matrix",
        requirement_understanding="u",
        design_summary="d",
        implementation_steps=[],
        declared_time_complexity="O(n * m)",
        code=(
            "def flatten(rows):\n"
            "    result = []\n"
            "    for row in rows:\n"
            "        for item in row:\n"
            "            result.append(item)\n"
            "    return result\n"
        ),
    )
    evidence = analyze_code(solution.code, "flatten")
    assert check_complexity_declaration(solution, evidence) is None


def test_constant_complexity_allows_fixed_range_loop():
    solution = SolutionTrace(
        problem_id="repeat_three",
        requirement_understanding="u",
        design_summary="d",
        implementation_steps=[],
        declared_time_complexity="O(1)",
        code=(
            "def repeat_three(value):\n"
            "    for _ in range(3):\n"
            "        value += 1\n"
            "    return value\n"
        ),
    )
    evidence = analyze_code(solution.code, "repeat_three")
    assert evidence.loop_count == 1
    assert evidence.input_dependent_loop_count == 0
    assert check_complexity_declaration(solution, evidence) is None


def test_constant_complexity_conflicts_with_input_dependent_loop():
    solution = SolutionTrace(
        problem_id="sum_items",
        requirement_understanding="u",
        design_summary="d",
        implementation_steps=[],
        declared_time_complexity="constant time",
        code=(
            "def sum_items(items):\n"
            "    total = 0\n"
            "    for item in items:\n"
            "        total += item\n"
            "    return total\n"
        ),
    )
    evidence = analyze_code(solution.code, "sum_items")
    assessment = check_complexity_declaration(solution, evidence)
    assert evidence.input_dependent_loop_count == 1
    assert assessment is not None
    assert assessment.error_type == ErrorType.P03_COMPLEXITY_MISMATCH
    assert "1 个输入相关循环" in assessment.explanation


def test_earliest_alignment_step_wins_over_rule_declaration_order(problem):
    solution = SolutionTrace(
        problem_id="safe_mean",
        requirement_understanding="u",
        design_summary="d",
        implementation_steps=[
            ImplementationStep(
                step_id="S1",
                content="使用集合保存元素",
                related_requirements=["R1"],
            ),
            ImplementationStep(
                step_id="S2",
                content="检查输入是否为空，如果为空直接返回 0",
                related_requirements=["R1"],
            ),
        ],
        code=FAULTY_CODE,
    )
    evidence = analyze_code(solution.code, "safe_mean")
    execution = TrustedLocalSandbox(2.0).run(solution.code, "safe_mean", problem.all_test_cases())
    assessment = evaluate_alignment_rules(problem, solution, evidence, execution)
    assert assessment is not None
    assert assessment.first_faulty_step == "S1"


def test_syntax_error_is_not_misreported_as_missing_claimed_branch(problem):
    solution = _faulty_solution().model_copy(update={"code": "def safe_mean(:\n    pass\n"})
    evidence = analyze_code(solution.code, "safe_mean")
    execution = TrustedLocalSandbox(2.0).run(solution.code, "safe_mean", problem.all_test_cases())
    assessment = evaluate_alignment_rules(problem, solution, evidence, execution)
    assert assessment is not None
    assert assessment.first_faulty_layer == "implementation"
    assert assessment.error_type == ErrorType.C04_INTERFACE_OR_FORMAT_ERROR
    assert assessment.code_span == "L1"


def test_set_claim_without_set_usage_flagged():
    solution = SolutionTrace(
        problem_id="p",
        requirement_understanding="u",
        design_summary="d",
        edge_cases_considered=[],
        implementation_steps=[
            ImplementationStep(
                step_id="S1", content="使用集合记录已访问元素", related_requirements=["R1"]
            )
        ],
        declared_time_complexity="O(n)",
        declared_space_complexity="O(n)",
        code="def f(xs):\n    result = []\n    for x in xs:\n        if x not in result:\n            result.append(x)\n    return result\n",
    )
    evidence = analyze_code(solution.code, "f")
    result = check_set_usage_claim(solution, evidence)
    assert result is not None
    assert result.error_type == ErrorType.A01_PLAN_CODE_MISMATCH


def test_full_pipeline_faulty_safe_mean_detects_boundary_defect(problem):
    solution = _faulty_solution()
    evidence = analyze_code(solution.code, "safe_mean")
    backend = TrustedLocalSandbox(per_test_timeout_seconds=2.0)
    execution = backend.run(solution.code, "safe_mean", problem.all_test_cases())
    execution = execution.model_copy(update={"problem_id": problem.problem_id})

    assert not execution.all_passed()
    assert any(r.category == "visible" and r.passed for r in execution.results)
    assert any(r.category == "hidden" and not r.passed for r in execution.results)

    rule_assessment = evaluate_alignment_rules(problem, solution, evidence, execution)
    assert rule_assessment is not None
    assert rule_assessment.error_type == ErrorType.A01_PLAN_CODE_MISMATCH

    assessment = combine_assessment(
        problem, solution, evidence, execution, llm_assessment=None, rule_assessment=rule_assessment
    )
    assert assessment.functional_correct is False
    assert assessment.process_correct is False
    assert assessment.error_type == ErrorType.A01_PLAN_CODE_MISMATCH
    assert assessment.first_faulty_step == "S1"
    assert assessment.violated_requirement == "R1"

    certificate = build_error_certificate(
        assessment, execution, counterexample=None, rule_fired=True
    )
    assert certificate is not None
    assert certificate.verdict in ("confirmed_bug", "strongly_supported")


def test_full_pipeline_correct_safe_mean_not_flagged(problem):
    solution = _correct_solution()
    evidence = analyze_code(solution.code, "safe_mean")
    backend = TrustedLocalSandbox(per_test_timeout_seconds=2.0)
    execution = backend.run(solution.code, "safe_mean", problem.all_test_cases())
    execution = execution.model_copy(update={"problem_id": problem.problem_id})

    assert execution.all_passed()

    rule_assessment = evaluate_alignment_rules(problem, solution, evidence, execution)
    assert rule_assessment is None

    assessment = combine_assessment(
        problem, solution, evidence, execution, llm_assessment=None, rule_assessment=rule_assessment
    )
    assert assessment.functional_correct is True
    assert assessment.error_type is None
    assert assessment.reasoning_correct is None
    assert assessment.plan_code_aligned is None
    assert assessment.process_correct is None

    certificate = build_error_certificate(
        assessment, execution, counterexample=None, rule_fired=False
    )
    assert certificate is None
