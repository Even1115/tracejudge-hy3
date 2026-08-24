"""Disclosure-safe entrypoint copied into the pinned EvalPlus container.

This file intentionally depends only on the Python standard library.  It is
executed *inside* the pinned official image, where it imports EvalPlus only
after stdout/stderr have been redirected away from normal infrastructure logs.
No test input, expected value, canonical solution, or candidate source is ever
printed by this process.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

EVALPLUS_VERSION = "0.3.1"
EVALPLUS_COMMIT = "e5d0ed0bab96280b60b637ec7f15b5e4841b0cb2"
HUMANEVAL_PLUS_VERSION = "v0.1.10"
EXPECTED_DATASET_TASK_COUNT = 164
EXPECTED_PREFLIGHT_TASK_COUNT = 10

_REQUEST_SCHEMA_VERSION = 1
_MAX_REQUEST_BYTES = 64 * 1024
_MAX_SAMPLE_BYTES = 2 * 1024 * 1024
_MAX_NATIVE_DATASET_BYTES = 512 * 1024 * 1024
_MAX_RESULT_BYTES = 128 * 1024 * 1024
_TASK_ID_RE = re.compile(r"^HumanEval/(?:0|[1-9][0-9]*)$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MD5_RE = re.compile(r"^[0-9a-f]{32}$")
_SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"
_OFFICIAL_STATUSES = {"pass", "fail", "timeout"}
_INTERNAL_EVALUATE_MODE = "__tracejudge_guarded_evalplus__"
_ISOLATION_STRATEGY = "official_parent_nofollow_forced_overwrite_v1"


class _EntrypointError(RuntimeError):
    """Internal exception carrying only an allowlisted, non-sensitive code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _DuplicateKey(ValueError):
    pass


class _SilentArgumentParser(argparse.ArgumentParser):
    """Convert CLI mistakes to a constant error without echoing arguments."""

    def error(self, _message: str) -> None:
        raise _EntrypointError("invalid_request")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


def _regular_file(path: Path, *, max_bytes: int, error_code: str) -> bytes:
    try:
        metadata = path.lstat()
    except OSError:
        raise _EntrypointError(error_code) from None
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
        raise _EntrypointError(error_code)
    try:
        payload = path.read_bytes()
    except OSError:
        raise _EntrypointError(error_code) from None
    if len(payload) > max_bytes:
        raise _EntrypointError(error_code)
    return payload


def _load_json(path: Path, *, max_bytes: int, error_code: str) -> Any:
    raw = _regular_file(path, max_bytes=max_bytes, error_code=error_code)
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKey):
        raise _EntrypointError(error_code) from None


