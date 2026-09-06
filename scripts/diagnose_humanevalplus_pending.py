#!/usr/bin/env python3
"""Serial, isolated diagnosis of unresolved HumanEval+ containers; no model calls.

Preserves the source run. Captures allowlisted Docker state before cleanup so a
diagnostic-induced SIGKILL is never mistaken for an OOM or a natural exit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tracejudge_hy3.evalplus import runner as phase2  # noqa: E402
from tracejudge_hy3.evalplus.docker_runner import DockerLimits, EvalPlusDockerRunner  # noqa: E402
from tracejudge_hy3.evalplus.exporter import load_validated_phase1_export  # noqa: E402
from tracejudge_hy3.evalplus.parser import parse_official_result  # noqa: E402


def now():
    return datetime.now(UTC).isoformat()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checkpoint(path, data):
    phase2._atomic_write_json(path, data)


def safe_state(state):
    """Return only typed state fields; never expose Docker Error strings."""
    result = {}
    for key in ("Running", "OOMKilled", "Dead", "Paused", "Restarting"):
        result[key] = state.get(key) if type(state.get(key)) is bool else None
    result["ExitCode"] = (
        state.get("ExitCode")
        if result["Running"] is False and type(state.get("ExitCode")) is int
        else None
    )
    for key in ("StartedAt", "FinishedAt"):
        value = state.get(key)
        result[key] = (
            value if isinstance(value, str) and re.fullmatch(r"[0-9TZ:.+\-]+", value) else None
        )
    if result.get("StartedAt") and result.get("FinishedAt") and not result["Running"]:
        try:
            start = datetime.fromisoformat(result["StartedAt"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(result["FinishedAt"].replace("Z", "+00:00"))
            result["runtime_seconds"] = max(0, (end - start).total_seconds())
        except ValueError:
            pass
    error = state.get("Error")
    result["has_error"] = bool(error)
    result["error_sha256"] = (
        hashlib.sha256(error.encode()).hexdigest() if isinstance(error, str) and error else None
    )
    return result


class DiagnosticRunner(EvalPlusDockerRunner):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.task_controls = {}
        self.observations = []
        self.calls = []
        self.problem_id = None
        self.output_dir = None
        source = (ROOT / "src/tracejudge_hy3/evalplus/container_entrypoint.py").read_text()
        self.control_errors = set(re.findall(r'_EntrypointError\("([a-z_]+)"', source)) | {
            "internal_error"
        }

    def _container_command(self, control, container_name, entrypoint_args, **kwargs):
        cmd = super()._container_command(control, container_name, entrypoint_args, **kwargs)
        if kwargs.get("detached"):
            self.task_controls[container_name] = kwargs["output_files"][1]
        return cmd

    def _invoke(self, command, *, timeout):
        started = time.monotonic()
        entry = {
            "operation": command[1] if len(command) > 1 else "unknown",
            "timeout_seconds": timeout,
        }
        try:
            result = super()._invoke(command, timeout=timeout)
            entry["cli_exit_code"] = result.returncode
            if entry["operation"] == "wait" and re.fullmatch(r"\d+", result.stdout.strip()):
                entry["container_exit_code"] = int(result.stdout.strip())
            return result
        except subprocess.TimeoutExpired:
            entry["timed_out"] = True
            raise
        finally:
            entry["elapsed_seconds"] = time.monotonic() - started
            self.calls.append(entry)

    def inspect_state(self, name):
        try:
            response = super()._invoke(
                ["docker", "inspect", "--format", "{{json .State}}", name],
                timeout=10,
            )
            if response.returncode == 0:
                state = json.loads(response.stdout)
                if isinstance(state, dict):
                    return {"available": True, **safe_state(state)}
            return {"available": False, "inspect_exit_code": response.returncode}
        except (OSError, subprocess.SubprocessError, ValueError):
            return {"available": False}

    def control_status(self, name):
        path = self.task_controls[name]
        try:
            if path.is_symlink() or path.stat().st_size > 16384:
                return {"available": False}
            data = json.loads(path.read_bytes())
            if not isinstance(data, dict):
                return {"available": False}
            result = {
                "available": True,
                "status": data.get("status") if data.get("status") in ("ok", "error") else None,
            }
            result["error_type"] = (
                data.get("error_type") if data.get("error_type") in self.control_errors else None
            )
            return result
        except (OSError, ValueError):
            return {"available": False}

    def _force_remove(self, container_name):
        if container_name not in self.task_controls:
            return super()._force_remove(container_name)
        observation = {
            "problem_id": self.problem_id,
            "container_name": container_name,
            "observed_at": now(),
            "before_cleanup": self.inspect_state(container_name),
            "control": self.control_status(container_name),
            "diagnostic_kill_requested": False,
        }
        # Only our exact, owned container is touched. Preserve the pre-kill state.
        if observation["before_cleanup"].get("Running") is True:
            observation["diagnostic_kill_requested"] = True
            try:
                response = super()._invoke(["docker", "kill", container_name], timeout=10)
                observation["kill_cli_exit_code"] = response.returncode
                if response.returncode == 0:
                    super()._invoke(["docker", "wait", container_name], timeout=10)
            except (OSError, subprocess.SubprocessError):
                observation["kill_or_wait_failed"] = True
            observation["after_diagnostic_kill"] = self.inspect_state(container_name)
        status = super()._force_remove(container_name)
        observation["cleanup_status"] = status
        self.observations.append(observation)
        if self.output_dir:
            checkpoint(self.output_dir / "container_observations.json", self.observations)
        return status


def require_idle():
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode or result.stdout.strip():
        raise ValueError("Docker must be available with no running containers for serial diagnosis")


def diagnose(args):
    source = args.source_run.resolve()
    manifest = json.loads((source / "manifest.json").read_bytes())
    paths = phase2._run_paths(source.parent, source.name)
    phase2._validate_completed_output_integrity(manifest, paths, expected_result_count=164)
    original_hashes = {p.name: sha(p) for p in source.iterdir() if p.is_file()}
    exported = load_validated_phase1_export(
        args.baseline_run, args.dataset_manifest, min_success_count=30
    )
    if asdict(exported.phase1) != manifest["phase1_source"]:
        raise ValueError("phase-one source differs")
    if manifest["git"]["implementation_sha256"] != phase2._implementation_sha256():
        raise ValueError("EvalPlus implementation differs from source run")
    existing = phase2._validated_existing_results(paths, exported, run_id=source.name)
    pending = [
        sample
        for sample in exported.samples
        if existing[sample.task_id]["infrastructure_status"] == "error"
    ]
    if args.problem_id:
        if not set(args.problem_id) <= {s.task_id for s in pending}:
            raise ValueError("only unresolved source tasks may be diagnosed")
        pending = [s for s in pending if s.task_id in args.problem_id]
    if not pending:
        raise ValueError("no unresolved source tasks")
    require_idle()
    output = args.output_dir.resolve()
    phase2._require_non_trackable_run_directory(output)
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    runner = DiagnosticRunner(limits=DockerLimits(per_task_timeout_seconds=args.per_task_timeout))
    runner.output_dir = output
    record = {
        "schema": "humanevalplus-container-diagnosis-v1",
        "started_at": now(),
        "purpose": "serial_diagnosis_not_a_resume_or_replacement_of_formal_run",
        "source_run": str(source),
        "source_hashes": original_hashes,
        "diagnostic_script_sha256": sha(Path(__file__)),
        "implementation_sha256": phase2._implementation_sha256(),
        "source_git": manifest["git"],
        "diagnostic_git": phase2._git_metadata(),
        "environment": phase2._environment_metadata(),
        "executor": runner.public_identity(),
        "serial": True,
        "model_api_calls": 0,
        "tasks": [s.task_id for s in pending],
        "results": [],
    }
    checkpoint(output / "report.json", record)
    meta = {m.problem_id: m for m in exported.task_metadata}
    preflight_dir = output / "preflight"
    preflight_dir.mkdir(mode=0o700)
    preflight = runner.preflight(
        task_metadata=[meta[s.task_id] for s in pending], workspace=preflight_dir
    )
    record["preflight"] = asdict(preflight)
    checkpoint(output / "report.json", record)
    if not preflight.ready:
        raise ValueError("Docker preflight failed")
    for sample in pending:
        require_idle()
        runner.problem_id = sample.task_id
        task_dir = output / sample.task_id.replace("/", "_")
        task_dir.mkdir(mode=0o700)
        first_call = len(runner.calls)
        started = time.monotonic()
        print(f"[start] {sample.task_id}, limit={args.per_task_timeout}s, serial", flush=True)
        outcome = runner.run_task(
            sample=sample, task_metadata=meta[sample.task_id], workspace=task_dir
        )
        item = {
            "problem_id": sample.task_id,
            "code_sha256": exported.reference_for(sample.task_id).code_sha256,
            "started_at": outcome.started_at,
            "ended_at": outcome.ended_at,
            "total_elapsed_seconds": time.monotonic() - started,
            "infrastructure_error_type": outcome.infrastructure_error_type,
            "container_observations": [
                o for o in runner.observations if o["problem_id"] == sample.task_id
            ],
            "docker_calls": runner.calls[first_call:],
        }
        if outcome.raw_result is not None:
            safe = parse_official_result(
                outcome.raw_result,
                expected_problem_id=sample.task_id,
                expected_solution_sha256=item["code_sha256"],
            )
            item["official_result"] = {
                k: safe.get(k)
                for k in ("passed_base", "passed_plus", "base_status", "plus_status", "error_type")
            }
        record["results"].append(item)
        checkpoint(output / "report.json", record)
        print(json.dumps(item, ensure_ascii=False), flush=True)
        if outcome.diagnostics.get("cleanup_status") == "failed":
            raise ValueError("owned container cleanup failed; stop diagnosis")
    record["completed_at"] = now()
    record["source_unchanged"] = original_hashes == {
        p.name: sha(p) for p in source.iterdir() if p.is_file()
    }
    checkpoint(output / "report.json", record)
    if not record["source_unchanged"]:
        raise ValueError("source changed during diagnosis")
    print(f"[done] {output / 'report.json'}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--baseline-run", type=Path, required=True)
    parser.add_argument("--dataset-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--problem-id", action="append")
    parser.add_argument("--per-task-timeout", type=float, default=180)
    args = parser.parse_args()
    try:
        diagnose(args)
    except Exception as exc:
        print(
            f"[blocked] diagnosis failed ({type(exc).__name__}); no raw exception echoed",
            flush=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
