"""Offline-first baseline/assumption validation; development and probes stay separate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main(argv=None):
    if sys.version_info < (3, 11):  # noqa: UP036 -- direct script can run outside package metadata
        print(
            "[blocked] Python 3.11+ required; use the configured WSL environment.", file=sys.stderr
        )
        return 2
    from tracejudge_hy3.process_eval_v2.ablation_live import _run_ablation
    from tracejudge_hy3.process_eval_v2.assumptions import RUBRICS
    from tracejudge_hy3.process_eval_v2.live import _default_settings, _implementation_fingerprints
    from tracejudge_hy3.process_eval_v2.materials import canonical, digest
    from tracejudge_hy3.process_eval_v2.method_validation import (
        compare_methods,
        offline_checks,
        prepare_validation,
    )
    from tracejudge_hy3.process_eval_v2.preflight import contained

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--dataset", choices=("development", "probes"), required=True)
    parser.add_argument("--phase", choices=("prepare", "compare"), default="prepare")
    parser.add_argument("--rubric", choices=RUBRICS)
    parser.add_argument("--output", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--baseline-run")
    parser.add_argument("--audit-run")
    parser.add_argument(
        "--location-v2", action="store_true", help="compare the new location-format methods"
    )
    args = parser.parse_args(argv)
    if args.phase == "prepare" and (not args.rubric or args.baseline_run or args.audit_run):
        parser.error("prepare requires --rubric and no comparison paths")
    if args.phase == "compare" and (
        args.rubric or args.execute or args.resume or not args.baseline_run or not args.audit_run
    ):
        parser.error("compare requires --baseline-run and --audit-run; execution flags are invalid")
    if args.resume and not args.execute:
        parser.error("--resume requires --execute")
    if args.phase == "prepare" and args.location_v2:
        parser.error(
            "prepare selects the location version through --rubric; --location-v2 is for compare"
        )
    root = args.project_root.resolve()
    try:
        target = contained(root, args.output)
        allowed = (root / "artifacts/experiments/process-method-v2").resolve()
        if target == allowed or not target.is_relative_to(allowed):
            raise ValueError(
                "output must be a subdirectory of artifacts/experiments/process-method-v2"
            )
        location_v2 = args.location_v2 or args.rubric in (
            "baseline_location_v2",
            "assumption_audit_location_v2",
        )
        pilot, config_hash = (
            prepare_validation(root, args.dataset, location_v2=True)
            if location_v2
            else prepare_validation(root, args.dataset)
        )
        if args.phase == "compare":
            result = compare_methods(
                pilot,
                config_hash,
                contained(root, args.baseline_run),
                contained(root, args.audit_run),
                location_v2=location_v2,
            )
            target.mkdir(parents=True, exist_ok=False)
            (target / "comparison.json").write_bytes(canonical(result) + b"\n")
        else:
            checked = offline_checks(pilot)
            # Persist the implementation identity at preflight, before credentials.
            binding = {
                "dataset": args.dataset,
                "config_sha256": config_hash,
                "rubric": args.rubric,
                "implementation_sha256": _implementation_fingerprints(),
                "entrypoint_sha256": digest(Path(__file__).read_bytes()),
            }
            binding_bytes = canonical(binding) + b"\n"
            binding_file = target / "validation-binding.json"
            if binding_file.exists() and binding_file.read_bytes() != binding_bytes:
                raise ValueError(
                    "implementation/configuration changed since validation preflight; use a new directory"
                )
            if target.exists() and not binding_file.exists():
                raise ValueError("existing directory lacks validation binding")
            if not target.exists():
                if args.resume:
                    raise ValueError("resume target does not exist")
                # Register a plan first; _run_ablation owns the run directory.
                _run_ablation(
                    pilot,
                    config_hash,
                    target,
                    execute=False,
                    resume=False,
                    max_requests=len(pilot.inputs) * 2,
                    max_attempts_per_judgment=2,
                    rubric=args.rubric,
                    conditions=("ablation_a",),
                )
                binding_file.write_bytes(binding_bytes)
                (target / "offline-validation.json").write_bytes(canonical(checked) + b"\n")
            result = _run_ablation(
                pilot,
                config_hash,
                target,
                execute=args.execute,
                resume=args.resume,
                max_requests=len(pilot.inputs) * 2,
                max_attempts_per_judgment=2,
                rubric=args.rubric,
                conditions=("ablation_a",),
                settings=_default_settings(root) if args.execute else None,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not args.execute or result["status"] == "completed" else 1
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"[blocked] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
