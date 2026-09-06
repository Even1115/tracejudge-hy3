"""Validate and immutably freeze the returned P1 formal second-rater labels.

The checker binds a complete 20-row blind response to the exact packet template,
packet manifest, coordinator identity map, delivery record, and returned archive.
Only aggregate validation metadata is exposed by public result objects; the label
payloads and identity-joined records remain in a Git-ignored private directory.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, ValidationError, model_validator

from tracejudge_hy3.phase3.annotations import AnnotationIdentityRecord
from tracejudge_hy3.phase3.contracts import AnnotationRecord
from tracejudge_hy3.phase3.contracts import Identifier as Phase3Identifier
from tracejudge_hy3.schemas.evaluation import ErrorType, FaultyLayer

from .contracts import Identifier, Phase4Contract, Sha256
from .p1_annotations import (
    P1_PRACTICE_RATER_ID,
    P1_PROTOCOL_SHA256,
    Phase4P1AnnotationError,
    _decode_json,
    _fsync_directory,
    _json_bytes,
    _jsonl_bytes,
    _read_regular_file,
)
from .p1_formal_packet import (
    P1_FORMAL_PACKET_DEFAULT_OUTPUT,
    P1_FORMAL_PACKET_ID,
    P1FormalDraftRecord,
    P1FormalPacketManifest,
    _write_private_file,
)
from .p1_study import (
    P1_DELIVERY_RECORD_DEFAULT_PATH,
    P1SingleDeliveryRecord,
    _assert_private_location,
)

P1_FORMAL_PACKET_MANIFEST_SHA256 = (
    "8297183a615e53f62dff40bed33c3b2d83f3b3ed45ba06b3f8882759f6fcde2f"
)
P1_FORMAL_LABEL_SET_ID = "phase4_p1_formal_labels_rater02_round1_v1"
P1_FORMAL_LABELS_DEFAULT_OUTPUT = P1_FORMAL_PACKET_DEFAULT_OUTPUT
P1_FORMAL_PACKET_DEFAULT_DIR = f"{P1_FORMAL_PACKET_DEFAULT_OUTPUT}/{P1_FORMAL_PACKET_ID}"
P1_FORMAL_LABELS_DEFAULT_MANIFEST = (
    f"{P1_FORMAL_LABELS_DEFAULT_OUTPUT}/{P1_FORMAL_LABEL_SET_ID}/manifest.json"
)

_EXPECTED_ITEM_IDS = tuple(f"formal_item_{index:03d}" for index in range(1, 21))
_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024


class P1CompletedFormalRecord(Phase4Contract):
    annotation_item_id: Identifier
    annotation_protocol_sha256: Literal[P1_PROTOCOL_SHA256]
    rater_id: Literal[P1_PRACTICE_RATER_ID]
    annotation_round: Literal[1]
    blinded_to_primary_labels: Literal[True]
    blinded_to_method_predictions: Literal[True]
    blinded_to_other_raters: Literal[True]
    status: Literal["completed"]
    process_correct: bool
    has_error: bool
    reasoning_correct: bool
    plan_code_aligned: bool
    first_faulty_layer: FaultyLayer | None = None
    first_faulty_step: Identifier | None = None
    error_type: ErrorType | None = None
    rationale: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_label(self) -> Self:
        if self.process_correct == self.has_error:
            raise ValueError("process_correct must be the complement of has_error")
        fault_fields = (self.first_faulty_layer, self.first_faulty_step, self.error_type)
        if not self.has_error:
            if any(value is not None for value in fault_fields):
                raise ValueError("no-error formal label cannot retain fault fields")
            if not self.reasoning_correct or not self.plan_code_aligned:
                raise ValueError("no-error formal label requires reasoning and alignment")
        elif self.first_faulty_layer is None or self.error_type is None:
            raise ValueError("error formal label requires first layer and error type")
        return self


class P1FormalLabelsManifest(Phase4Contract):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase4_p1_formal_labels"] = "tracejudge_phase4_p1_formal_labels"
    status: Literal["frozen"] = "frozen"
    label_set_id: Literal[P1_FORMAL_LABEL_SET_ID] = P1_FORMAL_LABEL_SET_ID
    packet_id: Literal[P1_FORMAL_PACKET_ID] = P1_FORMAL_PACKET_ID
    received_at: datetime
    received_at_source: Literal["coordinator_reported"] = "coordinator_reported"
    formal_due_at: datetime
    received_within_formal_deadline: Literal[True] = True
    frozen_at: datetime
    source_archive_original_filename: str = Field(min_length=1, max_length=255)
    source_archive_observed_modified_at: datetime
    source_archive_size_bytes: int = Field(ge=1, le=_MAX_ARCHIVE_BYTES)
    source_archive_sha256: Sha256
    source_completed_labels_original_filename: str = Field(min_length=1, max_length=255)
    source_completed_labels_observed_modified_at: datetime
    source_completed_labels_size_bytes: int = Field(ge=1)
    source_completed_labels_sha256: Sha256
    archive_extraction_binding: Literal["coordinator_reported"] = "coordinator_reported"
    source_storage_permissions_verified_restricted: Literal[True] = True
    source_packet_manifest_sha256: Sha256
    source_packet_sha256: Sha256
    source_labels_template_sha256: Sha256
    source_identity_map_sha256: Sha256
    source_delivery_record_sha256: Sha256
    annotation_protocol_sha256: Literal[P1_PROTOCOL_SHA256] = P1_PROTOCOL_SHA256
    phase3_annotation_guide_sha256: Sha256
    rater_id: Literal[P1_PRACTICE_RATER_ID] = P1_PRACTICE_RATER_ID
    annotation_round: Literal[1] = 1
    ordered_annotation_item_ids: tuple[Identifier, ...]
    ordered_trace_ids: tuple[Phase3Identifier, ...]
    record_count: Literal[20] = 20
    natural_item_count: Literal[15] = 15
    counterfactual_item_count: Literal[5] = 5
    has_error_true_count: int = Field(ge=0, le=20)
    has_error_false_count: int = Field(ge=0, le=20)
    completed_labels_path: Literal["completed_labels.jsonl"] = "completed_labels.jsonl"
    annotation_records_path: Literal["annotations.jsonl"] = "annotations.jsonl"
    source_archive_path: Literal["source_archive.7z"] = "source_archive.7z"
    completed_labels_sha256: Sha256
    annotation_records_sha256: Sha256
    frozen_source_archive_sha256: Sha256
    agreement_kind: Literal["not_computed"] = "not_computed"
    contains_primary_rater_labels: Literal[False] = False
    contains_method_predictions: Literal[False] = False
    contains_provider_raw: Literal[False] = False
    provider_call_count: Literal[0] = 0
    docker_call_count: Literal[0] = 0
    network_call_count: Literal[0] = 0

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        timestamps = (
            self.received_at,
            self.formal_due_at,
            self.frozen_at,
            self.source_archive_observed_modified_at,
            self.source_completed_labels_observed_modified_at,
        )
        if any(value.tzinfo is None for value in timestamps):
            raise ValueError("formal-label timestamps must be timezone-aware")
        if self.received_at > self.formal_due_at:
            raise ValueError("formal labels were received after the frozen deadline")
        if self.ordered_annotation_item_ids != _EXPECTED_ITEM_IDS:
            raise ValueError("formal label item IDs differ from the packet order")
        if len(self.ordered_trace_ids) != 20 or len(set(self.ordered_trace_ids)) != 20:
            raise ValueError("formal labels require 20 unique trace identities")
        if self.has_error_true_count + self.has_error_false_count != self.record_count:
            raise ValueError("formal label class counts do not cover the records")
        if self.completed_labels_sha256 != self.source_completed_labels_sha256:
            raise ValueError("frozen completed labels differ from the returned source")
        if self.frozen_source_archive_sha256 != self.source_archive_sha256:
            raise ValueError("frozen archive differs from the returned source")
        return self


@dataclass(frozen=True, slots=True)
class P1FormalLabelsPreflight:
    label_set_id: str
    record_count: int
    completed_count: int
    has_error_true_count: int
    has_error_false_count: int
    received_at: datetime
    formal_due_at: datetime
    received_within_formal_deadline: bool
    source_archive_sha256: str
    source_completed_labels_sha256: str
    source_packet_manifest_sha256: str
    completed_labels_sha256: str
    annotation_records_sha256: str
    ready_to_freeze: bool


@dataclass(frozen=True, slots=True)
class P1FormalLabelsResult(P1FormalLabelsPreflight):
    run_dir: Path
    manifest_path: Path
    completed_labels_path: Path
    annotation_records_path: Path
    source_archive_path: Path
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class P1FormalLabelsVerification:
    label_set_id: str
    record_count: int
    manifest_sha256: str
    source_archive_sha256: str
    completed_labels_sha256: str
    verified: bool


@dataclass(frozen=True, slots=True)
class _LoadedFormalPacket:
    bundle_dir: Path
    manifest: P1FormalPacketManifest
    manifest_sha256: str
    template_records: tuple[P1FormalDraftRecord, ...]
    identity_records: tuple[AnnotationIdentityRecord, ...]


@dataclass(frozen=True, slots=True)
class _PreparedFormalLabels:
    preflight: P1FormalLabelsPreflight
    manifest: P1FormalLabelsManifest
    manifest_payload: bytes
    completed_labels_payload: bytes
    annotation_records_payload: bytes
    archive_payload: bytes
    output_root: Path
    run_dir: Path


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _parse_jsonl(
    payload: bytes,
    *,
    label: str,
    model: type[P1FormalDraftRecord]
    | type[P1CompletedFormalRecord]
    | type[AnnotationIdentityRecord]
    | type[AnnotationRecord],
) -> tuple[Any, ...]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise Phase4P1AnnotationError(
            f"{label} is not UTF-8 JSONL", safe_stage="P4D_P1_FORMAL_LABELS_INPUT"
        ) from None
    lines = text.splitlines()
    if len(lines) != 20 or any(not line.strip() for line in lines):
        raise Phase4P1AnnotationError(
            f"{label} must contain exactly 20 non-empty lines",
            safe_stage="P4D_P1_FORMAL_LABELS_INPUT",
        )
    records: list[Any] = []
    for line in lines:
        try:
            records.append(model.model_validate(_decode_json(line.encode("utf-8"), label=label)))
        except ValidationError:
            raise Phase4P1AnnotationError(
                f"{label} failed schema validation",
                safe_stage="P4D_P1_FORMAL_LABELS_INPUT",
            ) from None
    return tuple(records)


def _require_private_mode(path: Path, *, directory: bool, label: str) -> None:
    if path.is_symlink() or (not path.is_dir() if directory else not path.is_file()):
        raise Phase4P1AnnotationError(
            f"{label} must be a regular non-symlink {'directory' if directory else 'file'}",
            safe_stage="P4D_P1_FORMAL_LABELS_PRIVACY",
        )
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise Phase4P1AnnotationError(
            f"{label} permissions are too broad",
            safe_stage="P4D_P1_FORMAL_LABELS_PRIVACY",
        )


def _load_formal_packet(
    *, packet_dir: str | Path, expected_manifest_sha256: str
) -> _LoadedFormalPacket:
    bundle_dir = Path(packet_dir).expanduser().resolve()
    _require_private_mode(bundle_dir, directory=True, label="P1 formal packet directory")
    manifest_path = bundle_dir / "manifest.json"
    _assert_private_location(manifest_path, label="P1 formal packet manifest")
    manifest_payload = _read_regular_file(manifest_path, label="P1 formal packet manifest")
    manifest_sha256 = _sha256(manifest_payload)
    if manifest_sha256 != expected_manifest_sha256:
        raise Phase4P1AnnotationError(
            "formal packet manifest differs from the expected identity",
            safe_stage="P4D_P1_FORMAL_LABELS_PACKET",
        )
    try:
        manifest = P1FormalPacketManifest.model_validate(
            _decode_json(manifest_payload, label="P1 formal packet manifest")
        )
    except ValidationError:
        raise Phase4P1AnnotationError(
            "formal packet manifest failed schema validation",
            safe_stage="P4D_P1_FORMAL_LABELS_PACKET",
        ) from None

    packet_path = bundle_dir / manifest.participant_packet_path
    template_path = bundle_dir / manifest.participant_labels_template_path
    identity_path = bundle_dir / manifest.coordinator_identity_map_path
    for path, label in (
        (packet_path, "P1 participant packet"),
        (template_path, "P1 participant label template"),
        (identity_path, "P1 coordinator identity map"),
    ):
        _assert_private_location(path, label=label)
    packet_payload = _read_regular_file(packet_path, label="P1 participant packet")
    template_payload = _read_regular_file(template_path, label="P1 participant label template")
    identity_payload = _read_regular_file(identity_path, label="P1 coordinator identity map")
    if (
        _sha256(packet_payload) != manifest.participant_packet_sha256
        or _sha256(template_payload) != manifest.participant_labels_template_sha256
        or _sha256(identity_payload) != manifest.coordinator_identity_map_sha256
    ):
        raise Phase4P1AnnotationError(
            "formal packet artifact hashes are inconsistent",
            safe_stage="P4D_P1_FORMAL_LABELS_PACKET",
        )
    templates = _parse_jsonl(
        template_payload, label="P1 participant label template", model=P1FormalDraftRecord
    )
    identities = _parse_jsonl(
        identity_payload, label="P1 coordinator identity map", model=AnnotationIdentityRecord
    )
    if tuple(item.annotation_item_id for item in templates) != manifest.ordered_annotation_item_ids:
        raise Phase4P1AnnotationError(
            "formal template order differs from the packet manifest",
            safe_stage="P4D_P1_FORMAL_LABELS_PACKET",
        )
    if (
        tuple(item.annotation_item_id for item in identities)
        != manifest.ordered_annotation_item_ids
    ):
        raise Phase4P1AnnotationError(
            "formal identity order differs from the packet manifest",
            safe_stage="P4D_P1_FORMAL_LABELS_PACKET",
        )
    return _LoadedFormalPacket(
        bundle_dir=bundle_dir,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        template_records=templates,
        identity_records=identities,
    )


def _load_delivery_record(
    *, path: str | Path, expected_sha256: str
) -> tuple[bytes, P1SingleDeliveryRecord]:
    record_path = Path(path).expanduser().resolve()
    _assert_private_location(record_path, label="P1 delivery record")
    payload = _read_regular_file(record_path, label="P1 delivery record")
    if _sha256(payload) != expected_sha256:
        raise Phase4P1AnnotationError(
            "delivery record differs from the formal packet binding",
            safe_stage="P4D_P1_FORMAL_LABELS_DELIVERY",
        )
    try:
        record = P1SingleDeliveryRecord.model_validate(
            _decode_json(payload, label="P1 delivery record")
        )
    except ValidationError:
        raise Phase4P1AnnotationError(
            "delivery record failed schema validation",
            safe_stage="P4D_P1_FORMAL_LABELS_DELIVERY",
        ) from None
    if not record.data_collection_allowed or record.formal_due_at is None:
        raise Phase4P1AnnotationError(
            "delivery record does not authorize formal data collection",
            safe_stage="P4D_P1_FORMAL_LABELS_DELIVERY",
        )
    return payload, record


def _source_payload(path: Path, *, label: str, maximum_bytes: int) -> tuple[bytes, datetime]:
    _require_private_mode(path.parent, directory=True, label=f"{label} parent directory")
    _require_private_mode(path, directory=False, label=label)
    if path.stat().st_size > maximum_bytes:
        raise Phase4P1AnnotationError(
            f"{label} exceeds the size limit", safe_stage="P4D_P1_FORMAL_LABELS_INPUT"
        )
    payload = _read_regular_file(path, label=label)
    return payload, datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)


def prepare_p1_formal_labels(
    *,
    completed_labels_path: str | Path,
    returned_archive_path: str | Path,
    received_at: datetime,
    archive_extraction_binding_confirmed: bool,
    packet_dir: str | Path = P1_FORMAL_PACKET_DEFAULT_DIR,
    expected_packet_manifest_sha256: str = P1_FORMAL_PACKET_MANIFEST_SHA256,
    delivery_record_path: str | Path = P1_DELIVERY_RECORD_DEFAULT_PATH,
    output_dir: str | Path = P1_FORMAL_LABELS_DEFAULT_OUTPUT,
    frozen_at: datetime | None = None,
) -> _PreparedFormalLabels:
    if received_at.tzinfo is None:
        raise Phase4P1AnnotationError(
            "received_at must be timezone-aware", safe_stage="P4D_P1_FORMAL_LABELS_RECEIPT"
        )
    if not archive_extraction_binding_confirmed:
        raise Phase4P1AnnotationError(
            "coordinator must confirm that the labels were extracted from the returned archive",
            safe_stage="P4D_P1_FORMAL_LABELS_RECEIPT",
        )
    completed_path = Path(completed_labels_path).expanduser().resolve()
    archive_path = Path(returned_archive_path).expanduser().resolve()
    if archive_path.suffix.lower() != ".7z":
        raise Phase4P1AnnotationError(
            "returned archive must be a .7z file",
            safe_stage="P4D_P1_FORMAL_LABELS_RECEIPT",
        )
    if completed_path.parent != archive_path.parent:
        raise Phase4P1AnnotationError(
            "returned archive and extracted labels must share one restricted directory",
            safe_stage="P4D_P1_FORMAL_LABELS_RECEIPT",
        )
    archive_payload, archive_modified_at = _source_payload(
        archive_path, label="returned formal archive", maximum_bytes=_MAX_ARCHIVE_BYTES
    )
    completed_payload, completed_modified_at = _source_payload(
        completed_path,
        label="returned formal completed labels",
        maximum_bytes=32 * 1024 * 1024,
    )
    packet = _load_formal_packet(
        packet_dir=packet_dir, expected_manifest_sha256=expected_packet_manifest_sha256
    )
    delivery_payload, delivery = _load_delivery_record(
        path=delivery_record_path,
        expected_sha256=packet.manifest.delivery_record_sha256,
    )
    if received_at > delivery.formal_due_at:
        raise Phase4P1AnnotationError(
            "formal labels were received after the recorded deadline",
            safe_stage="P4D_P1_FORMAL_LABELS_RECEIPT",
        )

    completed = _parse_jsonl(
        completed_payload, label="returned formal completed labels", model=P1CompletedFormalRecord
    )
    completed_ids = tuple(item.annotation_item_id for item in completed)
    if completed_ids != packet.manifest.ordered_annotation_item_ids:
        raise Phase4P1AnnotationError(
            "returned formal label IDs or order differ from the packet",
            safe_stage="P4D_P1_FORMAL_LABELS_INPUT",
        )
    if len(set(completed_ids)) != 20:
        raise Phase4P1AnnotationError(
            "returned formal labels contain duplicate item IDs",
            safe_stage="P4D_P1_FORMAL_LABELS_INPUT",
        )
    fixed_fields = (
        "annotation_item_id",
        "annotation_protocol_sha256",
        "rater_id",
        "annotation_round",
        "blinded_to_primary_labels",
        "blinded_to_method_predictions",
        "blinded_to_other_raters",
    )
    for returned, template in zip(completed, packet.template_records, strict=True):
        if any(getattr(returned, name) != getattr(template, name) for name in fixed_fields):
            raise Phase4P1AnnotationError(
                "returned formal labels changed immutable template fields",
                safe_stage="P4D_P1_FORMAL_LABELS_INPUT",
            )

    identity_by_item = {item.annotation_item_id: item for item in packet.identity_records}
    annotations: list[AnnotationRecord] = []
    for item in completed:
        identity = identity_by_item[item.annotation_item_id]
        try:
            annotations.append(
                AnnotationRecord(
                    trace_id=identity.trace_id,
                    code_sha256=identity.code_sha256,
                    structured_explanation_sha256=identity.structured_explanation_sha256,
                    functional_evidence_sha256=identity.functional_evidence_sha256,
                    annotation_protocol_sha256=item.annotation_protocol_sha256,
                    rater_id=item.rater_id,
                    annotation_round=item.annotation_round,
                    blinded_to_method_predictions=item.blinded_to_method_predictions,
                    blinded_to_other_raters=item.blinded_to_other_raters,
                    process_correct=item.process_correct,
                    has_error=item.has_error,
                    reasoning_correct=item.reasoning_correct,
                    plan_code_aligned=item.plan_code_aligned,
                    first_faulty_layer=item.first_faulty_layer,
                    first_faulty_step=item.first_faulty_step,
                    error_type=item.error_type,
                    rationale=item.rationale,
                )
            )
        except ValidationError:
            raise Phase4P1AnnotationError(
                "returned labels failed identity-joined annotation validation",
                safe_stage="P4D_P1_FORMAL_LABELS_INPUT",
            ) from None
    annotations_payload = _jsonl_bytes(annotations)
    output_root = Path(output_dir).expanduser().resolve()
    run_dir = output_root / P1_FORMAL_LABEL_SET_ID
    archive_sha256 = _sha256(archive_payload)
    completed_sha256 = _sha256(completed_payload)
    frozen_time = frozen_at or datetime.now(UTC)
    try:
        manifest = P1FormalLabelsManifest(
            received_at=received_at,
            formal_due_at=delivery.formal_due_at,
            frozen_at=frozen_time,
            source_archive_original_filename=archive_path.name,
            source_archive_observed_modified_at=archive_modified_at,
            source_archive_size_bytes=len(archive_payload),
            source_archive_sha256=archive_sha256,
            source_completed_labels_original_filename=completed_path.name,
            source_completed_labels_observed_modified_at=completed_modified_at,
            source_completed_labels_size_bytes=len(completed_payload),
            source_completed_labels_sha256=completed_sha256,
            source_packet_manifest_sha256=packet.manifest_sha256,
            source_packet_sha256=packet.manifest.participant_packet_sha256,
            source_labels_template_sha256=(packet.manifest.participant_labels_template_sha256),
            source_identity_map_sha256=packet.manifest.coordinator_identity_map_sha256,
            source_delivery_record_sha256=_sha256(delivery_payload),
            phase3_annotation_guide_sha256=(packet.manifest.phase3_annotation_guide_sha256),
            ordered_annotation_item_ids=completed_ids,
            ordered_trace_ids=tuple(item.trace_id for item in packet.identity_records),
            has_error_true_count=sum(item.has_error for item in completed),
            has_error_false_count=sum(not item.has_error for item in completed),
            completed_labels_sha256=completed_sha256,
            annotation_records_sha256=_sha256(annotations_payload),
            frozen_source_archive_sha256=archive_sha256,
        )
    except ValidationError:
        raise Phase4P1AnnotationError(
            "formal labels manifest failed validation",
            safe_stage="P4D_P1_FORMAL_LABELS_MANIFEST",
        ) from None
    manifest_payload = _json_bytes(manifest)
    preflight = P1FormalLabelsPreflight(
        label_set_id=manifest.label_set_id,
        record_count=manifest.record_count,
        completed_count=len(completed),
        has_error_true_count=manifest.has_error_true_count,
        has_error_false_count=manifest.has_error_false_count,
        received_at=manifest.received_at,
        formal_due_at=manifest.formal_due_at,
        received_within_formal_deadline=True,
        source_archive_sha256=archive_sha256,
        source_completed_labels_sha256=completed_sha256,
        source_packet_manifest_sha256=packet.manifest_sha256,
        completed_labels_sha256=completed_sha256,
        annotation_records_sha256=manifest.annotation_records_sha256,
        ready_to_freeze=True,
    )
    return _PreparedFormalLabels(
        preflight=preflight,
        manifest=manifest,
        manifest_payload=manifest_payload,
        completed_labels_payload=completed_payload,
        annotation_records_payload=annotations_payload,
        archive_payload=archive_payload,
        output_root=output_root,
        run_dir=run_dir,
    )


def preflight_p1_formal_labels(**kwargs: Any) -> P1FormalLabelsPreflight:
    return prepare_p1_formal_labels(**kwargs).preflight


def freeze_p1_formal_labels(**kwargs: Any) -> P1FormalLabelsResult:
    prepared = prepare_p1_formal_labels(**kwargs)
    if prepared.run_dir.exists() or prepared.run_dir.is_symlink():
        raise Phase4P1AnnotationError(
            "formal label-set directory already exists",
            safe_stage="P4D_P1_FORMAL_LABELS_OUTPUT",
        )
    prepared.output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    prepared.output_root.chmod(0o700)
    temporary_dir: Path | None = None
    try:
        temporary_dir = Path(
            tempfile.mkdtemp(prefix=f".{P1_FORMAL_LABEL_SET_ID}.", dir=prepared.output_root)
        )
        temporary_dir.chmod(0o700)
        _write_private_file(temporary_dir / "manifest.json", prepared.manifest_payload)
        _write_private_file(
            temporary_dir / "completed_labels.jsonl", prepared.completed_labels_payload
        )
        _write_private_file(
            temporary_dir / "annotations.jsonl", prepared.annotation_records_payload
        )
        _write_private_file(temporary_dir / "source_archive.7z", prepared.archive_payload)
        os.replace(temporary_dir, prepared.run_dir)
        temporary_dir = None
        _fsync_directory(prepared.output_root)
    except OSError:
        raise Phase4P1AnnotationError(
            "cannot atomically freeze formal labels",
            safe_stage="P4D_P1_FORMAL_LABELS_OUTPUT",
        ) from None
    finally:
        if temporary_dir is not None:
            shutil.rmtree(temporary_dir, ignore_errors=True)
    return P1FormalLabelsResult(
        **asdict(prepared.preflight),
        run_dir=prepared.run_dir,
        manifest_path=prepared.run_dir / "manifest.json",
        completed_labels_path=prepared.run_dir / "completed_labels.jsonl",
        annotation_records_path=prepared.run_dir / "annotations.jsonl",
        source_archive_path=prepared.run_dir / "source_archive.7z",
        manifest_sha256=_sha256(prepared.manifest_payload),
    )


def verify_p1_formal_labels(
    *, manifest_path: str | Path, expected_manifest_sha256: str | None = None
) -> P1FormalLabelsVerification:
    path = Path(manifest_path).expanduser().resolve()
    _assert_private_location(path, label="P1 formal labels manifest")
    _require_private_mode(path.parent, directory=True, label="P1 formal label-set directory")
    payload = _read_regular_file(path, label="P1 formal labels manifest")
    actual_manifest_sha256 = _sha256(payload)
    if expected_manifest_sha256 and actual_manifest_sha256 != expected_manifest_sha256:
        raise Phase4P1AnnotationError(
            "formal labels manifest differs from the expected identity",
            safe_stage="P4D_P1_FORMAL_LABELS_VERIFY",
        )
    try:
        manifest = P1FormalLabelsManifest.model_validate(
            _decode_json(payload, label="P1 formal labels manifest")
        )
    except ValidationError:
        raise Phase4P1AnnotationError(
            "formal labels manifest failed schema validation",
            safe_stage="P4D_P1_FORMAL_LABELS_VERIFY",
        ) from None
    completed_path = path.parent / manifest.completed_labels_path
    annotations_path = path.parent / manifest.annotation_records_path
    archive_path = path.parent / manifest.source_archive_path
    for item, label in (
        (path, "P1 formal labels manifest"),
        (completed_path, "P1 frozen completed labels"),
        (annotations_path, "P1 frozen annotation records"),
        (archive_path, "P1 frozen source archive"),
    ):
        _require_private_mode(item, directory=False, label=label)
    completed_payload = _read_regular_file(completed_path, label="P1 frozen completed labels")
    annotations_payload = _read_regular_file(annotations_path, label="P1 frozen annotations")
    archive_payload = _read_regular_file(archive_path, label="P1 frozen source archive")
    if (
        _sha256(completed_payload) != manifest.completed_labels_sha256
        or _sha256(annotations_payload) != manifest.annotation_records_sha256
        or _sha256(archive_payload) != manifest.frozen_source_archive_sha256
    ):
        raise Phase4P1AnnotationError(
            "formal label-set artifact hashes are inconsistent",
            safe_stage="P4D_P1_FORMAL_LABELS_VERIFY",
        )
    completed = _parse_jsonl(
        completed_payload, label="P1 frozen completed labels", model=P1CompletedFormalRecord
    )
    annotations = _parse_jsonl(
        annotations_payload, label="P1 frozen annotations", model=AnnotationRecord
    )
    if tuple(item.annotation_item_id for item in completed) != manifest.ordered_annotation_item_ids:
        raise Phase4P1AnnotationError(
            "frozen completed-label order differs from the manifest",
            safe_stage="P4D_P1_FORMAL_LABELS_VERIFY",
        )
    if tuple(item.trace_id for item in annotations) != manifest.ordered_trace_ids:
        raise Phase4P1AnnotationError(
            "frozen annotation order differs from the manifest",
            safe_stage="P4D_P1_FORMAL_LABELS_VERIFY",
        )
    return P1FormalLabelsVerification(
        label_set_id=manifest.label_set_id,
        record_count=manifest.record_count,
        manifest_sha256=actual_manifest_sha256,
        source_archive_sha256=manifest.source_archive_sha256,
        completed_labels_sha256=manifest.completed_labels_sha256,
        verified=True,
    )


__all__ = [
    "P1_FORMAL_LABELS_DEFAULT_MANIFEST",
    "P1_FORMAL_LABELS_DEFAULT_OUTPUT",
    "P1_FORMAL_LABEL_SET_ID",
    "P1_FORMAL_PACKET_DEFAULT_DIR",
    "P1_FORMAL_PACKET_MANIFEST_SHA256",
    "P1CompletedFormalRecord",
    "P1FormalLabelsManifest",
    "P1FormalLabelsPreflight",
    "P1FormalLabelsResult",
    "P1FormalLabelsVerification",
    "freeze_p1_formal_labels",
    "preflight_p1_formal_labels",
    "prepare_p1_formal_labels",
    "verify_p1_formal_labels",
]
