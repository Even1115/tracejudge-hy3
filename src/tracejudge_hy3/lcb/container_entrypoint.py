"""Container-side entrypoint for the pinned official LiveCodeBench checker.

This file is staged read-only into a hardened container (no network,
read-only root filesystem, all capabilities dropped) and invoked as::

    python3 -B -u /control/entrypoint.py

It is deliberately self-contained: only stdlib imports at module level so the
host test suite can import it without the checker package or numpy.  The
official checker (``lcb_runner``) and numpy are imported lazily inside the
container at ``/opt/lcb`` (see ``docker/livecodebench/Dockerfile``).

Flow:

1. Read and strictly validate ``/control/request.json`` plus the candidate
   source file; verify the candidate SHA-256.
2. Run a pure ``compile()`` syntax probe (``compile_ok``).  The official
   harness buckets syntax errors into ``-4``; this probe is what lets the
   normalizer distinguish a genuine compile error downstream.
3. Decode the public tests (plain JSON) and the hidden tests via the official
   ``json.loads(pickle.loads(zlib.decompress(base64.b64decode(...))))`` chain.
   The pickle decode executes data, which is exactly why it only happens
   here, inside the sandbox, never on the host.
4. Replicate the official ``check_correctness`` global-timeout wrapper and
   the ``evaluate_generations_by_problem`` per-generation semantics
   (``[-2]`` default result, numpy type fixing, exception -> ``-5``
   TestRunnerError) against the pinned ``testing_util.run_test``.
5. Write exactly the 11-field raw-result mapping to the pre-created
   ``/output/result.json`` bind.  Failure inputs, expected outputs, captured
   output, and error messages never leave the container.
"""

from __future__ import annotations

import base64
import hashlib
import json
import multiprocessing
import os
import pickle  # noqa: F401  (documenting the official decode chain; used via pickle.loads)
import re
import sys
import time
import zlib
from pathlib import Path
from typing import Any

# Drift-guarded duplicates of benchmark.livecodebench constants; the host
# test suite asserts these stay equal to the pinned adapter values.  The
# entrypoint cannot import the adapter because it must stay self-contained.
CHECKER_COMMIT = "28fef95ea8c9f7a547c8329f2cd3d32b92c1fa24"
EXECUTOR_ID = "official_livecodebench_codegen_stdio"

CHECKER_PACKAGE_ROOT = "/opt/lcb"
CONTROL_DIR = Path("/control")
REQUEST_PATH = CONTROL_DIR / "request.json"
CANDIDATE_PATH = CONTROL_DIR / "candidate.py"
RESULT_PATH = Path("/output/result.json")

