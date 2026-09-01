from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import tracejudge_hy3.cli as cli_module
import tracejudge_hy3.phase3.report as report_module
from tracejudge_hy3.cli import app
from tracejudge_hy3.phase3.contracts import MethodId
from tracejudge_hy3.phase3.report import (
    Phase3ReportError,
    Phase3ReportPreflight,
    generate_phase3_report,
)

H0 = "0" * 64
H1 = "1" * 64
H2 = "2" * 64
H3 = "3" * 64
H4 = "4" * 64


def _preflight(
    *,
    markdown_sha256: str = H0,
    validation_sha256: str = H1,
    certificate_sha256: str = H2,
    replay_command_sha256: str = H3,
) -> Phase3ReportPreflight:
    return Phase3ReportPreflight(
        report_id="phase3_report_fixture",
        statistics_id="phase3_stats_fixture",
        paired_run_id="phase3_run_fixture",
        trace_count=57,
        method_count=5,
        pair_count=285,
        valid_judgment_count=283,
        provider_error_count=2,
        overall_confidence="CAUTION",
        fallacy_scan_coverage=11,
        statistics_manifest_sha256=H0,
        statistics_report_sha256=H1,
        paired_run_manifest_sha256=H2,
        paired_results_sha256=H3,
        paired_index_sha256=H4,
        certificate_manifest_sha256=H0,
        confirmed_certificate_sha256=certificate_sha256,
        replay_evidence_sha256=H2,
        report_implementation_sha256=H3,
        markdown_sha256=markdown_sha256,
        validation_sha256=validation_sha256,
        replay_command_sha256=replay_command_sha256,
        git_commit="a" * 40,
        git_branch="phase3-process-evaluation",
        git_dirty=False,
    )


def _proportion(numerator: int, denominator: int) -> dict[str, float | int]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "estimate": numerator / denominator,
        "wilson_95_lower": 0.0,
        "wilson_95_upper": 1.0,
    }


