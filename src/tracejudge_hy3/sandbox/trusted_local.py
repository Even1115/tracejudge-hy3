"""Local subprocess sandbox.

This provides NO real isolation beyond separate OS processes and a per-test
parent-enforced timeout -- it must only be used for code the caller already
trusts (built-in mock fixtures) or when the user has explicitly opted in with
--allow-unsafe-local-exec. Enforcing that policy is the caller's job (see
pipeline/runner.py and cli.py); this class itself will run whatever code it is
given.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
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


class TrustedLocalSandbox(SandboxBackend):
    name = "trusted-local"

    def __init__(
        self,
        per_test_timeout_seconds: float = 5.0,
        *,
        allow_untrusted_code: bool = False,
    ) -> None:
        self.per_test_timeout_seconds = per_test_timeout_seconds
        self.allow_untrusted_code = allow_untrusted_code

    def is_available(self) -> tuple[bool, str | None]:
        return True, None

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

        with tempfile.TemporaryDirectory(prefix="tracejudge_local_") as tmp:
            tmp_path = Path(tmp)
            code_path = tmp_path / "code.py"
            runner_path = tmp_path / "runner.py"
            tests_path = tmp_path / "tests.json"

            code_path.write_text(code, encoding="utf-8")
            runner_path.write_text(RUNNER_SCRIPT, encoding="utf-8")
            tests_path.write_text(json.dumps(test_cases_payload(test_cases)), encoding="utf-8")

            overall_timeout = self.per_test_timeout_seconds * len(test_cases) + 10
            try:
                proc = subprocess.run(
                    [
                        sys.executable,
                        str(runner_path),
                        str(code_path),
                        function_name,
                        str(tests_path),
                        str(self.per_test_timeout_seconds),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=overall_timeout,
                )
            except subprocess.TimeoutExpired:
                return ExecutionSummary(
                    problem_id="",
                    function_name=function_name,
                    sandbox_backend=self.name,
                    results=[],
                    runtime_status="backend_error",
                    setup_error=f"local subprocess exceeded overall timeout {overall_timeout}s",
                )

            if proc.returncode != 0 or not proc.stdout.strip():
                return ExecutionSummary(
                    problem_id="",
                    function_name=function_name,
                    sandbox_backend=self.name,
                    results=[],
                    runtime_status="backend_error",
                    setup_error=(proc.stderr or "runner produced no output").strip()[:2000],
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
