from __future__ import annotations

from pathlib import Path

import pytest

from tracejudge_hy3.dataset.loader import load_problem_by_id
from tracejudge_hy3.providers.mock import MockProvider
from tracejudge_hy3.schemas.execution import ExecutionSummary
from tracejudge_hy3.schemas.solution import SolutionTrace
from tracejudge_hy3.static_analysis.ast_analyzer import analyze_code

DATASET = Path(__file__).resolve().parents[1] / "data" / "sample_problems.jsonl"


@pytest.mark.asyncio
async def test_mock_judge_does_not_reject_valid_multivariate_complexity():
    problem = load_problem_by_id(DATASET, "safe_mean")
    solution = SolutionTrace(
        problem_id=problem.problem_id,
        requirement_understanding="遍历每一行的所有元素。",
        design_summary="嵌套遍历矩阵，总工作量与元素数相同。",
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
    execution = ExecutionSummary(
        problem_id=problem.problem_id,
        function_name="flatten",
        sandbox_backend="trusted-local",
        results=[],
    )

    assessment = await MockProvider().evaluate_process(
        problem,
        solution,
        evidence,
        execution,
    )

    assert assessment.error_type is None
    assert assessment.process_correct is True
