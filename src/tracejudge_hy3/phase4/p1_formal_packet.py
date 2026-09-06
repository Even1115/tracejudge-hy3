"""P1 practice admission and gated formal second-rater packet generation.

The practice admission record binds only calibration aggregates and exact input
hashes.  The formal packet generator fails closed unless the private delivery
record is complete, the frozen 20-item subset verifies byte-for-byte, and an
admitted practice record is supplied.  Participant files never contain the
coordinator identity map, primary labels, method predictions, or provider raw.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, ValidationError, model_validator

from tracejudge_hy3.phase3.annotations import (
    AnnotationIdentityRecord,
    BlindedAnnotationTask,
    _structured_solution_trace,
)
from tracejudge_hy3.phase3.materials import LoadedPhase3Materials, load_phase3_materials
from tracejudge_hy3.phase3.privacy import assert_public_payload_safe, canonical_sha256
from tracejudge_hy3.phase3.runner import Phase3RunnerError, functional_evidence_payload
from tracejudge_hy3.schemas.evaluation import ErrorType, FaultyLayer

from .contracts import Identifier, Phase4Contract, Sha256
from .p1_annotations import (
    P1_COORDINATOR_REFERENCE_DEFAULT_PATH,
    P1_PHASE3_GUIDE_SHA256,
    P1_PRACTICE_ID,
    P1_PRACTICE_RATER_ID,
    P1_PRACTICE_SOURCE_RELATIVE_PATH,
    P1_PROTOCOL_RELATIVE_PATH,
    P1_PROTOCOL_SHA256,
    Phase4P1AnnotationError,
    _decode_json,
    _fsync_directory,
    _json_bytes,
    _jsonl_bytes,
    _load_private_references,
    _load_protocol_and_source,
    _read_regular_file,
)
from .p1_study import (
    P1_DELIVERY_RECORD_DEFAULT_PATH,
    P1_DELIVERY_SCHEMA_RELATIVE_PATH,
    P1_FORMAL_PRIVATE_MANIFEST_DEFAULT_PATH,
    P1_FORMAL_PUBLIC_COMMITMENT_DEFAULT_PATH,
    P1_FORMAL_SUBSET_ID,
    P1FormalSubsetManifest,
    _assert_private_location,
    preflight_p1_delivery_record,
    verify_p1_formal_subset,
)

P1_PRACTICE_ADMISSION_ID = "phase4_p1_practice_admission_rater02_v1"
P1_PRACTICE_ADMISSION_DEFAULT_PATH = (
    f"artifacts/experiments/phase4-p1-annotations/{P1_PRACTICE_ADMISSION_ID}/manifest.json"
)
P1_FORMAL_PACKET_ID = "phase4_p1_formal_packet_rater02_round1_v1"
P1_FORMAL_PACKET_DEFAULT_OUTPUT = "artifacts/experiments/phase4-p1-annotations"
P1_FORMAL_PACKET_ORDER_ALGORITHM = "sha256(protocol_sha256\\0subset_id\\0trace_id)-ascending-v1"

_FORMAL_ITEM_IDS = tuple(f"formal_item_{index:03d}" for index in range(1, 21))


class P1CompletedPracticeRecord(Phase4Contract):
    practice_item_id: Identifier
    annotation_protocol_sha256: Literal[P1_PROTOCOL_SHA256]
    rater_id: Literal[P1_PRACTICE_RATER_ID]
    calibration_round: Literal[1]
    blinded_to_primary_labels: Literal[True]
    blinded_to_method_predictions: Literal[True]
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
                raise ValueError("no-error practice label cannot retain fault fields")
            if not self.reasoning_correct or not self.plan_code_aligned:
                raise ValueError("no-error practice label requires reasoning and alignment")
        elif self.first_faulty_layer is None or self.error_type is None:
            raise ValueError("error practice label requires first layer and error type")
        return self


class P1PracticeAdmissionRecord(Phase4Contract):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase4_p1_practice_admission"] = (
        "tracejudge_phase4_p1_practice_admission"
    )
    admission_id: Literal[P1_PRACTICE_ADMISSION_ID] = P1_PRACTICE_ADMISSION_ID
    practice_id: Literal[P1_PRACTICE_ID] = P1_PRACTICE_ID
    formal_subset_id: Literal[P1_FORMAL_SUBSET_ID] = P1_FORMAL_SUBSET_ID
    admitted_at: datetime
    rater_id: Literal[P1_PRACTICE_RATER_ID] = P1_PRACTICE_RATER_ID
    calibration_round: Literal[1] = 1
    protocol_sha256: Literal[P1_PROTOCOL_SHA256] = P1_PROTOCOL_SHA256
    completed_labels_sha256: Sha256
    returned_archive_sha256: Sha256
    coordinator_reference_sha256: Sha256
    schema_valid_count: Literal[5] = 5
    privacy_or_blinding_violation_count: Literal[0] = 0
    has_error_exact_agreement_count: int = Field(ge=0, le=5)
    has_error_exact_agreement_required: Literal[4] = 4
    process_correct_exact_agreement_count: int = Field(ge=0, le=5)
    process_correct_exact_agreement_required: Literal[4] = 4
    error_item_first_faulty_layer_exact_agreement_count: int = Field(ge=0, le=3)
    error_item_first_faulty_layer_exact_agreement_required: Literal[2] = 2
    error_item_first_faulty_layer_denominator: Literal[3] = 3
    public_evidence_only_rationales_confirmed: Literal[True] = True
    coordinator_written_authorization_confirmed: Literal[True] = True
    decision: Literal["admitted_to_formal_20"] = "admitted_to_formal_20"
    practice_scores_are_calibration_only: Literal[True] = True
    excluded_from_research_endpoints: Literal[True] = True

    @model_validator(mode="after")
    def validate_admission(self) -> Self:
        if self.admitted_at.tzinfo is None:
            raise ValueError("practice admission timestamp must be timezone-aware")
        if self.has_error_exact_agreement_count < self.has_error_exact_agreement_required:
            raise ValueError("has_error practice threshold is not met")
        if self.process_correct_exact_agreement_count < (
            self.process_correct_exact_agreement_required
        ):
            raise ValueError("process_correct practice threshold is not met")
        if self.error_item_first_faulty_layer_exact_agreement_count < (
            self.error_item_first_faulty_layer_exact_agreement_required
        ):
            raise ValueError("first-faulty-layer practice threshold is not met")
        return self


class P1FormalDraftRecord(Phase4Contract):
    annotation_item_id: Identifier
    annotation_protocol_sha256: Literal[P1_PROTOCOL_SHA256] = P1_PROTOCOL_SHA256
    rater_id: Literal[P1_PRACTICE_RATER_ID] = P1_PRACTICE_RATER_ID
    annotation_round: Literal[1] = 1
    blinded_to_primary_labels: Literal[True] = True
    blinded_to_method_predictions: Literal[True] = True
    blinded_to_other_raters: Literal[True] = True
    status: Literal["pending"] = "pending"
    process_correct: None = None
    has_error: None = None
    reasoning_correct: None = None
    plan_code_aligned: None = None
    first_faulty_layer: None = None
    first_faulty_step: None = None
    error_type: None = None
    rationale: None = None


class P1FormalPacketManifest(Phase4Contract):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase4_p1_formal_annotation_packet"] = (
        "tracejudge_phase4_p1_formal_annotation_packet"
    )
    packet_id: Literal[P1_FORMAL_PACKET_ID] = P1_FORMAL_PACKET_ID
    status: Literal["frozen"] = "frozen"
    subset_id: Literal[P1_FORMAL_SUBSET_ID] = P1_FORMAL_SUBSET_ID
    protocol_sha256: Literal[P1_PROTOCOL_SHA256] = P1_PROTOCOL_SHA256
    phase3_annotation_guide_sha256: Sha256
    delivery_record_sha256: Sha256
    practice_admission_sha256: Sha256
    formal_subset_private_manifest_sha256: Sha256
    formal_subset_public_commitment_sha256: Sha256
    selected_materials_sha256: Sha256
    annotation_order_algorithm: Literal[P1_FORMAL_PACKET_ORDER_ALGORITHM] = (
        P1_FORMAL_PACKET_ORDER_ALGORITHM
    )
    ordered_annotation_item_ids: tuple[Identifier, ...]
    rater_id: Literal[P1_PRACTICE_RATER_ID] = P1_PRACTICE_RATER_ID
    annotation_round: Literal[1] = 1
    natural_item_count: Literal[15] = 15
    counterfactual_item_count: Literal[5] = 5
    item_count: Literal[20] = 20
    participant_packet_path: Literal["participant/packet.jsonl"] = "participant/packet.jsonl"
    participant_labels_template_path: Literal["participant/labels_template.jsonl"] = (
        "participant/labels_template.jsonl"
    )
    coordinator_identity_map_path: Literal["coordinator/identity_map.jsonl"] = (
        "coordinator/identity_map.jsonl"
    )
    participant_packet_sha256: Sha256
    participant_labels_template_sha256: Sha256
    coordinator_identity_map_sha256: Sha256
    participant_distribution_excludes_identity_map: Literal[True] = True
    contains_primary_rater_labels: Literal[False] = False
    contains_method_predictions: Literal[False] = False
    contains_provider_raw: Literal[False] = False
    contains_counterfactual_mutation_metadata: Literal[False] = False
    contains_official_hidden_inputs: Literal[False] = False
    provider_call_count: Literal[0] = 0
    docker_call_count: Literal[0] = 0
    network_call_count: Literal[0] = 0

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.ordered_annotation_item_ids != _FORMAL_ITEM_IDS:
            raise ValueError("formal packet item IDs differ from the frozen order")
        if self.item_count != self.natural_item_count + self.counterfactual_item_count:
            raise ValueError("formal packet kind counts do not cover all items")
        return self


@dataclass(frozen=True, slots=True)
class P1PracticeAdmissionResult:
    record: P1PracticeAdmissionRecord
    record_sha256: str


@dataclass(frozen=True, slots=True)
class P1PracticeAdmissionWriteResult(P1PracticeAdmissionResult):
    record_path: Path


@dataclass(frozen=True, slots=True)
class P1FormalPacketPreflight:
    manifest: P1FormalPacketManifest
    manifest_sha256: str
    participant_packet_sha256: str
    participant_labels_template_sha256: str
    coordinator_identity_map_sha256: str


@dataclass(frozen=True, slots=True)
class P1FormalPacketResult(P1FormalPacketPreflight):
    bundle_dir: Path
    manifest_path: Path
    participant_packet_path: Path
    participant_labels_template_path: Path
    coordinator_identity_map_path: Path


@dataclass(frozen=True, slots=True)
class P1FormalPacketVerification:
    packet_id: str
    item_count: int
    manifest_sha256: str
    verified: bool


@dataclass(frozen=True, slots=True)
class _PreparedFormalPacket:
    preflight: P1FormalPacketPreflight
    manifest_payload: bytes
    packet_payload: bytes
    labels_payload: bytes
    identity_payload: bytes
    output_root: Path
    bundle_dir: Path


def _parse_practice_labels(payload: bytes) -> tuple[P1CompletedPracticeRecord, ...]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise Phase4P1AnnotationError(
            "practice labels are not UTF-8 JSONL", safe_stage="P4D_P1_ADMISSION"
        ) from None
    lines = text.splitlines()
    if len(lines) != 5 or any(not line.strip() for line in lines):
        raise Phase4P1AnnotationError(
            "practice labels must contain exactly five non-empty lines",
            safe_stage="P4D_P1_ADMISSION",
        )
    records: list[P1CompletedPracticeRecord] = []
    for line in lines:
        try:
            records.append(
                P1CompletedPracticeRecord.model_validate(
                    _decode_json(line.encode("utf-8"), label="P1 completed practice label")
                )
            )
        except ValidationError:
            raise Phase4P1AnnotationError(
                "practice labels failed schema validation",
                safe_stage="P4D_P1_ADMISSION",
            ) from None
    expected = tuple(f"practice_item_{index:03d}" for index in range(1, 6))
    if tuple(item.practice_item_id for item in records) != expected:
        raise Phase4P1AnnotationError(
            "practice label IDs or order differ from the frozen template",
            safe_stage="P4D_P1_ADMISSION",
        )
    return tuple(records)


def prepare_p1_practice_admission(
    *,
    completed_labels_path: str | Path,
    returned_archive_sha256: str,
    public_evidence_only_rationales_confirmed: bool,
    coordinator_written_authorization_confirmed: bool,
    privacy_or_blinding_violation_count: int = 0,
    admitted_at: datetime | None = None,
    arrangement_path: str | Path = (
        "docs/experiments/phase4_p1_second_annotator_arrangement_v1.md"
    ),
    protocol_path: str | Path = P1_PROTOCOL_RELATIVE_PATH,
    phase3_guide_path: str | Path = "docs/experiments/phase3_annotation_guide_v1.md",
    source_path: str | Path = P1_PRACTICE_SOURCE_RELATIVE_PATH,
    coordinator_reference_path: str | Path = P1_COORDINATOR_REFERENCE_DEFAULT_PATH,
) -> P1PracticeAdmissionResult:
    """Score the five public calibration items and build a private admission record."""

    if privacy_or_blinding_violation_count != 0:
        raise Phase4P1AnnotationError(
            "practice admission requires zero privacy or blinding violations",
            safe_stage="P4D_P1_ADMISSION",
        )
    if not public_evidence_only_rationales_confirmed:
        raise Phase4P1AnnotationError(
            "practice rationales require coordinator public-evidence confirmation",
            safe_stage="P4D_P1_ADMISSION",
        )
    if not coordinator_written_authorization_confirmed:
        raise Phase4P1AnnotationError(
            "practice admission requires written coordinator authorization",
            safe_stage="P4D_P1_ADMISSION",
        )
    _protocol, source = _load_protocol_and_source(
        arrangement_path=arrangement_path,
        protocol_path=protocol_path,
        phase3_guide_path=phase3_guide_path,
        source_path=source_path,
    )
    reference_payload, references = _load_private_references(
        Path(coordinator_reference_path), source=source
    )
    labels_payload = _read_regular_file(
        Path(completed_labels_path), label="P1 completed practice labels"
    )
    labels = _parse_practice_labels(labels_payload)
    references_by_id = {item.practice_item_id: item for item in references}
    has_error_count = sum(
        item.has_error == references_by_id[item.practice_item_id].reference_annotation.has_error
        for item in labels
    )
    process_count = sum(
        item.process_correct
        == references_by_id[item.practice_item_id].reference_annotation.process_correct
        for item in labels
    )
    error_references = {
        item.practice_item_id: item for item in references if item.reference_annotation.has_error
    }
    layer_count = sum(
        item.first_faulty_layer
        == error_references[item.practice_item_id].reference_annotation.first_faulty_layer
        for item in labels
        if item.practice_item_id in error_references
    )
    try:
        record = P1PracticeAdmissionRecord(
            admitted_at=admitted_at or datetime.now(UTC),
            completed_labels_sha256=hashlib.sha256(labels_payload).hexdigest(),
            returned_archive_sha256=returned_archive_sha256,
            coordinator_reference_sha256=hashlib.sha256(reference_payload).hexdigest(),
            has_error_exact_agreement_count=has_error_count,
            process_correct_exact_agreement_count=process_count,
            error_item_first_faulty_layer_exact_agreement_count=layer_count,
        )
    except ValidationError:
        raise Phase4P1AnnotationError(
            "practice response does not meet the frozen admission thresholds",
            safe_stage="P4D_P1_ADMISSION",
        ) from None
    payload = _json_bytes(record)
    return P1PracticeAdmissionResult(
        record=record,
        record_sha256=hashlib.sha256(payload).hexdigest(),
    )


def write_p1_practice_admission(
    *, output_path: str | Path = P1_PRACTICE_ADMISSION_DEFAULT_PATH, **kwargs: Any
) -> P1PracticeAdmissionWriteResult:
    """Write one immutable private practice-admission record."""

    result = prepare_p1_practice_admission(**kwargs)
    destination = Path(output_path).expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        raise Phase4P1AnnotationError(
            "practice admission record already exists", safe_stage="P4D_P1_OUTPUT"
        )
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent.chmod(0o700)
    payload = _json_bytes(result.record)
    with destination.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    destination.chmod(0o600)
    _fsync_directory(parent)
    return P1PracticeAdmissionWriteResult(
        **asdict(result),
        record_path=destination,
    )


def _load_admission(path: str | Path) -> tuple[bytes, P1PracticeAdmissionRecord]:
    destination = Path(path)
    _assert_private_location(destination, label="P1 practice admission record")
    payload = _read_regular_file(destination, label="P1 practice admission record")
    try:
        record = P1PracticeAdmissionRecord.model_validate(
            _decode_json(payload, label="P1 practice admission record")
        )
    except ValidationError:
        raise Phase4P1AnnotationError(
            "practice admission record failed schema validation",
            safe_stage="P4D_P1_ADMISSION",
        ) from None
    return payload, record


def _formal_rank(trace_id: str) -> str:
    value = f"{P1_PROTOCOL_SHA256}\0{P1_FORMAL_SUBSET_ID}\0{trace_id}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_private_subset(path: str | Path) -> tuple[bytes, P1FormalSubsetManifest]:
    destination = Path(path)
    _assert_private_location(destination, label="P1 formal subset manifest")
    payload = _read_regular_file(destination, label="P1 formal subset manifest")
    try:
        value = P1FormalSubsetManifest.model_validate(
            _decode_json(payload, label="P1 formal subset manifest")
        )
    except ValidationError:
        raise Phase4P1AnnotationError(
            "formal subset manifest failed schema validation",
            safe_stage="P4D_P1_FORMAL_PACKET",
        ) from None
    return payload, value


def prepare_p1_formal_packet(
    *,
    protocol_path: str | Path = P1_PROTOCOL_RELATIVE_PATH,
    phase3_guide_path: str | Path = "docs/experiments/phase3_annotation_guide_v1.md",
    delivery_schema_path: str | Path = P1_DELIVERY_SCHEMA_RELATIVE_PATH,
    delivery_record_path: str | Path = P1_DELIVERY_RECORD_DEFAULT_PATH,
    practice_admission_path: str | Path = P1_PRACTICE_ADMISSION_DEFAULT_PATH,
    formal_subset_manifest_path: str | Path = P1_FORMAL_PRIVATE_MANIFEST_DEFAULT_PATH,
    formal_subset_commitment_path: str | Path = P1_FORMAL_PUBLIC_COMMITMENT_DEFAULT_PATH,
    cohort_manifest_path: str | Path,
    natural_manifest_path: str | Path,
    phase1_run_dir: str | Path,
    phase2_run_dir: str | Path,
    dataset_manifest_path: str | Path,
    source_bundle_path: str | Path,
    execution_run_dir: str | Path,
    output_dir: str | Path = P1_FORMAL_PACKET_DEFAULT_OUTPUT,
    privacy_canaries: tuple[str | bytes, ...] = (),
) -> _PreparedFormalPacket:
    """Build the exact formal packet in memory after every operational gate passes."""

    delivery = preflight_p1_delivery_record(
        schema_path=delivery_schema_path, record_path=delivery_record_path
    )
    if not delivery.data_collection_allowed:
        raise Phase4P1AnnotationError(
            "delivery record does not authorize formal packet creation",
            safe_stage="P4D_P1_DELIVERY",
        )
    admission_payload, admission = _load_admission(practice_admission_path)
    if admission.decision != "admitted_to_formal_20":
        raise Phase4P1AnnotationError(
            "practice admission does not authorize the formal 20",
            safe_stage="P4D_P1_ADMISSION",
        )
    guide_payload = _read_regular_file(Path(phase3_guide_path), label="Phase-3 annotation guide")
    if hashlib.sha256(guide_payload).hexdigest() != P1_PHASE3_GUIDE_SHA256:
        raise Phase4P1AnnotationError(
            "Phase-3 annotation guide differs from the frozen identity",
            safe_stage="P4D_P1_FORMAL_PACKET",
        )
    subset_verification = verify_p1_formal_subset(
        private_manifest_path=formal_subset_manifest_path,
        public_commitment_path=formal_subset_commitment_path,
        protocol_path=protocol_path,
        cohort_manifest_path=cohort_manifest_path,
        natural_manifest_path=natural_manifest_path,
    )
    subset_payload, subset = _load_private_subset(formal_subset_manifest_path)
    try:
        loaded: LoadedPhase3Materials = load_phase3_materials(
            cohort_manifest_path=cohort_manifest_path,
            natural_manifest_path=natural_manifest_path,
            phase1_run_dir=phase1_run_dir,
            phase2_run_dir=phase2_run_dir,
            dataset_manifest_path=dataset_manifest_path,
            source_bundle_path=source_bundle_path,
            execution_run_dir=execution_run_dir,
            privacy_canaries=privacy_canaries,
        )
    except Phase3RunnerError as exc:
        raise Phase4P1AnnotationError(
            "formal packet materials failed frozen binding", safe_stage=exc.safe_stage
        ) from None
    selected_ids = tuple(record.trace_id for record in subset.records)
    if any(trace_id not in loaded.materials for trace_id in selected_ids):
        raise Phase4P1AnnotationError(
            "formal subset is not covered by the frozen material set",
            safe_stage="P4D_P1_FORMAL_PACKET",
        )
    ordered_trace_ids = tuple(sorted(selected_ids, key=lambda item: (_formal_rank(item), item)))
    records_by_id = {item.trace_id: item for item in subset.records}
    tasks: list[BlindedAnnotationTask] = []
    identities: list[AnnotationIdentityRecord] = []
    drafts: list[P1FormalDraftRecord] = []
    material_payloads: list[dict[str, Any]] = []
    for item_id, trace_id in zip(_FORMAL_ITEM_IDS, ordered_trace_ids, strict=True):
        subset_record = records_by_id[trace_id]
        trace = loaded.cohort.traces_by_id[trace_id]
        material = loaded.materials[trace_id]
        if (
            trace.problem_id != subset_record.problem_id
            or trace.code_sha256 != subset_record.code_sha256
            or trace.structured_explanation_sha256 != subset_record.structured_explanation_sha256
            or trace.functional_evidence.functional_evidence_sha256
            != subset_record.functional_evidence_sha256
        ):
            raise Phase4P1AnnotationError(
                "formal subset record differs from frozen trace materials",
                safe_stage="P4D_P1_FORMAL_PACKET",
            )
        dynamic = material.public_dynamic_evidence
        if dynamic is None or dynamic.payload is None:
            raise Phase4P1AnnotationError(
                "formal item lacks a public-evidence availability record",
                safe_stage="P4D_P1_FORMAL_PACKET",
            )
        task = BlindedAnnotationTask(
            annotation_item_id=item_id,
            problem_id=trace.problem_id,
            code_sha256=trace.code_sha256,
            structured_explanation_sha256=trace.structured_explanation_sha256,
            functional_evidence_sha256=trace.functional_evidence.functional_evidence_sha256,
            public_problem=material.public_problem,
            structured_solution_trace=_structured_solution_trace(material.solution_trace),
            candidate_code=material.solution_trace.code,
            functional_evidence=functional_evidence_payload(material.functional_evidence),
            public_dynamic_evidence=dynamic.payload,
        )
        assert_public_payload_safe(task, canaries=privacy_canaries)
        serialized = json.dumps(task.model_dump(mode="json"), ensure_ascii=False)
        if any(
            forbidden in serialized
            for forbidden in (
                "mutation_kind",
                "sole_change",
                "expected_impact",
                "expected_execution_status",
                "expectation_met",
                "method_id",
                "method_predictions",
                "primary_rater",
            )
        ):
            raise Phase4P1AnnotationError(
                "formal participant task contains forbidden metadata",
                safe_stage="P4D_P1_BLINDING",
            )
        tasks.append(task)
        identities.append(
            AnnotationIdentityRecord(
                annotation_item_id=item_id,
                trace_id=trace_id,
                problem_id=trace.problem_id,
                code_sha256=trace.code_sha256,
                structured_explanation_sha256=trace.structured_explanation_sha256,
                functional_evidence_sha256=(trace.functional_evidence.functional_evidence_sha256),
            )
        )
        drafts.append(P1FormalDraftRecord(annotation_item_id=item_id))
        material_payloads.append(material.model_dump(mode="json"))
    packet_payload = _jsonl_bytes(tasks)
    identity_payload = _jsonl_bytes(identities)
    labels_payload = _jsonl_bytes(drafts)
    manifest = P1FormalPacketManifest(
        phase3_annotation_guide_sha256=P1_PHASE3_GUIDE_SHA256,
        delivery_record_sha256=delivery.record_sha256,
        practice_admission_sha256=hashlib.sha256(admission_payload).hexdigest(),
        formal_subset_private_manifest_sha256=hashlib.sha256(subset_payload).hexdigest(),
        formal_subset_public_commitment_sha256=(subset_verification.public_commitment_sha256),
        selected_materials_sha256=canonical_sha256(material_payloads),
        ordered_annotation_item_ids=_FORMAL_ITEM_IDS,
        participant_packet_sha256=hashlib.sha256(packet_payload).hexdigest(),
        participant_labels_template_sha256=hashlib.sha256(labels_payload).hexdigest(),
        coordinator_identity_map_sha256=hashlib.sha256(identity_payload).hexdigest(),
    )
    assert_public_payload_safe(manifest, canaries=privacy_canaries)
    manifest_payload = _json_bytes(manifest)
    output_root = Path(output_dir).expanduser().resolve()
    bundle_dir = output_root / P1_FORMAL_PACKET_ID
    return _PreparedFormalPacket(
        preflight=P1FormalPacketPreflight(
            manifest=manifest,
            manifest_sha256=hashlib.sha256(manifest_payload).hexdigest(),
            participant_packet_sha256=manifest.participant_packet_sha256,
            participant_labels_template_sha256=(manifest.participant_labels_template_sha256),
            coordinator_identity_map_sha256=manifest.coordinator_identity_map_sha256,
        ),
        manifest_payload=manifest_payload,
        packet_payload=packet_payload,
        labels_payload=labels_payload,
        identity_payload=identity_payload,
        output_root=output_root,
        bundle_dir=bundle_dir,
    )


def preflight_p1_formal_packet(**kwargs: Any) -> P1FormalPacketPreflight:
    return prepare_p1_formal_packet(**kwargs).preflight


def _write_private_file(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o600)


def write_p1_formal_packet(**kwargs: Any) -> P1FormalPacketResult:
    prepared = prepare_p1_formal_packet(**kwargs)
    if prepared.bundle_dir.exists() or prepared.bundle_dir.is_symlink():
        raise Phase4P1AnnotationError(
            "formal packet directory already exists", safe_stage="P4D_P1_OUTPUT"
        )
    prepared.output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    prepared.output_root.chmod(0o700)
    temporary_dir: Path | None = None
    try:
        temporary_dir = Path(
            tempfile.mkdtemp(prefix=f".{P1_FORMAL_PACKET_ID}.", dir=prepared.output_root)
        )
        temporary_dir.chmod(0o700)
        _write_private_file(temporary_dir / "manifest.json", prepared.manifest_payload)
        _write_private_file(temporary_dir / "participant/packet.jsonl", prepared.packet_payload)
        _write_private_file(
            temporary_dir / "participant/labels_template.jsonl", prepared.labels_payload
        )
        _write_private_file(
            temporary_dir / "coordinator/identity_map.jsonl", prepared.identity_payload
        )
        os.replace(temporary_dir, prepared.bundle_dir)
        temporary_dir = None
        _fsync_directory(prepared.output_root)
    except OSError:
        raise Phase4P1AnnotationError(
            "cannot atomically write formal packet", safe_stage="P4D_P1_OUTPUT"
        ) from None
    finally:
        if temporary_dir is not None:
            shutil.rmtree(temporary_dir, ignore_errors=True)
    return P1FormalPacketResult(
        **asdict(prepared.preflight),
        bundle_dir=prepared.bundle_dir,
        manifest_path=prepared.bundle_dir / "manifest.json",
        participant_packet_path=prepared.bundle_dir / "participant/packet.jsonl",
        participant_labels_template_path=(
            prepared.bundle_dir / "participant/labels_template.jsonl"
        ),
        coordinator_identity_map_path=(prepared.bundle_dir / "coordinator/identity_map.jsonl"),
    )


def verify_p1_formal_packet(
    *, manifest_path: str | Path, expected_manifest_sha256: str | None = None, **kwargs: Any
) -> P1FormalPacketVerification:
    path = Path(manifest_path)
    stored_payload = _read_regular_file(path, label="P1 formal packet manifest")
    actual_sha256 = hashlib.sha256(stored_payload).hexdigest()
    if expected_manifest_sha256 is not None and actual_sha256 != expected_manifest_sha256:
        raise Phase4P1AnnotationError(
            "formal packet manifest differs from the expected identity",
            safe_stage="P4D_P1_VERIFY",
        )
    prepared = prepare_p1_formal_packet(**kwargs)
    if stored_payload != prepared.manifest_payload:
        raise Phase4P1AnnotationError(
            "formal packet manifest differs from deterministic regeneration",
            safe_stage="P4D_P1_VERIFY",
        )
    bundle_dir = path.parent
    comparisons = (
        ("participant/packet.jsonl", prepared.packet_payload),
        ("participant/labels_template.jsonl", prepared.labels_payload),
        ("coordinator/identity_map.jsonl", prepared.identity_payload),
    )
    for relative_path, expected_payload in comparisons:
        if (
            _read_regular_file(bundle_dir / relative_path, label="P1 formal packet artifact")
            != expected_payload
        ):
            raise Phase4P1AnnotationError(
                "formal packet artifact differs from deterministic regeneration",
                safe_stage="P4D_P1_VERIFY",
            )
    return P1FormalPacketVerification(
        packet_id=prepared.preflight.manifest.packet_id,
        item_count=prepared.preflight.manifest.item_count,
        manifest_sha256=actual_sha256,
        verified=True,
    )


__all__ = [
    "P1_FORMAL_PACKET_DEFAULT_OUTPUT",
    "P1_FORMAL_PACKET_ID",
    "P1_FORMAL_PACKET_ORDER_ALGORITHM",
    "P1_PRACTICE_ADMISSION_DEFAULT_PATH",
    "P1_PRACTICE_ADMISSION_ID",
    "P1CompletedPracticeRecord",
    "P1FormalPacketManifest",
    "P1FormalPacketPreflight",
    "P1FormalPacketResult",
    "P1FormalPacketVerification",
    "P1PracticeAdmissionRecord",
    "P1PracticeAdmissionResult",
    "P1PracticeAdmissionWriteResult",
    "preflight_p1_formal_packet",
    "prepare_p1_formal_packet",
    "prepare_p1_practice_admission",
    "verify_p1_formal_packet",
    "write_p1_formal_packet",
    "write_p1_practice_admission",
]
