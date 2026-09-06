from __future__ import annotations

import hashlib
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tracejudge_hy3.phase3.annotations import AnnotationIdentityRecord
from tracejudge_hy3.phase4 import (
    P1_PROTOCOL_SHA256,
    Phase4P1AnnotationError,
    freeze_p1_formal_labels,
    preflight_p1_formal_labels,
    verify_p1_formal_labels,
)
from tracejudge_hy3.phase4.p1_annotations import _json_bytes, _jsonl_bytes
from tracejudge_hy3.phase4.p1_formal_labels import P1CompletedFormalRecord
from tracejudge_hy3.phase4.p1_formal_packet import (
    P1FormalDraftRecord,
    P1FormalPacketManifest,
)
from tracejudge_hy3.phase4.p1_study import (
    P1_DELIVERY_SCHEMA_SHA256,
    P1DeliveryChannels,
    P1SingleDeliveryRecord,
)


def _digest(value: bytes | str) -> str:
    payload = value if isinstance(value, bytes) else value.encode()
    return hashlib.sha256(payload).hexdigest()


def _write_private(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    path.write_bytes(payload)
    path.chmod(0o600)


def _formal_fixture(tmp_path: Path) -> dict[str, object]:
    received = datetime(2026, 9, 4, 7, 20, tzinfo=UTC)
    delivery = P1SingleDeliveryRecord(
        record_status="ready_for_practice_delivery",
        schema_sha256=P1_DELIVERY_SCHEMA_SHA256,
        participant_consent_confirmed=True,
        participant_consent_confirmed_at=datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
        participant_receipt_verified_at=datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
        channels=P1DeliveryChannels(
            file_delivery_channel="encrypted-file-channel",
            password_channel="separate-voice-channel",
            return_channel="encrypted-return-channel",
            faq_channel="private-faq-channel",
            emergency_channel="private-emergency-channel",
        ),
        practice_due_at=datetime(2026, 9, 2, 23, 59, tzinfo=UTC),
        formal_due_at=datetime(2026, 9, 4, 23, 59, tzinfo=UTC),
        compensation_terms="No compensation.",
        credit_and_authorship_terms="Anonymous acknowledgement only.",
        withdrawal_cutoff_terms="Voluntary withdrawal remains available before label freeze.",
        retention_and_destruction_terms="Restricted local storage and controlled deletion.",
        coordinator_contact="Private coordinator channel.",
        project_owner_delivery_authorization_confirmed=True,
        data_collection_allowed=True,
    )
    delivery_payload = _json_bytes(delivery)
    delivery_path = tmp_path / "delivery" / "delivery_record.json"
    _write_private(delivery_path, delivery_payload)

    item_ids = tuple(f"formal_item_{index:03d}" for index in range(1, 21))
    packet_payload = b"".join(b"{}\n" for _ in item_ids)
    drafts = tuple(P1FormalDraftRecord(annotation_item_id=item_id) for item_id in item_ids)
    template_payload = _jsonl_bytes(drafts)
    identities = tuple(
        AnnotationIdentityRecord(
            annotation_item_id=item_id,
            trace_id=f"natural:fixture/{index:03d}",
            problem_id=f"fixture/{index:03d}",
            code_sha256=_digest(f"code:{index}"),
            structured_explanation_sha256=_digest(f"explanation:{index}"),
            functional_evidence_sha256=_digest(f"evidence:{index}"),
        )
        for index, item_id in enumerate(item_ids, start=1)
    )
    identity_payload = _jsonl_bytes(identities)
    packet_manifest = P1FormalPacketManifest(
        phase3_annotation_guide_sha256=_digest("guide"),
        delivery_record_sha256=_digest(delivery_payload),
        practice_admission_sha256=_digest("admission"),
        formal_subset_private_manifest_sha256=_digest("private subset"),
        formal_subset_public_commitment_sha256=_digest("public commitment"),
        selected_materials_sha256=_digest("materials"),
        ordered_annotation_item_ids=item_ids,
        participant_packet_sha256=_digest(packet_payload),
        participant_labels_template_sha256=_digest(template_payload),
        coordinator_identity_map_sha256=_digest(identity_payload),
    )
    packet_manifest_payload = _json_bytes(packet_manifest)
    packet_dir = tmp_path / "packet"
    packet_dir.mkdir(mode=0o700)
    _write_private(packet_dir / "manifest.json", packet_manifest_payload)
    _write_private(packet_dir / "participant" / "packet.jsonl", packet_payload)
    _write_private(packet_dir / "participant" / "labels_template.jsonl", template_payload)
    _write_private(packet_dir / "coordinator" / "identity_map.jsonl", identity_payload)

    completed = tuple(
        P1CompletedFormalRecord(
            annotation_item_id=item_id,
            annotation_protocol_sha256=P1_PROTOCOL_SHA256,
            rater_id="p1_rater_02",
            annotation_round=1,
            blinded_to_primary_labels=True,
            blinded_to_method_predictions=True,
            blinded_to_other_raters=True,
            status="completed",
            process_correct=index > 5,
            has_error=index <= 5,
            reasoning_correct=index > 5,
            plan_code_aligned=True,
            first_faulty_layer="implementation" if index <= 5 else None,
            first_faulty_step="S1" if index <= 5 else None,
            error_type="C01_BOUNDARY_ERROR" if index <= 5 else None,
            rationale="Synthetic blind-label rationale for validation only.",
        )
        for index, item_id in enumerate(item_ids, start=1)
    )
    source_dir = tmp_path / "return"
    source_dir.mkdir(mode=0o700)
    archive_path = source_dir / "formal_return.7z"
    labels_path = source_dir / "labels_completed.jsonl"
    _write_private(archive_path, b"synthetic encrypted archive payload")
    _write_private(labels_path, _jsonl_bytes(completed))
    return {
        "completed_labels_path": labels_path,
        "returned_archive_path": archive_path,
        "received_at": received,
        "archive_extraction_binding_confirmed": True,
        "packet_dir": packet_dir,
        "expected_packet_manifest_sha256": _digest(packet_manifest_payload),
        "delivery_record_path": delivery_path,
        "output_dir": tmp_path / "output",
        "frozen_at": datetime(2026, 9, 4, 8, 0, tzinfo=UTC),
    }


def test_formal_labels_preflight_freeze_and_verify_are_fail_closed(tmp_path: Path) -> None:
    arguments = _formal_fixture(tmp_path)
    preflight = preflight_p1_formal_labels(**arguments)
    assert preflight.completed_count == 20
    assert preflight.has_error_true_count == 5
    assert preflight.has_error_false_count == 15
    assert preflight.received_within_formal_deadline is True
    assert preflight.ready_to_freeze is True

    frozen = freeze_p1_formal_labels(**arguments)
    assert frozen.record_count == 20
    assert stat.S_IMODE(frozen.run_dir.stat().st_mode) == 0o700
    for path in (
        frozen.manifest_path,
        frozen.completed_labels_path,
        frozen.annotation_records_path,
        frozen.source_archive_path,
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (
        frozen.completed_labels_path.read_bytes()
        == Path(arguments["completed_labels_path"]).read_bytes()
    )
    verification = verify_p1_formal_labels(
        manifest_path=frozen.manifest_path,
        expected_manifest_sha256=frozen.manifest_sha256,
    )
    assert verification.verified is True
    assert verification.record_count == 20

    with pytest.raises(Phase4P1AnnotationError, match="already exists"):
        freeze_p1_formal_labels(**arguments)

    frozen.annotation_records_path.write_bytes(frozen.annotation_records_path.read_bytes() + b" ")
    with pytest.raises(Phase4P1AnnotationError, match="hashes are inconsistent"):
        verify_p1_formal_labels(manifest_path=frozen.manifest_path)


def test_formal_labels_reject_unconfirmed_late_or_changed_return(tmp_path: Path) -> None:
    arguments = _formal_fixture(tmp_path)
    unconfirmed = arguments | {"archive_extraction_binding_confirmed": False}
    with pytest.raises(Phase4P1AnnotationError, match="must confirm"):
        preflight_p1_formal_labels(**unconfirmed)

    late = arguments | {"received_at": datetime(2026, 9, 5, 0, 0, tzinfo=UTC)}
    with pytest.raises(Phase4P1AnnotationError, match="after the recorded deadline"):
        preflight_p1_formal_labels(**late)

    labels_path = Path(arguments["completed_labels_path"])
    payload = labels_path.read_text(encoding="utf-8")
    labels_path.write_text(payload.replace(P1_PROTOCOL_SHA256, _digest("changed"), 1))
    labels_path.chmod(0o600)
    with pytest.raises(Phase4P1AnnotationError, match="schema validation"):
        preflight_p1_formal_labels(**arguments)
