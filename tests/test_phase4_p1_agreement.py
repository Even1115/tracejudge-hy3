from __future__ import annotations

import hashlib
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tracejudge_hy3.phase3.contracts import AnnotationRecord, AnnotationSetManifest
from tracejudge_hy3.phase4 import (
    P1_PROTOCOL_SHA256,
    Phase4P1AnnotationError,
    preflight_p1_agreement,
    publish_p1_agreement,
    verify_p1_agreement,
)
from tracejudge_hy3.phase4.p1_annotations import _json_bytes, _jsonl_bytes
from tracejudge_hy3.phase4.p1_formal_labels import P1FormalLabelsManifest

NOW = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)


def _digest(value: bytes | str) -> str:
    payload = value if isinstance(value, bytes) else value.encode()
    return hashlib.sha256(payload).hexdigest()


def _write_private(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    path.write_bytes(payload)
    path.chmod(0o600)


def _record(
    *,
    trace_id: str,
    rater_id: str,
    protocol_sha256: str,
    has_error: bool,
    variant: int,
) -> AnnotationRecord:
    return AnnotationRecord(
        trace_id=trace_id,
        code_sha256=_digest(f"code:{trace_id}"),
        structured_explanation_sha256=_digest(f"explanation:{trace_id}"),
        functional_evidence_sha256=_digest(f"evidence:{trace_id}"),
        annotation_protocol_sha256=protocol_sha256,
        rater_id=rater_id,
        annotation_round=1,
        blinded_to_method_predictions=True,
        blinded_to_other_raters=rater_id != "primary_rater",
        process_correct=not has_error,
        has_error=has_error,
        reasoning_correct=not has_error or variant % 2 == 0,
        plan_code_aligned=not has_error or variant % 3 == 0,
        first_faulty_layer=(
            ("requirement", "reasoning", "implementation")[variant % 3] if has_error else None
        ),
        first_faulty_step=f"S{variant % 4 + 1}" if has_error else None,
        error_type=(
            ("R01_REQUIREMENT_MISREAD", "P01_ALGORITHM_ERROR", "C01_BOUNDARY_ERROR")[variant % 3]
            if has_error
            else None
        ),
        rationale="SYNTHETIC_PRIVATE_RATIONALE_MARKER",
    )


def _agreement_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    natural_ids = tuple(f"natural:fixture/{index:03d}" for index in range(1, 43))
    counterfactual_ids = tuple(f"counterfactual:fixture/{index:03d}" for index in range(1, 16))
    primary_ids = (*natural_ids, *counterfactual_ids)
    selected_ids = (*natural_ids[:15], *counterfactual_ids[:5])
    primary_protocol_sha256 = _digest("primary protocol")

    primary_records = tuple(
        _record(
            trace_id=trace_id,
            rater_id="primary_rater",
            protocol_sha256=primary_protocol_sha256,
            has_error=index < 5,
            variant=index,
        )
        for index, trace_id in enumerate(primary_ids)
    )
    secondary_error_indices = {0, 1, 2, 3, 5}
    secondary_records = tuple(
        _record(
            trace_id=trace_id,
            rater_id="p1_rater_02",
            protocol_sha256=P1_PROTOCOL_SHA256,
            has_error=index in secondary_error_indices,
            variant=index + (1 if index in {2, 3} else 0),
        )
        for index, trace_id in enumerate(selected_ids)
    )

    primary_payload = _jsonl_bytes(primary_records)
    primary_manifest = AnnotationSetManifest(
        annotation_set_id="phase3_labels_primary_round1_v1",
        annotation_protocol_sha256=primary_protocol_sha256,
        annotation_guide_sha256=_digest("guide"),
        frozen_cohort_manifest_sha256=_digest("cohort"),
        source_packet_id="primary_packet",
        source_packet_manifest_sha256=_digest("packet manifest"),
        source_packet_sha256=_digest("packet"),
        source_identity_map_sha256=_digest("identity map"),
        source_labels_template_sha256=_digest("template"),
        source_completed_labels_sha256=_digest("completed source"),
        completed_labels_sha256=_digest("completed frozen"),
        ordered_trace_ids=primary_ids,
        record_count=57,
        natural_trace_count=42,
        counterfactual_trace_count=15,
        annotation_records_sha256=_digest(primary_payload),
        rater_ids=("primary_rater",),
        annotation_rounds=(1,),
        agreement_kind="not_computed",
        created_at=NOW,
    )
    primary_dir = tmp_path / "primary"
    _write_private(primary_dir / "annotations.jsonl", primary_payload)
    primary_manifest_path = primary_dir / "manifest.json"
    _write_private(primary_manifest_path, _json_bytes(primary_manifest))

    secondary_payload = _jsonl_bytes(secondary_records)
    returned_labels_hash = _digest("returned labels")
    archive_hash = _digest("archive")
    secondary_manifest = P1FormalLabelsManifest(
        received_at=NOW,
        formal_due_at=NOW,
        frozen_at=NOW,
        source_archive_original_filename="return.7z",
        source_archive_observed_modified_at=NOW,
        source_archive_size_bytes=7,
        source_archive_sha256=archive_hash,
        source_completed_labels_original_filename="labels.jsonl",
        source_completed_labels_observed_modified_at=NOW,
        source_completed_labels_size_bytes=15,
        source_completed_labels_sha256=returned_labels_hash,
        source_packet_manifest_sha256=_digest("formal packet manifest"),
        source_packet_sha256=_digest("formal packet"),
        source_labels_template_sha256=_digest("formal template"),
        source_identity_map_sha256=_digest("formal identity map"),
        source_delivery_record_sha256=_digest("delivery"),
        phase3_annotation_guide_sha256=_digest("guide"),
        ordered_annotation_item_ids=tuple(f"formal_item_{index:03d}" for index in range(1, 21)),
        ordered_trace_ids=selected_ids,
        has_error_true_count=5,
        has_error_false_count=15,
        completed_labels_sha256=returned_labels_hash,
        annotation_records_sha256=_digest(secondary_payload),
        frozen_source_archive_sha256=archive_hash,
    )
    secondary_dir = tmp_path / "secondary"
    _write_private(secondary_dir / "annotations.jsonl", secondary_payload)
    secondary_manifest_path = secondary_dir / "manifest.json"
    _write_private(secondary_manifest_path, _json_bytes(secondary_manifest))
    return primary_manifest_path, secondary_manifest_path, tmp_path / "analysis"


def test_p1_agreement_preflight_publish_and_verify_are_aggregate_only(
    tmp_path: Path,
) -> None:
    primary, secondary, output = _agreement_fixture(tmp_path)
    arguments = {
        "primary_manifest_path": primary,
        "secondary_manifest_path": secondary,
        "output_dir": output,
        "expected_primary_manifest_sha256": None,
        "expected_secondary_manifest_sha256": None,
    }
    preflight = preflight_p1_agreement(**arguments)
    has_error = preflight.analysis.binary_fields[0]
    assert has_error.field_name == "has_error"
    assert has_error.confusion.model_dump() == {
        "both_true": 4,
        "primary_true_secondary_false": 1,
        "primary_false_secondary_true": 1,
        "both_false": 14,
        "total": 20,
    }
    assert has_error.raw_agreement.agreeing_count == 18
    assert has_error.raw_agreement.estimate == 0.9
    assert has_error.cohen_kappa == pytest.approx(0.733333333333)
    assert has_error.kappa_bootstrap_valid_iterations
    assert (
        preflight.analysis.primary_annotation_protocol_sha256
        != preflight.analysis.secondary_annotation_protocol_sha256
    )
    assert preflight.analysis.shared_annotation_guide_sha256 == _digest("guide")
    assert preflight.analysis.localization_fields[0].union_error_items.denominator == 6
    assert preflight.analysis.localization_fields[0].both_error_items.denominator == 4
    counterfactual = preflight.analysis.cohort_has_error[2].has_error
    assert counterfactual.raw_agreement.estimate == 1.0
    assert counterfactual.kappa_status == "not_applicable"
    assert preflight.ready_to_publish is True

    result = publish_p1_agreement(**arguments)
    assert stat.S_IMODE(result.run_dir.stat().st_mode) == 0o700
    for path in (result.manifest_path, result.analysis_path, result.report_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        payload = path.read_bytes()
        assert b"natural:fixture" not in payload
        assert b"SYNTHETIC_PRIVATE_RATIONALE_MARKER" not in payload
    verification = verify_p1_agreement(
        manifest_path=result.manifest_path,
        expected_manifest_sha256=result.manifest_sha256,
        primary_manifest_path=primary,
        secondary_manifest_path=secondary,
        expected_primary_manifest_sha256=None,
        expected_secondary_manifest_sha256=None,
    )
    assert verification.verified is True

    with pytest.raises(Phase4P1AnnotationError, match="already exists"):
        publish_p1_agreement(**arguments)


def test_p1_agreement_rejects_tampered_aggregate(tmp_path: Path) -> None:
    primary, secondary, output = _agreement_fixture(tmp_path)
    result = publish_p1_agreement(
        primary_manifest_path=primary,
        secondary_manifest_path=secondary,
        output_dir=output,
        expected_primary_manifest_sha256=None,
        expected_secondary_manifest_sha256=None,
    )
    result.analysis_path.write_bytes(result.analysis_path.read_bytes() + b" ")
    result.analysis_path.chmod(0o600)
    with pytest.raises(Phase4P1AnnotationError, match="hashes are inconsistent"):
        verify_p1_agreement(
            manifest_path=result.manifest_path,
            primary_manifest_path=primary,
            secondary_manifest_path=secondary,
            expected_primary_manifest_sha256=None,
            expected_secondary_manifest_sha256=None,
        )
