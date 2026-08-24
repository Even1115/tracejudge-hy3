from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from tracejudge_hy3.evalplus.parser import (
    RAW_BUNDLE_KIND,
    WRONG_ANSWER_OR_CANDIDATE_EXCEPTION,
    EvalPlusParseError,
    SensitiveDataLeakError,
    assert_no_canaries,
    build_summary,
    infrastructure_error_result,
    infrastructure_error_results,
    load_official_raw_result,
    parse_official_result,
    parse_official_results,
    validate_summary,
)

DATASET_HASH = "a" * 32


def _official_raw(
    problem_id: str = "HumanEval/8",
    *,
    solution: str = "def candidate(value):\n    return value\n",
    base_status: str | None = "pass",
    plus_status: str | None = "pass",
    base_fail_tests: object | None = None,
    plus_fail_tests: object | None = None,
) -> dict:
    return {
        "date": "2026-08-24 12:30",
        "hash": DATASET_HASH,
        "eval": {
            problem_id: [
                {
                    "task_id": problem_id,
                    "solution": solution,
                    "base_status": base_status,
                    "plus_status": plus_status,
                    "base_fail_tests": ([] if base_fail_tests is None else base_fail_tests),
                    "plus_fail_tests": ([] if plus_fail_tests is None else plus_fail_tests),
                }
            ]
        },
    }


def _solution_hash(raw: dict) -> str:
    problem_id = next(iter(raw["eval"]))
    solution = raw["eval"][problem_id][0]["solution"]
    return hashlib.sha256(solution.encode("utf-8")).hexdigest()


def _bundle(*documents: dict) -> dict:
    return {
        "schema_version": 1,
        "kind": RAW_BUNDLE_KIND,
        "raw_results": list(documents),
    }


def test_pass_result_keeps_only_allowlisted_metadata_and_hashes_solution():
    solution_canary = "SOLUTION_BODY_MUST_NOT_BE_COPIED"
    raw = _official_raw(solution=f"def candidate():\n    return {solution_canary!r}\n")

    result = parse_official_result(raw, expected_problem_id="HumanEval/8")

    assert result == {
        "problem_id": "HumanEval/8",
        "base_status": "pass",
        "plus_status": "pass",
        "base_fail_test_count": 0,
        "plus_fail_test_count": 0,
        "passed_base": True,
        "passed_plus": True,
        "error_type": None,
        "infrastructure_status": "ok",
        "solution_sha256": _solution_hash(raw),
        "official_override_hash": DATASET_HASH,
    }
    assert solution_canary not in json.dumps(result)
    assert "solution" not in result


def test_passed_plus_requires_both_base_and_extra_to_pass():
    raw = _official_raw(
        base_status="fail",
        plus_status="pass",
        base_fail_tests=[["PRIVATE_BASE_INPUT"]],
    )

    result = parse_official_result(raw, expected_problem_id="HumanEval/8")

    assert result["passed_base"] is False
    assert result["passed_plus"] is False
    assert result["error_type"] == WRONG_ANSWER_OR_CANDIDATE_EXCEPTION


def test_fail_is_honestly_classified_and_failure_inputs_are_only_counted():
    hidden_canaries = (
        "PRIVATE_HIDDEN_FAILURE_1",
        "PRIVATE_HIDDEN_FAILURE_2",
        "PRIVATE_PLUS_FAILURE",
    )
    raw = _official_raw(
        base_status="fail",
        plus_status="fail",
        base_fail_tests=[
            [hidden_canaries[0]],
            {"deeply_nested": hidden_canaries[1]},
        ],
        plus_fail_tests=[{"value": hidden_canaries[2]}],
    )

    result = parse_official_result(
        raw,
        expected_problem_id="HumanEval/8",
        canaries=hidden_canaries,
    )

    assert result["base_fail_test_count"] == 2
    assert result["plus_fail_test_count"] == 1
    assert result["error_type"] == WRONG_ANSWER_OR_CANDIDATE_EXCEPTION
    serialized = json.dumps(result)
    assert not [canary for canary in hidden_canaries if canary in serialized]


