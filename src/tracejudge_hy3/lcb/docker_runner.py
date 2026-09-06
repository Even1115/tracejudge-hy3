"""Host-side hardened Docker runner for LiveCodeBench stdio evaluation.

Mirrors the ``evalplus.docker_runner`` isolation invariants: a read-only
control bind (request, candidate, entrypoint), exactly one pre-created
host-owned output file bind, no network, a read-only root filesystem, all
capabilities dropped, resource limits, bounded subprocesses, and guaranteed
container cleanup.  The host never executes, imports, or compiles candidate
code and never decodes hidden tests; it only stages opaque payloads and reads
back the strict 11-field raw-result mapping produced by the container-side
entrypoint and the pinned official checker.

The runner returns raw-result mappings for the private boundary.  Turn them
into frozen contract results with
``LiveCodeBenchBenchmarkAdapter.normalize_execution_result``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from tracejudge_hy3.benchmark.contracts import BenchmarkCandidate, BenchmarkTask
from tracejudge_hy3.benchmark.livecodebench import (
    LCB_CHECKER_COMMIT,
    LCB_EXECUTOR_ID,
)

DEFAULT_LCB_PLATFORM = "linux/amd64"
LCB_IMAGE_REPOSITORY = "tracejudge-lcb-checker"
DEFAULT_PER_TEST_TIMEOUT_SECONDS = 6

# Drift-guarded duplicate of the adapter's raw-result schema; the test suite
# asserts equality with benchmark.livecodebench._RESULT_FIELDS.
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

_RAW_RESULT_SIZE_LIMIT = 1024 * 1024
_CONTROL_OUTPUT_LIMIT = 1024 * 1024
_MAX_CANDIDATE_BYTES = 1024 * 1024
_IMAGE_RE = re.compile(rf"^{re.escape(LCB_IMAGE_REPOSITORY)}@sha256:[0-9a-f]{{64}}$")
_LOCAL_IMAGE_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
Clock = Callable[[], float]
UuidFactory = Callable[[], uuid.UUID]


class LCBRunnerError(RuntimeError):
    """Infrastructure failure with an allowlisted error type."""

    def __init__(self, error_type: str, message: str, *, cleanup_status: str = "not_attempted"):
        super().__init__(message)
        self.error_type = error_type
        self.cleanup_status = cleanup_status


class _OutputLimitExceeded(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DockerLimits:
    memory: str = "4g"
    cpus: str = "1"
    pids: int = 128
    tmpfs_size: str = "1g"
    inspect_timeout_seconds: float = 30.0
    per_task_timeout_seconds: float = 600.0
    cleanup_timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not self.memory or not self.cpus or not self.tmpfs_size:
            raise ValueError("Docker resource limits must be non-empty")
        if self.pids <= 0:
            raise ValueError("Docker pids limit must be positive")
        for value in (
            self.inspect_timeout_seconds,
            self.per_task_timeout_seconds,
            self.cleanup_timeout_seconds,
        ):
            if value <= 0:
                raise ValueError("Docker timeouts must be positive")


class LCBDockerRunner:
    """Run one candidate per hardened, explicitly ``linux/amd64`` container."""

    mode: Literal["docker"] = "docker"

    def __init__(
        self,
        *,
        image: str,
        requested_platform: str = DEFAULT_LCB_PLATFORM,
        limits: DockerLimits | None = None,
        command_runner: CommandRunner = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
        clock: Clock = time.monotonic,
        uuid_factory: UuidFactory = uuid.uuid4,
    ) -> None:
        if not (_IMAGE_RE.fullmatch(image) or _LOCAL_IMAGE_RE.fullmatch(image)):
            raise ValueError("only a pinned checker repository digest or local image ID is allowed")
        if requested_platform != DEFAULT_LCB_PLATFORM:
            raise ValueError("only the pinned linux/amd64 platform is allowed")
        self.image = image
        self.requested_platform = requested_platform
        self.limits = limits or DockerLimits()
        self._command_runner = command_runner
        self._which = which
        self._clock = clock
        self._uuid_factory = uuid_factory
        self._activity_lock = threading.Lock()
        self._active_containers: set[str] = set()
        self._cancel_event = threading.Event()

    # ------------------------------------------------------------------
    # Availability and image identity
    # ------------------------------------------------------------------
    def is_available(self) -> tuple[bool, str | None]:
        if self._which("docker") is None:
            return False, "docker CLI not found"
        try:
            completed = self._invoke(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                timeout=self.limits.inspect_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired, _OutputLimitExceeded):
            return False, "docker daemon unavailable"
        if completed.returncode != 0 or not completed.stdout.strip():
            return False, "docker daemon unavailable"
        return True, None

    def verify_image_identity(self) -> None:
        """Fail closed unless the local image digest matches the pin exactly."""

        try:
            completed = self._invoke(
                [
                    "docker",
                    "image",
                    "inspect",
                    self.image,
                    "--format",
                    "{{.Id}} {{json .RepoDigests}}",
                ],
                timeout=self.limits.inspect_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired, _OutputLimitExceeded):
            raise LCBRunnerError(
                "docker_unavailable", "could not inspect the pinned image"
            ) from None
        if completed.returncode != 0:
            raise LCBRunnerError("image_mismatch", "the pinned image is not present locally")
        identity, _, digests_text = completed.stdout.strip().partition(" ")
        try:
            digests = json.loads(digests_text) if digests_text else []
        except json.JSONDecodeError:
            raise LCBRunnerError("image_mismatch", "image inspect output is invalid") from None
        if digests is None and _LOCAL_IMAGE_RE.fullmatch(self.image):
            digests = []
        if not isinstance(digests, list):
            raise LCBRunnerError("image_mismatch", "image inspect output is invalid")
        # Registry-pulled images are pinned via RepoDigests; locally built
        # images have no RepoDigests and are pinned via the image ID (the
        # content hash of the local build, recorded at build time).
        matches = (
            identity == self.image
            if _LOCAL_IMAGE_RE.fullmatch(self.image)
            else self.image in digests
        )
        if not matches:
            raise LCBRunnerError(
                "image_mismatch", "local image digest does not match the pinned image"
            )

    # ------------------------------------------------------------------
    # Public execution API
    # ------------------------------------------------------------------
    def run_task(
        self,
        *,
        task: BenchmarkTask,
        candidate: BenchmarkCandidate,
        public_test_cases_raw: str,
        private_test_cases_raw: str,
        public_test_count: int,
        per_test_timeout_seconds: int = DEFAULT_PER_TEST_TIMEOUT_SECONDS,
        workspace: Path,
    ) -> Mapping[str, Any]:
        """Run one candidate in one container; return the private raw result.

        Infrastructure failures are returned as raw results with
        ``infrastructure_status="error"`` and an allowlisted ``error_type``;
        they are never silently merged into candidate outcomes.
        """

        started = self._clock()
        identity_context = {
            "question_id": task.identity.task_id,
            "candidate_sha256": candidate.code_sha256,
            "public_test_count": public_test_count,
        }
        try:
            self._validate_inputs(
                task=task,
                candidate=candidate,
                public_test_cases_raw=public_test_cases_raw,
                private_test_cases_raw=private_test_cases_raw,
                public_test_count=public_test_count,
                per_test_timeout_seconds=per_test_timeout_seconds,
            )
            workspace_path = Path(workspace).expanduser()
            if not workspace_path.is_dir() or workspace_path.is_symlink():
                raise LCBRunnerError("executor_error", "workspace must be a real directory")
            return self._run_container(
                task=task,
                candidate=candidate,
                public_test_cases_raw=public_test_cases_raw,
                private_test_cases_raw=private_test_cases_raw,
                public_test_count=public_test_count,
                per_test_timeout_seconds=per_test_timeout_seconds,
                workspace=workspace_path,
                started=started,
            )
        except LCBRunnerError as exc:
            return self._infrastructure_result(
                **identity_context,
                error_type=exc.error_type,
                started=started,
            )

    # ------------------------------------------------------------------
    # Input validation
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_inputs(
        *,
        task: BenchmarkTask,
        candidate: BenchmarkCandidate,
        public_test_cases_raw: str,
        private_test_cases_raw: str,
        public_test_count: int,
        per_test_timeout_seconds: int,
    ) -> None:
        if candidate.task != task.identity:
            raise LCBRunnerError("executor_error", "candidate task identity mismatch")
        expected_hash = hashlib.sha256(candidate.code.encode("utf-8")).hexdigest()
        if candidate.code_sha256 != expected_hash:
            raise LCBRunnerError("executor_error", "candidate hash does not bind the code")
        if len(candidate.code.encode("utf-8")) > _MAX_CANDIDATE_BYTES:
            raise LCBRunnerError("executor_error", "candidate exceeds the size limit")
        if not isinstance(public_test_cases_raw, str) or not public_test_cases_raw.strip():
            raise LCBRunnerError("executor_error", "public test payload must be a raw string")
        if not isinstance(private_test_cases_raw, str) or not private_test_cases_raw.strip():
            raise LCBRunnerError("executor_error", "private test payload must be a raw string")
        if (
            isinstance(public_test_count, bool)
            or not isinstance(public_test_count, int)
            or public_test_count < 0
        ):
            raise LCBRunnerError("executor_error", "public_test_count is invalid")
        if (
            isinstance(per_test_timeout_seconds, bool)
            or not isinstance(per_test_timeout_seconds, int)
            or not 1 <= per_test_timeout_seconds <= 120
        ):
            raise LCBRunnerError("executor_error", "per-test timeout is invalid")

    # ------------------------------------------------------------------
    # Container lifecycle
    # ------------------------------------------------------------------
    def _run_container(
        self,
        *,
        task: BenchmarkTask,
        candidate: BenchmarkCandidate,
        public_test_cases_raw: str,
        private_test_cases_raw: str,
        public_test_count: int,
        per_test_timeout_seconds: int,
        workspace: Path,
        started: float,
    ) -> Mapping[str, Any]:
        container_name = f"lcb-task-{self._uuid_factory().hex[:24]}"
        with tempfile.TemporaryDirectory(prefix=".lcb_container_", dir=workspace) as staging_value:
            staging = Path(staging_value)
            control = staging / "control"
            output = staging / "output"
            control.mkdir(mode=0o700)
            output.mkdir(mode=0o700)
            request = {
                "schema_version": 1,
                "question_id": task.identity.task_id,
                "candidate_sha256": candidate.code_sha256,
                "public_test_cases": public_test_cases_raw,
                "private_test_cases": private_test_cases_raw,
                "public_test_count": public_test_count,
                "per_test_timeout_seconds": per_test_timeout_seconds,
            }
            self._stage_control_files(control, request=request, candidate_code=candidate.code)
            staged_result = output / "result.json"
            self._stage_output_file(staged_result)
            command = self._container_command(control, container_name, staged_result)

            if not self._activate_container(container_name):
                raise LCBRunnerError("container_timeout", "cancelled before container start")
            try:
                try:
                    completed = self._invoke(
                        command,
                        timeout=min(
                            self.limits.inspect_timeout_seconds,
                            self.limits.per_task_timeout_seconds,
                        ),
                    )
                except subprocess.TimeoutExpired:
                    raise self._cleanup_error(
                        container_name, "container_timeout", "container create timed out"
                    ) from None
                except OSError:
                    raise self._cleanup_error(
                        container_name, "container_start_error", "could not start container"
                    ) from None
                except _OutputLimitExceeded:
                    raise self._cleanup_error(
                        container_name, "executor_error", "control output exceeded its limit"
                    ) from None
                if completed.returncode != 0:
                    raise self._cleanup_error(
                        container_name, "container_start_error", "container create failed"
                    )
                if self._cancel_event.is_set():
                    raise self._cleanup_error(
                        container_name, "container_timeout", "cancelled during container create"
                    )

                remaining = self.limits.per_task_timeout_seconds - (self._clock() - started)
                if remaining <= 0:
                    raise self._cleanup_error(
                        container_name, "container_timeout", "container exceeded its outer timeout"
                    )
                try:
                    waited = self._invoke(["docker", "wait", container_name], timeout=remaining)
                except subprocess.TimeoutExpired:
                    raise self._cleanup_error(
                        container_name, "container_timeout", "container exceeded its outer timeout"
                    ) from None
                except OSError:
                    raise self._cleanup_error(
                        container_name, "container_exit_error", "could not await container"
                    ) from None
                except _OutputLimitExceeded:
                    raise self._cleanup_error(
                        container_name, "executor_error", "control output exceeded its limit"
                    ) from None
                exit_code = self._bounded_text(waited.stdout).strip()
                if waited.returncode != 0 or exit_code != "0" or self._cancel_event.is_set():
                    error_type = (
                        "container_timeout"
                        if self._cancel_event.is_set()
                        else "container_exit_error"
                    )
                    raise self._cleanup_error(
                        container_name, error_type, "container exited unsuccessfully"
                    )
            finally:
                self._deactivate_container(container_name)

            cleanup_status = self._force_remove(container_name)
            raw_result = self._read_raw_result(staged_result)
            if raw_result is None:
                raise LCBRunnerError(
                    "invalid_raw_result",
                    "staged raw result is missing or unreadable",
                    cleanup_status=cleanup_status,
                )
            self._validate_raw_result(raw_result, task=task, candidate=candidate)
            return raw_result

    def _container_command(
        self, control: Path, container_name: str, staged_result: Path
    ) -> list[str]:
        control_source = str(control)
        result_source = str(staged_result)
        for source in (control_source, result_source):
            if any(character in source for character in (",", "\n", "\r")):
                raise LCBRunnerError("container_start_error", "workspace path is not mount-safe")
        return [
            "docker",
            "run",
            "-d",
            "--pull",
            "never",
            "--platform",
            self.requested_platform,
            "--name",
            container_name,
            "--network",
            "none",
            "--log-driver",
            "none",
            "--read-only",
            "--memory",
            self.limits.memory,
            "--memory-swap",
            self.limits.memory,
            "--cpus",
            self.limits.cpus,
            "--pids-limit",
            str(self.limits.pids),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--env",
            "HTTP_PROXY=",
            "--env",
            "HTTPS_PROXY=",
            "--env",
            "ALL_PROXY=",
            "--env",
            "NO_PROXY=",
            "--env",
            "http_proxy=",
            "--env",
            "https_proxy=",
            "--env",
            "all_proxy=",
            "--env",
            "no_proxy=",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={self.limits.tmpfs_size},mode=1777",
            "--mount",
            f"type=bind,src={control_source},dst=/control,ro",
            "--mount",
            f"type=bind,src={result_source},dst=/output/result.json",
            "--ulimit",
            f"fsize={_RAW_RESULT_SIZE_LIMIT}:{_RAW_RESULT_SIZE_LIMIT}",
            "--workdir",
            "/tmp",
            self.image,
            "python3",
            "-B",
            "-u",
            "/control/entrypoint.py",
        ]

    # ------------------------------------------------------------------
    # Staging
    # ------------------------------------------------------------------
    def _stage_control_files(
        self, control: Path, *, request: Mapping[str, Any], candidate_code: str
    ) -> None:
        source = Path(__file__).with_name("container_entrypoint.py")
        shutil.copyfile(source, control / "entrypoint.py")
        os.chmod(control / "entrypoint.py", 0o444)
        request_bytes = (
            json.dumps(request, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        self._exclusive_write(control / "request.json", request_bytes, mode=0o444)
        self._exclusive_write(control / "candidate.py", candidate_code.encode("utf-8"), mode=0o444)
        # Make the read-only bind source traversable/readable by the fixed
        # container user only after all trusted staging writes are complete.
        os.chmod(control, 0o555)

    def _stage_output_file(self, path: Path) -> None:
        """Create the single host-owned file exposed writable to the container."""

        if path.parent.is_symlink():
            raise LCBRunnerError("executor_error", "output staging is invalid")
        self._exclusive_write(path, b"", mode=0o666)
        # Apply the intended mode explicitly because process umask is
        # otherwise allowed to make a capability-less container unable to
        # write its exact file bind.
        os.chmod(path, 0o666)

    @staticmethod
    def _exclusive_write(path: Path, payload: bytes, *, mode: int) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())

    # ------------------------------------------------------------------
    # Raw result handling
    # ------------------------------------------------------------------
    def _read_raw_result(self, staged_result: Path) -> Mapping[str, Any] | None:
        try:
            metadata = staged_result.lstat()
            if staged_result.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                return None
            if metadata.st_size == 0 or metadata.st_size > _RAW_RESULT_SIZE_LIMIT:
                return None
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(staged_result, flags)
            with os.fdopen(descriptor, "rb") as stream:
                payload = stream.read(_RAW_RESULT_SIZE_LIMIT + 1)
            if len(payload) > _RAW_RESULT_SIZE_LIMIT:
                return None
            parsed = json.loads(payload)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, Mapping) else None

    @staticmethod
    def _validate_raw_result(
        raw_result: Mapping[str, Any], *, task: BenchmarkTask, candidate: BenchmarkCandidate
    ) -> None:
        if set(raw_result) != _RESULT_FIELDS:
            raise LCBRunnerError("invalid_raw_result", "raw result schema drifted")
        if raw_result["question_id"] != task.identity.task_id:
            raise LCBRunnerError("invalid_raw_result", "raw result task identity mismatch")
        if raw_result["candidate_sha256"] != candidate.code_sha256:
            raise LCBRunnerError("invalid_raw_result", "raw result candidate hash mismatch")
        if raw_result["checker_commit"] != LCB_CHECKER_COMMIT:
            raise LCBRunnerError("invalid_raw_result", "raw result checker identity mismatch")
        if raw_result["executor_id"] != LCB_EXECUTOR_ID:
            raise LCBRunnerError("invalid_raw_result", "raw result executor identity mismatch")

    def _infrastructure_result(
        self,
        *,
        question_id: str,
        candidate_sha256: str,
        public_test_count: int,
        error_type: str,
        started: float,
    ) -> Mapping[str, Any]:
        return {
            "question_id": question_id,
            "candidate_sha256": candidate_sha256,
            "infrastructure_status": "error",
            "error_type": error_type,
            "compile_ok": False,
            "official_test_results": [],
            "official_error_code": None,
            "public_test_count": public_test_count,
            "duration_seconds": max(0.0, self._clock() - started),
            "checker_commit": LCB_CHECKER_COMMIT,
            "executor_id": LCB_EXECUTOR_ID,
        }

    # ------------------------------------------------------------------
    # Cancellation and cleanup
    # ------------------------------------------------------------------
    def cancel_all(self) -> None:
        self._cancel_event.set()
        with self._activity_lock:
            active = tuple(self._active_containers)
        for container_name in active:
            self._force_remove(container_name)

    def _activate_container(self, container_name: str) -> bool:
        with self._activity_lock:
            if self._cancel_event.is_set():
                return False
            self._active_containers.add(container_name)
            return True

    def _deactivate_container(self, container_name: str) -> None:
        with self._activity_lock:
            self._active_containers.discard(container_name)

    def _force_remove(self, container_name: str) -> Literal["removed", "not_found", "failed"]:
        try:
            completed = self._invoke(
                ["docker", "rm", "-f", "-v", container_name],
                timeout=self.limits.cleanup_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired, _OutputLimitExceeded):
            return "failed"
        if completed.returncode == 0:
            return "removed"
        if "No such container" in self._bounded_text(completed.stderr):
            return "not_found"
        return "failed"

    def _cleanup_error(self, container_name: str, error_type: str, message: str) -> LCBRunnerError:
        cleanup_status = self._force_remove(container_name)
        return LCBRunnerError(error_type, message, cleanup_status=cleanup_status)

    # ------------------------------------------------------------------
    # Bounded subprocess helpers
    # ------------------------------------------------------------------
    def _invoke(self, command: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        if self._cancel_event.is_set():
            raise subprocess.TimeoutExpired(command, timeout)
        completed = self._command_runner(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        self._bounded_text(completed.stdout)
        self._bounded_text(completed.stderr)
        return completed

    @staticmethod
    def _bounded_text(value: str | None) -> str:
        if value is None:
            return ""
        if len(value) > _CONTROL_OUTPUT_LIMIT:
            raise _OutputLimitExceeded
        return value


__all__ = [
    "DEFAULT_LCB_PLATFORM",
    "DEFAULT_PER_TEST_TIMEOUT_SECONDS",
    "DockerLimits",
    "LCBDockerRunner",
    "LCBRunnerError",
    "LCB_IMAGE_REPOSITORY",
]
