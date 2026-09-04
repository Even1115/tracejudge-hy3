"""Deterministic, aggregate-only P1 inter-rater agreement analysis.

The analysis joins the frozen 20-item secondary annotation set to the matching
records in the frozen 57-item primary set.  It never writes per-item labels,
trace identifiers, rationales, or disagreement lists to its output bundle.
"""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import stat
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, ValidationError, model_validator

from tracejudge_hy3.phase3.annotations import _fsync_directory, _write_new_file
from tracejudge_hy3.phase3.contracts import AnnotationRecord, AnnotationSetManifest

from .contracts import Phase4Contract, Sha256
from .p1_annotations import Phase4P1AnnotationError, _decode_json, _json_bytes
from .p1_formal_labels import (
    P1_FORMAL_LABELS_DEFAULT_MANIFEST,
    P1FormalLabelsManifest,
)
from .p1_study import _assert_private_location

P1_PRIMARY_LABELS_DEFAULT_MANIFEST = (
    "artifacts/experiments/phase3-labels/phase3_labels_primary_round1_v1/manifest.json"
)
P1_PRIMARY_LABELS_MANIFEST_SHA256 = (
    "fbf89aa950318392e49d01a5235461c4ce6ae94acb55842b963bb54048eac0a3"
)
P1_SECONDARY_LABELS_MANIFEST_SHA256 = (
    "80c583d47b9e428e0148fcf7c556a9d6f4342541eed1d079554e73592b2496cf"
)
P1_AGREEMENT_ANALYSIS_ID = "phase4_p1_inter_rater_agreement_v1"
P1_AGREEMENT_DEFAULT_OUTPUT = "artifacts/experiments/phase4-p1-agreement"
P1_AGREEMENT_DEFAULT_MANIFEST = (
    f"{P1_AGREEMENT_DEFAULT_OUTPUT}/{P1_AGREEMENT_ANALYSIS_ID}/manifest.json"
)
P1_AGREEMENT_BOOTSTRAP_SEED = 20260904
P1_AGREEMENT_BOOTSTRAP_ITERATIONS = 10_000
P1_AGREEMENT_ORIGIN_DATE = "2026-09-04"

_BINARY_FIELDS = (
    "has_error",
    "process_correct",
    "reasoning_correct",
    "plan_code_aligned",
)
_LOCALIZATION_FIELDS = (
    "first_faulty_layer",
    "first_faulty_step",
    "error_type",
)
_Z_975 = 1.959963984540054


