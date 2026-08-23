from __future__ import annotations

from pathlib import Path

import pytest

from tracejudge_hy3.counterexample.differential import run_differential
from tracejudge_hy3.counterexample.generator import (
    find_counterexample,
    generate_boundary_candidates,
)
from tracejudge_hy3.counterexample.minimizer import minimize_counterexample_args
from tracejudge_hy3.dataset.loader import load_problem_by_id
from tracejudge_hy3.sandbox.trusted_local import TrustedLocalSandbox
from tracejudge_hy3.schemas.solution import SolutionTrace

DATASET = Path(__file__).resolve().parents[1] / "data" / "sample_problems.jsonl"

REFERENCE_SAFE_MEAN = (
    "def safe_mean(nums):\n    if not nums:\n        return 0.0\n    return sum(nums) / len(nums)\n"
)
FAULTY_SAFE_MEAN = "def safe_mean(nums):\n    return sum(nums) / len(nums)\n"
UNRELATED_R2_BUG = "def safe_mean(nums):\n    if not nums:\n        return 0.0\n    return 999.0\n"


@pytest.fixture
def backend():
    return TrustedLocalSandbox(per_test_timeout_seconds=2.0)


def test_run_differential_detects_divergence(backend):
    result = run_differential(backend, REFERENCE_SAFE_MEAN, FAULTY_SAFE_MEAN, "safe_mean", [[]])
    assert result.differs
    assert result.reference_output == 0.0
    assert result.candidate_exception == "ZeroDivisionError"


def test_run_differential_no_divergence_on_normal_input(backend):
    result = run_differential(
        backend, REFERENCE_SAFE_MEAN, FAULTY_SAFE_MEAN, "safe_mean", [[1, 2, 3]]
    )
    assert not result.differs


def test_generate_boundary_candidates_includes_empty_list():
    problem = load_problem_by_id(DATASET, "safe_mean")
    candidates = generate_boundary_candidates(problem)
    assert [[]] in candidates
    assert any(c[0] == [] for c in candidates)


def test_find_counterexample_reproduces_empty_list_bug(backend):
    problem = load_problem_by_id(DATASET, "safe_mean")
    solution = SolutionTrace(
        problem_id="safe_mean",
        requirement_understanding="u",
        design_summary="d",
        edge_cases_considered=[],
        implementation_steps=[],
        declared_time_complexity="O(n)",
        declared_space_complexity="O(1)",
        code=FAULTY_SAFE_MEAN,
    )
    execution = backend.run(solution.code, "safe_mean", problem.all_test_cases())
    ce = find_counterexample(
        problem,
        solution,
        execution,
        backend,
        violated_requirement="R1",
    )
    assert ce is not None
    assert ce.args == [[]]
    assert ce.source in ("challenge_test", "hidden_test")
    assert ce.reference_output == 0.0
    assert ce.reference_exception is None


def test_find_counterexample_returns_none_for_correct_code(backend):
    problem = load_problem_by_id(DATASET, "safe_mean")
    solution = SolutionTrace(
        problem_id="safe_mean",
        requirement_understanding="u",
        design_summary="d",
        edge_cases_considered=[],
        implementation_steps=[],
        declared_time_complexity="O(n)",
        declared_space_complexity="O(1)",
        code=REFERENCE_SAFE_MEAN,
    )
    execution = backend.run(solution.code, "safe_mean", problem.all_test_cases())
    ce = find_counterexample(problem, solution, execution, backend)
    assert ce is None


def test_find_counterexample_ignores_failure_for_another_requirement(backend):
    problem = load_problem_by_id(DATASET, "safe_mean")
    solution = SolutionTrace(
        problem_id="safe_mean",
        requirement_understanding="u",
        design_summary="d",
        edge_cases_considered=[],
        implementation_steps=[],
        declared_time_complexity="O(n)",
        declared_space_complexity="O(1)",
        code=UNRELATED_R2_BUG,
    )
    execution = backend.run(solution.code, "safe_mean", problem.all_test_cases())

    assert execution.failures()
    assert all("R2" in failure.related_requirements for failure in execution.failures())

    ce = find_counterexample(
        problem,
        solution,
        execution,
        backend,
        violated_requirement="R1",
    )

    assert ce is None


DUPLICATE_BUG_REFERENCE = (
    "def first_duplicate(xs):\n"
    "    seen = set()\n"
    "    for x in xs:\n"
    "        if x in seen:\n"
    "            return x\n"
    "        seen.add(x)\n"
    "    return None\n"
)

DUPLICATE_BUG_CANDIDATE = (
    "def first_duplicate(xs):\n"
    "    seen = set()\n"
    "    for x in xs:\n"
    "        if x in seen:\n"
    "            return x\n"
    "        seen.add(x)\n"
    "    return -1\n"
)


def test_minimize_counterexample_args_shrinks_list(backend):
    args = [[1, 2, 3, 4, 5, 6, 7]]
    minimized, shrunk = minimize_counterexample_args(
        backend, DUPLICATE_BUG_REFERENCE, DUPLICATE_BUG_CANDIDATE, "first_duplicate", args, {}
    )
    assert shrunk
    diff = run_differential(
        backend, DUPLICATE_BUG_REFERENCE, DUPLICATE_BUG_CANDIDATE, "first_duplicate", minimized
    )
    assert diff.differs
    assert len(minimized[0]) <= len(args[0])
