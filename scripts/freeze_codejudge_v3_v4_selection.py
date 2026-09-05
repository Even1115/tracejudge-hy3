#!/usr/bin/env python3
"""Freeze v3-A/v3-B selections bound to the corrected prompt bundle v4.

The original ``selection_v3a.json`` and ``selection_v3b.json`` remain intact
as superseded prompt-v3 preregistration artifacts.  This script writes new
``selection_v3a_v4.json`` and ``selection_v3b_v4.json`` files and refuses to
overwrite either one.
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
    CodeJudgeV3Selection,
    select_v3_experiment_a,
    select_v3_experiment_b,
    used_raw_task_ids,
)
from tracejudge_hy3.benchmark.judge_only_prompt_v4 import (  # noqa: E402
    judge_only_prompt_v4_bundle_sha256,
)

DATA_DIR = REPO_ROOT / "data" / "codejudge_eval"


def derive() -> tuple[CodeJudgeV3Selection, CodeJudgeV3Selection]:
    adapter = CodeJudgeEvalAdapter()
    samples = adapter.load_samples(DATA_DIR, exclude_ambiguous_interface=True)
    frozen_v2 = CodeJudgeEvalSelection.model_validate_json(
        (DATA_DIR / "selection_v2.json").read_text(encoding="utf-8")
    )
    full_v1_used = used_raw_task_ids(frozen_v2)
    prompt_hash = judge_only_prompt_v4_bundle_sha256()
    selection_a = select_v3_experiment_a(
        samples,
        descriptor=adapter.descriptor,
        exclude_raw_task_ids=full_v1_used,
        prompt_bundle_sha256=prompt_hash,
    )
    exclude_b = full_v1_used | {entry.raw_task_id for entry in selection_a.entries}
    selection_b = select_v3_experiment_b(
        samples,
        descriptor=adapter.descriptor,
        exclude_raw_task_ids=frozenset(exclude_b),
        prompt_bundle_sha256=prompt_hash,
    )
    return selection_a, selection_b


def main() -> None:
    selection_a, selection_b = derive()
    for name, selection in (
        ("selection_v3a_v4.json", selection_a),
        ("selection_v3b_v4.json", selection_b),
    ):
        path = DATA_DIR / name
        if path.exists():
            raise SystemExit(f"refusing to overwrite frozen selection: {path}")
        path.write_text(selection.model_dump_json(indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "selection": name,
                    "entries": len(selection.entries),
                    "entries_sha256": selection.entries_sha256,
                    "prompt_bundle_sha256": selection.prompt_bundle_sha256,
                    "degradation_notes": len(selection.degradation_log),
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
