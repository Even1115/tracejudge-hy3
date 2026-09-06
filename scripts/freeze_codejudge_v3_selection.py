#!/usr/bin/env python3
"""Freeze the pre-registered v3 selections (NO model calls, NO downloads).

Loads the pinned local 0-shot files, derives both v3 holdouts deterministically
(experiment A: balanced functional correctness; experiment B: paired
granularity), and writes ``selection_v3a.json`` / ``selection_v3b.json`` with
status ``preregistered-not-run``.  Both exclude every raw task id used by the
frozen full-v1 selection; B additionally excludes A's task ids.

Usage: python3 scripts/freeze_codejudge_v3_selection.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tracejudge_hy3.benchmark.codejudge_eval import (  # noqa: E402
    CodeJudgeEvalAdapter,
    CodeJudgeEvalSelection,
)
from tracejudge_hy3.benchmark.codejudge_v3 import (  # noqa: E402
    select_v3_experiment_a,
    select_v3_experiment_b,
    used_raw_task_ids,
)

DATA_DIR = REPO_ROOT / "data" / "codejudge_eval"


def main() -> None:
    adapter = CodeJudgeEvalAdapter()
    samples = adapter.load_samples(DATA_DIR, exclude_ambiguous_interface=True)

    frozen_v2 = CodeJudgeEvalSelection.model_validate(
        json.loads((DATA_DIR / "selection_v2.json").read_text(encoding="utf-8"))
    )
    full_v1_used = used_raw_task_ids(frozen_v2)
    print(f"[v3] full-v1 raw task ids excluded: {len(full_v1_used)}")

    selection_a = select_v3_experiment_a(
        samples, descriptor=adapter.descriptor, exclude_raw_task_ids=full_v1_used
    )
    exclude_b = full_v1_used | {entry.raw_task_id for entry in selection_a.entries}
    selection_b = select_v3_experiment_b(
        samples, descriptor=adapter.descriptor, exclude_raw_task_ids=frozenset(exclude_b)
    )

    for name, selection in (
        ("selection_v3a.json", selection_a),
        ("selection_v3b.json", selection_b),
    ):
        path = DATA_DIR / name
        if path.exists():
            raise SystemExit(f"refusing to overwrite frozen selection: {path}")
        path.write_text(selection.model_dump_json(indent=2) + "\n", encoding="utf-8")
        print(
            f"[v3] froze {path.name}: {len(selection.entries)} entries, "
            f"entries_sha256={selection.entries_sha256[:16]}…, "
            f"degradation_log={len(selection.degradation_log)} note(s)"
        )

    by_stratum_a: dict[str, int] = {}
    for entry in selection_a.entries:
        by_stratum_a[entry.stratum] = by_stratum_a.get(entry.stratum, 0) + 1
    print(f"[v3a] strata: {dict(sorted(by_stratum_a.items()))}")
    by_stratum_b: dict[str, int] = {}
    for entry in selection_b.entries:
        by_stratum_b[entry.stratum] = by_stratum_b.get(entry.stratum, 0) + 1
    print(f"[v3b] strata: {dict(sorted(by_stratum_b.items()))}")


if __name__ == "__main__":
    main()