class P1AgreementProportion(Phase4Contract):
    agreeing_count: int = Field(ge=0)
    denominator: int = Field(ge=0)
    estimate: float | None = Field(default=None, ge=0.0, le=1.0)
    interval_kind: Literal["wilson_95", "not_applicable"]
    interval_lower: float | None = Field(default=None, ge=0.0, le=1.0)
    interval_upper: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_proportion(self) -> Self:
        if self.agreeing_count > self.denominator:
            raise ValueError("agreement count exceeds denominator")
        if self.denominator == 0:
            if (
                self.estimate is not None
                or self.interval_kind != "not_applicable"
                or self.interval_lower is not None
                or self.interval_upper is not None
            ):
                raise ValueError("empty agreement denominator must be not applicable")
        else:
            if self.estimate is None or self.interval_kind != "wilson_95":
                raise ValueError("non-empty agreement denominator requires Wilson output")
            if self.interval_lower is None or self.interval_upper is None:
                raise ValueError("Wilson agreement interval is incomplete")
            if not math.isclose(
                self.estimate,
                self.agreeing_count / self.denominator,
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                raise ValueError("agreement estimate differs from its counts")
        return self


class P1BinaryConfusionCounts(Phase4Contract):
    both_true: int = Field(ge=0)
    primary_true_secondary_false: int = Field(ge=0)
    primary_false_secondary_true: int = Field(ge=0)
    both_false: int = Field(ge=0)
    total: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_total(self) -> Self:
        if (
            self.both_true
            + self.primary_true_secondary_false
            + self.primary_false_secondary_true
            + self.both_false
            != self.total
        ):
            raise ValueError("binary confusion cells do not sum to total")
        return self


class P1BinaryAgreement(Phase4Contract):
    field_name: Literal["has_error", "process_correct", "reasoning_correct", "plan_code_aligned"]
    confusion: P1BinaryConfusionCounts
    raw_agreement: P1AgreementProportion
    primary_true_count: int = Field(ge=0)
    secondary_true_count: int = Field(ge=0)
    positive_agreement: float | None = Field(default=None, ge=0.0, le=1.0)
    negative_agreement: float | None = Field(default=None, ge=0.0, le=1.0)
    expected_agreement: float | None = Field(default=None, ge=0.0, le=1.0)
    cohen_kappa: float | None = Field(default=None, ge=-1.0, le=1.0)
    kappa_status: Literal["computed", "not_applicable"]
    kappa_reason: str | None = None
    kappa_bootstrap_kind: Literal["paired_item_percentile_95", "not_applicable"]
    kappa_bootstrap_seed: int | None = Field(default=None, ge=0)
    kappa_bootstrap_iterations: int | None = Field(default=None, ge=1)
    kappa_bootstrap_valid_iterations: int | None = Field(default=None, ge=0)
    kappa_interval_lower: float | None = Field(default=None, ge=-1.0, le=1.0)
    kappa_interval_upper: float | None = Field(default=None, ge=-1.0, le=1.0)

    @model_validator(mode="after")
    def validate_kappa(self) -> Self:
        total = self.confusion.total
        if self.primary_true_count != (
            self.confusion.both_true + self.confusion.primary_true_secondary_false
        ):
            raise ValueError("primary true count differs from confusion cells")
        if self.secondary_true_count != (
            self.confusion.both_true + self.confusion.primary_false_secondary_true
        ):
            raise ValueError("secondary true count differs from confusion cells")
        if self.raw_agreement.denominator != total:
            raise ValueError("binary raw-agreement denominator differs from confusion total")
        if self.kappa_status == "computed":
            required = (
                self.expected_agreement,
                self.cohen_kappa,
                self.kappa_bootstrap_seed,
                self.kappa_bootstrap_iterations,
                self.kappa_bootstrap_valid_iterations,
                self.kappa_interval_lower,
                self.kappa_interval_upper,
            )
            if any(value is None for value in required):
                raise ValueError("computed kappa is missing required metadata")
            if self.kappa_reason is not None or self.kappa_bootstrap_kind != (
                "paired_item_percentile_95"
            ):
                raise ValueError("computed kappa carries an inconsistent status")
        else:
            if self.cohen_kappa is not None or self.kappa_reason is None:
                raise ValueError("not-applicable kappa requires one explicit reason")
            if self.kappa_bootstrap_kind != "not_applicable":
                raise ValueError("not-applicable kappa cannot carry a bootstrap kind")
        return self


class P1ConditionalExactAgreement(Phase4Contract):
    field_name: Literal[
        "first_faulty_layer", "first_faulty_step", "error_type", "joint_fault_label"
    ]
    all_items_including_no_error_null: P1AgreementProportion
    union_error_items: P1AgreementProportion
    both_error_items: P1AgreementProportion
    kappa_status: Literal["not_reported_conditional_sparse_nominal"] = (
        "not_reported_conditional_sparse_nominal"
    )


class P1CohortAgreement(Phase4Contract):
    cohort: Literal["all", "natural", "counterfactual"]
    has_error: P1BinaryAgreement


class P1InterRaterAgreementAnalysis(Phase4Contract):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase4_p1_inter_rater_agreement"] = (
        "tracejudge_phase4_p1_inter_rater_agreement"
    )
    analysis_id: Literal[P1_AGREEMENT_ANALYSIS_ID] = P1_AGREEMENT_ANALYSIS_ID
    status: Literal["analyzed"] = "analyzed"
    exploratory_only: Literal[True] = True
    source_primary_manifest_sha256: Sha256
    source_primary_annotations_sha256: Sha256
    source_secondary_manifest_sha256: Sha256
    source_secondary_annotations_sha256: Sha256
    primary_annotation_protocol_sha256: Sha256
    secondary_annotation_protocol_sha256: Sha256
    shared_annotation_guide_sha256: Sha256
    protocol_comparability_basis: Literal[
        "distinct_frozen_protocols_shared_guide_and_annotation_record_schema"
    ] = "distinct_frozen_protocols_shared_guide_and_annotation_record_schema"
    primary_rater_id: str = Field(min_length=1, max_length=128)
    secondary_rater_id: str = Field(min_length=1, max_length=128)
    item_count: Literal[20] = 20
    natural_item_count: Literal[15] = 15
    counterfactual_item_count: Literal[5] = 5
    matched_trace_count: Literal[20] = 20
    missing_primary_trace_count: Literal[0] = 0
    duplicate_trace_count: Literal[0] = 0
    binary_fields: tuple[P1BinaryAgreement, ...]
    localization_fields: tuple[P1ConditionalExactAgreement, ...]
    full_record_exact_agreement: P1AgreementProportion
    cohort_has_error: tuple[P1CohortAgreement, ...]
    raw_labels_unchanged: Literal[True] = True
    adjudication_performed: Literal[False] = False
    disagreement_items_emitted: Literal[False] = False
    contains_trace_ids: Literal[False] = False
    contains_per_item_labels: Literal[False] = False
    contains_rationales: Literal[False] = False
    multiple_comparison_policy: Literal["descriptive_no_hypothesis_tests"] = (
        "descriptive_no_hypothesis_tests"
    )
    provider_call_count: Literal[0] = 0
    docker_call_count: Literal[0] = 0
    network_call_count: Literal[0] = 0

    @model_validator(mode="after")
    def validate_analysis(self) -> Self:
        if self.primary_rater_id == self.secondary_rater_id:
            raise ValueError("inter-rater analysis requires distinct raters")
        if tuple(item.field_name for item in self.binary_fields) != _BINARY_FIELDS:
            raise ValueError("binary agreement fields differ from the frozen order")
        if tuple(item.field_name for item in self.localization_fields) != (
            *_LOCALIZATION_FIELDS,
            "joint_fault_label",
        ):
            raise ValueError("localization agreement fields differ from the frozen order")
        if tuple(item.cohort for item in self.cohort_has_error) != (
            "all",
            "natural",
            "counterfactual",
        ):
            raise ValueError("cohort agreement summaries differ from the frozen order")
        return self


