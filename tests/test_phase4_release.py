from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tracejudge_hy3.cli import app
from tracejudge_hy3.phase3.statistics import wilson_interval
from tracejudge_hy3.phase4 import (
    Phase4GitIdentity,
    Phase4ReleaseError,
    prepare_public_charts,
    verify_public_charts,
    write_public_charts,
)

METHODS = (
    "test_only",
    "direct_llm_judge",
    "four_layer_structured_judge",
    "four_layer_ast",
    "full_tracejudge",
)
GIT_IDENTITY = Phase4GitIdentity(
    commit="a" * 40,
    branch="phase4-release-fixture",
    dirty=False,
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True).encode(
            "utf-8"
        )
        + b"\n"
    )


def _proportion(numerator: int, denominator: int) -> dict[str, int | float]:
    lower, upper = wilson_interval(numerator, denominator)
    assert lower is not None and upper is not None
    return {
        "numerator": numerator,
        "denominator": denominator,
        "estimate": numerator / denominator,
        "wilson_95_lower": lower,
        "wilson_95_upper": upper,
    }


def _confusion(
    *,
    true_positive: int,
    true_negative: int,
    false_positive: int,
    false_negative: int,
) -> dict[str, object]:
    precision = true_positive / (true_positive + false_positive)
    recall = true_positive / (true_positive + false_negative)
    return {
        "valid_prediction_count": (true_positive + true_negative + false_positive + false_negative),
        "true_positive": true_positive,
        "true_negative": true_negative,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": _proportion(true_positive, true_positive + false_positive),
        "recall": _proportion(true_positive, true_positive + false_negative),
        "f1": 2 * precision * recall / (precision + recall),
    }


def _aggregate_report() -> dict[str, object]:
    accuracy = {
        "test_only": (54, 42, 12),
        "direct_llm_judge": (55, 41, 14),
        "four_layer_structured_judge": (56, 42, 14),
        "four_layer_ast": (54, 40, 14),
        "full_tracejudge": (55, 41, 14),
    }
    availability = {
        "test_only": 57,
        "direct_llm_judge": 56,
        "four_layer_structured_judge": 57,
        "four_layer_ast": 56,
        "full_tracejudge": 57,
    }
    confusions = {
        "test_only": _confusion(
            true_positive=12,
            true_negative=42,
            false_positive=1,
            false_negative=2,
        ),
        "direct_llm_judge": _confusion(
            true_positive=13,
            true_negative=42,
            false_positive=0,
            false_negative=1,
        ),
        "four_layer_structured_judge": _confusion(
            true_positive=13,
            true_negative=43,
            false_positive=0,
            false_negative=1,
        ),
        "four_layer_ast": _confusion(
            true_positive=13,
            true_negative=41,
            false_positive=1,
            false_negative=1,
        ),
        "full_tracejudge": _confusion(
            true_positive=13,
            true_negative=42,
            false_positive=1,
            false_negative=1,
        ),
    }
    method_metrics: list[dict[str, object]] = []
    for method_id in METHODS:
        all_correct, natural_correct, counterfactual_correct = accuracy[method_id]
        provider_errors = int(method_id in {"direct_llm_judge", "four_layer_ast"})
        method_metrics.append(
            {
                "method_id": method_id,
                "scopes": {
                    "all": {
                        "judgment_availability": _proportion(
                            availability[method_id],
                            57,
                        ),
                        "final_status_counts": {
                            "valid_judgment": availability[method_id],
                            "provider_error": provider_errors,
                        },
                        "error_detection_accuracy_full_denominator": _proportion(
                            all_correct,
                            57,
                        ),
                        "valid_only_confusion": confusions[method_id],
                    },
                    "natural": {
                        "error_detection_accuracy_full_denominator": _proportion(
                            natural_correct,
                            42,
                        )
                    },
                    "counterfactual": {
                        "error_detection_accuracy_full_denominator": _proportion(
                            counterfactual_correct,
                            15,
                        )
                    },
                },
            }
        )
    return {
        "schema_version": 1,
        "kind": "tracejudge_phase3_paired_statistics",
        "statistics_id": "phase3_stats_fixture_v1",
        "analysis_contract": {
            "positive_class": "has_error_true",
            "invalid_method_outcome_policy": (
                "retain_in_full_denominator_count_as_incorrect_and_report_separately"
            ),
            "counterfactual_interval": "parent_problem_cluster_percentile_bootstrap",
            "exploratory_only": True,
        },
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
                    "denominator": 42,
                    "full_correct": 41,
                    "baseline_correct": 42,
                    "accuracy_difference_full_minus_baseline": -1 / 42,
                    "n01_baseline_incorrect_full_correct": 0,
                    "n10_baseline_correct_full_incorrect": 1,
                    "exact_two_sided_mcnemar_p_value": 1.0,
                    "holm_adjusted_p_value": 1.0,
                },
                {
                    "comparison": "full_tracejudge_vs_direct_llm_judge",
                    "denominator": 42,
                    "full_correct": 41,
                    "baseline_correct": 41,
                    "accuracy_difference_full_minus_baseline": 0.0,
                    "n01_baseline_incorrect_full_correct": 1,
                    "n10_baseline_correct_full_incorrect": 1,
                    "exact_two_sided_mcnemar_p_value": 1.0,
                    "holm_adjusted_p_value": 1.0,
                },
            ],
            "counterfactual": [
                {
                    "comparison": "full_tracejudge_vs_test_only",
                    "denominator": 15,
                    "full_correct": 14,
                    "baseline_correct": 12,
                    "accuracy_difference_full_minus_baseline": 2 / 15,
                    "cluster_bootstrap_95_lower": 0.0,
                    "cluster_bootstrap_95_upper": 0.2,
                    "parent_cluster_count": 3,
                    "bootstrap_iteration_count": 10_000,
                    "bootstrap_seed": 20_260_828,
                    "percentile_rule": "type7_linear_interpolation",
                },
                {
                    "comparison": "full_tracejudge_vs_direct_llm_judge",
                    "denominator": 15,
                    "full_correct": 14,
                    "baseline_correct": 14,
                    "accuracy_difference_full_minus_baseline": 0.0,
                    "cluster_bootstrap_95_lower": 0.0,
                    "cluster_bootstrap_95_upper": 0.0,
                    "parent_cluster_count": 3,
                    "bootstrap_iteration_count": 10_000,
                    "bootstrap_seed": 20_260_828,
                    "percentile_rule": "type7_linear_interpolation",
                },
            ],
        },
    }