def test_timeout_has_a_distinct_classification_even_when_other_suite_fails():
    raw = _official_raw(
        base_status="fail",
        plus_status="timeout",
        base_fail_tests=[[1]],
    )

    result = parse_official_result(raw, expected_problem_id="HumanEval/8")

    assert result["base_status"] == "fail"
    assert result["plus_status"] == "timeout"
    assert result["error_type"] == "timeout"
    assert result["passed_plus"] is False


@pytest.mark.parametrize("field", ["base_status", "plus_status"])
@pytest.mark.parametrize("invalid_status", [None, "error", "passed", "TIMEOUT", 1])
def test_only_pinned_official_statuses_are_accepted(field, invalid_status):
    raw = _official_raw()
    raw["eval"]["HumanEval/8"][0][field] = invalid_status

    with pytest.raises(EvalPlusParseError, match="unsupported status"):
        parse_official_result(raw, expected_problem_id="HumanEval/8")


@pytest.mark.parametrize(
    ("status_field", "failures_field"),
    [
        ("base_status", "base_fail_tests"),
        ("plus_status", "plus_fail_tests"),
    ],
)
def test_passing_status_cannot_claim_failed_tests(status_field, failures_field):
    raw = _official_raw()
    candidate = raw["eval"]["HumanEval/8"][0]
    candidate[status_field] = "pass"
    candidate[failures_field] = [["PRIVATE_FAILURE"]]

    with pytest.raises(EvalPlusParseError, match="passing"):
        parse_official_result(raw, expected_problem_id="HumanEval/8")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update({"unexpected": True}),
        lambda payload: payload.pop("date"),
        lambda payload: payload.update({"hash": "not-an-md5"}),
        lambda payload: payload.update({"date": None}),
    ],
)
def test_official_top_level_schema_is_strict(mutation):
    raw = _official_raw()
    mutation(raw)

    with pytest.raises(EvalPlusParseError):
        parse_official_result(raw, expected_problem_id="HumanEval/8")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda candidate: candidate.update({"unexpected": "PRIVATE_VALUE"}),
        lambda candidate: candidate.pop("solution"),
        lambda candidate: candidate.update({"solution": ""}),
        lambda candidate: candidate.update({"base_fail_tests": "not-an-array"}),
        lambda candidate: candidate.update({"plus_fail_tests": {"not": "an-array"}}),
    ],
)
def test_official_candidate_schema_is_strict_without_echoing_values(mutation):
    canary = "PRIVATE_VALUE"
    raw = _official_raw()
    mutation(raw["eval"]["HumanEval/8"][0])

    with pytest.raises(EvalPlusParseError) as caught:
        parse_official_result(raw, expected_problem_id="HumanEval/8")
    assert canary not in str(caught.value)


def test_task_key_candidate_id_and_expected_id_must_all_match():
    raw = _official_raw()
    raw["eval"]["HumanEval/8"][0]["task_id"] = "HumanEval/26"
    with pytest.raises(EvalPlusParseError, match="does not match"):
        parse_official_result(raw, expected_problem_id="HumanEval/8")

    with pytest.raises(EvalPlusParseError, match="expected set"):
        parse_official_result(_official_raw(), expected_problem_id="HumanEval/26")


def test_one_candidate_and_one_task_per_official_document_are_required():
    multiple_candidates = _official_raw()
    multiple_candidates["eval"]["HumanEval/8"].append(
        deepcopy(multiple_candidates["eval"]["HumanEval/8"][0])
    )
    with pytest.raises(EvalPlusParseError, match="exactly one candidate"):
        parse_official_result(multiple_candidates, expected_problem_id="HumanEval/8")

    multiple_tasks = _official_raw()
    multiple_tasks["eval"]["HumanEval/26"] = _official_raw("HumanEval/26")["eval"]["HumanEval/26"]
    with pytest.raises(EvalPlusParseError, match="exactly one task"):
        parse_official_result(multiple_tasks, expected_problem_id="HumanEval/8")