def _task_identity(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {
        "task_id",
        "prompt_sha256",
        "entry_point",
    }:
        raise _EntrypointError("invalid_request")
    task_id = value.get("task_id")
    prompt_sha256 = value.get("prompt_sha256")
    entry_point = value.get("entry_point")
    if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
        raise _EntrypointError("invalid_request")
    if not isinstance(prompt_sha256, str) or not _SHA256_RE.fullmatch(prompt_sha256):
        raise _EntrypointError("invalid_request")
    if (
        not isinstance(entry_point, str)
        or not entry_point.isascii()
        or not entry_point.isidentifier()
    ):
        raise _EntrypointError("invalid_request")
    return {
        "task_id": task_id,
        "prompt_sha256": prompt_sha256,
        "entry_point": entry_point,
    }


def _request(path: Path, *, mode: str) -> tuple[dict[str, str], ...]:
    payload = _load_json(path, max_bytes=_MAX_REQUEST_BYTES, error_code="invalid_request")
    if not isinstance(payload, dict) or payload.get("schema_version") != _REQUEST_SCHEMA_VERSION:
        raise _EntrypointError("invalid_request")
    if mode == "inspect":
        if set(payload) != {"schema_version", "tasks"}:
            raise _EntrypointError("invalid_request")
        tasks_value = payload.get("tasks")
        if not isinstance(tasks_value, list) or len(tasks_value) != EXPECTED_PREFLIGHT_TASK_COUNT:
            raise _EntrypointError("invalid_request")
        tasks = tuple(_task_identity(item) for item in tasks_value)
    else:
        if set(payload) != {"schema_version", "task"}:
            raise _EntrypointError("invalid_request")
        tasks = (_task_identity(payload.get("task")),)
    if len({item["task_id"] for item in tasks}) != len(tasks):
        raise _EntrypointError("invalid_request")
    return tasks


@contextlib.contextmanager
def _silence_output():
    """Prevent EvalPlus/data-loader output from reaching ordinary logs."""

    try:
        with open(os.devnull, "w", encoding="utf-8") as sink:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                yield
    except OSError:
        raise _EntrypointError("runtime_unavailable") from None


def _canonical_dataset_sha256(problems: dict[str, dict[str, Any]]) -> str:
    """Fingerprint the loaded native corpus without revealing any corpus value."""

    try:
        encoder = json.JSONEncoder(
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256()
        for chunk in encoder.iterencode(problems):
            digest.update(chunk.encode("utf-8"))
    except (TypeError, ValueError, UnicodeEncodeError):
        raise _EntrypointError("dataset_identity_mismatch") from None
    return digest.hexdigest()


def _official_file_sha256(path_value: Any) -> tuple[str, int]:
    """Hash the exact ready release file without exposing its path or bytes."""

    if not isinstance(path_value, str) or not path_value:
        raise _EntrypointError("dataset_identity_mismatch")
    path = Path(path_value)
    try:
        metadata = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > _MAX_NATIVE_DATASET_BYTES
        ):
            raise _EntrypointError("dataset_identity_mismatch")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except _EntrypointError:
        raise
    except OSError:
        raise _EntrypointError("dataset_identity_mismatch") from None
    return digest.hexdigest(), metadata.st_size


def _load_official_dataset() -> tuple[dict[str, dict[str, Any]], str, str, int, str]:
    """Load the image-preinstalled native dataset without emitting its values."""

    try:
        with _silence_output():
            data_module = importlib.import_module("evalplus.data")
            humaneval_module = importlib.import_module("evalplus.data.humaneval")
            version = importlib.metadata.version("evalplus")
            dataset_version = getattr(humaneval_module, "HUMANEVAL_PLUS_VERSION", None)
            problems = data_module.get_human_eval_plus()
            dataset_hash = data_module.get_human_eval_plus_hash()
            ready_path = getattr(humaneval_module, "_ready_human_eval_plus_path", None)
            if not callable(ready_path):
                raise _EntrypointError("runtime_identity_mismatch")
            dataset_file_sha256, dataset_file_size = _official_file_sha256(ready_path())
    except _EntrypointError:
        raise
    except Exception:
        raise _EntrypointError("runtime_unavailable") from None

    if version != EVALPLUS_VERSION or dataset_version != HUMANEVAL_PLUS_VERSION:
        raise _EntrypointError("runtime_identity_mismatch")
    expected_ids = {f"HumanEval/{index}" for index in range(EXPECTED_DATASET_TASK_COUNT)}
    if (
        not isinstance(problems, dict)
        or len(problems) != EXPECTED_DATASET_TASK_COUNT
        or set(problems) != expected_ids
        or not isinstance(dataset_hash, str)
        or not re.fullmatch(r"[0-9a-f]{32}", dataset_hash)
    ):
        raise _EntrypointError("dataset_identity_mismatch")
    return (
        problems,
        dataset_hash,
        dataset_file_sha256,
        dataset_file_size,
        _canonical_dataset_sha256(problems),
    )


def _verify_tasks(
    problems: dict[str, dict[str, Any]],
    tasks: tuple[dict[str, str], ...],
) -> None:
    for expected in tasks:
        problem = problems.get(expected["task_id"])
        if not isinstance(problem, dict):
            raise _EntrypointError("task_identity_mismatch")
        prompt = problem.get("prompt")
        entry_point = problem.get("entry_point")
        if not isinstance(prompt, str) or not isinstance(entry_point, str):
            raise _EntrypointError("task_identity_mismatch")
        prompt_digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if prompt_digest != expected["prompt_sha256"] or entry_point != expected["entry_point"]:
            raise _EntrypointError("task_identity_mismatch")


def _safe_runtime_metadata(
    official_dataset_hash: str,
    dataset_file_sha256: str,
    dataset_file_size: int,
    dataset_canonical_sha256: str,
    *,
    verified_task_count: int,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "mode": "inspect",
        "status": "ok",
        "evalplus_version": EVALPLUS_VERSION,
        "evalplus_commit": EVALPLUS_COMMIT,
        "humaneval_plus_version": HUMANEVAL_PLUS_VERSION,
        # This is the MD5 of the complete pinned HumanEval+ release.  It is
        # intentionally distinct from the per-task override hash emitted by
        # ``run`` below.
        "official_dataset_hash": official_dataset_hash,
        "official_dataset_file_sha256": dataset_file_sha256,
        "official_dataset_file_sha256_basis": ("exact_bytes_from_pinned_release_ready_path"),
        "official_dataset_file_size_bytes": dataset_file_size,
        "native_dataset_canonical_sha256": dataset_canonical_sha256,
        "native_dataset_sha256_basis": (
            "loaded_native_corpus_canonical_json_utf8_sort_keys_compact"
        ),
        "dataset_task_count": EXPECTED_DATASET_TASK_COUNT,
        "verified_task_count": verified_task_count,
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform_system": platform.system(),
        "platform_machine": platform.machine(),
        "output_integrity_strategy": _ISOLATION_STRATEGY,
        "official_parent_forced_overwrite": True,
        "official_parent_nofollow": True,
        "reliability_guard_security_sandbox": False,
    }


def _inspect(request_path: Path) -> dict[str, Any]:
    tasks = _request(request_path, mode="inspect")
    (
        problems,
        official_dataset_hash,
        dataset_file_sha256,
        dataset_file_size,
        dataset_canonical_sha256,
    ) = _load_official_dataset()
    _verify_tasks(problems, tasks)
    return _safe_runtime_metadata(
        official_dataset_hash,
        dataset_file_sha256,
        dataset_file_size,
        dataset_canonical_sha256,
        verified_task_count=len(tasks),
    )


class _PinnedResultPathProxy:
    """Delegate every path operation except the official result existence check."""

    def __init__(self, target: Path) -> None:
        self._target = target

    def __getattr__(self, name: str) -> Any:
        return getattr(os.path, name)

    def isfile(self, value: Any) -> bool:
        try:
            candidate = Path(os.path.abspath(os.fspath(value)))
        except (OSError, TypeError, ValueError):
            return os.path.isfile(value)
        if candidate == self._target:
            # EvalPlus 0.3.1 checks twice and otherwise trusts a pre-existing
            # document.  Force the official parent down its final write path.
            return False
        return os.path.isfile(value)


class _PinnedResultOsProxy:
    def __init__(self, target: Path) -> None:
        self.path = _PinnedResultPathProxy(target)

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)


