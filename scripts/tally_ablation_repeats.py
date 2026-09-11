"""Tally repeated fixed-configuration ablation runs (offline, no model calls).

Verifies every listed repetition (hash-pinned run files, run identity against
the current pilot configuration and condition fingerprints, scorer re-merge
via ``score_ablation``) and reports per-sample x per-condition detection
frequencies across ALL repetitions.  Pre-registered in
docs/experiments/process-pilot-ablation-v1.md ("重复验证方案"): every
registered round is reported; no round is dropped or cherry-picked.

Example (WSL, from the repository root):

    PYTHONPATH=src python scripts/tally_ablation_repeats.py \
      --runs artifacts/experiments/process-pilot/mbpp12-ablation-20260909-v1 \
             artifacts/experiments/process-pilot/mbpp12-ablation-20260909-v1-rep2 \
             artifacts/experiments/process-pilot/mbpp12-ablation-20260909-v1-rep3 \
             artifacts/experiments/process-pilot/mbpp12-ablation-20260909-v1-rep4 \
             artifacts/experiments/process-pilot/mbpp12-ablation-20260909-v1-rep5 \
      --expect-plan-sha256 1448c841986d6a4bdc0e0b5ba887fededf7c05b6fc7bc2272ddae239c9609774 \
      --output artifacts/experiments/process-pilot/mbpp12-ablation-repetition-tally-20260909-v1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 11):  # noqa: UP036 -- direct script launches bypass package requirements
        print(
            f"[blocked] Python 3.11+ is required; current Python is {sys.version.split()[0]} "
            f"({sys.executable}). Use the existing WSL Python environment; "
            "see docs/experiments/process-pilot-v1.md for the PowerShell command.",
            file=sys.stderr,
        )
        return 2

    from tracejudge_hy3.process_eval_v2.materials import digest
    from tracejudge_hy3.process_eval_v2.preflight import DEFAULT_CONFIG, contained
    from tracejudge_hy3.process_eval_v2.repetition import tally_repetitions

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--runs",
        nargs="+",
        required=True,
        metavar="RUN_DIR",
        help="Repository-relative repetition run directories, in repetition order; "
        "every listed round is verified and reported",
    )
    parser.add_argument(
        "--expect-plan-sha256",
        default=None,
        help="Pre-registered ablation plan hash; any run binding a different plan "
        "refuses the whole tally",
    )
    parser.add_argument(
        "--output",
        help="Repository-relative NEW directory below artifacts/experiments/process-pilot "
        "for tally-report.json + manifest.json (must not exist)",
    )
    args = parser.parse_args(argv)

    root = args.project_root.resolve()
    try:
        report = tally_repetitions(
            root, args.config, list(args.runs), expect_plan_sha256=args.expect_plan_sha256
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
            implementation = [
                Path(__file__).resolve(),
                code_root / "src/tracejudge_hy3/process_eval_v2/repetition.py",
            ]
            manifest = {
                "schema": "tracejudge-process-ablation-repetition-tally-manifest-v1",
                "files_sha256": {"tally-report.json": digest(payload)},
                "implementation_sha256": {
                    path.relative_to(code_root).as_posix(): digest(path.read_bytes())
                    for path in implementation
                },
            }
            # Never overwrite an existing tally: a new tally is a new directory.
            target.mkdir(parents=True, exist_ok=False)
            (target / "tally-report.json").write_bytes(payload)
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