def _write_statistics(
    root: Path,
    *,
    mutate: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[Path, Path, str, str]:
    report = _aggregate_report()
    if mutate is not None:
        mutate(report)
    report_payload = _json_bytes(report)
    report_sha256 = _sha256(report_payload)
    manifest = {
        "schema_version": 1,
        "phase": "phase3_paired_statistics",
        "status": "completed",
        "statistics_id": report["statistics_id"],
        "report_sha256": report_sha256,
        "contains_per_trace_rows": False,
        "contains_annotation_rationales": False,
        "contains_provider_raw": False,
        "contains_hidden_evaluation_content": False,
    }
    manifest_payload = _json_bytes(manifest)
    manifest_path = root / "statistics-manifest.json"
    report_path = root / "statistics-report.json"
    manifest_path.write_bytes(manifest_payload)
    report_path.write_bytes(report_payload)
    return manifest_path, report_path, _sha256(manifest_payload), report_sha256


def _prepare_kwargs(tmp_path: Path, **overrides: object) -> dict[str, object]:
    manifest, report, manifest_sha256, report_sha256 = _write_statistics(tmp_path)
    kwargs: dict[str, object] = {
        "statistics_manifest_path": manifest,
        "statistics_report_path": report,
        "expected_statistics_manifest_sha256": manifest_sha256,
        "expected_statistics_report_sha256": report_sha256,
        "chart_bundle_id": "phase4_public_charts_fixture_v1",
        "repo_root": tmp_path,
        "git_identity": GIT_IDENTITY,
    }
    kwargs.update(overrides)
    return kwargs


def test_phase4_chart_cli_commands_are_registered() -> None:
    result = CliRunner().invoke(app, ["phase4", "--help"])

    assert result.exit_code == 0, result.output
    assert "charts-preflight" in result.output
    assert "charts-publish" in result.output
    assert "charts-verify" in result.output


def test_prepare_public_charts_is_deterministic_and_aggregate_only(tmp_path: Path) -> None:
    kwargs = _prepare_kwargs(tmp_path)

    first = prepare_public_charts(**kwargs)
    second = prepare_public_charts(**kwargs)

    assert first.manifest_payload == second.manifest_payload
    assert first.figure_payloads == second.figure_payloads
    assert first.manifest_sha256 == second.manifest_sha256
    assert first.manifest.verification_status == "ANALYZED"
    assert first.manifest.overall_confidence == "CAUTION"
    assert first.manifest.reproducibility == "CANNOT_VERIFY"
    assert first.manifest.cohort.valid_judgment_count == 283
    assert first.manifest.cohort.provider_error_count == 2
    assert len(first.manifest.figures) == 3
    assert all(
        item.accuracy_counterfactual.interval_kind == "descriptive_only"
        and item.accuracy_counterfactual.interval_lower is None
        and item.accuracy_counterfactual.interval_upper is None
        for item in first.manifest.methods
    )
    public_text = first.manifest_payload.decode("utf-8") + "".join(
        payload.decode("utf-8") for payload in first.figure_payloads.values()
    )
    for forbidden in (
        "/Users/",
        "Authorization:",
        "Bearer ",
        "canonical_solution",
        "failure_input",
        "annotation_rationale",
        "<script",
        " href=",
    ):
        assert forbidden not in public_text


def test_prepare_public_charts_rejects_hash_and_analysis_contract_drift(
    tmp_path: Path,
) -> None:
    kwargs = _prepare_kwargs(tmp_path)
    kwargs["expected_statistics_report_sha256"] = "0" * 64
    with pytest.raises(Phase4ReleaseError, match="hash differs"):
        prepare_public_charts(**kwargs)

    contract_root = tmp_path / "contract"
    contract_root.mkdir()

    def mutate(report: dict[str, Any]) -> None:
        analysis = report["analysis_contract"]
        assert isinstance(analysis, dict)
        analysis["exploratory_only"] = False

    manifest, report, manifest_sha256, report_sha256 = _write_statistics(
        contract_root,
        mutate=mutate,
    )
    with pytest.raises(Phase4ReleaseError, match="analysis contract"):
        prepare_public_charts(
            statistics_manifest_path=manifest,
            statistics_report_path=report,
            expected_statistics_manifest_sha256=manifest_sha256,
            expected_statistics_report_sha256=report_sha256,
            chart_bundle_id="phase4_public_charts_fixture_v1",
            repo_root=tmp_path,
            git_identity=GIT_IDENTITY,
        )


def test_prepare_public_charts_rejects_aggregate_accounting_drift(tmp_path: Path) -> None:
    def mutate(report: dict[str, Any]) -> None:
        comparisons = report["primary_comparisons"]
        assert isinstance(comparisons, dict)
        natural = comparisons["natural"]
        assert isinstance(natural, list)
        natural[0]["baseline_correct"] = 41

    manifest, report, manifest_sha256, report_sha256 = _write_statistics(
        tmp_path,
        mutate=mutate,
    )
    with pytest.raises(Phase4ReleaseError):
        prepare_public_charts(
            statistics_manifest_path=manifest,
            statistics_report_path=report,
            expected_statistics_manifest_sha256=manifest_sha256,
            expected_statistics_report_sha256=report_sha256,
            chart_bundle_id="phase4_public_charts_fixture_v1",
            repo_root=tmp_path,
            git_identity=GIT_IDENTITY,
        )


def test_public_chart_canary_fails_closed(tmp_path: Path) -> None:
    kwargs = _prepare_kwargs(
        tmp_path,
        chart_bundle_id="phase4_SECRET_CANARY_charts_v1",
        privacy_canaries=("SECRET_CANARY",),
    )

    with pytest.raises(Phase4ReleaseError) as exc_info:
        prepare_public_charts(**kwargs)

    assert exc_info.value.safe_stage == "P4E_PRIVACY"
    assert "SECRET_CANARY" not in str(exc_info.value)


def test_chart_publication_is_immutable_and_tamper_evident(tmp_path: Path) -> None:
    kwargs = _prepare_kwargs(tmp_path)
    output_root = tmp_path / "public-charts"

    result = write_public_charts(output_dir=output_root, **kwargs)
    verified = verify_public_charts(
        manifest_path=result.manifest_path,
        expected_manifest_sha256=result.manifest_sha256,
    )

    assert verified.verified is True
    assert verified.figure_count == 3
    assert stat.S_IMODE(result.run_dir.stat().st_mode) == 0o755
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o644 for path in result.figure_paths)
    with pytest.raises(Phase4ReleaseError, match="already exists"):
        write_public_charts(output_dir=output_root, **kwargs)

    result.figure_paths[0].write_text("<svg/>\n", encoding="utf-8")
    with pytest.raises(Phase4ReleaseError, match="deterministic rendering"):
        verify_public_charts(manifest_path=result.manifest_path)


def test_chart_input_symlink_is_rejected(tmp_path: Path) -> None:
    kwargs = _prepare_kwargs(tmp_path)
    source = kwargs["statistics_report_path"]
    assert isinstance(source, Path)
    linked = tmp_path / "linked-report.json"
    linked.symlink_to(source)
    kwargs["statistics_report_path"] = linked

    with pytest.raises(Phase4ReleaseError, match="symbolic link"):
        prepare_public_charts(**kwargs)
