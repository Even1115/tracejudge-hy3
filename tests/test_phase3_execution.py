from __future__ import annotations

import hashlib
import json
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

import tracejudge_hy3.cli as cli_module
from tracejudge_hy3.cli import app
from tracejudge_hy3.config import Settings
from tracejudge_hy3.phase3.annotations import (
    ANNOTATION_GUIDE_RELATIVE_PATH,
    ANNOTATION_GUIDE_SHA256,
    ANNOTATION_PROTOCOL_RELATIVE_PATH,
    ANNOTATION_PROTOCOL_SHA256,
)
from tracejudge_hy3.phase3.contracts import AnnotationSetManifest
from tracejudge_hy3.phase3.execution import (
    Phase3EvaluationPreflight,
    _hy3_public_configuration,
    execute_phase3_evaluation,
    load_annotation_set_binding,
)
from tracejudge_hy3.phase3.runner import Phase3RunnerError
from tracejudge_hy3.resources import data_path

H0 = "0" * 64
H1 = "1" * 64
H2 = "2" * 64
H3 = "3" * 64
H4 = "4" * 64
NOW = datetime(2026, 8, 31, tzinfo=UTC)


def _write_private(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    path.chmod(0o600)


def _annotation_fixture(tmp_path: Path) -> tuple[Path, str, bytes, bytes]:
    root = tmp_path / "phase3_labels_primary_round1_v1"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    completed = b'{"status":"completed"}\n'
    annotations = b'{"trace_id":"natural:fixture"}\n'
    _write_private(root / "completed_labels.jsonl", completed)
    _write_private(root / "annotations.jsonl", annotations)
    manifest = AnnotationSetManifest(
        annotation_set_id="phase3_labels_primary_round1_v1",
        annotation_protocol_sha256=ANNOTATION_PROTOCOL_SHA256,
        annotation_guide_sha256=ANNOTATION_GUIDE_SHA256,
        frozen_cohort_manifest_sha256=H0,
        source_packet_id="phase3_annotation_primary_round1_v1",
        source_packet_manifest_sha256=H1,
        source_packet_sha256=H2,
        source_identity_map_sha256=H3,
        source_labels_template_sha256=H4,
        source_completed_labels_sha256=H0,
        completed_labels_sha256=hashlib.sha256(completed).hexdigest(),
        ordered_trace_ids=("natural:fixture",),
        record_count=1,
        natural_trace_count=1,
        counterfactual_trace_count=0,
        annotation_records_sha256=hashlib.sha256(annotations).hexdigest(),
        rater_ids=("primary_rater",),
        annotation_rounds=(1,),
        agreement_kind="not_computed",
        created_at=NOW,
    )
    payload = (
        json.dumps(
            manifest.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        ).encode()
        + b"\n"
    )
    _write_private(root / "manifest.json", payload)
    return root / "manifest.json", hashlib.sha256(payload).hexdigest(), completed, annotations


def test_annotation_binding_verifies_private_payloads_without_parsing_labels(tmp_path: Path):
    manifest_path, manifest_sha, _completed, _annotations = _annotation_fixture(tmp_path)

    loaded = load_annotation_set_binding(
        manifest_path=manifest_path,
        expected_manifest_sha256=manifest_sha,
        frozen_manifest_sha256=H0,
        ordered_trace_ids=("natural:fixture",),
        natural_trace_count=1,
        counterfactual_trace_count=0,
        protocol_path=data_path(ANNOTATION_PROTOCOL_RELATIVE_PATH),
        guide_path=Path(ANNOTATION_GUIDE_RELATIVE_PATH),
    )

    assert loaded.manifest.annotation_set_id == "phase3_labels_primary_round1_v1"
    assert loaded.manifest_sha256 == manifest_sha


def test_annotation_binding_rejects_tampered_payload_and_broad_permissions(tmp_path: Path):
    manifest_path, manifest_sha, completed, _annotations = _annotation_fixture(tmp_path)
    completed_path = manifest_path.parent / "completed_labels.jsonl"
    _write_private(completed_path, completed + b"tampered\n")

    with pytest.raises(Phase3RunnerError, match="payload hash differs"):
        load_annotation_set_binding(
            manifest_path=manifest_path,
            expected_manifest_sha256=manifest_sha,
            frozen_manifest_sha256=H0,
            ordered_trace_ids=("natural:fixture",),
            natural_trace_count=1,
            counterfactual_trace_count=0,
            protocol_path=data_path(ANNOTATION_PROTOCOL_RELATIVE_PATH),
            guide_path=Path(ANNOTATION_GUIDE_RELATIVE_PATH),
        )

    _write_private(completed_path, completed)
    completed_path.chmod(0o644)
    assert stat.S_IMODE(completed_path.stat().st_mode) == 0o644
    with pytest.raises(Phase3RunnerError, match="permissions are too broad"):
        load_annotation_set_binding(
            manifest_path=manifest_path,
            expected_manifest_sha256=manifest_sha,
            frozen_manifest_sha256=H0,
            ordered_trace_ids=("natural:fixture",),
            natural_trace_count=1,
            counterfactual_trace_count=0,
            protocol_path=data_path(ANNOTATION_PROTOCOL_RELATIVE_PATH),
            guide_path=Path(ANNOTATION_GUIDE_RELATIVE_PATH),
        )


def test_hy3_public_configuration_contains_no_endpoint_or_key():
    settings = Settings(
        hy3_base_url="https://user:password@hy3.invalid/v1/?token=secret",
        hy3_api_key="unit-test-api-key",
        hy3_model="unit-test-model",
        hy3_reasoning_effort="high",
        hy3_enable_reasoning_effort=True,
    )

    configuration = _hy3_public_configuration(settings, model="unit-test-model")

    serialized = json.dumps(configuration, sort_keys=True)
    assert "unit-test-api-key" not in serialized
    assert "user" not in serialized
    assert "password" not in serialized
    assert "token" not in serialized
    assert configuration["provider"] == "hy3"
    assert configuration["transport_max_retries"] == 0


async def test_execution_requires_explicit_real_provider_confirmation_before_preparation():
    with pytest.raises(Phase3RunnerError) as exc_info:
        await execute_phase3_evaluation(confirm_real_provider=False)
    assert exc_info.value.safe_stage == "P3E_REAL_PROVIDER_CONFIRMATION"


def test_cli_evaluate_preflight_is_read_only(monkeypatch: pytest.MonkeyPatch):
    expected = Phase3EvaluationPreflight(
        run_id="phase3_hy3_57x5_v1",
        resume=False,
        annotation_set_id="phase3_labels_primary_round1_v1",
        natural_trace_count=42,
        counterfactual_trace_count=15,
        trace_count=57,
        method_count=5,
        pair_count=285,
        provider_pair_count=228,
        maximum_provider_call_count=456,
        method_specs_sha256=H0,
        prompt_bundle_sha256=H1,
        output_schema_sha256=H2,
        material_payloads_sha256=H3,
        provider_config_sha256=H4,
        annotation_set_manifest_sha256=H0,
        completed_labels_sha256=H1,
        annotation_records_sha256=H2,
        resume_identity_sha256=H3,
        provider="hy3",
        model="unit-test-model",
        git_commit="a" * 40,
        git_branch="phase3-process-evaluation",
        git_dirty=False,
    )
    calls: list[dict[str, object]] = []

    def fake_preflight(**kwargs):
        calls.append(kwargs)
        return expected

    monkeypatch.setattr(cli_module, "preflight_phase3_evaluation", fake_preflight)
    result = CliRunner().invoke(
        app,
        [
            "phase3",
            "evaluate-preflight",
            "--run-id",
            "phase3_hy3_57x5_v1",
            "--cohort-manifest",
            "cohort.json",
            "--natural-manifest",
            "natural.json",
            "--annotation-set-manifest",
            "labels.json",
            "--annotation-set-manifest-sha256",
            H0,
            "--phase1-run",
            "phase1",
            "--phase2-run",
            "phase2",
            "--dataset-manifest",
            "dataset.json",
            "--execution-run",
            "public-evidence",
            "--model",
            "unit-test-model",
        ],
    )

    assert result.exit_code == 0
    assert "57 / 5 / 285" in result.stdout
    assert "228 / 456" in result.stdout
    assert "否 / 否 / 否 / 否" in result.stdout
    assert len(calls) == 1


def test_cli_evaluate_requires_explicit_real_provider_confirmation():
    result = CliRunner().invoke(
        app,
        [
            "phase3",
            "evaluate",
            "--run-id",
            "phase3_hy3_57x5_v1",
            "--cohort-manifest",
            "cohort.json",
            "--natural-manifest",
            "natural.json",
            "--annotation-set-manifest",
            "labels.json",
            "--annotation-set-manifest-sha256",
            H0,
            "--phase1-run",
            "phase1",
            "--phase2-run",
            "phase2",
            "--dataset-manifest",
            "dataset.json",
            "--execution-run",
            "public-evidence",
            "--model",
            "unit-test-model",
        ],
    )

    assert result.exit_code == 1
    assert "P3E_REAL_PROVIDER_CONFIRMATION" in result.stdout
