#!/usr/bin/env python3
"""One command for the fixed MBPP+120 generation -> official EvalPlus workflow."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tracejudge_hy3.baseline import run_baseline_experiment  # noqa: E402
from tracejudge_hy3.benchmark.mbpp_readiness import validate_mbpp_readiness  # noqa: E402
from tracejudge_hy3.config import get_settings  # noqa: E402
from tracejudge_hy3.dataset.loader import load_problems  # noqa: E402
from tracejudge_hy3.evalplus_mbpp.docker_runner import (  # noqa: E402
    DEFAULT_EVALPLUS_IMAGE,
    DockerLimits,
    MbppPlusDockerRunner,
)
from tracejudge_hy3.evalplus_mbpp.exporter import (  # noqa: E402
    _selection_identity,
    export_mbpp_candidates,
)
from tracejudge_hy3.evalplus_mbpp.runner import run_mbpp_experiment  # noqa: E402
from tracejudge_hy3.evalplus_mbpp.schemas import MbppPlusTaskMetadata  # noqa: E402
from tracejudge_hy3.lcb.experiment import (  # noqa: E402
    execution_identity,
    fraction,
    now,
    require_idle_benchmark_containers,
    run_lock,
    source_identity,
    write_json,
)
from tracejudge_hy3.providers.hy3_openai import Hy3OpenAIProvider  # noqa: E402


def combined_report(run_dir, n, run_id):
    generation = run_dir / "generation" / run_id / "summary.json"
    execution = run_dir / "execution" / run_id / "summary.json"
    g = json.loads(generation.read_text()) if generation.exists() else {}
    e = json.loads(execution.read_text()) if execution.exists() else {}
    actual = e.get("actual_execution_count", 0)
    base, plus = e.get("base_pass_count", 0), e.get("base_plus_pass_count", 0)
    return {
        "schema": "mbpp120-combined-report-v1",
        "updated_at": now(),
        "planned_n": n,
        "generation_coverage": fraction(g.get("success_count", 0), n),
        "provider_failure_n": g.get("provider_error_count", 0),
        "parse_failure_n": g.get("parse_error_count", 0),
        "actual_execution_n": actual,
        "base_pass_full_denominator": fraction(base, n),
        "base_plus_pass_full_denominator": fraction(plus, n),
        "base_pass_conditional_on_execution": fraction(base, actual),
        "base_plus_pass_conditional_on_execution": fraction(plus, actual),
        "execution_summary": e,
        "complete": actual == n,
        "metric_note": "Fixed 120-task sample, one successful generation per task; not full MBPP+ ranking.",
        "input_sha256": {
            "generation_summary": hashlib.sha256(generation.read_bytes()).hexdigest()
            if generation.exists()
            else None,
            "execution_summary": hashlib.sha256(execution.read_bytes()).hexdigest()
            if execution.exists()
            else None,
        },
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project-root", type=Path, default=Path.cwd())
    p.add_argument("--preflight", action="store_true")
    p.add_argument("--report-only", action="store_true")
    p.add_argument("--run-id")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--phase", choices=("all", "generate", "execute"), default="all")
    p.add_argument("--confirm-real-provider", action="store_true")
    p.add_argument("--parallel", type=int, default=1, choices=range(1, 5))
    p.add_argument("--batch-timeout", type=float, default=14400)
    args = p.parse_args()
    if (args.resume or args.report_only) and not args.run_id:
        p.error("--resume/--report-only requires --run-id")
    project = args.project_root.resolve()
    bundle = project / "artifacts/datasets/mbppplus/sample120"
    manifest = bundle / "dataset_manifest.json"
    _, selected, _ = _selection_identity(manifest)
    if len(selected) != 120:
        raise ValueError("formal entry point requires the frozen 120-task selection")
    run_id = (
        args.run_id
        or "mbpp120-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", run_id):
        raise ValueError("invalid run ID")
    run_dir = ROOT / "artifacts/experiments/mbppplus" / run_id
    if args.report_only:
        print(json.dumps(combined_report(run_dir, 120, run_id), ensure_ascii=False, indent=2))
        return 0
    readiness = json.loads((project / "artifacts/benchmark-readiness/mbpp.json").read_text())
    validate_mbpp_readiness(
        readiness, image=DEFAULT_EVALPLUS_IMAGE, execution_source=execution_identity(ROOT, "mbpp")
    )
    require_idle_benchmark_containers()
    executor = MbppPlusDockerRunner(limits=DockerLimits(per_task_timeout_seconds=180))
    # Verify every selected prompt/entry point against the container dataset,
    # before spending any model calls.
    problems = load_problems(bundle / "problems.jsonl")
    metadata = [
        MbppPlusTaskMetadata(
            problem_id=t.problem_id,
            prompt_sha256=hashlib.sha256(t.requirement.encode()).hexdigest(),
            entry_point=t.function_name,
        )
        for t in problems
    ]
    work = ROOT / "artifacts/benchmark-preflight/mbpp"
    work.mkdir(parents=True, exist_ok=True)
    preflight = executor.preflight(task_metadata=metadata, workspace=work)
    # Docker Desktop on WSL can leave the Python process attached to a deleted
    # staging-directory inode after the preflight bind mounts are cleaned up.
    # Re-enter the explicit project root before generation code asks for cwd.
    os.chdir(project)
    if not preflight.ready:
        print(f"[blocked] MBPP preflight: {preflight.infrastructure_error_type}")
        return 1
    if args.preflight:
        # Check configuration presence only, without constructing an API client.
        # Authentication, quota and remote availability remain untested.
        configured = get_settings().hy3_configured()
        environment = source_identity(ROOT)
        write_json(
            work / "receipt.json",
            {
                "schema": "mbpp120-preflight-v1",
                "ready": configured,
                "completed_at": now(),
                "runtime_root": str(ROOT),
                "planned_n": len(problems),
                "dataset_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "problems_sha256": hashlib.sha256(
                    (bundle / "problems.jsonl").read_bytes()
                ).hexdigest(),
                "smoke_receipt_sha256": hashlib.sha256(
                    (project / "artifacts/benchmark-readiness/mbpp.json").read_bytes()
                ).hexdigest(),
                "execution_source_sha256": execution_identity(ROOT, "mbpp"),
                "official_runtime": preflight.runtime,
                "execution_limits": asdict(executor.limits),
                "formal_parallel": 1,
                "formal_batch_timeout_seconds": args.batch_timeout,
                "python": environment["python"],
                "dependencies": environment["dependencies"],
                "provider_configuration_present": configured,
                "remote_auth_quota_availability_checked": False,
                "uses_model_api": False,
            },
        )
        if not configured:
            print("[blocked] HY3 configuration missing; no API calls")
            return 1
        print(
            "[ready] 120 selected tasks, official EvalPlus image and public identities verified; no API calls"
        )
        return 0
    if args.phase != "execute" and not args.confirm_real_provider:
        p.error("generation requires --confirm-real-provider")
    print(f"[run] {run_id}\n[artifacts] {run_dir}", flush=True)
    with run_lock(run_dir):
        phase1 = run_dir / "generation" / run_id
        phase2 = run_dir / "execution" / run_id
        if not args.resume and (phase1.exists() or phase2.exists()):
            raise ValueError("existing run requires --resume")
        if args.resume and not phase1.exists():
            raise ValueError("resume generation run does not exist")
        try:
            if args.phase in {"all", "generate"}:
                result = asyncio.run(
                    run_baseline_experiment(
                        dataset_path=bundle / "problems.jsonl",
                        dataset_manifest_path=manifest,
                        provider=Hy3OpenAIProvider(),
                        output_dir=run_dir / "generation",
                        run_id=run_id,
                        resume=args.resume,
                    )
                )
                if result.summary["success_count"] != 120:
                    print(
                        "[incomplete] successful candidates retained; use --resume to retry failed tasks"
                    )
                    return 2
            if args.phase == "generate":
                return 0
            export = export_mbpp_candidates(
                phase1_run_dir=phase1,
                dataset_manifest_path=manifest,
                output_path=run_dir / "candidates.jsonl",
            )
            result = run_mbpp_experiment(
                dataset_manifest_path=manifest,
                candidates_path=export.output_path,
                output_dir=run_dir / "execution",
                executor=executor,
                run_id=run_id,
                resume=args.resume and phase2.exists(),
                max_workers=args.parallel,
                per_task_timeout_seconds=180,
                batch_timeout_seconds=args.batch_timeout,
            )
            return 0 if result.summary["actual_execution_count"] == 120 else 2
        finally:
            write_json(run_dir / "report.json", combined_report(run_dir, 120, run_id))
            print(f"[report] {run_dir / 'report.json'}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("[interrupted] checkpoints retained; resume with the printed run ID")
        raise SystemExit(130) from None
    except Exception as exc:
        print(
            f"[blocked] {type(exc).__name__}; verify smoke receipt, data and resume identity",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
