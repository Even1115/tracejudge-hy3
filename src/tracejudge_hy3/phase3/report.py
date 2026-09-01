"""Gate-F hash-bound interpretation and de-identified research reporting.

This module consumes only the aggregate Gate-E4 report, the public Gate-D
certificate fixture, and the structured Gate-E3 result ledger for aggregate
runtime accounting. It never reads Provider raw output, annotation rationales,
candidate bodies, official hidden tests, or EvalPlus raw artifacts.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from tracejudge_hy3.baseline.runner import _dependency_versions, _git_metadata

from .contracts import (
    MethodId,
    MethodOutcome,
    MethodOutcomeStatus,
    PairedEvaluationIndex,
    Phase3ErrorCertificate,
    Phase3PublicCertificateManifest,
    Phase3RunManifest,
)
from .privacy import assert_public_payload_safe, canonical_sha256, jsonl_record_sha256
from .statistics import (
    _decode_json,
    _decode_jsonl_models,
    _fsync_directory,
    _pretty_json,
    _read_regular_file,
    _write_new_file,
)

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_METHOD_LABELS = {
    MethodId.TEST_ONLY: "Test-only",
    MethodId.DIRECT_LLM_JUDGE: "Direct LLM Judge",
    MethodId.FOUR_LAYER_STRUCTURED_JUDGE: "Four-layer Structured Judge",
    MethodId.FOUR_LAYER_AST: "Four-layer + AST",
    MethodId.FULL_TRACEJUDGE: "Full TraceJudge",
}


class Phase3ReportError(ValueError):
    """Safe, content-free Gate-F failure."""

    def __init__(self, message: str, *, safe_stage: str = "P3F_REPORT") -> None:
        super().__init__(message)
        self.safe_stage = safe_stage


@dataclass(frozen=True, slots=True)
class Phase3ReportPreflight:
    report_id: str
    statistics_id: str
    paired_run_id: str
    trace_count: int
    method_count: int
    pair_count: int
    valid_judgment_count: int
    provider_error_count: int
    overall_confidence: str
    fallacy_scan_coverage: int
    statistics_manifest_sha256: str
    statistics_report_sha256: str
    paired_run_manifest_sha256: str
    paired_results_sha256: str
    paired_index_sha256: str
    certificate_manifest_sha256: str
    confirmed_certificate_sha256: str
    replay_evidence_sha256: str
    report_implementation_sha256: str
    markdown_sha256: str
    validation_sha256: str
    replay_command_sha256: str
    git_commit: str
    git_branch: str
    git_dirty: bool


@dataclass(frozen=True, slots=True)
class Phase3ReportResult(Phase3ReportPreflight):
    run_dir: Path
    manifest_path: Path
    markdown_path: Path
    validation_path: Path
    demo_certificate_path: Path
    replay_command_path: Path
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class _LoadedReportInputs:
    statistics_manifest: Mapping[str, Any]
    statistics_report: Mapping[str, Any]
    statistics_manifest_sha256: str
    statistics_report_sha256: str
    run_manifest: Phase3RunManifest
    runtime_accounting: Mapping[str, Mapping[str, Any]]
    paired_run_manifest_sha256: str
    paired_results_sha256: str
    paired_index_sha256: str
    certificate_manifest: Phase3PublicCertificateManifest
    certificate: Phase3ErrorCertificate
    certificate_payload: bytes
    certificate_manifest_sha256: str
    certificate_sha256: str


@dataclass(frozen=True, slots=True)
class _PreparedReport:
    preflight: Phase3ReportPreflight
    markdown_payload: bytes
    validation_payload: bytes
    certificate_payload: bytes
    replay_command_payload: bytes
    output_root: Path
    run_dir: Path


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _require_sha256(value: str, *, label: str) -> None:
    if not _SHA256_PATTERN.fullmatch(value):
        raise Phase3ReportError(f"{label} is invalid", safe_stage="P3F_INPUT")


def _expect_mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise Phase3ReportError(f"{label} is malformed", safe_stage="P3F_INPUT")
    return value


def _expect_sequence(value: Any, *, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise Phase3ReportError(f"{label} is malformed", safe_stage="P3F_INPUT")
    return value


def _expect_int(value: Any, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise Phase3ReportError(f"{label} is malformed", safe_stage="P3F_INPUT")
    return value


def _expect_number(value: Any, *, label: str) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise Phase3ReportError(f"{label} is malformed", safe_stage="P3F_INPUT")
    return float(value)


def _load_statistics(
    *,
    statistics_run_dir: Path,
    expected_manifest_sha256: str,
    expected_report_sha256: str,
) -> tuple[Mapping[str, Any], Mapping[str, Any], str, str]:
    _require_sha256(expected_manifest_sha256, label="statistics manifest SHA256")
    _require_sha256(expected_report_sha256, label="statistics report SHA256")
    if statistics_run_dir.is_symlink() or not statistics_run_dir.is_dir():
        raise Phase3ReportError("statistics directory is unsafe", safe_stage="P3F_STATISTICS")
    manifest_payload = _read_regular_file(
        statistics_run_dir / "manifest.json",
        label="statistics manifest",
        private=True,
    )
    report_payload = _read_regular_file(
        statistics_run_dir / "report.json",
        label="statistics report",
        private=True,
    )
    manifest_sha = _sha256(manifest_payload)
    report_sha = _sha256(report_payload)
    if manifest_sha != expected_manifest_sha256 or report_sha != expected_report_sha256:
        raise Phase3ReportError(
            "statistics artifact hash differs",
            safe_stage="P3F_STATISTICS",
        )
    manifest = _decode_json(manifest_payload, label="statistics manifest")
    report = _decode_json(report_payload, label="statistics report")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("phase") != "phase3_paired_statistics"
        or manifest.get("status") != "completed"
        or manifest.get("report_sha256") != report_sha
        or report.get("schema_version") != 1
        or report.get("kind") != "tracejudge_phase3_paired_statistics"
        or report.get("statistics_id") != manifest.get("statistics_id")
    ):
        raise Phase3ReportError(
            "statistics artifact contract differs",
            safe_stage="P3F_STATISTICS",
        )
    for field in (
        "contains_per_trace_rows",
        "contains_annotation_rationales",
        "contains_provider_raw",
        "contains_hidden_evaluation_content",
    ):
        if manifest.get(field) is not False:
            raise Phase3ReportError(
                "statistics privacy declaration differs",
                safe_stage="P3F_STATISTICS",
            )
    identities = _expect_mapping(report.get("identities"), label="statistics identities")
    if (
        identities.get("paired_run_manifest_sha256") != manifest.get("paired_run_manifest_sha256")
        or identities.get("paired_results_sha256") != manifest.get("paired_results_sha256")
        or identities.get("paired_index_sha256") != manifest.get("paired_index_sha256")
        or identities.get("annotation_set_manifest_sha256")
        != manifest.get("annotation_set_manifest_sha256")
        or identities.get("completed_labels_sha256") != manifest.get("completed_labels_sha256")
        or identities.get("annotation_records_sha256") != manifest.get("annotation_records_sha256")
    ):
        raise Phase3ReportError(
            "statistics identities disagree with manifest",
            safe_stage="P3F_STATISTICS",
        )
    analysis = _expect_mapping(report.get("analysis_contract"), label="analysis contract")
    if (
        analysis.get("exploratory_only") is not True
        or analysis.get("invalid_method_outcome_policy")
        != "retain_in_full_denominator_count_as_incorrect_and_report_separately"
        or analysis.get("multiple_comparison_policy") != "holm_for_confirmatory_primary_comparisons"
    ):
        raise Phase3ReportError(
            "statistics analysis contract differs",
            safe_stage="P3F_STATISTICS",
        )
    assert_public_payload_safe(manifest)
    assert_public_payload_safe(report)
    return manifest, report, manifest_sha, report_sha


def _load_paired_runtime(
    *,
    paired_run_dir: Path,
    statistics_report: Mapping[str, Any],
) -> tuple[Phase3RunManifest, Mapping[str, Mapping[str, Any]], str, str, str]:
    if paired_run_dir.is_symlink() or not paired_run_dir.is_dir():
        raise Phase3ReportError("paired run directory is unsafe", safe_stage="P3F_RUNTIME")
    identities = _expect_mapping(
        statistics_report.get("identities"),
        label="statistics identities",
    )
    manifest_payload = _read_regular_file(
        paired_run_dir / "manifest.json",
        label="paired run manifest",
        private=True,
    )
    results_payload = _read_regular_file(
        paired_run_dir / "results.jsonl",
        label="paired results",
        private=True,
    )
    index_payload = _read_regular_file(
        paired_run_dir / "index.json",
        label="paired index",
        private=True,
    )
    manifest_sha = _sha256(manifest_payload)
    results_sha = _sha256(results_payload)
    index_sha = _sha256(index_payload)
    if (
        manifest_sha != identities.get("paired_run_manifest_sha256")
        or results_sha != identities.get("paired_results_sha256")
        or index_sha != identities.get("paired_index_sha256")
    ):
        raise Phase3ReportError(
            "paired runtime artifact hash differs",
            safe_stage="P3F_RUNTIME",
        )
    try:
        manifest = Phase3RunManifest.model_validate(
            _decode_json(manifest_payload, label="paired run manifest")
        )
        index = PairedEvaluationIndex.model_validate(
            _decode_json(index_payload, label="paired index")
        )
    except ValidationError:
        raise Phase3ReportError(
            "paired runtime contract failed validation",
            safe_stage="P3F_RUNTIME",
        ) from None
    rows = _decode_jsonl_models(results_payload, label="paired results", model=MethodOutcome)
    assert all(isinstance(item, MethodOutcome) for item in rows)
    row_hashes = tuple(
        jsonl_record_sha256(raw) for raw in results_payload.splitlines(keepends=True)
    )
    if (
        manifest.status != "completed"
        or manifest.run_id != identities.get("paired_run_id")
        or index.run_id != manifest.run_id
        or index.results_sha256 != results_sha
        or index.resume_identity_sha256 != manifest.resume_identity_sha256
        or len(rows) != len(index.result_references)
    ):
        raise Phase3ReportError(
            "paired runtime identity differs",
            safe_stage="P3F_RUNTIME",
        )
    for reference, row, row_sha in zip(index.result_references, rows, row_hashes, strict=True):
        if (
            row.run_id != manifest.run_id
            or (
                row.status == MethodOutcomeStatus.PROVIDER_ERROR
                and (row.attempt_count != 1 or row.parse_repair_count != 0)
            )
            or reference.trace_id != row.trace_id
            or reference.method_id != row.method_id
            or reference.status != row.status
            or reference.result_record_sha256 != row_sha
        ):
            raise Phase3ReportError(
                "paired runtime index differs from results",
                safe_stage="P3F_RUNTIME",
            )

    accounting: dict[str, Mapping[str, Any]] = {}
    for method_id in MethodId:
        selected = [item for item in rows if item.method_id == method_id]
        if len(selected) != len(index.ordered_trace_ids):
            raise Phase3ReportError(
                "paired runtime method denominator differs",
                safe_stage="P3F_RUNTIME",
            )
        prompt_values = [item.usage.prompt_tokens for item in selected]
        completion_values = [item.usage.completion_tokens for item in selected]
        cost_values = [item.usage.reported_cost_microusd for item in selected]
        accounting[method_id.value] = {
            "pair_count": len(selected),
            "status_counts": dict(sorted(Counter(item.status.value for item in selected).items())),
            "duration_seconds_total": sum(item.duration_seconds for item in selected),
            "attempt_count_total": sum(item.attempt_count for item in selected),
            "parse_repair_count_total": sum(item.parse_repair_count for item in selected),
            "prompt_token_known_count": sum(value is not None for value in prompt_values),
            "prompt_token_total_known_rows": sum(
                value for value in prompt_values if value is not None
            ),
            "completion_token_known_count": sum(value is not None for value in completion_values),
            "completion_token_total_known_rows": sum(
                value for value in completion_values if value is not None
            ),
            "reported_cost_known_count": sum(value is not None for value in cost_values),
            "reported_cost_microusd_total_known_rows": sum(
                value for value in cost_values if value is not None
            ),
            "cost_status_counts": dict(
                sorted(Counter(item.usage.cost_status for item in selected).items())
            ),
            "diagnostic_counts": dict(
                sorted(
                    Counter(
                        item.diagnostic_code
                        for item in selected
                        if item.diagnostic_code is not None
                    ).items()
                )
            ),
        }
    assert_public_payload_safe(accounting)
    return manifest, accounting, manifest_sha, results_sha, index_sha


def _load_certificate(
    *,
    certificate_run_dir: Path,
    expected_manifest_sha256: str,
    certificate_path: Path,
    expected_certificate_sha256: str,
    expected_replay_evidence_sha256: str,
    statistics_report: Mapping[str, Any],
) -> tuple[Phase3PublicCertificateManifest, Phase3ErrorCertificate, bytes, str, str]:
    for value, label in (
        (expected_manifest_sha256, "certificate manifest SHA256"),
        (expected_certificate_sha256, "confirmed certificate SHA256"),
        (expected_replay_evidence_sha256, "replay evidence SHA256"),
    ):
        _require_sha256(value, label=label)
    if certificate_run_dir.is_symlink() or not certificate_run_dir.is_dir():
        raise Phase3ReportError(
            "certificate directory is unsafe",
            safe_stage="P3F_CERTIFICATE",
        )
    manifest_payload = _read_regular_file(
        certificate_run_dir / "manifest.json",
        label="certificate manifest",
        private=True,
    )
    certificate_payload = _read_regular_file(
        certificate_path,
        label="confirmed certificate",
        private=True,
    )
    manifest_sha = _sha256(manifest_payload)
    certificate_sha = _sha256(certificate_payload)
    if manifest_sha != expected_manifest_sha256 or certificate_sha != expected_certificate_sha256:
        raise Phase3ReportError(
            "certificate artifact hash differs",
            safe_stage="P3F_CERTIFICATE",
        )
    try:
        manifest = Phase3PublicCertificateManifest.model_validate(
            _decode_json(manifest_payload, label="certificate manifest")
        )
        certificate = Phase3ErrorCertificate.model_validate(
            _decode_json(certificate_payload, label="confirmed certificate")
        )
    except ValidationError:
        raise Phase3ReportError(
            "certificate artifact failed validation",
            safe_stage="P3F_CERTIFICATE",
        ) from None
    references = [
        item
        for item in manifest.certificate_artifacts
        if item.certificate_id == certificate.certificate_id
    ]
    try:
        relative = certificate_path.resolve().relative_to(certificate_run_dir.resolve()).as_posix()
    except ValueError:
        raise Phase3ReportError(
            "certificate is outside its run directory",
            safe_stage="P3F_CERTIFICATE",
        ) from None
    identities = _expect_mapping(
        statistics_report.get("identities"),
        label="statistics identities",
    )
    counterexample = certificate.counterexample
    if (
        manifest.status != "completed"
        or manifest.confirmed_bug_count != 1
        or len(references) != 1
        or references[0].relative_path != relative
        or references[0].certificate_sha256 != certificate_sha
        or certificate.verdict != "confirmed_bug"
        or counterexample is None
        or not counterexample.verified_in_restricted_sandbox
        or counterexample.execution_evidence_sha256 != expected_replay_evidence_sha256
        or certificate.replay_command is None
        or manifest.frozen_manifest_sha256 != identities.get("frozen_cohort_manifest_sha256")
        or manifest.natural_manifest_sha256 != identities.get("natural_manifest_sha256")
        or certificate.frozen_manifest_sha256 != identities.get("frozen_cohort_manifest_sha256")
    ):
        raise Phase3ReportError(
            "confirmed certificate binding differs",
            safe_stage="P3F_CERTIFICATE",
        )
    assert_public_payload_safe(manifest)
    assert_public_payload_safe(certificate)
    return manifest, certificate, certificate_payload, manifest_sha, certificate_sha


def _method_metrics(report: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    records = _expect_sequence(report.get("method_metrics"), label="method metrics")
    result: dict[str, Mapping[str, Any]] = {}
    for record in records:
        item = _expect_mapping(record, label="method metric")
        method_id = item.get("method_id")
        if not isinstance(method_id, str) or method_id in result:
            raise Phase3ReportError("method metric identity differs", safe_stage="P3F_STATISTICS")
        result[method_id] = item
    if tuple(result) != tuple(item.value for item in MethodId):
        raise Phase3ReportError("method metric order differs", safe_stage="P3F_STATISTICS")
    return result


def _scope_metric(
    methods: Mapping[str, Mapping[str, Any]],
    method_id: MethodId,
    scope: str,
    metric: str,
) -> Mapping[str, Any]:
    scopes = _expect_mapping(methods[method_id.value].get("scopes"), label="method scopes")
    scope_value = _expect_mapping(scopes.get(scope), label="method scope")
    return _expect_mapping(scope_value.get(metric), label="method metric value")


def _count(metric: Mapping[str, Any]) -> tuple[int, int, float]:
    numerator = _expect_int(metric.get("numerator"), label="metric numerator")
    denominator = _expect_int(metric.get("denominator"), label="metric denominator")
    estimate = _expect_number(metric.get("estimate"), label="metric estimate")
    if denominator == 0 or numerator > denominator:
        raise Phase3ReportError("metric count differs", safe_stage="P3F_STATISTICS")
    return numerator, denominator, estimate


def _require_equal(actual: Any, expected: Any, *, label: str) -> None:
    if actual != expected:
        raise Phase3ReportError(f"{label} differs", safe_stage="P3F_STATISTICS")


def _validate_frozen_report(report: Mapping[str, Any]) -> None:
    """Fail closed before rendering conclusions tied to the frozen 57x5 study."""

    cohort = _expect_mapping(report.get("cohort"), label="cohort summary")
    for field, expected in (
        ("natural_trace_count", 42),
        ("counterfactual_trace_count", 15),
        ("counterfactual_parent_cluster_count", 3),
        ("trace_count", 57),
        ("method_count", 5),
        ("pair_count", 285),
    ):
        _require_equal(cohort.get(field), expected, label=f"cohort {field}")
    statuses = _expect_mapping(cohort.get("final_status_counts"), label="status counts")
    _require_equal(
        dict(statuses),
        {
            "ast_error": 0,
            "infrastructure_error": 0,
            "parse_error": 0,
            "provider_error": 2,
            "public_execution_timeout": 0,
            "reused": 0,
            "skipped": 0,
            "valid_judgment": 283,
        },
        label="final status counts",
    )

    methods = _method_metrics(report)
    expected_detection = {
        MethodId.TEST_ONLY: ((54, 57), (42, 42), (12, 15)),
        MethodId.DIRECT_LLM_JUDGE: ((55, 57), (41, 42), (14, 15)),
        MethodId.FOUR_LAYER_STRUCTURED_JUDGE: ((56, 57), (42, 42), (14, 15)),
        MethodId.FOUR_LAYER_AST: ((54, 57), (40, 42), (14, 15)),
        MethodId.FULL_TRACEJUDGE: ((55, 57), (41, 42), (14, 15)),
    }
    for method_id, expected_scopes in expected_detection.items():
        for scope, expected in zip(
            ("all", "natural", "counterfactual"),
            expected_scopes,
            strict=True,
        ):
            metric = _scope_metric(
                methods,
                method_id,
                scope,
                "error_detection_accuracy_full_denominator",
            )
            _require_equal(_count(metric)[:2], expected, label=f"{method_id} {scope} accuracy")

    for scope, expected in (
        ("all", (14, 57)),
        ("natural", (2, 42)),
        ("counterfactual", (12, 15)),
    ):
        prevalence = _scope_metric(
            methods,
            MethodId.TEST_ONLY,
            scope,
            "gold_error_prevalence",
        )
        _require_equal(_count(prevalence)[:2], expected, label=f"{scope} prevalence")

    for metric_name in (
        "process_correct_accuracy_full_denominator",
        "reasoning_accuracy_full_denominator",
        "plan_code_alignment_accuracy_full_denominator",
    ):
        metric = _scope_metric(methods, MethodId.TEST_ONLY, "all", metric_name)
        _require_equal(_count(metric)[:2], (0, 57), label=f"Test-only {metric_name}")

    expected_localization = {
        MethodId.TEST_ONLY: ((2, 14), (0, 11), (2, 14)),
        MethodId.DIRECT_LLM_JUDGE: ((9, 14), (9, 11), (11, 14)),
        MethodId.FOUR_LAYER_STRUCTURED_JUDGE: ((13, 14), (8, 11), (13, 14)),
        MethodId.FOUR_LAYER_AST: ((10, 14), (10, 11), (13, 14)),
        MethodId.FULL_TRACEJUDGE: ((7, 14), (9, 11), (10, 14)),
    }
    for method_id, expected_values in expected_localization.items():
        for metric_name, expected in zip(
            (
                "first_faulty_layer_accuracy_gold_errors",
                "first_faulty_step_accuracy_labeled_gold_steps",
                "error_type_accuracy_gold_errors",
            ),
            expected_values,
            strict=True,
        ):
            metric = _scope_metric(methods, method_id, "all", metric_name)
            _require_equal(_count(metric)[:2], expected, label=f"{method_id} {metric_name}")

    comparisons = _expect_mapping(report.get("primary_comparisons"), label="comparisons")
    natural = _expect_sequence(comparisons.get("natural"), label="natural comparisons")
    counterfactual = _expect_sequence(
        comparisons.get("counterfactual"),
        label="counterfactual comparisons",
    )
    expected_natural = (
        ("full_tracejudge_vs_test_only", 41, 42, 0, 1, 1.0, 1.0),
        ("full_tracejudge_vs_direct_llm_judge", 41, 41, 1, 1, 1.0, 1.0),
    )
    expected_counterfactual = (
        ("full_tracejudge_vs_test_only", 14, 12, 0.0, 0.2, 3),
        ("full_tracejudge_vs_direct_llm_judge", 14, 14, 0.0, 0.0, 3),
    )
    if len(natural) != 2 or len(counterfactual) != 2:
        raise Phase3ReportError("primary comparison count differs", safe_stage="P3F_STATISTICS")
    for raw, expected in zip(natural, expected_natural, strict=True):
        item = _expect_mapping(raw, label="natural comparison")
        observed = (
            item.get("comparison"),
            item.get("full_correct"),
            item.get("baseline_correct"),
            item.get("n01_baseline_incorrect_full_correct"),
            item.get("n10_baseline_correct_full_incorrect"),
            item.get("exact_two_sided_mcnemar_p_value"),
            item.get("holm_adjusted_p_value"),
        )
        _require_equal(observed, expected, label="natural comparison")
    for raw, expected in zip(counterfactual, expected_counterfactual, strict=True):
        item = _expect_mapping(raw, label="counterfactual comparison")
        observed = (
            item.get("comparison"),
            item.get("full_correct"),
            item.get("baseline_correct"),
            item.get("cluster_bootstrap_95_lower"),
            item.get("cluster_bootstrap_95_upper"),
            item.get("parent_cluster_count"),
        )
        _require_equal(observed, expected, label="counterfactual comparison")

    mutation_rows = _expect_sequence(
        report.get("counterfactual_mutation_breakdown"),
        label="counterfactual breakdown",
    )
    expected_mutations = (
        ("reasoning_swap", (0, 3, 3, 3, 3)),
        ("code_defect", (3, 3, 3, 3, 3)),
        ("boundary_deletion", (3, 3, 3, 3, 3)),
        ("shortcut", (3, 3, 3, 3, 3)),
        ("equivalent_implementation", (3, 2, 2, 2, 2)),
    )
    if len(mutation_rows) != len(expected_mutations):
        raise Phase3ReportError("mutation breakdown count differs", safe_stage="P3F_STATISTICS")
    for raw, (expected_kind, expected_counts) in zip(
        mutation_rows,
        expected_mutations,
        strict=True,
    ):
        item = _expect_mapping(raw, label="mutation row")
        values = _expect_sequence(item.get("method_error_detection"), label="mutation metrics")
        observed_counts = tuple(
            _count(
                _expect_mapping(
                    _expect_mapping(value, label="mutation metric").get(
                        "accuracy_full_denominator"
                    ),
                    label="mutation accuracy",
                )
            )[0]
            for value in values
        )
        _require_equal(item.get("mutation_kind"), expected_kind, label="mutation kind")
        _require_equal(observed_counts, expected_counts, label=f"{expected_kind} counts")


def _validate_runtime_accounting(
    accounting: Mapping[str, Mapping[str, Any]],
) -> None:
    expected_provider_errors = {
        MethodId.TEST_ONLY: 0,
        MethodId.DIRECT_LLM_JUDGE: 1,
        MethodId.FOUR_LAYER_STRUCTURED_JUDGE: 0,
        MethodId.FOUR_LAYER_AST: 1,
        MethodId.FULL_TRACEJUDGE: 0,
    }
    for method_id, provider_errors in expected_provider_errors.items():
        runtime = accounting[method_id.value]
        _require_equal(runtime.get("pair_count"), 57, label=f"{method_id} runtime pairs")
        statuses = _expect_mapping(runtime.get("status_counts"), label="runtime statuses")
        _require_equal(
            dict(statuses),
            {
                "valid_judgment": 57 - provider_errors,
                **({"provider_error": provider_errors} if provider_errors else {}),
            },
            label=f"{method_id} runtime statuses",
        )
        _require_equal(
            runtime.get("reported_cost_known_count"),
            0,
            label=f"{method_id} reported cost count",
        )
        cost_statuses = _expect_mapping(
            runtime.get("cost_status_counts"),
            label="cost statuses",
        )
        expected_cost_status = (
            "not_applicable" if method_id == MethodId.TEST_ONLY else "unavailable"
        )
        _require_equal(
            dict(cost_statuses),
            {expected_cost_status: 57},
            label=f"{method_id} cost status",
        )
        diagnostics = _expect_mapping(
            runtime.get("diagnostic_counts"),
            label="diagnostic counts",
        )
        expected_diagnostics = {"provider_connection_error": 1} if provider_errors else {}
        _require_equal(
            dict(diagnostics),
            expected_diagnostics,
            label=f"{method_id} diagnostics",
        )


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _count_percent(metric: Mapping[str, Any], *, include_interval: bool = True) -> str:
    numerator, denominator, estimate = _count(metric)
    text = f"{numerator}/{denominator}（{_percent(estimate)}）"
    if include_interval:
        lower = _expect_number(metric.get("wilson_95_lower"), label="Wilson lower")
        upper = _expect_number(metric.get("wilson_95_upper"), label="Wilson upper")
        text += f"；95% Wilson CI [{_percent(lower)}, {_percent(upper)}]"
    return text


def _md_cell(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _fallacy_scan() -> list[dict[str, str]]:
    return [
        {
            "fallacy": "1. Simpson's paradox",
            "severity": "CAUTION",
            "detail": "总体 Full−Test-only 为 +1/57，但自然集为 −1/42、反事实为 +2/15；这不是经典同向子组反转，却说明总体值会掩盖明显的来源异质性。",
            "guardrail": "始终并列报告自然与反事实分层结果，不以总体排名替代配对主比较。",
        },
        {
            "fallacy": "2. Ecological fallacy",
            "severity": "NOTE",
            "detail": "分析单位是冻结轨迹；没有从聚合方法指标推断单个题目、开发者或一般模型行为。",
            "guardrail": "结论限定在本轮 57 条冻结轨迹。",
        },
        {
            "fallacy": "3. Berkson's paradox",
            "severity": "CAUTION",
            "detail": "自然轨迹只来自阶段一成功生成的 42/45 条，属于按可分析性筛选后的群体。",
            "guardrail": "不外推到全部生成尝试；同时保留 45 条来源核算。",
        },
        {
            "fallacy": "4. Collider bias",
            "severity": "NOTE",
            "detail": "未进行含控制变量的回归或条件化分析，本轮没有可识别的 collider 调整。",
            "guardrail": "不对未建模的控制路径作因果解释。",
        },
        {
            "fallacy": "5. Base-rate neglect",
            "severity": "CAUTION",
            "detail": "人工错误正类为 14/57；自然仅 2/42，反事实为 12/15。总体准确率受到负类占比和来源构成影响。",
            "guardrail": "同时报告正类基率、混淆计数、precision/recall 和分层准确率。",
        },
        {
            "fallacy": "6. Regression to the mean",
            "severity": "NOTE",
            "detail": "没有按极端前测分数选组的前后测设计，不适用。",
            "guardrail": "不使用改善或退步的前后测语言。",
        },
        {
            "fallacy": "7. Survivorship bias",
            "severity": "CAUTION",
            "detail": "3 条阶段一 Provider 失败没有完整轨迹，不能进入 57 条过程评估，但仍属于 45 条来源分母。",
            "guardrail": "明确区分来源覆盖与完整轨迹上的条件性研究结果。",
        },
        {
            "fallacy": "8. Look-elsewhere effect",
            "severity": "CAUTION",
            "detail": "两项预注册自然主比较使用 Holm；其余多指标和五类反事实拆分均为探索性描述。",
            "guardrail": "不从次要指标中事后挑选显著性结论。",
        },
        {
            "fallacy": "9. Garden of forking paths",
            "severity": "NOTE",
            "detail": "cohort、标签协议、主比较、seed 和失败分母均在比较前冻结；仍存在单标注者与工程选择带来的研究自由度。",
            "guardrail": "将全部结果标记为探索性并保留冻结哈希。",
        },
        {
            "fallacy": "10. Correlation is not causation",
            "severity": "CAUTION",
            "detail": "配对结果描述固定实现和预算下的关联差异，不能证明 AST、动态反例或四层结构本身造成性能变化。",
            "guardrail": "使用“观察到”而非“导致/提升”的因果措辞。",
        },
        {
            "fallacy": "11. Reverse causality",
            "severity": "NOTE",
            "detail": "没有时间方向或预测因果主张，不适用。",
            "guardrail": "不把标签与方法判断的对应关系解释为方向性因果。",
        },
    ]


def _build_validation(
    *,
    report_id: str,
    loaded: _LoadedReportInputs,
) -> dict[str, Any]:
    report = loaded.statistics_report
    cohort = _expect_mapping(report.get("cohort"), label="cohort summary")
    comparisons = _expect_mapping(report.get("primary_comparisons"), label="comparisons")
    natural = _expect_sequence(comparisons.get("natural"), label="natural comparisons")
    counterfactual = _expect_sequence(
        comparisons.get("counterfactual"),
        label="counterfactual comparisons",
    )
    if len(natural) != 2 or len(counterfactual) != 2:
        raise Phase3ReportError("primary comparison count differs", safe_stage="P3F_STATISTICS")
    fallacies = _fallacy_scan()
    validation = {
        "schema_version": 1,
        "kind": "tracejudge_phase3_validation_report",
        "report_id": report_id,
        "source": _expect_mapping(report.get("identities"), label="identities").get(
            "paired_run_id"
        ),
        "overall_confidence": "CAUTION",
        "verification_status": "ANALYZED",
        "statistical_findings": {
            "natural_primary_comparisons": list(natural),
            "counterfactual_primary_comparisons": list(counterfactual),
        },
        "warnings": [
            {
                "type": "single_rater",
                "detail": "只有一名主标注者和一轮标签；agreement_kind=not_computed。",
                "affected": "全部人工标签比较",
            },
            {
                "type": "cluster_count",
                "detail": "反事实只有 3 个父问题 cluster，bootstrap 区间不稳定。",
                "affected": "反事实配对差值",
            },
            {
                "type": "clustered_single_method_interval",
                "detail": "E4 中反事实单方法 Wilson 区间未建模同父题相关性，Gate F 不将其用于推断。",
                "affected": "反事实单方法准确率区间",
            },
            {
                "type": "structural_not_applicable",
                "detail": "Test-only 不输出过程、推理或计划代码字段；对应 0 分子是结构性不适用。",
                "affected": "Test-only 非二元检测指标",
            },
            {
                "type": "provider_failure",
                "detail": "285 对中 2 对为 Provider 连接失败，已按协议计入全分母错误并单独报告。",
                "affected": "Direct LLM Judge、Four-layer + AST",
            },
            {
                "type": "no_equivalence_claim",
                "detail": "p=1 或差值区间 [0,0] 都不能证明方法等效。",
                "affected": "所有无差异主比较",
            },
            {
                "type": "certificate_fixture_boundary",
                "detail": "Gate D 证书是公开工程 Fixture，不代表五方法在研究 cohort 上的证书有效率。",
                "affected": "错误证书 Demo",
            },
        ],
        "fallacy_scan": {
            "coverage": len(fallacies),
            "required": 11,
            "items": fallacies,
        },
        "reproducibility": {
            "method": "artifact_hash_verification_without_hy3_rerun",
            "verdict": "CANNOT_VERIFY",
            "detail": "统计、运行和证书产物已按精确字节哈希绑定；Gate F 不重跑外部 Hy3，且没有单独持久化的 replay receipt。",
            "statistics_report_sha256": loaded.statistics_report_sha256,
            "paired_results_sha256": loaded.paired_results_sha256,
            "certificate_sha256": loaded.certificate_sha256,
        },
        "cohort_summary": {
            "trace_count": _expect_int(cohort.get("trace_count"), label="trace count"),
            "method_count": _expect_int(cohort.get("method_count"), label="method count"),
            "pair_count": _expect_int(cohort.get("pair_count"), label="pair count"),
        },
        "material_passport": {
            "origin_skill": "academic-research-suite/experiment-agent",
            "origin_mode": "validate",
            "origin_date": "2026-08-31",
            "verification_status": "ANALYZED",
            "version_label": "phase3_gate_f_validation_v1",
        },
    }
    assert_public_payload_safe(validation)
    return validation


def _build_markdown(
    *,
    report_id: str,
    loaded: _LoadedReportInputs,
    validation: Mapping[str, Any],
) -> str:
    report = loaded.statistics_report
    methods = _method_metrics(report)
    cohort = _expect_mapping(report.get("cohort"), label="cohort summary")
    status_counts = _expect_mapping(cohort.get("final_status_counts"), label="status counts")
    comparisons = _expect_mapping(report.get("primary_comparisons"), label="comparisons")
    natural_comparisons = _expect_sequence(
        comparisons.get("natural"),
        label="natural comparisons",
    )
    counterfactual_comparisons = _expect_sequence(
        comparisons.get("counterfactual"),
        label="counterfactual comparisons",
    )
    mutation_rows = _expect_sequence(
        report.get("counterfactual_mutation_breakdown"),
        label="counterfactual breakdown",
    )
    identities = _expect_mapping(report.get("identities"), label="identities")

    lines = [
        "# TraceJudge-Hy3 阶段三研究验证报告",
        "",
        "## Material Passport",
        "",
        "- Origin Skill: academic-research-suite/experiment-agent",
        "- Origin Mode: validate",
        "- Origin Date: 2026-08-31",
        "- Verification Status: ANALYZED",
        "- Version Label: phase3_gate_f_report_v1",
        "",
        "## 1. 报告身份与结论边界",
        "",
        f"- Report ID: `{_md_cell(report_id)}`",
        f"- Paired run: `{_md_cell(identities.get('paired_run_id'))}`",
        f"- Statistics: `{_md_cell(report.get('statistics_id'))}`",
        f"- 研究规模：{cohort.get('natural_trace_count')} 条自然轨迹 + "
        f"{cohort.get('counterfactual_trace_count')} 条反事实轨迹 = "
        f"{cohort.get('trace_count')} 条；5 种方法，共 {cohort.get('pair_count')} 个配对。",
        "- 本报告只提供探索性证据，不代表完整 HumanEval+ 排名、标准 pass@k、普遍模型能力或因果效应。",
        "- 总体置信等级：**CAUTION**。核心原因是单标注者单轮次、自然错误基率低、反事实仅 3 个父题 cluster，以及 2 个 Provider 失败。",
        "",
        "## 2. 执行覆盖与失败核算",
        "",
        f"285 个配对中，`valid_judgment={status_counts.get('valid_judgment')}`，"
        f"`provider_error={status_counts.get('provider_error')}`；其余 parse/AST/公开执行超时/基础设施/skipped/reused 均为 0。",
        "两条 Provider 连接失败未重试、未删行，按预注册协议在主指标中计为错误并单独报告。",
        "",
        "| 方法 | 有效/总数 | Provider 失败 | 实际尝试 | JSON 修复 | 已知 Prompt tokens | 已知 Completion tokens | 总耗时（秒） | 金额成本 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for method_id in MethodId:
        runtime = loaded.runtime_accounting[method_id.value]
        statuses = _expect_mapping(runtime.get("status_counts"), label="runtime statuses")
        valid = _expect_int(statuses.get("valid_judgment", 0), label="valid count")
        provider_errors = _expect_int(statuses.get("provider_error", 0), label="provider count")
        pair_count = _expect_int(runtime.get("pair_count"), label="runtime pairs")
        cost_known = _expect_int(runtime.get("reported_cost_known_count"), label="cost count")
        cost_text = "不适用" if method_id == MethodId.TEST_ONLY else "不可用"
        if cost_known:
            cost_text = (
                f"{runtime.get('reported_cost_microusd_total_known_rows')} microusd / "
                f"{cost_known} rows"
            )
        lines.append(
            "| "
            + " | ".join(
                (
                    _METHOD_LABELS[method_id],
                    f"{valid}/{pair_count}",
                    str(provider_errors),
                    str(runtime.get("attempt_count_total")),
                    str(runtime.get("parse_repair_count_total")),
                    f"{runtime.get('prompt_token_total_known_rows')} / {runtime.get('prompt_token_known_count')} rows",
                    f"{runtime.get('completion_token_total_known_rows')} / {runtime.get('completion_token_known_count')} rows",
                    f"{float(runtime.get('duration_seconds_total', 0.0)):.1f}",
                    cost_text,
                )
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "金额成本未由 Provider 返回，不能由 token 数反推或声称精确费用。耗时为逐配对 duration 求和，不等同于端到端墙钟时间。",
            "",
            "## 3. 主终点：错误存在判断",
            "",
            "人工标签正类为 14/57（24.6%）：自然轨迹 2/42（4.8%），反事实轨迹 12/15（80.0%）。因此总体准确率必须与来源分层和混淆计数一起解释。",
            "",
            "| 方法 | 全部 57 条 | 自然 42 条 | 反事实 15 条 |",
            "|---|---|---|---|",
        ]
    )
    for method_id in MethodId:
        all_metric = _scope_metric(
            methods,
            method_id,
            "all",
            "error_detection_accuracy_full_denominator",
        )
        natural_metric = _scope_metric(
            methods,
            method_id,
            "natural",
            "error_detection_accuracy_full_denominator",
        )
        counterfactual_metric = _scope_metric(
            methods,
            method_id,
            "counterfactual",
            "error_detection_accuracy_full_denominator",
        )
        lines.append(
            f"| {_METHOD_LABELS[method_id]} | {_count_percent(all_metric)} | "
            f"{_count_percent(natural_metric)} | "
            f"{_count_percent(counterfactual_metric, include_interval=False)} |"
        )
    lines.extend(
        [
            "",
            "反事实单方法列只报告原始数和比例；E4 中的独立二项 Wilson 区间没有建模同父题相关性，Gate F 不将其用于推断。",
            "",
            "描述性地，Four-layer Structured Judge 为 56/57（98.2%），Full TraceJudge 为 55/57（96.5%）。这不是预注册的结构化方法对完整方法确认性比较，不能据此声称某组件有确定增益或损害。",
            "",
            "## 4. 预注册配对主比较",
            "",
            "### 4.1 自然轨迹：双侧精确 McNemar + Holm",
            "",
            "| 比较 | Full 正确 | 基线正确 | Full−基线 | n01（基线错/Full 对） | n10（基线对/Full 错） | exact p | Holm p |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for raw in natural_comparisons:
        item = _expect_mapping(raw, label="natural comparison")
        lines.append(
            f"| {_md_cell(item.get('comparison'))} | {item.get('full_correct')}/42 | "
            f"{item.get('baseline_correct')}/42 | "
            f"{float(item.get('accuracy_difference_full_minus_baseline')) * 100:+.1f} pp | "
            f"{item.get('n01_baseline_incorrect_full_correct')} | "
            f"{item.get('n10_baseline_correct_full_incorrect')} | "
            f"{float(item.get('exact_two_sided_mcnemar_p_value')):.3f} | "
            f"{float(item.get('holm_adjusted_p_value')):.3f} |"
        )
    lines.extend(
        [
            "",
            "自然集没有观察到 Full TraceJudge 优于两个预注册基线的证据：相对 Test-only 少 1 个正确判断，相对 Direct Judge 正确数相同；两个校正后 p 值均为 1.000。该结果不能反向证明方法等效。",
            "",
            "### 4.2 反事实：父题 cluster bootstrap",
            "",
            "| 比较 | Full 正确 | 基线正确 | Full−基线 | 95% cluster bootstrap CI | 父题 clusters |",
            "|---|---:|---:|---:|---|---:|",
        ]
    )
    for raw in counterfactual_comparisons:
        item = _expect_mapping(raw, label="counterfactual comparison")
        lower = float(item.get("cluster_bootstrap_95_lower")) * 100
        upper = float(item.get("cluster_bootstrap_95_upper")) * 100
        lines.append(
            f"| {_md_cell(item.get('comparison'))} | {item.get('full_correct')}/15 | "
            f"{item.get('baseline_correct')}/15 | "
            f"{float(item.get('accuracy_difference_full_minus_baseline')) * 100:+.1f} pp | "
            f"[{lower:+.1f}, {upper:+.1f}] pp | {item.get('parent_cluster_count')} |"
        )
    lines.extend(
        [
            "",
            "Full 相对 Test-only 多 2/15 个正确判断，差值 +13.3 pp，但区间含 0；相对 Direct Judge 为 0/15，区间 [0,0] 也不能作为等效证据，因为只有 3 个父题 cluster。",
            "",
            "## 5. 过程与首错定位（探索性）",
            "",
            "| 方法 | 过程判断 | 推理判断 | 计划—代码对齐 | 首错层 | 首错步骤 | 错误类型 |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for method_id in MethodId:
        if method_id == MethodId.TEST_ONLY:
            lines.append(
                "| Test-only | N/A | N/A | N/A | 2/14（14.3%） | 0/11（0.0%） | 2/14（14.3%） |"
            )
            continue
        values = [
            _scope_metric(methods, method_id, "all", "process_correct_accuracy_full_denominator"),
            _scope_metric(methods, method_id, "all", "reasoning_accuracy_full_denominator"),
            _scope_metric(
                methods,
                method_id,
                "all",
                "plan_code_alignment_accuracy_full_denominator",
            ),
            _scope_metric(methods, method_id, "all", "first_faulty_layer_accuracy_gold_errors"),
            _scope_metric(
                methods,
                method_id,
                "all",
                "first_faulty_step_accuracy_labeled_gold_steps",
            ),
            _scope_metric(methods, method_id, "all", "error_type_accuracy_gold_errors"),
        ]
        lines.append(
            f"| {_METHOD_LABELS[method_id]} | "
            + " | ".join(_count_percent(value, include_interval=False) for value in values)
            + " |"
        )
    lines.extend(
        [
            "",
            "Four-layer Structured Judge 的首错层和错误类型为 13/14；Four-layer + AST 的首错步骤为 10/11；Full TraceJudge 分别为 7/14、9/11、10/14。由于这些不是预注册确认性比较且分母很小，只能描述，不能据此归因组件效果。",
            "",
            "## 6. 反事实类型拆分",
            "",
            "| 修改类型 | Test-only | Direct | Structured | +AST | Full |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for raw in mutation_rows:
        item = _expect_mapping(raw, label="mutation row")
        values = _expect_sequence(
            item.get("method_error_detection"),
            label="mutation method metrics",
        )
        counts: list[str] = []
        for value in values:
            metric = _expect_mapping(value, label="mutation metric")
            numerator, denominator, _estimate = _count(
                _expect_mapping(
                    metric.get("accuracy_full_denominator"),
                    label="mutation accuracy",
                )
            )
            counts.append(f"{numerator}/{denominator}")
        lines.append(f"| {_md_cell(item.get('mutation_kind'))} | " + " | ".join(counts) + " |")
    lines.extend(
        [
            "",
            "Test-only 在 reasoning_swap 为 0/3，而四个 Judge 均为 3/3；在 equivalent_implementation 中 Test-only 为 3/3，其余方法均为 2/3。每类只有 3 条，不能形成普遍机制结论。",
            "",
            "## 7. 公开错误证书 Demo",
            "",
            "该 Demo 来自 Gate D 的公开自建 Fixture，不是从 HumanEval+ 隐藏失败输入恢复，也不代表五方法证书有效率。输出目录同时保存原始脱敏 JSON 证书的逐字节副本。",
            "",
            f"- Certificate ID: `{_md_cell(loaded.certificate.certificate_id)}`",
            f"- Problem: `{_md_cell(loaded.certificate.problem_id)}`",
            f"- Verdict: `{_md_cell(loaded.certificate.verdict)}`",
            f"- 公开需求：{_md_cell(loaded.certificate.violated_public_requirement)}",
            f"- 首错层 / 步骤：`{_md_cell(loaded.certificate.first_faulty_layer)}` / "
            f"`{_md_cell(loaded.certificate.first_faulty_step)}`",
            f"- 错误类型：`{_md_cell(loaded.certificate.error_type)}`",
            f"- 公开执行证据 SHA256：`{loaded.certificate.counterexample.execution_evidence_sha256}`",
            "- 受限公开 Fixture 已验证：`true`",
            "- Gate F replay receipt：未生成；本门槛不自动执行候选。",
            "",
            "重放命令：",
            "",
            "    " + " ".join(str(loaded.certificate.replay_command).splitlines()),
            "",
            "## 8. 统计验证与警告",
            "",
            "- Verification Status: **ANALYZED**",
            "- Overall Confidence: **CAUTION**",
            "- Reproducibility: **CANNOT_VERIFY**（Gate F 未重跑外部 Hy3；聚合输入与输出已进行精确哈希绑定）",
            "",
            "| 警告 | 影响 |",
            "|---|---|",
        ]
    )
    warnings = _expect_sequence(validation.get("warnings"), label="validation warnings")
    for raw in warnings:
        item = _expect_mapping(raw, label="warning")
        lines.append(f"| {_md_cell(item.get('detail'))} | {_md_cell(item.get('affected'))} |")
    lines.extend(
        [
            "",
            "## 9. 统计谬误扫描",
            "",
            "覆盖：**11/11**。CAUTION 表示需要限制解释，不表示数据必然错误。",
            "",
            "| 类型 | 严重度 | 检查结果 | 报告护栏 |",
            "|---|---|---|---|",
        ]
    )
    fallacy = _expect_mapping(validation.get("fallacy_scan"), label="fallacy scan")
    for raw in _expect_sequence(fallacy.get("items"), label="fallacy items"):
        item = _expect_mapping(raw, label="fallacy item")
        lines.append(
            f"| {_md_cell(item.get('fallacy'))} | {_md_cell(item.get('severity'))} | "
            f"{_md_cell(item.get('detail'))} | {_md_cell(item.get('guardrail'))} |"
        )
    lines.extend(
        [
            "",
            "## 10. 可支持与不可支持的结论",
            "",
            "可以支持：在本轮固定 Hy3、Prompt、单候选、57 条冻结轨迹和既定预算下，五方法已完成严格配对；完整方法在反事实 reasoning_swap 上捕获了 Test-only 看不到的说明错误，但没有在两个预注册主比较中显示确定优势。",
            "",
            "不能支持：完整 TraceJudge 普遍优于简单方法；AST 或动态反例造成了性能提升；p=1 证明方法等效；3 条/类型证明普遍机制；单标注者标签具有跨标注者一致性；Gate D 工程证书代表研究 cohort 的证书准确率。",
            "",
            "## 11. 审计身份",
            "",
            f"- Statistics manifest SHA256: `{loaded.statistics_manifest_sha256}`",
            f"- Statistics report SHA256: `{loaded.statistics_report_sha256}`",
            f"- Paired run manifest SHA256: `{loaded.paired_run_manifest_sha256}`",
            f"- Paired results SHA256: `{loaded.paired_results_sha256}`",
            f"- Paired index SHA256: `{loaded.paired_index_sha256}`",
            f"- Certificate manifest SHA256: `{loaded.certificate_manifest_sha256}`",
            f"- Confirmed certificate SHA256: `{loaded.certificate_sha256}`",
            "",
        ]
    )
    markdown = "\n".join(lines)
    assert_public_payload_safe(markdown)
    return markdown


def report_implementation_sha256() -> str:
    paths = (
        Path(__file__),
        Path(__file__).with_name("statistics.py"),
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


def _resolve_output(*, output_dir: str | Path, report_id: str) -> tuple[Path, Path]:
    if not _ID_PATTERN.fullmatch(report_id):
        raise Phase3ReportError(
            "report_id contains unsupported characters", safe_stage="P3F_OUTPUT"
        )
    root = Path(output_dir).expanduser()
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise Phase3ReportError("report output root is unsafe", safe_stage="P3F_OUTPUT")
    resolved = root.resolve()
    run_dir = resolved / report_id
    if run_dir.exists() or run_dir.is_symlink():
        raise Phase3ReportError("report directory already exists", safe_stage="P3F_OUTPUT")
    return resolved, run_dir


def _load_inputs(
    *,
    statistics_run_dir: str | Path,
    expected_statistics_manifest_sha256: str,
    expected_statistics_report_sha256: str,
    paired_run_dir: str | Path,
    certificate_run_dir: str | Path,
    expected_certificate_manifest_sha256: str,
    confirmed_certificate_path: str | Path,
    expected_confirmed_certificate_sha256: str,
    expected_replay_evidence_sha256: str,
) -> _LoadedReportInputs:
    statistics_manifest, statistics_report, statistics_manifest_sha, statistics_report_sha = (
        _load_statistics(
            statistics_run_dir=Path(statistics_run_dir),
            expected_manifest_sha256=expected_statistics_manifest_sha256,
            expected_report_sha256=expected_statistics_report_sha256,
        )
    )
    _validate_frozen_report(statistics_report)
    run_manifest, accounting, run_manifest_sha, results_sha, index_sha = _load_paired_runtime(
        paired_run_dir=Path(paired_run_dir),
        statistics_report=statistics_report,
    )
    _validate_runtime_accounting(accounting)
    (
        certificate_manifest,
        certificate,
        certificate_payload,
        certificate_manifest_sha,
        certificate_sha,
    ) = _load_certificate(
        certificate_run_dir=Path(certificate_run_dir),
        expected_manifest_sha256=expected_certificate_manifest_sha256,
        certificate_path=Path(confirmed_certificate_path),
        expected_certificate_sha256=expected_confirmed_certificate_sha256,
        expected_replay_evidence_sha256=expected_replay_evidence_sha256,
        statistics_report=statistics_report,
    )
    return _LoadedReportInputs(
        statistics_manifest=statistics_manifest,
        statistics_report=statistics_report,
        statistics_manifest_sha256=statistics_manifest_sha,
        statistics_report_sha256=statistics_report_sha,
        run_manifest=run_manifest,
        runtime_accounting=accounting,
        paired_run_manifest_sha256=run_manifest_sha,
        paired_results_sha256=results_sha,
        paired_index_sha256=index_sha,
        certificate_manifest=certificate_manifest,
        certificate=certificate,
        certificate_payload=certificate_payload,
        certificate_manifest_sha256=certificate_manifest_sha,
        certificate_sha256=certificate_sha,
    )


def _prepare_report(
    *,
    report_id: str,
    statistics_run_dir: str | Path,
    expected_statistics_manifest_sha256: str,
    expected_statistics_report_sha256: str,
    paired_run_dir: str | Path,
    certificate_run_dir: str | Path,
    expected_certificate_manifest_sha256: str,
    confirmed_certificate_path: str | Path,
    expected_confirmed_certificate_sha256: str,
    expected_replay_evidence_sha256: str,
    output_dir: str | Path,
    allow_dirty: bool = False,
    privacy_canaries: Sequence[str | bytes] = (),
) -> _PreparedReport:
    loaded = _load_inputs(
        statistics_run_dir=statistics_run_dir,
        expected_statistics_manifest_sha256=expected_statistics_manifest_sha256,
        expected_statistics_report_sha256=expected_statistics_report_sha256,
        paired_run_dir=paired_run_dir,
        certificate_run_dir=certificate_run_dir,
        expected_certificate_manifest_sha256=expected_certificate_manifest_sha256,
        confirmed_certificate_path=confirmed_certificate_path,
        expected_confirmed_certificate_sha256=expected_confirmed_certificate_sha256,
        expected_replay_evidence_sha256=expected_replay_evidence_sha256,
    )
    output_root, run_dir = _resolve_output(output_dir=output_dir, report_id=report_id)
    git = _git_metadata(Path.cwd(), excluded_paths=(output_root,))
    if (
        not git["available"]
        or not isinstance(git["commit"], str)
        or not isinstance(git["branch"], str)
        or not isinstance(git["dirty"], bool)
    ):
        raise Phase3ReportError("Git identity is unavailable", safe_stage="P3F_GIT")
    if git["dirty"] and not allow_dirty:
        raise Phase3ReportError(
            "Git worktree is dirty; commit Gate-F implementation before formal reporting",
            safe_stage="P3F_GIT_DIRTY",
        )
    validation = _build_validation(report_id=report_id, loaded=loaded)
    markdown = _build_markdown(report_id=report_id, loaded=loaded, validation=validation)
    validation_payload = _pretty_json(validation)
    markdown_payload = markdown.encode("utf-8") + b"\n"
    replay_command_payload = (
        " ".join(str(loaded.certificate.replay_command).splitlines()).encode("utf-8") + b"\n"
    )
    for payload in (
        validation_payload,
        markdown_payload,
        loaded.certificate_payload,
        replay_command_payload,
    ):
        assert_public_payload_safe(payload, canaries=privacy_canaries)
    cohort = _expect_mapping(loaded.statistics_report.get("cohort"), label="cohort")
    statuses = _expect_mapping(cohort.get("final_status_counts"), label="status counts")
    implementation_sha = report_implementation_sha256()
    preflight = Phase3ReportPreflight(
        report_id=report_id,
        statistics_id=str(loaded.statistics_report.get("statistics_id")),
        paired_run_id=loaded.run_manifest.run_id,
        trace_count=_expect_int(cohort.get("trace_count"), label="trace count"),
        method_count=_expect_int(cohort.get("method_count"), label="method count"),
        pair_count=_expect_int(cohort.get("pair_count"), label="pair count"),
        valid_judgment_count=_expect_int(
            statuses.get("valid_judgment"),
            label="valid judgment count",
        ),
        provider_error_count=_expect_int(
            statuses.get("provider_error"),
            label="provider error count",
        ),
        overall_confidence="CAUTION",
        fallacy_scan_coverage=11,
        statistics_manifest_sha256=loaded.statistics_manifest_sha256,
        statistics_report_sha256=loaded.statistics_report_sha256,
        paired_run_manifest_sha256=loaded.paired_run_manifest_sha256,
        paired_results_sha256=loaded.paired_results_sha256,
        paired_index_sha256=loaded.paired_index_sha256,
        certificate_manifest_sha256=loaded.certificate_manifest_sha256,
        confirmed_certificate_sha256=loaded.certificate_sha256,
        replay_evidence_sha256=expected_replay_evidence_sha256,
        report_implementation_sha256=implementation_sha,
        markdown_sha256=_sha256(markdown_payload),
        validation_sha256=_sha256(validation_payload),
        replay_command_sha256=_sha256(replay_command_payload),
        git_commit=git["commit"],
        git_branch=git["branch"],
        git_dirty=git["dirty"],
    )
    return _PreparedReport(
        preflight=preflight,
        markdown_payload=markdown_payload,
        validation_payload=validation_payload,
        certificate_payload=loaded.certificate_payload,
        replay_command_payload=replay_command_payload,
        output_root=output_root,
        run_dir=run_dir,
    )


def preflight_phase3_report(**kwargs: Any) -> Phase3ReportPreflight:
    """Validate and render Gate F in memory without writing artifacts."""

    return _prepare_report(**kwargs).preflight


def generate_phase3_report(**kwargs: Any) -> Phase3ReportResult:
    """Atomically publish one de-identified Gate-F report bundle."""

    prepared = _prepare_report(**kwargs)
    for payload, expected, label in (
        (
            prepared.markdown_payload,
            prepared.preflight.markdown_sha256,
            "Markdown report",
        ),
        (
            prepared.validation_payload,
            prepared.preflight.validation_sha256,
            "validation report",
        ),
        (
            prepared.certificate_payload,
            prepared.preflight.confirmed_certificate_sha256,
            "certificate Demo",
        ),
        (
            prepared.replay_command_payload,
            prepared.preflight.replay_command_sha256,
            "replay command",
        ),
    ):
        if _sha256(payload) != expected:
            raise Phase3ReportError(
                f"prepared {label} identity differs",
                safe_stage="P3F_OUTPUT",
            )
    prepared.output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    prepared.output_root.chmod(0o700)
    temporary_dir: Path | None = None
    try:
        temporary_dir = Path(
            tempfile.mkdtemp(prefix=f".{prepared.preflight.report_id}.", dir=prepared.output_root)
        )
        temporary_dir.chmod(0o700)
        _write_new_file(temporary_dir / "phase3_research_report.md", prepared.markdown_payload)
        _write_new_file(temporary_dir / "validation.json", prepared.validation_payload)
        _write_new_file(temporary_dir / "demo_certificate.json", prepared.certificate_payload)
        _write_new_file(temporary_dir / "replay_command.txt", prepared.replay_command_payload)
        manifest = {
            "schema_version": 1,
            "phase": "phase3_deidentified_research_report",
            "status": "completed",
            "report_id": prepared.preflight.report_id,
            "created_at": datetime.now(UTC),
            "statistics_id": prepared.preflight.statistics_id,
            "paired_run_id": prepared.preflight.paired_run_id,
            "statistics_manifest_sha256": prepared.preflight.statistics_manifest_sha256,
            "statistics_report_sha256": prepared.preflight.statistics_report_sha256,
            "paired_run_manifest_sha256": prepared.preflight.paired_run_manifest_sha256,
            "paired_results_sha256": prepared.preflight.paired_results_sha256,
            "paired_index_sha256": prepared.preflight.paired_index_sha256,
            "certificate_manifest_sha256": prepared.preflight.certificate_manifest_sha256,
            "confirmed_certificate_sha256": prepared.preflight.confirmed_certificate_sha256,
            "replay_evidence_sha256": prepared.preflight.replay_evidence_sha256,
            "report_implementation_sha256": prepared.preflight.report_implementation_sha256,
            "markdown_sha256": prepared.preflight.markdown_sha256,
            "validation_sha256": prepared.preflight.validation_sha256,
            "demo_certificate_sha256": prepared.preflight.confirmed_certificate_sha256,
            "replay_command_sha256": prepared.preflight.replay_command_sha256,
            "git_commit": prepared.preflight.git_commit,
            "git_branch": prepared.preflight.git_branch,
            "git_dirty": prepared.preflight.git_dirty,
            "python_version": platform.python_version(),
            "direct_dependencies_sha256": canonical_sha256(_dependency_versions()),
            "verification_status": "ANALYZED",
            "overall_confidence": prepared.preflight.overall_confidence,
            "fallacy_scan_coverage": prepared.preflight.fallacy_scan_coverage,
            "contains_per_trace_predictions": False,
            "contains_annotation_rationales": False,
            "contains_provider_raw": False,
            "contains_hidden_evaluation_content": False,
            "contains_public_counterexample": True,
            "contains_certificate_replay_receipt": False,
        }
        assert_public_payload_safe(manifest)
        _write_new_file(temporary_dir / "manifest.json", _pretty_json(manifest))
        if prepared.run_dir.exists() or prepared.run_dir.is_symlink():
            raise Phase3ReportError(
                "report directory appeared during publication",
                safe_stage="P3F_OUTPUT",
            )
        os.replace(temporary_dir, prepared.run_dir)
        temporary_dir = None
        _fsync_directory(prepared.output_root)
    except OSError:
        raise Phase3ReportError(
            "cannot atomically publish report",
            safe_stage="P3F_OUTPUT",
        ) from None
    finally:
        if temporary_dir is not None:
            shutil.rmtree(temporary_dir, ignore_errors=True)
    manifest_path = prepared.run_dir / "manifest.json"
    return Phase3ReportResult(
        **asdict(prepared.preflight),
        run_dir=prepared.run_dir,
        manifest_path=manifest_path,
        markdown_path=prepared.run_dir / "phase3_research_report.md",
        validation_path=prepared.run_dir / "validation.json",
        demo_certificate_path=prepared.run_dir / "demo_certificate.json",
        replay_command_path=prepared.run_dir / "replay_command.txt",
        manifest_sha256=_sha256(manifest_path.read_bytes()),
    )


__all__ = [
    "Phase3ReportError",
    "Phase3ReportPreflight",
    "Phase3ReportResult",
    "generate_phase3_report",
    "preflight_phase3_report",
    "report_implementation_sha256",
]