def test_versioned_per_task_bundle_is_ordered_by_expected_ids_and_checks_code_hashes():
    raw_8 = _official_raw("HumanEval/8")
    raw_26 = _official_raw(
        "HumanEval/26",
        base_status="pass",
        plus_status="fail",
        plus_fail_tests=[[26]],
    )
    expected_ids = ["HumanEval/8", "HumanEval/26"]

    results = parse_official_results(
        _bundle(raw_26, raw_8),
        expected_problem_ids=expected_ids,
        expected_solution_sha256={
            "HumanEval/8": _solution_hash(raw_8),
            "HumanEval/26": _solution_hash(raw_26),
        },
    )

    assert [result["problem_id"] for result in results] == expected_ids
    assert results[1]["passed_plus"] is False


def test_plain_sequence_is_not_an_accepted_per_task_raw_bundle():
    with pytest.raises(EvalPlusParseError, match="versioned bundle"):
        parse_official_results(
            [_official_raw("HumanEval/26"), _official_raw("HumanEval/8")],
            expected_problem_ids=["HumanEval/8", "HumanEval/26"],
        )


def test_bundle_rejects_duplicate_missing_extra_and_invalid_identity():
    raw_8 = _official_raw("HumanEval/8")
    raw_26 = _official_raw("HumanEval/26")

    with pytest.raises(EvalPlusParseError, match="duplicate task"):
        parse_official_results(
            _bundle(raw_8, raw_8),
            expected_problem_ids=["HumanEval/8", "HumanEval/26"],
        )
    with pytest.raises(EvalPlusParseError, match="expected set"):
        parse_official_results(
            _bundle(raw_8),
            expected_problem_ids=["HumanEval/8", "HumanEval/26"],
        )
    with pytest.raises(EvalPlusParseError, match="expected set"):
        parse_official_results(
            _bundle(raw_8, raw_26),
            expected_problem_ids=["HumanEval/8"],
        )

    invalid_bundle = _bundle(raw_8)
    invalid_bundle["kind"] = "forged_bundle"
    with pytest.raises(EvalPlusParseError, match="identity"):
        parse_official_results(
            invalid_bundle,
            expected_problem_ids=["HumanEval/8"],
        )


def test_solution_fingerprint_mismatch_rejects_stale_official_cache():
    with pytest.raises(EvalPlusParseError, match="fingerprint"):
        parse_official_result(
            _official_raw(),
            expected_problem_id="HumanEval/8",
            expected_solution_sha256="0" * 64,
        )


def test_raw_loader_rejects_duplicate_keys_malformed_utf8_and_size_limit(tmp_path):
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"date":"first","date":"second"}', encoding="utf-8")
    with pytest.raises(EvalPlusParseError, match="unambiguous"):
        load_official_raw_result(duplicate)

    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"value":NaN}', encoding="utf-8")
    with pytest.raises(EvalPlusParseError, match="unambiguous"):
        load_official_raw_result(nonfinite)

    malformed = tmp_path / "malformed.json"
    malformed.write_bytes(b"\xff")
    with pytest.raises(EvalPlusParseError, match="UTF-8"):
        load_official_raw_result(malformed)

    oversized = tmp_path / "oversized.json"
    oversized.write_text("{}", encoding="utf-8")
    with pytest.raises(EvalPlusParseError, match="size limit"):
        load_official_raw_result(oversized, max_bytes=1)


def test_raw_loader_rejects_symlink_and_reads_valid_bundle(tmp_path):
    target = tmp_path / "raw.json"
    payload = _bundle(_official_raw())
    target.write_text(json.dumps(payload), encoding="utf-8")
    assert load_official_raw_result(target) == payload

    link = tmp_path / "raw-link.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are not supported on this platform")
    with pytest.raises(EvalPlusParseError, match="non-symlink"):
        load_official_raw_result(link)


def test_infrastructure_error_is_explicit_and_not_a_candidate_failure():
    result = infrastructure_error_result(
        "HumanEval/8",
        error_type="container_timeout",
    )
    summary = build_summary(
        [result],
        expected_problem_ids=["HumanEval/8"],
    )

    assert result["base_status"] is None
    assert result["plus_status"] is None
    assert result["error_type"] == "container_timeout"
    assert summary["actual_execution_count"] == 0
    assert summary["infrastructure_error_count"] == 1
    assert summary["base_fail_count"] == 0
    assert summary["plus_fail_count"] == 0
    assert summary["wrong_answer_or_candidate_exception_count"] == 0
    assert summary["timeout_count"] == 0