def _frozen_report_fixture() -> dict[str, object]:
    detection = {
        MethodId.TEST_ONLY: ((54, 57), (42, 42), (12, 15)),
        MethodId.DIRECT_LLM_JUDGE: ((55, 57), (41, 42), (14, 15)),
        MethodId.FOUR_LAYER_STRUCTURED_JUDGE: ((56, 57), (42, 42), (14, 15)),
        MethodId.FOUR_LAYER_AST: ((54, 57), (40, 42), (14, 15)),
        MethodId.FULL_TRACEJUDGE: ((55, 57), (41, 42), (14, 15)),
    }
    localization = {
        MethodId.TEST_ONLY: ((2, 14), (0, 11), (2, 14)),
        MethodId.DIRECT_LLM_JUDGE: ((9, 14), (9, 11), (11, 14)),
        MethodId.FOUR_LAYER_STRUCTURED_JUDGE: ((13, 14), (8, 11), (13, 14)),
        MethodId.FOUR_LAYER_AST: ((10, 14), (10, 11), (13, 14)),
        MethodId.FULL_TRACEJUDGE: ((7, 14), (9, 11), (10, 14)),
    }
    prevalence = ((14, 57), (2, 42), (12, 15))
    process_metrics = {
        MethodId.TEST_ONLY: (0, 0, 0),
        MethodId.DIRECT_LLM_JUDGE: (49, 53, 55),
        MethodId.FOUR_LAYER_STRUCTURED_JUDGE: (49, 54, 52),
        MethodId.FOUR_LAYER_AST: (48, 54, 54),
        MethodId.FULL_TRACEJUDGE: (50, 56, 55),
    }
    method_metrics: list[dict[str, object]] = []
    for method_id in MethodId:
        scopes: dict[str, dict[str, object]] = {}
        for scope_index, scope in enumerate(("all", "natural", "counterfactual")):
            numerator, denominator = detection[method_id][scope_index]
            scopes[scope] = {
                "error_detection_accuracy_full_denominator": _proportion(
                    numerator,
                    denominator,
                ),
                "gold_error_prevalence": _proportion(*prevalence[scope_index]),
            }
        layer, step, error_type = localization[method_id]
        scopes["all"].update(
            {
                "first_faulty_layer_accuracy_gold_errors": _proportion(*layer),
                "first_faulty_step_accuracy_labeled_gold_steps": _proportion(*step),
                "error_type_accuracy_gold_errors": _proportion(*error_type),
            }
        )
        process, reasoning, alignment = process_metrics[method_id]
        scopes["all"].update(
            {
                "process_correct_accuracy_full_denominator": _proportion(process, 57),
                "reasoning_accuracy_full_denominator": _proportion(reasoning, 57),
                "plan_code_alignment_accuracy_full_denominator": _proportion(alignment, 57),
            }
        )
        method_metrics.append({"method_id": method_id.value, "scopes": scopes})

    mutation_counts = (
        ("reasoning_swap", (0, 3, 3, 3, 3)),
        ("code_defect", (3, 3, 3, 3, 3)),
        ("boundary_deletion", (3, 3, 3, 3, 3)),
        ("shortcut", (3, 3, 3, 3, 3)),
        ("equivalent_implementation", (3, 2, 2, 2, 2)),
    )
    mutations = []
    for kind, counts in mutation_counts:
        mutations.append(
            {
                "mutation_kind": kind,
                "method_error_detection": [
                    {
                        "method_id": method_id.value,
                        "accuracy_full_denominator": _proportion(count, 3),
                    }
                    for method_id, count in zip(MethodId, counts, strict=True)
                ],
            }
        )
    return {
        "statistics_id": "phase3_stats_fixture",
        "identities": {"paired_run_id": "phase3_run_fixture"},
        "cohort": {
            "natural_trace_count": 42,
            "counterfactual_trace_count": 15,
            "counterfactual_parent_cluster_count": 3,
            "trace_count": 57,
            "method_count": 5,
            "pair_count": 285,
            "final_status_counts": {
                "ast_error": 0,
                "infrastructure_error": 0,
                "parse_error": 0,
                "provider_error": 2,
                "public_execution_timeout": 0,
                "reused": 0,
                "skipped": 0,
                "valid_judgment": 283,
            },
        },
        "method_metrics": method_metrics,
        "primary_comparisons": {
            "natural": [
                {
                    "comparison": "full_tracejudge_vs_test_only",
                    "full_correct": 41,
                    "baseline_correct": 42,
                    "n01_baseline_incorrect_full_correct": 0,
                    "n10_baseline_correct_full_incorrect": 1,
                    "exact_two_sided_mcnemar_p_value": 1.0,
                    "holm_adjusted_p_value": 1.0,
                    "accuracy_difference_full_minus_baseline": -1 / 42,
                    "denominator": 42,
                },
                {
                    "comparison": "full_tracejudge_vs_direct_llm_judge",
                    "full_correct": 41,
                    "baseline_correct": 41,
                    "n01_baseline_incorrect_full_correct": 1,
                    "n10_baseline_correct_full_incorrect": 1,
                    "exact_two_sided_mcnemar_p_value": 1.0,
                    "holm_adjusted_p_value": 1.0,
                    "accuracy_difference_full_minus_baseline": 0.0,
                    "denominator": 42,
                },
            ],
            "counterfactual": [
                {
                    "comparison": "full_tracejudge_vs_test_only",
                    "full_correct": 14,
                    "baseline_correct": 12,
                    "cluster_bootstrap_95_lower": 0.0,
                    "cluster_bootstrap_95_upper": 0.2,
                    "parent_cluster_count": 3,
                    "accuracy_difference_full_minus_baseline": 2 / 15,
                    "denominator": 15,
                },
                {
                    "comparison": "full_tracejudge_vs_direct_llm_judge",
                    "full_correct": 14,
                    "baseline_correct": 14,
                    "cluster_bootstrap_95_lower": 0.0,
                    "cluster_bootstrap_95_upper": 0.0,
                    "parent_cluster_count": 3,
                    "accuracy_difference_full_minus_baseline": 0.0,
                    "denominator": 15,
                },
            ],
        },
        "counterfactual_mutation_breakdown": mutations,
    }


