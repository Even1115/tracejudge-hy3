"""Shared test-execution logic used by every sandbox backend.

The actual candidate-code execution happens inside RUNNER_SCRIPT, a standalone
Python script with no dependency on this package, so it can run unmodified
inside a Docker container or a local subprocess. It never calls eval() on test
input -- args/kwargs are loaded from JSON and passed positionally/by keyword.

build_execution_summary() then does the (backend-independent) comparison of
actual vs. expected output.
"""

from __future__ import annotations

import json

from tracejudge_hy3.schemas.execution import ExecutionSummary, TestExecutionResult
from tracejudge_hy3.schemas.problem import TestCase

RUNNER_SCRIPT = """\
import ast
import importlib.util
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


_MAX_CAPTURE_BYTES = 65536
_MAX_RESULT_CHARS = 1000000
_MAX_REPORT_BYTES = 2000000


def _write_report(path, payload, *, _dumps=json.dumps, _open=open):
    with _open(path, "w", encoding="utf-8") as report_file:
        report_file.write(_dumps(payload))


def _exception_message(exc):
    try:
        return str(exc)[:2000]
    except BaseException:
        return "exception message could not be rendered"


def _serializable_output(output, *, _dumps=json.dumps):
    try:
        _dumps(output)
        return output
    except (TypeError, ValueError, OverflowError):
        try:
            return repr(output)
        except BaseException:
            return "<output could not be rendered>"


def _child_main():
    code_path, function_name, case_path, report_path = sys.argv[2:6]

    with open(case_path, encoding="utf-8") as case_file:
        case = json.load(case_file)

    spec = importlib.util.spec_from_file_location("candidate_module", code_path)
    if spec is None or spec.loader is None:
        _write_report(report_path, {
            "runtime_status": "import_error",
            "setup_error": "could not create an import spec for candidate code",
            "case": None,
        })
        return

    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except BaseException as exc:
        _write_report(report_path, {
            "runtime_status": "import_error",
            "setup_error": f"{type(exc).__name__}: {_exception_message(exc)}",
            "case": None,
        })
        return

    func = getattr(module, function_name, None)
    if func is None or not callable(func):
        _write_report(report_path, {
            "runtime_status": "import_error",
            "setup_error": f"function '{function_name}' not found in candidate code",
            "case": None,
        })
        return

    start = time.perf_counter()
    output = None
    exc_type = None
    exc_msg = None
    try:
        output = func(*case.get("args", []), **case.get("kwargs", {}))
    except BaseException as exc:
        exc_type = type(exc).__name__
        exc_msg = _exception_message(exc)
    elapsed_ms = (time.perf_counter() - start) * 1000

    serializable_output = _serializable_output(output)
    try:
        output_size = len(json.dumps(serializable_output))
    except BaseException:
        output_size = _MAX_RESULT_CHARS + 1
    if output_size > _MAX_RESULT_CHARS and exc_type is None:
        serializable_output = None
        exc_type = "OutputLimitError"
        exc_msg = f"serialized output exceeded {_MAX_RESULT_CHARS} characters"

    _write_report(report_path, {
        "runtime_status": "completed",
        "setup_error": None,
        "case": {
            "case_id": case["case_id"],
            "output": serializable_output,
            "exception_type": exc_type,
            "exception_message": exc_msg,
            "timed_out": False,
            "execution_time_ms": elapsed_ms,
        },
    })


def _stop_process_group(proc):
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif proc.poll() is None:
        proc.kill()

    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _failed_case(
    case_id,
    exc_type,
    message,
    elapsed_ms,
    stdout="",
    stderr="",
    exit_code=None,
):
    return {
        "runtime_status": "completed",
        "setup_error": None,
        "case": {
            "case_id": case_id,
            "output": None,
            "exception_type": exc_type,
            "exception_message": message,
            "timed_out": exc_type == "TimeoutError",
            "execution_time_ms": elapsed_ms,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
        },
    }


def _drain_bounded(stream, captured):
    while True:
        chunk = stream.read(8192)
        if not chunk:
            return
        remaining = _MAX_CAPTURE_BYTES - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])


def _captured_text(data):
    return bytes(data).decode("utf-8", errors="replace")


def _validate_child_result(payload, expected_case_id):
    if not isinstance(payload, dict):
        return None
    status = payload.get("runtime_status")
    if status == "import_error":
        setup_error = payload.get("setup_error")
        if not isinstance(setup_error, str):
            return None
        return {"runtime_status": "import_error", "setup_error": setup_error, "case": None}
    if status != "completed" or payload.get("setup_error") is not None:
        return None

    case = payload.get("case")
    if not isinstance(case, dict) or case.get("case_id") != expected_case_id:
        return None
    exception_type = case.get("exception_type")
    exception_message = case.get("exception_message")
    timed_out = case.get("timed_out")
    execution_time_ms = case.get("execution_time_ms")
    if exception_type is not None and not isinstance(exception_type, str):
        return None
    if exception_message is not None and not isinstance(exception_message, str):
        return None
    if not isinstance(timed_out, bool):
        return None
    if (
        isinstance(execution_time_ms, bool)
        or not isinstance(execution_time_ms, (int, float))
        or not math.isfinite(execution_time_ms)
        or execution_time_ms < 0
    ):
        return None
    return {
        "runtime_status": "completed",
        "setup_error": None,
        "case": {
            "case_id": expected_case_id,
            "output": case.get("output"),
            "exception_type": exception_type,
            "exception_message": exception_message,
            "timed_out": timed_out,
            "execution_time_ms": execution_time_ms,
        },
    }


def _run_case(code_path, function_name, case, timeout_s):
    case_id = case["case_id"]
    with tempfile.TemporaryDirectory(prefix="tracejudge_case_") as tmp:
        tmp_path = Path(tmp)
        case_path = tmp_path / "case.json"
        report_path = tmp_path / "result.json"
        case_path.write_text(json.dumps(case), encoding="utf-8")

        start = time.perf_counter()
        try:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    __file__,
                    "--case-child",
                    code_path,
                    function_name,
                    str(case_path),
                    str(report_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000
            return _failed_case(
                case_id,
                "ChildProcessError",
                f"could not start isolated test process: {_exception_message(exc)}",
                elapsed_ms,
            )

        captured_stdout = bytearray()
        captured_stderr = bytearray()
        stdout_thread = threading.Thread(
            target=_drain_bounded,
            args=(proc.stdout, captured_stdout),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_drain_bounded,
            args=(proc.stderr, captured_stderr),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _stop_process_group(proc)
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)
            elapsed_ms = (time.perf_counter() - start) * 1000
            return _failed_case(
                case_id,
                "TimeoutError",
                f"exceeded {timeout_s}s",
                elapsed_ms,
                _captured_text(captured_stdout),
                _captured_text(captured_stderr),
                proc.returncode,
            )

        # A candidate may leave descendants behind. Each case has its own process
        # group, so clean it up before advancing to the next isolated case.
        _stop_process_group(proc)
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
        elapsed_ms = (time.perf_counter() - start) * 1000
        stdout = _captured_text(captured_stdout)
        stderr = _captured_text(captured_stderr)

        if not report_path.is_file():
            return _failed_case(
                case_id,
                "ChildProcessError",
                f"isolated test process exited with status {proc.returncode}",
                elapsed_ms,
                stdout,
                stderr,
                proc.returncode,
            )

        try:
            if report_path.stat().st_size > _MAX_REPORT_BYTES:
                return _failed_case(
                    case_id,
                    "OutputLimitError",
                    f"isolated test report exceeded {_MAX_REPORT_BYTES} bytes",
                    elapsed_ms,
                    stdout,
                    stderr,
                    proc.returncode,
                )
        except OSError as exc:
            return _failed_case(
                case_id,
                "ChildProcessError",
                f"could not inspect isolated test result: {_exception_message(exc)}",
                elapsed_ms,
                stdout,
                stderr,
                proc.returncode,
            )

        try:
            child_result = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return _failed_case(
                case_id,
                "ChildProcessError",
                f"invalid isolated test result: {_exception_message(exc)}",
                elapsed_ms,
                stdout,
                stderr,
                proc.returncode,
            )

        child_result = _validate_child_result(child_result, case_id)
        if child_result is None:
            return _failed_case(
                case_id,
                "ChildProcessError",
                "isolated test result failed protocol validation",
                elapsed_ms,
                stdout,
                stderr,
                proc.returncode,
            )
        raw_case = child_result.get("case")
        if isinstance(raw_case, dict):
            raw_case["stdout"] = stdout
            raw_case["stderr"] = stderr
            raw_case["exit_code"] = proc.returncode
        return child_result


def main():
    code_path, function_name, tests_path, timeout_s = (
        sys.argv[1],
        sys.argv[2],
        sys.argv[3],
        float(sys.argv[4]),
    )

    with open(tests_path, encoding="utf-8") as f:
        test_cases = json.load(f)

    with open(code_path, encoding="utf-8") as f:
        source = f.read()

    try:
        ast.parse(source)
    except SyntaxError as exc:
        print(json.dumps({
            "runtime_status": "syntax_error",
            "setup_error": f"{exc.msg} (line {exc.lineno})",
            "cases": [],
        }))
        return

    results = []
    for case in test_cases:
        child_result = _run_case(code_path, function_name, case, timeout_s)
        if child_result.get("runtime_status") == "import_error":
            print(json.dumps({
                "runtime_status": "import_error",
                "setup_error": child_result.get("setup_error"),
                "cases": [],
            }))
            return
        raw_case = child_result.get("case")
        if not isinstance(raw_case, dict):
            raw_case = _failed_case(
                case["case_id"],
                "ChildProcessError",
                "isolated test returned no case result",
                0.0,
            )["case"]
        results.append(raw_case)

    print(json.dumps({"runtime_status": "completed", "setup_error": None, "cases": results}))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--case-child":
        _child_main()
    else:
        main()
"""