def test_infrastructure_error_constructor_uses_a_safe_allowlist():
    secret = "AUTHORIZATION_SECRET_MUST_NOT_APPEAR"
    with pytest.raises(EvalPlusParseError) as caught:
        infrastructure_error_result("HumanEval/8", error_type=secret)
    assert secret not in str(caught.value)

    results = infrastructure_error_results(
        ["HumanEval/8", "HumanEval/26"],
        error_type="docker_unavailable",
    )
    assert [result["problem_id"] for result in results] == [
        "HumanEval/8",
        "HumanEval/26",
    ]

    cleanup_failed = infrastructure_error_result(
        "HumanEval/8",
        error_type="container_cleanup_failed",
    )
    summary = build_summary([cleanup_failed], expected_problem_ids=["HumanEval/8"])
    assert summary["container_cleanup_failed_count"] == 1


def test_summary_is_recomputed_from_safe_results_with_base_plus_semantics():
    passed = parse_official_result(
        _official_raw("HumanEval/8"),
        expected_problem_id="HumanEval/8",
    )
    plus_failed = parse_official_result(
        _official_raw(
            "HumanEval/26",
            base_status="pass",
            plus_status="fail",
            plus_fail_tests=[[1], [2]],
        ),
        expected_problem_id="HumanEval/26",
    )
    timed_out = parse_official_result(
        _official_raw(
            "HumanEval/41",
            base_status="timeout",
            plus_status="timeout",
        ),
        expected_problem_id="HumanEval/41",
    )
    infrastructure = infrastructure_error_result(
        "HumanEval/51",
        error_type="container_exit_error",
    )
    results = [
        {**passed, "duration_seconds": 1.0},
        {**plus_failed, "duration_seconds": 2.0},
        {**timed_out, "duration_seconds": 3.0},
        {**infrastructure, "duration_seconds": 4.0},
    ]
    expected_ids = ["HumanEval/8", "HumanEval/26", "HumanEval/41", "HumanEval/51"]

    summary = build_summary(results, expected_problem_ids=expected_ids)

    assert summary["total_problem_count"] == 4
    assert summary["actual_execution_count"] == 3
    assert summary["evaluation_complete"] is False
    assert summary["base_pass_count"] == 2
    assert summary["base_pass_rate"] == pytest.approx(2 / 3)
    assert summary["base_plus_pass_count"] == 1
    assert summary["base_plus_pass_rate"] == pytest.approx(1 / 3)
    assert summary["plus_fail_count"] == 1
    assert summary["timeout_count"] == 1
    assert summary["wrong_answer_or_candidate_exception_count"] == 1
    assert summary["infrastructure_error_count"] == 1
    assert summary["observed_plus_failed_test_count"] == 2
    assert summary["average_duration_seconds"] == pytest.approx(2.0)
    validate_summary(summary, results, expected_problem_ids=expected_ids)


def test_summary_validation_rejects_mutated_metrics_and_inconsistent_task_records():
    result = parse_official_result(
        _official_raw(),
        expected_problem_id="HumanEval/8",
    )
    summary = build_summary([result], expected_problem_ids=["HumanEval/8"])
    summary["base_plus_pass_count"] = 0
    with pytest.raises(EvalPlusParseError, match="inconsistent"):
        validate_summary(
            summary,
            [result],
            expected_problem_ids=["HumanEval/8"],
        )

    forged_result = {**result, "passed_plus": False}
    with pytest.raises(EvalPlusParseError, match=r"Base\+Extra"):
        build_summary([forged_result], expected_problem_ids=["HumanEval/8"])


