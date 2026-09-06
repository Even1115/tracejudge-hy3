#!/usr/bin/env python3
"""Prepare, preflight, generate, execute and resume the frozen LCB60 cohort."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tracejudge_hy3.benchmark.contracts import canonical_sha256  # noqa: E402
from tracejudge_hy3.benchmark.livecodebench import (  # noqa: E402
    LCB_PIN,
    LiveCodeBenchBenchmarkAdapter,
    build_selection_lock,
    load_source_records,
    verify_selection_lock,
)
from tracejudge_hy3.lcb.docker_runner import DockerLimits, LCBDockerRunner  # noqa: E402
from tracejudge_hy3.lcb.experiment import (  # noqa: E402
    execution_identity,
    load_events,
    require_idle_benchmark_containers,
    run_experiment,
    source_identity,
    summarize,
    write_json,
)
from tracejudge_hy3.lcb.generation import StdioHy3Provider, prompt_bundle_hash  # noqa: E402


def load_cohort(project):
    lock_path = project / "artifacts/datasets/livecodebench/selection60.json"
    keep = (
        frozenset(json.loads(lock_path.read_text())["selected_task_ids"])
        if lock_path.exists()
        else frozenset()
    )
    records = load_source_records(
        project / "artifacts/datasets/raw/livecodebench/code_generation_lite",
        retain_private_task_ids=keep,
    )
    lock = build_selection_lock(records)
    if lock_path.exists():
        frozen = json.loads(lock_path.read_text())
        verify_selection_lock(frozen, records)
        if frozen != lock:
            raise ValueError("selection differs from frozen lock")
    adapter = LiveCodeBenchBenchmarkAdapter()
    tasks = adapter.select_tasks(adapter.project_records(records), lock["selected_task_ids"])
    selected = set(lock["selected_task_ids"])
    return (
        adapter,
        {r.question_id: r for r in records if r.question_id in selected},
        tasks,
        lock,
        lock_path,
    )


async def run(args):
    project = args.project_root.resolve()
    if not args.prepare and not args.report_only:
        require_idle_benchmark_containers()
    adapter, records, tasks, lock, lock_path = load_cohort(project)
    if args.prepare:
        write_json(lock_path, lock, immutable=True)
        write_json(
            lock_path.with_name("tasks60.json"),
            [t.model_dump(mode="json") for t in tasks],
            immutable=True,
        )
        print(f"[prepared] {len(tasks)} tasks, selection={lock['selected_task_ids_sha256']}")
        return 0
    if not lock_path.is_file():
        raise ValueError("run --prepare before starting an experiment")
    run_id = (
        args.run_id
        or "lcb60-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", run_id):
        raise ValueError("invalid run ID")
    run_dir = ROOT / "artifacts/experiments/livecodebench" / run_id
    if args.report_only:
        events, generations, executions = load_events(run_dir, tasks, run_id)
        report = summarize(tasks, generations, executions, events)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    config_path = project / "artifacts/datasets/livecodebench/image.json"
    image = args.image or json.loads(config_path.read_text())["image"]
    executor = LCBDockerRunner(
        image=image, limits=DockerLimits(per_task_timeout_seconds=args.task_timeout)
    )
    if not executor.is_available()[0]:
        raise ValueError("Docker is unavailable")
    executor.verify_image_identity()
    # Bind checker files baked into the image through the smoke receipt;
    # never build from an unverified checkout.
    readiness = project / "artifacts/benchmark-readiness/lcb.json"
    if not readiness.exists():
        raise ValueError("run the real container smoke script first")
    receipt = json.loads(readiness.read_text())
    if not receipt.get("ready") or receipt.get("image") != image:
        raise ValueError("container smoke receipt does not match the selected image")
    if receipt.get("checker_commit") != LCB_PIN.checker_commit:
        raise ValueError("smoke checker commit mismatch")
    if receipt.get("execution_source_sha256") != execution_identity(ROOT, "lcb"):
        raise ValueError("execution source differs from the real smoke-tested source")
    if args.preflight:
        print(f"[ready] {len(tasks)} tasks; image={image}; no API calls")
        return 0
    if args.phase != "execute" and not args.confirm_real_provider:
        raise ValueError("generation requires --confirm-real-provider")
    provider = None
    try:
        if args.phase != "execute":
            provider = StdioHy3Provider()
            config = provider.public_generation_config()
        else:
            if not args.resume:
                raise ValueError("execute requires --resume --run-id of a generated run")
            config = json.loads((run_dir / "manifest.json").read_text())["identity"]["provider"]
        identity = {
            "selection": lock,
            "tasks_sha256": canonical_sha256([t.model_dump(mode="json") for t in tasks]),
            "source": source_identity(ROOT),
            "provider": config,
            "prompt_sha256": prompt_bundle_hash(),
            "image": image,
            "limits": asdict(executor.limits),
            "per_test_timeout_seconds": args.test_timeout,
        }
        print(f"[run] {run_id}\n[artifacts] {run_dir}", flush=True)
        report = await run_experiment(
            tasks=tasks,
            records=records,
            adapter=adapter,
            executor=executor,
            provider=provider,
            run_dir=run_dir,
            identity=identity,
            resume=args.resume,
            phase=args.phase,
        )
        print(f"[report] {run_dir / 'report.json'}")
        if args.phase == "generate":
            return 0 if report["generation_coverage"]["numerator"] == len(tasks) else 2
        return 0 if report["complete"] else 2
    finally:
        if provider is not None:
            await provider.aclose()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--project-root", type=Path, default=Path.cwd(), help="project containing downloaded data"
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--report-only", action="store_true")
    p.add_argument("--image", help="full local image ID or checker repository digest")
    p.add_argument("--run-id")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--confirm-real-provider", action="store_true")
    p.add_argument("--phase", choices=("all", "generate", "execute"), default="all")
    p.add_argument("--task-timeout", type=float, default=600)
    p.add_argument("--test-timeout", type=int, choices=range(1, 121), default=6)
    args = p.parse_args()
    if (args.resume or args.report_only) and not args.run_id:
        p.error("--resume/--report-only requires --run-id")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("[interrupted] completed checkpoints retained; resume with the printed run ID")
        return 130
    except Exception as exc:
        # No provider output, hidden test payload, or exception repr in CLI logs.
        print(
            f"[blocked] {type(exc).__name__}; check selection, smoke receipt, configuration and resume identity",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
