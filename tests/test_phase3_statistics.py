from __future__ import annotations

import hashlib
import json
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

import tracejudge_hy3.cli as cli_module
import tracejudge_hy3.phase3.statistics as statistics_module
from tracejudge_hy3.cli import app
from tracejudge_hy3.phase3.contracts import (
    AnnotationRecord,
    MethodId,
    MethodJudgment,
    MethodOutcome,
)
from tracejudge_hy3.phase3.statistics import (
    Phase3StatisticsError,
    Phase3StatisticsPreflight,
    _EffectiveOutcome,
    _method_scope_metrics,
    cluster_bootstrap_interval,
    exact_mcnemar_p_value,
    generate_phase3_statistics,
    holm_adjust,
    percentile_type7,
    wilson_interval,
)

H0 = "0" * 64
H1 = "1" * 64
H2 = "2" * 64
H3 = "3" * 64
H4 = "4" * 64
NOW = datetime(2026, 8, 31, tzinfo=UTC)


def _annotation(trace_id: str, *, has_error: bool) -> AnnotationRecord:
    common = {
        "trace_id": trace_id,
        "code_sha256": H0,
        "structured_explanation_sha256": H1,
        "functional_evidence_sha256": H2,
        "annotation_protocol_sha256": H3,
        "rater_id": "primary_rater",
        "annotation_round": 1,
        "blinded_to_method_predictions": True,
        "blinded_to_other_raters": True,
        "process_correct": not has_error,
        "has_error": has_error,
        "reasoning_correct": not has_error,
        "plan_code_aligned": not has_error,
        "rationale": "fixture rationale",
    }
    if has_error:
        common.update(
            {
                "first_faulty_layer": "reasoning",
                "first_faulty_step": "step_1",
                "error_type": "P01_ALGORITHM_ERROR",
            }
        )
    return AnnotationRecord.model_validate(common)


def _outcome(
    trace_id: str,
    method_id: MethodId,
    *,
    judgment: MethodJudgment | None,
) -> MethodOutcome:
    valid = judgment is not None
    return MethodOutcome(
        run_id="phase3_fixture",
        trace_id=trace_id,
        method_id=method_id,
        status="valid_judgment" if valid else "provider_error",
        method_input_sha256=H4,
        judgment=judgment,
        attempt_count=1,
        parse_repair_count=0,
        started_at=NOW,
        ended_at=NOW,
        duration_seconds=0.1,
    )


def _preflight(
    statistics_id: str = "phase3_stats_fixture",
    *,
    report_sha256: str = H2,
) -> Phase3StatisticsPreflight:
    return Phase3StatisticsPreflight(
        statistics_id=statistics_id,
        paired_run_id="phase3_run_fixture",
        annotation_set_id="phase3_labels_fixture",
        natural_trace_count=42,
        counterfactual_trace_count=15,
        trace_count=57,
        method_count=5,
        pair_count=285,
        final_status_counts={"valid_judgment": 283, "provider_error": 2},
        paired_run_manifest_sha256=H0,
        paired_results_sha256=H1,
        paired_index_sha256=H2,
        annotation_set_manifest_sha256=H3,
        completed_labels_sha256=H4,
        annotation_records_sha256=H4,
        protocol_sha256=H0,
        statistics_implementation_sha256=H1,
        report_sha256=report_sha256,
        git_commit="a" * 40,
        git_branch="phase3-process-evaluation",
        git_dirty=False,
    )


def test_exact_mcnemar_holm_wilson_and_percentile_contracts():
    assert exact_mcnemar_p_value(0, 0) == 1.0
    assert exact_mcnemar_p_value(3, 0) == 0.25
    assert holm_adjust((0.01, 0.04)) == pytest.approx((0.02, 0.04))
    assert wilson_interval(0, 0) == (None, None)
    lower, upper = wilson_interval(5, 10)
    assert lower == pytest.approx(0.236593, abs=1e-6)
    assert upper == pytest.approx(0.763407, abs=1e-6)
    assert percentile_type7((0.0, 10.0), 0.25) == pytest.approx(2.5)