def test_cases_payload(test_cases: list[TestCase]) -> list[dict]:
    return [{"case_id": tc.case_id, "args": tc.args, "kwargs": tc.kwargs} for tc in test_cases]


def build_execution_summary(
    function_name: str,
    backend_name: str,
    raw: object,
    test_cases: list[TestCase],
) -> ExecutionSummary:
    if not isinstance(raw, dict):
        return ExecutionSummary(
            problem_id="",
            function_name=function_name,
            sandbox_backend=backend_name,
            results=[],
            runtime_status="backend_error",
            setup_error="sandbox runner returned a non-object payload",
        )

    runtime_status = raw.get("runtime_status", "backend_error")
    setup_error = raw.get("setup_error")
    valid_statuses = {"completed", "import_error", "syntax_error", "backend_error"}
    if runtime_status not in valid_statuses:
        return ExecutionSummary(
            problem_id="",
            function_name=function_name,
            sandbox_backend=backend_name,
            results=[],
            runtime_status="backend_error",
            setup_error=f"sandbox runner returned invalid runtime_status: {runtime_status!r}",
        )
    if setup_error is not None and not isinstance(setup_error, str):
        return ExecutionSummary(
            problem_id="",
            function_name=function_name,
            sandbox_backend=backend_name,
            results=[],
            runtime_status="backend_error",
            setup_error="sandbox runner returned a non-string setup_error",
        )

    if runtime_status != "completed":
        return ExecutionSummary(
            problem_id="",
            function_name=function_name,
            sandbox_backend=backend_name,
            results=[],
            runtime_status=runtime_status,  # type: ignore[arg-type]
            setup_error=setup_error,
        )

    raw_cases = raw.get("cases", [])
    if not isinstance(raw_cases, list):
        raw_cases = []
    by_case = {
        case["case_id"]: case
        for case in raw_cases
        if isinstance(case, dict) and isinstance(case.get("case_id"), str)
    }
    results: list[TestExecutionResult] = []
    for tc in test_cases:
        raw_case = by_case.get(tc.case_id)
        if raw_case is None:
            results.append(
                TestExecutionResult(
                    case_id=tc.case_id,
                    category=tc.category,
                    passed=False,
                    expected_output=tc.expected,
                    exception_type="MissingResult",
                    exception_message="no execution result returned by sandbox",
                    related_requirements=tc.related_requirements,
                )
            )
            continue

        required_fields = {
            "output",
            "exception_type",
            "exception_message",
            "execution_time_ms",
            "timed_out",
        }
        if not required_fields.issubset(raw_case):
            results.append(
                TestExecutionResult(
                    case_id=tc.case_id,
                    category=tc.category,
                    passed=False,
                    expected_output=tc.expected,
                    exception_type="MalformedResult",
                    exception_message="sandbox result did not satisfy the case protocol",
                    related_requirements=tc.related_requirements,
                )
            )
            continue

        expected_exception = tc.expected_exception
        if expected_exception is not None:
            passed = not raw_case["timed_out"] and raw_case["exception_type"] == expected_exception
        else:
            passed = (
                raw_case["exception_type"] is None
                and not raw_case["timed_out"]
                and raw_case["output"] == tc.expected
            )
        results.append(
            TestExecutionResult(
                case_id=tc.case_id,
                category=tc.category,
                passed=passed,
                actual_output=raw_case["output"],
                expected_output=tc.expected,
                exception_type=raw_case["exception_type"],
                exception_message=raw_case["exception_message"],
                execution_time_ms=raw_case["execution_time_ms"],
                timed_out=raw_case["timed_out"],
                stdout=raw_case.get("stdout", ""),
                stderr=raw_case.get("stderr", ""),
                exit_code=raw_case.get("exit_code"),
                related_requirements=tc.related_requirements,
            )
        )

    return ExecutionSummary(
        problem_id="",
        function_name=function_name,
        sandbox_backend=backend_name,
        results=results,
        runtime_status="completed",
    )


def parse_runner_stdout(stdout: str) -> object:
    stdout = stdout.strip()
    if not stdout:
        raise json.JSONDecodeError("empty runner output", stdout, 0)
    last_line = stdout.splitlines()[-1]
    return json.loads(last_line)
