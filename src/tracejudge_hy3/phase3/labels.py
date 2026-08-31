"""Gate-E2 blind-label progress checks and immutable annotation-set freezing.

The progress checker deliberately does not open the coordinator identity map.
Only a complete label file may cross the later freeze boundary, where opaque
annotation item IDs are joined to the exact frozen cohort in one private,
auditable artifact.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, ValidationError, model_validator

from tracejudge_hy3.schemas.evaluation import ErrorType, FaultyLayer

from .annotations import (
    ANNOTATION_GUIDE_SHA256,
    ANNOTATION_PROTOCOL_SHA256,
    AnnotationDraftRecord,
    AnnotationIdentityRecord,
    AnnotationPacketManifest,
    Phase3AnnotationError,
    _decode_json,
    _fsync_directory,
    _jsonl_bytes,
    _load_protocol,
    _read_regular_file,
    _write_new_file,
)
from .contracts import (
    AnnotationRecord,
    AnnotationSetManifest,
    Identifier,
    NonEmptyText,
    Sha256,
    StrictFrozenModel,
)
from .privacy import assert_public_payload_safe
from .runner import Phase3RunnerError, load_paired_cohort

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_LABEL_LINES = 10_000


class CompletedAnnotationDraftRecord(StrictFrozenModel):
    """One completed row that still carries only the opaque annotation ID."""

    annotation_item_id: Identifier
    annotation_protocol_sha256: Sha256
    rater_id: Identifier
    annotation_round: int = Field(ge=1)
    blinded_to_method_predictions: Literal[True] = True
    blinded_to_other_raters: bool
    status: Literal["completed"]
    process_correct: bool
    has_error: bool
    reasoning_correct: bool
    plan_code_aligned: bool
    first_faulty_layer: FaultyLayer | None = None
    first_faulty_step: Identifier | None = None
    error_type: ErrorType | None = None
    rationale: NonEmptyText

    @model_validator(mode="after")
    def validate_completed_label(self) -> Self:
        if self.process_correct == self.has_error:
            raise ValueError("process_correct must be the complement of has_error")
        fault_fields = (self.first_faulty_layer, self.first_faulty_step, self.error_type)
        if not self.has_error:
            if any(value is not None for value in fault_fields):
                raise ValueError("no-error label may not retain fault fields")
            if not self.reasoning_correct or not self.plan_code_aligned:
                raise ValueError("no-error label requires correct reasoning and alignment")
        elif self.first_faulty_layer is None or self.error_type is None:
            raise ValueError("error label requires layer, error type, and rationale")
        return self


@dataclass(frozen=True, slots=True)
class AnnotationLabelCheckResult:
    packet_id: str
    expected_item_count: int
    completed_count: int
    pending_count: int
    invalid_count: int
    missing_item_count: int
    extra_line_count: int
    order_mismatch_count: int
    invalid_line_numbers: tuple[int, ...]
    working_labels_sha256: str
    packet_manifest_sha256: str
    ready_to_freeze: bool


@dataclass(frozen=True, slots=True)
class AnnotationLabelsFreezePreflightResult:
    annotation_set_id: str
    packet_id: str
    record_count: int
    natural_trace_count: int
    counterfactual_trace_count: int
    rater_id: str
    annotation_round: int
    annotation_protocol_sha256: str
    annotation_guide_sha256: str
    frozen_cohort_manifest_sha256: str
    source_packet_manifest_sha256: str
    source_completed_labels_sha256: str
    completed_labels_sha256: str
    annotation_records_sha256: str


@dataclass(frozen=True, slots=True)
class AnnotationLabelsFreezeResult(AnnotationLabelsFreezePreflightResult):
    run_dir: Path
    manifest_path: Path
    completed_labels_path: Path
    annotation_records_path: Path
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class _LoadedPacketHeader:
    run_dir: Path
    manifest: AnnotationPacketManifest
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class _InspectedWorkingLabels:
    result: AnnotationLabelCheckResult
    source_payload: bytes
    completed_records: tuple[CompletedAnnotationDraftRecord, ...]


@dataclass(frozen=True, slots=True)
class _PreparedAnnotationSet:
    preflight: AnnotationLabelsFreezePreflightResult
    packet: _LoadedPacketHeader
    completed_labels_payload: bytes
    annotation_records_payload: bytes
    ordered_trace_ids: tuple[str, ...]
    output_root: Path
    run_dir: Path


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _parse_model_jsonl(
    payload: bytes,
    *,
    label: str,
    model: type[StrictFrozenModel],
) -> tuple[StrictFrozenModel, ...]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise Phase3AnnotationError(
            f"{label} is not UTF-8 JSONL",
            safe_stage="P3E_LABELS_INPUT",
        ) from None
    lines = text.splitlines()
    if not lines or len(lines) > _MAX_LABEL_LINES or any(not line.strip() for line in lines):
        raise Phase3AnnotationError(
            f"{label} has an invalid JSONL structure",
            safe_stage="P3E_LABELS_INPUT",
        )
    records: list[StrictFrozenModel] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            value = _decode_json(line.encode("utf-8"), label=label)
            records.append(model.model_validate(value))
        except (Phase3AnnotationError, ValidationError):
            raise Phase3AnnotationError(
                f"{label} line {line_number} failed schema validation",
                safe_stage="P3E_LABELS_INPUT",
            ) from None
    return tuple(records)


def _load_packet_header(
    *,
    packet_run_dir: str | Path,
    expected_packet_manifest_sha256: str,
    protocol_path: str | Path,
    guide_path: str | Path,
) -> _LoadedPacketHeader:
    if not _SHA256_PATTERN.fullmatch(expected_packet_manifest_sha256):
        raise Phase3AnnotationError(
            "expected packet manifest SHA256 is invalid",
            safe_stage="P3E_PACKET_IDENTITY",
        )
    protocol = _load_protocol(protocol_path=protocol_path, guide_path=guide_path)
    run_dir = Path(packet_run_dir).expanduser()
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise Phase3AnnotationError(
            "annotation packet directory is unavailable",
            safe_stage="P3E_PACKET_IDENTITY",
        )
    if stat.S_IMODE(run_dir.stat().st_mode) & 0o077:
        raise Phase3AnnotationError(
            "annotation packet directory permissions are too broad",
            safe_stage="P3E_PACKET_IDENTITY",
        )
    manifest_payload = _read_regular_file(run_dir / "manifest.json", label="packet manifest")
    manifest_sha256 = _sha256(manifest_payload)
    if manifest_sha256 != expected_packet_manifest_sha256:
        raise Phase3AnnotationError(
            "annotation packet manifest differs from the expected identity",
            safe_stage="P3E_PACKET_IDENTITY",
        )
    try:
        manifest = AnnotationPacketManifest.model_validate(
            _decode_json(manifest_payload, label="packet manifest")
        )
    except ValidationError:
        raise Phase3AnnotationError(
            "annotation packet manifest failed schema validation",
            safe_stage="P3E_PACKET_IDENTITY",
        ) from None
    if (
        manifest.annotation_protocol_sha256 != ANNOTATION_PROTOCOL_SHA256
        or manifest.annotation_guide_sha256 != ANNOTATION_GUIDE_SHA256
        or manifest.annotation_protocol_id != protocol.protocol_id
        or manifest.frozen_cohort_manifest_sha256 != protocol.frozen_cohort_manifest_sha256
    ):
        raise Phase3AnnotationError(
            "annotation packet differs from the frozen protocol",
            safe_stage="P3E_PACKET_IDENTITY",
        )

    packet_payload = _read_regular_file(run_dir / "packet.jsonl", label="blind packet")
    template_payload = _read_regular_file(
        run_dir / "labels_template.jsonl",
        label="label template",
    )
    if (
        _sha256(packet_payload) != manifest.packet_sha256
        or _sha256(template_payload) != manifest.labels_template_sha256
    ):
        raise Phase3AnnotationError(
            "annotation packet payload hashes are inconsistent",
            safe_stage="P3E_PACKET_IDENTITY",
        )
    template_records = _parse_model_jsonl(
        template_payload,
        label="label template",
        model=AnnotationDraftRecord,
    )
    if tuple(item.annotation_item_id for item in template_records) != (
        manifest.ordered_annotation_item_ids
    ):
        raise Phase3AnnotationError(
            "annotation template order differs from the packet manifest",
            safe_stage="P3E_PACKET_IDENTITY",
        )
    for item in template_records:
        if (
            item.annotation_protocol_sha256 != manifest.annotation_protocol_sha256
            or item.rater_id != manifest.rater_id
            or item.annotation_round != manifest.annotation_round
            or item.blinded_to_other_raters != manifest.blinded_to_other_raters
        ):
            raise Phase3AnnotationError(
                "annotation template metadata differs from the packet manifest",
                safe_stage="P3E_PACKET_IDENTITY",
            )
    return _LoadedPacketHeader(
        run_dir=run_dir.resolve(),
        manifest=manifest,
        manifest_sha256=manifest_sha256,
    )


def _inspect_working_labels(
    *,
    packet: _LoadedPacketHeader,
    completed_labels_path: str | Path,
) -> _InspectedWorkingLabels:
    labels_path = Path(completed_labels_path)
    if labels_path.exists() and stat.S_IMODE(labels_path.stat().st_mode) & 0o077:
        raise Phase3AnnotationError(
            "working label permissions are too broad",
            safe_stage="P3E_LABELS_PERMISSIONS",
        )
    payload = _read_regular_file(labels_path, label="working labels")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise Phase3AnnotationError(
            "working labels are not UTF-8 JSONL",
            safe_stage="P3E_LABELS_INPUT",
        ) from None
    lines = text.splitlines()
    if len(lines) > _MAX_LABEL_LINES:
        raise Phase3AnnotationError(
            "working labels exceed the line limit",
            safe_stage="P3E_LABELS_INPUT",
        )

    expected_ids = packet.manifest.ordered_annotation_item_ids
    completed: list[CompletedAnnotationDraftRecord] = []
    pending_count = 0
    invalid_lines: list[int] = []
    order_mismatch_count = 0
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            invalid_lines.append(line_number)
            continue
        try:
            value = _decode_json(line.encode("utf-8"), label="working labels")
            if value.get("status") == "pending":
                record: AnnotationDraftRecord | CompletedAnnotationDraftRecord = (
                    AnnotationDraftRecord.model_validate(value)
                )
                pending_count += 1
            elif value.get("status") == "completed":
                record = CompletedAnnotationDraftRecord.model_validate(value)
                completed.append(record)
            else:
                raise ValueError
        except (Phase3AnnotationError, ValidationError, ValueError):
            invalid_lines.append(line_number)
            continue

        if (
            record.annotation_protocol_sha256 != packet.manifest.annotation_protocol_sha256
            or record.rater_id != packet.manifest.rater_id
            or record.annotation_round != packet.manifest.annotation_round
            or record.blinded_to_other_raters != packet.manifest.blinded_to_other_raters
        ):
            invalid_lines.append(line_number)
            if isinstance(record, CompletedAnnotationDraftRecord):
                completed.pop()
            else:
                pending_count -= 1
            continue
        if (
            line_number > len(expected_ids)
            or record.annotation_item_id != expected_ids[line_number - 1]
        ):
            order_mismatch_count += 1

    missing_item_count = max(0, len(expected_ids) - len(lines))
    extra_line_count = max(0, len(lines) - len(expected_ids))
    invalid_count = len(invalid_lines)
    ready = (
        len(completed) == len(expected_ids)
        and pending_count == 0
        and invalid_count == 0
        and missing_item_count == 0
        and extra_line_count == 0
        and order_mismatch_count == 0
    )
    result = AnnotationLabelCheckResult(
        packet_id=packet.manifest.packet_id,
        expected_item_count=len(expected_ids),
        completed_count=len(completed),
        pending_count=pending_count,
        invalid_count=invalid_count,
        missing_item_count=missing_item_count,
        extra_line_count=extra_line_count,
        order_mismatch_count=order_mismatch_count,
        invalid_line_numbers=tuple(invalid_lines),
        working_labels_sha256=_sha256(payload),
        packet_manifest_sha256=packet.manifest_sha256,
        ready_to_freeze=ready,
    )
    return _InspectedWorkingLabels(
        result=result,
        source_payload=payload,
        completed_records=tuple(completed),
    )


def check_annotation_labels(
    *,
    packet_run_dir: str | Path,
    expected_packet_manifest_sha256: str,
    completed_labels_path: str | Path,
    protocol_path: str | Path,
    guide_path: str | Path,
) -> AnnotationLabelCheckResult:
    """Check blind-label progress without opening the coordinator identity map."""

    packet = _load_packet_header(
        packet_run_dir=packet_run_dir,
        expected_packet_manifest_sha256=expected_packet_manifest_sha256,
        protocol_path=protocol_path,
        guide_path=guide_path,
    )
    return _inspect_working_labels(
        packet=packet,
        completed_labels_path=completed_labels_path,
    ).result


def _load_identities(packet: _LoadedPacketHeader) -> tuple[AnnotationIdentityRecord, ...]:
    payload = _read_regular_file(
        packet.run_dir / "identity_map.jsonl",
        label="coordinator identity map",
    )
    if _sha256(payload) != packet.manifest.identity_map_sha256:
        raise Phase3AnnotationError(
            "coordinator identity map hash is inconsistent",
            safe_stage="P3E_IDENTITY_JOIN",
        )
    parsed = _parse_model_jsonl(
        payload,
        label="coordinator identity map",
        model=AnnotationIdentityRecord,
    )
    identities = tuple(item for item in parsed if isinstance(item, AnnotationIdentityRecord))
    if tuple(item.annotation_item_id for item in identities) != (
        packet.manifest.ordered_annotation_item_ids
    ):
        raise Phase3AnnotationError(
            "coordinator identity order differs from the packet manifest",
            safe_stage="P3E_IDENTITY_JOIN",
        )
    return identities


def _resolve_output(
    *,
    output_dir: str | Path,
    annotation_set_id: str,
) -> tuple[Path, Path]:
    if not _ID_PATTERN.fullmatch(annotation_set_id):
        raise Phase3AnnotationError(
            "annotation_set_id contains unsupported characters",
            safe_stage="P3E_LABELS_OUTPUT",
        )
    root = Path(output_dir).expanduser()
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise Phase3AnnotationError(
            "annotation-set output root is unsafe",
            safe_stage="P3E_LABELS_OUTPUT",
        )
    resolved = root.resolve()
    run_dir = resolved / annotation_set_id
    if run_dir.exists() or run_dir.is_symlink():
        raise Phase3AnnotationError(
            "annotation-set directory already exists",
            safe_stage="P3E_LABELS_OUTPUT",
        )
    return resolved, run_dir


def _prepare_annotation_set(
    *,
    annotation_set_id: str,
    packet_run_dir: str | Path,
    expected_packet_manifest_sha256: str,
    completed_labels_path: str | Path,
    cohort_manifest_path: str | Path,
    natural_manifest_path: str | Path,
    protocol_path: str | Path,
    guide_path: str | Path,
    output_dir: str | Path,
    privacy_canaries: Sequence[str | bytes] = (),
) -> _PreparedAnnotationSet:
    packet = _load_packet_header(
        packet_run_dir=packet_run_dir,
        expected_packet_manifest_sha256=expected_packet_manifest_sha256,
        protocol_path=protocol_path,
        guide_path=guide_path,
    )
    inspected = _inspect_working_labels(
        packet=packet,
        completed_labels_path=completed_labels_path,
    )
    if not inspected.result.ready_to_freeze:
        raise Phase3AnnotationError(
            "working labels are incomplete or invalid",
            safe_stage="P3E_LABELS_NOT_READY",
        )

    identities = _load_identities(packet)
    try:
        cohort = load_paired_cohort(
            overlay_manifest_path=cohort_manifest_path,
            natural_manifest_path=natural_manifest_path,
        )
    except Phase3RunnerError as exc:
        raise Phase3AnnotationError(
            "frozen cohort failed label binding",
            safe_stage=exc.safe_stage,
        ) from None
    if cohort.overlay_manifest_sha256 != packet.manifest.frozen_cohort_manifest_sha256:
        raise Phase3AnnotationError(
            "annotation packet references a different frozen cohort",
            safe_stage="P3E_IDENTITY_JOIN",
        )

    if len(identities) != len(cohort.ordered_trace_ids):
        raise Phase3AnnotationError(
            "coordinator identity map does not cover the frozen cohort",
            safe_stage="P3E_IDENTITY_JOIN",
        )
    identity_by_trace = {item.trace_id: item for item in identities}
    if set(identity_by_trace) != set(cohort.ordered_trace_ids):
        raise Phase3AnnotationError(
            "coordinator identity traces differ from the frozen cohort",
            safe_stage="P3E_IDENTITY_JOIN",
        )
    for trace_id, identity in identity_by_trace.items():
        trace = cohort.traces_by_id[trace_id]
        if (
            identity.problem_id != trace.problem_id
            or identity.code_sha256 != trace.code_sha256
            or identity.structured_explanation_sha256 != trace.structured_explanation_sha256
            or identity.functional_evidence_sha256
            != trace.functional_evidence.functional_evidence_sha256
        ):
            raise Phase3AnnotationError(
                "coordinator identity hashes differ from the frozen cohort",
                safe_stage="P3E_IDENTITY_JOIN",
            )

    completed_by_item = {item.annotation_item_id: item for item in inspected.completed_records}
    if len(completed_by_item) != len(inspected.completed_records):
        raise Phase3AnnotationError(
            "completed labels contain duplicate item IDs",
            safe_stage="P3E_LABELS_INPUT",
        )
    records: list[AnnotationRecord] = []
    for trace_id in cohort.ordered_trace_ids:
        identity = identity_by_trace[trace_id]
        completed = completed_by_item.get(identity.annotation_item_id)
        if completed is None:
            raise Phase3AnnotationError(
                "completed labels do not cover the frozen cohort",
                safe_stage="P3E_IDENTITY_JOIN",
            )
        try:
            record = AnnotationRecord(
                trace_id=trace_id,
                code_sha256=identity.code_sha256,
                structured_explanation_sha256=identity.structured_explanation_sha256,
                functional_evidence_sha256=identity.functional_evidence_sha256,
                annotation_protocol_sha256=completed.annotation_protocol_sha256,
                rater_id=completed.rater_id,
                annotation_round=completed.annotation_round,
                blinded_to_other_raters=completed.blinded_to_other_raters,
                process_correct=completed.process_correct,
                has_error=completed.has_error,
                reasoning_correct=completed.reasoning_correct,
                plan_code_aligned=completed.plan_code_aligned,
                first_faulty_layer=completed.first_faulty_layer,
                first_faulty_step=completed.first_faulty_step,
                error_type=completed.error_type,
                rationale=completed.rationale,
            )
        except ValidationError:
            raise Phase3AnnotationError(
                "completed labels failed frozen annotation validation",
                safe_stage="P3E_LABELS_INPUT",
            ) from None
        assert_public_payload_safe(record, canaries=privacy_canaries)
        records.append(record)

    completed_payload = _jsonl_bytes(inspected.completed_records)
    annotation_payload = _jsonl_bytes(records)
    output_root, run_dir = _resolve_output(
        output_dir=output_dir,
        annotation_set_id=annotation_set_id,
    )
    preflight = AnnotationLabelsFreezePreflightResult(
        annotation_set_id=annotation_set_id,
        packet_id=packet.manifest.packet_id,
        record_count=len(records),
        natural_trace_count=cohort.natural_trace_count,
        counterfactual_trace_count=cohort.counterfactual_trace_count,
        rater_id=packet.manifest.rater_id,
        annotation_round=packet.manifest.annotation_round,
        annotation_protocol_sha256=packet.manifest.annotation_protocol_sha256,
        annotation_guide_sha256=packet.manifest.annotation_guide_sha256,
        frozen_cohort_manifest_sha256=cohort.overlay_manifest_sha256,
        source_packet_manifest_sha256=packet.manifest_sha256,
        source_completed_labels_sha256=inspected.result.working_labels_sha256,
        completed_labels_sha256=_sha256(completed_payload),
        annotation_records_sha256=_sha256(annotation_payload),
    )
    return _PreparedAnnotationSet(
        preflight=preflight,
        packet=packet,
        completed_labels_payload=completed_payload,
        annotation_records_payload=annotation_payload,
        ordered_trace_ids=cohort.ordered_trace_ids,
        output_root=output_root,
        run_dir=run_dir,
    )


def preflight_annotation_labels_freeze(**kwargs: Any) -> AnnotationLabelsFreezePreflightResult:
    """Join completed blind labels to identities without writing artifacts."""

    return _prepare_annotation_set(**kwargs).preflight


def freeze_annotation_labels(**kwargs: Any) -> AnnotationLabelsFreezeResult:
    """Atomically freeze one complete primary blind-label round."""

    prepared = _prepare_annotation_set(**kwargs)
    prepared.output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    prepared.output_root.chmod(0o700)
    temporary_dir: Path | None = None
    try:
        temporary_dir = Path(
            tempfile.mkdtemp(
                prefix=f".{prepared.preflight.annotation_set_id}.",
                dir=prepared.output_root,
            )
        )
        temporary_dir.chmod(0o700)
        completed_path = temporary_dir / "completed_labels.jsonl"
        records_path = temporary_dir / "annotations.jsonl"
        _write_new_file(completed_path, prepared.completed_labels_payload)
        _write_new_file(records_path, prepared.annotation_records_payload)
        manifest = AnnotationSetManifest(
            annotation_set_id=prepared.preflight.annotation_set_id,
            annotation_protocol_sha256=prepared.preflight.annotation_protocol_sha256,
            annotation_guide_sha256=prepared.preflight.annotation_guide_sha256,
            frozen_cohort_manifest_sha256=(prepared.preflight.frozen_cohort_manifest_sha256),
            source_packet_id=prepared.packet.manifest.packet_id,
            source_packet_manifest_sha256=(prepared.preflight.source_packet_manifest_sha256),
            source_packet_sha256=prepared.packet.manifest.packet_sha256,
            source_identity_map_sha256=prepared.packet.manifest.identity_map_sha256,
            source_labels_template_sha256=(prepared.packet.manifest.labels_template_sha256),
            source_completed_labels_sha256=(prepared.preflight.source_completed_labels_sha256),
            completed_labels_sha256=prepared.preflight.completed_labels_sha256,
            ordered_trace_ids=prepared.ordered_trace_ids,
            record_count=prepared.preflight.record_count,
            natural_trace_count=prepared.preflight.natural_trace_count,
            counterfactual_trace_count=prepared.preflight.counterfactual_trace_count,
            annotation_records_sha256=(prepared.preflight.annotation_records_sha256),
            rater_ids=(prepared.preflight.rater_id,),
            annotation_rounds=(prepared.preflight.annotation_round,),
            agreement_kind="not_computed",
            created_at=datetime.now(UTC),
        )
        manifest_payload = (
            json.dumps(
                manifest.model_dump(mode="json"),
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        _write_new_file(temporary_dir / "manifest.json", manifest_payload)
        os.replace(temporary_dir, prepared.run_dir)
        temporary_dir = None
        _fsync_directory(prepared.output_root)
    except OSError:
        raise Phase3AnnotationError(
            "cannot atomically publish the annotation set",
            safe_stage="P3E_LABELS_OUTPUT",
        ) from None
    finally:
        if temporary_dir is not None:
            shutil.rmtree(temporary_dir, ignore_errors=True)

    return AnnotationLabelsFreezeResult(
        **asdict(prepared.preflight),
        run_dir=prepared.run_dir,
        manifest_path=prepared.run_dir / "manifest.json",
        completed_labels_path=prepared.run_dir / "completed_labels.jsonl",
        annotation_records_path=prepared.run_dir / "annotations.jsonl",
        manifest_sha256=_sha256((prepared.run_dir / "manifest.json").read_bytes()),
    )


__all__ = [
    "AnnotationLabelCheckResult",
    "AnnotationLabelsFreezePreflightResult",
    "AnnotationLabelsFreezeResult",
    "CompletedAnnotationDraftRecord",
    "check_annotation_labels",
    "freeze_annotation_labels",
    "preflight_annotation_labels_freeze",
]
