from __future__ import annotations

import pytest
from mbppplus_fixtures import official_raw_document, sha256_text

from tracejudge_hy3.evalplus_mbpp.parser import (
    RAW_BUNDLE_KIND,
    EvalPlusParseError,
    build_summary,
    infrastructure_error_result,
    infrastructure_error_results,
    parse_official_result,
    parse_official_results,
)

IDS = ("Mbpp/2", "Mbpp/601")
SOLUTION_A = "def solve_task_0(v):\n    return v\n"
SOLUTION_B = "def solve_task_1(v):\n    return v[::-1]\n"
FAILURE_CANARY = "PRIVATE_EVALPLUS_FAILURE_INPUT_CANARY"


def _bundle(*documents: dict) -> dict:
    return {"schema_version": 1, "kind": RAW_BUNDLE_KIND, "raw_results": list(documents)}


def test_parse_single_pass_result_keeps_only_safe_fields():
    payload = official_raw_document("Mbpp/2", SOLUTION_A)
    result = parse_official_result(
        payload,
        expected_problem_id="Mbpp/2",
        expected_solution_sha256=sha256_text(SOLUTION_A),
    )
    assert result["problem_id"] == "Mbpp/2"
    assert result["base_status"] == result["plus_status"] == "pass"
    assert result["passed_base"] is True
    assert result["passed_plus"] is True
    assert result["error_type"] is None
    assert result["infrastructure_status"] == "ok"
    assert result["solution_sha256"] == sha256_text(SOLUTION_A)
    assert "solution" not in result


def test_parse_counts_failures_without_copying_failing_inputs():
    payload = official_raw_document(
        "Mbpp/2",
        SOLUTION_A,
        base_status="fail",
        plus_status="fail",
        base_fail_tests=[FAILURE_CANARY, "x"],
        plus_fail_tests=[FAILURE_CANARY],
    )
    result = parse_official_result(payload, expected_problem_id="Mbpp/2")
    assert result["base_fail_test_count"] == 2
    assert result["plus_fail_test_count"] == 1
    assert result["error_type"] == "wrong_answer_or_candidate_exception"
    assert FAILURE_CANARY not in repr(result)


def test_parse_timeout_status():
    payload = official_raw_document(
        "Mbpp/2", SOLUTION_A, base_status="timeout", plus_status="timeout"
    )
    result = parse_official_result(payload, expected_problem_id="Mbpp/2")
    assert result["error_type"] == "timeout"
    assert result["passed_plus"] is False


def test_parse_bundle_in_expected_order():
    payload = _bundle(
        official_raw_document("Mbpp/601", SOLUTION_B),
        official_raw_document("Mbpp/2", SOLUTION_A),
    )
    results = parse_official_results(payload, expected_problem_ids=IDS)
    assert [result["problem_id"] for result in results] == list(IDS)


def test_parse_rejects_wrong_task_id_and_duplicate_task():
    payload = official_raw_document("HumanEval/2", SOLUTION_A)
    with pytest.raises(EvalPlusParseError, match="task ID is invalid"):
        parse_official_result(payload, expected_problem_id="Mbpp/2")

    duplicate = _bundle(
        official_raw_document("Mbpp/2", SOLUTION_A),
        official_raw_document("Mbpp/2", SOLUTION_A),
    )
    with pytest.raises(EvalPlusParseError, match="duplicate task"):
        parse_official_results(duplicate, expected_problem_ids=["Mbpp/2"])

    unexpected = _bundle(official_raw_document("Mbpp/601", SOLUTION_B))
    with pytest.raises(EvalPlusParseError, match="differ from the expected set"):
        parse_official_results(unexpected, expected_problem_ids=["Mbpp/2"])


def test_parse_rejects_solution_hash_mismatch():
    payload = official_raw_document("Mbpp/2", SOLUTION_A)
    with pytest.raises(EvalPlusParseError, match="fingerprint differs"):
        parse_official_result(
            payload,
            expected_problem_id="Mbpp/2",
            expected_solution_sha256="0" * 64,
        )


def test_parse_rejects_canary_in_official_payload():
    from tracejudge_hy3.evalplus.parser import SensitiveDataLeakError

    payload = official_raw_document("Mbpp/2", SOLUTION_A)
    results = parse_official_results(payload, expected_problem_ids=["Mbpp/2"])
    with pytest.raises(SensitiveDataLeakError):
        parse_official_results(
            payload,
            expected_problem_ids=["Mbpp/2"],
            canaries=[results[0]["solution_sha256"]],
        )


def test_infrastructure_results_are_distinct_from_candidate_failures():
    result = infrastructure_error_result("Mbpp/2", error_type="docker_unavailable")
    assert result["infrastructure_status"] == "error"
    assert result["base_status"] is None
    assert result["plus_status"] is None
    assert result["passed_base"] is False
    assert result["error_type"] == "docker_unavailable"
    assert result["solution_sha256"] is None

    results = infrastructure_error_results(IDS, error_type="batch_timeout")
    assert [item["problem_id"] for item in results] == list(IDS)

    with pytest.raises(EvalPlusParseError):
        infrastructure_error_result("Mbpp/2", error_type="wrong_answer")


def test_build_summary_separates_execution_failures_and_infrastructure():
    executed_pass = parse_official_result(
        official_raw_document("Mbpp/2", SOLUTION_A), expected_problem_id="Mbpp/2"
    )
    executed_fail = parse_official_result(
        official_raw_document(
            "Mbpp/601",
            SOLUTION_B,
            plus_status="fail",
            plus_fail_tests=["x", "y"],
        ),
        expected_problem_id="Mbpp/601",
    )
    infra = infrastructure_error_result("Mbpp/7", error_type="batch_timeout")
    ids = ["Mbpp/2", "Mbpp/601", "Mbpp/7"]
    summary = build_summary([executed_pass, executed_fail, infra], expected_problem_ids=ids)

    assert summary["total_problem_count"] == 3
    assert summary["actual_execution_count"] == 2
    assert summary["base_pass_count"] == 2
    assert summary["base_plus_pass_count"] == 1
    assert summary["base_plus_pass_rate"] == 0.5
    assert summary["plus_fail_count"] == 1
    assert summary["wrong_answer_or_candidate_exception_count"] == 1
    assert summary["timeout_count"] == 0
    assert summary["infrastructure_error_count"] == 1
    assert summary["batch_timeout_count"] == 1
    assert summary["evaluation_complete"] is False
    assert summary["execution_error_count"] is None


def test_build_summary_rejects_inconsistent_safe_result():
    executed_pass = parse_official_result(
        official_raw_document("Mbpp/2", SOLUTION_A), expected_problem_id="Mbpp/2"
    )
    tampered = dict(executed_pass)
    tampered["passed_plus"] = False
    with pytest.raises(EvalPlusParseError, match="inconsistent"):
        build_summary([tampered], expected_problem_ids=["Mbpp/2"])
