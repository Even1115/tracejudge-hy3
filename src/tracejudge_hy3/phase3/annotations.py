"""Gate-E frozen annotation protocol and blinded packet generation."""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, ValidationError, model_validator

from tracejudge_hy3.schemas.solution import SolutionTrace

from .contracts import Identifier, Sha256, StrictFrozenModel
from .materials import LoadedPhase3Materials, load_phase3_materials
from .privacy import assert_public_payload_safe
from .runner import Phase3RunnerError, functional_evidence_payload

ANNOTATION_PROTOCOL_RELATIVE_PATH = "phase3/annotation_protocol_v1.json"
ANNOTATION_PROTOCOL_SHA256 = "a2d77ae20102364170a6391c544437601c6e5871e86b9a01f64ad9492556ea85"
ANNOTATION_GUIDE_RELATIVE_PATH = "docs/experiments/phase3_annotation_guide_v1.md"
ANNOTATION_GUIDE_SHA256 = "0c789671fc926e8286ca7317eae0496efc9f39616783b2c8cbebd678de20beb1"

_MAX_INPUT_BYTES = 128 * 1024 * 1024
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VISIBLE_MATERIALS = (
    "public_problem",
    "structured_solution_trace",
    "candidate_code",
    "functional_evidence",
    "public_dynamic_evidence",
)
_FORBIDDEN_MATERIALS = (
    "method_predictions",
    "provider_raw",
    "other_rater_labels",
    "counterfactual_mutation",
    "counterfactual_expected_impact",
    "canonical_solution",
    "official_test_inputs",
    "official_failure_inputs",
    "evalplus_raw",
    "credentials",
)
_LABEL_FIELDS = (
    "process_correct",
    "has_error",
    "reasoning_correct",
    "plan_code_aligned",
    "first_faulty_layer",
    "first_faulty_step",
    "error_type",
    "rationale",
)


class Phase3AnnotationError(ValueError):
    def __init__(self, message: str, *, safe_stage: str = "P3E_ANNOTATION") -> None:
        super().__init__(message)
        self.safe_stage = safe_stage


class AnnotationProtocol(StrictFrozenModel):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase3_annotation_protocol"]
    protocol_id: Identifier
    frozen_cohort_manifest_sha256: Sha256
    annotation_guide_relative_path: Literal["docs/experiments/phase3_annotation_guide_v1.md"]
    annotation_guide_sha256: Sha256
    annotation_order_seed: int = Field(ge=0)
    visible_materials: tuple[str, ...]
    forbidden_materials: tuple[str, ...]
    required_label_fields: tuple[str, ...]
    positive_class: Literal["has_error_true"]
    unverified_suspicion_policy: Literal[
        "positive_for_any_error_detection_but_not_confirmed_evidence"
    ]
    invalid_method_outcome_policy: Literal[
        "retain_in_full_denominator_count_as_incorrect_and_report_separately"
    ]
    primary_comparisons: tuple[
        Literal[
            "full_tracejudge_vs_test_only",
            "full_tracejudge_vs_direct_llm_judge",
        ],
        ...,
    ]
    natural_binary_test: Literal["two_sided_exact_mcnemar_report_n01_n10"]
    counterfactual_interval: Literal["parent_problem_cluster_percentile_bootstrap"]
    bootstrap_iterations: Literal[10000]
    bootstrap_seed: int = Field(ge=0)
    confidence_level: Literal[0.95]
    multiple_comparison_policy: Literal["holm_for_confirmatory_primary_comparisons"]
    agreement_subset_minimum: int = Field(ge=1)
    agreement_subset_maximum: int = Field(ge=1)
    intra_rater_minimum_interval_days: int = Field(ge=1)
    exploratory_only: Literal[True] = True

    @model_validator(mode="after")
    def validate_frozen_semantics(self) -> Self:
        if self.visible_materials != _VISIBLE_MATERIALS:
            raise ValueError("annotation visible materials differ from the frozen order")
        if self.forbidden_materials != _FORBIDDEN_MATERIALS:
            raise ValueError("annotation forbidden materials differ from the frozen policy")
        if self.required_label_fields != _LABEL_FIELDS:
            raise ValueError("annotation label fields differ from the frozen schema")
        if self.primary_comparisons != (
            "full_tracejudge_vs_test_only",
            "full_tracejudge_vs_direct_llm_judge",
        ):
            raise ValueError("annotation primary comparisons differ from preregistration")
        if not 15 <= self.agreement_subset_minimum <= self.agreement_subset_maximum <= 20:
            raise ValueError("agreement subset must remain within the frozen 15-20 range")
        return self


