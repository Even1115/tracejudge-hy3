#!/usr/bin/env python3
"""Offline re-score of the historical full-v1 run with the v2 scoring layer.

Loads ``runs/full-v1/records.jsonl`` (immutable), re-derives the frozen
selection from the pinned data files, and writes a clearly-named derived
analysis ``report_analysis_v2.json`` next to the originals.  Nothing under
``runs/full-v1/`` that already exists is modified; the derived file records
the hashes of its inputs.

Usage: python3 scripts/recompute_full_v1_analysis.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tracejudge_hy3.benchmark.codejudge_eval import (  # noqa: E402
    CodeJudgeEvalAdapter,
    select_samples,
)
from tracejudge_hy3.benchmark.contracts import BenchmarkJudgeRecord  # noqa: E402
from tracejudge_hy3.benchmark.judge_only_metrics import score_judge_only  # noqa: E402

DATA_DIR = REPO_ROOT / "data" / "codejudge_eval"
RUN_DIR = DATA_DIR / "runs" / "full-v1"
OUT_PATH = RUN_DIR / "report_analysis_v2.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    records_path = RUN_DIR / "records.jsonl"
    records = [
        BenchmarkJudgeRecord.model_validate(json.loads(line))
        for line in records_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    adapter = CodeJudgeEvalAdapter()
    samples = adapter.load_samples(DATA_DIR, exclude_ambiguous_interface=True)
    selection = select_samples(samples, descriptor=adapter.descriptor)
    sample_index = {(s.task.identity.task_id, s.candidate.candidate_id): s for s in samples}
    selected = [sample_index[(e.task_id, e.candidate_id)] for e in selection.entries]

    report = score_judge_only(records, selected)
    derived = {
        "derived_from": {
            "run_id": "full-v1",
            "records_jsonl_sha256": _sha256(records_path),
            "selection_file_sha256": _sha256(DATA_DIR / "selection_v2.json"),
            "scoring_layer": "judge_only_metrics v2 (zero_division=0; execution-outcome "
            "agreement; label-space separation)",
        },
        "computed_at": datetime.now(UTC).isoformat(),
        "report": report,
    }
    OUT_PATH.write_text(json.dumps(derived, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    functional = report["functional_correctness"]
    outcome = report["execution_outcome"]["agreement_on_mapped"]
    full_space = report["coarse_verdict"]["macro_f1_full_gold_space"]
    subset = report["coarse_verdict"]["macro_f1_compatible_subset"]
    print(f"[ok] wrote {OUT_PATH}")
    print(f"[check] accuracy: {report['correctness_accuracy_full_denominator']['estimate']:.4f}")
    print(
        f"[check] balanced accuracy: {functional['balanced_accuracy']['estimate']:.4f} "
        f"(sens {functional['sensitivity']['estimate']:.4f}, "
        f"spec {functional['specificity']['estimate']:.4f})"
    )
    print(f"[check] MCC: {functional['mcc']['estimate']:.4f}")
    print(
        f"[check] majority baseline: {report['majority_class_baseline_accuracy']['estimate']:.4f}"
    )
    print(
        f"[check] execution-outcome agreement: {outcome['numerator']}/{outcome['denominator']}"
        f" = {outcome['estimate']:.4f}"
    )
    print(
        f"[check] macro-F1 full gold space (diagnostic): {full_space['macro_f1']:.6f} "
        f"over {full_space['n_classes_averaged']} classes"
    )
    print(
        f"[check] macro-F1 compatible subset: {subset['macro_f1']:.6f} over "
        f"{subset['n_classes_averaged']} classes "
        f"(included {subset['included_n']}, excluded {subset['excluded_n']})"
    )


if __name__ == "__main__":
    main()
