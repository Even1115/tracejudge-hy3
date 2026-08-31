from __future__ import annotations

import hashlib

import pytest

from tracejudge_hy3.phase3.privacy import (
    PublicPayloadError,
    assert_public_payload_safe,
    canonical_json_bytes,
    canonical_sha256,
    jsonl_record_sha256,
)


def test_canonical_json_hash_is_order_independent_and_utf8_stable():
    left = {"说明": "边界", "nested": {"b": 2, "a": 1}}
    right = {"nested": {"a": 1, "b": 2}, "说明": "边界"}

    assert canonical_json_bytes(left) == canonical_json_bytes(right)
    assert canonical_sha256(left) == canonical_sha256(right)
    assert b"\\u" not in canonical_json_bytes(left)


def test_canonical_json_rejects_non_finite_values():
    with pytest.raises(ValueError):
        canonical_json_bytes({"metric": float("nan")})


def test_jsonl_record_hash_includes_final_lf_and_rejects_non_records():
    record = b'{"trace_id":"t1"}\n'
    assert jsonl_record_sha256(record) == hashlib.sha256(record).hexdigest()

    for invalid in (b"", b"\n", record.rstrip(b"\n"), record + b"{}\n", b"\xff\n"):
        with pytest.raises(ValueError):
            jsonl_record_sha256(invalid)


def test_public_payload_rejects_nested_sensitive_key_without_echoing_value():
    secret = "must-never-be-echoed"
    with pytest.raises(PublicPayloadError) as exc_info:
        assert_public_payload_safe({"safe": {"Authorization": secret}})

    assert "$.safe.Authorization" in str(exc_info.value)
    assert secret not in str(exc_info.value)


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "canonical_solution",
        "official_failure_inputs",
        "official_test_inputs",
        "evalplus_raw",
        "reference_code",
        "access_token",
    ],
)
def test_public_payload_rejects_phase_three_private_fields(forbidden_key):
    with pytest.raises(PublicPayloadError):
        assert_public_payload_safe({forbidden_key: "private"})


def test_public_payload_rejects_value_and_key_canaries_without_echoing_them():
    canary = "OFFICIAL-HIDDEN-INPUT-CANARY"
    for payload in ({"safe": canary}, {canary: "safe"}):
        with pytest.raises(PublicPayloadError) as exc_info:
            assert_public_payload_safe(payload, canaries=(canary,))
        assert canary not in str(exc_info.value)


def test_public_payload_allows_safe_hashes_and_method_policy_names():
    payload = {
        "trace_id": "natural:1",
        "code_sha256": "a" * 64,
        "forbidden_inputs": ["canonical_solution", "official_failure_inputs"],
        "source": {"phase2_run_id": "phase2_safe"},
    }
    assert_public_payload_safe(payload)
