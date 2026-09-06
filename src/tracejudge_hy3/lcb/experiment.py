"""Resumable LCB generation and official execution with atomic, private events."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import subprocess
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from tracejudge_hy3.benchmark.contracts import (
    BenchmarkExecutionResult,
    ExecutionStatus,
    canonical_sha256,
)
from tracejudge_hy3.benchmark.livecodebench import candidate_from_code
from tracejudge_hy3.schemas.solution import SolutionTrace


def now():
    return datetime.now(UTC).isoformat()


def require_idle_benchmark_containers():
    """Read-only startup guard; never stop another experiment's containers.

    This is a point-in-time check, not a lock on legacy HumanEval runners.
    Users must still avoid launching evaluations in another window afterwards.
    """
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError(
            "Docker is unavailable or busy; retry after the running evaluation finishes"
        ) from None
    if any(
        name.startswith(("tracejudge-evalplus-", "tracejudge-mbpp-", "lcb-task-"))
        for name in result.stdout.splitlines()
    ):
        raise RuntimeError(
            "Another benchmark container is running; wait for that evaluation to finish"
        )


def write_json(path: Path, payload, *, immutable=False):
    """Atomic complete file checkpoints; a crash cannot leave a partial event."""
    if path.is_symlink():
        raise ValueError("symlinked output is not allowed")
    data = (
        json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n"
    ).encode()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if immutable and path.exists():
        if path.read_bytes() != data:
            raise ValueError("immutable artifact differs")
        return
    fd, name = tempfile.mkstemp(prefix=".checkpoint-", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        tmp.unlink(missing_ok=True)


def source_identity(root: Path) -> dict:
    files = sorted((root / "src").rglob("*.py"))
    files += sorted((root / "scripts").glob("*.py"))
    files += [p for p in (root / "pyproject.toml", root / "uv.lock") if p.is_file()]
    hashes = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    return {
        "files_sha256": hashes,
        "python": platform.python_version(),
        "dependencies": {
            p: version(p) for p in ("pydantic", "pydantic-settings", "openai", "typer", "rich")
        },
    }


def execution_identity(root: Path, dataset: str) -> dict:
    package = root / "src/tracejudge_hy3"
    files = (
        sorted((package / "evalplus_mbpp").glob("*.py"))
        + [
            package / "evalplus/parser.py",
            package / "benchmark/mbpp_readiness.py",
            root / "scripts/smoke_external_benchmarks.py",
        ]
        if dataset == "mbpp"
        else [
            package / "lcb/docker_runner.py",
            package / "lcb/container_entrypoint.py",
            package / "benchmark/livecodebench.py",
        ]
    )
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


@contextmanager
def run_lock(run_dir: Path):
    if run_dir.is_symlink():
        raise ValueError("symlinked run directory is not allowed")
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = run_dir / ".lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("run is already active in another process") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def candidate_for(task, solution: SolutionTrace, run_id):
    return candidate_from_code(
        task=task,
        candidate_id=run_id,
        code=solution.code,
        solution_trace_sha256=canonical_sha256(solution),
        source_run_id=run_id,
    )


def load_events(run_dir: Path, tasks, run_id):
    tasks_by_id = {t.identity.task_id: t for t in tasks}
    generations, executions, events = {}, {}, []
    previous = None
    for index, path in enumerate(sorted((run_dir / "events").glob("*.json"))):
        if path.is_symlink() or path.name != f"{index:06d}.json":
            raise ValueError("event sequence is incomplete or symlinked")
        envelope = json.loads(path.read_text())
        event = envelope["event"]
        if (
            set(envelope) != {"event", "sha256"}
            or envelope["sha256"] != canonical_sha256(event)
            or event["previous_sha256"] != previous
        ):
            raise ValueError("event integrity violation")
        task_id = event["task_id"]
        if task_id not in tasks_by_id:
            raise ValueError("event coverage violation")
        task = tasks_by_id[task_id]
        if event["kind"] == "generation":
            if task_id in generations and generations[task_id]["status"] == "success":
                raise ValueError("successful generation must not be replaced")
            if event["status"] not in {"success", "provider_error", "parse_error"}:
                raise ValueError("invalid generation status")
            if event["status"] == "success":
                solution = SolutionTrace.model_validate(event["solution"])
                if solution.problem_id != task_id or not solution.code.strip():
                    raise ValueError("solution identity or code is invalid")
            elif event["solution"] is not None:
                raise ValueError("failed generation contains a solution")
            generations[task_id] = event
        elif event["kind"] == "execution":
            if task_id not in generations or generations[task_id]["status"] != "success":
                raise ValueError("execution has no successful generation")
            solution = SolutionTrace.model_validate(generations[task_id]["solution"])
            candidate = candidate_for(task, solution, run_id)
            result = BenchmarkExecutionResult.model_validate(event["result"])
            if result.task != task.identity or result.candidate_sha256 != candidate.code_sha256:
                raise ValueError("execution candidate identity mismatch")
            prior = executions.get(task_id)
            if prior and prior.status not in {
                ExecutionStatus.INFRASTRUCTURE_ERROR,
                ExecutionStatus.NOT_RUN,
            }:
                raise ValueError("completed execution must not be replaced")
            executions[task_id] = result
        else:
            raise ValueError("invalid event kind")
        events.append(envelope)
        previous = envelope["sha256"]
    receipt_path = run_dir / "completion_receipt.json"
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        count = receipt["event_count"]
        if not isinstance(count, int) or not 0 <= count <= len(events):
            raise ValueError("receipt event coverage violation")
        expected = events[count - 1]["sha256"] if count else None
        if receipt["last_event_sha256"] != expected:
            raise ValueError("receipt event integrity violation")
    return events, generations, executions


def append_event(run_dir, events, event):
    event = {**event, "previous_sha256": events[-1]["sha256"] if events else None}
    envelope = {"event": event, "sha256": canonical_sha256(event)}
    write_json(run_dir / "events" / f"{len(events):06d}.json", envelope, immutable=True)
    events.append(envelope)


def fraction(n, d):
    if not d:
        return {"numerator": n, "denominator": d, "value": None, "wilson_95": None}
    z = 1.959963984540054
    p = n / d
    center = (p + z * z / (2 * d)) / (1 + z * z / d)
    half = z * ((p * (1 - p) / d + z * z / (4 * d * d)) ** 0.5) / (1 + z * z / d)
    return {
        "numerator": n,
        "denominator": d,
        "value": p,
        "wilson_95": [max(0.0, center - half), min(1.0, center + half)],
    }


def summarize(tasks, generations, executions, events):
    def group(selected):
        ids = {t.identity.task_id for t in selected}
        states = Counter(generations[i]["status"] if i in generations else "pending" for i in ids)
        outcomes = Counter(executions[i].status.value for i in ids if i in executions)
        evaluated = sum(
            v for k, v in outcomes.items() if k not in {"infrastructure_error", "not_run"}
        )
        passed = outcomes["passed"]
        return {
            "planned_n": len(ids),
            "generation_status_counts": dict(states),
            "generation_coverage": fraction(states["success"], len(ids)),
            "execution_status_counts": dict(outcomes),
            "actual_execution_n": evaluated,
            "execution_pending_n": states["success"] - len(set(executions) & ids),
            "pass_full_denominator": fraction(passed, len(ids)),
            "pass_conditional_on_execution": fraction(passed, evaluated),
        }

    summary = group(tasks)
    summary.update(
        {
            "schema": "lcb60-generation-execution-report-v1",
            "metrics_scope": "fixed_stratified_stdio_subset_single_sample",
            "per_difficulty": {
                d: group([t for t in tasks if t.difficulty.value == d])
                for d in sorted({t.difficulty.value for t in tasks})
            },
            "provider_attempts": sum(e["event"].get("attempt_count", 0) for e in events),
            "generation_events": sum(e["event"]["kind"] == "generation" for e in events),
            "execution_events": sum(e["event"]["kind"] == "execution" for e in events),
            "limitations": [
                "not_full_livecodebench",
                "difficulty_balanced_not_natural_prevalence",
                "no_training_contamination_guarantee",
                "process_judge_not_run",
            ],
        }
    )
    summary["complete"] = summary["actual_execution_n"] == len(tasks)
    return summary


async def run_experiment(
    *,
    tasks,
    records,
    adapter,
    executor,
    provider,
    run_dir: Path,
    identity: dict,
    resume=False,
    phase="all",
    progress=print,
):
    """One sample per task. Resume retries only generation failures/infra errors."""
    if phase not in {"all", "generate", "execute"}:
        raise ValueError("invalid phase")
    if len({t.identity.task_id for t in tasks}) != len(tasks) or not tasks:
        raise ValueError("task coverage violation")
    run_id = run_dir.name
    with run_lock(run_dir):
        manifest_path = run_dir / "manifest.json"
        if resume:
            manifest = json.loads(manifest_path.read_text())
            if manifest.get("identity") != identity:
                raise ValueError("resume refused: source, data, provider, image or limits changed")
        else:
            if manifest_path.exists():
                raise ValueError("existing run requires --resume")
            if list((run_dir / "events").glob("*.json")):
                raise ValueError("events exist without a manifest")
            manifest = {
                "schema": "lcb60-run-v1",
                "run_id": run_id,
                "started_at": now(),
                "identity": identity,
            }
            write_json(manifest_path, manifest, immutable=True)
        events, generations, executions = load_events(run_dir, tasks, run_id)
        try:
            for index, task in enumerate(tasks, 1):
                tid = task.identity.task_id
                if (
                    phase in {"all", "generate"}
                    and generations.get(tid, {}).get("status") != "success"
                ):
                    if provider is None:
                        raise ValueError("generation requires provider")
                    started, clock = now(), time.monotonic()
                    outcome = await provider.generate_task(task)
                    event = {
                        "kind": "generation",
                        "task_id": tid,
                        "started_at": started,
                        "completed_at": now(),
                        "duration_seconds": time.monotonic() - clock,
                        "status": outcome.status,
                        "attempt_count": outcome.attempt_count,
                        "attempt_outcomes": list(outcome.attempt_outcomes),
                        "error_type": type(outcome.error).__name__ if outcome.error else None,
                        "solution": outcome.solution.model_dump(mode="json")
                        if outcome.solution
                        else None,
                        "raw_output": outcome.raw_output,
                    }
                    append_event(run_dir, events, event)
                    generations[tid] = event
                    progress(f"[generate] {index}/{len(tasks)} {tid}: {outcome.status}")
                    if event["error_type"] == "ProviderAuthError":
                        break
                if (
                    phase in {"all", "execute"}
                    and generations.get(tid, {}).get("status") == "success"
                ):
                    previous = executions.get(tid)
                    if previous and previous.status not in {
                        ExecutionStatus.INFRASTRUCTURE_ERROR,
                        ExecutionStatus.NOT_RUN,
                    }:
                        continue
                    record = records[tid]
                    solution = SolutionTrace.model_validate(generations[tid]["solution"])
                    candidate = candidate_for(task, solution, run_id)
                    raw = executor.run_task(
                        task=task,
                        candidate=candidate,
                        public_test_cases_raw=record.public_test_cases_raw,
                        private_test_cases_raw=record.private_test_cases_raw,
                        public_test_count=len(record.public_test_cases),
                        per_test_timeout_seconds=identity["per_test_timeout_seconds"],
                        workspace=run_dir,
                    )
                    result = adapter.normalize_execution_result(
                        task=task, candidate=candidate, result=raw
                    )
                    append_event(
                        run_dir,
                        events,
                        {
                            "kind": "execution",
                            "task_id": tid,
                            "completed_at": now(),
                            "result": result.model_dump(mode="json"),
                        },
                    )
                    executions[tid] = result
                    progress(f"[execute] {index}/{len(tasks)} {tid}: {result.status.value}")
        finally:
            summary = summarize(tasks, generations, executions, events)
            write_json(run_dir / "report.json", summary)
            write_json(
                run_dir / "completion_receipt.json",
                {
                    "updated_at": now(),
                    "complete": summary["complete"],
                    "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                    "report_sha256": hashlib.sha256(
                        (run_dir / "report.json").read_bytes()
                    ).hexdigest(),
                    "event_count": len(events),
                    "last_event_sha256": events[-1]["sha256"] if events else None,
                },
            )
    return summary
