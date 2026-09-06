"""Shared normalization of disclosure-safe EvalPlus results for contract v1.

Both official EvalPlus datasets (HumanEval+ and MBPP+) emit the same pinned
raw schema, and the phase-two runners reduce it to the same safe field set
(``base_status``/``plus_status``/``*_fail_test_count``/``infrastructure_status``
…).  This module is the single normalization path so dataset bridges cannot
drift apart.  It performs no I/O and never sees raw evaluator payloads.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .contracts import (
    BenchmarkCandidate,
    BenchmarkExecutionResult,
    BenchmarkTask,
    ExecutionStatus,
    TestGroupSummary,
    TestVisibility,
    canonical_sha256,
)

_OFFICIAL_STATUS_MAP = {
    "pass": ExecutionStatus.PASSED,
    "fail": ExecutionStatus.FAILED,
    "timeout": ExecutionStatus.TIMEOUT,
}


def _test_group(result: Mapping[str, Any], *, name: str) -> TestGroupSummary:
    raw_status = result.get(f"{name}_status")
    try:
        status = _OFFICIAL_STATUS_MAP[raw_status]
    except (KeyError, TypeError):
        raise ValueError(f"execution result {name}_status is unsupported") from None
    failed_count = result.get(f"{name}_fail_test_count")
    if not isinstance(failed_count, int) or isinstance(failed_count, bool) or failed_count < 0:
        raise ValueError(f"execution result {name}_fail_test_count is invalid")
    return TestGroupSummary(
        group_id=name,
        visibility=TestVisibility.HIDDEN,
        status=status,
        failed_test_count=failed_count,
    )


def normalize_evalplus_execution_result(
    *,
    dataset_id: str,
    executor_id: str,
    task: BenchmarkTask,
    candidate: BenchmarkCandidate,
    result: Mapping[str, Any],
) -> BenchmarkExecutionResult:
    """Normalize one safe EvalPlus result into the frozen v1 boundary.

    Infrastructure errors, timeouts, candidate failures, and not-run outcomes
    stay distinct; only disclosure-safe group statuses and counts are kept.
    """

    if task.identity.dataset_id != dataset_id:
        raise ValueError("task does not belong to this dataset adapter")
    if candidate.task != task.identity:
        raise ValueError("candidate task identity does not match the benchmark task")
    if result.get("problem_id") != task.identity.task_id:
        raise ValueError("execution result task identity does not match the benchmark task")

    infrastructure_status = result.get("infrastructure_status")
    observed_solution_hash = result.get("solution_sha256")
    if observed_solution_hash is not None and observed_solution_hash != candidate.code_sha256:
        raise ValueError("execution result solution hash does not match the candidate")

    groups: tuple[TestGroupSummary, ...] = ()
    failure_kind = result.get("error_type")
    if failure_kind is not None and not isinstance(failure_kind, str):
        raise ValueError("execution result error_type must be a string or null")

    if infrastructure_status == "ok":
        base = _test_group(result, name="base")
        plus = _test_group(result, name="plus")
        groups = (base, plus)
        if ExecutionStatus.TIMEOUT in {base.status, plus.status}:
            status = ExecutionStatus.TIMEOUT
        elif plus.status is ExecutionStatus.PASSED:
            status = ExecutionStatus.PASSED
        else:
            status = ExecutionStatus.FAILED
    elif infrastructure_status == "error":
        status = ExecutionStatus.INFRASTRUCTURE_ERROR
        if not failure_kind:
            raise ValueError("infrastructure errors require an error_type")
    elif infrastructure_status == "mocked":
        status = ExecutionStatus.NOT_RUN
        if not failure_kind:
            failure_kind = "mock_not_executed"
    else:
        raise ValueError("execution result infrastructure_status is unsupported")

    duration = result.get("duration_seconds")
    if duration is not None and (
        not isinstance(duration, int | float) or isinstance(duration, bool) or duration < 0
    ):
        raise ValueError("execution result duration_seconds is invalid")

    return BenchmarkExecutionResult(
        task=task.identity,
        candidate_sha256=candidate.code_sha256,
        executor_id=executor_id,
        status=status,
        groups=groups,
        failure_kind=failure_kind,
        duration_seconds=float(duration) if duration is not None else None,
        source_result_sha256=canonical_sha256(dict(result)),
    )


__all__ = ["normalize_evalplus_execution_result"]
