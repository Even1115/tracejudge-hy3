"""Prepare the revised pilot, run budgeted live judgments, and score predictions.

Phases:
- preflight (default): offline source verification and judge-view export;
- run: the original three-method live pilot (requires --execute);
- ablate: the 2x2 evidence ablation -- offline plan registration by default,
  real budgeted judgments with --execute, continuation with --resume;
- score / ablation-score: offline scoring of pilot / ablation predictions.
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

    from tracejudge_hy3.process_eval_v2.contracts import Prediction
    from tracejudge_hy3.process_eval_v2.materials import digest
    from tracejudge_hy3.process_eval_v2.preflight import (
        DEFAULT_CONFIG,
        contained,
        preflight_report,
        prepare_pilot,
        read_rows,
    )
    from tracejudge_hy3.process_eval_v2.scoring import score_predictions

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        choices=("preflight", "score", "run", "ablate", "ablation-score"),
        default="preflight",
    )
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--predictions", type=Path)
    parser.add_argument(
        "--output",
        help="Repository-relative directory below artifacts/experiments/process-pilot",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="With --phase run/ablate: performs real paid model calls under the budget caps",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Continue an existing run/ablate --execute directory after verifying its identity",
    )
    parser.add_argument("--max-attempts-per-judgment", type=int, default=2)
    parser.add_argument(
        "--max-requests",
        type=int,
        default=None,
        help="Cumulative request cap (default: 72 for run, 96 for ablate)",
    )
    args = parser.parse_args(argv)
    score_phase = args.phase in ("score", "ablation-score")
    if score_phase != (args.predictions is not None):
        parser.error("--predictions is required exactly for the score phases")
    if args.phase == "run" and not args.execute:
        parser.error("--phase run requires --execute; the default phase stays offline")
    if args.phase in ("run", "ablate") and not args.output:
        parser.error(f"--phase {args.phase} requires --output")
    if args.execute and args.phase not in ("run", "ablate"):
        parser.error("--execute is only valid with --phase run or --phase ablate")
    if args.resume and not (args.phase in ("run", "ablate") and args.execute):
        parser.error("--resume is only valid together with --phase run/ablate --execute")
    max_requests = (
        args.max_requests
        if args.max_requests is not None
        else (96 if args.phase == "ablate" else 72)
    )
    root = args.project_root.resolve()
    try:
        if args.phase == "ablate":
            from tracejudge_hy3.exceptions import ProviderError
            from tracejudge_hy3.process_eval_v2.ablation_live import run_ablation

            target = contained(root, args.output)
            allowed = (root / "artifacts/experiments/process-pilot").resolve()
            if target == allowed or not target.is_relative_to(allowed):
                raise ValueError(
                    "output must be a subdirectory of artifacts/experiments/process-pilot"
                )
            try:
                report = run_ablation(
                    root,
                    args.config,
                    target,
                    execute=args.execute,
                    resume=args.resume,
                    max_requests=max_requests,
                    max_attempts_per_judgment=args.max_attempts_per_judgment,
                )
            except ProviderError as exc:
                print(f"[blocked] {type(exc).__name__}: {exc}", file=sys.stderr)
                return 2
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if not args.execute:
                return 0
            return 0 if report["status"] == "completed" else 1

        if args.phase == "run":
            from tracejudge_hy3.exceptions import ProviderError
            from tracejudge_hy3.process_eval_v2.live import run_pilot

            target = contained(root, args.output)
            allowed = (root / "artifacts/experiments/process-pilot").resolve()
            if target == allowed or not target.is_relative_to(allowed):
                raise ValueError(
                    "output must be a subdirectory of artifacts/experiments/process-pilot"
                )
            try:
                report = run_pilot(
                    root,
                    args.config,
                    target,
                    resume=args.resume,
                    max_requests=max_requests,
                    max_attempts_per_judgment=args.max_attempts_per_judgment,
                )
            except ProviderError as exc:
                print(f"[blocked] {type(exc).__name__}: {exc}", file=sys.stderr)
                return 2
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["status"] == "completed" else 1

        pilot = prepare_pilot(root, args.config)
        report = preflight_report(
            pilot, max_attempts=args.max_attempts_per_judgment, max_requests=max_requests
        )
        outputs = {
            "preflight.json": report,
            "prediction.schema.json": Prediction.model_json_schema(),
        }
        if args.phase == "score":
            path = args.predictions if args.predictions.is_absolute() else root / args.predictions
            raw = path.read_bytes()
            scores = score_predictions(
                pilot, [Prediction.model_validate(row) for row in read_rows(raw)]
            )
            scores["predictions_sha256"] = digest(raw)
            scores["verified_sources"] = pilot.provenance
            scores["input_hashes"] = report["input_hashes"]
            outputs["scores.json"] = scores
        if args.phase == "ablation-score":
            from tracejudge_hy3.process_eval_v2.ablation import AblationPrediction
            from tracejudge_hy3.process_eval_v2.preflight import read_json
            from tracejudge_hy3.process_eval_v2.scoring import score_ablation

            path = args.predictions if args.predictions.is_absolute() else root / args.predictions
            raw = path.read_bytes()
            run_report = None
            sibling = path.parent / "run-report.json"
            if sibling.exists():
                run_report = read_json(sibling.read_bytes())
            scores = score_ablation(
                pilot,
                [AblationPrediction.model_validate(row) for row in read_rows(raw)],
                run_report=run_report,
            )
            scores["predictions_sha256"] = digest(raw)
            scores["verified_sources"] = pilot.provenance
            scores["input_hashes"] = report["input_hashes"]
            outputs["prediction.schema.json"] = AblationPrediction.model_json_schema()
            outputs["ablation-scores.json"] = scores
        if args.output:
            target = contained(root, args.output)
            allowed = (root / "artifacts/experiments/process-pilot").resolve()
            if target == allowed or not target.is_relative_to(allowed):
                raise ValueError(
                    "output must be a new subdirectory of artifacts/experiments/process-pilot"
                )
            payloads = {
                name: (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                for name, value in outputs.items()
            }
            payloads["judgment-inputs.jsonl"] = (
                "\n".join(item.model_dump_json() for item in pilot.inputs.values()) + "\n"
            ).encode("utf-8")
            code_root = Path(__file__).resolve().parents[1]
            implementation = [
                Path(__file__).resolve(),
                *sorted((code_root / "src/tracejudge_hy3/process_eval_v2").glob("*.py")),
                code_root / "src/tracejudge_hy3/schemas/location.py",
                code_root / "src/tracejudge_hy3/schemas/evaluation.py",
                code_root / "src/tracejudge_hy3/schemas/solution.py",
            ]
            manifest = {
                "schema": "tracejudge-process-pilot-output-v1",
                "files_sha256": {name: digest(raw) for name, raw in payloads.items()},
                "implementation_sha256": {
                    path.relative_to(code_root).as_posix(): digest(path.read_bytes())
                    for path in implementation
                },
            }
            # No source is overwritten and malformed inputs fail before creating output.
            target.mkdir(parents=True, exist_ok=False)
            for name, raw in payloads.items():
                (target / name).write_bytes(raw)
            (target / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        print(
            json.dumps(
                outputs.get("ablation-scores.json") or outputs.get("scores.json") or report,
                ensure_ascii=False,
                indent=2,
            )
        )
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"[blocked] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
