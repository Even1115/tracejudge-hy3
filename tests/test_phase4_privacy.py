from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tracejudge_hy3.phase3.privacy import PublicPayloadError, assert_public_payload_safe
from tracejudge_hy3.phase4 import (
    Phase4GitIdentity,
    Phase4PublicArtifactDigest,
    Phase4PublicReplayReceipt,
    Phase4ReplayRuntime,
    Phase4ReplaySafety,
    PublicArtifactAnchor,
)

NOW = datetime(2026, 9, 1, 7, 7, 55, tzinfo=UTC)
GIT = Phase4GitIdentity(commit="a" * 64, branch="phase4-test", dirty=False)


def _receipt(*, command: str = "tracejudge phase3 replay --certificate public.json"):
    return Phase4PublicReplayReceipt(
        receipt_id="phase4_replay_fixture_v1",
        replay_started_at=NOW,
        replay_completed_at=NOW,
        source_git=GIT,
        certificate_id="certificate:fixture:v1",
        trace_id="counterfactual:fixture:v1",
        problem_id="fixture_problem",
        certificate_sha256="1" * 64,
        certificate_manifest_sha256="2" * 64,
        cohort_manifest_sha256="3" * 64,
        natural_manifest_sha256="4" * 64,
        public_source_sha256="5" * 64,
        execution_evidence_sha256="6" * 64,
        replay_command=command,
        runtime=Phase4ReplayRuntime(
            python_version="3.12.14",
            replay_implementation_sha256="7" * 64,
            direct_dependencies_sha256="8" * 64,
        ),
        safety=Phase4ReplaySafety(),
    )


def test_public_digest_contains_only_logical_anchors_and_hashes():
    digest = Phase4PublicArtifactDigest(
        digest_id="phase4_digest_fixture_v1",
        inventory_id="phase4_inventory_fixture_v1",
        created_at=NOW,
        source_git=GIT,
        artifact_set_sha256="8" * 64,
        private_inventory_sha256="9" * 64,
        private_artifact_count=40,
        permission_warning_count=0,
        public_anchor_count=1,
        public_anchors=(
            PublicArtifactAnchor(
                artifact_id="phase3_report_markdown",
                sha256="a" * 64,
                size_bytes=123,
            ),
        ),
        privacy_review_status="passed",
    )
    assert_public_payload_safe(digest)
    payload = json.dumps(digest.model_dump(mode="json"), sort_keys=True)
    assert "/Users/" not in payload
    assert "artifacts/experiments" not in payload


def test_public_receipt_canary_scan_is_fail_closed_without_echoing_secret():
    canary = "phase4-private-canary-value"
    receipt = _receipt(command=f"tracejudge phase3 replay --certificate {canary}")
    with pytest.raises(PublicPayloadError) as captured:
        assert_public_payload_safe(receipt, canaries=(canary,))
    assert canary not in str(captured.value)


def test_public_receipt_declares_every_sensitive_payload_absent():
    receipt = _receipt()
    assert_public_payload_safe(receipt)
    assert receipt.safety.contains_candidate_source is False
    assert receipt.safety.contains_counterexample_inputs is False
    assert receipt.safety.contains_provider_raw is False
    assert receipt.safety.contains_hidden_evaluation_content is False
    assert receipt.safety.contains_per_trace_predictions is False


def test_formal_public_replay_receipt_is_schema_valid_and_hash_bound():
    receipt_path = (
        Path(__file__).parents[1] / "docs/releases/phase4/phase4_public_replay_receipt_v1.json"
    )
    receipt = Phase4PublicReplayReceipt.model_validate_json(receipt_path.read_bytes())

    assert receipt.source_git.commit == "1de193266d4be57df412614cbc06f4da0eb5868c"
    assert receipt.source_git.dirty is False
    assert (
        receipt.execution_evidence_sha256
        == "cfd897334643853fc10901835a5203aa51ee7edd4442e314893c1e5bc152e670"
    )
    assert receipt.runtime.replay_implementation_sha256 == (
        "0c94426c64959f9213ca12a2df4da940f253eae6c807a6a56ba678c663abd677"
    )
    assert_public_payload_safe(receipt)


def test_formal_public_artifact_digest_is_schema_valid_hash_bound_and_deidentified():
    digest_path = (
        Path(__file__).parents[1] / "docs/releases/phase4/phase4_public_artifact_digest_v1.json"
    )
    digest = Phase4PublicArtifactDigest.model_validate_json(digest_path.read_bytes())

    assert digest.source_git.commit == "065085bfa27795d6432e1fcf8b6421103f0b00e8"
    assert digest.source_git.dirty is False
    assert digest.artifact_set_sha256 == (
        "84c584a116700430b7fea14c5f81d8b23f6094badc1dc410a013c7bd7615f13b"
    )
    assert digest.private_inventory_sha256 == (
        "ad2e4489d608b8bdb21a3a108eb4eba5ca078f8db5b748cd6d6669d58d1ab997"
    )
    assert digest.private_artifact_count == 103
    assert digest.public_anchor_count == 13
    assert digest.permission_warning_count == 0
    assert digest.privacy_review_status == "passed"
    assert_public_payload_safe(digest)
