"""Deterministic, aggregate-only public charts for phase-four release evidence."""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from tracejudge_hy3.phase3.privacy import assert_public_payload_safe

from .contracts import (
    Phase4GitIdentity,
    Phase4PublicChartArtifact,
    Phase4PublicChartCohort,
    Phase4PublicChartProportion,
    Phase4PublicChartsManifest,
    Phase4PublicConfusionSummary,
    Phase4PublicCounterfactualComparison,
    Phase4PublicMethodChartSummary,
    Phase4PublicNaturalComparison,
)
from .reproducibility import _git_identity

_MAX_INPUT_BYTES = 16 * 1024 * 1024
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_METHODS = (
    "test_only",
    "direct_llm_judge",
    "four_layer_structured_judge",
    "four_layer_ast",
    "full_tracejudge",
)
_METHOD_DISPLAY_NAMES = {
    "test_only": "Test-only",
    "direct_llm_judge": "Direct LLM Judge",
    "four_layer_structured_judge": "Four-layer Structured",
    "four_layer_ast": "Four-layer + AST",
    "full_tracejudge": "Full TraceJudge",
}
_COMPARISONS = (
    "full_tracejudge_vs_test_only",
    "full_tracejudge_vs_direct_llm_judge",
)
_FIGURE_TITLES = {
    "01_cohort_and_execution.svg": "Research cohort and paired execution accounting",
    "02_error_detection_by_source.svg": "Full-denominator error detection by source",
    "03_preregistered_paired_comparisons.svg": "Pre-registered paired comparisons",
}


class Phase4ReleaseError(ValueError):
    """Safe, content-free Gate-E release preparation failure."""

    def __init__(self, message: str, *, safe_stage: str = "P4E_RELEASE") -> None:
        super().__init__(message)
        self.safe_stage = safe_stage


@dataclass(frozen=True, slots=True)
class Phase4ChartsPreflight:
    manifest: Phase4PublicChartsManifest
    manifest_payload: bytes
    manifest_sha256: str
    figure_payloads: Mapping[str, bytes]


@dataclass(frozen=True, slots=True)
class Phase4ChartsResult:
    chart_bundle_id: str
    run_dir: Path
    manifest_path: Path
    manifest_sha256: str
    figure_paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class Phase4ChartsVerificationResult:
    chart_bundle_id: str
    figure_count: int
    verified: bool


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


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _pretty_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True).encode(
            "utf-8"
        )
        + b"\n"
    )


