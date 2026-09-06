from __future__ import annotations

import json
from pathlib import Path

from tracejudge_hy3.phase4.contest_summary import (
    build_contest_summary,
    build_difficulty_analysis,
    render_difficulty_markdown,
    render_overview_markdown,
)

REPO_ROOT = Path(__file__).parents[1]
OVERVIEW = REPO_ROOT / "docs/releases/phase4/phase4_contest_results_overview_v1.md"
DIFFICULTY = REPO_ROOT / "docs/releases/phase4/phase4_difficulty_proxy_analysis_v1.md"
README = REPO_ROOT / "README.md"
RELEASE_INDEX = REPO_ROOT / "docs/releases/phase4/README.md"


def test_difficulty_proxy_is_pre_outcome_balanced_and_aggregate_only():
    analysis = build_difficulty_analysis(REPO_ROOT)

    assert analysis["difficulty_proxy"]["uses_model_outputs"] is False
    assert analysis["difficulty_proxy"]["uses_human_labels"] is False
    assert analysis["difficulty_proxy"]["uses_execution_outcomes_for_assignment"] is False
    assert [row["source_task_count"] for row in analysis["strata"]] == [15, 15, 15]
    assert [row["included_natural_trace_count"] for row in analysis["strata"]] == [14, 14, 14]
    assert [row["base_and_plus_pass_count"] for row in analysis["strata"]] == [14, 13, 13]
    assert analysis["observed_degradation"]["first_observed_drop_tier"] == "medium-proxy"
    assert analysis["observed_degradation"]["lowest_observed_tiers"] == [
        "medium-proxy",
        "hard-proxy",
    ]

    public_payload = json.dumps(analysis, ensure_ascii=False)
    for forbidden in ('"problem_id"', '"trace_id"', '"rater_id"', '"candidate_code"'):
        assert forbidden not in public_payload


def test_contest_overview_surfaces_confusion_fpr_and_human_coverage():
    summary = build_contest_summary(REPO_ROOT)

    assert summary["headline"]["trace_count"] == 57
    assert summary["headline"]["pair_count"] == 285
    assert summary["headline"]["best_detection_numerator"] == 56
    assert summary["headline"]["best_detection_denominator"] == 57
    assert summary["headline"]["best_localization_numerator"] == 10
    assert summary["headline"]["best_localization_denominator"] == 11
    assert summary["headline"]["full_false_positive_numerator"] == 1
    assert summary["headline"]["full_false_positive_denominator"] == 43

    full = next(row for row in summary["methods"] if row["method_id"] == "full_tracejudge")
    assert (full["true_positive"], full["false_positive"]) == (13, 1)
    assert (full["true_negative"], full["false_negative"]) == (42, 1)
    assert summary["human_review"]["primary_labeled_trace_count"] == 57
    assert summary["human_review"]["second_rater_completed_count"] == 20
    assert summary["human_review"]["second_rater_planned_subset_count"] == 20
    assert summary["human_review"]["agreement_status"] == "computed"
    assert summary["human_review"]["has_error_raw_agreement_numerator"] == 20
    assert summary["human_review"]["has_error_raw_agreement_denominator"] == 20
    assert summary["human_review"]["has_error_cohen_kappa"] == 1.0


def test_published_contest_documents_are_exact_deterministic_renders():
    difficulty = build_difficulty_analysis(REPO_ROOT)
    summary = build_contest_summary(REPO_ROOT)

    assert DIFFICULTY.read_text(encoding="utf-8") == render_difficulty_markdown(difficulty)
    assert OVERVIEW.read_text(encoding="utf-8") == render_overview_markdown(summary)


def test_readme_first_screen_links_contribution_results_demo_and_case():
    readme = README.read_text(encoding="utf-8")
    first_screen = readme[: readme.index("## 1. 项目要解决的问题")]

    assert "## 60 秒看懂项目" in first_screen
    assert "### 一句话研究问题" in first_screen
    assert "### 四项贡献" in first_screen
    assert "### 五个核心数字" in first_screen
    assert "**98.2%**" in first_screen
    assert "**2.33%**" in first_screen
    assert "### 代表案例：答案正确，但过程错误" in first_screen
    assert "phase4_contest_results_overview_v1.md" in first_screen
    assert "phase4_p1_post_adjudication_sensitivity_v1.md" in first_screen
    assert "phase4_difficulty_proxy_analysis_v1.md" in first_screen
    assert "phase4_fixture_demo_v1.md" in first_screen
    assert "tracejudge_hy3_contest_demo.mp4" in first_screen
    assert "04_reasoning_swap_detection.svg" in first_screen
    assert "./scripts/run_demo.sh" in first_screen
    assert "版本化公开 Fixture 回归卡片" in first_screen


def test_release_index_exposes_overview_and_sensitivity_analyses():
    release_index = RELEASE_INDEX.read_text(encoding="utf-8")

    assert "phase4_contest_results_overview_v1.md" in release_index
    assert "phase4_difficulty_proxy_analysis_v1.md" in release_index
    assert "phase4_p1_post_adjudication_sensitivity_v1.md" in release_index