def _runtime_fixture() -> dict[str, dict[str, object]]:
    accounting = {}
    for method_id in MethodId:
        provider_errors = int(method_id in {MethodId.DIRECT_LLM_JUDGE, MethodId.FOUR_LAYER_AST})
        cost_status = "not_applicable" if method_id == MethodId.TEST_ONLY else "unavailable"
        accounting[method_id.value] = {
            "pair_count": 57,
            "status_counts": {
                "valid_judgment": 57 - provider_errors,
                **({"provider_error": provider_errors} if provider_errors else {}),
            },
            "duration_seconds_total": 1.0,
            "attempt_count_total": 0 if method_id == MethodId.TEST_ONLY else 57,
            "parse_repair_count_total": 0,
            "prompt_token_known_count": 0 if method_id == MethodId.TEST_ONLY else 57,
            "prompt_token_total_known_rows": 0 if method_id == MethodId.TEST_ONLY else 100,
            "completion_token_known_count": (0 if method_id == MethodId.TEST_ONLY else 57),
            "completion_token_total_known_rows": (0 if method_id == MethodId.TEST_ONLY else 100),
            "reported_cost_known_count": 0,
            "reported_cost_microusd_total_known_rows": 0,
            "cost_status_counts": {cost_status: 57},
            "diagnostic_counts": ({"provider_connection_error": 1} if provider_errors else {}),
        }
    return accounting


def test_fallacy_scan_covers_all_required_checks_once():
    items = report_module._fallacy_scan()

    assert len(items) == 11
    assert len({item["fallacy"] for item in items}) == 11
    assert {item["severity"] for item in items} == {"CAUTION", "NOTE"}


def test_frozen_report_guard_accepts_bound_aggregates_and_rejects_drift():
    aggregate = _frozen_report_fixture()
    report_module._validate_frozen_report(aggregate)

    aggregate["cohort"]["trace_count"] = 58  # type: ignore[index]
    with pytest.raises(Phase3ReportError, match="trace_count differs"):
        report_module._validate_frozen_report(aggregate)


def test_runtime_guard_requires_unknown_cost_and_preserved_provider_failures():
    accounting = _runtime_fixture()

    report_module._validate_runtime_accounting(accounting)
    accounting[MethodId.DIRECT_LLM_JUDGE.value]["reported_cost_known_count"] = 1
    with pytest.raises(Phase3ReportError, match="reported cost count differs"):
        report_module._validate_runtime_accounting(accounting)


def test_markdown_interpretation_preserves_statistical_boundaries():
    loaded = report_module._LoadedReportInputs(
        statistics_manifest={},
        statistics_report=_frozen_report_fixture(),
        statistics_manifest_sha256=H0,
        statistics_report_sha256=H1,
        run_manifest=None,  # type: ignore[arg-type]
        runtime_accounting=_runtime_fixture(),
        paired_run_manifest_sha256=H2,
        paired_results_sha256=H3,
        paired_index_sha256=H4,
        certificate_manifest=None,  # type: ignore[arg-type]
        certificate=SimpleNamespace(
            certificate_id="certificate:public-fixture",
            problem_id="safe_mean",
            verdict="confirmed_bug",
            violated_public_requirement="return zero for an empty list",
            first_faulty_layer="implementation",
            first_faulty_step="step_1",
            error_type="boundary_condition",
            counterexample=SimpleNamespace(execution_evidence_sha256=H2),
            replay_command="tracejudge phase3 replay --certificate public.json",
        ),
        certificate_payload=b'{"certificate_id":"public-fixture"}\n',
        certificate_manifest_sha256=H3,
        certificate_sha256=H4,
    )

    validation = report_module._build_validation(
        report_id="phase3_report_fixture",
        loaded=loaded,
    )
    markdown = report_module._build_markdown(
        report_id="phase3_report_fixture",
        loaded=loaded,
        validation=validation,
    )

    assert validation["verification_status"] == "ANALYZED"
    assert validation["overall_confidence"] == "CAUTION"
    assert validation["reproducibility"]["verdict"] == "CANNOT_VERIFY"
    assert validation["fallacy_scan"]["coverage"] == 11
    assert "| Test-only | N/A | N/A | N/A |" in markdown
    assert "反事实单方法列只报告原始数和比例" in markdown
    assert "不能反向证明方法等效" in markdown
    assert "不能据此归因组件效果" in markdown
    assert "trace_id" not in json.dumps(validation, ensure_ascii=False)


