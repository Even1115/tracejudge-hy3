"""Strict, disclosure-safe parsing for official EvalPlus v0.3.1 results.

EvalPlus' raw result contains both the submitted solution and concrete failing
test inputs.  Both are evaluation-only data.  This module therefore uses a
positive allowlist: it retains statuses, counts failing-input array entries,
and hashes the solution, but never copies either sensitive value into normal
results or summaries.

EvalPlus v0.3.1 reports only ``pass``, ``fail`` and ``timeout``.  In
particular, ``fail`` combines wrong answers and exceptions raised by candidate
code; downstream code must not pretend that those cases can be separated.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

OfficialStatus = Literal["pass", "fail", "timeout"]

OFFICIAL_STATUSES = frozenset({"pass", "fail", "timeout"})
RAW_BUNDLE_KIND = "tracejudge_evalplus_v031_per_task_raw_bundle"
WRONG_ANSWER_OR_CANDIDATE_EXCEPTION = "wrong_answer_or_candidate_exception"
TIMEOUT_ERROR = "timeout"

_TASK_ID_PATTERN = re.compile(r"^HumanEval/(?:0|[1-9][0-9]*)$")
_MD5_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_OFFICIAL_TOP_LEVEL_FIELDS = {"date", "hash", "eval"}
_OFFICIAL_RESULT_FIELDS = {
    "task_id",
    "solution",
    "base_status",
    "plus_status",
    "base_fail_tests",
    "plus_fail_tests",
}
_SAFE_RESULT_FIELDS = {
    "problem_id",
    "base_status",
    "plus_status",
    "base_fail_test_count",
    "plus_fail_test_count",
    "passed_base",
    "passed_plus",
    "error_type",
    "infrastructure_status",
    "solution_sha256",
    "official_override_hash",
}
INFRASTRUCTURE_ERROR_TYPES = frozenset(
    {
        "batch_deadline_not_started",
        "batch_timeout",
        "container_exit_error",
        "container_cleanup_failed",
        "container_start_error",
        "container_timeout",
        "docker_unavailable",
        "executor_error",
        "image_mismatch",
        "invalid_raw_result",
        "missing_raw_result",
    }
)
_MOCK_ERROR_TYPE = "mock_not_executed"
_MAX_RAW_RESULT_BYTES = 128 * 1024 * 1024
_MAX_SOLUTION_BYTES = 2 * 1024 * 1024


class EvalPlusParseError(ValueError):
    """Raised when an official result is incomplete, stale, or malformed."""


class SensitiveDataLeakError(AssertionError):
    """Raised by :func:`assert_no_canaries` without echoing sensitive data."""


class _DuplicateJSONKey(ValueError):
    pass


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise _DuplicateJSONKey


def load_official_raw_result(
    path: str | Path,
    *,
    max_bytes: int = _MAX_RAW_RESULT_BYTES,
) -> Any:
    """Load one official result or a per-task bundle without leaking bad input.

    Duplicate JSON object keys are rejected because accepting them would make
    the interpreted task/status depend on the JSON implementation.
    """

    resolved = Path(path).expanduser()
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    try:
        metadata = resolved.lstat()
    except OSError:
        raise EvalPlusParseError("official result file is unavailable") from None
    if resolved.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise EvalPlusParseError("official result path must be a regular non-symlink file")
    if metadata.st_size > max_bytes:
        raise EvalPlusParseError("official result exceeds the configured size limit")
    try:
        raw_bytes = resolved.read_bytes()
    except OSError:
        raise EvalPlusParseError("official result file could not be read") from None
    if len(raw_bytes) > max_bytes:
        raise EvalPlusParseError("official result exceeds the configured size limit")
    try:
        return json.loads(
            raw_bytes.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJSONKey):
        raise EvalPlusParseError("official result is not unambiguous UTF-8 JSON") from None


def _expected_ids(problem_ids: Sequence[str]) -> tuple[str, ...]:
    if isinstance(problem_ids, str | bytes) or not isinstance(problem_ids, Sequence):
        raise EvalPlusParseError("expected problem IDs must be a sequence")
    result = tuple(problem_ids)
    if not result or any(
        not isinstance(problem_id, str) or not _TASK_ID_PATTERN.fullmatch(problem_id)
        for problem_id in result
    ):
        raise EvalPlusParseError("expected problem IDs are invalid")
    if len(result) != len(set(result)):
        raise EvalPlusParseError("expected problem IDs contain duplicates")
    return result


def _expected_solution_hashes(
    value: Mapping[str, str] | None,
    expected_ids: tuple[str, ...],
) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != set(expected_ids):
        raise EvalPlusParseError("expected solution hashes do not match the problem set")
    hashes: dict[str, str] = {}
    for problem_id in expected_ids:
        digest = value.get(problem_id)
        if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
            raise EvalPlusParseError("an expected solution hash is invalid")
        hashes[problem_id] = digest
    return hashes


def _raw_documents(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, Mapping):
        if "eval" in payload:
            return [payload]
        if set(payload) == {"schema_version", "kind", "raw_results"}:
            if payload.get("schema_version") != 1 or payload.get("kind") != RAW_BUNDLE_KIND:
                raise EvalPlusParseError("per-task raw bundle identity is invalid")
            raw_results = payload.get("raw_results")
            if isinstance(raw_results, str | bytes) or not isinstance(raw_results, Sequence):
                raise EvalPlusParseError("per-task raw bundle results must be a sequence")
            documents = list(raw_results)
        else:
            raise EvalPlusParseError("unsupported official result container")
    else:
        raise EvalPlusParseError("official result must be a JSON object or versioned bundle")
    if not documents or any(not isinstance(item, Mapping) for item in documents):
        raise EvalPlusParseError("per-task raw bundle contains an invalid document")
    return documents  # type: ignore[return-value]


def _status(value: Any) -> OfficialStatus:
    if not isinstance(value, str) or value not in OFFICIAL_STATUSES:
        raise EvalPlusParseError("official result contains an unsupported status")
    return value  # type: ignore[return-value]


def _failure_kind(base_status: OfficialStatus, plus_status: OfficialStatus) -> str | None:
    if TIMEOUT_ERROR in {base_status, plus_status}:
        return TIMEOUT_ERROR
    if "fail" in {base_status, plus_status}:
        return WRONG_ANSWER_OR_CANDIDATE_EXCEPTION
    return None


def _parse_document(document: Mapping[str, Any]) -> dict[str, Any]:
    if set(document) != _OFFICIAL_TOP_LEVEL_FIELDS:
        raise EvalPlusParseError("official EvalPlus v0.3.1 top-level fields are invalid")
    official_date = document.get("date")
    dataset_hash = document.get("hash")
    evaluations = document.get("eval")
    if not isinstance(official_date, str) or not official_date.strip():
        raise EvalPlusParseError("official result date is invalid")
    if not isinstance(dataset_hash, str) or not _MD5_PATTERN.fullmatch(dataset_hash):
        raise EvalPlusParseError("official result override hash is invalid")
    if not isinstance(evaluations, Mapping) or len(evaluations) != 1:
        raise EvalPlusParseError("each official raw document must contain exactly one task")

    problem_id, candidates = next(iter(evaluations.items()))
    if not isinstance(problem_id, str) or not _TASK_ID_PATTERN.fullmatch(problem_id):
        raise EvalPlusParseError("official result task ID is invalid")
    if (
        isinstance(candidates, str | bytes)
        or not isinstance(candidates, Sequence)
        or len(candidates) != 1
        or not isinstance(candidates[0], Mapping)
    ):
        raise EvalPlusParseError("official result must contain exactly one candidate per task")
    candidate = candidates[0]
    if set(candidate) != _OFFICIAL_RESULT_FIELDS:
        raise EvalPlusParseError("official candidate result fields are invalid")
    if candidate.get("task_id") != problem_id:
        raise EvalPlusParseError("official candidate task ID does not match its task key")

    solution = candidate.get("solution")
    if not isinstance(solution, str) or not solution.strip():
        raise EvalPlusParseError("official candidate solution is invalid")
    try:
        solution_bytes = solution.encode("utf-8")
    except UnicodeEncodeError:
        raise EvalPlusParseError("official candidate solution is not valid Unicode") from None
    if len(solution_bytes) > _MAX_SOLUTION_BYTES:
        raise EvalPlusParseError("official candidate solution exceeds the size limit")

    base_status = _status(candidate.get("base_status"))
    plus_status = _status(candidate.get("plus_status"))
    base_fail_tests = candidate.get("base_fail_tests")
    plus_fail_tests = candidate.get("plus_fail_tests")
    if (
        isinstance(base_fail_tests, str | bytes)
        or not isinstance(base_fail_tests, Sequence)
        or isinstance(plus_fail_tests, str | bytes)
        or not isinstance(plus_fail_tests, Sequence)
    ):
        raise EvalPlusParseError("official failed-test fields must be arrays")
    if base_status == "pass" and len(base_fail_tests) != 0:
        raise EvalPlusParseError("a passing base status cannot contain failed tests")
    if plus_status == "pass" and len(plus_fail_tests) != 0:
        raise EvalPlusParseError("a passing plus status cannot contain failed tests")

    return {
        "problem_id": problem_id,
        "base_status": base_status,
        "plus_status": plus_status,
        # Deliberately use len() only.  Do not traverse, stringify, or retain
        # concrete failing inputs from these evaluation-only arrays.
        "base_fail_test_count": len(base_fail_tests),
        "plus_fail_test_count": len(plus_fail_tests),
        "passed_base": base_status == "pass",
        "passed_plus": base_status == plus_status == "pass",
        "error_type": _failure_kind(base_status, plus_status),
        "infrastructure_status": "ok",
        "solution_sha256": hashlib.sha256(solution_bytes).hexdigest(),
        # With one official invocation per task, EvalPlus' raw `hash` binds
        # the private single-task HUMANEVAL_OVERRIDE_PATH, not the complete
        # release corpus inspected during preflight.
        "official_override_hash": dataset_hash,
    }


def parse_official_results(
    payload: Any,
    *,
    expected_problem_ids: Sequence[str],
    expected_solution_sha256: Mapping[str, str] | None = None,
    canaries: Sequence[str | bytes] = (),
) -> list[dict[str, Any]]:
    """Parse single-task raw documents into safe results in expected ID order.

    ``payload`` may be one official single-task result or an exact versioned
    ``RAW_BUNDLE_KIND`` wrapper.  Any missing, extra, duplicate, unversioned,
    or multi-sample task is rejected.
    """

    expected_ids = _expected_ids(expected_problem_ids)
    expected_hashes = _expected_solution_hashes(expected_solution_sha256, expected_ids)
    by_problem: dict[str, dict[str, Any]] = {}
    for document in _raw_documents(payload):
        safe_result = _parse_document(document)
        problem_id = safe_result["problem_id"]
        if problem_id in by_problem:
            raise EvalPlusParseError("official per-task bundle contains a duplicate task")
        by_problem[problem_id] = safe_result
    if set(by_problem) != set(expected_ids):
        raise EvalPlusParseError("official result task IDs differ from the expected set")

    results = [by_problem[problem_id] for problem_id in expected_ids]
    if expected_hashes is not None and any(
        result["solution_sha256"] != expected_hashes[result["problem_id"]] for result in results
    ):
        raise EvalPlusParseError("official result solution fingerprint differs from samples")
    assert_no_canaries(results, canaries)
    return results


def parse_official_result(
    payload: Any,
    *,
    expected_problem_id: str,
    expected_solution_sha256: str | None = None,
    canaries: Sequence[str | bytes] = (),
) -> dict[str, Any]:
    """Convenience wrapper for one official single-task raw result."""

    expected_hashes = (
        {expected_problem_id: expected_solution_sha256}
        if expected_solution_sha256 is not None
        else None
    )
    return parse_official_results(
        payload,
        expected_problem_ids=[expected_problem_id],
        expected_solution_sha256=expected_hashes,
        canaries=canaries,
    )[0]


def infrastructure_error_result(
    problem_id: str,
    *,
    error_type: str,
) -> dict[str, Any]:
    """Construct a non-candidate infrastructure outcome.

    Base/plus statuses are ``None`` rather than ``fail`` so summaries cannot
    accidentally count Docker, image, or result-transport failures against the
    submitted code.
    """

    if not isinstance(problem_id, str) or not _TASK_ID_PATTERN.fullmatch(problem_id):
        raise EvalPlusParseError("infrastructure result problem ID is invalid")
    if error_type not in INFRASTRUCTURE_ERROR_TYPES:
        raise EvalPlusParseError("infrastructure error type is invalid")
    return {
        "problem_id": problem_id,
        "base_status": None,
        "plus_status": None,
        "base_fail_test_count": 0,
        "plus_fail_test_count": 0,
        "passed_base": False,
        "passed_plus": False,
        "error_type": error_type,
        "infrastructure_status": "error",
        "solution_sha256": None,
        "official_override_hash": None,
    }


def infrastructure_error_results(
    problem_ids: Sequence[str],
    *,
    error_type: str,
) -> list[dict[str, Any]]:
    """Construct one infrastructure outcome for every expected task."""

    return [
        infrastructure_error_result(problem_id, error_type=error_type)
        for problem_id in _expected_ids(problem_ids)
    ]


def _non_negative_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validated_results(
    results: Sequence[Mapping[str, Any]],
    expected_problem_ids: Sequence[str],
) -> tuple[list[Mapping[str, Any]], tuple[str, ...]]:
    expected_ids = _expected_ids(expected_problem_ids)
    if isinstance(results, str | bytes) or not isinstance(results, Sequence):
        raise EvalPlusParseError("safe results must be a sequence")
    materialized = list(results)
    if len(materialized) != len(expected_ids) or any(
        not isinstance(result, Mapping) for result in materialized
    ):
        raise EvalPlusParseError("safe result count differs from the expected problem set")
    by_problem: dict[str, Mapping[str, Any]] = {}
    for result in materialized:
        if not _SAFE_RESULT_FIELDS.issubset(result):
            raise EvalPlusParseError("safe result fields are incomplete")
        problem_id = result.get("problem_id")
        if not isinstance(problem_id, str) or problem_id in by_problem:
            raise EvalPlusParseError("safe results contain an invalid or duplicate task")
        by_problem[problem_id] = result
        for field in ("base_fail_test_count", "plus_fail_test_count"):
            if not _non_negative_count(result.get(field)):
                raise EvalPlusParseError("safe failed-test count is invalid")
        infrastructure_status = result.get("infrastructure_status")
        if infrastructure_status == "ok":
            base_status = _status(result.get("base_status"))
            plus_status = _status(result.get("plus_status"))
            if result.get("passed_base") is not (base_status == "pass"):
                raise EvalPlusParseError("safe base-pass flag is inconsistent")
            if result.get("passed_plus") is not (base_status == plus_status == "pass"):
                raise EvalPlusParseError("safe Base+Extra pass flag is inconsistent")
            if result.get("error_type") != _failure_kind(base_status, plus_status):
                raise EvalPlusParseError("safe candidate failure classification is inconsistent")
            solution_hash = result.get("solution_sha256")
            dataset_hash = result.get("official_override_hash")
            if not isinstance(solution_hash, str) or not _SHA256_PATTERN.fullmatch(solution_hash):
                raise EvalPlusParseError("safe solution fingerprint is invalid")
            if not isinstance(dataset_hash, str) or not _MD5_PATTERN.fullmatch(dataset_hash):
                raise EvalPlusParseError("safe official override hash is invalid")
        elif infrastructure_status == "error":
            if (
                result.get("base_status") is not None
                or result.get("plus_status") is not None
                or result.get("base_fail_test_count") != 0
                or result.get("plus_fail_test_count") != 0
                or result.get("passed_base") is not False
                or result.get("passed_plus") is not False
                or result.get("error_type") not in INFRASTRUCTURE_ERROR_TYPES
                or result.get("solution_sha256") is not None
                or result.get("official_override_hash") is not None
            ):
                raise EvalPlusParseError("safe infrastructure outcome is inconsistent")
        elif infrastructure_status == "mocked":
            if (
                result.get("base_status") is not None
                or result.get("plus_status") is not None
                or result.get("base_fail_test_count") != 0
                or result.get("plus_fail_test_count") != 0
                or result.get("passed_base") is not False
                or result.get("passed_plus") is not False
                or result.get("error_type") != _MOCK_ERROR_TYPE
                or not isinstance(result.get("solution_sha256"), str)
                or not _SHA256_PATTERN.fullmatch(str(result.get("solution_sha256")))
                or result.get("official_override_hash") is not None
            ):
                raise EvalPlusParseError("safe mock outcome is inconsistent")
        else:
            raise EvalPlusParseError("safe infrastructure status is invalid")

        duration = result.get("duration_seconds")
        if duration is not None and (
            isinstance(duration, bool)
            or not isinstance(duration, int | float)
            or not math.isfinite(float(duration))
            or duration < 0
        ):
            raise EvalPlusParseError("safe result duration is invalid")
    if set(by_problem) != set(expected_ids):
        raise EvalPlusParseError("safe result task IDs differ from the expected set")
    return [by_problem[problem_id] for problem_id in expected_ids], expected_ids


def build_summary(
    results: Sequence[Mapping[str, Any]],
    *,
    expected_problem_ids: Sequence[str],
    execution_mode: Literal["docker", "mock"] = "docker",
    canaries: Sequence[str | bytes] = (),
) -> dict[str, Any]:
    """Build a fixed-subset summary solely from disclosure-safe results."""

    if execution_mode not in {"docker", "mock"}:
        raise EvalPlusParseError("summary execution mode is invalid")
    safe_results, expected_ids = _validated_results(results, expected_problem_ids)
    assert_no_canaries(safe_results, canaries)
    executed = [result for result in safe_results if result.get("infrastructure_status") == "ok"]
    infrastructure_errors = [
        result for result in safe_results if result.get("infrastructure_status") == "error"
    ]
    mocked = [result for result in safe_results if result.get("infrastructure_status") == "mocked"]
    durations = [
        float(result["duration_seconds"])
        for result in executed
        if result.get("duration_seconds") is not None
    ]
    base_pass_count = sum(result.get("passed_base") is True for result in executed)
    base_plus_pass_count = sum(result.get("passed_plus") is True for result in executed)
    executed_count = len(executed)
    summary = {
        "schema_version": 1,
        "metrics_scope": (
            "fixed_subset_single_sample_execution_pilot"
            if execution_mode == "docker"
            else "mock_dry_run_only"
        ),
        "execution_mode": execution_mode,
        "total_problem_count": len(expected_ids),
        "result_count": len(safe_results),
        "actual_execution_count": executed_count,
        "mock_not_executed_count": len(mocked),
        "evaluation_complete": executed_count == len(expected_ids),
        "base_pass_count": base_pass_count,
        "base_pass_rate": base_pass_count / executed_count if executed_count else None,
        # EvalPlus' own HumanEval+ metric requires both statuses to pass.
        "base_plus_pass_count": base_plus_pass_count,
        "base_plus_pass_rate": (base_plus_pass_count / executed_count if executed_count else None),
        "base_fail_count": sum(result.get("base_status") == "fail" for result in executed),
        "plus_fail_count": sum(result.get("plus_status") == "fail" for result in executed),
        "timeout_count": sum(result.get("error_type") == TIMEOUT_ERROR for result in executed),
        "wrong_answer_or_candidate_exception_count": sum(
            result.get("error_type") == WRONG_ANSWER_OR_CANDIDATE_EXCEPTION for result in executed
        ),
        "infrastructure_error_count": len(infrastructure_errors),
        "batch_timeout_count": sum(
            result.get("error_type") == "batch_timeout" for result in infrastructure_errors
        ),
        "batch_deadline_not_started_count": sum(
            result.get("error_type") == "batch_deadline_not_started"
            for result in infrastructure_errors
        ),
        "container_cleanup_failed_count": sum(
            result.get("error_type") == "container_cleanup_failed"
            for result in infrastructure_errors
        ),
        # EvalPlus v0.3.1 folds wrong answers, syntax errors, missing entry
        # points, and ordinary candidate exceptions into the same `fail`
        # status.  A numeric execution-error count would fabricate precision.
        "execution_error_count": None,
        "execution_error_observability": "not_available_in_evalplus_v0.3.1",
        "observed_base_failed_test_count": sum(
            int(result["base_fail_test_count"]) for result in executed
        ),
        "observed_plus_failed_test_count": sum(
            int(result["plus_fail_test_count"]) for result in executed
        ),
        "average_duration_seconds": sum(durations) / len(durations) if durations else None,
        "limitations": [
            "fixed_problem_subset_not_full_humanevalplus",
            "single_sample_engineering_pilot_not_official_benchmark_ranking",
            "evalplus_v0.3.1_fail_combines_wrong_answers_and_candidate_exceptions",
            "public_benchmark_training_contamination_is_possible",
        ],
    }
    assert_no_canaries(summary, canaries)
    return summary


def validate_summary(
    summary: Mapping[str, Any],
    results: Sequence[Mapping[str, Any]],
    *,
    expected_problem_ids: Sequence[str],
    execution_mode: Literal["docker", "mock"] = "docker",
    canaries: Sequence[str | bytes] = (),
) -> None:
    """Reject any summary that does not exactly match its safe task records."""

    if not isinstance(summary, Mapping):
        raise EvalPlusParseError("summary must be a JSON object")
    assert_no_canaries(summary, canaries)
    expected = build_summary(
        results,
        expected_problem_ids=expected_problem_ids,
        execution_mode=execution_mode,
        canaries=canaries,
    )
    if dict(summary) != expected:
        raise EvalPlusParseError("summary is inconsistent with safe task results")


def assert_no_canaries(value: Any, canaries: Sequence[str | bytes]) -> None:
    """Recursively assert that no supplied sensitive canary appears in ``value``.

    The exception is deliberately constant and never contains the canary, the
    offending value, or a mapping key that might itself be sensitive.
    """

    string_canaries = tuple(
        item.decode("utf-8", errors="ignore") if isinstance(item, bytes) else item
        for item in canaries
        if item
    )
    byte_canaries = tuple(item.encode("utf-8") for item in string_canaries if item)
    seen: set[int] = set()

    def visit(item: Any) -> None:
        if isinstance(item, str):
            if any(canary in item for canary in string_canaries):
                raise SensitiveDataLeakError("sensitive canary reached a public value")
            return
        if isinstance(item, bytes):
            if any(canary in item for canary in byte_canaries):
                raise SensitiveDataLeakError("sensitive canary reached a public value")
            return
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in seen:
                return
            seen.add(identity)
            for key, nested in item.items():
                visit(key)
                visit(nested)
            return
        if isinstance(item, Sequence) and not isinstance(item, str | bytes):
            identity = id(item)
            if identity in seen:
                return
            seen.add(identity)
            for nested in item:
                visit(nested)
            return
        if isinstance(item, set | frozenset):
            identity = id(item)
            if identity in seen:
                return
            seen.add(identity)
            for nested in item:
                visit(nested)

    visit(value)


__all__ = [
    "EvalPlusParseError",
    "INFRASTRUCTURE_ERROR_TYPES",
    "OFFICIAL_STATUSES",
    "RAW_BUNDLE_KIND",
    "SensitiveDataLeakError",
    "WRONG_ANSWER_OR_CANDIDATE_EXCEPTION",
    "assert_no_canaries",
    "build_summary",
    "infrastructure_error_result",
    "infrastructure_error_results",
    "load_official_raw_result",
    "parse_official_result",
    "parse_official_results",
    "validate_summary",
]
