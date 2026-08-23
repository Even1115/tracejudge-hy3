"""Counterexample search, in priority order:

1. Challenge test cases already provided by the problem (checked against
   already-executed TestExecutionResults -- no re-execution needed).
2. Hidden test cases already provided by the problem, same as above.
3. A small set of type-driven boundary candidates (empty list, single-element,
   duplicate, zero, negative, nearby values), diffed against the reference
   implementation via the same sandbox backend used for normal test runs.
4. Delta-debugging minimization of any list-valued argument in a found
   counterexample.

If nothing reproduces, this returns None -- callers must not upgrade an LLM's
suspicion to confirmed_bug on their own (see evaluator/evidence.py).
"""

from __future__ import annotations

import json
from typing import Any

from tracejudge_hy3.counterexample.differential import run_differential
from tracejudge_hy3.counterexample.minimizer import minimize_counterexample_args
from tracejudge_hy3.sandbox.base import SandboxBackend
from tracejudge_hy3.schemas.evaluation import Counterexample
from tracejudge_hy3.schemas.execution import ExecutionSummary
from tracejudge_hy3.schemas.problem import ProblemSpec
from tracejudge_hy3.schemas.solution import SolutionTrace

_MAX_BOUNDARY_CANDIDATES = 16


def _template_args(
    problem: ProblemSpec,
    related_requirement: str | None = None,
) -> list[Any]:
    for group in (
        problem.visible_test_cases,
        problem.hidden_test_cases,
        problem.challenge_test_cases,
    ):
        for tc in group:
            if (
                related_requirement is not None
                and related_requirement not in tc.related_requirements
            ):
                continue
            if tc.args:
                return list(tc.args)
    return []


def generate_boundary_candidates(
    problem: ProblemSpec,
    *,
    related_requirement: str | None = None,
) -> list[list[Any]]:
    """Derive type-driven candidates from relevant examples when one is specified."""

    template = _template_args(problem, related_requirement=related_requirement)
    if not template:
        return []

    candidates: list[list[Any]] = []
    for i, val in enumerate(template):
        if isinstance(val, bool):
            continue
        if isinstance(val, list):
            variants: list[Any] = [[]]
            if val:
                variants.append([val[0]])
                variants.append([val[0], val[0]])
            for v in variants:
                new_args = list(template)
                new_args[i] = v
                candidates.append(new_args)
        elif isinstance(val, int | float):
            variants = {0, -1}
            if val != 0:
                variants.add(-val)
            variants.add(val + 1)
            variants.add(val - 1)
            for v in variants:
                new_args = list(template)
                new_args[i] = v
                candidates.append(new_args)
        elif isinstance(val, str):
            for v in ("", val * 2):
                new_args = list(template)
                new_args[i] = v
                candidates.append(new_args)

    seen: set[str] = set()
    unique: list[list[Any]] = []
    for c in candidates:
        key = json.dumps(c, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique[:_MAX_BOUNDARY_CANDIDATES]


def _test_based_counterexample(
    problem: ProblemSpec,
    execution_result: ExecutionSummary,
    violated_requirement: str | None = None,
) -> Counterexample | None:
    """Return a failing challenge/hidden case relevant to the assessed requirement.

    ``violated_requirement=None`` preserves the original generic search behavior.
    When an assessment identifies a concrete requirement, an unrelated failure
    must not be promoted into evidence for that assessment.
    """

    by_id = {tc.case_id: tc for tc in problem.all_test_cases()}
    for category, source in (("challenge", "challenge_test"), ("hidden", "hidden_test")):
        for result in execution_result.results:
            if result.category != category or result.passed:
                continue
            tc = by_id.get(result.case_id)
            if tc is None:
                continue
            if (
                violated_requirement is not None
                and violated_requirement not in tc.related_requirements
            ):
                continue
            return Counterexample(
                args=tc.args,
                kwargs=tc.kwargs,
                expected=tc.expected,
                reference_output=None if tc.expected_exception else tc.expected,
                candidate_output=result.actual_output,
                candidate_exception=result.exception_type,
                reference_exception=tc.expected_exception,
                source=source,  # type: ignore[arg-type]
            )
    return None


def find_counterexample(
    problem: ProblemSpec,
    solution: SolutionTrace,
    execution_result: ExecutionSummary,
    backend: SandboxBackend,
    *,
    violated_requirement: str | None = None,
) -> Counterexample | None:
    test_based = _test_based_counterexample(
        problem,
        execution_result,
        violated_requirement=violated_requirement,
    )
    if test_based is not None:
        return test_based

    function_name = problem.function_name
    for args in generate_boundary_candidates(
        problem,
        related_requirement=violated_requirement,
    ):
        diff = run_differential(backend, problem.reference_code, solution.code, function_name, args)
        if not diff.differs:
            continue

        minimized_args, shrunk = minimize_counterexample_args(
            backend, problem.reference_code, solution.code, function_name, args, {}
        )
        if shrunk:
            final_diff = run_differential(
                backend, problem.reference_code, solution.code, function_name, minimized_args
            )
            if final_diff.differs:
                return Counterexample(
                    args=minimized_args,
                    kwargs={},
                    expected=final_diff.reference_output,
                    reference_output=final_diff.reference_output,
                    candidate_output=final_diff.candidate_output,
                    candidate_exception=final_diff.candidate_exception,
                    reference_exception=final_diff.reference_exception,
                    source="differential_search",
                    minimized=True,
                )

        return Counterexample(
            args=args,
            kwargs={},
            expected=diff.reference_output,
            reference_output=diff.reference_output,
            candidate_output=diff.candidate_output,
            candidate_exception=diff.candidate_exception,
            reference_exception=diff.reference_exception,
            source="boundary_candidate",
            minimized=False,
        )

    return None