def test_report_writer_is_private_atomic_and_refuses_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    markdown = b"# de-identified report\n"
    validation = b'{"fallacy_scan":{"coverage":11}}\n'
    certificate = b'{"certificate_id":"public-fixture"}\n'
    replay = b"tracejudge phase3 replay --certificate public-fixture.json\n"
    preflight = _preflight(
        markdown_sha256=hashlib.sha256(markdown).hexdigest(),
        validation_sha256=hashlib.sha256(validation).hexdigest(),
        certificate_sha256=hashlib.sha256(certificate).hexdigest(),
        replay_command_sha256=hashlib.sha256(replay).hexdigest(),
    )
    prepared = report_module._PreparedReport(
        preflight=preflight,
        markdown_payload=markdown,
        validation_payload=validation,
        certificate_payload=certificate,
        replay_command_payload=replay,
        output_root=tmp_path,
        run_dir=tmp_path / preflight.report_id,
    )
    monkeypatch.setattr(report_module, "_prepare_report", lambda **_kwargs: prepared)

    result = generate_phase3_report()

    assert result.markdown_path.read_bytes() == markdown
    assert result.validation_path.read_bytes() == validation
    assert result.demo_certificate_path.read_bytes() == certificate
    assert result.replay_command_path.read_bytes() == replay
    assert stat.S_IMODE(result.run_dir.stat().st_mode) == 0o700
    for path in (
        result.manifest_path,
        result.markdown_path,
        result.validation_path,
        result.demo_certificate_path,
        result.replay_command_path,
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["contains_per_trace_predictions"] is False
    assert manifest["contains_annotation_rationales"] is False
    assert manifest["contains_public_counterexample"] is True
    assert manifest["contains_certificate_replay_receipt"] is False

    with pytest.raises(Phase3ReportError, match="already exists"):
        report_module._resolve_output(
            output_dir=tmp_path,
            report_id=preflight.report_id,
        )


def test_report_writer_rechecks_prepared_payload_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    prepared = report_module._PreparedReport(
        preflight=_preflight(),
        markdown_payload=b"tampered\n",
        validation_payload=b"validation\n",
        certificate_payload=b"certificate\n",
        replay_command_payload=b"replay\n",
        output_root=tmp_path,
        run_dir=tmp_path / "phase3_report_fixture",
    )
    monkeypatch.setattr(report_module, "_prepare_report", lambda **_kwargs: prepared)

    with pytest.raises(Phase3ReportError, match="identity differs"):
        generate_phase3_report()
    assert not prepared.run_dir.exists()


def test_cli_report_preflight_displays_only_safe_summary(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(cli_module, "preflight_phase3_report", lambda **_kwargs: _preflight())
    result = CliRunner().invoke(
        app,
        [
            "phase3",
            "report-preflight",
            "--report-id",
            "phase3_report_fixture",
            "--statistics-run",
            "statistics-run",
            "--statistics-manifest-sha256",
            H0,
            "--statistics-report-sha256",
            H1,
            "--paired-run",
            "paired-run",
            "--certificate-run",
            "certificate-run",
            "--certificate-manifest-sha256",
            H2,
            "--confirmed-certificate",
            "certificate.json",
            "--confirmed-certificate-sha256",
            H3,
            "--replay-evidence-sha256",
            H4,
        ],
    )

    assert result.exit_code == 0
    assert "57 / 5 / 285" in result.stdout
    assert "11/11" in result.stdout
    assert "CAUTION" in result.stdout
    assert "不展示方法结果" in result.stdout
    assert "Structured" not in result.stdout
