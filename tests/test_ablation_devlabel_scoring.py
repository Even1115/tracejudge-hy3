"""Tests for the dual-label supplementary ablation scoring (offline).

Real-artifact integration: rep1's frozen predictions are scored under both the
v1 pilot consensus labels and the development-stage labels; the original-stage
numbers must equal a direct ``score_ablation`` recomputation, and only the
two re-adjudicated v1-unknown items may appear in label diffs (sample_nam's
extension row restates its v1 gold and must NOT diff).  A synthetic test pins
the refusal behaviour for a wrong plan hash.  No model calls anywhere.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from score_ablation_dev_labels import score_runs_dual_label  # noqa: E402

REP1 = "artifacts/experiments/process-pilot/mbpp12-ablation-20260909-v1"
PLAN_SHA256 = "1448c841986d6a4bdc0e0b5ba887fededf7c05b6fc7bc2272ddae239c9609774"
READJUDICATED = {
    "item-ab1645e8d37a7d4cb6b0",  # newman_prime: unknown -> correct
    "item-d97b96cd8a87051bb420",  # opposite_Signs: unknown -> R03 error
}


def test_real_rep1_dual_label_scoring() -> None:
    report = score_runs_dual_label(
        ROOT,
        Path("data/manifests/process_pilot_v1.json"),
        Path("data/manifests/process_development_v1.json"),
        [REP1],
        expect_plan_sha256=PLAN_SHA256,
    )
    assert report["schema"] == "tracejudge-process-ablation-devlabel-scoring-v1"
    assert report["item_count"] == 12
    assert report["label_stages"] == {
        "original": "post_feedback_development_consensus",
        "supplementary": "post_feedback_development_extension",
    }
    assert set(report["label_diffs"]) == READJUDICATED
    for diff in report["label_diffs"].values():
        assert diff["pilot_v1"]["process_correct"] is None
        assert diff["development_stage"]["process_correct"] is not None

    (run,) = report["runs"]
    assert run["run_status"] == "completed"
    assert run["plan_sha256"] == PLAN_SHA256

    original = run["original_stage"]
    supplementary = run["development_stage"]
    assert original["label_stage"] == "post_feedback_development_consensus"
    assert supplementary["label_stage"] == "post_feedback_development_extension"

    # The original stage keeps the v1 denominator (10 known, 2 unknown); the
    # supplementary stage knows all 12.
    for view in ("judge_raw", "rule_merged"):
        for condition in original["conditions"]:
            before = original["views"][view][condition]["dimensions"]["public_process_correct"]
            after = supplementary["views"][view][condition]["dimensions"][
                "public_process_correct"
            ]
            assert before["gold_known"] == 10
            assert after["gold_known"] == 12


def test_original_stage_matches_direct_score_ablation() -> None:
    """The original-stage block must equal a direct score_ablation call."""

    from tracejudge_hy3.process_eval_v2.ablation import AblationPrediction
    from tracejudge_hy3.process_eval_v2.preflight import prepare_pilot, read_json, read_rows
    from tracejudge_hy3.process_eval_v2.scoring import score_ablation

    report = score_runs_dual_label(
        ROOT,
        Path("data/manifests/process_pilot_v1.json"),
        Path("data/manifests/process_development_v1.json"),
        [REP1],
        expect_plan_sha256=PLAN_SHA256,
    )
    run_dir = ROOT / REP1
    predictions = [
        AblationPrediction.model_validate(row)
        for row in read_rows((run_dir / "predictions.jsonl").read_bytes())
    ]
    run_report = read_json((run_dir / "run-report.json").read_bytes())
    pilot = prepare_pilot(ROOT, Path("data/manifests/process_pilot_v1.json"))
    direct = score_ablation(pilot, predictions, run_report=run_report)
    assert report["runs"][0]["original_stage"] == direct


def test_wrong_plan_hash_refused() -> None:
    with pytest.raises(ValueError, match="refusing to score"):
        score_runs_dual_label(
            ROOT,
            Path("data/manifests/process_pilot_v1.json"),
            Path("data/manifests/process_development_v1.json"),
            [REP1],
            expect_plan_sha256="0" * 64,
        )


def test_report_is_json_serializable() -> None:
    report = score_runs_dual_label(
        ROOT,
        Path("data/manifests/process_pilot_v1.json"),
        Path("data/manifests/process_development_v1.json"),
        [REP1],
        expect_plan_sha256=PLAN_SHA256,
    )
    json.dumps(report, ensure_ascii=False)
