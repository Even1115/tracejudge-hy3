"""Fail-closed P1 delivery gating and deterministic formal-subset freezing.

This module never sends participant materials and never reads Phase-3 labels,
method predictions, provider outcomes, or post-hoc analysis.  The delivery
record is private and Git-ignored; the formal-subset commitment is public but
contains no selected trace identifiers.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import stat
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, ValidationError, field_validator, model_validator

from tracejudge_hy3.phase3.contracts import Identifier as Phase3Identifier
from tracejudge_hy3.phase3.privacy import assert_public_payload_safe

from .contracts import Phase4Contract, Sha256
from .p1_annotations import (
    P1_COHORT_MANIFEST_SHA256,
    P1_NATURAL_MANIFEST_SHA256,
    P1_PROTOCOL_RELATIVE_PATH,
    P1_PROTOCOL_SHA256,
    P1SecondAnnotatorProtocol,
    Phase4P1AnnotationError,
    _decode_json,
    _fsync_directory,
    _json_bytes,
    _load_hashed_json,
    _load_phase3_manifests,
    _read_regular_file,
    _write_public_file,
)

P1_DELIVERY_ID = "phase4_p1_single_delivery_v1"
P1_DELIVERY_SCHEMA_RELATIVE_PATH = "data/phase4/p1_single_delivery_record_schema_v1.json"
P1_DELIVERY_SCHEMA_SHA256 = "3e2cf0921da2bebac52505cc87e503d2f559d889c9672c53a57614116f32fdd9"
P1_DELIVERY_RECORD_DEFAULT_PATH = (
    "artifacts/experiments/phase4-p1-annotations/phase4_p1_single_delivery_v1/delivery_record.json"
)
P1_FORMAL_SUBSET_ID = "phase4_p1_formal_subset_v1"
P1_FORMAL_SUBSET_SEED = 20260902
P1_FORMAL_SELECTION_ALGORITHM = (
    "sha256_seeded_rank_v1:natural_top15;"
    "counterfactual_parent_first_then_global_fill_max2;source_order_output"
)
P1_FORMAL_PRIVATE_MANIFEST_DEFAULT_PATH = (
    "artifacts/experiments/phase4-p1-annotations/phase4_p1_formal_subset_v1/manifest.json"
)
P1_FORMAL_PUBLIC_COMMITMENT_DEFAULT_PATH = (
    "docs/experiments/phase4_p1_formal_subset/phase4_p1_formal_subset_v1/commitment.json"
)
P1_SELECTION_FORBIDDEN_INPUTS = (
    "primary_rater_labels",
    "method_predictions",
    "provider_status",
    "post_hoc_results",
)


class P1DeliveryChannels(Phase4Contract):
    file_delivery_channel: str | None = Field(default=None, min_length=1, max_length=500)
    password_channel: str | None = Field(default=None, min_length=1, max_length=500)
    return_channel: str | None = Field(default=None, min_length=1, max_length=500)
    faq_channel: str | None = Field(default=None, min_length=1, max_length=500)
    emergency_channel: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_separate_password_channel(self) -> Self:
        if (
            self.file_delivery_channel is not None
            and self.password_channel is not None
            and self.file_delivery_channel == self.password_channel
        ):
            raise ValueError("password channel must differ from the file delivery channel")
        return self


class P1SingleDeliveryRecord(Phase4Contract):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase4_p1_single_delivery_record"] = (
        "tracejudge_phase4_p1_single_delivery_record"
    )
    delivery_id: Literal[P1_DELIVERY_ID] = P1_DELIVERY_ID
    record_status: Literal["pending_completion", "ready_for_practice_delivery"] = (
        "pending_completion"
    )
    schema_relative_path: Literal[P1_DELIVERY_SCHEMA_RELATIVE_PATH] = (
        P1_DELIVERY_SCHEMA_RELATIVE_PATH
    )
    schema_sha256: Sha256
    protocol_id: Literal["phase4_p1_second_annotator_protocol_v1"] = (
        "phase4_p1_second_annotator_protocol_v1"
    )
    protocol_sha256: Literal[P1_PROTOCOL_SHA256] = P1_PROTOCOL_SHA256
    ethics_status: Literal["READY"] = "READY"
    ethics_decision: Literal["approved"] = "approved"
    ethics_confirmed_on: Literal["2026-09-02"] = "2026-09-02"
    ethics_verifier_role: Literal["supervising_advisor"] = "supervising_advisor"
    ethics_record_storage: Literal["private_restricted_location"] = "private_restricted_location"
    rater_id: Literal["p1_rater_02"] = "p1_rater_02"
    participant_consent_confirmed: bool = False
    participant_consent_confirmed_at: datetime | None = None
    participant_receipt_verified_at: datetime | None = None
    channels: P1DeliveryChannels = Field(default_factory=P1DeliveryChannels)
    practice_due_at: datetime | None = None
    formal_due_at: datetime | None = None
    compensation_terms: str | None = Field(default=None, min_length=1, max_length=1000)
    credit_and_authorship_terms: str | None = Field(default=None, min_length=1, max_length=1000)
    withdrawal_cutoff_terms: str | None = Field(default=None, min_length=1, max_length=1000)
    retention_and_destruction_terms: str | None = Field(default=None, min_length=1, max_length=1000)
    coordinator_contact: str | None = Field(default=None, min_length=1, max_length=500)
    project_owner_delivery_authorization_confirmed: bool = False
    outbound_practice_archive_sha256: Sha256 | None = None
    outbound_formal_archive_sha256: Sha256 | None = None
    returned_archive_sha256: Sha256 | None = None
    incident_count: int = Field(default=0, ge=0)
    final_deletion_confirmed_at: datetime | None = None
    private_restricted_storage: Literal[True] = True
    public_release_forbidden: Literal[True] = True
    may_contain_private_contact_details: Literal[True] = True
    data_collection_allowed: bool = False
    formal_packet_created: Literal[False] = False
    formal_data_collected: Literal[False] = False

    @field_validator(
        "participant_consent_confirmed_at",
        "participant_receipt_verified_at",
        "practice_due_at",
        "formal_due_at",
        "final_deletion_confirmed_at",
    )
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("delivery timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_gate(self) -> Self:
        if self.schema_sha256 != P1_DELIVERY_SCHEMA_SHA256:
            raise ValueError("delivery record schema hash differs from the frozen identity")
        required_values = (
            self.participant_consent_confirmed_at,
            self.participant_receipt_verified_at,
            self.channels.file_delivery_channel,
            self.channels.password_channel,
            self.channels.return_channel,
            self.channels.faq_channel,
            self.channels.emergency_channel,
            self.practice_due_at,
            self.formal_due_at,
            self.compensation_terms,
            self.credit_and_authorship_terms,
            self.withdrawal_cutoff_terms,
            self.retention_and_destruction_terms,
            self.coordinator_contact,
        )
        ready = (
            self.participant_consent_confirmed
            and self.project_owner_delivery_authorization_confirmed
            and all(value is not None for value in required_values)
        )
        if self.record_status == "pending_completion":
            if self.data_collection_allowed:
                raise ValueError("pending delivery record cannot allow data collection")
        elif not ready or not self.data_collection_allowed:
            raise ValueError("ready delivery record is missing a required confirmation")
        if self.practice_due_at and self.participant_receipt_verified_at:
            if self.practice_due_at <= self.participant_receipt_verified_at:
                raise ValueError("practice deadline must follow verified receipt")
        if self.formal_due_at and self.participant_receipt_verified_at:
            if self.formal_due_at <= self.participant_receipt_verified_at:
                raise ValueError("formal deadline must follow verified receipt")
        return self


class P1FormalSubsetRecord(Phase4Contract):
    trace_id: Phase3Identifier
    trace_kind: Literal["natural", "counterfactual"]
    problem_id: Phase3Identifier
    public_problem_sha256: Sha256
    solution_trace_sha256: Sha256
    structured_explanation_sha256: Sha256
    code_sha256: Sha256
    functional_evidence_sha256: Sha256
    parent_trace_id: Phase3Identifier | None = None

    @model_validator(mode="after")
    def validate_kind(self) -> Self:
        if (self.trace_kind == "counterfactual") != (self.parent_trace_id is not None):
            raise ValueError("only counterfactual rows may contain a parent trace ID")
        return self


class P1FormalSubsetManifest(Phase4Contract):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase4_p1_formal_subset_private_manifest"] = (
        "tracejudge_phase4_p1_formal_subset_private_manifest"
    )
    subset_id: Literal[P1_FORMAL_SUBSET_ID] = P1_FORMAL_SUBSET_ID
    status: Literal["frozen"] = "frozen"
    frozen_on: Literal["2026-09-02"] = "2026-09-02"
    protocol_id: Literal["phase4_p1_second_annotator_protocol_v1"] = (
        "phase4_p1_second_annotator_protocol_v1"
    )
    protocol_sha256: Literal[P1_PROTOCOL_SHA256] = P1_PROTOCOL_SHA256
    natural_manifest_sha256: Literal[P1_NATURAL_MANIFEST_SHA256] = P1_NATURAL_MANIFEST_SHA256
    cohort_manifest_sha256: Literal[P1_COHORT_MANIFEST_SHA256] = P1_COHORT_MANIFEST_SHA256
    selection_seed: Literal[P1_FORMAL_SUBSET_SEED] = P1_FORMAL_SUBSET_SEED
    selection_algorithm: Literal[P1_FORMAL_SELECTION_ALGORITHM] = P1_FORMAL_SELECTION_ALGORITHM
    selection_forbidden_inputs: tuple[
        Literal[
            "primary_rater_labels", "method_predictions", "provider_status", "post_hoc_results"
        ],
        ...,
    ]
    source_natural_count: Literal[42] = 42
    source_counterfactual_count: Literal[15] = 15
    selected_natural_count: Literal[15] = 15
    selected_counterfactual_count: Literal[5] = 5
    selected_total_count: Literal[20] = 20
    counterfactual_parent_count: Literal[3] = 3
    counterfactual_per_parent_maximum: Literal[2] = 2
    counterfactual_parent_counts: dict[Phase3Identifier, int]
    ordered_trace_ids_sha256: Sha256
    records: tuple[P1FormalSubsetRecord, ...]
    selection_implementation_sha256: Sha256
    practice_response_reviewed_before_freeze: Literal[False] = False
    contains_primary_rater_labels: Literal[False] = False
    contains_method_predictions: Literal[False] = False
    contains_provider_status: Literal[False] = False
    contains_post_hoc_results: Literal[False] = False
    formal_packet_created: Literal[False] = False
    formal_data_collected: Literal[False] = False

    @model_validator(mode="after")
    def validate_subset(self) -> Self:
        if self.selection_forbidden_inputs != P1_SELECTION_FORBIDDEN_INPUTS:
            raise ValueError("formal subset forbidden inputs differ from policy")
        if len(self.records) != 20 or len({item.trace_id for item in self.records}) != 20:
            raise ValueError("formal subset must contain 20 unique records")
        kinds = Counter(item.trace_kind for item in self.records)
        if kinds != {"natural": 15, "counterfactual": 5}:
            raise ValueError("formal subset kind counts are inconsistent")
        actual_parent_counts = Counter(
            item.parent_trace_id for item in self.records if item.parent_trace_id is not None
        )
        if dict(sorted(actual_parent_counts.items())) != dict(
            sorted(self.counterfactual_parent_counts.items())
        ):
            raise ValueError("formal subset parent counts are inconsistent")
        if len(actual_parent_counts) != 3 or max(actual_parent_counts.values()) > 2:
            raise ValueError("formal subset violates parent coverage or maximum")
        if self.ordered_trace_ids_sha256 != _ordered_ids_sha256(
            tuple(item.trace_id for item in self.records)
        ):
            raise ValueError("formal subset ordered ID hash is inconsistent")
        return self


class P1FormalSubsetCommitment(Phase4Contract):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase4_p1_formal_subset_public_commitment"] = (
        "tracejudge_phase4_p1_formal_subset_public_commitment"
    )
    subset_id: Literal[P1_FORMAL_SUBSET_ID] = P1_FORMAL_SUBSET_ID
    status: Literal["frozen"] = "frozen"
    frozen_on: Literal["2026-09-02"] = "2026-09-02"
    protocol_sha256: Literal[P1_PROTOCOL_SHA256] = P1_PROTOCOL_SHA256
    natural_manifest_sha256: Literal[P1_NATURAL_MANIFEST_SHA256] = P1_NATURAL_MANIFEST_SHA256
    cohort_manifest_sha256: Literal[P1_COHORT_MANIFEST_SHA256] = P1_COHORT_MANIFEST_SHA256
    selection_seed: Literal[P1_FORMAL_SUBSET_SEED] = P1_FORMAL_SUBSET_SEED
    selection_algorithm: Literal[P1_FORMAL_SELECTION_ALGORITHM] = P1_FORMAL_SELECTION_ALGORITHM
    selection_forbidden_inputs: tuple[
        Literal[
            "primary_rater_labels", "method_predictions", "provider_status", "post_hoc_results"
        ],
        ...,
    ]
    source_natural_count: Literal[42] = 42
    source_counterfactual_count: Literal[15] = 15
    selected_natural_count: Literal[15] = 15
    selected_counterfactual_count: Literal[5] = 5
    selected_total_count: Literal[20] = 20
    counterfactual_parent_count: Literal[3] = 3
    counterfactual_per_parent_maximum: Literal[2] = 2
    ordered_trace_ids_sha256: Sha256
    private_manifest_artifact_id: Literal["phase4_p1_formal_subset_v1_private_manifest"] = (
        "phase4_p1_formal_subset_v1_private_manifest"
    )
    private_manifest_sha256: Sha256
    private_manifest_storage: Literal["git_ignored_private_artifact"] = (
        "git_ignored_private_artifact"
    )
    selection_implementation_sha256: Sha256
    practice_response_reviewed_before_freeze: Literal[False] = False
    contains_selected_trace_ids: Literal[False] = False
    contains_problem_ids: Literal[False] = False
    contains_private_paths: Literal[False] = False
    contains_primary_rater_labels: Literal[False] = False
    contains_method_predictions: Literal[False] = False
    contains_provider_status: Literal[False] = False
    contains_post_hoc_results: Literal[False] = False
    provider_call_count: Literal[0] = 0
    docker_call_count: Literal[0] = 0
    network_call_count: Literal[0] = 0
    formal_packet_created: Literal[False] = False
    formal_data_collected: Literal[False] = False

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        if self.selection_forbidden_inputs != P1_SELECTION_FORBIDDEN_INPUTS:
            raise ValueError("public commitment forbidden inputs differ from policy")
        return self


@dataclass(frozen=True, slots=True)
class P1DeliveryRecordPreflight:
    delivery_id: str
    record_status: str
    schema_sha256: str
    record_sha256: str
    missing_required_count: int
    data_collection_allowed: bool


@dataclass(frozen=True, slots=True)
class P1FormalSubsetPreflight:
    manifest: P1FormalSubsetManifest
    commitment: P1FormalSubsetCommitment
    private_manifest_sha256: str
    public_commitment_sha256: str


@dataclass(frozen=True, slots=True)
class P1FormalSubsetResult(P1FormalSubsetPreflight):
    private_manifest_path: Path
    public_commitment_path: Path


@dataclass(frozen=True, slots=True)
class P1FormalSubsetVerification:
    subset_id: str
    private_manifest_sha256: str
    public_commitment_sha256: str
    selected_total_count: int
    verified: bool


@dataclass(frozen=True, slots=True)
class _PreparedFormalSubset:
    preflight: P1FormalSubsetPreflight
    private_payload: bytes
    public_payload: bytes


def _ordered_ids_sha256(values: tuple[str, ...]) -> str:
    payload = json.dumps(
        values,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _assert_private_location(path: Path, *, label: str, require_file: bool = True) -> None:
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise Phase4P1AnnotationError(
            f"{label} parent must be a private non-symlink directory",
            safe_stage="P4D_P1_PRIVACY",
        )
    try:
        if stat.S_IMODE(parent.stat().st_mode) & 0o077:
            raise Phase4P1AnnotationError(
                f"{label} parent permissions are too broad",
                safe_stage="P4D_P1_PRIVACY",
            )
        if require_file:
            if path.is_symlink() or not path.is_file():
                raise Phase4P1AnnotationError(
                    f"{label} must be a regular non-symlink file",
                    safe_stage="P4D_P1_PRIVACY",
                )
            if stat.S_IMODE(path.stat().st_mode) & 0o077:
                raise Phase4P1AnnotationError(
                    f"{label} permissions are too broad",
                    safe_stage="P4D_P1_PRIVACY",
                )
    except OSError:
        raise Phase4P1AnnotationError(f"cannot inspect {label}") from None


def _prepare_private_parent(path: Path, *, label: str) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise Phase4P1AnnotationError(
            f"{label} parent is unsafe",
            safe_stage="P4D_P1_PRIVACY",
        )
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise Phase4P1AnnotationError(
                f"{label} parent is unsafe",
                safe_stage="P4D_P1_PRIVACY",
            )
        path.chmod(0o700)
    except OSError:
        raise Phase4P1AnnotationError(f"cannot prepare {label} parent") from None


def delivery_record_schema_payload() -> bytes:
    schema = P1SingleDeliveryRecord.model_json_schema(mode="validation")
    schema["$id"] = "https://tracejudge-hy3.invalid/schema/p1_single_delivery_record_v1.json"
    return (
        json.dumps(schema, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True).encode(
            "utf-8"
        )
        + b"\n"
    )


def create_p1_delivery_record_template(
    *,
    schema_path: str | Path = P1_DELIVERY_SCHEMA_RELATIVE_PATH,
    record_path: str | Path = P1_DELIVERY_RECORD_DEFAULT_PATH,
) -> P1DeliveryRecordPreflight:
    schema_payload = _read_regular_file(Path(schema_path), label="P1 delivery record schema")
    if hashlib.sha256(schema_payload).hexdigest() != P1_DELIVERY_SCHEMA_SHA256:
        raise Phase4P1AnnotationError(
            "P1 delivery record schema differs from the frozen identity",
            safe_stage="P4D_P1_IDENTITY",
        )
    destination = Path(record_path).expanduser()
    if destination.exists() or destination.is_symlink():
        raise Phase4P1AnnotationError(
            "P1 delivery record already exists",
            safe_stage="P4D_P1_OUTPUT",
        )
    _prepare_private_parent(destination.parent, label="P1 delivery record")
    record = P1SingleDeliveryRecord(schema_sha256=P1_DELIVERY_SCHEMA_SHA256)
    payload = _json_bytes(record)
    try:
        with destination.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        destination.chmod(0o600)
        _fsync_directory(destination.parent)
    except OSError:
        raise Phase4P1AnnotationError(
            "cannot create private P1 delivery record",
            safe_stage="P4D_P1_OUTPUT",
        ) from None
    return preflight_p1_delivery_record(schema_path=schema_path, record_path=destination)


def _delivery_missing_count(record: P1SingleDeliveryRecord) -> int:
    required = (
        record.participant_consent_confirmed,
        record.participant_consent_confirmed_at,
        record.participant_receipt_verified_at,
        record.channels.file_delivery_channel,
        record.channels.password_channel,
        record.channels.return_channel,
        record.channels.faq_channel,
        record.channels.emergency_channel,
        record.practice_due_at,
        record.formal_due_at,
        record.compensation_terms,
        record.credit_and_authorship_terms,
        record.withdrawal_cutoff_terms,
        record.retention_and_destruction_terms,
        record.coordinator_contact,
        record.project_owner_delivery_authorization_confirmed,
    )
    return sum(value is None or value is False for value in required)


def preflight_p1_delivery_record(
    *,
    schema_path: str | Path = P1_DELIVERY_SCHEMA_RELATIVE_PATH,
    record_path: str | Path = P1_DELIVERY_RECORD_DEFAULT_PATH,
) -> P1DeliveryRecordPreflight:
    schema_payload = _read_regular_file(Path(schema_path), label="P1 delivery record schema")
    schema_sha256 = hashlib.sha256(schema_payload).hexdigest()
    if (
        schema_payload != delivery_record_schema_payload()
        or schema_sha256 != P1_DELIVERY_SCHEMA_SHA256
    ):
        raise Phase4P1AnnotationError(
            "P1 delivery record schema differs from deterministic generation",
            safe_stage="P4D_P1_IDENTITY",
        )
    path = Path(record_path)
    _assert_private_location(path, label="P1 delivery record")
    payload = _read_regular_file(path, label="P1 delivery record")
    try:
        record = P1SingleDeliveryRecord.model_validate(
            _decode_json(payload, label="P1 delivery record")
        )
    except ValidationError:
        raise Phase4P1AnnotationError(
            "P1 delivery record failed schema validation",
            safe_stage="P4D_P1_DELIVERY",
        ) from None
    missing_count = _delivery_missing_count(record)
    if record.record_status == "ready_for_practice_delivery" and missing_count:
        raise Phase4P1AnnotationError(
            "P1 delivery record claims readiness while required fields are missing",
            safe_stage="P4D_P1_DELIVERY",
        )
    return P1DeliveryRecordPreflight(
        delivery_id=record.delivery_id,
        record_status=record.record_status,
        schema_sha256=schema_sha256,
        record_sha256=hashlib.sha256(payload).hexdigest(),
        missing_required_count=missing_count,
        data_collection_allowed=record.data_collection_allowed and missing_count == 0,
    )


def _rank_digest(trace_id: str) -> str:
    return hashlib.sha256(f"{P1_FORMAL_SUBSET_SEED}\0{trace_id}".encode()).hexdigest()


def _select_formal_traces(
    natural_traces: Sequence[Any], counterfactual_traces: Sequence[Any]
) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    natural_ranked = sorted(
        natural_traces, key=lambda item: (_rank_digest(item.trace_id), item.trace_id)
    )
    natural_ids = {item.trace_id for item in natural_ranked[:15]}
    selected_natural = tuple(item for item in natural_traces if item.trace_id in natural_ids)

    grouped: dict[str, list[Any]] = {}
    for item in counterfactual_traces:
        grouped.setdefault(item.parent_trace_id, []).append(item)
    if len(grouped) != 3:
        raise Phase4P1AnnotationError(
            "formal subset requires exactly three counterfactual parents",
            safe_stage="P4D_P1_SUBSET",
        )
    selected_ids: set[str] = set()
    parent_counts: Counter[str] = Counter()
    for parent_id in sorted(grouped):
        first = min(
            grouped[parent_id], key=lambda item: (_rank_digest(item.trace_id), item.trace_id)
        )
        selected_ids.add(first.trace_id)
        parent_counts[parent_id] += 1
    remaining = sorted(
        (item for item in counterfactual_traces if item.trace_id not in selected_ids),
        key=lambda item: (_rank_digest(item.trace_id), item.trace_id),
    )
    for item in remaining:
        if len(selected_ids) == 5:
            break
        if parent_counts[item.parent_trace_id] < 2:
            selected_ids.add(item.trace_id)
            parent_counts[item.parent_trace_id] += 1
    selected_counterfactuals = tuple(
        item for item in counterfactual_traces if item.trace_id in selected_ids
    )
    if len(selected_natural) != 15 or len(selected_counterfactuals) != 5:
        raise Phase4P1AnnotationError(
            "formal subset cannot satisfy the frozen quotas",
            safe_stage="P4D_P1_SUBSET",
        )
    return selected_natural, selected_counterfactuals


def formal_selection_implementation_sha256() -> str:
    source = inspect.getsource(_rank_digest) + inspect.getsource(_select_formal_traces)
    return hashlib.sha256(source.encode()).hexdigest()


def _formal_record(trace: Any) -> P1FormalSubsetRecord:
    return P1FormalSubsetRecord(
        trace_id=trace.trace_id,
        trace_kind=trace.trace_kind,
        problem_id=trace.problem_id,
        public_problem_sha256=trace.public_problem_sha256,
        solution_trace_sha256=trace.solution_trace_sha256,
        structured_explanation_sha256=trace.structured_explanation_sha256,
        code_sha256=trace.code_sha256,
        functional_evidence_sha256=trace.functional_evidence.functional_evidence_sha256,
        parent_trace_id=getattr(trace, "parent_trace_id", None),
    )


def prepare_p1_formal_subset(
    *,
    protocol_path: str | Path = P1_PROTOCOL_RELATIVE_PATH,
    cohort_manifest_path: str | Path = (
        "artifacts/experiments/phase3-freezes/phase3_cohort_42_plus_15_v1/manifest.json"
    ),
    natural_manifest_path: str | Path = (
        "artifacts/experiments/phase3-freezes/phase3_natural_42_v1/manifest.json"
    ),
    privacy_canaries: Sequence[str | bytes] = (),
) -> _PreparedFormalSubset:
    _protocol_payload, protocol_value = _load_hashed_json(
        Path(protocol_path), label="P1 protocol", expected_sha256=P1_PROTOCOL_SHA256
    )
    try:
        protocol = P1SecondAnnotatorProtocol.model_validate(protocol_value)
    except ValidationError:
        raise Phase4P1AnnotationError(
            "P1 protocol failed schema validation", safe_stage="P4D_P1_SCHEMA"
        ) from None
    if (
        protocol.formal_subset.subset_id != P1_FORMAL_SUBSET_ID
        or protocol.formal_subset.selection_seed != P1_FORMAL_SUBSET_SEED
        or protocol.formal_subset.selection_algorithm != P1_FORMAL_SELECTION_ALGORITHM
        or protocol.formal_subset.selection_forbidden_inputs != P1_SELECTION_FORBIDDEN_INPUTS
    ):
        raise Phase4P1AnnotationError(
            "P1 formal subset plan differs from the frozen implementation",
            safe_stage="P4D_P1_SUBSET",
        )
    cohort, natural = _load_phase3_manifests(
        cohort_manifest_path=cohort_manifest_path,
        natural_manifest_path=natural_manifest_path,
    )
    if len(natural.traces) != 42 or len(cohort.counterfactuals) != 15:
        raise Phase4P1AnnotationError(
            "Phase-3 source cohort does not contain the frozen 42+15 records",
            safe_stage="P4D_P1_COHORT",
        )
    selected_natural, selected_counterfactuals = _select_formal_traces(
        natural.traces, cohort.counterfactuals
    )
    records = tuple(_formal_record(item) for item in (*selected_natural, *selected_counterfactuals))
    parent_counts = Counter(item.parent_trace_id for item in selected_counterfactuals)
    ordered_trace_ids_sha256 = _ordered_ids_sha256(tuple(item.trace_id for item in records))
    implementation_sha256 = formal_selection_implementation_sha256()
    manifest = P1FormalSubsetManifest(
        selection_forbidden_inputs=P1_SELECTION_FORBIDDEN_INPUTS,
        counterfactual_parent_counts=dict(sorted(parent_counts.items())),
        ordered_trace_ids_sha256=ordered_trace_ids_sha256,
        records=records,
        selection_implementation_sha256=implementation_sha256,
    )
    private_payload = _json_bytes(manifest)
    private_sha256 = hashlib.sha256(private_payload).hexdigest()
    commitment = P1FormalSubsetCommitment(
        selection_forbidden_inputs=P1_SELECTION_FORBIDDEN_INPUTS,
        ordered_trace_ids_sha256=ordered_trace_ids_sha256,
        private_manifest_sha256=private_sha256,
        selection_implementation_sha256=implementation_sha256,
    )
    assert_public_payload_safe(commitment, canaries=privacy_canaries)
    public_payload = _json_bytes(commitment)
    preflight = P1FormalSubsetPreflight(
        manifest=manifest,
        commitment=commitment,
        private_manifest_sha256=private_sha256,
        public_commitment_sha256=hashlib.sha256(public_payload).hexdigest(),
    )
    return _PreparedFormalSubset(
        preflight=preflight,
        private_payload=private_payload,
        public_payload=public_payload,
    )


def preflight_p1_formal_subset(**kwargs: Any) -> P1FormalSubsetPreflight:
    """Regenerate the 20-item selection in memory without writing files."""

    return prepare_p1_formal_subset(**kwargs).preflight


def _write_private_file(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o600)


def freeze_p1_formal_subset(
    *,
    private_manifest_path: str | Path = P1_FORMAL_PRIVATE_MANIFEST_DEFAULT_PATH,
    public_commitment_path: str | Path = P1_FORMAL_PUBLIC_COMMITMENT_DEFAULT_PATH,
    **kwargs: Any,
) -> P1FormalSubsetResult:
    """Write the private manifest and public commitment without overwriting either."""

    prepared = prepare_p1_formal_subset(**kwargs)
    private_path = Path(private_manifest_path).expanduser()
    public_path = Path(public_commitment_path).expanduser()
    for path, label in (
        (private_path, "private formal subset manifest"),
        (public_path, "public formal subset commitment"),
    ):
        if path.exists() or path.is_symlink():
            raise Phase4P1AnnotationError(f"{label} already exists", safe_stage="P4D_P1_OUTPUT")
    _prepare_private_parent(private_path.parent, label="private formal subset manifest")
    if public_path.parent.is_symlink() or (
        public_path.parent.exists() and not public_path.parent.is_dir()
    ):
        raise Phase4P1AnnotationError(
            "public formal subset commitment parent is unsafe",
            safe_stage="P4D_P1_OUTPUT",
        )
    public_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    public_path.parent.chmod(0o755)
    public_written = False
    private_written = False
    try:
        _write_private_file(private_path, prepared.private_payload)
        private_written = True
        _write_public_file(public_path, prepared.public_payload)
        public_written = True
        _fsync_directory(private_path.parent)
        _fsync_directory(public_path.parent)
    except OSError:
        if public_written:
            public_path.unlink(missing_ok=True)
        if private_written:
            private_path.unlink(missing_ok=True)
        raise Phase4P1AnnotationError(
            "cannot freeze P1 formal subset", safe_stage="P4D_P1_OUTPUT"
        ) from None
    return P1FormalSubsetResult(
        **asdict(prepared.preflight),
        private_manifest_path=private_path,
        public_commitment_path=public_path,
    )


def verify_p1_formal_subset(
    *,
    private_manifest_path: str | Path = P1_FORMAL_PRIVATE_MANIFEST_DEFAULT_PATH,
    public_commitment_path: str | Path = P1_FORMAL_PUBLIC_COMMITMENT_DEFAULT_PATH,
    expected_public_commitment_sha256: str | None = None,
    **kwargs: Any,
) -> P1FormalSubsetVerification:
    """Regenerate and compare the private and public frozen artifacts byte-for-byte."""

    private_path = Path(private_manifest_path)
    public_path = Path(public_commitment_path)
    _assert_private_location(private_path, label="private formal subset manifest")
    private_payload = _read_regular_file(private_path, label="private formal subset manifest")
    public_payload = _read_regular_file(public_path, label="public formal subset commitment")
    prepared = prepare_p1_formal_subset(**kwargs)
    if private_payload != prepared.private_payload or public_payload != prepared.public_payload:
        raise Phase4P1AnnotationError(
            "P1 formal subset differs from deterministic regeneration",
            safe_stage="P4D_P1_VERIFY",
        )
    public_sha256 = hashlib.sha256(public_payload).hexdigest()
    if (
        expected_public_commitment_sha256 is not None
        and public_sha256 != expected_public_commitment_sha256
    ):
        raise Phase4P1AnnotationError(
            "P1 public subset commitment SHA256 differs from the expected identity",
            safe_stage="P4D_P1_VERIFY",
        )
    return P1FormalSubsetVerification(
        subset_id=prepared.preflight.commitment.subset_id,
        private_manifest_sha256=hashlib.sha256(private_payload).hexdigest(),
        public_commitment_sha256=public_sha256,
        selected_total_count=prepared.preflight.commitment.selected_total_count,
        verified=True,
    )


__all__ = [
    "P1_DELIVERY_ID",
    "P1_DELIVERY_RECORD_DEFAULT_PATH",
    "P1_DELIVERY_SCHEMA_RELATIVE_PATH",
    "P1_DELIVERY_SCHEMA_SHA256",
    "P1_FORMAL_PRIVATE_MANIFEST_DEFAULT_PATH",
    "P1_FORMAL_PUBLIC_COMMITMENT_DEFAULT_PATH",
    "P1_FORMAL_SELECTION_ALGORITHM",
    "P1_FORMAL_SUBSET_ID",
    "P1_FORMAL_SUBSET_SEED",
    "P1DeliveryRecordPreflight",
    "P1FormalSubsetCommitment",
    "P1FormalSubsetManifest",
    "P1FormalSubsetPreflight",
    "P1FormalSubsetResult",
    "P1FormalSubsetVerification",
    "P1SingleDeliveryRecord",
    "create_p1_delivery_record_template",
    "delivery_record_schema_payload",
    "formal_selection_implementation_sha256",
    "freeze_p1_formal_subset",
    "preflight_p1_delivery_record",
    "preflight_p1_formal_subset",
    "prepare_p1_formal_subset",
    "verify_p1_formal_subset",
]
