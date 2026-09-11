"""Supplementary ablation scoring under the development-stage labels (offline, no model calls).

Scores already-recorded ablation predictions twice, side by side:

- original stage: the frozen v1 pilot consensus labels (the registered scoring
  basis; results unchanged);
- supplementary stage: the post-feedback development extension labels
  (``post_feedback_development_extension``), restricted to the same 12 pilot
  items.  Only the two re-adjudicated v1-unknown items (newman_prime,
  opposite_Signs) change gold; sample_nam's extension row restates its v1
  gold unchanged (annotator marker only).

This NEVER rewrites the original scoring; the two stages are reported in
parallel in one new artifact.  No model calls are made and no candidate code
is executed; scoring reuses ``score_ablation``'s deterministic re-merge
verification for both stages.

Example (WSL, from the repository root):

    PYTHONPATH=src python scripts/score_ablation_dev_labels.py \
      --runs artifacts/experiments/process-pilot/mbpp12-ablation-20260909-v1 \
      --expect-plan-sha256 1448c841986d6a4bdc0e0b5ba887fededf7c05b6fc7bc2272ddae239c9609774 \
      --output artifacts/experiments/process-pilot/mbpp12-ablation-devlabel-scoring-20260909-v1
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

SCHEMA = "tracejudge-process-ablation-devlabel-scoring-v1"


def headline(scores: dict) -> dict:
    """Compact side-by-side summary SELECTED from a score_ablation result.

    Only re-labels existing fields; never recomputes scoring rules here.
    """

    out = {}
    for view, methods in scores["views"].items():
        view_out = {}
        for condition, metrics in methods.items():
            dimension = metrics["dimensions"]["public_process_correct"]
            view_out[condition] = {
                "accuracy_all_known": dimension["accuracy_all_known"],
                "decision_coverage_known": dimension["decision_coverage_known"],
                "localization_first_layer": metrics["localization"]["first_layer"],
                "localization_first_step": metrics["localization"]["first_step"],
            }
        out[view] = view_out
    return out


def score_runs_dual_label(
    root: Path,
    pilot_config: Path,
    dev_config: Path,
    runs: list[str],
    *,
    expect_plan_sha256: str | None = None,
) -> dict:
    from tracejudge_hy3.process_eval_v2.ablation import AblationPrediction
    from tracejudge_hy3.process_eval_v2.development import prepare_development
    from tracejudge_hy3.process_eval_v2.materials import digest
    from tracejudge_hy3.process_eval_v2.preflight import (
        contained,
        prepare_pilot,
        read_json,
        read_rows,
    )
    from tracejudge_hy3.process_eval_v2.scoring import score_ablation

    root = root.resolve()
    pilot = prepare_pilot(root, pilot_config)
    development = prepare_development(root, dev_config)
    missing = sorted(set(pilot.inputs) - set(development.labels))
    if missing:
        raise ValueError(f"development labels do not cover pilot items: {missing}")
    stage_labels = {item_id: development.labels[item_id] for item_id in pilot.inputs}

    # Label diffs compare everything except annotator bookkeeping and free text.
    label_diffs = {}
    for item_id in sorted(pilot.inputs):
        before = pilot.labels[item_id].model_dump(exclude={"evidence", "rationale", "annotator"})
        after = stage_labels[item_id].model_dump(exclude={"evidence", "rationale", "annotator"})
        if before != after:
            label_diffs[item_id] = {"pilot_v1": before, "development_stage": after}

    development_pilot = replace(
        pilot,
        labels=stage_labels,
        label_stage=development.label_stage,
        annotation_provenance=development.annotation_provenance,
        provenance={**pilot.provenance, **development.provenance},
    )

    run_entries = []
    for run in runs:
        run_dir = contained(root, run)
        predictions_raw = (run_dir / "predictions.jsonl").read_bytes()
        run_report = read_json((run_dir / "run-report.json").read_bytes())
        plan_sha = run_report.get("run_identity", {}).get("plan_sha256")
        if expect_plan_sha256 is not None and plan_sha != expect_plan_sha256:
            raise ValueError(
                f"run {run} binds plan {plan_sha}, not the pre-registered "
                f"{expect_plan_sha256}; refusing to score"
            )
        predictions = [AblationPrediction.model_validate(row) for row in read_rows(predictions_raw)]
        original = score_ablation(pilot, predictions, run_report=run_report)
        supplementary = score_ablation(development_pilot, predictions, run_report=run_report)
        run_entries.append(
            {
                "run": run,
                "run_status": run_report.get("status"),
                "plan_sha256": plan_sha,
                "predictions_sha256": digest(predictions_raw),
                "original_stage": original,
                "development_stage": supplementary,
                "headline": {
                    "original_stage": headline(original),
                    "development_stage": headline(supplementary),
                },
            }
        )

    return {
        "schema": SCHEMA,
        "label_stages": {
            "original": pilot.label_stage,
            "supplementary": development.label_stage,
        },
        "annotation_provenance": {
            "original": pilot.annotation_provenance,
            "supplementary": development.annotation_provenance,
        },
        "item_count": len(pilot.inputs),
        "label_diffs": label_diffs,
        "runs": run_entries,
        "notes": [
            "supplementary stage restricts development labels to the same 12 pilot items; "
            "the 24 dev-only items never enter this scoring",
            "the original-stage numbers are recomputed from the same frozen predictions and "
            "must equal the registered ablation scoring; nothing is rewritten",
            "unknown gold in v1 became known in the development stage for the three "
            "re-adjudicated items; denominators change accordingly",
            "offline scoring only: no model calls, no candidate-code execution",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 11):  # noqa: UP036 -- direct script launches bypass package requirements
        print(
            f"[blocked] Python 3.11+ is required; current Python is {sys.version.split()[0]} "
            f"({sys.executable}). Use the existing WSL Python environment; "
            "see docs/experiments/process-pilot-v1.md for the PowerShell command.",
            file=sys.stderr,
        )
        return 2

    from tracejudge_hy3.process_eval_v2.development import DEFAULT_DEVELOPMENT_CONFIG
    from tracejudge_hy3.process_eval_v2.materials import digest
    from tracejudge_hy3.process_eval_v2.preflight import DEFAULT_CONFIG, contained

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--dev-config", type=Path, default=DEFAULT_DEVELOPMENT_CONFIG)
    parser.add_argument(
        "--runs",
        nargs="+",
        required=True,
        metavar="RUN_DIR",
        help="Repository-relative ablation run directories whose predictions are scored",
    )
    parser.add_argument(
        "--expect-plan-sha256",
        default=None,
        help="Pre-registered ablation plan hash; any run binding a different plan "
        "refuses the whole report",
    )
    parser.add_argument(
        "--output",
        help="Repository-relative NEW directory below artifacts/experiments/process-pilot "
        "for devlabel-scoring.json + manifest.json (must not exist)",
    )
    args = parser.parse_args(argv)
    root = args.project_root.resolve()
    try:
        report = score_runs_dual_label(
            root,
            args.config,
            args.dev_config,
            list(args.runs),
            expect_plan_sha256=args.expect_plan_sha256,
        )
        if args.output:
            target = contained(root, args.output)
            allowed = (root / "artifacts/experiments/process-pilot").resolve()
            if target == allowed or not target.is_relative_to(allowed):
                raise ValueError(
                    "output must be a new subdirectory of artifacts/experiments/process-pilot"
                )
            payload = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            code_root = Path(__file__).resolve().parents[1]
            manifest = {
                "schema": "tracejudge-process-ablation-devlabel-scoring-manifest-v1",
                "files_sha256": {"devlabel-scoring.json": digest(payload)},
                "implementation_sha256": {
                    Path(__file__).resolve()
                    .relative_to(code_root)
                    .as_posix(): digest(Path(__file__).resolve().read_bytes())
                },
            }
            # Never overwrite an existing report: a new report is a new directory.
            target.mkdir(parents=True, exist_ok=False)
            (target / "devlabel-scoring.json").write_bytes(payload)
            (target / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        print(json.dumps(report, ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"[blocked] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