class BlindedAnnotationTask(StrictFrozenModel):
    annotation_item_id: Identifier
    problem_id: Identifier
    code_sha256: Sha256
    structured_explanation_sha256: Sha256
    functional_evidence_sha256: Sha256
    public_problem: dict[str, Any]
    structured_solution_trace: dict[str, Any]
    candidate_code: str
    functional_evidence: dict[str, Any]
    public_dynamic_evidence: dict[str, Any]


class AnnotationIdentityRecord(StrictFrozenModel):
    annotation_item_id: Identifier
    trace_id: Identifier
    problem_id: Identifier
    code_sha256: Sha256
    structured_explanation_sha256: Sha256
    functional_evidence_sha256: Sha256


class AnnotationDraftRecord(StrictFrozenModel):
    annotation_item_id: Identifier
    annotation_protocol_sha256: Sha256
    rater_id: Identifier
    annotation_round: int = Field(ge=1)
    blinded_to_method_predictions: Literal[True] = True
    blinded_to_other_raters: bool
    status: Literal["pending"] = "pending"
    process_correct: None = None
    has_error: None = None
    reasoning_correct: None = None
    plan_code_aligned: None = None
    first_faulty_layer: None = None
    first_faulty_step: None = None
    error_type: None = None
    rationale: None = None


