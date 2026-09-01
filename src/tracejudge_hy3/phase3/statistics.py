"""Gate-E4 paired statistics over frozen labels and completed method outcomes.

The loader fails closed on every cohort, annotation, run, result, and index
identity.  Published artifacts contain aggregate counts only: annotation
rationales, per-trace predictions, Provider raw output, and hidden evaluation
material never cross this module's output boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import random
import re
import shutil
import stat
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from tracejudge_hy3.baseline.runner import _dependency_versions, _git_metadata

from .annotations import AnnotationProtocol, _load_protocol
from .contracts import (
    AnnotationRecord,
    CounterfactualKind,
    CounterfactualTrace,
    MethodId,
    MethodOutcome,
    MethodOutcomeStatus,
    PairedEvaluationIndex,
    Phase3RunManifest,
)
from .execution import load_annotation_set_binding
from .privacy import assert_public_payload_safe, canonical_sha256, jsonl_record_sha256
from .runner import LoadedPairedCohort, Phase3RunnerError, load_paired_cohort

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_INPUT_BYTES = 128 * 1024 * 1024
_Z_95 = 1.959963984540054
_PRIMARY_BASELINES = (MethodId.TEST_ONLY, MethodId.DIRECT_LLM_JUDGE)
_SCOPES = ("all", "natural", "counterfactual")


class Phase3StatisticsError(ValueError):
    """Safe, content-free Gate-E4 failure."""

    def __init__(self, message: str, *, safe_stage: str = "P3E4_STATISTICS") -> None:
        super().__init__(message)
        self.safe_stage = safe_stage


@dataclass(frozen=True, slots=True)
class Phase3StatisticsPreflight:
    statistics_id: str
    paired_run_id: str
    annotation_set_id: str
    natural_trace_count: int
    counterfactual_trace_count: int
    trace_count: int
    method_count: int
    pair_count: int
    final_status_counts: Mapping[str, int]
    paired_run_manifest_sha256: str
    paired_results_sha256: str
    paired_index_sha256: str
    annotation_set_manifest_sha256: str
    completed_labels_sha256: str
    annotation_records_sha256: str
    protocol_sha256: str
    statistics_implementation_sha256: str
    report_sha256: str
    git_commit: str
    git_branch: str
    git_dirty: bool


@dataclass(frozen=True, slots=True)
class Phase3StatisticsResult(Phase3StatisticsPreflight):
    run_dir: Path
    manifest_path: Path
    report_path: Path
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class _EffectiveOutcome:
    final: MethodOutcome
    effective: MethodOutcome


@dataclass(frozen=True, slots=True)
class _LoadedStatisticsInputs:
    cohort: LoadedPairedCohort
    protocol: AnnotationProtocol
    annotations: Mapping[str, AnnotationRecord]
    run_manifest: Phase3RunManifest
    effective_outcomes: Mapping[tuple[str, MethodId], _EffectiveOutcome]
    run_manifest_sha256: str
    results_sha256: str
    index_sha256: str
    annotation_set_id: str
    annotation_manifest_sha256: str
    completed_labels_sha256: str
    annotation_records_sha256: str
    final_status_counts: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class _PreparedStatistics:
    preflight: Phase3StatisticsPreflight
    report: Mapping[str, Any]
    report_payload: bytes
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


def _read_regular_file(path: Path, *, label: str, private: bool = False) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise Phase3StatisticsError(
            f"{label} must be a regular non-symlink file",
            safe_stage="P3E4_INPUT",
        )
    try:
        details = path.stat()
        if private and stat.S_IMODE(details.st_mode) & 0o077:
            raise Phase3StatisticsError(
                f"{label} permissions are too broad",
                safe_stage="P3E4_INPUT",
            )
        if details.st_size > _MAX_INPUT_BYTES:
            raise Phase3StatisticsError(
                f"{label} exceeds the size limit",
                safe_stage="P3E4_INPUT",
            )
        payload = path.read_bytes()
    except OSError:
        raise Phase3StatisticsError(
            f"cannot read {label}",
            safe_stage="P3E4_INPUT",
        ) from None
    if len(payload) > _MAX_INPUT_BYTES:
        raise Phase3StatisticsError(
            f"{label} exceeds the size limit",
            safe_stage="P3E4_INPUT",
        )
    return payload


def _decode_json(payload: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKey, ValueError):
        raise Phase3StatisticsError(
            f"{label} is not strict UTF-8 JSON",
            safe_stage="P3E4_INPUT",
        ) from None
    if not isinstance(value, dict):
        raise Phase3StatisticsError(
            f"{label} must contain one JSON object",
            safe_stage="P3E4_INPUT",
        )
    return value


def _decode_jsonl_models(
    payload: bytes,
    *,
    label: str,
    model: type[AnnotationRecord] | type[MethodOutcome],
) -> list[AnnotationRecord] | list[MethodOutcome]:
    if payload and not payload.endswith(b"\n"):
        raise Phase3StatisticsError(
            f"{label} is truncated",
            safe_stage="P3E4_INPUT",
        )
    rows: list[Any] = []
    for line_number, raw in enumerate(payload.splitlines(keepends=True), start=1):
        try:
            value = _decode_json(raw, label=f"{label} row")
            rows.append(model.model_validate(value))
        except (ValidationError, Phase3StatisticsError):
            raise Phase3StatisticsError(
                f"{label} row {line_number} failed strict validation",
                safe_stage="P3E4_INPUT",
            ) from None
    return rows


def wilson_interval(successes: int, total: int) -> tuple[float | None, float | None]:
    """Return the two-sided 95% Wilson score interval."""

    if successes < 0 or total < 0 or successes > total:
        raise ValueError("invalid binomial count")
    if total == 0:
        return None, None
    estimate = successes / total
    z2 = _Z_95 * _Z_95
    denominator = 1.0 + z2 / total
    center = (estimate + z2 / (2.0 * total)) / denominator
    half_width = (
        _Z_95
        * math.sqrt(estimate * (1.0 - estimate) / total + z2 / (4.0 * total * total))
        / denominator
    )
    return max(0.0, center - half_width), min(1.0, center + half_width)


def _proportion(successes: int, total: int) -> dict[str, Any]:
    lower, upper = wilson_interval(successes, total)
    return {
        "numerator": successes,
        "denominator": total,
        "estimate": successes / total if total else None,
        "wilson_95_lower": lower,
        "wilson_95_upper": upper,
    }


def exact_mcnemar_p_value(n01: int, n10: int) -> float:
    """Two-sided exact McNemar p-value, conditional on discordant pairs."""

    if n01 < 0 or n10 < 0:
        raise ValueError("discordant counts must be non-negative")
    discordant = n01 + n10
    if discordant == 0:
        return 1.0
    tail = sum(math.comb(discordant, index) for index in range(min(n01, n10) + 1))
    return min(1.0, 2.0 * tail / (2**discordant))


def holm_adjust(p_values: Sequence[float]) -> tuple[float, ...]:
    """Return Holm step-down adjusted p-values in the original order."""

    if any(not 0.0 <= value <= 1.0 for value in p_values):
        raise ValueError("p-values must be within [0, 1]")
    ordered = sorted(enumerate(p_values), key=lambda item: item[1])
    adjusted = [0.0] * len(ordered)
    running = 0.0
    count = len(ordered)
    for rank, (original_index, value) in enumerate(ordered):
        running = max(running, min(1.0, (count - rank) * value))
        adjusted[original_index] = running
    return tuple(adjusted)


def percentile_type7(values: Sequence[float], probability: float) -> float:
    """R type-7 percentile with linear interpolation."""

    if not values or not 0.0 <= probability <= 1.0:
        raise ValueError("percentile input is invalid")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def cluster_bootstrap_interval(
    cluster_deltas: Mapping[str, Sequence[int]],
    *,
    iterations: int,
    seed: int,
    confidence_level: float,
) -> tuple[float, float]:
    """Percentile interval for a paired mean difference, resampling clusters."""

    if not cluster_deltas or iterations < 1 or not 0.0 < confidence_level < 1.0:
        raise ValueError("cluster bootstrap configuration is invalid")
    clusters = tuple(sorted(cluster_deltas))
    if any(not values for values in cluster_deltas.values()):
        raise ValueError("cluster bootstrap contains an empty cluster")
    generator = random.Random(seed)
    estimates: list[float] = []
    for _ in range(iterations):
        total = 0
        count = 0
        for _cluster_index in clusters:
            sampled = generator.choice(clusters)
            values = cluster_deltas[sampled]
            total += sum(values)
            count += len(values)
        estimates.append(total / count)
    alpha = 1.0 - confidence_level
    return (
        percentile_type7(estimates, alpha / 2.0),
        percentile_type7(estimates, 1.0 - alpha / 2.0),
    )


def statistics_implementation_sha256() -> str:
    """Bind the analysis implementation and its public-output safety contract."""

    paths = (
        Path(__file__),
        Path(__file__).with_name("contracts.py"),
        Path(__file__).with_name("privacy.py"),
    )
    digest = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(Path(__file__).parents[1])
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _load_annotations(
    *,
    annotation_manifest_path: Path,
    expected_manifest_sha256: str,
    cohort: LoadedPairedCohort,
    protocol_path: str | Path,
    guide_path: str | Path,
) -> tuple[Mapping[str, AnnotationRecord], str, str, str, str]:
    try:
        binding = load_annotation_set_binding(
            manifest_path=annotation_manifest_path,
            expected_manifest_sha256=expected_manifest_sha256,
            frozen_manifest_sha256=cohort.overlay_manifest_sha256,
            ordered_trace_ids=cohort.ordered_trace_ids,
            natural_trace_count=cohort.natural_trace_count,
            counterfactual_trace_count=cohort.counterfactual_trace_count,
            protocol_path=protocol_path,
            guide_path=guide_path,
        )
    except Phase3RunnerError as exc:
        raise Phase3StatisticsError(
            "annotation set failed frozen binding",
            safe_stage=exc.safe_stage,
        ) from None
    payload = _read_regular_file(
        annotation_manifest_path.parent / "annotations.jsonl",
        label="annotation records",
        private=True,
    )
    if hashlib.sha256(payload).hexdigest() != binding.manifest.annotation_records_sha256:
        raise Phase3StatisticsError(
            "annotation record hash differs",
            safe_stage="P3E4_ANNOTATION",
        )
    records = _decode_jsonl_models(payload, label="annotation records", model=AnnotationRecord)
    assert all(isinstance(item, AnnotationRecord) for item in records)
    annotations = {item.trace_id: item for item in records}
    if tuple(item.trace_id for item in records) != cohort.ordered_trace_ids:
        raise Phase3StatisticsError(
            "annotation record order differs from the cohort",
            safe_stage="P3E4_ANNOTATION",
        )
    if len(annotations) != len(records):
        raise Phase3StatisticsError(
            "annotation trace IDs are not unique",
            safe_stage="P3E4_ANNOTATION",
        )
    for trace_id, record in annotations.items():
        trace = cohort.traces_by_id[trace_id]
        if (
            record.code_sha256 != trace.code_sha256
            or record.structured_explanation_sha256 != trace.structured_explanation_sha256
            or record.functional_evidence_sha256
            != trace.functional_evidence.functional_evidence_sha256
            or record.annotation_protocol_sha256 != binding.manifest.annotation_protocol_sha256
        ):
            raise Phase3StatisticsError(
                "annotation record identity differs from its frozen trace",
                safe_stage="P3E4_ANNOTATION",
            )
    return (
        annotations,
        binding.manifest.annotation_set_id,
        binding.manifest_sha256,
        binding.manifest.completed_labels_sha256,
        binding.manifest.annotation_records_sha256,
    )


def _load_outcome_rows(payload: bytes, *, run_id: str, label: str) -> list[MethodOutcome]:
    rows = _decode_jsonl_models(payload, label=label, model=MethodOutcome)
    assert all(isinstance(item, MethodOutcome) for item in rows)
    if any(item.run_id != run_id for item in rows):
        raise Phase3StatisticsError(
            f"{label} run identity differs",
            safe_stage="P3E4_RUN",
        )
    return rows


def _resolve_effective_outcomes(
    *,
    run_dir: Path,
    manifest: Phase3RunManifest,
    final_rows: Sequence[MethodOutcome],
    final_row_hashes: Sequence[str],
    expected_pairs: Sequence[tuple[str, MethodId]],
) -> Mapping[tuple[str, MethodId], _EffectiveOutcome]:
    by_hash: dict[str, MethodOutcome] = {}
    effective_by_hash: dict[str, MethodOutcome] = {}
    latest_hash_by_pair: dict[tuple[str, MethodId], str] = {}
    last_rows: list[MethodOutcome] = []
    last_hashes: list[str] = []
    for invocation in manifest.invocations:
        path = run_dir / "invocations" / invocation.invocation_id / "results.jsonl"
        payload = _read_regular_file(path, label="invocation results", private=True)
        rows = _load_outcome_rows(
            payload,
            run_id=manifest.run_id,
            label="invocation results",
        )
        pairs = tuple((item.trace_id, item.method_id) for item in rows)
        if pairs != tuple(expected_pairs[: len(rows)]):
            raise Phase3StatisticsError(
                "invocation results are not a trace-major prefix",
                safe_stage="P3E4_RUN",
            )
        invocation_hashes: list[str] = []
        for raw, row in zip(payload.splitlines(keepends=True), rows, strict=True):
            row_sha = jsonl_record_sha256(raw)
            if row_sha in by_hash:
                raise Phase3StatisticsError(
                    "invocation result row is duplicated",
                    safe_stage="P3E4_RUN",
                )
            pair = (row.trace_id, row.method_id)
            if row.status == MethodOutcomeStatus.REUSED:
                source_sha = row.reused_from_result_sha256
                if source_sha is None or latest_hash_by_pair.get(pair) != source_sha:
                    raise Phase3StatisticsError(
                        "reused outcome does not reference the latest prior pair",
                        safe_stage="P3E4_RUN",
                    )
                effective = effective_by_hash.get(source_sha)
                if effective is None:
                    raise Phase3StatisticsError(
                        "reused outcome provenance cannot be resolved",
                        safe_stage="P3E4_RUN",
                    )
            else:
                if pair in latest_hash_by_pair:
                    raise Phase3StatisticsError(
                        "resume history re-executed a terminal pair",
                        safe_stage="P3E4_RUN",
                    )
                effective = row
            by_hash[row_sha] = row
            effective_by_hash[row_sha] = effective
            latest_hash_by_pair[pair] = row_sha
            invocation_hashes.append(row_sha)
        last_rows = list(rows)
        last_hashes = invocation_hashes

    if tuple(last_rows) != tuple(final_rows) or tuple(last_hashes) != tuple(final_row_hashes):
        raise Phase3StatisticsError(
            "final paired results differ from the completed invocation",
            safe_stage="P3E4_RUN",
        )

    resolved: dict[tuple[str, MethodId], _EffectiveOutcome] = {}
    for final, final_sha in zip(final_rows, final_row_hashes, strict=True):
        effective = effective_by_hash[final_sha]
        resolved[(final.trace_id, final.method_id)] = _EffectiveOutcome(
            final=final,
            effective=effective,
        )
    return resolved


def _load_run(
    *,
    paired_run_dir: Path,
    expected_manifest_sha256: str,
    expected_results_sha256: str,
    expected_index_sha256: str,
    cohort: LoadedPairedCohort,
    annotation_manifest_sha256: str,
    completed_labels_sha256: str,
    annotation_records_sha256: str,
    protocol_sha256: str,
) -> tuple[
    Phase3RunManifest,
    Mapping[tuple[str, MethodId], _EffectiveOutcome],
    str,
    str,
    str,
    Mapping[str, int],
]:
    for value, label in (
        (expected_manifest_sha256, "paired run manifest SHA256"),
        (expected_results_sha256, "paired results SHA256"),
        (expected_index_sha256, "paired index SHA256"),
    ):
        if not _SHA256_PATTERN.fullmatch(value):
            raise Phase3StatisticsError(f"{label} is invalid", safe_stage="P3E4_RUN")
    if paired_run_dir.is_symlink() or not paired_run_dir.is_dir():
        raise Phase3StatisticsError("paired run directory is unsafe", safe_stage="P3E4_RUN")
    try:
        if stat.S_IMODE(paired_run_dir.stat().st_mode) & 0o077:
            raise Phase3StatisticsError(
                "paired run directory permissions are too broad",
                safe_stage="P3E4_RUN",
            )
    except OSError:
        raise Phase3StatisticsError(
            "cannot inspect paired run directory",
            safe_stage="P3E4_RUN",
        ) from None

    manifest_payload = _read_regular_file(
        paired_run_dir / "manifest.json",
        label="paired run manifest",
        private=True,
    )
    manifest_sha = hashlib.sha256(manifest_payload).hexdigest()
    if manifest_sha != expected_manifest_sha256:
        raise Phase3StatisticsError("paired run manifest hash differs", safe_stage="P3E4_RUN")
    try:
        manifest = Phase3RunManifest.model_validate(
            _decode_json(manifest_payload, label="paired run manifest")
        )
    except ValidationError:
        raise Phase3StatisticsError(
            "paired run manifest failed schema validation",
            safe_stage="P3E4_RUN",
        ) from None
    if manifest.status != "completed":
        raise Phase3StatisticsError("paired run is not completed", safe_stage="P3E4_RUN")
    identity = manifest.resume_identity
    if (
        manifest.frozen_manifest_sha256 != cohort.overlay_manifest_sha256
        or identity.frozen_manifest_sha256 != cohort.overlay_manifest_sha256
        or identity.natural_manifest_sha256 != cohort.natural_manifest_sha256
        or identity.ordered_trace_ids_sha256 != canonical_sha256(cohort.ordered_trace_ids)
        or identity.annotation_set_manifest_sha256 != annotation_manifest_sha256
        or identity.completed_labels_sha256 != completed_labels_sha256
        or identity.annotation_records_sha256 != annotation_records_sha256
        or identity.annotation_protocol_sha256 != protocol_sha256
        or manifest.resume_identity_sha256 != canonical_sha256(identity)
    ):
        raise Phase3StatisticsError(
            "paired run identity differs from frozen analysis inputs",
            safe_stage="P3E4_RUN",
        )

    results_payload = _read_regular_file(
        paired_run_dir / "results.jsonl",
        label="paired results",
        private=True,
    )
    results_sha = hashlib.sha256(results_payload).hexdigest()
    if results_sha != expected_results_sha256:
        raise Phase3StatisticsError("paired results hash differs", safe_stage="P3E4_RUN")
    final_rows = _load_outcome_rows(
        results_payload,
        run_id=manifest.run_id,
        label="paired results",
    )
    expected_pairs = tuple(
        (trace_id, method_id) for trace_id in cohort.ordered_trace_ids for method_id in MethodId
    )
    if tuple((item.trace_id, item.method_id) for item in final_rows) != expected_pairs:
        raise Phase3StatisticsError(
            "paired results are not the complete trace-major product",
            safe_stage="P3E4_RUN",
        )
    final_hashes = tuple(
        jsonl_record_sha256(raw) for raw in results_payload.splitlines(keepends=True)
    )

    index_payload = _read_regular_file(
        paired_run_dir / "index.json",
        label="paired index",
        private=True,
    )
    index_sha = hashlib.sha256(index_payload).hexdigest()
    if index_sha != expected_index_sha256:
        raise Phase3StatisticsError("paired index hash differs", safe_stage="P3E4_RUN")
    try:
        index = PairedEvaluationIndex.model_validate(
            _decode_json(index_payload, label="paired index")
        )
    except ValidationError:
        raise Phase3StatisticsError(
            "paired index failed schema validation",
            safe_stage="P3E4_RUN",
        ) from None
    if (
        index.run_id != manifest.run_id
        or index.frozen_manifest_sha256 != cohort.overlay_manifest_sha256
        or index.resume_identity_sha256 != manifest.resume_identity_sha256
        or index.ordered_trace_ids != cohort.ordered_trace_ids
        or index.results_sha256 != results_sha
    ):
        raise Phase3StatisticsError(
            "paired index identity differs",
            safe_stage="P3E4_RUN",
        )
    for reference, outcome, row_sha in zip(
        index.result_references,
        final_rows,
        final_hashes,
        strict=True,
    ):
        if (
            reference.trace_id != outcome.trace_id
            or reference.method_id != outcome.method_id
            or reference.status != outcome.status
            or reference.result_record_sha256 != row_sha
        ):
            raise Phase3StatisticsError(
                "paired index row reference differs from results",
                safe_stage="P3E4_RUN",
            )

    effective = _resolve_effective_outcomes(
        run_dir=paired_run_dir,
        manifest=manifest,
        final_rows=final_rows,
        final_row_hashes=final_hashes,
        expected_pairs=expected_pairs,
    )
    status_counts = Counter(item.status.value for item in final_rows)
    return (
        manifest,
        effective,
        manifest_sha,
        results_sha,
        index_sha,
        {status.value: status_counts[status.value] for status in MethodOutcomeStatus},
    )


def _scope_trace_ids(cohort: LoadedPairedCohort, scope: str) -> tuple[str, ...]:
    if scope == "all":
        return cohort.ordered_trace_ids
    return tuple(
        trace_id
        for trace_id in cohort.ordered_trace_ids
        if cohort.traces_by_id[trace_id].trace_kind == scope
    )


def _prediction_is_correct(outcome: _EffectiveOutcome, gold: AnnotationRecord) -> bool:
    effective = outcome.effective
    return (
        effective.status == MethodOutcomeStatus.VALID_JUDGMENT
        and effective.judgment is not None
        and effective.judgment.has_error == gold.has_error
    )


def _method_scope_metrics(
    *,
    trace_ids: Sequence[str],
    method_id: MethodId,
    annotations: Mapping[str, AnnotationRecord],
    outcomes: Mapping[tuple[str, MethodId], _EffectiveOutcome],
) -> dict[str, Any]:
    selected = [(annotations[trace_id], outcomes[(trace_id, method_id)]) for trace_id in trace_ids]
    total = len(selected)
    final_statuses = Counter(item.final.status.value for _gold, item in selected)
    effective_statuses = Counter(item.effective.status.value for _gold, item in selected)
    valid = [
        (gold, item.effective.judgment)
        for gold, item in selected
        if item.effective.status == MethodOutcomeStatus.VALID_JUDGMENT
        and item.effective.judgment is not None
    ]
    detection_correct = sum(_prediction_is_correct(item, gold) for gold, item in selected)
    gold_positive = sum(gold.has_error for gold, _item in selected)
    true_positive = sum(gold.has_error and judgment.has_error for gold, judgment in valid)
    true_negative = sum(not gold.has_error and not judgment.has_error for gold, judgment in valid)
    false_positive = sum(not gold.has_error and judgment.has_error for gold, judgment in valid)
    false_negative = sum(gold.has_error and not judgment.has_error for gold, judgment in valid)

    reasoning_correct = sum(
        item.effective.status == MethodOutcomeStatus.VALID_JUDGMENT
        and item.effective.judgment is not None
        and item.effective.judgment.reasoning_correct is not None
        and item.effective.judgment.reasoning_correct == gold.reasoning_correct
        for gold, item in selected
    )
    alignment_correct = sum(
        item.effective.status == MethodOutcomeStatus.VALID_JUDGMENT
        and item.effective.judgment is not None
        and item.effective.judgment.plan_code_aligned is not None
        and item.effective.judgment.plan_code_aligned == gold.plan_code_aligned
        for gold, item in selected
    )
    process_correct = sum(
        item.effective.status == MethodOutcomeStatus.VALID_JUDGMENT
        and item.effective.judgment is not None
        and item.effective.judgment.process_correct is not None
        and item.effective.judgment.process_correct == gold.process_correct
        for gold, item in selected
    )
    positive_rows = [(gold, item) for gold, item in selected if gold.has_error]
    layer_correct = sum(
        item.effective.status == MethodOutcomeStatus.VALID_JUDGMENT
        and item.effective.judgment is not None
        and item.effective.judgment.has_error
        and item.effective.judgment.first_faulty_layer == gold.first_faulty_layer
        for gold, item in positive_rows
    )
    type_correct = sum(
        item.effective.status == MethodOutcomeStatus.VALID_JUDGMENT
        and item.effective.judgment is not None
        and item.effective.judgment.has_error
        and item.effective.judgment.error_type == gold.error_type
        for gold, item in positive_rows
    )
    step_rows = [(gold, item) for gold, item in positive_rows if gold.first_faulty_step is not None]
    step_correct = sum(
        item.effective.status == MethodOutcomeStatus.VALID_JUDGMENT
        and item.effective.judgment is not None
        and item.effective.judgment.has_error
        and item.effective.judgment.first_faulty_step == gold.first_faulty_step
        for gold, item in step_rows
    )
    precision_denominator = true_positive + false_positive
    recall_denominator = true_positive + false_negative
    precision = _proportion(true_positive, precision_denominator)
    recall = _proportion(true_positive, recall_denominator)
    f1_denominator = 2 * true_positive + false_positive + false_negative
    f1 = 2 * true_positive / f1_denominator if f1_denominator else None
    verdict_counts = Counter(
        judgment.verdict
        for _gold, judgment in valid
        if judgment.has_error and judgment.verdict is not None
    )
    return {
        "denominator": total,
        "gold_error_prevalence": _proportion(gold_positive, total),
        "judgment_availability": _proportion(len(valid), total),
        "invalid_effective_outcomes": total - len(valid),
        "final_status_counts": {
            status.value: final_statuses[status.value] for status in MethodOutcomeStatus
        },
        "effective_status_counts": {
            status.value: effective_statuses[status.value] for status in MethodOutcomeStatus
        },
        "error_detection_accuracy_full_denominator": _proportion(detection_correct, total),
        "process_correct_accuracy_full_denominator": _proportion(process_correct, total),
        "reasoning_accuracy_full_denominator": _proportion(reasoning_correct, total),
        "plan_code_alignment_accuracy_full_denominator": _proportion(
            alignment_correct,
            total,
        ),
        "first_faulty_layer_accuracy_gold_errors": _proportion(
            layer_correct,
            len(positive_rows),
        ),
        "first_faulty_step_accuracy_labeled_gold_steps": _proportion(
            step_correct,
            len(step_rows),
        ),
        "error_type_accuracy_gold_errors": _proportion(type_correct, len(positive_rows)),
        "valid_only_confusion": {
            "true_positive": true_positive,
            "true_negative": true_negative,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "valid_prediction_count": len(valid),
            "precision": precision,
            "recall": recall,
            "f1": f1,
        },
        "valid_error_verdict_counts": {
            "confirmed_bug": verdict_counts["confirmed_bug"],
            "strongly_supported": verdict_counts["strongly_supported"],
            "unverified_suspicion": verdict_counts["unverified_suspicion"],
        },
        "duration_seconds_total": sum(item.final.duration_seconds for _gold, item in selected),
    }


def _primary_comparisons(
    *,
    loaded: _LoadedStatisticsInputs,
) -> dict[str, Any]:
    full = MethodId.FULL_TRACEJUDGE
    natural_ids = _scope_trace_ids(loaded.cohort, "natural")
    counterfactual_ids = _scope_trace_ids(loaded.cohort, "counterfactual")

    natural_rows: list[dict[str, Any]] = []
    raw_p_values: list[float] = []
    for baseline in _PRIMARY_BASELINES:
        n01 = 0
        n10 = 0
        full_correct_count = 0
        baseline_correct_count = 0
        for trace_id in natural_ids:
            gold = loaded.annotations[trace_id]
            baseline_correct = _prediction_is_correct(
                loaded.effective_outcomes[(trace_id, baseline)],
                gold,
            )
            full_correct = _prediction_is_correct(
                loaded.effective_outcomes[(trace_id, full)],
                gold,
            )
            baseline_correct_count += baseline_correct
            full_correct_count += full_correct
            n01 += (not baseline_correct) and full_correct
            n10 += baseline_correct and (not full_correct)
        p_value = exact_mcnemar_p_value(n01, n10)
        raw_p_values.append(p_value)
        natural_rows.append(
            {
                "comparison": f"full_tracejudge_vs_{baseline.value}",
                "denominator": len(natural_ids),
                "full_correct": full_correct_count,
                "baseline_correct": baseline_correct_count,
                "accuracy_difference_full_minus_baseline": (
                    (full_correct_count - baseline_correct_count) / len(natural_ids)
                ),
                "n01_baseline_incorrect_full_correct": n01,
                "n10_baseline_correct_full_incorrect": n10,
                "exact_two_sided_mcnemar_p_value": p_value,
            }
        )
    for row, adjusted in zip(natural_rows, holm_adjust(raw_p_values), strict=True):
        row["holm_adjusted_p_value"] = adjusted

    counterfactual_rows: list[dict[str, Any]] = []
    for baseline in _PRIMARY_BASELINES:
        clusters: dict[str, list[int]] = {}
        full_correct_count = 0
        baseline_correct_count = 0
        for trace_id in counterfactual_ids:
            trace = loaded.cohort.traces_by_id[trace_id]
            assert isinstance(trace, CounterfactualTrace)
            gold = loaded.annotations[trace_id]
            baseline_correct = _prediction_is_correct(
                loaded.effective_outcomes[(trace_id, baseline)],
                gold,
            )
            full_correct = _prediction_is_correct(
                loaded.effective_outcomes[(trace_id, full)],
                gold,
            )
            baseline_correct_count += baseline_correct
            full_correct_count += full_correct
            clusters.setdefault(trace.parent_trace_id, []).append(
                int(full_correct) - int(baseline_correct)
            )
        lower, upper = cluster_bootstrap_interval(
            clusters,
            iterations=loaded.protocol.bootstrap_iterations,
            seed=loaded.protocol.bootstrap_seed,
            confidence_level=loaded.protocol.confidence_level,
        )
        counterfactual_rows.append(
            {
                "comparison": f"full_tracejudge_vs_{baseline.value}",
                "denominator": len(counterfactual_ids),
                "parent_cluster_count": len(clusters),
                "full_correct": full_correct_count,
                "baseline_correct": baseline_correct_count,
                "accuracy_difference_full_minus_baseline": (
                    (full_correct_count - baseline_correct_count) / len(counterfactual_ids)
                ),
                "cluster_bootstrap_95_lower": lower,
                "cluster_bootstrap_95_upper": upper,
                "bootstrap_iteration_count": loaded.protocol.bootstrap_iterations,
                "bootstrap_seed": loaded.protocol.bootstrap_seed,
                "percentile_rule": "type7_linear_interpolation",
            }
        )
    return {
        "natural": natural_rows,
        "counterfactual": counterfactual_rows,
    }


def _build_report(
    *,
    statistics_id: str,
    loaded: _LoadedStatisticsInputs,
    implementation_sha256: str,
) -> dict[str, Any]:
    method_metrics: list[dict[str, Any]] = []
    for method_id in MethodId:
        method_metrics.append(
            {
                "method_id": method_id.value,
                "scopes": {
                    scope: _method_scope_metrics(
                        trace_ids=_scope_trace_ids(loaded.cohort, scope),
                        method_id=method_id,
                        annotations=loaded.annotations,
                        outcomes=loaded.effective_outcomes,
                    )
                    for scope in _SCOPES
                },
            }
        )

    mutation_breakdown: list[dict[str, Any]] = []
    for mutation_kind in CounterfactualKind:
        trace_ids = tuple(
            trace_id
            for trace_id in _scope_trace_ids(loaded.cohort, "counterfactual")
            if isinstance(loaded.cohort.traces_by_id[trace_id], CounterfactualTrace)
            and loaded.cohort.traces_by_id[trace_id].mutation.mutation_kind == mutation_kind
        )
        mutation_breakdown.append(
            {
                "mutation_kind": mutation_kind.value,
                "trace_count": len(trace_ids),
                "method_error_detection": [
                    {
                        "method_id": method_id.value,
                        "accuracy_full_denominator": _method_scope_metrics(
                            trace_ids=trace_ids,
                            method_id=method_id,
                            annotations=loaded.annotations,
                            outcomes=loaded.effective_outcomes,
                        )["error_detection_accuracy_full_denominator"],
                    }
                    for method_id in MethodId
                ],
            }
        )

    report = {
        "schema_version": 1,
        "kind": "tracejudge_phase3_paired_statistics",
        "statistics_id": statistics_id,
        "source_run_completed_at": loaded.run_manifest.completed_at,
        "identities": {
            "frozen_cohort_manifest_sha256": loaded.cohort.overlay_manifest_sha256,
            "natural_manifest_sha256": loaded.cohort.natural_manifest_sha256,
            "annotation_set_manifest_sha256": loaded.annotation_manifest_sha256,
            "completed_labels_sha256": loaded.completed_labels_sha256,
            "annotation_records_sha256": loaded.annotation_records_sha256,
            "paired_run_id": loaded.run_manifest.run_id,
            "paired_run_manifest_sha256": loaded.run_manifest_sha256,
            "paired_results_sha256": loaded.results_sha256,
            "paired_index_sha256": loaded.index_sha256,
            "paired_resume_identity_sha256": loaded.run_manifest.resume_identity_sha256,
            "annotation_protocol_sha256": loaded.run_manifest.resume_identity.annotation_protocol_sha256,
            "statistics_implementation_sha256": implementation_sha256,
        },
        "analysis_contract": {
            "positive_class": loaded.protocol.positive_class,
            "invalid_method_outcome_policy": loaded.protocol.invalid_method_outcome_policy,
            "natural_binary_test": loaded.protocol.natural_binary_test,
            "counterfactual_interval": loaded.protocol.counterfactual_interval,
            "multiple_comparison_policy": loaded.protocol.multiple_comparison_policy,
            "confidence_level": loaded.protocol.confidence_level,
            "exploratory_only": loaded.protocol.exploratory_only,
        },
        "cohort": {
            "natural_trace_count": loaded.cohort.natural_trace_count,
            "counterfactual_trace_count": loaded.cohort.counterfactual_trace_count,
            "trace_count": len(loaded.cohort.ordered_trace_ids),
            "method_count": len(MethodId),
            "pair_count": len(loaded.effective_outcomes),
            "counterfactual_parent_cluster_count": len(
                {
                    trace.parent_trace_id
                    for trace in loaded.cohort.traces_by_id.values()
                    if isinstance(trace, CounterfactualTrace)
                }
            ),
            "final_status_counts": dict(loaded.final_status_counts),
        },
        "method_metrics": method_metrics,
        "primary_comparisons": _primary_comparisons(loaded=loaded),
        "counterfactual_mutation_breakdown": mutation_breakdown,
        "limitations": [
            "Exploratory analysis only; the cohort was not sized for definitive inference.",
            "One primary rater and one round are frozen, so agreement is not computed.",
            "Invalid method outcomes remain in the full denominator as incorrect and are reported separately.",
            "Valid-only confusion counts exclude invalid outcomes and must not replace the full-denominator endpoint.",
            "The counterfactual interval has only three parent-problem clusters and is therefore unstable.",
            "A non-significant comparison is not evidence of equivalence.",
            "Associations in this frozen evaluation do not establish causal effects.",
            "Verdict levels are judge outputs, not independently replayed certificate-validity claims.",
        ],
        "material_passport": {
            "origin_skill": "academic-research-suite/experiment-agent",
            "origin_mode": "validate_engineering",
            "date": "2026-08-31",
            "verification_status": "hash_bound_aggregate_analysis",
            "version_label": "phase3_gate_e4_v1",
        },
    }
    assert_public_payload_safe(report)
    return report


def _pretty_json(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
            default=lambda item: item.isoformat() if isinstance(item, datetime) else str(item),
        ).encode("utf-8")
        + b"\n"
    )


def _resolve_output(*, output_dir: str | Path, statistics_id: str) -> tuple[Path, Path]:
    if not _ID_PATTERN.fullmatch(statistics_id):
        raise Phase3StatisticsError(
            "statistics_id contains unsupported characters",
            safe_stage="P3E4_OUTPUT",
        )
    root = Path(output_dir).expanduser()
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise Phase3StatisticsError("statistics output root is unsafe", safe_stage="P3E4_OUTPUT")
    resolved = root.resolve()
    run_dir = resolved / statistics_id
    if run_dir.exists() or run_dir.is_symlink():
        raise Phase3StatisticsError(
            "statistics directory already exists",
            safe_stage="P3E4_OUTPUT",
        )
    return resolved, run_dir


def _load_inputs(
    *,
    paired_run_dir: str | Path,
    expected_paired_run_manifest_sha256: str,
    expected_results_sha256: str,
    expected_index_sha256: str,
    cohort_manifest_path: str | Path,
    natural_manifest_path: str | Path,
    annotation_set_manifest_path: str | Path,
    expected_annotation_set_manifest_sha256: str,
    protocol_path: str | Path,
    guide_path: str | Path,
) -> _LoadedStatisticsInputs:
    try:
        cohort = load_paired_cohort(
            overlay_manifest_path=cohort_manifest_path,
            natural_manifest_path=natural_manifest_path,
        )
    except Phase3RunnerError as exc:
        raise Phase3StatisticsError(
            "cohort failed frozen binding",
            safe_stage=exc.safe_stage,
        ) from None
    protocol = _load_protocol(protocol_path=protocol_path, guide_path=guide_path)
    if protocol.frozen_cohort_manifest_sha256 != cohort.overlay_manifest_sha256:
        raise Phase3StatisticsError(
            "protocol references a different cohort",
            safe_stage="P3E4_PROTOCOL",
        )
    annotation_path = Path(annotation_set_manifest_path)
    (
        annotations,
        annotation_set_id,
        annotation_manifest_sha,
        completed_labels_sha,
        annotation_records_sha,
    ) = _load_annotations(
        annotation_manifest_path=annotation_path,
        expected_manifest_sha256=expected_annotation_set_manifest_sha256,
        cohort=cohort,
        protocol_path=protocol_path,
        guide_path=guide_path,
    )
    protocol_sha = run_manifest_protocol_sha(protocol_path)
    (
        run_manifest,
        outcomes,
        run_manifest_sha,
        results_sha,
        index_sha,
        status_counts,
    ) = _load_run(
        paired_run_dir=Path(paired_run_dir),
        expected_manifest_sha256=expected_paired_run_manifest_sha256,
        expected_results_sha256=expected_results_sha256,
        expected_index_sha256=expected_index_sha256,
        cohort=cohort,
        annotation_manifest_sha256=annotation_manifest_sha,
        completed_labels_sha256=completed_labels_sha,
        annotation_records_sha256=annotation_records_sha,
        protocol_sha256=protocol_sha,
    )
    return _LoadedStatisticsInputs(
        cohort=cohort,
        protocol=protocol,
        annotations=annotations,
        run_manifest=run_manifest,
        effective_outcomes=outcomes,
        run_manifest_sha256=run_manifest_sha,
        results_sha256=results_sha,
        index_sha256=index_sha,
        annotation_set_id=annotation_set_id,
        annotation_manifest_sha256=annotation_manifest_sha,
        completed_labels_sha256=completed_labels_sha,
        annotation_records_sha256=annotation_records_sha,
        final_status_counts=status_counts,
    )


def run_manifest_protocol_sha(protocol_path: str | Path) -> str:
    """Return the exact frozen protocol file identity."""

    return hashlib.sha256(
        _read_regular_file(Path(protocol_path), label="annotation protocol")
    ).hexdigest()


def _prepare_statistics(
    *,
    statistics_id: str,
    paired_run_dir: str | Path,
    expected_paired_run_manifest_sha256: str,
    expected_results_sha256: str,
    expected_index_sha256: str,
    cohort_manifest_path: str | Path,
    natural_manifest_path: str | Path,
    annotation_set_manifest_path: str | Path,
    expected_annotation_set_manifest_sha256: str,
    protocol_path: str | Path,
    guide_path: str | Path,
    output_dir: str | Path,
    allow_dirty: bool = False,
    privacy_canaries: Sequence[str | bytes] = (),
) -> _PreparedStatistics:
    loaded = _load_inputs(
        paired_run_dir=paired_run_dir,
        expected_paired_run_manifest_sha256=expected_paired_run_manifest_sha256,
        expected_results_sha256=expected_results_sha256,
        expected_index_sha256=expected_index_sha256,
        cohort_manifest_path=cohort_manifest_path,
        natural_manifest_path=natural_manifest_path,
        annotation_set_manifest_path=annotation_set_manifest_path,
        expected_annotation_set_manifest_sha256=expected_annotation_set_manifest_sha256,
        protocol_path=protocol_path,
        guide_path=guide_path,
    )
    output_root, run_dir = _resolve_output(output_dir=output_dir, statistics_id=statistics_id)
    git = _git_metadata(Path.cwd(), excluded_paths=(output_root,))
    if (
        not git["available"]
        or not isinstance(git["commit"], str)
        or not isinstance(git["branch"], str)
        or not isinstance(git["dirty"], bool)
    ):
        raise Phase3StatisticsError(
            "Git identity is unavailable",
            safe_stage="P3E4_GIT",
        )
    if git["dirty"] and not allow_dirty:
        raise Phase3StatisticsError(
            "Git worktree is dirty; commit the Gate-E4 implementation before formal statistics",
            safe_stage="P3E4_GIT_DIRTY",
        )
    implementation_sha = statistics_implementation_sha256()
    report = _build_report(
        statistics_id=statistics_id,
        loaded=loaded,
        implementation_sha256=implementation_sha,
    )
    assert_public_payload_safe(report, canaries=privacy_canaries)
    report_payload = _pretty_json(report)
    assert_public_payload_safe(report_payload, canaries=privacy_canaries)
    preflight = Phase3StatisticsPreflight(
        statistics_id=statistics_id,
        paired_run_id=loaded.run_manifest.run_id,
        annotation_set_id=loaded.annotation_set_id,
        natural_trace_count=loaded.cohort.natural_trace_count,
        counterfactual_trace_count=loaded.cohort.counterfactual_trace_count,
        trace_count=len(loaded.cohort.ordered_trace_ids),
        method_count=len(MethodId),
        pair_count=len(loaded.effective_outcomes),
        final_status_counts=loaded.final_status_counts,
        paired_run_manifest_sha256=loaded.run_manifest_sha256,
        paired_results_sha256=loaded.results_sha256,
        paired_index_sha256=loaded.index_sha256,
        annotation_set_manifest_sha256=loaded.annotation_manifest_sha256,
        completed_labels_sha256=loaded.completed_labels_sha256,
        annotation_records_sha256=loaded.annotation_records_sha256,
        protocol_sha256=run_manifest_protocol_sha(protocol_path),
        statistics_implementation_sha256=implementation_sha,
        report_sha256=hashlib.sha256(report_payload).hexdigest(),
        git_commit=git["commit"],
        git_branch=git["branch"],
        git_dirty=git["dirty"],
    )
    return _PreparedStatistics(
        preflight=preflight,
        report=report,
        report_payload=report_payload,
        output_root=output_root,
        run_dir=run_dir,
    )


def preflight_phase3_statistics(**kwargs: Any) -> Phase3StatisticsPreflight:
    """Validate and calculate Gate-E4 in memory without writing artifacts."""

    return _prepare_statistics(**kwargs).preflight


def _write_new_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def generate_phase3_statistics(**kwargs: Any) -> Phase3StatisticsResult:
    """Atomically publish one deterministic aggregate Gate-E4 report."""

    prepared = _prepare_statistics(**kwargs)
    if hashlib.sha256(prepared.report_payload).hexdigest() != prepared.preflight.report_sha256:
        raise Phase3StatisticsError(
            "prepared report identity differs",
            safe_stage="P3E4_OUTPUT",
        )
    prepared.output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    prepared.output_root.chmod(0o700)
    temporary_dir: Path | None = None
    try:
        temporary_dir = Path(
            tempfile.mkdtemp(
                prefix=f".{prepared.preflight.statistics_id}.",
                dir=prepared.output_root,
            )
        )
        temporary_dir.chmod(0o700)
        report_path = temporary_dir / "report.json"
        _write_new_file(report_path, prepared.report_payload)
        dependencies = _dependency_versions()
        manifest = {
            "schema_version": 1,
            "phase": "phase3_paired_statistics",
            "status": "completed",
            "statistics_id": prepared.preflight.statistics_id,
            "created_at": datetime.now(UTC),
            "paired_run_id": prepared.preflight.paired_run_id,
            "paired_run_manifest_sha256": prepared.preflight.paired_run_manifest_sha256,
            "paired_results_sha256": prepared.preflight.paired_results_sha256,
            "paired_index_sha256": prepared.preflight.paired_index_sha256,
            "annotation_set_manifest_sha256": (prepared.preflight.annotation_set_manifest_sha256),
            "completed_labels_sha256": prepared.preflight.completed_labels_sha256,
            "annotation_records_sha256": prepared.preflight.annotation_records_sha256,
            "annotation_protocol_sha256": prepared.preflight.protocol_sha256,
            "statistics_implementation_sha256": (
                prepared.preflight.statistics_implementation_sha256
            ),
            "report_sha256": prepared.preflight.report_sha256,
            "git_commit": prepared.preflight.git_commit,
            "git_branch": prepared.preflight.git_branch,
            "git_dirty": prepared.preflight.git_dirty,
            "python_version": platform.python_version(),
            "direct_dependencies_sha256": canonical_sha256(dependencies),
            "contains_per_trace_rows": False,
            "contains_annotation_rationales": False,
            "contains_provider_raw": False,
            "contains_hidden_evaluation_content": False,
        }
        assert_public_payload_safe(manifest)
        manifest_payload = _pretty_json(manifest)
        _write_new_file(temporary_dir / "manifest.json", manifest_payload)
        os.replace(temporary_dir, prepared.run_dir)
        temporary_dir = None
        _fsync_directory(prepared.output_root)
    except OSError:
        raise Phase3StatisticsError(
            "cannot atomically publish statistics",
            safe_stage="P3E4_OUTPUT",
        ) from None
    finally:
        if temporary_dir is not None:
            shutil.rmtree(temporary_dir, ignore_errors=True)

    manifest_path = prepared.run_dir / "manifest.json"
    return Phase3StatisticsResult(
        **asdict(prepared.preflight),
        run_dir=prepared.run_dir,
        manifest_path=manifest_path,
        report_path=prepared.run_dir / "report.json",
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )


__all__ = [
    "Phase3StatisticsError",
    "Phase3StatisticsPreflight",
    "Phase3StatisticsResult",
    "cluster_bootstrap_interval",
    "exact_mcnemar_p_value",
    "generate_phase3_statistics",
    "holm_adjust",
    "percentile_type7",
    "preflight_phase3_statistics",
    "statistics_implementation_sha256",
    "wilson_interval",
]