def _official_parent_open(
    target: Path,
    state: dict[str, int],
    directory_descriptor: int,
    value: Any,
    mode: str = "r",
    buffering: int = -1,
    encoding: str | None = None,
    errors: str | None = None,
    newline: str | None = None,
    closefd: bool = True,
    opener: Any = None,
) -> Any:
    """Force the exact official parent write through no-follow/truncate flags."""

    try:
        candidate = Path(os.path.abspath(os.fspath(value)))
    except (OSError, TypeError, ValueError):
        candidate = None
    if candidate != target:
        return open(
            value,
            mode,
            buffering=buffering,
            encoding=encoding,
            errors=errors,
            newline=newline,
            closefd=closefd,
            opener=opener,
        )
    if mode != "w" or not closefd or opener is not None or state.get("write_count", 0):
        raise _EntrypointError("result_integrity_failed")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(nofollow, int):
        raise _EntrypointError("result_integrity_failed")
    try:
        try:
            before = os.stat(
                target.name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            before = None
        if before is not None and not stat.S_ISREG(before.st_mode):
            raise _EntrypointError("result_integrity_failed")
        descriptor = os.open(
            target.name,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NONBLOCK | nofollow,
            0o600,
            dir_fd=directory_descriptor,
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            os.close(descriptor)
            raise _EntrypointError("result_integrity_failed")
        os.fchmod(descriptor, 0o600)
        state["write_count"] = 1
        state["inode"] = opened.st_ino
        state["device"] = opened.st_dev
        state["opened_ctime_ns"] = opened.st_ctime_ns
        return os.fdopen(
            descriptor,
            mode,
            buffering=buffering,
            encoding=encoding,
            errors=errors,
            newline=newline,
            closefd=True,
        )
    except _EntrypointError:
        raise
    except OSError:
        raise _EntrypointError("result_integrity_failed") from None


def _guarded_official_evaluate(
    sample_path: Path,
    private_root: Path,
    _control_root: Path,
    _output_root: Path,
) -> int:
    """Run pinned EvalPlus with a forced, no-follow official-parent result write.

    EvalPlus' own ``reliability_guard`` explicitly is not a security sandbox.
    This narrow patch prevents candidate-created result documents from being
    loaded or preserved as official output; all other evaluation logic remains
    the pinned official implementation.
    """

    target = Path(os.path.abspath(private_root / "sample_eval_results.json"))
    if sample_path.parent != private_root or target.exists() or target.is_symlink():
        raise _EntrypointError("result_integrity_failed")
    state: dict[str, int] = {}
    directory_descriptor = -1
    try:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
        directory_descriptor = os.open(private_root, directory_flags)
        protected_directory = os.fstat(directory_descriptor)
        if not stat.S_ISDIR(protected_directory.st_mode):
            raise _EntrypointError("result_integrity_failed")
        evaluator_module = importlib.import_module("evalplus.evaluate")
        official_evaluate = getattr(evaluator_module, "evaluate", None)
        if not callable(official_evaluate):
            raise _EntrypointError("runtime_identity_mismatch")
        evaluator_globals = getattr(official_evaluate, "__globals__", None)
        if not isinstance(evaluator_globals, dict) or evaluator_globals is not vars(
            evaluator_module
        ):
            raise _EntrypointError("runtime_identity_mismatch")
        evaluator_globals["os"] = _PinnedResultOsProxy(target)
        evaluator_globals["open"] = lambda *args, **kwargs: _official_parent_open(
            target,
            state,
            directory_descriptor,
            *args,
            **kwargs,
        )
        official_evaluate(
            dataset="humaneval",
            samples=str(sample_path),
            parallel=1,
            min_time_limit=4.0,
            gt_time_limit_factor=4.0,
            test_details=True,
        )
        final = os.stat(
            target.name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        final_directory = private_root.lstat()
        if (
            state.get("write_count") != 1
            or target.is_symlink()
            or not stat.S_ISREG(final.st_mode)
            or final.st_ino != state.get("inode")
            or final.st_dev != state.get("device")
            or final_directory.st_ino != protected_directory.st_ino
            or final_directory.st_dev != protected_directory.st_dev
            or final.st_ctime_ns < state.get("opened_ctime_ns", 0)
            or final.st_size <= 0
            or final.st_size > _MAX_RESULT_BYTES
        ):
            raise _EntrypointError("result_integrity_failed")
    except _EntrypointError:
        raise
    except Exception:
        raise _EntrypointError("executor_failed") from None
    finally:
        if directory_descriptor >= 0:
            try:
                os.close(directory_descriptor)
            except OSError:
                pass
    return 0


def _load_sample(path: Path, *, expected_task_id: str) -> tuple[bytes, str]:
    raw = _regular_file(path, max_bytes=_MAX_SAMPLE_BYTES, error_code="sample_invalid")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _EntrypointError("sample_invalid") from None
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise _EntrypointError("sample_invalid")
    try:
        sample = json.loads(lines[0], object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, _DuplicateKey):
        raise _EntrypointError("sample_invalid") from None
    if not isinstance(sample, dict) or set(sample) != {"task_id", "solution"}:
        raise _EntrypointError("sample_invalid")
    solution = sample.get("solution")
    if (
        sample.get("task_id") != expected_task_id
        or not isinstance(solution, str)
        or not solution.strip()
    ):
        raise _EntrypointError("sample_invalid")
    try:
        solution_sha256 = hashlib.sha256(solution.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        raise _EntrypointError("sample_invalid") from None
    return raw, solution_sha256


def _write_private_bytes(path: Path, payload: bytes, *, error_code: str) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise _EntrypointError(error_code) from None


def _write_private_jsonl(path: Path, record: dict[str, Any]) -> str:
    try:
        payload = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
    except (TypeError, ValueError):
        raise _EntrypointError("executor_setup_failed") from None
    _write_private_bytes(path, payload, error_code="executor_setup_failed")
    return hashlib.md5(payload, usedforsecurity=False).hexdigest()


def _reject_json_constant(_value: str) -> None:
    raise ValueError


def _validate_official_raw_result(
    path: Path,
    *,
    expected_task_id: str,
    expected_solution_sha256: str,
    official_override_hash: str,
) -> None:
    """Validate only raw identity/shape; never emit or retain failed-test values."""

    raw = _regular_file(path, max_bytes=_MAX_RESULT_BYTES, error_code="result_invalid")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateKey,
        ValueError,
    ):
        raise _EntrypointError("result_invalid") from None
    if not isinstance(payload, dict) or set(payload) != {"date", "hash", "eval"}:
        raise _EntrypointError("result_invalid")
    if (
        not isinstance(payload.get("date"), str)
        or not payload["date"].strip()
        or payload.get("hash") != official_override_hash
        or not _MD5_RE.fullmatch(official_override_hash)
    ):
        raise _EntrypointError("result_identity_mismatch")
    evaluations = payload.get("eval")
    if not isinstance(evaluations, dict) or set(evaluations) != {expected_task_id}:
        raise _EntrypointError("result_identity_mismatch")
    candidates = evaluations.get(expected_task_id)
    if (
        not isinstance(candidates, list)
        or len(candidates) != 1
        or not isinstance(candidates[0], dict)
    ):
        raise _EntrypointError("result_invalid")
    candidate = candidates[0]
    if set(candidate) != {
        "task_id",
        "solution",
        "base_status",
        "plus_status",
        "base_fail_tests",
        "plus_fail_tests",
    }:
        raise _EntrypointError("result_invalid")
    solution = candidate.get("solution")
    try:
        actual_solution_sha256 = (
            hashlib.sha256(solution.encode("utf-8")).hexdigest()
            if isinstance(solution, str)
            else None
        )
    except UnicodeEncodeError:
        raise _EntrypointError("result_invalid") from None
    if (
        candidate.get("task_id") != expected_task_id
        or actual_solution_sha256 != expected_solution_sha256
    ):
        raise _EntrypointError("result_identity_mismatch")
    if (
        candidate.get("base_status") not in _OFFICIAL_STATUSES
        or candidate.get("plus_status") not in _OFFICIAL_STATUSES
        or not isinstance(candidate.get("base_fail_tests"), list)
        or not isinstance(candidate.get("plus_fail_tests"), list)
    ):
        raise _EntrypointError("result_invalid")


def _precreated_output_identity(
    path: Path,
    *,
    expected_name: str,
    error_code: str,
) -> tuple[int, int]:
    """Validate one initially-empty host file mounted at one fixed output path."""

    nofollow = getattr(os, "O_NOFOLLOW", None)
    if path.name != expected_name or not isinstance(nofollow, int):
        raise _EntrypointError(error_code)
    try:
        parent_metadata = path.parent.lstat()
        metadata = path.lstat()
    except OSError:
        raise _EntrypointError(error_code) from None
    if (
        path.parent.is_symlink()
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size != 0
        or stat.S_IMODE(metadata.st_mode) not in {0o622, 0o666}
    ):
        raise _EntrypointError(error_code)
    return metadata.st_dev, metadata.st_ino


def _open_precreated_output(
    path: Path,
    *,
    expected_identity: tuple[int, int],
    error_code: str,
) -> tuple[int, int]:
    """Open the exact precreated bind target for a forced no-follow overwrite."""

    directory_descriptor = -1
    try:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory_descriptor = os.open(path.parent, directory_flags)
        descriptor = os.open(
            path.name,
            os.O_WRONLY | os.O_TRUNC | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW,
            dir_fd=directory_descriptor,
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != expected_identity:
            os.close(descriptor)
            raise _EntrypointError(error_code)
        return descriptor, directory_descriptor
    except _EntrypointError:
        if directory_descriptor >= 0:
            os.close(directory_descriptor)
        raise
    except OSError:
        if directory_descriptor >= 0:
            try:
                os.close(directory_descriptor)
            except OSError:
                pass
        raise _EntrypointError(error_code) from None


def _verify_precreated_output(
    path: Path,
    *,
    expected_identity: tuple[int, int],
    expected_size: int,
    expected_digest: bytes,
    max_bytes: int,
    error_code: str,
) -> None:
    try:
        metadata = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != expected_identity
            or metadata.st_size != expected_size
            or not 0 < metadata.st_size <= max_bytes
        ):
            raise OSError
        digest = hashlib.sha256()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != expected_identity:
                raise OSError
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
        finally:
            os.close(descriptor)
        if digest.digest() != expected_digest:
            raise OSError
    except OSError:
        raise _EntrypointError(error_code) from None


def _overwrite_precreated_bytes(
    path: Path,
    payload: bytes,
    *,
    expected_identity: tuple[int, int],
    max_bytes: int,
    error_code: str,
) -> None:
    if not 0 < len(payload) <= max_bytes:
        raise _EntrypointError(error_code)
    descriptor, directory_descriptor = _open_precreated_output(
        path,
        expected_identity=expected_identity,
        error_code=error_code,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError
            view = view[written:]
        os.fsync(descriptor)
        final = os.fstat(descriptor)
        if (final.st_dev, final.st_ino) != expected_identity or final.st_size != len(payload):
            raise OSError
    except OSError:
        raise _EntrypointError(error_code) from None
    finally:
        os.close(descriptor)
        os.close(directory_descriptor)
    _verify_precreated_output(
        path,
        expected_identity=expected_identity,
        expected_size=len(payload),
        expected_digest=hashlib.sha256(payload).digest(),
        max_bytes=max_bytes,
        error_code=error_code,
    )


def _publish_private_result(
    source: Path,
    destination: Path,
    *,
    expected_identity: tuple[int, int],
) -> None:
    """Force-overwrite the exact precreated raw-result file bind."""

    try:
        source_metadata = source.lstat()
    except OSError:
        raise _EntrypointError("result_missing") from None
    if (
        source.is_symlink()
        or not stat.S_ISREG(source_metadata.st_mode)
        or source_metadata.st_size <= 0
        or source_metadata.st_size > _MAX_RESULT_BYTES
    ):
        raise _EntrypointError("result_missing")

    descriptor, directory_descriptor = _open_precreated_output(
        destination,
        expected_identity=expected_identity,
        error_code="result_publish_failed",
    )
    source_digest = hashlib.sha256()
    copied = 0
    try:
        with source.open("rb") as input_stream:
            while chunk := input_stream.read(1024 * 1024):
                copied += len(chunk)
                if copied > _MAX_RESULT_BYTES:
                    raise OSError
                source_digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError
                    view = view[written:]
        os.fsync(descriptor)
        final = os.fstat(descriptor)
        if (
            copied != source_metadata.st_size
            or (final.st_dev, final.st_ino) != expected_identity
            or final.st_size != copied
        ):
            raise OSError
    except OSError:
        raise _EntrypointError("result_publish_failed") from None
    finally:
        os.close(descriptor)
        os.close(directory_descriptor)
    _verify_precreated_output(
        destination,
        expected_identity=expected_identity,
        expected_size=copied,
        expected_digest=source_digest.digest(),
        max_bytes=_MAX_RESULT_BYTES,
        error_code="result_publish_failed",
    )


def _run(
    request_path: Path,
    sample_path: Path,
    result_path: Path,
    *,
    result_identity: tuple[int, int],
) -> dict[str, Any]:
    tasks = _request(request_path, mode="run")
    expected = tasks[0]
    sample_payload, solution_sha256 = _load_sample(
        sample_path,
        expected_task_id=expected["task_id"],
    )
    (
        problems,
        _official_dataset_hash,
        _dataset_file_sha256,
        _dataset_file_size,
        _dataset_canonical_sha256,
    ) = _load_official_dataset()
    _verify_tasks(problems, tasks)
    problem = problems[expected["task_id"]]

    try:
        with tempfile.TemporaryDirectory(prefix="tracejudge_evalplus_", dir="/tmp") as tmp:
            tmp_path = Path(tmp)
            dataset_path = tmp_path / "HumanEvalPlus.jsonl"
            private_sample_path = tmp_path / "sample.jsonl"
            private_result_path = tmp_path / "sample_eval_results.json"
            cache_path = tmp_path / "cache"
            home_path = tmp_path / "home"
            cache_path.mkdir(mode=0o700)
            home_path.mkdir(mode=0o700)
            official_override_hash = _write_private_jsonl(dataset_path, problem)
            # EvalPlus and candidate code see only this exact tmpfs copy.  The
            # bind-mounted control input remains read-only for the whole run.
            _write_private_bytes(
                private_sample_path,
                sample_payload,
                error_code="executor_setup_failed",
            )

            # Use a fresh Python process so EvalPlus calculates CACHE_DIR after
            # HOME/XDG_CACHE_HOME are redirected to this container-only tmpfs.
            os.environ["HOME"] = str(home_path)
            os.environ["XDG_CACHE_HOME"] = str(cache_path)
            child_env = {
                "HOME": str(home_path),
                "XDG_CACHE_HOME": str(cache_path),
                "HUMANEVAL_OVERRIDE_PATH": str(dataset_path),
                "LANG": "C.UTF-8",
                "PATH": _SAFE_PATH,
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            command = [
                sys.executable,
                "-B",
                "-u",
                str(Path(__file__).resolve()),
                _INTERNAL_EVALUATE_MODE,
                str(private_sample_path),
                str(tmp_path),
                str(request_path.parent),
                str(result_path.parent),
            ]
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=tmp_path,
                env=child_env,
                check=False,
            )
            if completed.returncode == 73:
                raise _EntrypointError("candidate_isolation_unavailable")
            if completed.returncode != 0:
                raise _EntrypointError("executor_failed")
            _validate_official_raw_result(
                private_result_path,
                expected_task_id=expected["task_id"],
                expected_solution_sha256=solution_sha256,
                official_override_hash=official_override_hash,
            )
            _publish_private_result(
                private_result_path,
                result_path,
                expected_identity=result_identity,
            )
    except _EntrypointError:
        raise
    except Exception:
        raise _EntrypointError("executor_failed") from None

    return {
        "schema_version": 1,
        "mode": "run",
        "status": "ok",
        "task_id": expected["task_id"],
        "official_override_hash": official_override_hash,
        "result_available": True,
    }


def _emit(payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    sys.stdout.write(encoded + "\n")
    sys.stdout.flush()


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = _SilentArgumentParser(add_help=False)
    parser.add_argument("mode", choices=("inspect", "run"))
    parser.add_argument("request")
    parser.add_argument("sample", nargs="?")
    parser.add_argument("result", nargs="?")
    parser.add_argument("control", nargs="?")
    return parser.parse_args(argv)


def _publish_control(
    path: Path,
    payload: dict[str, Any],
    *,
    expected_identity: tuple[int, int],
) -> None:
    try:
        encoded = (
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise _EntrypointError("control_publish_failed") from None
    if len(encoded) > _MAX_REQUEST_BYTES:
        raise _EntrypointError("control_publish_failed")
    _overwrite_precreated_bytes(
        path,
        encoded,
        expected_identity=expected_identity,
        max_bytes=_MAX_REQUEST_BYTES,
        error_code="control_publish_failed",
    )


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv[:1] == [_INTERNAL_EVALUATE_MODE]:
        if len(raw_argv) != 5:
            return 73
        try:
            return _guarded_official_evaluate(*(Path(value) for value in raw_argv[1:]))
        except BaseException:
            return 73
    args: argparse.Namespace | None = None
    control_identity: tuple[int, int] | None = None
    try:
        args = _arguments(raw_argv)
        request_path = Path(args.request)
        if args.mode == "inspect":
            if args.sample is not None or args.result is not None or args.control is not None:
                raise _EntrypointError("invalid_request")
            payload = _inspect(request_path)
        else:
            if args.sample is None or args.result is None:
                raise _EntrypointError("invalid_request")
            if args.control is not None:
                control_identity = _precreated_output_identity(
                    Path(args.control),
                    expected_name="control.json",
                    error_code="control_publish_failed",
                )
            result_path = Path(args.result)
            result_identity = _precreated_output_identity(
                result_path,
                expected_name="sample_eval_results.json",
                error_code="output_invalid",
            )
            payload = _run(
                request_path,
                Path(args.sample),
                result_path,
                result_identity=result_identity,
            )
        exit_code = 0
    except _EntrypointError as exc:
        payload = {
            "schema_version": 1,
            "status": "error",
            "error_type": exc.code,
        }
        exit_code = 2
    except (Exception, SystemExit):
        payload = {
            "schema_version": 1,
            "status": "error",
            "error_type": "internal_error",
        }
        exit_code = 2

    if args is not None and args.mode == "run" and args.control is not None:
        if control_identity is None:
            return 2
        try:
            _publish_control(
                Path(args.control),
                payload,
                expected_identity=control_identity,
            )
        except _EntrypointError:
            return 2
        return exit_code
    _emit(payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