class AnnotationPacketManifest(StrictFrozenModel):
    schema_version: Literal[1] = 1
    phase: Literal["phase3_blinded_annotation_packet"] = "phase3_blinded_annotation_packet"
    status: Literal["completed"] = "completed"
    packet_id: Identifier
    created_at: datetime
    frozen_cohort_manifest_sha256: Sha256
    annotation_protocol_id: Identifier
    annotation_protocol_sha256: Sha256
    annotation_guide_sha256: Sha256
    material_payloads_sha256: Sha256
    annotation_order_seed: int = Field(ge=0)
    rater_id: Identifier
    annotation_round: int = Field(ge=1)
    blinded_to_other_raters: bool
    ordered_annotation_item_ids: tuple[Identifier, ...]
    item_count: int = Field(ge=1)
    natural_item_count: int = Field(ge=0)
    counterfactual_item_count: int = Field(ge=0)
    packet_sha256: Sha256
    identity_map_sha256: Sha256
    labels_template_sha256: Sha256
    contains_method_predictions: Literal[False] = False
    contains_counterfactual_mutation_metadata: Literal[False] = False
    contains_official_hidden_inputs: Literal[False] = False

    @model_validator(mode="after")
    def validate_packet_counts(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("annotation packet timestamp must be timezone-aware")
        if self.item_count != len(self.ordered_annotation_item_ids):
            raise ValueError("annotation packet count differs from item order")
        if self.item_count != self.natural_item_count + self.counterfactual_item_count:
            raise ValueError("annotation packet source counts do not cover all items")
        if len(self.ordered_annotation_item_ids) != len(set(self.ordered_annotation_item_ids)):
            raise ValueError("annotation item IDs must be unique")
        return self


@dataclass(frozen=True, slots=True)
class AnnotationPacketPreflightResult:
    packet_id: str
    protocol_id: str
    annotation_protocol_sha256: str
    annotation_guide_sha256: str
    item_count: int
    natural_item_count: int
    counterfactual_item_count: int
    material_payloads_sha256: str
    packet_sha256: str
    identity_map_sha256: str
    labels_template_sha256: str
    rater_id: str
    annotation_round: int


@dataclass(frozen=True, slots=True)
class AnnotationPacketExportResult(AnnotationPacketPreflightResult):
    run_dir: Path
    manifest_path: Path
    packet_path: Path
    identity_map_path: Path
    labels_template_path: Path
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class _PreparedAnnotationPacket:
    preflight: AnnotationPacketPreflightResult
    protocol: AnnotationProtocol
    packet_payload: bytes
    identity_payload: bytes
    labels_template_payload: bytes
    output_root: Path
    run_dir: Path


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
        raise Phase3AnnotationError(
            f"{label} must be a regular non-symlink file",
            safe_stage="P3E_INPUT",
        )
    try:
        if path.stat().st_size > _MAX_INPUT_BYTES:
            raise Phase3AnnotationError(f"{label} exceeds the size limit")
        payload = path.read_bytes()
    except OSError:
        raise Phase3AnnotationError(f"cannot read {label}") from None
    if len(payload) > _MAX_INPUT_BYTES:
        raise Phase3AnnotationError(f"{label} exceeds the size limit")
    return payload


def _decode_json(payload: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKey, ValueError):
        raise Phase3AnnotationError(f"{label} is not strict UTF-8 JSON") from None
    if not isinstance(value, dict):
        raise Phase3AnnotationError(f"{label} must contain one JSON object")
    return value


def _load_protocol(
    *,
    protocol_path: str | Path,
    guide_path: str | Path,
) -> AnnotationProtocol:
    protocol_payload = _read_regular_file(Path(protocol_path), label="annotation protocol")
    protocol_sha = hashlib.sha256(protocol_payload).hexdigest()
    if protocol_sha != ANNOTATION_PROTOCOL_SHA256:
        raise Phase3AnnotationError(
            "annotation protocol differs from the frozen Gate-E identity",
            safe_stage="P3E_PROTOCOL",
        )
    try:
        protocol = AnnotationProtocol.model_validate(
            _decode_json(protocol_payload, label="annotation protocol")
        )
    except ValidationError:
        raise Phase3AnnotationError(
            "annotation protocol failed schema validation",
            safe_stage="P3E_PROTOCOL",
        ) from None
    guide_payload = _read_regular_file(Path(guide_path), label="annotation guide")
    guide_sha = hashlib.sha256(guide_payload).hexdigest()
    if (
        guide_sha != ANNOTATION_GUIDE_SHA256
        or protocol.annotation_guide_sha256 != guide_sha
        or protocol.annotation_guide_relative_path != ANNOTATION_GUIDE_RELATIVE_PATH
    ):
        raise Phase3AnnotationError(
            "annotation guide differs from the frozen protocol",
            safe_stage="P3E_PROTOCOL",
        )
    return protocol


def _jsonl_bytes(values: Sequence[StrictFrozenModel]) -> bytes:
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


def _structured_solution_trace(solution: SolutionTrace) -> dict[str, Any]:
    payload = solution.model_dump(mode="json")
    payload.pop("code")
    return payload


def _resolve_output(
    *,
    output_dir: str | Path,
    packet_id: str,
) -> tuple[Path, Path]:
    if not _ID_PATTERN.fullmatch(packet_id):
        raise Phase3AnnotationError(
            "packet_id contains unsupported characters",
            safe_stage="P3E_OUTPUT",
        )
    root = Path(output_dir).expanduser()
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise Phase3AnnotationError(
            "annotation output root is unsafe",
            safe_stage="P3E_OUTPUT",
        )
    resolved = root.resolve()
    run_dir = resolved / packet_id
    if run_dir.exists() or run_dir.is_symlink():
        raise Phase3AnnotationError(
            "annotation packet directory already exists",
            safe_stage="P3E_OUTPUT",
        )
    return resolved, run_dir


def _prepare_annotation_packet(
    *,
    packet_id: str,
    rater_id: str,
    annotation_round: int,
    blinded_to_other_raters: bool,
    cohort_manifest_path: str | Path,
    natural_manifest_path: str | Path,
    phase1_run_dir: str | Path,
    phase2_run_dir: str | Path,
    dataset_manifest_path: str | Path,
    source_bundle_path: str | Path,
    execution_run_dir: str | Path,
    protocol_path: str | Path,
    guide_path: str | Path,
    output_dir: str | Path,
    privacy_canaries: Sequence[str | bytes] = (),
) -> _PreparedAnnotationPacket:
    if not _ID_PATTERN.fullmatch(rater_id):
        raise Phase3AnnotationError(
            "rater_id contains unsupported characters",
            safe_stage="P3E_INPUT",
        )
    if isinstance(annotation_round, bool) or annotation_round < 1:
        raise Phase3AnnotationError(
            "annotation_round must be a positive integer",
            safe_stage="P3E_INPUT",
        )
    protocol = _load_protocol(protocol_path=protocol_path, guide_path=guide_path)
    output_root, run_dir = _resolve_output(output_dir=output_dir, packet_id=packet_id)
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
        raise Phase3AnnotationError(
            "frozen annotation materials failed Gate-E binding",
            safe_stage=exc.safe_stage,
        ) from None
    if protocol.frozen_cohort_manifest_sha256 != loaded.cohort.overlay_manifest_sha256:
        raise Phase3AnnotationError(
            "annotation protocol references a different cohort",
            safe_stage="P3E_PROTOCOL",
        )

    shuffled_trace_ids = list(loaded.cohort.ordered_trace_ids)
    random.Random(protocol.annotation_order_seed).shuffle(shuffled_trace_ids)
    tasks: list[BlindedAnnotationTask] = []
    identities: list[AnnotationIdentityRecord] = []
    drafts: list[AnnotationDraftRecord] = []
    for index, trace_id in enumerate(shuffled_trace_ids, start=1):
        item_id = f"item_{index:03d}"
        material = loaded.materials[trace_id]
        trace = loaded.cohort.traces_by_id[trace_id]
        dynamic = material.public_dynamic_evidence
        if dynamic is None or dynamic.payload is None:
            raise Phase3AnnotationError(
                "annotation material lacks a frozen public-evidence availability record",
                safe_stage="P3E_MATERIAL",
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
            )
        ):
            raise Phase3AnnotationError(
                "blinded annotation task contains forbidden construction metadata",
                safe_stage="P3E_BLINDING",
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
        drafts.append(
            AnnotationDraftRecord(
                annotation_item_id=item_id,
                annotation_protocol_sha256=ANNOTATION_PROTOCOL_SHA256,
                rater_id=rater_id,
                annotation_round=annotation_round,
                blinded_to_other_raters=blinded_to_other_raters,
            )
        )

    packet_payload = _jsonl_bytes(tasks)
    identity_payload = _jsonl_bytes(identities)
    labels_payload = _jsonl_bytes(drafts)
    preflight = AnnotationPacketPreflightResult(
        packet_id=packet_id,
        protocol_id=protocol.protocol_id,
        annotation_protocol_sha256=ANNOTATION_PROTOCOL_SHA256,
        annotation_guide_sha256=protocol.annotation_guide_sha256,
        item_count=len(tasks),
        natural_item_count=loaded.cohort.natural_trace_count,
        counterfactual_item_count=loaded.cohort.counterfactual_trace_count,
        material_payloads_sha256=loaded.material_payloads_sha256,
        packet_sha256=hashlib.sha256(packet_payload).hexdigest(),
        identity_map_sha256=hashlib.sha256(identity_payload).hexdigest(),
        labels_template_sha256=hashlib.sha256(labels_payload).hexdigest(),
        rater_id=rater_id,
        annotation_round=annotation_round,
    )
    return _PreparedAnnotationPacket(
        preflight=preflight,
        protocol=protocol,
        packet_payload=packet_payload,
        identity_payload=identity_payload,
        labels_template_payload=labels_payload,
        output_root=output_root,
        run_dir=run_dir,
    )


def preflight_annotation_packet(**kwargs: Any) -> AnnotationPacketPreflightResult:
    """Validate and hash a blinded packet without writing any artifact."""

    return _prepare_annotation_packet(**kwargs).preflight


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


def _write_new_file(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o600)


def export_annotation_packet(**kwargs: Any) -> AnnotationPacketExportResult:
    """Atomically publish one private blinded packet and coordinator identity map."""

    prepared = _prepare_annotation_packet(**kwargs)
    prepared.output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    prepared.output_root.chmod(0o700)
    temporary_dir: Path | None = None
    try:
        temporary_dir = Path(
            tempfile.mkdtemp(prefix=f".{prepared.preflight.packet_id}.", dir=prepared.output_root)
        )
        temporary_dir.chmod(0o700)
        packet_path = temporary_dir / "packet.jsonl"
        identity_path = temporary_dir / "identity_map.jsonl"
        labels_path = temporary_dir / "labels_template.jsonl"
        _write_new_file(packet_path, prepared.packet_payload)
        _write_new_file(identity_path, prepared.identity_payload)
        _write_new_file(labels_path, prepared.labels_template_payload)
        manifest = AnnotationPacketManifest(
            packet_id=prepared.preflight.packet_id,
            created_at=datetime.now(UTC),
            frozen_cohort_manifest_sha256=prepared.protocol.frozen_cohort_manifest_sha256,
            annotation_protocol_id=prepared.protocol.protocol_id,
            annotation_protocol_sha256=prepared.preflight.annotation_protocol_sha256,
            annotation_guide_sha256=prepared.preflight.annotation_guide_sha256,
            material_payloads_sha256=prepared.preflight.material_payloads_sha256,
            annotation_order_seed=prepared.protocol.annotation_order_seed,
            rater_id=prepared.preflight.rater_id,
            annotation_round=prepared.preflight.annotation_round,
            blinded_to_other_raters=bool(kwargs.get("blinded_to_other_raters")),
            ordered_annotation_item_ids=tuple(
                f"item_{index:03d}" for index in range(1, prepared.preflight.item_count + 1)
            ),
            item_count=prepared.preflight.item_count,
            natural_item_count=prepared.preflight.natural_item_count,
            counterfactual_item_count=prepared.preflight.counterfactual_item_count,
            packet_sha256=prepared.preflight.packet_sha256,
            identity_map_sha256=prepared.preflight.identity_map_sha256,
            labels_template_sha256=prepared.preflight.labels_template_sha256,
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
            "cannot atomically publish the annotation packet",
            safe_stage="P3E_OUTPUT",
        ) from None
    finally:
        if temporary_dir is not None:
            shutil.rmtree(temporary_dir, ignore_errors=True)

    return AnnotationPacketExportResult(
        **asdict(prepared.preflight),
        run_dir=prepared.run_dir,
        manifest_path=prepared.run_dir / "manifest.json",
        packet_path=prepared.run_dir / "packet.jsonl",
        identity_map_path=prepared.run_dir / "identity_map.jsonl",
        labels_template_path=prepared.run_dir / "labels_template.jsonl",
        manifest_sha256=hashlib.sha256(
            (prepared.run_dir / "manifest.json").read_bytes()
        ).hexdigest(),
    )


__all__ = [
    "ANNOTATION_GUIDE_RELATIVE_PATH",
    "ANNOTATION_GUIDE_SHA256",
    "ANNOTATION_PROTOCOL_RELATIVE_PATH",
    "ANNOTATION_PROTOCOL_SHA256",
    "AnnotationPacketExportResult",
    "AnnotationPacketManifest",
    "AnnotationPacketPreflightResult",
    "AnnotationProtocol",
    "BlindedAnnotationTask",
    "Phase3AnnotationError",
    "export_annotation_packet",
    "preflight_annotation_packet",
]