def test_cluster_bootstrap_is_seeded_and_resamples_whole_clusters():
    clusters = {"parent_a": (1, 1), "parent_b": (-1, -1), "parent_c": (0, 0)}
    first = cluster_bootstrap_interval(
        clusters,
        iterations=1000,
        seed=20260828,
        confidence_level=0.95,
    )
    second = cluster_bootstrap_interval(
        clusters,
        iterations=1000,
        seed=20260828,
        confidence_level=0.95,
    )
    assert first == second
    assert first[0] <= 0.0 <= first[1]


def test_invalid_outcome_stays_in_full_denominator_and_is_reported_separately():
    method_id = MethodId.FULL_TRACEJUDGE
    error_judgment = MethodJudgment(
        functional_correct=False,
        has_error=True,
        reasoning_correct=False,
        plan_code_aligned=False,
        process_correct=False,
        first_faulty_layer="reasoning",
        first_faulty_step="step_1",
        error_type="P01_ALGORITHM_ERROR",
        verdict="strongly_supported",
        evidence_summary=("fixture evidence",),
    )
    valid = _outcome("trace_1", method_id, judgment=error_judgment)
    invalid = _outcome("trace_2", method_id, judgment=None)
    metrics = _method_scope_metrics(
        trace_ids=("trace_1", "trace_2"),
        method_id=method_id,
        annotations={
            "trace_1": _annotation("trace_1", has_error=True),
            "trace_2": _annotation("trace_2", has_error=False),
        },
        outcomes={
            ("trace_1", method_id): _EffectiveOutcome(final=valid, effective=valid),
            ("trace_2", method_id): _EffectiveOutcome(final=invalid, effective=invalid),
        },
    )

    assert metrics["denominator"] == 2
    assert metrics["invalid_effective_outcomes"] == 1
    assert metrics["error_detection_accuracy_full_denominator"]["numerator"] == 1
    assert metrics["error_detection_accuracy_full_denominator"]["denominator"] == 2
    assert metrics["valid_only_confusion"]["valid_prediction_count"] == 1
    assert metrics["valid_only_confusion"]["true_positive"] == 1


def test_statistics_writer_is_private_atomic_and_refuses_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    report_payload = b'{"kind":"tracejudge_phase3_paired_statistics"}\n'
    prepared = statistics_module._PreparedStatistics(
        preflight=_preflight(report_sha256=hashlib.sha256(report_payload).hexdigest()),
        report={"kind": "tracejudge_phase3_paired_statistics"},
        report_payload=report_payload,
        output_root=tmp_path,
        run_dir=tmp_path / "phase3_stats_fixture",
    )
    monkeypatch.setattr(statistics_module, "_prepare_statistics", lambda **_kwargs: prepared)

    result = generate_phase3_statistics()

    assert result.report_path.read_bytes() == report_payload
    assert stat.S_IMODE(result.run_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(result.report_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(result.manifest_path.stat().st_mode) == 0o600
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["contains_per_trace_rows"] is False
    assert manifest["contains_annotation_rationales"] is False
    assert manifest["report_sha256"] == hashlib.sha256(report_payload).hexdigest()

    with pytest.raises(Phase3StatisticsError, match="already exists"):
        statistics_module._resolve_output(
            output_dir=tmp_path,
            statistics_id="phase3_stats_fixture",
        )


def test_cli_statistics_preflight_displays_identities_not_method_metrics(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(cli_module, "preflight_phase3_statistics", lambda **_kwargs: _preflight())
    result = CliRunner().invoke(
        app,
        [
            "phase3",
            "statistics-preflight",
            "--statistics-id",
            "phase3_stats_fixture",
            "--paired-run",
            "paired-run",
            "--paired-run-manifest-sha256",
            H0,
            "--results-sha256",
            H1,
            "--index-sha256",
            H2,
            "--cohort-manifest",
            "cohort.json",
            "--natural-manifest",
            "natural.json",
            "--annotation-set-manifest",
            "labels.json",
            "--annotation-set-manifest-sha256",
            H3,
        ],
    )

    assert result.exit_code == 0
    assert "57 / 5 / 285" in result.stdout
    assert "valid_judgment=283" in result.stdout
    assert "标签分布或方法结果" in result.stdout
    assert "accuracy" not in result.stdout.casefold()
