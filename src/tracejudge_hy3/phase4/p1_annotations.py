"""P1 second-rater protocol binding and cohort-external practice package generation.

The only executed code comes from one exact SHA256-allowlisted, repository-owned
public Fixture source.  The generated participant package contains no Phase-3
labels, predictions, provider output, hidden evaluation input, or identity map.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, ValidationError, model_validator

from tracejudge_hy3.phase3.contracts import CounterfactualCohortManifest, FrozenCohortManifest
from tracejudge_hy3.phase3.privacy import assert_public_payload_safe, canonical_sha256
from tracejudge_hy3.sandbox.trusted_local import TrustedLocalSandbox
from tracejudge_hy3.schemas.evaluation import ErrorType, FaultyLayer
from tracejudge_hy3.schemas.problem import ProblemSpec
from tracejudge_hy3.schemas.solution import SolutionTrace

from .contracts import Identifier, Phase4Contract, Sha256

P1_ARRANGEMENT_RELATIVE_PATH = "docs/experiments/phase4_p1_second_annotator_arrangement_v1.md"
P1_ARRANGEMENT_SHA256 = "15a0de0efc0b6695b8021f5912ea6670bfc234f686a89cfcf5d267b53e3d7c6b"
P1_PROTOCOL_RELATIVE_PATH = "data/phase4/p1_second_annotator_protocol_v1.json"
P1_PROTOCOL_SHA256 = "3f7268eb757f452d3902de3d60274ce2d45fb022ba047e64bfd5e680b044bf6c"
P1_PRACTICE_SOURCE_RELATIVE_PATH = "data/phase4/p1_public_practice_source_v1.json"
P1_PRACTICE_SOURCE_SHA256 = "f2c99f44a35a821d00da0625b2847ee4628055548886ccce621a381efef744c9"
P1_PRACTICE_ID = "phase4_p1_public_practice_v1"
P1_PRACTICE_RATER_ID = "p1_rater_02"
P1_COORDINATOR_REFERENCE_DEFAULT_PATH = (
    "artifacts/experiments/phase4-p1-annotations/"
    "phase4_p1_public_practice_v1/coordinator_reference.jsonl"
)
P1_COHORT_MANIFEST_SHA256 = "3290221625d687e6d7412a0544247dc81a34857b114a545458b93cc04e35d255"
P1_NATURAL_MANIFEST_SHA256 = "a4116a7ddb7ac910b79bd52e9530db79dd0f05c9edee8ecd947fc78c35c03692"
P1_PHASE3_GUIDE_SHA256 = "0c789671fc926e8286ca7317eae0496efc9f39616783b2c8cbebd678de20beb1"

_MAX_INPUT_BYTES = 32 * 1024 * 1024
_EXPECTED_ITEM_IDS = tuple(f"practice_item_{index:03d}" for index in range(1, 6))
_PARTICIPANT_PACKET_PATH = "participant/packet.jsonl"
_PARTICIPANT_TEMPLATE_PATH = "participant/labels_template.jsonl"


class Phase4P1AnnotationError(ValueError):
    def __init__(self, message: str, *, safe_stage: str = "P4D_P1_PRACTICE") -> None:
        super().__init__(message)
        self.safe_stage = safe_stage


class P1ReferenceAnnotation(Phase4Contract):
    process_correct: bool
    has_error: bool
    reasoning_correct: bool
    plan_code_aligned: bool
    first_faulty_layer: FaultyLayer | None = None
    first_faulty_step: Identifier | None = None
    error_type: ErrorType | None = None
    rationale: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_annotation(self) -> Self:
        if self.process_correct == self.has_error:
            raise ValueError("process_correct must be the complement of has_error")
        if not self.has_error:
            if not self.reasoning_correct or not self.plan_code_aligned:
                raise ValueError("no-error reference must mark reasoning and alignment correct")
            if any(
                value is not None
                for value in (self.first_faulty_layer, self.first_faulty_step, self.error_type)
            ):
                raise ValueError("no-error reference cannot retain fault fields")
        elif self.first_faulty_layer is None or self.error_type is None:
            raise ValueError("error reference requires first layer and error type")
        return self


class P1PracticeSourceItem(Phase4Contract):
    practice_item_id: Identifier
    problem: ProblemSpec
    solution_trace: SolutionTrace

    @model_validator(mode="after")
    def validate_fixture(self) -> Self:
        if self.solution_trace.problem_id != self.problem.problem_id:
            raise ValueError("practice problem and solution trace IDs differ")
        if self.problem.source != "self_constructed_phase4_p1_public_fixture":
            raise ValueError("practice item is not an approved self-constructed public Fixture")
        if self.problem.hidden_test_cases:
            raise ValueError("practice Fixture may not contain hidden test cases")
        if not self.problem.visible_test_cases or not self.problem.challenge_test_cases:
            raise ValueError("practice Fixture requires visible and challenge public cases")
        return self


class P1PracticeSource(Phase4Contract):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase4_p1_public_practice_source"]
    source_id: Literal["phase4_p1_public_practice_source_v1"]
    license: Literal["MIT"]
    authorship: Literal["TraceJudge-Hy3 self-constructed public fixtures"]
    frozen_phase3_cohort_manifest_sha256: Literal[P1_COHORT_MANIFEST_SHA256]
    frozen_phase3_natural_manifest_sha256: Literal[P1_NATURAL_MANIFEST_SHA256]
    selection_role: Literal["cohort_external_calibration_only"]
    items: tuple[P1PracticeSourceItem, ...]

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        if tuple(item.practice_item_id for item in self.items) != _EXPECTED_ITEM_IDS:
            raise ValueError("practice source must contain the five frozen item IDs in order")
        problem_ids = [item.problem.problem_id for item in self.items]
        if len(problem_ids) != len(set(problem_ids)):
            raise ValueError("practice problem IDs must be unique")
        return self


class P1FormalSubsetPlan(Phase4Contract):
    subset_id: Literal["phase4_p1_formal_subset_v1"]
    subset_status: Literal["frozen"]
    selection_seed: Literal[20260902]
    selection_algorithm: Literal[
        "sha256_seeded_rank_v1:natural_top15;"
        "counterfactual_parent_first_then_global_fill_max2;source_order_output"
    ]
    target_item_count: Literal[20]
    natural_item_count: Literal[15]
    counterfactual_item_count: Literal[5]
    counterfactual_parent_minimum: Literal[3]
    counterfactual_per_parent_maximum: Literal[2]
    selection_forbidden_inputs: tuple[
        Literal[
            "primary_rater_labels",
            "method_predictions",
            "provider_status",
            "post_hoc_results",
        ],
        ...,
    ]
    freeze_before_practice_response_review: Literal[True]

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        expected = (
            "primary_rater_labels",
            "method_predictions",
            "provider_status",
            "post_hoc_results",
        )
        if self.selection_forbidden_inputs != expected:
            raise ValueError("formal subset forbidden inputs differ from the frozen policy")
        return self


class P1AdmissionRule(Phase4Contract):
    schema_valid_count_required: Literal[5]
    privacy_or_blinding_violation_count_required: Literal[0]
    has_error_exact_agreement_required: Literal[4]
    process_correct_exact_agreement_required: Literal[4]
    error_item_first_faulty_layer_exact_agreement_required: Literal[2]
    error_item_first_faulty_layer_denominator: Literal[3]
    public_evidence_only_rationale_required: Literal[True]
    coordinator_written_authorization_required: Literal[True]


class P1PracticePlan(Phase4Contract):
    source_id: Literal["phase4_p1_public_practice_source_v1"]
    practice_id: Literal[P1_PRACTICE_ID]
    item_count: Literal[5]
    clean_reference_count: Literal[2]
    error_reference_count: Literal[3]
    cohort_overlap_required: Literal[0]
    coordinator_reference_storage: Literal["git_ignored_private_artifact"]
    public_manifest_excludes_reference_content_and_private_path: Literal[True]
    maximum_calibration_rounds_total: Literal[2]
    admission: P1AdmissionRule


class P1RelativeDeadlines(Phase4Contract):
    practice_calendar_days_after_verified_receipt: Literal[3]
    coordinator_feedback_business_days: Literal[2]
    formal_calendar_days_after_written_admission_and_verified_receipt: Literal[10]
    coordinator_wait_extension_threshold_hours: Literal[24]
    coordinator_wait_time_extends_deadline_one_for_one: Literal[True]


class P1TransportPolicy(Phase4Contract):
    encrypted_archive_minimum: Literal["AES-256"]
    authenticated_one_to_one_file_channel_required: Literal[True]
    password_via_separate_realtime_voice_or_in_person_channel: Literal[True]
    exact_channels_recorded_before_delivery: Literal[True]
    project_owner_per_delivery_upload_authorization_required: Literal[True]
    local_non_sync_storage_only: Literal[True]
    full_disk_encryption_or_equivalent_required: Literal[True]
    sha256_verification_before_opening_required: Literal[True]


class P1BlindingPolicy(Phase4Contract):
    blinded_to_primary_labels: Literal[True]
    blinded_to_method_predictions: Literal[True]
    blinded_to_other_raters: Literal[True]
    active_project_search_forbidden: Literal[True]
    item_discussion_with_others_forbidden: Literal[True]
    external_ai_or_execution_forbidden: Literal[True]
    disagreement_discussion_before_raw_freeze_forbidden: Literal[True]


class P1AgreementAnalysisPolicy(Phase4Contract):
    raw_agreement_required: Literal[True]
    confusion_counts_required: Literal[True]
    cohen_kappa_policy: Literal[
        "report_only_when_both_classes_present_and_expected_agreement_defined"
    ]
    adjudication_overwrites_original_labels: Literal[False]
    exploratory_only: Literal[True]


class P1EthicsDetermination(Phase4Contract):
    decision: Literal["approved"]
    confirmed_on: Literal["2026-09-02"]
    verifier_role: Literal["supervising_advisor"]
    record_storage: Literal["private_restricted_location"]
    participant_consent_requirements_confirmed: Literal[True]
    data_management_requirements_confirmed: Literal[True]


class P1SecondAnnotatorProtocol(Phase4Contract):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase4_p1_second_annotator_protocol"]
    protocol_id: Literal["phase4_p1_second_annotator_protocol_v1"]
    arrangement_relative_path: Literal[P1_ARRANGEMENT_RELATIVE_PATH]
    arrangement_sha256: Literal[P1_ARRANGEMENT_SHA256]
    phase3_annotation_guide_relative_path: Literal["docs/experiments/phase3_annotation_guide_v1.md"]
    phase3_annotation_guide_sha256: Literal[P1_PHASE3_GUIDE_SHA256]
    ethics_status: Literal["READY"]
    ethics_determination: P1EthicsDetermination
    delivery_record_status: Literal["pending_completion"]
    data_collection_allowed: Literal[False]
    formal_subset: P1FormalSubsetPlan
    practice: P1PracticePlan
    relative_deadlines: P1RelativeDeadlines
    transport: P1TransportPolicy
    blinding: P1BlindingPolicy
    incident_rule: Literal["stop_preserve_report_wait_for_written_instruction"]
    withdrawal_allowed_before_freeze: Literal[True]
    partial_labels_used_after_pre_freeze_withdrawal: Literal[False]
    participant_copy_deletion_hours_after_receipt_or_withdrawal: Literal[24]
    analysis: P1AgreementAnalysisPolicy
    formal_packet_created: Literal[False]
    formal_data_collected: Literal[False]


class P1PracticeTask(Phase4Contract):
    practice_item_id: Identifier
    problem_id: Identifier
    code_sha256: Sha256
    structured_explanation_sha256: Sha256
    functional_evidence_sha256: Sha256
    public_problem: dict[str, Any]
    structured_solution_trace: dict[str, Any]
    candidate_code: str = Field(min_length=1)
    functional_evidence: dict[str, Any]
    public_dynamic_evidence: dict[str, Any]


class P1PracticeDraftRecord(Phase4Contract):
    practice_item_id: Identifier
    annotation_protocol_sha256: Literal[P1_PROTOCOL_SHA256] = P1_PROTOCOL_SHA256
    rater_id: Literal[P1_PRACTICE_RATER_ID] = P1_PRACTICE_RATER_ID
    calibration_round: Literal[1] = 1
    blinded_to_primary_labels: Literal[True] = True
    blinded_to_method_predictions: Literal[True] = True
    status: Literal["pending"] = "pending"
    process_correct: None = None
    has_error: None = None
    reasoning_correct: None = None
    plan_code_aligned: None = None
    first_faulty_layer: None = None
    first_faulty_step: None = None
    error_type: None = None
    rationale: None = None


class P1PracticeReferenceRecord(Phase4Contract):
    practice_item_id: Identifier
    problem_id: Identifier
    code_sha256: Sha256
    structured_explanation_sha256: Sha256
    functional_evidence_sha256: Sha256
    reference_annotation: P1ReferenceAnnotation
    reference_role: Literal["public_fixture_calibration_reference"] = (
        "public_fixture_calibration_reference"
    )
    is_human_participant_annotation: Literal[False] = False


class P1PracticeManifest(Phase4Contract):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase4_p1_public_practice_bundle"] = (
        "tracejudge_phase4_p1_public_practice_bundle"
    )
    practice_id: Literal[P1_PRACTICE_ID]
    status: Literal["frozen"] = "frozen"
    source_id: Literal["phase4_p1_public_practice_source_v1"]
    source_relative_path: Literal[P1_PRACTICE_SOURCE_RELATIVE_PATH] = (
        P1_PRACTICE_SOURCE_RELATIVE_PATH
    )
    source_sha256: Literal[P1_PRACTICE_SOURCE_SHA256] = P1_PRACTICE_SOURCE_SHA256
    protocol_id: Literal["phase4_p1_second_annotator_protocol_v1"]
    protocol_relative_path: Literal[P1_PROTOCOL_RELATIVE_PATH] = P1_PROTOCOL_RELATIVE_PATH
    protocol_sha256: Literal[P1_PROTOCOL_SHA256] = P1_PROTOCOL_SHA256
    arrangement_relative_path: Literal[P1_ARRANGEMENT_RELATIVE_PATH] = P1_ARRANGEMENT_RELATIVE_PATH
    arrangement_sha256: Literal[P1_ARRANGEMENT_SHA256] = P1_ARRANGEMENT_SHA256
    phase3_annotation_guide_sha256: Literal[P1_PHASE3_GUIDE_SHA256] = P1_PHASE3_GUIDE_SHA256
    phase3_cohort_manifest_sha256: Literal[P1_COHORT_MANIFEST_SHA256] = P1_COHORT_MANIFEST_SHA256
    phase3_natural_manifest_sha256: Literal[P1_NATURAL_MANIFEST_SHA256] = P1_NATURAL_MANIFEST_SHA256
    cohort_overlap_count: Literal[0] = 0
    item_count: Literal[5] = 5
    clean_reference_count: Literal[2] = 2
    error_reference_count: Literal[3] = 3
    executed_public_case_count: int = Field(ge=1)
    ordered_practice_item_ids: tuple[Identifier, ...]
    participant_packet_path: Literal[_PARTICIPANT_PACKET_PATH] = _PARTICIPANT_PACKET_PATH
    participant_packet_sha256: Sha256
    participant_labels_template_path: Literal[_PARTICIPANT_TEMPLATE_PATH] = (
        _PARTICIPANT_TEMPLATE_PATH
    )
    participant_labels_template_sha256: Sha256
    coordinator_reference_artifact_id: Literal[
        "phase4_p1_public_practice_v1_coordinator_reference"
    ] = "phase4_p1_public_practice_v1_coordinator_reference"
    coordinator_reference_sha256: Sha256
    coordinator_reference_storage: Literal["git_ignored_private_artifact"] = (
        "git_ignored_private_artifact"
    )
    generator_implementation_sha256: Sha256
    participant_distribution_excludes_coordinator_reference: Literal[True] = True
    reference_is_public_fixture_not_human_annotation: Literal[True] = True
    exact_source_allowlist: Literal[True] = True
    sandbox_backend: Literal["trusted-local"] = "trusted-local"
    provider_call_count: Literal[0] = 0
    docker_call_count: Literal[0] = 0
    network_call_count: Literal[0] = 0
    contains_phase3_annotation_records: Literal[False] = False
    contains_method_predictions: Literal[False] = False
    contains_provider_raw: Literal[False] = False
    contains_hidden_evaluation_content: Literal[False] = False
    formal_packet_created: Literal[False] = False
    formal_data_collected: Literal[False] = False
    ethics_status: Literal["READY"] = "READY"
    delivery_record_status: Literal["pending_completion"] = "pending_completion"

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        if self.ordered_practice_item_ids != _EXPECTED_ITEM_IDS:
            raise ValueError("practice manifest item order differs from the frozen source")
        return self


@dataclass(frozen=True, slots=True)
class P1PracticePreflight:
    manifest: P1PracticeManifest
    manifest_sha256: str
    participant_packet_sha256: str
    participant_labels_template_sha256: str
    coordinator_reference_sha256: str


@dataclass(frozen=True, slots=True)
class P1PracticeResult(P1PracticePreflight):
    bundle_dir: Path
    manifest_path: Path
    participant_packet_path: Path
    participant_labels_template_path: Path


@dataclass(frozen=True, slots=True)
class P1PracticeVerification:
    practice_id: str
    item_count: int
    executed_public_case_count: int
    manifest_sha256: str
    verified: bool


@dataclass(frozen=True, slots=True)
class _PreparedPractice:
    preflight: P1PracticePreflight
    manifest_payload: bytes
    packet_payload: bytes
    template_payload: bytes


class _DuplicateJsonKey(ValueError):
    pass


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey
        value[key] = item
    return value


def _reject_constant(_value: str) -> None:
    raise ValueError


def _read_regular_file(path: Path, *, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise Phase4P1AnnotationError(
            f"{label} must be a regular non-symlink file",
            safe_stage="P4D_P1_INPUT",
        )
    try:
        if path.stat().st_size > _MAX_INPUT_BYTES:
            raise Phase4P1AnnotationError(f"{label} exceeds the size limit")
        payload = path.read_bytes()
    except OSError:
        raise Phase4P1AnnotationError(f"cannot read {label}") from None
    if len(payload) > _MAX_INPUT_BYTES:
        raise Phase4P1AnnotationError(f"{label} exceeds the size limit")
    return payload


def _decode_json(payload: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKey, ValueError):
        raise Phase4P1AnnotationError(f"{label} is not strict UTF-8 JSON") from None
    if not isinstance(value, dict):
        raise Phase4P1AnnotationError(f"{label} must contain one JSON object")
    return value


def _load_hashed_json(
    path: Path,
    *,
    label: str,
    expected_sha256: str,
) -> tuple[bytes, dict[str, Any]]:
    payload = _read_regular_file(path, label=label)
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise Phase4P1AnnotationError(
            f"{label} differs from the frozen identity",
            safe_stage="P4D_P1_IDENTITY",
        )
    return payload, _decode_json(payload, label=label)


def _load_private_references(
    path: Path,
    *,
    source: P1PracticeSource,
) -> tuple[bytes, tuple[P1PracticeReferenceRecord, ...]]:
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise Phase4P1AnnotationError(
            "P1 coordinator reference parent must be a private non-symlink directory",
            safe_stage="P4D_P1_PRIVACY",
        )
    payload = _read_regular_file(path, label="P1 coordinator reference")
    try:
        if stat.S_IMODE(parent.stat().st_mode) & 0o077:
            raise Phase4P1AnnotationError(
                "P1 coordinator reference parent permissions are too broad",
                safe_stage="P4D_P1_PRIVACY",
            )
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise Phase4P1AnnotationError(
                "P1 coordinator reference permissions are too broad",
                safe_stage="P4D_P1_PRIVACY",
            )
    except OSError:
        raise Phase4P1AnnotationError("cannot inspect P1 coordinator reference") from None
    lines = payload.splitlines()
    if len(lines) != 5 or any(not line for line in lines):
        raise Phase4P1AnnotationError(
            "P1 coordinator reference must contain five JSONL records",
            safe_stage="P4D_P1_REFERENCE",
        )
    try:
        references = tuple(
            P1PracticeReferenceRecord.model_validate(
                _decode_json(line, label="P1 coordinator reference row")
            )
            for line in lines
        )
    except ValidationError:
        raise Phase4P1AnnotationError(
            "P1 coordinator reference failed schema validation",
            safe_stage="P4D_P1_REFERENCE",
        ) from None
    if tuple(item.practice_item_id for item in references) != _EXPECTED_ITEM_IDS:
        raise Phase4P1AnnotationError(
            "P1 coordinator reference order differs from the frozen practice order",
            safe_stage="P4D_P1_REFERENCE",
        )
    source_by_id = {item.practice_item_id: item for item in source.items}
    for reference in references:
        source_item = source_by_id[reference.practice_item_id]
        if reference.problem_id != source_item.problem.problem_id:
            raise Phase4P1AnnotationError(
                "P1 coordinator reference problem identity differs from the public source",
                safe_stage="P4D_P1_REFERENCE",
            )
        step_ids = {step.step_id for step in source_item.solution_trace.implementation_steps}
        if (
            reference.reference_annotation.first_faulty_step is not None
            and reference.reference_annotation.first_faulty_step not in step_ids
        ):
            raise Phase4P1AnnotationError(
                "P1 coordinator reference cites an unknown solution step",
                safe_stage="P4D_P1_REFERENCE",
            )
    error_references = [item for item in references if item.reference_annotation.has_error]
    clean_references = [item for item in references if not item.reference_annotation.has_error]
    if len(error_references) != 3 or len(clean_references) != 2:
        raise Phase4P1AnnotationError(
            "P1 coordinator reference must contain three error and two clean items",
            safe_stage="P4D_P1_REFERENCE",
        )
    if tuple(item.reference_annotation.first_faulty_layer for item in error_references) != (
        "alignment",
        "reasoning",
        "requirement",
    ):
        raise Phase4P1AnnotationError(
            "P1 coordinator reference differs from the frozen fault-layer calibration",
            safe_stage="P4D_P1_REFERENCE",
        )
    return payload, references


def _jsonl_bytes(values: Sequence[Phase4Contract]) -> bytes:
    return b"".join(
        json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
        for value in values
    )


def _json_bytes(value: Phase4Contract) -> bytes:
    return (
        json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _structured_solution_trace(solution: SolutionTrace) -> dict[str, Any]:
    payload = solution.model_dump(mode="json")
    payload.pop("code")
    return payload


def _public_problem(problem: ProblemSpec) -> dict[str, Any]:
    payload = problem.model_dump(mode="json")
    payload.pop("reference_code")
    payload.pop("hidden_test_cases")
    return payload


def _implementation_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _load_protocol_and_source(
    *,
    arrangement_path: str | Path,
    protocol_path: str | Path,
    phase3_guide_path: str | Path,
    source_path: str | Path,
) -> tuple[P1SecondAnnotatorProtocol, P1PracticeSource]:
    arrangement_payload = _read_regular_file(Path(arrangement_path), label="P1 arrangement")
    if hashlib.sha256(arrangement_payload).hexdigest() != P1_ARRANGEMENT_SHA256:
        raise Phase4P1AnnotationError(
            "P1 arrangement differs from the frozen identity",
            safe_stage="P4D_P1_IDENTITY",
        )
    guide_payload = _read_regular_file(Path(phase3_guide_path), label="Phase-3 annotation guide")
    if hashlib.sha256(guide_payload).hexdigest() != P1_PHASE3_GUIDE_SHA256:
        raise Phase4P1AnnotationError(
            "Phase-3 annotation guide differs from the frozen identity",
            safe_stage="P4D_P1_IDENTITY",
        )
    _protocol_payload, protocol_value = _load_hashed_json(
        Path(protocol_path),
        label="P1 protocol",
        expected_sha256=P1_PROTOCOL_SHA256,
    )
    _source_payload, source_value = _load_hashed_json(
        Path(source_path),
        label="P1 public practice source",
        expected_sha256=P1_PRACTICE_SOURCE_SHA256,
    )
    try:
        protocol = P1SecondAnnotatorProtocol.model_validate(protocol_value)
        source = P1PracticeSource.model_validate(source_value)
    except ValidationError:
        raise Phase4P1AnnotationError(
            "P1 protocol or practice source failed schema validation",
            safe_stage="P4D_P1_SCHEMA",
        ) from None
    return protocol, source


def _load_phase3_manifests(
    *,
    cohort_manifest_path: str | Path,
    natural_manifest_path: str | Path,
) -> tuple[CounterfactualCohortManifest, FrozenCohortManifest]:
    _cohort_payload, cohort_value = _load_hashed_json(
        Path(cohort_manifest_path),
        label="Phase-3 cohort manifest",
        expected_sha256=P1_COHORT_MANIFEST_SHA256,
    )
    _natural_payload, natural_value = _load_hashed_json(
        Path(natural_manifest_path),
        label="Phase-3 natural manifest",
        expected_sha256=P1_NATURAL_MANIFEST_SHA256,
    )
    try:
        cohort = CounterfactualCohortManifest.model_validate(cohort_value)
        natural = FrozenCohortManifest.model_validate(natural_value)
    except ValidationError:
        raise Phase4P1AnnotationError(
            "Phase-3 cohort identity failed schema validation",
            safe_stage="P4D_P1_COHORT",
        ) from None
    if cohort.natural_cohort.manifest_sha256 != P1_NATURAL_MANIFEST_SHA256:
        raise Phase4P1AnnotationError(
            "Phase-3 overlay references a different natural cohort",
            safe_stage="P4D_P1_COHORT",
        )
    return cohort, natural


def _assert_cohort_external(
    source: P1PracticeSource,
    *,
    cohort: CounterfactualCohortManifest,
    natural: FrozenCohortManifest,
) -> None:
    cohort_problem_ids = {trace.problem_id for trace in natural.traces}
    cohort_problem_ids.update(parent.problem_id for parent in cohort.parents)
    cohort_problem_ids.update(trace.problem_id for trace in cohort.counterfactuals)
    cohort_code_hashes = {trace.code_sha256 for trace in natural.traces}
    cohort_code_hashes.update(parent.code_sha256 for parent in cohort.parents)
    cohort_code_hashes.update(trace.code_sha256 for trace in cohort.counterfactuals)
    cohort_explanation_hashes = {trace.structured_explanation_sha256 for trace in natural.traces}
    cohort_explanation_hashes.update(
        parent.structured_explanation_sha256 for parent in cohort.parents
    )
    cohort_explanation_hashes.update(
        trace.structured_explanation_sha256 for trace in cohort.counterfactuals
    )
    for item in source.items:
        code_sha256 = hashlib.sha256(item.solution_trace.code.encode("utf-8")).hexdigest()
        explanation_sha256 = canonical_sha256(_structured_solution_trace(item.solution_trace))
        if (
            item.problem.problem_id in cohort_problem_ids
            or code_sha256 in cohort_code_hashes
            or explanation_sha256 in cohort_explanation_hashes
        ):
            raise Phase4P1AnnotationError(
                "public practice Fixture overlaps the frozen Phase-3 cohort",
                safe_stage="P4D_P1_COHORT",
            )


def _execute_item(
    item: P1PracticeSourceItem,
    *,
    reference: P1PracticeReferenceRecord,
    timeout_seconds: float,
) -> tuple[dict[str, Any], dict[str, Any], int]:
    cases = [*item.problem.visible_test_cases, *item.problem.challenge_test_cases]
    sandbox = TrustedLocalSandbox(per_test_timeout_seconds=timeout_seconds)
    summary = sandbox.run(
        item.solution_trace.code,
        item.problem.function_name,
        cases,
    )
    if summary.runtime_status != "completed" or len(summary.results) != len(cases):
        raise Phase4P1AnnotationError(
            "allowlisted public practice execution failed",
            safe_stage="P4D_P1_EXECUTION",
        )
    safe_results: list[dict[str, Any]] = []
    for case, result in zip(cases, summary.results, strict=True):
        if result.timed_out:
            raise Phase4P1AnnotationError(
                "allowlisted public practice case timed out",
                safe_stage="P4D_P1_EXECUTION",
            )
        safe_results.append(
            {
                "case_id": result.case_id,
                "category": result.category,
                "passed": result.passed,
                "expected_output": result.expected_output,
                "actual_output": result.actual_output,
                "exception_type": result.exception_type,
                "timed_out": result.timed_out,
                "related_requirements": case.related_requirements,
            }
        )
    pass_count = sum(result["passed"] for result in safe_results)
    execution_status = "pass" if pass_count == len(safe_results) else "fail"
    expected_error = reference.reference_annotation.has_error
    if (execution_status == "pass") == expected_error:
        raise Phase4P1AnnotationError(
            "practice execution status disagrees with the frozen reference role",
            safe_stage="P4D_P1_REFERENCE",
        )
    code_sha256 = hashlib.sha256(item.solution_trace.code.encode("utf-8")).hexdigest()
    functional_evidence = {
        "kind": "public_fixture_calibration_status",
        "practice_id": P1_PRACTICE_ID,
        "problem_id": item.problem.problem_id,
        "code_sha256": code_sha256,
        "execution_status": execution_status,
        "case_count": len(safe_results),
        "pass_count": pass_count,
        "fail_count": len(safe_results) - pass_count,
        "publicly_replayable": True,
    }
    dynamic_evidence = {
        "policy_version": "phase4_p1_public_practice_execution_v1",
        "availability": "available",
        "source": "exact_sha256_allowlisted_self_constructed_fixture",
        "problem_id": item.problem.problem_id,
        "code_sha256": code_sha256,
        "execution_status": execution_status,
        "case_count": len(safe_results),
        "pass_count": pass_count,
        "fail_count": len(safe_results) - pass_count,
        "case_results": safe_results,
    }
    assert_public_payload_safe(functional_evidence)
    assert_public_payload_safe(dynamic_evidence)
    return functional_evidence, dynamic_evidence, len(safe_results)


def prepare_p1_practice_bundle(
    *,
    arrangement_path: str | Path = P1_ARRANGEMENT_RELATIVE_PATH,
    protocol_path: str | Path = P1_PROTOCOL_RELATIVE_PATH,
    phase3_guide_path: str | Path = "docs/experiments/phase3_annotation_guide_v1.md",
    source_path: str | Path = P1_PRACTICE_SOURCE_RELATIVE_PATH,
    coordinator_reference_path: str | Path = P1_COORDINATOR_REFERENCE_DEFAULT_PATH,
    cohort_manifest_path: str | Path = (
        "artifacts/experiments/phase3-freezes/phase3_cohort_42_plus_15_v1/manifest.json"
    ),
    natural_manifest_path: str | Path = (
        "artifacts/experiments/phase3-freezes/phase3_natural_42_v1/manifest.json"
    ),
    timeout_seconds: float = 2.0,
    privacy_canaries: Sequence[str | bytes] = (),
) -> _PreparedPractice:
    """Validate identities and execute only the five allowlisted public Fixture items."""

    if timeout_seconds <= 0 or timeout_seconds > 10:
        raise Phase4P1AnnotationError(
            "practice timeout must be in (0, 10] seconds",
            safe_stage="P4D_P1_INPUT",
        )
    protocol, source = _load_protocol_and_source(
        arrangement_path=arrangement_path,
        protocol_path=protocol_path,
        phase3_guide_path=phase3_guide_path,
        source_path=source_path,
    )
    reference_payload, references = _load_private_references(
        Path(coordinator_reference_path),
        source=source,
    )
    cohort, natural = _load_phase3_manifests(
        cohort_manifest_path=cohort_manifest_path,
        natural_manifest_path=natural_manifest_path,
    )
    _assert_cohort_external(source, cohort=cohort, natural=natural)

    tasks: list[P1PracticeTask] = []
    drafts: list[P1PracticeDraftRecord] = []
    references_by_id = {item.practice_item_id: item for item in references}
    executed_case_count = 0
    for item in source.items:
        reference = references_by_id[item.practice_item_id]
        functional, dynamic, case_count = _execute_item(
            item,
            reference=reference,
            timeout_seconds=timeout_seconds,
        )
        executed_case_count += case_count
        code_sha256 = hashlib.sha256(item.solution_trace.code.encode("utf-8")).hexdigest()
        structured = _structured_solution_trace(item.solution_trace)
        explanation_sha256 = canonical_sha256(structured)
        functional_sha256 = canonical_sha256(functional)
        if (
            reference.code_sha256 != code_sha256
            or reference.structured_explanation_sha256 != explanation_sha256
            or reference.functional_evidence_sha256 != functional_sha256
        ):
            raise Phase4P1AnnotationError(
                "P1 coordinator reference hashes differ from regenerated public materials",
                safe_stage="P4D_P1_REFERENCE",
            )
        task = P1PracticeTask(
            practice_item_id=item.practice_item_id,
            problem_id=item.problem.problem_id,
            code_sha256=code_sha256,
            structured_explanation_sha256=explanation_sha256,
            functional_evidence_sha256=functional_sha256,
            public_problem=_public_problem(item.problem),
            structured_solution_trace=structured,
            candidate_code=item.solution_trace.code,
            functional_evidence=functional,
            public_dynamic_evidence=dynamic,
        )
        draft = P1PracticeDraftRecord(practice_item_id=item.practice_item_id)
        assert_public_payload_safe(task, canaries=privacy_canaries)
        assert_public_payload_safe(draft, canaries=privacy_canaries)
        tasks.append(task)
        drafts.append(draft)

    packet_payload = _jsonl_bytes(tasks)
    template_payload = _jsonl_bytes(drafts)
    manifest = P1PracticeManifest(
        practice_id=protocol.practice.practice_id,
        source_id=source.source_id,
        protocol_id=protocol.protocol_id,
        executed_public_case_count=executed_case_count,
        ordered_practice_item_ids=tuple(item.practice_item_id for item in source.items),
        participant_packet_sha256=hashlib.sha256(packet_payload).hexdigest(),
        participant_labels_template_sha256=hashlib.sha256(template_payload).hexdigest(),
        coordinator_reference_sha256=hashlib.sha256(reference_payload).hexdigest(),
        generator_implementation_sha256=_implementation_sha256(),
    )
    assert_public_payload_safe(manifest, canaries=privacy_canaries)
    manifest_payload = _json_bytes(manifest)
    preflight = P1PracticePreflight(
        manifest=manifest,
        manifest_sha256=hashlib.sha256(manifest_payload).hexdigest(),
        participant_packet_sha256=manifest.participant_packet_sha256,
        participant_labels_template_sha256=manifest.participant_labels_template_sha256,
        coordinator_reference_sha256=manifest.coordinator_reference_sha256,
    )
    return _PreparedPractice(
        preflight=preflight,
        manifest_payload=manifest_payload,
        packet_payload=packet_payload,
        template_payload=template_payload,
    )


def preflight_p1_practice_bundle(**kwargs: Any) -> P1PracticePreflight:
    """Build the practice package in memory without writing files."""

    return prepare_p1_practice_bundle(**kwargs).preflight


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_public_file(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o644)


def write_p1_practice_bundle(
    *,
    output_dir: str | Path = "docs/experiments/phase4_p1_practice",
    **kwargs: Any,
) -> P1PracticeResult:
    """Atomically write the public practice package without overwriting a prior freeze."""

    prepared = prepare_p1_practice_bundle(**kwargs)
    output_root = Path(output_dir).expanduser()
    if output_root.is_symlink() or (output_root.exists() and not output_root.is_dir()):
        raise Phase4P1AnnotationError(
            "practice output root is unsafe",
            safe_stage="P4D_P1_OUTPUT",
        )
    output_root = output_root.resolve()
    bundle_dir = output_root / prepared.preflight.manifest.practice_id
    if bundle_dir.exists() or bundle_dir.is_symlink():
        raise Phase4P1AnnotationError(
            "practice bundle directory already exists",
            safe_stage="P4D_P1_OUTPUT",
        )
    output_root.mkdir(parents=True, exist_ok=True, mode=0o755)
    temporary_dir: Path | None = None
    try:
        temporary_dir = Path(
            tempfile.mkdtemp(
                prefix=f".{prepared.preflight.manifest.practice_id}.",
                dir=output_root,
            )
        )
        temporary_dir.chmod(0o755)
        _write_public_file(temporary_dir / "manifest.json", prepared.manifest_payload)
        _write_public_file(
            temporary_dir / _PARTICIPANT_PACKET_PATH,
            prepared.packet_payload,
        )
        _write_public_file(
            temporary_dir / _PARTICIPANT_TEMPLATE_PATH,
            prepared.template_payload,
        )
        os.replace(temporary_dir, bundle_dir)
        temporary_dir = None
        _fsync_directory(output_root)
    except OSError:
        raise Phase4P1AnnotationError(
            "cannot atomically publish the practice bundle",
            safe_stage="P4D_P1_OUTPUT",
        ) from None
    finally:
        if temporary_dir is not None:
            shutil.rmtree(temporary_dir, ignore_errors=True)

    return P1PracticeResult(
        **asdict(prepared.preflight),
        bundle_dir=bundle_dir,
        manifest_path=bundle_dir / "manifest.json",
        participant_packet_path=bundle_dir / _PARTICIPANT_PACKET_PATH,
        participant_labels_template_path=bundle_dir / _PARTICIPANT_TEMPLATE_PATH,
    )


def verify_p1_practice_bundle(
    *,
    manifest_path: str | Path,
    expected_manifest_sha256: str | None = None,
    **kwargs: Any,
) -> P1PracticeVerification:
    """Regenerate the exact bundle and compare every tracked byte."""

    manifest_path = Path(manifest_path)
    manifest_payload = _read_regular_file(manifest_path, label="P1 practice manifest")
    actual_manifest_sha256 = hashlib.sha256(manifest_payload).hexdigest()
    if expected_manifest_sha256 is not None and actual_manifest_sha256 != expected_manifest_sha256:
        raise Phase4P1AnnotationError(
            "practice manifest SHA256 differs from the expected identity",
            safe_stage="P4D_P1_VERIFY",
        )
    try:
        stored_manifest = P1PracticeManifest.model_validate(
            _decode_json(manifest_payload, label="P1 practice manifest")
        )
    except ValidationError:
        raise Phase4P1AnnotationError(
            "practice manifest failed schema validation",
            safe_stage="P4D_P1_VERIFY",
        ) from None
    prepared = prepare_p1_practice_bundle(**kwargs)
    if manifest_payload != prepared.manifest_payload:
        raise Phase4P1AnnotationError(
            "practice manifest differs from deterministic regeneration",
            safe_stage="P4D_P1_VERIFY",
        )
    bundle_dir = manifest_path.parent
    comparisons = (
        (stored_manifest.participant_packet_path, prepared.packet_payload),
        (stored_manifest.participant_labels_template_path, prepared.template_payload),
    )
    for relative_path, expected_payload in comparisons:
        actual_payload = _read_regular_file(
            bundle_dir / relative_path,
            label="P1 practice bundle artifact",
        )
        if actual_payload != expected_payload:
            raise Phase4P1AnnotationError(
                "practice bundle artifact differs from deterministic regeneration",
                safe_stage="P4D_P1_VERIFY",
            )
    return P1PracticeVerification(
        practice_id=stored_manifest.practice_id,
        item_count=stored_manifest.item_count,
        executed_public_case_count=stored_manifest.executed_public_case_count,
        manifest_sha256=actual_manifest_sha256,
        verified=True,
    )


__all__ = [
    "P1_ARRANGEMENT_RELATIVE_PATH",
    "P1_ARRANGEMENT_SHA256",
    "P1_COHORT_MANIFEST_SHA256",
    "P1_COORDINATOR_REFERENCE_DEFAULT_PATH",
    "P1_NATURAL_MANIFEST_SHA256",
    "P1_PRACTICE_ID",
    "P1_PRACTICE_SOURCE_RELATIVE_PATH",
    "P1_PRACTICE_SOURCE_SHA256",
    "P1_PROTOCOL_RELATIVE_PATH",
    "P1_PROTOCOL_SHA256",
    "P1PracticeManifest",
    "P1PracticePreflight",
    "P1PracticeResult",
    "P1PracticeVerification",
    "P1EthicsDetermination",
    "P1ReferenceAnnotation",
    "P1SecondAnnotatorProtocol",
    "Phase4P1AnnotationError",
    "prepare_p1_practice_bundle",
    "preflight_p1_practice_bundle",
    "verify_p1_practice_bundle",
    "write_p1_practice_bundle",
]