def test_timeout_classification_is_mutually_exclusive_when_other_status_is_fail():
    mixed = parse_official_result(
        _official_raw(
            "HumanEval/8",
            base_status="fail",
            plus_status="timeout",
            base_fail_tests=[[1]],
        ),
        expected_problem_id="HumanEval/8",
    )

    summary = build_summary([mixed], expected_problem_ids=["HumanEval/8"])

    assert mixed["error_type"] == "timeout"
    assert summary["timeout_count"] == 1
    assert summary["wrong_answer_or_candidate_exception_count"] == 0


def test_mock_summary_is_never_labelled_as_real_execution():
    result = infrastructure_error_result("HumanEval/8", error_type="executor_error")
    summary = build_summary(
        [result],
        expected_problem_ids=["HumanEval/8"],
        execution_mode="mock",
    )
    assert summary["execution_mode"] == "mock"
    assert summary["metrics_scope"] == "mock_dry_run_only"
    assert summary["actual_execution_count"] == 0


def test_hidden_inputs_credentials_and_dotenv_content_never_reach_safe_outputs():
    canaries = (
        "HIDDEN_TEST_INPUT_CANARY_61d5",
        "API_KEY_CANARY_61d5",
        "AUTHORIZATION_CANARY_61d5",
        "DOTENV_CONTENT_CANARY_61d5",
    )
    raw = _official_raw(
        solution=f"# {canaries[1]}\ndef candidate():\n    return 1\n",
        base_status="fail",
        plus_status="fail",
        base_fail_tests=[
            {
                "hidden": canaries[0],
                "Authorization": canaries[2],
            }
        ],
        plus_fail_tests=[{".env": canaries[3]}],
    )

    result = parse_official_result(
        raw,
        expected_problem_id="HumanEval/8",
        canaries=canaries,
    )
    summary = build_summary(
        [result],
        expected_problem_ids=["HumanEval/8"],
        canaries=canaries,
    )

    published = json.dumps({"results": [result], "summary": summary}, ensure_ascii=False)
    assert not [canary for canary in canaries if canary in published]


@pytest.mark.parametrize(
    "unsafe_value",
    [
        {"nested": ["RECURSIVE_CANARY"]},
        {"RECURSIVE_CANARY": "safe-looking value"},
        [b"prefix-RECURSIVE_CANARY-suffix"],
    ],
)
def test_recursive_canary_assertion_checks_values_keys_and_bytes_without_echo(unsafe_value):
    with pytest.raises(SensitiveDataLeakError) as caught:
        assert_no_canaries(unsafe_value, ["RECURSIVE_CANARY"])
    assert "RECURSIVE_CANARY" not in str(caught.value)


def test_summary_canary_check_covers_safe_record_extensions():
    result = parse_official_result(
        _official_raw(),
        expected_problem_id="HumanEval/8",
    )
    result["unsafe_extension"] = {"nested": "MANIFEST_SECRET_CANARY"}

    with pytest.raises(SensitiveDataLeakError):
        build_summary(
            [result],
            expected_problem_ids=["HumanEval/8"],
            canaries=["MANIFEST_SECRET_CANARY"],
        )


def test_parser_errors_never_echo_an_unknown_private_task_key():
    private_task_canary = "PRIVATE_TASK_KEY_CANARY"
    raw = _official_raw()
    raw["eval"] = {private_task_canary: raw["eval"]["HumanEval/8"]}

    with pytest.raises(EvalPlusParseError) as caught:
        parse_official_result(raw, expected_problem_id="HumanEval/8")
    assert private_task_canary not in str(caught.value)


def test_invalid_durations_and_infrastructure_records_cannot_corrupt_summary():
    result = parse_official_result(
        _official_raw(),
        expected_problem_id="HumanEval/8",
    )
    for invalid in (-1, float("inf"), float("nan"), True, "1.0"):
        with pytest.raises(EvalPlusParseError, match="duration"):
            build_summary(
                [{**result, "duration_seconds": invalid}],
                expected_problem_ids=["HumanEval/8"],
            )

    infrastructure = infrastructure_error_result(
        "HumanEval/8",
        error_type="missing_raw_result",
    )
    infrastructure["base_status"] = "fail"
    with pytest.raises(EvalPlusParseError, match="infrastructure outcome"):
        build_summary(
            [infrastructure],
            expected_problem_ids=["HumanEval/8"],
        )
