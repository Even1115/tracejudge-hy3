"""Docker-based sandbox: the default backend for real (non-mock) model code.

This provides "basic isolation", not an absolute security guarantee: no
network, dropped Linux capabilities, no-new-privileges, a read-only bind mount
of code/tests, and CPU/memory/pids limits, all via the Docker CLI. It is still
possible for a sufficiently motivated adversary to find container escapes or
exhaust resources within the granted limits.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from tracejudge_hy3.sandbox.base import SandboxBackend
from tracejudge_hy3.sandbox.test_runner import (
    RUNNER_SCRIPT,
    build_execution_summary,
    parse_runner_stdout,
    test_cases_payload,
)
from tracejudge_hy3.schemas.execution import ExecutionSummary
from tracejudge_hy3.schemas.problem import TestCase


class DockerSandbox(SandboxBackend):
    name = "docker"

    def __init__(
        self,
        image: str = "python:3.11-slim",
        memory_limit: str = "256m",
        cpu_limit: str = "1",
        per_test_timeout_seconds: float = 5.0,
    ) -> None:
        self.image = image
        self.memory_limit = memory_limit
        self.cpu_limit = cpu_limit
        self.per_test_timeout_seconds = per_test_timeout_seconds
        self._availability_cache: tuple[bool, str | None] | None = None

    def is_available(self) -> tuple[bool, str | None]:
        if self._availability_cache is not None:
            return self._availability_cache
        if shutil.which("docker") is None:
            self._availability_cache = (False, "docker CLI not found on PATH")
            return self._availability_cache
        try:
            proc = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            self._availability_cache = (False, f"docker info failed: {exc}")
            return self._availability_cache
        if proc.returncode != 0:
            self._availability_cache = (
                False,
                (proc.stderr or "docker daemon not reachable").strip()[:500],
            )
            return self._availability_cache
        self._availability_cache = (True, None)
        return self._availability_cache

    def run(
        self,
        code: str,
        function_name: str,
        test_cases: list[TestCase],
    ) -> ExecutionSummary:
        if not test_cases:
            return ExecutionSummary(
                problem_id="",
                function_name=function_name,
                sandbox_backend=self.name,
                results=[],
                runtime_status="completed",
            )

        available, reason = self.is_available()
        if not available:
            return ExecutionSummary(
                problem_id="",
                function_name=function_name,
                sandbox_backend=self.name,
                results=[],
                runtime_status="backend_error",
                setup_error=f"docker sandbox unavailable: {reason}",
            )

        with tempfile.TemporaryDirectory(prefix="tracejudge_docker_") as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "code.py").write_text(code, encoding="utf-8")
            (tmp_path / "runner.py").write_text(RUNNER_SCRIPT, encoding="utf-8")
            (tmp_path / "tests.json").write_text(
                json.dumps(test_cases_payload(test_cases)), encoding="utf-8"
            )

            overall_timeout = self.per_test_timeout_seconds * len(test_cases) + 30
            container_name = f"tracejudge-{uuid.uuid4().hex}"

            cmd = [
                "docker",
                "run",
                "--rm",
                "--name",
                container_name,
                "--network",
                "none",
                "--read-only",
                "--memory",
                self.memory_limit,
                "--cpus",
                self.cpu_limit,
                "--pids-limit",
                "128",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--tmpfs",
                "/tmp",
                "-v",
                f"{tmp_path}:/sandbox:ro",
                "-w",
                "/sandbox",
                "-e",
                "PYTHONDONTWRITEBYTECODE=1",
                self.image,
                "python",
                "runner.py",
                "code.py",
                function_name,
                "tests.json",
                str(self.per_test_timeout_seconds),
            ]

            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=overall_timeout,
                )
            except subprocess.TimeoutExpired:
                cleanup_error = self._force_remove(container_name)
                return ExecutionSummary(
                    problem_id="",
                    function_name=function_name,
                    sandbox_backend=self.name,
                    results=[],
                    runtime_status="backend_error",
                    setup_error=(
                        f"docker container exceeded overall timeout {overall_timeout}s"
                        + (f"; cleanup warning: {cleanup_error}" if cleanup_error else "")
                    ),
                )
            except OSError as exc:
                cleanup_error = self._force_remove(container_name)
                return ExecutionSummary(
                    problem_id="",
                    function_name=function_name,
                    sandbox_backend=self.name,
                    results=[],
                    runtime_status="backend_error",
                    setup_error=(
                        f"could not start docker sandbox: {exc}"
                        + (f"; cleanup warning: {cleanup_error}" if cleanup_error else "")
                    ),
                )

            if proc.returncode != 0 or not proc.stdout.strip():
                cleanup_error = self._force_remove(container_name)
                return ExecutionSummary(
                    problem_id="",
                    function_name=function_name,
                    sandbox_backend=self.name,
                    results=[],
                    runtime_status="backend_error",
                    setup_error=(
                        (proc.stderr or "container produced no output").strip()[:2000]
                        + (f"; cleanup warning: {cleanup_error}" if cleanup_error else "")
                    ),
                )

            try:
                raw = parse_runner_stdout(proc.stdout)
            except json.JSONDecodeError as exc:
                return ExecutionSummary(
                    problem_id="",
                    function_name=function_name,
                    sandbox_backend=self.name,
                    results=[],
                    runtime_status="backend_error",
                    setup_error=f"could not parse runner output: {exc}",
                )

            return build_execution_summary(function_name, self.name, raw, test_cases)

    @staticmethod
    def _force_remove(container_name: str) -> str | None:
        """Best-effort cleanup for timeout/error paths where --rm may not run."""

        try:
            proc = subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return str(exc)
        if proc.returncode == 0:
            return None
        message = (proc.stderr or proc.stdout).strip()
        if "No such container" in message:
            return None
        return message[:500] or f"docker rm exited with status {proc.returncode}"