class P1AgreementManifest(Phase4Contract):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase4_p1_agreement_bundle"] = "tracejudge_phase4_p1_agreement_bundle"
    status: Literal["frozen"] = "frozen"
    analysis_id: Literal[P1_AGREEMENT_ANALYSIS_ID] = P1_AGREEMENT_ANALYSIS_ID
    created_at: datetime
    analysis_path: Literal["agreement.json"] = "agreement.json"
    report_path: Literal["report.md"] = "report.md"
    analysis_sha256: Sha256
    report_sha256: Sha256
    source_primary_manifest_sha256: Sha256
    source_primary_annotations_sha256: Sha256
    source_secondary_manifest_sha256: Sha256
    source_secondary_annotations_sha256: Sha256
    item_count: Literal[20] = 20
    agreement_kind: Literal["inter_rater"] = "inter_rater"
    contains_trace_ids: Literal[False] = False
    contains_per_item_labels: Literal[False] = False
    contains_rationales: Literal[False] = False

    @model_validator(mode="after")
    def validate_timestamp(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("agreement manifest timestamp must be timezone-aware")
        return self


@dataclass(frozen=True, slots=True)
class P1AgreementPreflight:
    analysis: P1InterRaterAgreementAnalysis
    analysis_sha256: str
    report_sha256: str
    ready_to_publish: bool


@dataclass(frozen=True, slots=True)
class P1AgreementResult(P1AgreementPreflight):
    run_dir: Path
    manifest_path: Path
    analysis_path: Path
    report_path: Path
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class P1AgreementVerification:
    analysis_id: str
    item_count: int
    manifest_sha256: str
    analysis_sha256: str
    report_sha256: str
    verified: bool


@dataclass(frozen=True, slots=True)
class _LoadedAgreementSources:
    primary_manifest_payload: bytes
    primary_manifest: AnnotationSetManifest
    primary_records: tuple[AnnotationRecord, ...]
    secondary_manifest_payload: bytes
    secondary_manifest: P1FormalLabelsManifest
    secondary_records: tuple[AnnotationRecord, ...]


@dataclass(frozen=True, slots=True)
class _PreparedAgreement:
    preflight: P1AgreementPreflight
    analysis_payload: bytes
    report_payload: bytes
    output_root: Path
    run_dir: Path


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_private_file(path: Path, *, label: str) -> bytes:
    _assert_private_location(path, label=label)
    if path.is_symlink() or not path.is_file():
        raise Phase4P1AnnotationError(
            f"{label} must be a regular non-symlink file",
            safe_stage="P4D_P1_AGREEMENT_SOURCE",
        )
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise Phase4P1AnnotationError(
            f"{label} permissions are too broad",
            safe_stage="P4D_P1_AGREEMENT_SOURCE",
        )
    try:
        return path.read_bytes()
    except OSError:
        raise Phase4P1AnnotationError(
            f"cannot read {label}", safe_stage="P4D_P1_AGREEMENT_SOURCE"
        ) from None


def _parse_annotation_jsonl(payload: bytes, *, expected_count: int) -> tuple[AnnotationRecord, ...]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise Phase4P1AnnotationError(
            "annotation records are not UTF-8 JSONL",
            safe_stage="P4D_P1_AGREEMENT_SOURCE",
        ) from None
    lines = text.splitlines()
    if len(lines) != expected_count or any(not line.strip() for line in lines):
        raise Phase4P1AnnotationError(
            "annotation record count or JSONL structure is invalid",
            safe_stage="P4D_P1_AGREEMENT_SOURCE",
        )
    records: list[AnnotationRecord] = []
    for line in lines:
        try:
            records.append(
                AnnotationRecord.model_validate(
                    _decode_json(line.encode("utf-8"), label="annotation record")
                )
            )
        except ValidationError:
            raise Phase4P1AnnotationError(
                "annotation record failed schema validation",
                safe_stage="P4D_P1_AGREEMENT_SOURCE",
            ) from None
    return tuple(records)


def _load_sources(
    *,
    primary_manifest_path: str | Path,
    secondary_manifest_path: str | Path,
    expected_primary_manifest_sha256: str | None,
    expected_secondary_manifest_sha256: str | None,
) -> _LoadedAgreementSources:
    primary_path = Path(primary_manifest_path).expanduser().resolve()
    secondary_path = Path(secondary_manifest_path).expanduser().resolve()
    primary_payload = _read_private_file(primary_path, label="primary label manifest")
    secondary_payload = _read_private_file(secondary_path, label="secondary label manifest")
    if (
        expected_primary_manifest_sha256
        and _sha256(primary_payload) != expected_primary_manifest_sha256
    ) or (
        expected_secondary_manifest_sha256
        and _sha256(secondary_payload) != expected_secondary_manifest_sha256
    ):
        raise Phase4P1AnnotationError(
            "agreement source manifest differs from its frozen identity",
            safe_stage="P4D_P1_AGREEMENT_SOURCE",
        )
    try:
        primary = AnnotationSetManifest.model_validate(
            _decode_json(primary_payload, label="primary label manifest")
        )
        secondary = P1FormalLabelsManifest.model_validate(
            _decode_json(secondary_payload, label="secondary label manifest")
        )
    except ValidationError:
        raise Phase4P1AnnotationError(
            "agreement source manifest failed schema validation",
            safe_stage="P4D_P1_AGREEMENT_SOURCE",
        ) from None
    if (
        primary.status != "completed"
        or primary.agreement_kind != "not_computed"
        or primary.record_count != 57
        or secondary.status != "frozen"
        or secondary.agreement_kind != "not_computed"
        or secondary.record_count != 20
    ):
        raise Phase4P1AnnotationError(
            "agreement source manifests are not the required raw frozen rounds",
            safe_stage="P4D_P1_AGREEMENT_SOURCE",
        )
    primary_annotations_path = primary_path.parent / "annotations.jsonl"
    secondary_annotations_path = secondary_path.parent / secondary.annotation_records_path
    primary_annotations_payload = _read_private_file(
        primary_annotations_path, label="primary annotation records"
    )
    secondary_annotations_payload = _read_private_file(
        secondary_annotations_path, label="secondary annotation records"
    )
    if (
        _sha256(primary_annotations_payload) != primary.annotation_records_sha256
        or _sha256(secondary_annotations_payload) != secondary.annotation_records_sha256
    ):
        raise Phase4P1AnnotationError(
            "agreement source annotation hashes are inconsistent",
            safe_stage="P4D_P1_AGREEMENT_SOURCE",
        )
    primary_records = _parse_annotation_jsonl(
        primary_annotations_payload, expected_count=primary.record_count
    )
    secondary_records = _parse_annotation_jsonl(
        secondary_annotations_payload, expected_count=secondary.record_count
    )
    if any(
        item.annotation_protocol_sha256 != primary.annotation_protocol_sha256
        for item in primary_records
    ) or any(
        item.annotation_protocol_sha256 != secondary.annotation_protocol_sha256
        for item in secondary_records
    ):
        raise Phase4P1AnnotationError(
            "annotation records are not bound to their own frozen protocols",
            safe_stage="P4D_P1_AGREEMENT_SOURCE",
        )
    if primary.annotation_guide_sha256 != secondary.phase3_annotation_guide_sha256:
        raise Phase4P1AnnotationError(
            "the two annotation rounds do not share the frozen annotation guide",
            safe_stage="P4D_P1_AGREEMENT_SOURCE",
        )
    if (
        tuple(item.trace_id for item in primary_records) != primary.ordered_trace_ids
        or any(
            item.rater_id != primary.rater_ids[0]
            or item.annotation_round != primary.annotation_rounds[0]
            for item in primary_records
        )
        or any(
            item.rater_id != secondary.rater_id
            or item.annotation_round != secondary.annotation_round
            or not item.blinded_to_other_raters
            for item in secondary_records
        )
    ):
        raise Phase4P1AnnotationError(
            "annotation record order, rater, round, or blinding differs from its manifest",
            safe_stage="P4D_P1_AGREEMENT_SOURCE",
        )
    return _LoadedAgreementSources(
        primary_manifest_payload=primary_payload,
        primary_manifest=primary,
        primary_records=primary_records,
        secondary_manifest_payload=secondary_payload,
        secondary_manifest=secondary,
        secondary_records=secondary_records,
    )


def _round(value: float) -> float:
    return round(value, 12)


def _agreement_proportion(agreeing: int, denominator: int) -> P1AgreementProportion:
    if denominator == 0:
        return P1AgreementProportion(
            agreeing_count=0,
            denominator=0,
            estimate=None,
            interval_kind="not_applicable",
        )
    estimate = agreeing / denominator
    z2 = _Z_975**2
    scale = 1 + z2 / denominator
    center = (estimate + z2 / (2 * denominator)) / scale
    half = (
        _Z_975
        * math.sqrt(estimate * (1 - estimate) / denominator + z2 / (4 * denominator**2))
        / scale
    )
    return P1AgreementProportion(
        agreeing_count=agreeing,
        denominator=denominator,
        estimate=_round(estimate),
        interval_kind="wilson_95",
        interval_lower=_round(max(0.0, center - half)),
        interval_upper=_round(min(1.0, center + half)),
    )


def _kappa(values_a: tuple[bool, ...], values_b: tuple[bool, ...]) -> tuple[float, float] | None:
    if len(values_a) != len(values_b) or not values_a:
        raise ValueError("paired binary labels must have equal non-zero length")
    if len(set(values_a)) < 2 or len(set(values_b)) < 2:
        return None
    total = len(values_a)
    observed = sum(a == b for a, b in zip(values_a, values_b, strict=True)) / total
    primary_true = sum(values_a) / total
    secondary_true = sum(values_b) / total
    expected = primary_true * secondary_true + (1 - primary_true) * (1 - secondary_true)
    if math.isclose(expected, 1.0, rel_tol=0.0, abs_tol=1e-15):
        return None
    return _round((observed - expected) / (1 - expected)), _round(expected)


def _bootstrap_indices(*, iteration: int, draw: int, size: int, seed: int) -> int:
    payload = f"{seed}\0{iteration}\0{draw}".encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") % size


def _percentile_type7(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def _bootstrap_kappa(
    values_a: tuple[bool, ...],
    values_b: tuple[bool, ...],
    *,
    seed: int,
    iterations: int,
) -> tuple[int, float, float]:
    kappas: list[float] = []
    for iteration in range(iterations):
        indices = tuple(
            _bootstrap_indices(iteration=iteration, draw=draw, size=len(values_a), seed=seed)
            for draw in range(len(values_a))
        )
        result = _kappa(
            tuple(values_a[index] for index in indices),
            tuple(values_b[index] for index in indices),
        )
        if result is not None:
            kappas.append(result[0])
    if not kappas:
        raise Phase4P1AnnotationError(
            "kappa bootstrap has no defined resamples",
            safe_stage="P4D_P1_AGREEMENT_STATISTICS",
        )
    return (
        len(kappas),
        _round(_percentile_type7(kappas, 0.025)),
        _round(_percentile_type7(kappas, 0.975)),
    )


def _binary_agreement(
    field_name: str,
    values_a: tuple[bool, ...],
    values_b: tuple[bool, ...],
    *,
    seed: int,
    iterations: int,
) -> P1BinaryAgreement:
    both_true = sum(a and b for a, b in zip(values_a, values_b, strict=True))
    primary_true_secondary_false = sum(a and not b for a, b in zip(values_a, values_b, strict=True))
    primary_false_secondary_true = sum(not a and b for a, b in zip(values_a, values_b, strict=True))
    both_false = sum(not a and not b for a, b in zip(values_a, values_b, strict=True))
    total = len(values_a)
    confusion = P1BinaryConfusionCounts(
        both_true=both_true,
        primary_true_secondary_false=primary_true_secondary_false,
        primary_false_secondary_true=primary_false_secondary_true,
        both_false=both_false,
        total=total,
    )
    positive_denominator = (
        2 * both_true + primary_true_secondary_false + (primary_false_secondary_true)
    )
    negative_denominator = (
        2 * both_false + primary_true_secondary_false + (primary_false_secondary_true)
    )
    kappa = _kappa(values_a, values_b)
    common: dict[str, Any] = {
        "field_name": field_name,
        "confusion": confusion,
        "raw_agreement": _agreement_proportion(both_true + both_false, total),
        "primary_true_count": both_true + primary_true_secondary_false,
        "secondary_true_count": both_true + primary_false_secondary_true,
        "positive_agreement": (
            _round(2 * both_true / positive_denominator) if positive_denominator else None
        ),
        "negative_agreement": (
            _round(2 * both_false / negative_denominator) if negative_denominator else None
        ),
    }
    if kappa is None:
        return P1BinaryAgreement(
            **common,
            expected_agreement=None,
            cohen_kappa=None,
            kappa_status="not_applicable",
            kappa_reason=(
                "both binary classes are not present for each rater or expected agreement is one"
            ),
            kappa_bootstrap_kind="not_applicable",
        )
    valid, lower, upper = _bootstrap_kappa(values_a, values_b, seed=seed, iterations=iterations)
    return P1BinaryAgreement(
        **common,
        expected_agreement=kappa[1],
        cohen_kappa=kappa[0],
        kappa_status="computed",
        kappa_reason=None,
        kappa_bootstrap_kind="paired_item_percentile_95",
        kappa_bootstrap_seed=seed,
        kappa_bootstrap_iterations=iterations,
        kappa_bootstrap_valid_iterations=valid,
        kappa_interval_lower=lower,
        kappa_interval_upper=upper,
    )


def _conditional_agreement(
    field_name: str,
    values_a: tuple[Any, ...],
    values_b: tuple[Any, ...],
    error_a: tuple[bool, ...],
    error_b: tuple[bool, ...],
) -> P1ConditionalExactAgreement:
    all_indices = tuple(range(len(values_a)))
    union_indices = tuple(index for index in all_indices if error_a[index] or error_b[index])
    both_indices = tuple(index for index in all_indices if error_a[index] and error_b[index])

    def proportion(indices: tuple[int, ...]) -> P1AgreementProportion:
        return _agreement_proportion(
            sum(values_a[index] == values_b[index] for index in indices), len(indices)
        )

    return P1ConditionalExactAgreement(
        field_name=field_name,
        all_items_including_no_error_null=proportion(all_indices),
        union_error_items=proportion(union_indices),
        both_error_items=proportion(both_indices),
    )


def _percent(value: float | None) -> str:
    return "N/A" if value is None else f"{value * 100:.1f}%"


def _ratio(value: P1AgreementProportion) -> str:
    if value.estimate is None:
        return "0/0 (N/A)"
    return (
        f"{value.agreeing_count}/{value.denominator} ({_percent(value.estimate)}; "
        f"Wilson 95% CI {_percent(value.interval_lower)}–{_percent(value.interval_upper)})"
    )


def _render_report(analysis: P1InterRaterAgreementAnalysis) -> str:
    binary_rows = []
    for item in analysis.binary_fields:
        kappa = (
            f"{item.cohen_kappa:.3f} "
            f"(bootstrap 95% CI {item.kappa_interval_lower:.3f}–{item.kappa_interval_upper:.3f})"
            if item.cohen_kappa is not None
            else f"N/A ({item.kappa_reason})"
        )
        binary_rows.append(
            f"| `{item.field_name}` | {_ratio(item.raw_agreement)} | "
            f"{item.primary_true_count}/{item.confusion.total} | "
            f"{item.secondary_true_count}/{item.confusion.total} | {kappa} |"
        )
    location_rows = []
    for item in analysis.localization_fields:
        location_rows.append(
            f"| `{item.field_name}` | {_ratio(item.all_items_including_no_error_null)} | "
            f"{_ratio(item.union_error_items)} | {_ratio(item.both_error_items)} |"
        )
    cohort_rows = []
    for item in analysis.cohort_has_error:
        cohort_rows.append(
            f"| {item.cohort} | {_ratio(item.has_error.raw_agreement)} | "
            f"{item.has_error.confusion.both_true} | "
            f"{item.has_error.confusion.primary_true_secondary_false} | "
            f"{item.has_error.confusion.primary_false_secondary_true} | "
            f"{item.has_error.confusion.both_false} |"
        )
    core = next(item for item in analysis.binary_fields if item.field_name == "has_error")
    confusion = core.confusion
    return f"""## Material Passport

- Origin Skill: experiment-agent
- Origin Mode: validate
- Origin Date: {P1_AGREEMENT_ORIGIN_DATE}
- Verification Status: ANALYZED
- Version Label: phase4_p1_inter_rater_agreement_v1

## 两位标注者一致性分析

- 分析范围：预先冻结的 20 条子集（15 条自然轨迹、5 条反事实轨迹）
- 分析性质：探索性；无逐条标签、轨迹 ID、理由或分歧清单输出
- 原始标签：保持不变；尚未裁决分歧
- 协议可比性：两轮分别绑定各自冻结协议，共享同一标注指南和 `AnnotationRecord` 标签 Schema；协议哈希不同，不表述为同一协议文件
- 总体可信度：CAUTION（n=20，确定性子集，类别分布会影响 κ）

### 二元字段

| 字段 | 原始一致率 | 第一标注者 True | 第二标注者 True | Cohen's κ |
|---|---:|---:|---:|---:|
{chr(10).join(binary_rows)}

`process_correct` 由 Schema 强制为 `not has_error`，因此其一致率和 κ 与 `has_error` 等价，不应当作独立证据重复计数。

### `has_error` 双向混淆计数

| | 第二标注者：有错 | 第二标注者：无错 |
|---|---:|---:|
| 第一标注者：有错 | {confusion.both_true} | {confusion.primary_true_secondary_false} |
| 第一标注者：无错 | {confusion.primary_false_secondary_true} | {confusion.both_false} |

该表没有“真阳性/假阳性”含义；两名标注者均不是这里的金标准。正类一致率为 {_percent(core.positive_agreement)}，负类一致率为 {_percent(core.negative_agreement)}，Cohen's κ 的期望一致率为 {_percent(core.expected_agreement)}。

### 首错定位字段

| 字段 | 全 20 条（含双方无错时的 null） | 至少一人判错 | 双方都判错 |
|---|---:|---:|---:|
{chr(10).join(location_rows)}

定位字段不报告 κ：它们是条件性、稀疏的多类别标签。全 20 条读数会被双方无错时共同为 `null` 抬高，应优先同时查看“至少一人判错”和“双方都判错”分母。

完整七字段记录逐条完全一致：{_ratio(analysis.full_record_exact_agreement)}。

### `has_error` 分层描述

| 子集 | 原始一致率 | 双方有错 | 第一有错/第二无错 | 第一无错/第二有错 | 双方无错 |
|---|---:|---:|---:|---:|---:|
{chr(10).join(cohort_rows)}

分层结果只用于检查自然/反事实构成是否掩盖总体差异；样本尤其是反事实 n=5，不能作稳定总体推断。

### 警告与边界

- κ 是机会校正的一致性描述，不是准确率、效度或标注质量的充分证明。
- κ 的区间来自固定种子 `{P1_AGREEMENT_BOOTSTRAP_SEED}` 的 {P1_AGREEMENT_BOOTSTRAP_ITERATIONS:,} 次配对条目非参数 bootstrap；由于子集为确定性选取，该区间仅描述这 20 条的重抽样不确定性，不代表对所有任务的概率抽样推断。
- 若观察到的配对标签零分歧，经验分布 bootstrap 会机械地产生退化的 κ 区间 1–1；它不能表达未观察条目或潜在标注误差的不确定性，应同时查看原始一致率的 Wilson 区间。
- 多个字段均为描述性输出，没有进行显著性检验，也不据此筛选“最好看的”字段。
- 分歧讨论只能在本原始统计冻结后进行；任何裁决必须写入新产物，不得覆盖两份原始标签。

### 统计谬误扫描

- 覆盖：11/11

| 谬误 | 严重度 | 本次检查 |
|---|---|---|
| Simpson's paradox | NOTE | 同时报告总体、自然和反事实分层；不把分层差异解释为总体反转。 |
| Ecological fallacy | NOTE | 只描述条目级配对一致性，不从聚合值推断标注者个体能力。 |
| Berkson's paradox | CAUTION | 20 条来自预先冻结的确定性子集；外推范围受限。 |
| Collider bias | NOTE | 未进行协变量控制或条件回归，不产生该类调整偏差。 |
| Base-rate neglect | NOTE | 同时报出两位标注者的正类计数、正/负类一致率和 κ 期望一致率。 |
| Regression to the mean | NOTE | 无基于极端得分选择的前后测设计。 |
| Survivorship bias | NOTE | 正式 20/20 条均完成并纳入，没有完成者筛除。 |
| Look-elsewhere effect | CAUTION | 多字段只作描述，不进行显著性筛选；不得挑选单一高一致字段概括全部表现。 |
| Garden of forking paths | CAUTION | raw agreement/混淆计数/κ 已预注册；定位字段的三分母和 bootstrap 区间属于透明的分析细化，均标为探索性。 |
| Correlation ≠ causation | NOTE | 本分析不作因果主张。 |
| Reverse causality | NOTE | 不存在时间方向或因果方向模型。 |

### Reproducibility

- Method: deterministic aggregate recomputation from two frozen manifests
- Verdict: run `p1-agreement-verify` after publication; no Provider, Docker, or network calls
"""


def prepare_p1_agreement(
    *,
    primary_manifest_path: str | Path = P1_PRIMARY_LABELS_DEFAULT_MANIFEST,
    secondary_manifest_path: str | Path = P1_FORMAL_LABELS_DEFAULT_MANIFEST,
    output_dir: str | Path = P1_AGREEMENT_DEFAULT_OUTPUT,
    expected_primary_manifest_sha256: str | None = P1_PRIMARY_LABELS_MANIFEST_SHA256,
    expected_secondary_manifest_sha256: str | None = P1_SECONDARY_LABELS_MANIFEST_SHA256,
    bootstrap_seed: int = P1_AGREEMENT_BOOTSTRAP_SEED,
    bootstrap_iterations: int = P1_AGREEMENT_BOOTSTRAP_ITERATIONS,
) -> _PreparedAgreement:
    if bootstrap_iterations != P1_AGREEMENT_BOOTSTRAP_ITERATIONS:
        raise Phase4P1AnnotationError(
            "agreement bootstrap iterations differ from the fixed analysis",
            safe_stage="P4D_P1_AGREEMENT_CONFIG",
        )
    if bootstrap_seed != P1_AGREEMENT_BOOTSTRAP_SEED:
        raise Phase4P1AnnotationError(
            "agreement bootstrap seed differs from the fixed analysis",
            safe_stage="P4D_P1_AGREEMENT_CONFIG",
        )
    sources = _load_sources(
        primary_manifest_path=primary_manifest_path,
        secondary_manifest_path=secondary_manifest_path,
        expected_primary_manifest_sha256=expected_primary_manifest_sha256,
        expected_secondary_manifest_sha256=expected_secondary_manifest_sha256,
    )
    if len(sources.primary_manifest.rater_ids) != 1:
        raise Phase4P1AnnotationError(
            "primary source must contain exactly one raw rater round",
            safe_stage="P4D_P1_AGREEMENT_SOURCE",
        )
    primary_by_trace = {item.trace_id: item for item in sources.primary_records}
    if len(primary_by_trace) != len(sources.primary_records):
        raise Phase4P1AnnotationError(
            "primary annotations contain duplicate trace IDs",
            safe_stage="P4D_P1_AGREEMENT_SOURCE",
        )
    secondary_ids = tuple(item.trace_id for item in sources.secondary_records)
    if secondary_ids != sources.secondary_manifest.ordered_trace_ids:
        raise Phase4P1AnnotationError(
            "secondary annotation order differs from its manifest",
            safe_stage="P4D_P1_AGREEMENT_SOURCE",
        )
    if len(set(secondary_ids)) != 20 or any(
        trace_id not in primary_by_trace for trace_id in secondary_ids
    ):
        raise Phase4P1AnnotationError(
            "secondary traces do not form a unique 20-item subset of primary labels",
            safe_stage="P4D_P1_AGREEMENT_SOURCE",
        )
    primary = tuple(primary_by_trace[trace_id] for trace_id in secondary_ids)
    secondary = sources.secondary_records
    for left, right in zip(primary, secondary, strict=True):
        if (
            left.trace_id != right.trace_id
            or left.code_sha256 != right.code_sha256
            or left.structured_explanation_sha256 != right.structured_explanation_sha256
            or left.functional_evidence_sha256 != right.functional_evidence_sha256
        ):
            raise Phase4P1AnnotationError(
                "paired annotation identity hashes are inconsistent",
                safe_stage="P4D_P1_AGREEMENT_SOURCE",
            )
    primary_rater_id = sources.primary_manifest.rater_ids[0]
    secondary_rater_id = sources.secondary_manifest.rater_id
    if primary_rater_id == secondary_rater_id:
        raise Phase4P1AnnotationError(
            "agreement sources do not represent distinct raters",
            safe_stage="P4D_P1_AGREEMENT_SOURCE",
        )

    binary = tuple(
        _binary_agreement(
            field_name,
            tuple(bool(getattr(item, field_name)) for item in primary),
            tuple(bool(getattr(item, field_name)) for item in secondary),
            seed=bootstrap_seed,
            iterations=bootstrap_iterations,
        )
        for field_name in _BINARY_FIELDS
    )
    error_a = tuple(item.has_error for item in primary)
    error_b = tuple(item.has_error for item in secondary)
    localization = tuple(
        _conditional_agreement(
            field_name,
            tuple(getattr(item, field_name) for item in primary),
            tuple(getattr(item, field_name) for item in secondary),
            error_a,
            error_b,
        )
        for field_name in _LOCALIZATION_FIELDS
    )
    joint_a = tuple(
        (item.first_faulty_layer, item.first_faulty_step, item.error_type) for item in primary
    )
    joint_b = tuple(
        (item.first_faulty_layer, item.first_faulty_step, item.error_type) for item in secondary
    )
    localization = (
        *localization,
        _conditional_agreement("joint_fault_label", joint_a, joint_b, error_a, error_b),
    )
    full_record_a = tuple(
        (
            item.process_correct,
            item.has_error,
            item.reasoning_correct,
            item.plan_code_aligned,
            item.first_faulty_layer,
            item.first_faulty_step,
            item.error_type,
        )
        for item in primary
    )
    full_record_b = tuple(
        (
            item.process_correct,
            item.has_error,
            item.reasoning_correct,
            item.plan_code_aligned,
            item.first_faulty_layer,
            item.first_faulty_step,
            item.error_type,
        )
        for item in secondary
    )
    full_record = _agreement_proportion(
        sum(a == b for a, b in zip(full_record_a, full_record_b, strict=True)), 20
    )

    prefixes = tuple(trace_id.split(":", 1)[0] for trace_id in secondary_ids)
    if prefixes.count("natural") != 15 or prefixes.count("counterfactual") != 5:
        raise Phase4P1AnnotationError(
            "secondary trace kinds differ from the frozen 15+5 design",
            safe_stage="P4D_P1_AGREEMENT_SOURCE",
        )
    cohort_summaries: list[P1CohortAgreement] = []
    for cohort in ("all", "natural", "counterfactual"):
        indices = tuple(
            index for index, prefix in enumerate(prefixes) if cohort == "all" or prefix == cohort
        )
        cohort_summaries.append(
            P1CohortAgreement(
                cohort=cohort,
                has_error=_binary_agreement(
                    "has_error",
                    tuple(error_a[index] for index in indices),
                    tuple(error_b[index] for index in indices),
                    seed=bootstrap_seed,
                    iterations=bootstrap_iterations,
                ),
            )
        )
    try:
        analysis = P1InterRaterAgreementAnalysis(
            source_primary_manifest_sha256=_sha256(sources.primary_manifest_payload),
            source_primary_annotations_sha256=(sources.primary_manifest.annotation_records_sha256),
            source_secondary_manifest_sha256=_sha256(sources.secondary_manifest_payload),
            source_secondary_annotations_sha256=(
                sources.secondary_manifest.annotation_records_sha256
            ),
            primary_annotation_protocol_sha256=(
                sources.primary_manifest.annotation_protocol_sha256
            ),
            secondary_annotation_protocol_sha256=(
                sources.secondary_manifest.annotation_protocol_sha256
            ),
            shared_annotation_guide_sha256=(sources.primary_manifest.annotation_guide_sha256),
            primary_rater_id=primary_rater_id,
            secondary_rater_id=secondary_rater_id,
            binary_fields=binary,
            localization_fields=localization,
            full_record_exact_agreement=full_record,
            cohort_has_error=tuple(cohort_summaries),
        )
    except ValidationError:
        raise Phase4P1AnnotationError(
            "agreement analysis failed aggregate validation",
            safe_stage="P4D_P1_AGREEMENT_STATISTICS",
        ) from None
    analysis_payload = _json_bytes(analysis)
    report_payload = (_render_report(analysis).rstrip() + "\n").encode("utf-8")
    output_root = Path(output_dir).expanduser().resolve()
    run_dir = output_root / P1_AGREEMENT_ANALYSIS_ID
    return _PreparedAgreement(
        preflight=P1AgreementPreflight(
            analysis=analysis,
            analysis_sha256=_sha256(analysis_payload),
            report_sha256=_sha256(report_payload),
            ready_to_publish=True,
        ),
        analysis_payload=analysis_payload,
        report_payload=report_payload,
        output_root=output_root,
        run_dir=run_dir,
    )


def preflight_p1_agreement(**kwargs: Any) -> P1AgreementPreflight:
    return prepare_p1_agreement(**kwargs).preflight


def publish_p1_agreement(**kwargs: Any) -> P1AgreementResult:
    prepared = prepare_p1_agreement(**kwargs)
    if prepared.run_dir.exists() or prepared.run_dir.is_symlink():
        raise Phase4P1AnnotationError(
            "agreement analysis directory already exists",
            safe_stage="P4D_P1_AGREEMENT_OUTPUT",
        )
    prepared.output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    prepared.output_root.chmod(0o700)
    temporary_dir: Path | None = None
    try:
        temporary_dir = Path(
            tempfile.mkdtemp(prefix=f".{P1_AGREEMENT_ANALYSIS_ID}.", dir=prepared.output_root)
        )
        temporary_dir.chmod(0o700)
        _write_new_file(temporary_dir / "agreement.json", prepared.analysis_payload)
        _write_new_file(temporary_dir / "report.md", prepared.report_payload)
        manifest = P1AgreementManifest(
            created_at=datetime.now(UTC),
            analysis_sha256=prepared.preflight.analysis_sha256,
            report_sha256=prepared.preflight.report_sha256,
            source_primary_manifest_sha256=(
                prepared.preflight.analysis.source_primary_manifest_sha256
            ),
            source_primary_annotations_sha256=(
                prepared.preflight.analysis.source_primary_annotations_sha256
            ),
            source_secondary_manifest_sha256=(
                prepared.preflight.analysis.source_secondary_manifest_sha256
            ),
            source_secondary_annotations_sha256=(
                prepared.preflight.analysis.source_secondary_annotations_sha256
            ),
        )
        manifest_payload = _json_bytes(manifest)
        _write_new_file(temporary_dir / "manifest.json", manifest_payload)
        os.replace(temporary_dir, prepared.run_dir)
        temporary_dir = None
        _fsync_directory(prepared.output_root)
    except OSError:
        raise Phase4P1AnnotationError(
            "cannot atomically publish agreement analysis",
            safe_stage="P4D_P1_AGREEMENT_OUTPUT",
        ) from None
    finally:
        if temporary_dir is not None:
            shutil.rmtree(temporary_dir, ignore_errors=True)
    manifest_path = prepared.run_dir / "manifest.json"
    return P1AgreementResult(
        **asdict(prepared.preflight),
        run_dir=prepared.run_dir,
        manifest_path=manifest_path,
        analysis_path=prepared.run_dir / "agreement.json",
        report_path=prepared.run_dir / "report.md",
        manifest_sha256=_sha256(manifest_path.read_bytes()),
    )


def verify_p1_agreement(
    *,
    manifest_path: str | Path = P1_AGREEMENT_DEFAULT_MANIFEST,
    expected_manifest_sha256: str | None = None,
    primary_manifest_path: str | Path = P1_PRIMARY_LABELS_DEFAULT_MANIFEST,
    secondary_manifest_path: str | Path = P1_FORMAL_LABELS_DEFAULT_MANIFEST,
    expected_primary_manifest_sha256: str | None = P1_PRIMARY_LABELS_MANIFEST_SHA256,
    expected_secondary_manifest_sha256: str | None = P1_SECONDARY_LABELS_MANIFEST_SHA256,
) -> P1AgreementVerification:
    path = Path(manifest_path).expanduser().resolve()
    payload = _read_private_file(path, label="P1 agreement manifest")
    manifest_sha256 = _sha256(payload)
    if expected_manifest_sha256 and manifest_sha256 != expected_manifest_sha256:
        raise Phase4P1AnnotationError(
            "agreement manifest differs from the expected identity",
            safe_stage="P4D_P1_AGREEMENT_VERIFY",
        )
    try:
        manifest = P1AgreementManifest.model_validate(
            _decode_json(payload, label="P1 agreement manifest")
        )
    except ValidationError:
        raise Phase4P1AnnotationError(
            "agreement manifest failed schema validation",
            safe_stage="P4D_P1_AGREEMENT_VERIFY",
        ) from None
    analysis_payload = _read_private_file(
        path.parent / manifest.analysis_path, label="P1 agreement analysis"
    )
    report_payload = _read_private_file(
        path.parent / manifest.report_path, label="P1 agreement report"
    )
    if (
        _sha256(analysis_payload) != manifest.analysis_sha256
        or _sha256(report_payload) != manifest.report_sha256
    ):
        raise Phase4P1AnnotationError(
            "agreement bundle hashes are inconsistent",
            safe_stage="P4D_P1_AGREEMENT_VERIFY",
        )
    prepared = prepare_p1_agreement(
        primary_manifest_path=primary_manifest_path,
        secondary_manifest_path=secondary_manifest_path,
        output_dir=path.parent.parent,
        expected_primary_manifest_sha256=expected_primary_manifest_sha256,
        expected_secondary_manifest_sha256=expected_secondary_manifest_sha256,
    )
    if (
        analysis_payload != prepared.analysis_payload
        or report_payload != prepared.report_payload
        or manifest.source_primary_manifest_sha256
        != prepared.preflight.analysis.source_primary_manifest_sha256
        or manifest.source_secondary_manifest_sha256
        != prepared.preflight.analysis.source_secondary_manifest_sha256
    ):
        raise Phase4P1AnnotationError(
            "agreement bundle differs from deterministic source recomputation",
            safe_stage="P4D_P1_AGREEMENT_VERIFY",
        )
    return P1AgreementVerification(
        analysis_id=manifest.analysis_id,
        item_count=manifest.item_count,
        manifest_sha256=manifest_sha256,
        analysis_sha256=manifest.analysis_sha256,
        report_sha256=manifest.report_sha256,
        verified=True,
    )


__all__ = [
    "P1_AGREEMENT_ANALYSIS_ID",
    "P1_AGREEMENT_BOOTSTRAP_ITERATIONS",
    "P1_AGREEMENT_BOOTSTRAP_SEED",
    "P1_AGREEMENT_DEFAULT_MANIFEST",
    "P1_AGREEMENT_DEFAULT_OUTPUT",
    "P1_AGREEMENT_ORIGIN_DATE",
    "P1_PRIMARY_LABELS_DEFAULT_MANIFEST",
    "P1_PRIMARY_LABELS_MANIFEST_SHA256",
    "P1_SECONDARY_LABELS_MANIFEST_SHA256",
    "P1AgreementManifest",
    "P1AgreementPreflight",
    "P1AgreementProportion",
    "P1AgreementResult",
    "P1AgreementVerification",
    "P1BinaryAgreement",
    "P1BinaryConfusionCounts",
    "P1CohortAgreement",
    "P1ConditionalExactAgreement",
    "P1InterRaterAgreementAnalysis",
    "preflight_p1_agreement",
    "prepare_p1_agreement",
    "publish_p1_agreement",
    "verify_p1_agreement",
]