_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "question_id",
        "candidate_sha256",
        "public_test_cases",
        "private_test_cases",
        "public_test_count",
        "per_test_timeout_seconds",
    }
)
_RESULT_FIELDS = frozenset(
    {
        "question_id",
        "candidate_sha256",
        "infrastructure_status",
        "error_type",
        "compile_ok",
        "official_test_results",
        "official_error_code",
        "public_test_count",
        "duration_seconds",
        "checker_commit",
        "executor_id",
    }
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$")

_MAX_REQUEST_BYTES = 64 * 1024 * 1024
_MAX_CANDIDATE_BYTES = 1024 * 1024
_OFFICIAL_WRONG_ANSWER = -2
_OFFICIAL_TIMEOUT = -3
_OFFICIAL_RUNTIME_ERROR = -4
_OFFICIAL_TEST_RUNNER_ERROR = -5
_ALLOWED_RESULT_CODES = frozenset(
    {_OFFICIAL_WRONG_ANSWER, _OFFICIAL_TIMEOUT, _OFFICIAL_RUNTIME_ERROR}
)

_DEFAULT_PER_TEST_TIMEOUT = 6
_MAX_PER_TEST_TIMEOUT = 120


class _EntrypointError(RuntimeError):
    """Internal failure that must surface as an infrastructure result."""


def _read_limited(path: Path, *, max_bytes: int) -> bytes:
    try:
        metadata = path.lstat()
    except OSError:
        raise _EntrypointError("required control file is unavailable") from None
    if path.is_symlink() or not metadata.st_mode or not os.path.isfile(path):
        raise _EntrypointError("required control file must be a regular file")
    if metadata.st_size > max_bytes:
        raise _EntrypointError("control file exceeds its size limit")
    with path.open("rb") as stream:
        payload = stream.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise _EntrypointError("control file exceeds its size limit")
    return payload


def load_request(path: Path = REQUEST_PATH) -> dict[str, Any]:
    """Strictly validate the control request; unknown fields are rejected."""

    try:
        request = json.loads(_read_limited(path, max_bytes=_MAX_REQUEST_BYTES))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _EntrypointError("request is not valid JSON") from None
    if not isinstance(request, dict) or set(request) != _REQUEST_FIELDS:
        raise _EntrypointError("request schema is invalid")
    if request["schema_version"] != 1:
        raise _EntrypointError("request schema_version is unsupported")
    if not isinstance(request["question_id"], str) or not _TASK_ID_RE.fullmatch(
        request["question_id"]
    ):
        raise _EntrypointError("request question_id is invalid")
    if not isinstance(request["candidate_sha256"], str) or not _SHA256_RE.fullmatch(
        request["candidate_sha256"]
    ):
        raise _EntrypointError("request candidate_sha256 is invalid")
    for field in ("public_test_cases", "private_test_cases"):
        if not isinstance(request[field], str):
            raise _EntrypointError(f"request {field} must be a string")
    public_count = request["public_test_count"]
    if isinstance(public_count, bool) or not isinstance(public_count, int) or public_count < 0:
        raise _EntrypointError("request public_test_count is invalid")
    timeout = request["per_test_timeout_seconds"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not 1 <= timeout <= _MAX_PER_TEST_TIMEOUT
    ):
        raise _EntrypointError("request per_test_timeout_seconds is invalid")
    return request


def load_candidate(path: Path, *, expected_sha256: str) -> str:
    payload = _read_limited(path, max_bytes=_MAX_CANDIDATE_BYTES)
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise _EntrypointError("candidate hash does not match the request")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        raise _EntrypointError("candidate is not valid UTF-8") from None


def compile_probe(code: str) -> bool:
    """Pure syntax probe; never executes candidate code."""

    try:
        compile(code, "<candidate>", "exec")
    except (SyntaxError, ValueError, OverflowError, MemoryError, RecursionError):
        return False
    return True


def decode_public_tests(raw: str) -> list[dict[str, Any]]:
    """Public tests are plain JSON (official ``CodeGenerationProblem`` line)."""

    try:
        cases = json.loads(raw)
    except json.JSONDecodeError:
        raise _EntrypointError("public test cases are not valid JSON") from None
    if not isinstance(cases, list):
        raise _EntrypointError("public test cases must be a list")
    return cases


def decode_private_tests(raw: str) -> list[dict[str, Any]]:
    """Official hidden-test decode chain; runs pickle, container-only."""

    import pickle as _pickle

    try:
        decoded = json.loads(_pickle.loads(zlib.decompress(base64.b64decode(raw.encode("utf-8")))))
    except Exception:
        raise _EntrypointError("private test cases do not follow the official encoding") from None
    if not isinstance(decoded, list):
        raise _EntrypointError("private test cases must decode to a list")
    return decoded


def build_evaluation_sample(
    public_cases: list[dict[str, Any]], private_cases: list[dict[str, Any]]
) -> dict[str, str]:
    """Replicate ``CodeGenerationProblem.get_evaluation_sample`` for stdio."""

    inputs: list[str] = []
    outputs: list[str] = []
    for case in (*public_cases, *private_cases):
        if not isinstance(case, dict) or not isinstance(case.get("input"), str):
            raise _EntrypointError("test case schema is invalid")
        if not isinstance(case.get("output"), str):
            raise _EntrypointError("test case schema is invalid")
        inputs.append(case["input"])
        outputs.append(case["output"])
    if not inputs:
        raise _EntrypointError("evaluation sample must contain at least one test")
    return {"input_output": json.dumps({"inputs": inputs, "outputs": outputs})}


def _temp_run(
    sample: dict[str, str],
    generation: str,
    result: Any,
    metadata_list: Any,
    timeout: int,
) -> None:
    """Official ``_temp_run``; module-level so multiprocessing can pickle it."""

    sys.path.insert(0, CHECKER_PACKAGE_ROOT)
    from lcb_runner.evaluation.testing_util import run_test

    res, metadata = run_test(sample, test=generation, debug=False, timeout=timeout)
    result.append(res)
    metadata_list.append(metadata)


def check_correctness(sample: dict[str, str], generation: str, timeout: int) -> tuple[Any, Any]:
    """Replicate the official global-timeout wrapper around ``run_test``."""

    manager = multiprocessing.Manager()
    try:
        result = manager.list()
        metadata_list = manager.list()
        process = multiprocessing.Process(
            target=_temp_run,
            args=(sample, generation, result, metadata_list, timeout),
        )
        process.start()
        process.join(timeout=(timeout + 1) * len(json.loads(sample["input_output"])["inputs"]) + 5)
        if process.is_alive():
            process.kill()
        if not result:
            in_outs = json.loads(sample["input_output"])
            result = [[-1 for _ in range(len(in_outs["inputs"]))]]
        return result[0], metadata_list[0]
    finally:
        manager.shutdown()


def fix_official_types(results: Any) -> list[Any]:
    """Official numpy type fixing from ``evaluate_generations_by_problem``."""

    import numpy as np

    if not isinstance(results, list):
        raise _EntrypointError("official results are not a list")
    fixed: list[Any] = []
    for item in results:
        if isinstance(item, np.ndarray):
            item = item.item(0)
        if isinstance(item, np.bool_):
            item = bool(item)
        fixed.append(item)
    return fixed


def sanitize_result_codes(fixed: list[Any]) -> list[Any]:
    """Keep only official stdio codes; anything else is a harness anomaly."""

    sanitized: list[Any] = []
    for item in fixed:
        if item is True:
            sanitized.append(True)
        elif (
            isinstance(item, int) and not isinstance(item, bool) and item in (_ALLOWED_RESULT_CODES)
        ):
            sanitized.append(item)
        else:
            raise _EntrypointError("official results contain an unsupported code")
    return sanitized


def extract_official_error_code(metadata: Any) -> int | None:
    """Surface only the official ``-5`` TestRunnerError infrastructure code."""

    if isinstance(metadata, dict) and metadata.get("error_code") == _OFFICIAL_TEST_RUNNER_ERROR:
        return _OFFICIAL_TEST_RUNNER_ERROR
    return None


def extract_official_duration(metadata: Any, measured: float) -> float:
    if isinstance(metadata, dict):
        official = metadata.get("execution time")
        if isinstance(official, int | float) and not isinstance(official, bool) and official >= 0:
            return float(official)
    return measured


def build_result(
    *,
    question_id: str,
    candidate_sha256: str,
    infrastructure_status: str,
    error_type: str | None,
    compile_ok: bool,
    official_test_results: list[Any],
    official_error_code: int | None,
    public_test_count: int,
    duration_seconds: float | None,
) -> dict[str, Any]:
    """Assemble exactly the 11-field raw-result mapping; nothing else."""

    result = {
        "question_id": question_id,
        "candidate_sha256": candidate_sha256,
        "infrastructure_status": infrastructure_status,
        "error_type": error_type,
        "compile_ok": compile_ok,
        "official_test_results": official_test_results,
        "official_error_code": official_error_code,
        "public_test_count": public_test_count,
        "duration_seconds": duration_seconds,
        "checker_commit": CHECKER_COMMIT,
        "executor_id": EXECUTOR_ID,
    }
    if set(result) != _RESULT_FIELDS:
        raise _EntrypointError("raw result assembly drifted from the schema")
    return result


def _write_result(result: dict[str, Any], path: Path = RESULT_PATH) -> None:
    payload = (
        json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _evaluate(request: dict[str, Any], code: str) -> dict[str, Any]:
    compile_ok = compile_probe(code)
    if not compile_ok:
        return build_result(
            question_id=request["question_id"],
            candidate_sha256=request["candidate_sha256"],
            infrastructure_status="ok",
            error_type=None,
            compile_ok=False,
            official_test_results=[],
            official_error_code=None,
            public_test_count=request["public_test_count"],
            duration_seconds=None,
        )

    public_cases = decode_public_tests(request["public_test_cases"])
    if len(public_cases) != request["public_test_count"]:
        raise _EntrypointError("public test count does not match the request")
    private_cases = decode_private_tests(request["private_test_cases"])
    sample = build_evaluation_sample(public_cases, private_cases)

    timeout = request["per_test_timeout_seconds"]
    started = time.monotonic()
    try:
        results, metadata = check_correctness(sample, code, timeout)
        measured = time.monotonic() - started
        fixed = fix_official_types(results)
        sanitized = sanitize_result_codes(fixed)
        if not sanitized:
            raise _EntrypointError("official results are empty")
        official_error_code = extract_official_error_code(metadata)
        duration = extract_official_duration(metadata, measured)
    except _EntrypointError:
        raise
    except Exception:
        # Official semantics: any harness exception is a TestRunnerError (-5),
        # i.e. infrastructure, never candidate evidence.
        official_error_code = _OFFICIAL_TEST_RUNNER_ERROR
        sanitized = [_OFFICIAL_WRONG_ANSWER]
        duration = time.monotonic() - started

    return build_result(
        question_id=request["question_id"],
        candidate_sha256=request["candidate_sha256"],
        infrastructure_status="ok",
        error_type=None,
        compile_ok=True,
        official_test_results=sanitized,
        official_error_code=official_error_code,
        public_test_count=request["public_test_count"],
        duration_seconds=duration,
    )


def main() -> int:
    request: dict[str, Any] | None = None
    try:
        request = load_request()
        code = load_candidate(CANDIDATE_PATH, expected_sha256=request["candidate_sha256"])
        result = _evaluate(request, code)
    except _EntrypointError:
        identity = request if isinstance(request, dict) else {}
        question_id = identity.get("question_id")
        candidate_sha256 = identity.get("candidate_sha256")
        public_test_count = identity.get("public_test_count")
        if not isinstance(question_id, str) or not isinstance(candidate_sha256, str):
            return 3
        result = build_result(
            question_id=question_id,
            candidate_sha256=candidate_sha256,
            infrastructure_status="error",
            error_type="executor_error",
            compile_ok=False,
            official_test_results=[],
            official_error_code=None,
            public_test_count=public_test_count if isinstance(public_test_count, int) else 0,
            duration_seconds=None,
        )
    except Exception:
        return 3
    try:
        _write_result(result)
    except OSError:
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