def _read_regular_file(path: Path, *, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise Phase4ReleaseError(
            f"{label} must be a regular non-symlink file",
            safe_stage="P4E_INPUT",
        )
    try:
        if path.stat().st_size > _MAX_INPUT_BYTES:
            raise Phase4ReleaseError(
                f"{label} exceeds the size limit",
                safe_stage="P4E_INPUT",
            )
        payload = path.read_bytes()
    except OSError:
        raise Phase4ReleaseError(
            f"cannot read {label}",
            safe_stage="P4E_INPUT",
        ) from None
    if len(payload) > _MAX_INPUT_BYTES:
        raise Phase4ReleaseError(
            f"{label} exceeds the size limit",
            safe_stage="P4E_INPUT",
        )
    return payload


def _absolute_without_resolving_symlinks(path: str | Path) -> Path:
    expanded = Path(path).expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    return expanded.absolute()


def _reject_existing_symlink_components(
    path: Path,
    *,
    label: str,
    safe_stage: str = "P4E_INPUT",
) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise Phase4ReleaseError(
                f"{label} cannot traverse a symbolic link",
                safe_stage=safe_stage,
            )


def _decode_json(payload: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKey, ValueError):
        raise Phase4ReleaseError(
            f"{label} is not strict UTF-8 JSON",
            safe_stage="P4E_INPUT",
        ) from None
    if not isinstance(value, dict):
        raise Phase4ReleaseError(
            f"{label} must contain one JSON object",
            safe_stage="P4E_INPUT",
        )
    return value


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise Phase4ReleaseError(f"{label} is invalid", safe_stage="P4E_SCHEMA")
    return value


def _sequence(value: Any, *, label: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise Phase4ReleaseError(f"{label} is invalid", safe_stage="P4E_SCHEMA")
    return value


def _integer(value: Any, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise Phase4ReleaseError(f"{label} is invalid", safe_stage="P4E_SCHEMA")
    return value


def _number(value: Any, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise Phase4ReleaseError(f"{label} is invalid", safe_stage="P4E_SCHEMA")
    return float(value)


def _proportion(
    value: Any,
    *,
    label: str,
    interval_kind: str,
) -> Phase4PublicChartProportion:
    payload = _mapping(value, label=label)
    kwargs: dict[str, Any] = {
        "numerator": _integer(payload.get("numerator"), label=f"{label} numerator"),
        "denominator": _integer(
            payload.get("denominator"),
            label=f"{label} denominator",
            minimum=1,
        ),
        "estimate": _number(payload.get("estimate"), label=f"{label} estimate"),
        "interval_kind": interval_kind,
    }
    if interval_kind == "wilson_95":
        kwargs["interval_lower"] = _number(
            payload.get("wilson_95_lower"),
            label=f"{label} Wilson lower",
        )
        kwargs["interval_upper"] = _number(
            payload.get("wilson_95_upper"),
            label=f"{label} Wilson upper",
        )
    try:
        return Phase4PublicChartProportion.model_validate(kwargs)
    except ValidationError:
        raise Phase4ReleaseError(
            f"{label} failed the public chart contract",
            safe_stage="P4E_SCHEMA",
        ) from None


def _confusion(value: Any, *, label: str) -> Phase4PublicConfusionSummary:
    payload = _mapping(value, label=label)
    try:
        return Phase4PublicConfusionSummary(
            valid_prediction_count=_integer(
                payload.get("valid_prediction_count"),
                label=f"{label} valid prediction count",
                minimum=1,
            ),
            true_positive=_integer(payload.get("true_positive"), label=f"{label} TP"),
            true_negative=_integer(payload.get("true_negative"), label=f"{label} TN"),
            false_positive=_integer(payload.get("false_positive"), label=f"{label} FP"),
            false_negative=_integer(payload.get("false_negative"), label=f"{label} FN"),
            precision=_proportion(
                payload.get("precision"),
                label=f"{label} precision",
                interval_kind="wilson_95",
            ),
            recall=_proportion(
                payload.get("recall"),
                label=f"{label} recall",
                interval_kind="wilson_95",
            ),
            f1=_number(payload.get("f1"), label=f"{label} F1"),
        )
    except ValidationError:
        raise Phase4ReleaseError(
            f"{label} failed the public confusion contract",
            safe_stage="P4E_SCHEMA",
        ) from None


def _method_summaries(report: Mapping[str, Any]) -> tuple[Phase4PublicMethodChartSummary, ...]:
    rows = _sequence(report.get("method_metrics"), label="method metrics")
    summaries: list[Phase4PublicMethodChartSummary] = []
    for row in rows:
        payload = _mapping(row, label="method metric")
        method_id = payload.get("method_id")
        if method_id not in _METHODS:
            raise Phase4ReleaseError("method ID is invalid", safe_stage="P4E_SCHEMA")
        scopes = _mapping(payload.get("scopes"), label="method scopes")
        all_scope = _mapping(scopes.get("all"), label="all scope")
        natural_scope = _mapping(scopes.get("natural"), label="natural scope")
        counterfactual_scope = _mapping(
            scopes.get("counterfactual"),
            label="counterfactual scope",
        )
        statuses = _mapping(all_scope.get("final_status_counts"), label="method statuses")
        try:
            summaries.append(
                Phase4PublicMethodChartSummary(
                    method_id=method_id,
                    display_name=_METHOD_DISPLAY_NAMES[method_id],
                    judgment_availability=_proportion(
                        all_scope.get("judgment_availability"),
                        label="judgment availability",
                        interval_kind="wilson_95",
                    ),
                    provider_error_count=_integer(
                        statuses.get("provider_error"),
                        label="method Provider errors",
                    ),
                    accuracy_all=_proportion(
                        all_scope.get("error_detection_accuracy_full_denominator"),
                        label="all-scope error detection",
                        interval_kind="wilson_95",
                    ),
                    accuracy_natural=_proportion(
                        natural_scope.get("error_detection_accuracy_full_denominator"),
                        label="natural error detection",
                        interval_kind="wilson_95",
                    ),
                    accuracy_counterfactual=_proportion(
                        counterfactual_scope.get("error_detection_accuracy_full_denominator"),
                        label="counterfactual error detection",
                        interval_kind="descriptive_only",
                    ),
                    valid_only_confusion=_confusion(
                        all_scope.get("valid_only_confusion"),
                        label="valid-only confusion",
                    ),
                )
            )
        except ValidationError:
            raise Phase4ReleaseError(
                "method metric failed the public chart contract",
                safe_stage="P4E_SCHEMA",
            ) from None
    if tuple(item.method_id for item in summaries) != _METHODS:
        raise Phase4ReleaseError(
            "method metrics are incomplete or out of order",
            safe_stage="P4E_SCHEMA",
        )
    return tuple(summaries)


def _cohort_summary(
    report: Mapping[str, Any],
    methods: Sequence[Phase4PublicMethodChartSummary],
) -> Phase4PublicChartCohort:
    payload = _mapping(report.get("cohort"), label="cohort")
    statuses = _mapping(payload.get("final_status_counts"), label="cohort statuses")
    valid = _integer(statuses.get("valid_judgment"), label="valid judgments")
    provider = _integer(statuses.get("provider_error"), label="Provider errors")
    pair_count = _integer(payload.get("pair_count"), label="pair count", minimum=1)
    other_invalid = 0
    for key, value in statuses.items():
        count = _integer(value, label=f"status {key}")
        if key not in {"valid_judgment", "provider_error"}:
            other_invalid += count
    try:
        cohort = Phase4PublicChartCohort(
            natural_trace_count=_integer(
                payload.get("natural_trace_count"),
                label="natural trace count",
                minimum=1,
            ),
            counterfactual_trace_count=_integer(
                payload.get("counterfactual_trace_count"),
                label="counterfactual trace count",
                minimum=1,
            ),
            counterfactual_parent_cluster_count=_integer(
                payload.get("counterfactual_parent_cluster_count"),
                label="counterfactual parent cluster count",
                minimum=1,
            ),
            trace_count=_integer(payload.get("trace_count"), label="trace count", minimum=1),
            method_count=_integer(
                payload.get("method_count"),
                label="method count",
                minimum=1,
            ),
            pair_count=pair_count,
            valid_judgment_count=valid,
            provider_error_count=provider,
            other_invalid_count=other_invalid,
        )
    except ValidationError:
        raise Phase4ReleaseError(
            "cohort failed the public chart contract",
            safe_stage="P4E_SCHEMA",
        ) from None
    if sum(item.provider_error_count for item in methods) != cohort.provider_error_count:
        raise Phase4ReleaseError(
            "method and cohort Provider error counts differ",
            safe_stage="P4E_SCHEMA",
        )
    if cohort.method_count != len(methods):
        raise Phase4ReleaseError(
            "cohort method count differs from chart methods",
            safe_stage="P4E_SCHEMA",
        )
    if sum(item.judgment_availability.numerator for item in methods) != valid:
        raise Phase4ReleaseError(
            "method and cohort valid-judgment counts differ",
            safe_stage="P4E_SCHEMA",
        )
    method_other_invalid = sum(
        item.judgment_availability.denominator
        - item.judgment_availability.numerator
        - item.provider_error_count
        for item in methods
    )
    if method_other_invalid != cohort.other_invalid_count:
        raise Phase4ReleaseError(
            "method and cohort other-invalid counts differ",
            safe_stage="P4E_SCHEMA",
        )
    if any(
        item.judgment_availability.denominator != cohort.trace_count
        or item.accuracy_natural.denominator != cohort.natural_trace_count
        or item.accuracy_counterfactual.denominator != cohort.counterfactual_trace_count
        for item in methods
    ):
        raise Phase4ReleaseError(
            "method denominators differ from cohort source counts",
            safe_stage="P4E_SCHEMA",
        )
    return cohort


def _validate_comparison_accounting(
    *,
    cohort: Phase4PublicChartCohort,
    methods: Sequence[Phase4PublicMethodChartSummary],
    natural: Sequence[Phase4PublicNaturalComparison],
    counterfactual: Sequence[Phase4PublicCounterfactualComparison],
) -> None:
    by_method = {item.method_id: item for item in methods}
    baseline_by_comparison = {
        "full_tracejudge_vs_test_only": "test_only",
        "full_tracejudge_vs_direct_llm_judge": "direct_llm_judge",
    }
    full = by_method["full_tracejudge"]
    for item in natural:
        baseline = by_method[baseline_by_comparison[item.comparison]]
        if (
            item.denominator != cohort.natural_trace_count
            or item.full_correct != full.accuracy_natural.numerator
            or item.baseline_correct != baseline.accuracy_natural.numerator
        ):
            raise Phase4ReleaseError(
                "natural comparison differs from aggregate method metrics",
                safe_stage="P4E_SCHEMA",
            )
    for item in counterfactual:
        baseline = by_method[baseline_by_comparison[item.comparison]]
        if (
            item.denominator != cohort.counterfactual_trace_count
            or item.full_correct != full.accuracy_counterfactual.numerator
            or item.baseline_correct != baseline.accuracy_counterfactual.numerator
            or item.parent_cluster_count != cohort.counterfactual_parent_cluster_count
        ):
            raise Phase4ReleaseError(
                "counterfactual comparison differs from aggregate method metrics",
                safe_stage="P4E_SCHEMA",
            )


def _natural_comparisons(report: Mapping[str, Any]) -> tuple[Phase4PublicNaturalComparison, ...]:
    root = _mapping(report.get("primary_comparisons"), label="primary comparisons")
    rows = _sequence(root.get("natural"), label="natural primary comparisons")
    result: list[Phase4PublicNaturalComparison] = []
    for row in rows:
        payload = _mapping(row, label="natural comparison")
        try:
            result.append(
                Phase4PublicNaturalComparison(
                    comparison=payload.get("comparison"),
                    denominator=_integer(
                        payload.get("denominator"),
                        label="natural comparison denominator",
                        minimum=1,
                    ),
                    full_correct=_integer(
                        payload.get("full_correct"),
                        label="natural Full correct",
                    ),
                    baseline_correct=_integer(
                        payload.get("baseline_correct"),
                        label="natural baseline correct",
                    ),
                    difference_full_minus_baseline=_number(
                        payload.get("accuracy_difference_full_minus_baseline"),
                        label="natural comparison difference",
                    ),
                    n01_baseline_incorrect_full_correct=_integer(
                        payload.get("n01_baseline_incorrect_full_correct"),
                        label="natural n01",
                    ),
                    n10_baseline_correct_full_incorrect=_integer(
                        payload.get("n10_baseline_correct_full_incorrect"),
                        label="natural n10",
                    ),
                    exact_two_sided_mcnemar_p_value=_number(
                        payload.get("exact_two_sided_mcnemar_p_value"),
                        label="natural exact p-value",
                    ),
                    holm_adjusted_p_value=_number(
                        payload.get("holm_adjusted_p_value"),
                        label="natural Holm p-value",
                    ),
                )
            )
        except ValidationError:
            raise Phase4ReleaseError(
                "natural comparison failed the public chart contract",
                safe_stage="P4E_SCHEMA",
            ) from None
    if tuple(item.comparison for item in result) != _COMPARISONS:
        raise Phase4ReleaseError(
            "natural comparisons are incomplete or out of order",
            safe_stage="P4E_SCHEMA",
        )
    return tuple(result)


def _counterfactual_comparisons(
    report: Mapping[str, Any],
) -> tuple[Phase4PublicCounterfactualComparison, ...]:
    root = _mapping(report.get("primary_comparisons"), label="primary comparisons")
    rows = _sequence(root.get("counterfactual"), label="counterfactual comparisons")
    result: list[Phase4PublicCounterfactualComparison] = []
    for row in rows:
        payload = _mapping(row, label="counterfactual comparison")
        try:
            result.append(
                Phase4PublicCounterfactualComparison(
                    comparison=payload.get("comparison"),
                    denominator=_integer(
                        payload.get("denominator"),
                        label="counterfactual comparison denominator",
                        minimum=1,
                    ),
                    full_correct=_integer(
                        payload.get("full_correct"),
                        label="counterfactual Full correct",
                    ),
                    baseline_correct=_integer(
                        payload.get("baseline_correct"),
                        label="counterfactual baseline correct",
                    ),
                    difference_full_minus_baseline=_number(
                        payload.get("accuracy_difference_full_minus_baseline"),
                        label="counterfactual comparison difference",
                    ),
                    cluster_bootstrap_95_lower=_number(
                        payload.get("cluster_bootstrap_95_lower"),
                        label="counterfactual interval lower",
                    ),
                    cluster_bootstrap_95_upper=_number(
                        payload.get("cluster_bootstrap_95_upper"),
                        label="counterfactual interval upper",
                    ),
                    parent_cluster_count=_integer(
                        payload.get("parent_cluster_count"),
                        label="counterfactual parent clusters",
                        minimum=1,
                    ),
                    bootstrap_iteration_count=_integer(
                        payload.get("bootstrap_iteration_count"),
                        label="counterfactual bootstrap iterations",
                        minimum=1,
                    ),
                    bootstrap_seed=_integer(
                        payload.get("bootstrap_seed"),
                        label="counterfactual bootstrap seed",
                    ),
                    percentile_rule=payload.get("percentile_rule"),
                )
            )
        except ValidationError:
            raise Phase4ReleaseError(
                "counterfactual comparison failed the public chart contract",
                safe_stage="P4E_SCHEMA",
            ) from None
    if tuple(item.comparison for item in result) != _COMPARISONS:
        raise Phase4ReleaseError(
            "counterfactual comparisons are incomplete or out of order",
            safe_stage="P4E_SCHEMA",
        )
    return tuple(result)


def charts_implementation_sha256() -> str:
    """Bind chart extraction, rendering, contracts, and public-output privacy rules."""

    paths = (
        Path(__file__),
        Path(__file__).with_name("contracts.py"),
        Path(__file__).parents[1] / "phase3" / "privacy.py",
    )
    digest = hashlib.sha256()
    root = Path(__file__).parents[1]
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _text(
    x: float,
    y: float,
    value: str,
    *,
    size: int = 18,
    weight: int = 400,
    anchor: str = "start",
    fill: str = "#172033",
) -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-weight="{weight}" '
        f'text-anchor="{anchor}" fill="{fill}">{html.escape(value)}</text>'
    )


def _line(x1: float, y1: float, x2: float, y2: float, *, stroke: str, width: float = 2) -> str:
    return (
        f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
        f'stroke="{stroke}" stroke-width="{width:.1f}" />'
    )


def _rect(x: float, y: float, width: float, height: float, *, fill: str, radius: int = 0) -> str:
    return (
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" height="{height:.1f}" '
        f'fill="{fill}" rx="{radius}" />'
    )


def _circle(x: float, y: float, *, fill: str, radius: int = 6) -> str:
    return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius}" fill="{fill}" />'


def _svg_document(*, title: str, description: str, body: Sequence[str]) -> bytes:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="720" viewBox="0 0 1200 720" role="img">',
        f"<title>{html.escape(title)}</title>",
        f"<desc>{html.escape(description)}</desc>",
        "<style>text { font-family: system-ui, -apple-system, 'Segoe UI', sans-serif; }</style>",
        _rect(0, 0, 1200, 720, fill="#ffffff"),
        *body,
        "</svg>",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _render_accounting(cohort: Phase4PublicChartCohort) -> bytes:
    body = [
        _text(60, 70, "Research cohort and paired execution accounting", size=30, weight=700),
        _text(
            60,
            105,
            "Counts are shown on separate denominators; bars are normalized within each unit.",
            size=17,
            fill="#526078",
        ),
    ]
    x, width, height = 260.0, 840.0, 58.0
    natural_width = width * cohort.natural_trace_count / cohort.trace_count
    body.extend(
        (
            _text(60, 210, f"Frozen traces (n={cohort.trace_count})", size=21, weight=650),
            _rect(x, 170, natural_width, height, fill="#2457C5", radius=8),
            _rect(x + natural_width, 170, width - natural_width, height, fill="#E08A2E", radius=8),
            _text(
                x + natural_width / 2,
                206,
                f"Natural {cohort.natural_trace_count}",
                size=17,
                weight=650,
                anchor="middle",
                fill="#ffffff",
            ),
            _text(
                x + natural_width + (width - natural_width) / 2,
                206,
                f"Counterfactual {cohort.counterfactual_trace_count}",
                size=17,
                weight=650,
                anchor="middle",
                fill="#172033",
            ),
            _text(
                260,
                255,
                f"Counterfactual parent clusters: {cohort.counterfactual_parent_cluster_count}",
                size=16,
                fill="#526078",
            ),
        )
    )
    valid_width = width * cohort.valid_judgment_count / cohort.pair_count
    provider_width = width * cohort.provider_error_count / cohort.pair_count
    other_width = width - valid_width - provider_width
    body.extend(
        (
            _text(60, 430, f"Method pairs (n={cohort.pair_count})", size=21, weight=650),
            _rect(x, 390, valid_width, height, fill="#2D8A65", radius=8),
            _rect(x + valid_width, 390, provider_width, height, fill="#C7463B"),
            _rect(x + valid_width + provider_width, 390, other_width, height, fill="#7A8394"),
            _text(
                x + valid_width / 2,
                426,
                f"Valid judgment {cohort.valid_judgment_count}",
                size=17,
                weight=650,
                anchor="middle",
                fill="#ffffff",
            ),
            _text(
                260,
                485,
                f"Provider error {cohort.provider_error_count}; other invalid {cohort.other_invalid_count}",
                size=16,
                fill="#526078",
            ),
            _text(
                60,
                620,
                "Exploratory · CAUTION · invalid outcomes remain in the full denominator",
                size=18,
                weight=650,
                fill="#8A3B32",
            ),
        )
    )
    return _svg_document(
        title=_FIGURE_TITLES["01_cohort_and_execution.svg"],
        description="Two normalized bars show 42 natural and 15 counterfactual traces, then valid and invalid method outcomes.",
        body=body,
    )


def _render_accuracy(methods: Sequence[Phase4PublicMethodChartSummary]) -> bytes:
    body = [
        _text(50, 62, "Full-denominator error detection by source", size=29, weight=700),
        _text(
            50,
            96,
            "All invalid method outcomes count as incorrect. Counterfactual values are descriptive only.",
            size=16,
            fill="#526078",
        ),
    ]
    panels = (
        ("All traces (n=57)", "accuracy_all", 330.0),
        ("Natural (n=42)", "accuracy_natural", 625.0),
        ("Counterfactual (n=15)", "accuracy_counterfactual", 920.0),
    )
    axis_width = 230.0
    for title, _field, x in panels:
        body.append(_text(x + axis_width / 2, 145, title, size=18, weight=650, anchor="middle"))
        for tick in (0, 25, 50, 75, 100):
            tick_x = x + axis_width * tick / 100
            body.append(_line(tick_x, 170, tick_x, 590, stroke="#E2E6ED", width=1))
            body.append(_text(tick_x, 620, str(tick), size=14, anchor="middle", fill="#667085"))
    for index, method in enumerate(methods):
        y = 215.0 + index * 82
        body.append(_text(45, y + 5, method.display_name, size=16, weight=600))
        for _title, field, x in panels:
            metric = getattr(method, field)
            point_x = x + axis_width * metric.estimate
            if metric.interval_lower is not None and metric.interval_upper is not None:
                lower_x = x + axis_width * metric.interval_lower
                upper_x = x + axis_width * metric.interval_upper
                body.append(_line(lower_x, y, upper_x, y, stroke="#2457C5", width=4))
                body.append(_line(lower_x, y - 6, lower_x, y + 6, stroke="#2457C5", width=2))
                body.append(_line(upper_x, y - 6, upper_x, y + 6, stroke="#2457C5", width=2))
            body.append(_circle(point_x, y, fill="#172033", radius=6))
            body.append(
                _text(
                    x + axis_width / 2,
                    y + 26,
                    f"{metric.numerator}/{metric.denominator} ({metric.estimate * 100:.1f}%)",
                    size=13,
                    anchor="middle",
                    fill="#526078",
                )
            )
    body.extend(
        (
            _text(330, 664, "Wilson 95% intervals: all and natural only", size=15, fill="#526078"),
            _text(
                50,
                698,
                "Exploratory · CAUTION · source strata must not be collapsed into a universal ranking",
                size=17,
                weight=650,
                fill="#8A3B32",
            ),
        )
    )
    return _svg_document(
        title=_FIGURE_TITLES["02_error_detection_by_source.svg"],
        description="Five methods are shown across all, natural, and counterfactual full-denominator error detection accuracy.",
        body=body,
    )


def _comparison_label(value: str) -> str:
    return "vs Test-only" if value.endswith("test_only") else "vs Direct LLM Judge"


def _render_comparisons(
    natural: Sequence[Phase4PublicNaturalComparison],
    counterfactual: Sequence[Phase4PublicCounterfactualComparison],
) -> bytes:
    body = [
        _text(55, 62, "Pre-registered Full TraceJudge paired comparisons", size=29, weight=700),
        _text(
            55,
            96,
            "Point estimates are percentage-point differences: Full minus baseline.",
            size=16,
            fill="#526078",
        ),
        _text(
            300, 150, "Natural traces · exact McNemar + Holm", size=19, weight=650, anchor="middle"
        ),
        _text(
            890,
            150,
            "Counterfactual · parent-cluster bootstrap",
            size=19,
            weight=650,
            anchor="middle",
        ),
    ]
    natural_x, counterfactual_x, axis_width = 110.0, 700.0, 400.0
    for start in (natural_x, counterfactual_x):
        for tick in (-20, -10, 0, 10, 20):
            x = start + axis_width * (tick + 25) / 50
            body.append(_line(x, 190, x, 535, stroke="#E2E6ED" if tick else "#7A8394", width=1.5))
            body.append(_text(x, 565, f"{tick:+d}", size=14, anchor="middle", fill="#667085"))
    for index, item in enumerate(natural):
        y = 270.0 + index * 170
        x = natural_x + axis_width * (item.difference_full_minus_baseline * 100 + 25) / 50
        body.append(
            _text(natural_x, y - 34, _comparison_label(item.comparison), size=17, weight=650)
        )
        body.append(_circle(x, y, fill="#2457C5", radius=7))
        body.append(
            _text(
                natural_x,
                y + 36,
                f"Δ {item.difference_full_minus_baseline * 100:+.1f} pp · n01/n10 {item.n01_baseline_incorrect_full_correct}/{item.n10_baseline_correct_full_incorrect} · Holm p={item.holm_adjusted_p_value:.3f}",
                size=14,
                fill="#526078",
            )
        )
    for index, item in enumerate(counterfactual):
        y = 270.0 + index * 170
        lower = counterfactual_x + axis_width * (item.cluster_bootstrap_95_lower * 100 + 25) / 50
        upper = counterfactual_x + axis_width * (item.cluster_bootstrap_95_upper * 100 + 25) / 50
        x = counterfactual_x + axis_width * (item.difference_full_minus_baseline * 100 + 25) / 50
        body.append(
            _text(counterfactual_x, y - 34, _comparison_label(item.comparison), size=17, weight=650)
        )
        body.append(_line(lower, y, upper, y, stroke="#E08A2E", width=5))
        body.append(_line(lower, y - 7, lower, y + 7, stroke="#E08A2E", width=2))
        body.append(_line(upper, y - 7, upper, y + 7, stroke="#E08A2E", width=2))
        body.append(_circle(x, y, fill="#172033", radius=7))
        body.append(
            _text(
                counterfactual_x,
                y + 36,
                f"Δ {item.difference_full_minus_baseline * 100:+.1f} pp · 95% CI [{item.cluster_bootstrap_95_lower * 100:+.1f}, {item.cluster_bootstrap_95_upper * 100:+.1f}] · clusters={item.parent_cluster_count}",
                size=14,
                fill="#526078",
            )
        )
    body.extend(
        (
            _text(
                55,
                615,
                "Natural comparisons report exact p-values, not effect-size confidence intervals.",
                size=15,
                fill="#526078",
            ),
            _text(
                55,
                646,
                "A non-significant result or a [0, 0] interval is not evidence of method equivalence.",
                size=16,
                weight=650,
                fill="#8A3B32",
            ),
            _text(
                55,
                686,
                "Exploratory · CAUTION · only 3 counterfactual parent clusters",
                size=17,
                weight=650,
                fill="#8A3B32",
            ),
        )
    )
    return _svg_document(
        title=_FIGURE_TITLES["03_preregistered_paired_comparisons.svg"],
        description="Two natural and two counterfactual pre-registered comparisons are shown without equivalence claims.",
        body=body,
    )


def _render_chart_payloads(
    *,
    cohort: Phase4PublicChartCohort,
    methods: Sequence[Phase4PublicMethodChartSummary],
    natural_comparisons: Sequence[Phase4PublicNaturalComparison],
    counterfactual_comparisons: Sequence[Phase4PublicCounterfactualComparison],
) -> dict[str, bytes]:
    return {
        "01_cohort_and_execution.svg": _render_accounting(cohort),
        "02_error_detection_by_source.svg": _render_accuracy(methods),
        "03_preregistered_paired_comparisons.svg": _render_comparisons(
            natural_comparisons,
            counterfactual_comparisons,
        ),
    }


def _assert_svg_safe(payload: bytes, *, canaries: Sequence[str | bytes]) -> None:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise Phase4ReleaseError("SVG is not UTF-8", safe_stage="P4E_PRIVACY") from None
    lowered = text.casefold()
    forbidden = ("<script", " href=", "xlink:href", "file:", "/users/", "c:\\")
    if any(item in lowered for item in forbidden):
        raise Phase4ReleaseError(
            "SVG contains an external or absolute reference",
            safe_stage="P4E_PRIVACY",
        )
    try:
        assert_public_payload_safe(text, canaries=canaries)
    except ValueError:
        raise Phase4ReleaseError(
            "SVG failed the public privacy scan",
            safe_stage="P4E_PRIVACY",
        ) from None


def prepare_public_charts(
    *,
    statistics_manifest_path: str | Path,
    statistics_report_path: str | Path,
    expected_statistics_manifest_sha256: str,
    expected_statistics_report_sha256: str,
    chart_bundle_id: str,
    repo_root: str | Path,
    git_identity: Phase4GitIdentity | None = None,
    allow_dirty: bool = False,
    privacy_canaries: Sequence[str | bytes] = (),
) -> Phase4ChartsPreflight:
    """Validate frozen aggregate statistics and render three public SVGs in memory."""

    for value, label in (
        (expected_statistics_manifest_sha256, "statistics manifest SHA256"),
        (expected_statistics_report_sha256, "statistics report SHA256"),
    ):
        if _SHA256_PATTERN.fullmatch(value) is None:
            raise Phase4ReleaseError(f"{label} is invalid", safe_stage="P4E_INPUT")
    manifest_path = _absolute_without_resolving_symlinks(statistics_manifest_path)
    report_path = _absolute_without_resolving_symlinks(statistics_report_path)
    _reject_existing_symlink_components(manifest_path, label="statistics manifest")
    _reject_existing_symlink_components(report_path, label="statistics report")
    manifest_payload = _read_regular_file(manifest_path, label="statistics manifest")
    report_payload = _read_regular_file(report_path, label="statistics report")
    if _sha256(manifest_payload) != expected_statistics_manifest_sha256:
        raise Phase4ReleaseError("statistics manifest hash differs", safe_stage="P4E_INPUT")
    if _sha256(report_payload) != expected_statistics_report_sha256:
        raise Phase4ReleaseError("statistics report hash differs", safe_stage="P4E_INPUT")
    source_manifest = _decode_json(manifest_payload, label="statistics manifest")
    report = _decode_json(report_payload, label="statistics report")
    if (
        source_manifest.get("schema_version") != 1
        or source_manifest.get("phase") != "phase3_paired_statistics"
        or source_manifest.get("status") != "completed"
        or source_manifest.get("report_sha256") != expected_statistics_report_sha256
        or any(
            source_manifest.get(key) is not False
            for key in (
                "contains_per_trace_rows",
                "contains_annotation_rationales",
                "contains_provider_raw",
                "contains_hidden_evaluation_content",
            )
        )
    ):
        raise Phase4ReleaseError(
            "statistics manifest is not a completed aggregate-only source",
            safe_stage="P4E_INPUT",
        )
    if (
        report.get("schema_version") != 1
        or report.get("kind") != "tracejudge_phase3_paired_statistics"
        or report.get("statistics_id") != source_manifest.get("statistics_id")
    ):
        raise Phase4ReleaseError(
            "statistics report identity differs from its manifest",
            safe_stage="P4E_INPUT",
        )
    analysis = _mapping(report.get("analysis_contract"), label="analysis contract")
    if (
        analysis.get("exploratory_only") is not True
        or analysis.get("positive_class") != "has_error_true"
        or analysis.get("invalid_method_outcome_policy")
        != "retain_in_full_denominator_count_as_incorrect_and_report_separately"
        or analysis.get("counterfactual_interval") != "parent_problem_cluster_percentile_bootstrap"
    ):
        raise Phase4ReleaseError(
            "statistics analysis contract is incompatible with public charts",
            safe_stage="P4E_INPUT",
        )
    root = Path(repo_root).expanduser().resolve()
    identity = git_identity or _git_identity(root)
    if identity.dirty and not allow_dirty:
        raise Phase4ReleaseError(
            "Git worktree is dirty; formal chart publication requires a clean commit",
            safe_stage="P4E_GIT_DIRTY",
        )
    methods = _method_summaries(report)
    cohort = _cohort_summary(report, methods)
    natural = _natural_comparisons(report)
    counterfactual = _counterfactual_comparisons(report)
    _validate_comparison_accounting(
        cohort=cohort,
        methods=methods,
        natural=natural,
        counterfactual=counterfactual,
    )
    figure_payloads = _render_chart_payloads(
        cohort=cohort,
        methods=methods,
        natural_comparisons=natural,
        counterfactual_comparisons=counterfactual,
    )
    for payload in figure_payloads.values():
        _assert_svg_safe(payload, canaries=privacy_canaries)
    figures = tuple(
        Phase4PublicChartArtifact(
            filename=filename,
            title=_FIGURE_TITLES[filename],
            sha256=_sha256(payload),
            size_bytes=len(payload),
        )
        for filename, payload in figure_payloads.items()
    )
    try:
        public_manifest = Phase4PublicChartsManifest(
            chart_bundle_id=chart_bundle_id,
            source_statistics_id=str(report["statistics_id"]),
            source_statistics_manifest_sha256=expected_statistics_manifest_sha256,
            source_statistics_report_sha256=expected_statistics_report_sha256,
            source_git=identity,
            render_implementation_sha256=charts_implementation_sha256(),
            cohort=cohort,
            methods=methods,
            natural_comparisons=natural,
            counterfactual_comparisons=counterfactual,
            figures=figures,
        )
        assert_public_payload_safe(public_manifest, canaries=privacy_canaries)
    except (ValidationError, ValueError):
        raise Phase4ReleaseError(
            "public chart manifest failed contract or privacy validation",
            safe_stage="P4E_PRIVACY",
        ) from None
    public_payload = _pretty_json(public_manifest.model_dump(mode="json"))
    return Phase4ChartsPreflight(
        manifest=public_manifest,
        manifest_payload=public_payload,
        manifest_sha256=_sha256(public_payload),
        figure_payloads=figure_payloads,
    )


def write_public_charts(
    *,
    output_dir: str | Path,
    **preflight_kwargs: Any,
) -> Phase4ChartsResult:
    """Atomically publish one immutable public chart bundle."""

    prepared = prepare_public_charts(**preflight_kwargs)
    output_root = _absolute_without_resolving_symlinks(output_dir)
    _reject_existing_symlink_components(
        output_root,
        label="chart output root",
        safe_stage="P4E_OUTPUT",
    )
    if output_root.is_symlink() or (output_root.exists() and not output_root.is_dir()):
        raise Phase4ReleaseError("chart output root is unsafe", safe_stage="P4E_OUTPUT")
    output_root.mkdir(parents=True, exist_ok=True, mode=0o755)
    run_dir = output_root / prepared.manifest.chart_bundle_id
    if run_dir.exists() or run_dir.is_symlink():
        raise Phase4ReleaseError("chart output already exists", safe_stage="P4E_OUTPUT")
    temporary = Path(tempfile.mkdtemp(prefix=f".{run_dir.name}.", dir=output_root))
    try:
        for filename, payload in prepared.figure_payloads.items():
            path = temporary / filename
            path.write_bytes(payload)
            path.chmod(0o644)
        manifest_path = temporary / "manifest.json"
        manifest_path.write_bytes(prepared.manifest_payload)
        manifest_path.chmod(0o644)
        os.replace(temporary, run_dir)
        run_dir.chmod(0o755)
    except OSError:
        raise Phase4ReleaseError(
            "cannot atomically publish chart bundle",
            safe_stage="P4E_OUTPUT",
        ) from None
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return Phase4ChartsResult(
        chart_bundle_id=prepared.manifest.chart_bundle_id,
        run_dir=run_dir,
        manifest_path=run_dir / "manifest.json",
        manifest_sha256=prepared.manifest_sha256,
        figure_paths=tuple(run_dir / item.filename for item in prepared.manifest.figures),
    )


def verify_public_charts(
    *,
    manifest_path: str | Path,
    expected_manifest_sha256: str | None = None,
) -> Phase4ChartsVerificationResult:
    """Regenerate SVG bytes from a tracked manifest and reject any drift or tampering."""

    path = _absolute_without_resolving_symlinks(manifest_path)
    _reject_existing_symlink_components(path, label="public chart manifest")
    payload = _read_regular_file(path, label="public chart manifest")
    if expected_manifest_sha256 is not None:
        if _SHA256_PATTERN.fullmatch(expected_manifest_sha256) is None:
            raise Phase4ReleaseError("chart manifest SHA256 is invalid", safe_stage="P4E_VERIFY")
        if _sha256(payload) != expected_manifest_sha256:
            raise Phase4ReleaseError("chart manifest hash differs", safe_stage="P4E_VERIFY")
    try:
        manifest = Phase4PublicChartsManifest.model_validate(
            _decode_json(payload, label="public chart manifest")
        )
        assert_public_payload_safe(manifest)
    except (ValidationError, ValueError):
        raise Phase4ReleaseError(
            "public chart manifest failed contract or privacy validation",
            safe_stage="P4E_VERIFY",
        ) from None
    if manifest.render_implementation_sha256 != charts_implementation_sha256():
        raise Phase4ReleaseError(
            "chart implementation differs from the manifest",
            safe_stage="P4E_VERIFY",
        )
    expected_payloads = _render_chart_payloads(
        cohort=manifest.cohort,
        methods=manifest.methods,
        natural_comparisons=manifest.natural_comparisons,
        counterfactual_comparisons=manifest.counterfactual_comparisons,
    )
    actual_svg_names = {item.name for item in path.parent.glob("*.svg") if item.is_file()}
    if actual_svg_names != set(expected_payloads):
        raise Phase4ReleaseError(
            "public chart file set differs from the manifest",
            safe_stage="P4E_VERIFY",
        )
    by_name = {item.filename: item for item in manifest.figures}
    for filename, expected in expected_payloads.items():
        actual = _read_regular_file(path.parent / filename, label="public chart SVG")
        artifact = by_name[filename]
        if (
            actual != expected
            or len(actual) != artifact.size_bytes
            or _sha256(actual) != artifact.sha256
        ):
            raise Phase4ReleaseError(
                "public chart differs from its deterministic rendering",
                safe_stage="P4E_VERIFY",
            )
        _assert_svg_safe(actual, canaries=())
    return Phase4ChartsVerificationResult(
        chart_bundle_id=manifest.chart_bundle_id,
        figure_count=len(manifest.figures),
        verified=True,
    )
