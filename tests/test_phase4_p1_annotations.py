from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import tracejudge_hy3.cli as cli_module
import tracejudge_hy3.phase4.p1_annotations as p1_module
from tracejudge_hy3.cli import app
from tracejudge_hy3.phase4 import (
    P1_ARRANGEMENT_SHA256,
    P1_PRACTICE_SOURCE_SHA256,
    P1_PROTOCOL_SHA256,
    Phase4P1AnnotationError,
    preflight_p1_practice_bundle,
    verify_p1_practice_bundle,
    write_p1_practice_bundle,
)

REPO_ROOT = Path(__file__).parents[1]
ARRANGEMENT = REPO_ROOT / "docs/experiments/phase4_p1_second_annotator_arrangement_v1.md"
PROTOCOL = REPO_ROOT / "data/phase4/p1_second_annotator_protocol_v1.json"
SOURCE = REPO_ROOT / "data/phase4/p1_public_practice_source_v1.json"
PHASE3_GUIDE = REPO_ROOT / "docs/experiments/phase3_annotation_guide_v1.md"
FROZEN_BUNDLE = REPO_ROOT / "docs/experiments/phase4_p1_practice/phase4_p1_public_practice_v1"
FROZEN_BUNDLE_HASHES = {
    "manifest.json": "cc6beef9b439a42a3011700096a9e8541edad211d5ea1733b47d07c9ad8ce855",
    "participant/packet.jsonl": "1c5ab229dbfa81203f17a4ec50f0c783593b4e60d2e7dfd3e766a9349f33610b",
    "participant/labels_template.jsonl": (
        "887dbbb79410c1397c44c2136bbb6ca9967311bb2c1c46c96f107ffc602b3409"
    ),
}
FROZEN_COORDINATOR_REFERENCE_SHA256 = (
    "3562185b1e05d0710b89410d3fcac22709a2deb668ed7a7b9f36415a32b10929"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _patch_empty_frozen_cohort(monkeypatch: pytest.MonkeyPatch) -> None:
    cohort = SimpleNamespace(parents=(), counterfactuals=())
    natural = SimpleNamespace(traces=())
    monkeypatch.setattr(
        p1_module,
        "_load_phase3_manifests",
        lambda **_kwargs: (cohort, natural),
    )


def _patch_private_references(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use non-authoritative synthetic roles so tests never require private answers."""

    packet_rows = tuple(
        json.loads(line)
        for line in (FROZEN_BUNDLE / "participant/packet.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    references = []
    for row in packet_rows:
        has_error = row["functional_evidence"]["execution_status"] == "fail"
        references.append(
            p1_module.P1PracticeReferenceRecord(
                practice_item_id=row["practice_item_id"],
                problem_id=row["problem_id"],
                code_sha256=row["code_sha256"],
                structured_explanation_sha256=row["structured_explanation_sha256"],
                functional_evidence_sha256=row["functional_evidence_sha256"],
                reference_annotation=p1_module.P1ReferenceAnnotation(
                    process_correct=not has_error,
                    has_error=has_error,
                    reasoning_correct=not has_error,
                    plan_code_aligned=True,
                    first_faulty_layer="implementation" if has_error else None,
                    first_faulty_step="S1" if has_error else None,
                    error_type="C01_BOUNDARY_ERROR" if has_error else None,
                    rationale="Synthetic role used only by the automated test suite.",
                ),
            )
        )
    monkeypatch.setattr(
        p1_module,
        "_load_private_references",
        lambda *_args, **_kwargs: (b"synthetic-private-reference\n", tuple(references)),
    )


def _synthetic_private_reference_payload() -> bytes:
    """Build schema-valid synthetic rows without reading production references."""

    packet_rows = tuple(
        json.loads(line)
        for line in (FROZEN_BUNDLE / "participant/packet.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    layers = iter(("alignment", "reasoning", "requirement"))
    records = []
    for row in packet_rows:
        has_error = row["functional_evidence"]["execution_status"] == "fail"
        layer = next(layers) if has_error else None
        records.append(
            p1_module.P1PracticeReferenceRecord(
                practice_item_id=row["practice_item_id"],
                problem_id=row["problem_id"],
                code_sha256=row["code_sha256"],
                structured_explanation_sha256=row["structured_explanation_sha256"],
                functional_evidence_sha256=row["functional_evidence_sha256"],
                reference_annotation=p1_module.P1ReferenceAnnotation(
                    process_correct=not has_error,
                    has_error=has_error,
                    reasoning_correct=not has_error,
                    plan_code_aligned=not has_error or layer != "alignment",
                    first_faulty_layer=layer,
                    first_faulty_step="S1" if has_error else None,
                    error_type="C01_BOUNDARY_ERROR" if has_error else None,
                    rationale="Synthetic non-authoritative test reference.",
                ),
            )
        )
    return b"".join(
        json.dumps(
            record.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        + b"\n"
        for record in records
    )


def _patch_runtime_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_empty_frozen_cohort(monkeypatch)
    _patch_private_references(monkeypatch)


def _arguments() -> dict[str, Path]:
    return {
        "arrangement_path": ARRANGEMENT,
        "protocol_path": PROTOCOL,
        "phase3_guide_path": PHASE3_GUIDE,
        "source_path": SOURCE,
    }


def test_frozen_arrangement_protocol_and_source_identities() -> None:
    assert _sha256(ARRANGEMENT) == P1_ARRANGEMENT_SHA256
    assert _sha256(PROTOCOL) == P1_PROTOCOL_SHA256
    assert _sha256(SOURCE) == P1_PRACTICE_SOURCE_SHA256

    arrangement = ARRANGEMENT.read_text(encoding="utf-8")
    for required in (
        "伦理状态记为 `READY`",
        "不自动授权发包或收集答案",
        "5/5",
        "至少 4/5",
        "最多进行两轮校准",
        "立即停止标注",
        "不得主动检索",
        "不得与任何人讨论",
        "Get-FileHash -Algorithm SHA256",
        "certutil -hashfile <file> SHA256",
        "任一项缺失都表示不得发包",
        "公开 manifest 只记录无答案的逻辑 artifact ID、存储类别和 SHA256",
        "phase4_p1_formal_subset_v1",
    ):
        assert required in arrangement

    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    assert protocol["ethics_status"] == "READY"
    assert protocol["ethics_determination"] == {
        "decision": "approved",
        "confirmed_on": "2026-09-02",
        "verifier_role": "supervising_advisor",
        "record_storage": "private_restricted_location",
        "participant_consent_requirements_confirmed": True,
        "data_management_requirements_confirmed": True,
    }
    assert protocol["delivery_record_status"] == "pending_completion"
    assert protocol["data_collection_allowed"] is False
    assert protocol["formal_subset"]["target_item_count"] == 20
    assert protocol["formal_subset"]["subset_status"] == "frozen"
    assert (
        protocol["practice"]["public_manifest_excludes_reference_content_and_private_path"] is True
    )
    assert protocol["practice"]["maximum_calibration_rounds_total"] == 2
    assert protocol["formal_packet_created"] is False
    assert protocol["formal_data_collected"] is False


def test_preflight_is_deterministic_cohort_external_and_write_free(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_runtime_inputs(monkeypatch)
    before = set(tmp_path.iterdir())
    first = preflight_p1_practice_bundle(**_arguments())
    second = preflight_p1_practice_bundle(**_arguments())

    assert first == second
    assert first.manifest.item_count == 5
    assert first.manifest.executed_public_case_count == 15
    assert first.manifest.cohort_overlap_count == 0
    assert first.manifest.clean_reference_count == 2
    assert first.manifest.error_reference_count == 3
    assert first.manifest.provider_call_count == 0
    assert first.manifest.docker_call_count == 0
    assert first.manifest.network_call_count == 0
    assert first.manifest.formal_packet_created is False
    assert first.manifest.formal_data_collected is False
    assert set(tmp_path.iterdir()) == before


def test_checked_in_practice_bundle_matches_frozen_hashes() -> None:
    for relative_path, expected_sha256 in FROZEN_BUNDLE_HASHES.items():
        assert _sha256(FROZEN_BUNDLE / relative_path) == expected_sha256
    manifest = json.loads((FROZEN_BUNDLE / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["arrangement_sha256"] == P1_ARRANGEMENT_SHA256
    assert manifest["protocol_sha256"] == P1_PROTOCOL_SHA256
    assert manifest["source_sha256"] == P1_PRACTICE_SOURCE_SHA256
    assert manifest["cohort_overlap_count"] == 0
    assert manifest["formal_packet_created"] is False
    assert manifest["formal_data_collected"] is False
    assert manifest["ethics_status"] == "READY"
    assert manifest["delivery_record_status"] == "pending_completion"
    assert manifest["coordinator_reference_sha256"] == (FROZEN_COORDINATOR_REFERENCE_SHA256)
    assert manifest["coordinator_reference_storage"] == "git_ignored_private_artifact"
    assert "coordinator_reference_path" not in manifest
    assert not (FROZEN_BUNDLE / "coordinator").exists()


def test_public_practice_participant_packet_excludes_reference_and_private_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_runtime_inputs(monkeypatch)
    result = write_p1_practice_bundle(output_dir=tmp_path, **_arguments())

    packet_text = result.participant_packet_path.read_text(encoding="utf-8")
    template_text = result.participant_labels_template_path.read_text(encoding="utf-8")
    assert packet_text.count("\n") == 5
    assert template_text.count("\n") == 5
    for forbidden in (
        '"reference_annotation":',
        '"reference_role":',
        '"reference_code":',
        '"canonical_solution":',
        '"provider_raw":',
        '"method_predictions":',
        '"official_test_inputs":',
        '"identity_map":',
    ):
        assert forbidden not in packet_text
        assert forbidden not in template_text
    assert '"status":"pending"' in template_text
    assert '"process_correct":null' in template_text
    assert not (result.bundle_dir / "coordinator").exists()
    assert '"reference_annotation"' not in SOURCE.read_text(encoding="utf-8")


def test_write_is_no_overwrite_and_verify_is_byte_exact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_runtime_inputs(monkeypatch)
    result = write_p1_practice_bundle(output_dir=tmp_path, **_arguments())
    verified = verify_p1_practice_bundle(
        manifest_path=result.manifest_path,
        expected_manifest_sha256=result.manifest_sha256,
        **_arguments(),
    )
    assert verified.verified is True
    assert verified.item_count == 5
    assert verified.executed_public_case_count == 15

    with pytest.raises(Phase4P1AnnotationError, match="already exists"):
        write_p1_practice_bundle(output_dir=tmp_path, **_arguments())

    original = result.participant_labels_template_path.read_bytes()
    result.participant_labels_template_path.write_bytes(original + b" ")
    with pytest.raises(Phase4P1AnnotationError, match="differs"):
        verify_p1_practice_bundle(manifest_path=result.manifest_path, **_arguments())


def test_tampered_source_and_privacy_canary_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_runtime_inputs(monkeypatch)
    tampered_source = tmp_path / "source.json"
    tampered_source.write_bytes(SOURCE.read_bytes().replace(b'"MIT"', b'"MIT-0"', 1))
    with pytest.raises(Phase4P1AnnotationError, match="frozen identity"):
        preflight_p1_practice_bundle(
            **(_arguments() | {"source_path": tampered_source}),
        )

    with pytest.raises(ValueError):
        preflight_p1_practice_bundle(
            **_arguments(),
            privacy_canaries=("p1_rotate_left_once",),
        )


def test_cohort_problem_or_content_overlap_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_private_references(monkeypatch)
    source = p1_module.P1PracticeSource.model_validate(
        json.loads(SOURCE.read_text(encoding="utf-8"))
    )
    first = source.items[0]
    overlap_trace = SimpleNamespace(
        problem_id=first.problem.problem_id,
        code_sha256="0" * 64,
        structured_explanation_sha256="1" * 64,
    )
    cohort = SimpleNamespace(parents=(), counterfactuals=())
    natural = SimpleNamespace(traces=(overlap_trace,))
    monkeypatch.setattr(
        p1_module,
        "_load_phase3_manifests",
        lambda **_kwargs: (cohort, natural),
    )
    with pytest.raises(Phase4P1AnnotationError, match="overlaps"):
        preflight_p1_practice_bundle(**_arguments())


def test_private_reference_permissions_fail_closed(tmp_path: Path) -> None:
    source = p1_module.P1PracticeSource.model_validate(
        json.loads(SOURCE.read_text(encoding="utf-8"))
    )
    unsafe_reference = tmp_path / "coordinator_reference.jsonl"
    unsafe_reference.write_text("{}\n", encoding="utf-8")
    unsafe_reference.chmod(0o644)

    with pytest.raises(Phase4P1AnnotationError, match="permissions are too broad"):
        p1_module._load_private_references(unsafe_reference, source=source)


def test_private_reference_real_loader_validates_schema_order_and_layers(tmp_path: Path) -> None:
    source = p1_module.P1PracticeSource.model_validate(
        json.loads(SOURCE.read_text(encoding="utf-8"))
    )
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    reference_path = private_dir / "coordinator_reference.jsonl"
    reference_path.write_bytes(_synthetic_private_reference_payload())
    reference_path.chmod(0o600)

    payload, references = p1_module._load_private_references(reference_path, source=source)
    assert payload == _synthetic_private_reference_payload()
    assert tuple(item.practice_item_id for item in references) == tuple(
        f"practice_item_{index:03d}" for index in range(1, 6)
    )
    assert tuple(
        item.reference_annotation.first_faulty_layer
        for item in references
        if item.reference_annotation.has_error
    ) == ("alignment", "reasoning", "requirement")

    rows = payload.splitlines()
    reference_path.write_bytes(b"\n".join((rows[1], rows[0], *rows[2:])) + b"\n")
    with pytest.raises(Phase4P1AnnotationError, match="order differs"):
        p1_module._load_private_references(reference_path, source=source)


def test_private_reference_parent_permissions_fail_closed(tmp_path: Path) -> None:
    source = p1_module.P1PracticeSource.model_validate(
        json.loads(SOURCE.read_text(encoding="utf-8"))
    )
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o755)
    reference_path = private_dir / "coordinator_reference.jsonl"
    reference_path.write_bytes(_synthetic_private_reference_payload())
    reference_path.chmod(0o600)

    with pytest.raises(Phase4P1AnnotationError, match="parent permissions are too broad"):
        p1_module._load_private_references(reference_path, source=source)


def test_cli_preflight_reports_safety_without_reference_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runtime_inputs(monkeypatch)
    expected = preflight_p1_practice_bundle(**_arguments())
    monkeypatch.setattr(cli_module, "preflight_p1_practice_bundle", lambda **_kwargs: expected)

    result = CliRunner().invoke(app, ["phase4", "p1-practice-preflight"])
    assert result.exit_code == 0
    assert "5 / 15" in result.stdout
    assert "READY / 禁止（待单次交付记录）" in result.stdout
    assert "Provider / Docker / 网络" in result.stdout
    assert "reference_annotation" not in result.stdout
    assert "rationale" not in result.stdout
