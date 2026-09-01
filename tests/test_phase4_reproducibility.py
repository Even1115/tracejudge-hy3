from __future__ import annotations

import hashlib
import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tracejudge_hy3.cli import app
from tracejudge_hy3.phase3.replay import PublicCertificateReplayResult
from tracejudge_hy3.phase4 import (
    ArtifactSpec,
    Phase4ArtifactInventory,
    Phase4GitIdentity,
    Phase4PublicArtifactDigest,
    Phase4PublicReplayReceipt,
    Phase4ReproducibilityError,
    freeze_artifact_inventory,
    preflight_artifact_inventory,
    prepare_public_replay_receipt,
    verify_artifact_inventory,
    write_public_replay_receipt,
)

NOW = datetime(2026, 9, 1, 7, 7, 55, tzinfo=UTC)
GIT = Phase4GitIdentity(commit="a" * 64, branch="phase4-test", dirty=False)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write(path: Path, payload: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(mode)


def _inventory_fixture(root: Path) -> tuple[ArtifactSpec, ...]:
    _write(root / "artifacts/private/labels.jsonl", b'{"label":"fixture"}\n', mode=0o600)
    _write(root / "artifacts/aggregate/report.json", b'{"count":1}\n', mode=0o644)
    _write(root / "artifacts/public/certificate.json", b'{"verdict":"confirmed"}\n', mode=0o644)
    return (
        ArtifactSpec(
            "private_labels",
            "artifacts/private/labels.jsonl",
            "private_restricted",
        ),
        ArtifactSpec(
            "aggregate_report",
            "artifacts/aggregate/report.json",
            "deidentified_aggregate",
            True,
        ),
        ArtifactSpec(
            "public_certificate",
            "artifacts/public/certificate.json",
            "public_fixture",
            True,
        ),
    )


def _receipt_files(root: Path) -> dict[str, str]:
    paths = {
        "certificate_path": "artifacts/certificate.json",
        "certificate_manifest_path": "artifacts/certificate-manifest.json",
        "cohort_manifest_path": "artifacts/cohort.json",
        "natural_manifest_path": "artifacts/natural.json",
        "source_bundle_path": "data/public-source.json",
    }
    for index, relative_path in enumerate(paths.values()):
        _write(root / relative_path, f'{{"fixture":{index}}}\n'.encode(), mode=0o600)
    return paths


def _fake_replay(**_kwargs: object) -> PublicCertificateReplayResult:
    return PublicCertificateReplayResult(
        certificate_id="certificate:fixture:v1",
        trace_id="counterfactual:fixture:v1",
        problem_id="fixture_problem",
        verified=True,
        reproduced_failure=True,
        execution_evidence_sha256="b" * 64,
        sandbox_backend="trusted-local",
        executed_case_count=1,
    )


def test_phase4_cli_commands_are_registered_without_execution():
    result = CliRunner().invoke(app, ["phase4", "--help"])
    assert result.exit_code == 0
    for command in (
        "artifact-preflight",
        "artifact-freeze",
        "artifact-verify",
        "replay-receipt-preflight",
        "replay-receipt",
    ):
        assert command in result.stdout


def test_inventory_preflight_hashes_only_allowlisted_files_without_public_paths(tmp_path: Path):
    specs = _inventory_fixture(tmp_path)
    result = preflight_artifact_inventory(
        repo_root=tmp_path,
        inventory_id="phase4_inventory_fixture_v1",
        artifact_specs=specs,
        created_at=NOW,
        git_identity=GIT,
    )

    assert result.inventory.artifact_count == 3
    assert result.inventory.permission_warning_count == 0
    assert result.public_digest.public_anchor_count == 2
    assert result.public_digest.artifact_set_sha256 == result.inventory.artifact_set_sha256
    assert result.public_digest.private_inventory_sha256 == result.private_manifest_sha256
    assert b"artifacts/private/labels.jsonl" in result.private_manifest_payload
    assert b"artifacts/private/labels.jsonl" not in result.public_digest_payload
    assert b'"label"' not in result.public_digest_payload
    Phase4ArtifactInventory.model_validate_json(result.private_manifest_payload)
    Phase4PublicArtifactDigest.model_validate_json(result.public_digest_payload)


def test_inventory_artifact_set_digest_is_independent_of_capture_time(tmp_path: Path):
    specs = _inventory_fixture(tmp_path)
    first = preflight_artifact_inventory(
        repo_root=tmp_path,
        inventory_id="phase4_inventory_fixture_v1",
        artifact_specs=specs,
        created_at=NOW,
        git_identity=GIT,
    )
    second = preflight_artifact_inventory(
        repo_root=tmp_path,
        inventory_id="phase4_inventory_fixture_v1",
        artifact_specs=specs,
        created_at=NOW + timedelta(seconds=1),
        git_identity=GIT,
    )

    assert first.inventory.artifact_set_sha256 == second.inventory.artifact_set_sha256
    assert first.private_manifest_sha256 != second.private_manifest_sha256


def test_inventory_rejects_private_mode_broadening_unless_explicitly_recorded(tmp_path: Path):
    specs = _inventory_fixture(tmp_path)
    (tmp_path / "artifacts/private/labels.jsonl").chmod(0o644)

    with pytest.raises(Phase4ReproducibilityError) as captured:
        preflight_artifact_inventory(
            repo_root=tmp_path,
            inventory_id="phase4_inventory_fixture_v1",
            artifact_specs=specs,
            created_at=NOW,
            git_identity=GIT,
        )
    assert captured.value.safe_stage == "P4B_PERMISSIONS"

    recorded = preflight_artifact_inventory(
        repo_root=tmp_path,
        inventory_id="phase4_inventory_fixture_v1",
        artifact_specs=specs,
        created_at=NOW,
        git_identity=GIT,
        allow_permission_warnings=True,
    )
    assert recorded.inventory.permission_warning_count == 1
    assert recorded.public_digest.privacy_review_status == "permission_hardening_required"


def test_inventory_rejects_symlinks_and_paths_outside_root(tmp_path: Path):
    target = tmp_path / "target.json"
    _write(target, b"{}\n", mode=0o600)
    link = tmp_path / "artifacts/link.json"
    link.parent.mkdir()
    link.symlink_to(target)

    with pytest.raises(Phase4ReproducibilityError) as captured:
        preflight_artifact_inventory(
            repo_root=tmp_path,
            inventory_id="phase4_inventory_fixture_v1",
            artifact_specs=(ArtifactSpec("linked", "artifacts/link.json", "private_restricted"),),
            created_at=NOW,
            git_identity=GIT,
        )
    assert captured.value.safe_stage == "P4B_ARTIFACT"


def test_inventory_freeze_and_restore_verification_are_atomic_and_mode_bound(tmp_path: Path):
    specs = _inventory_fixture(tmp_path)
    result = freeze_artifact_inventory(
        repo_root=tmp_path,
        inventory_id="phase4_inventory_fixture_v1",
        artifact_specs=specs,
        created_at=NOW,
        git_identity=GIT,
        private_output_dir=tmp_path / "private-output",
        public_output_dir=tmp_path / "public-output",
    )

    assert stat.S_IMODE(result.private_manifest_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(result.private_manifest_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(result.public_digest_path.stat().st_mode) == 0o644
    assert _sha(result.private_manifest_path.read_bytes()) == result.private_manifest_sha256
    verified = verify_artifact_inventory(
        repo_root=tmp_path,
        manifest_path=result.private_manifest_path,
    )
    assert verified.verified is True
    assert verified.artifact_count == 3

    with pytest.raises(Phase4ReproducibilityError):
        freeze_artifact_inventory(
            repo_root=tmp_path,
            inventory_id="phase4_inventory_fixture_v1",
            artifact_specs=specs,
            created_at=NOW,
            git_identity=GIT,
            private_output_dir=tmp_path / "private-output",
            public_output_dir=tmp_path / "public-output",
        )


def test_restore_verification_rejects_hash_size_or_mode_drift(tmp_path: Path):
    specs = _inventory_fixture(tmp_path)
    result = freeze_artifact_inventory(
        repo_root=tmp_path,
        inventory_id="phase4_inventory_fixture_v1",
        artifact_specs=specs,
        created_at=NOW,
        git_identity=GIT,
        private_output_dir=tmp_path / "private-output",
        public_output_dir=tmp_path / "public-output",
    )
    (tmp_path / "artifacts/private/labels.jsonl").write_bytes(b"changed\n")

    with pytest.raises(Phase4ReproducibilityError) as captured:
        verify_artifact_inventory(
            repo_root=tmp_path,
            manifest_path=result.private_manifest_path,
        )
    assert captured.value.safe_stage == "P4B_VERIFY_MISMATCH"


def test_restore_verification_rejects_tampered_artifact_set_digest(tmp_path: Path):
    specs = _inventory_fixture(tmp_path)
    result = freeze_artifact_inventory(
        repo_root=tmp_path,
        inventory_id="phase4_inventory_fixture_v1",
        artifact_specs=specs,
        created_at=NOW,
        git_identity=GIT,
        private_output_dir=tmp_path / "private-output",
        public_output_dir=tmp_path / "public-output",
    )
    payload = json.loads(result.private_manifest_path.read_text())
    payload["artifact_set_sha256"] = "f" * 64
    result.private_manifest_path.write_text(json.dumps(payload))

    with pytest.raises(Phase4ReproducibilityError) as captured:
        verify_artifact_inventory(
            repo_root=tmp_path,
            manifest_path=result.private_manifest_path,
        )
    assert captured.value.safe_stage == "P4B_VERIFY"


def test_public_replay_receipt_binds_inputs_without_copying_content(tmp_path: Path):
    paths = _receipt_files(tmp_path)
    receipt = prepare_public_replay_receipt(
        receipt_id="phase4_replay_fixture_v1",
        repo_root=tmp_path,
        replay_started_at=NOW,
        git_identity=GIT,
        replay=_fake_replay,
        **paths,
    )

    assert receipt.reproduced_failure is True
    assert receipt.evidence_hash_verified is True
    assert receipt.execution_evidence_sha256 == "b" * 64
    assert receipt.safety.provider_call_count == 0
    assert receipt.safety.docker_call_count == 0
    assert receipt.safety.network_call_count == 0
    assert receipt.certificate_sha256 == _sha((tmp_path / paths["certificate_path"]).read_bytes())
    payload = json.dumps(receipt.model_dump(mode="json"), sort_keys=True)
    assert 'fixture":0' not in payload


def test_public_replay_receipt_writer_is_non_overwriting(tmp_path: Path):
    paths = _receipt_files(tmp_path)
    arguments = {
        "receipt_id": "phase4_replay_fixture_v1",
        "repo_root": tmp_path,
        "replay_started_at": NOW,
        "git_identity": GIT,
        "replay": _fake_replay,
        **paths,
    }
    result = write_public_replay_receipt(
        output_dir=tmp_path / "public-output",
        **arguments,
    )
    assert stat.S_IMODE(result.receipt_path.stat().st_mode) == 0o644
    Phase4PublicReplayReceipt.model_validate_json(result.receipt_path.read_bytes())

    with pytest.raises(Phase4ReproducibilityError) as captured:
        write_public_replay_receipt(
            output_dir=tmp_path / "public-output",
            **arguments,
        )
    assert captured.value.safe_stage == "P4B_OUTPUT"
