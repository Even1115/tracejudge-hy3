"""Pinned, disclosure-safe Docker runner for the official EvalPlus image.

The host never imports or executes candidate source.  Each invocation mounts a
read-only control directory plus two exact host-owned output files and delegates
filtering and evaluation to :mod:`container_entrypoint` inside the pinned image.
All subprocess interaction is injectable so ordinary tests require neither Docker
nor network access.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .runner import ExecutorPreflight, ExecutorTaskOutcome
    from .schemas import EvalPlusSample, HumanEvalPlusTaskMetadata

DEFAULT_EVALPLUS_IMAGE = (
    "ganler/evalplus@sha256:26b118098bef281fe8dfe999bf05f1d5b45374b4e6c00161ec0f30592aef4740"
)
DEFAULT_PLATFORM = "linux/amd64"
EVALPLUS_VERSION = "0.4.0.dev2"
EVALPLUS_COMMIT = "f11cfb92c1d52896a87f988cbebbd74727d56c7e"
EVALPLUS_EVALUATE_PY_SHA256 = "6fcd78d262eae6eff8af4ef6eb00b22909d37beebd90dc37b84b756053e981dd"
HUMANEVAL_PLUS_VERSION = "v0.1.10"
IMAGE_PYTHON_VERSION = "3.11.10"
MAX_PREFLIGHT_TASK_COUNT = 164

_IMAGE_DIGEST = "sha256:26b118098bef281fe8dfe999bf05f1d5b45374b4e6c00161ec0f30592aef4740"
_CONTROL_OUTPUT_LIMIT = 16 * 1024
_SUBPROCESS_STREAM_LIMIT = 16 * 1024
_SAMPLE_SIZE_LIMIT = 2 * 1024 * 1024
_RAW_RESULT_SIZE_LIMIT = 128 * 1024 * 1024
_TASK_ID_RE = re.compile(r"^HumanEval/(?:0|[1-9][0-9]*)$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
Clock = Callable[[], float]
UuidFactory = Callable[[], uuid.UUID]


class DockerRunnerError(RuntimeError):
    """A disclosure-safe Docker/preflight failure."""

    def __init__(
        self,
        error_type: str,
        message: str,
        *,
        cleanup_status: Literal["not_needed", "removed", "not_found", "failed"] = "not_needed",
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.cleanup_status = cleanup_status


class _CancellationRequested(RuntimeError):
    pass


class _OutputLimitExceeded(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PublicTaskIdentity:
    task_id: str
    prompt_sha256: str
    entry_point: str

    def __post_init__(self) -> None:
        if not _TASK_ID_RE.fullmatch(self.task_id):
            raise ValueError("invalid HumanEval task ID")
        if not _SHA256_RE.fullmatch(self.prompt_sha256):
            raise ValueError("invalid prompt SHA256")
        if (
            not self.entry_point.isascii()
            or not self.entry_point.isidentifier()
            or not self.entry_point
        ):
            raise ValueError("invalid entry point")

    @classmethod
    def from_prompt(cls, *, task_id: str, prompt: str, entry_point: str) -> PublicTaskIdentity:
        if not isinstance(prompt, str):
            raise TypeError("prompt must be text")
        return cls(
            task_id=task_id,
            prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            entry_point=entry_point,
        )


@dataclass(frozen=True, slots=True)
class ImageInspection:
    image: str
    image_id: str
    repo_digests: tuple[str, ...]
    operating_system: str
    architecture: str
    platform: str


@dataclass(frozen=True, slots=True)
class RuntimeMetadata:
    evalplus_version: str
    evalplus_commit: str
    evalplus_commit_basis: str
    evalplus_evaluate_py_sha256: str
    evalplus_evaluate_py_sha256_basis: str
    humaneval_plus_version: str
    official_dataset_hash: str
    official_dataset_file_sha256: str
    official_dataset_file_sha256_basis: str
    official_dataset_file_size_bytes: int
    native_dataset_canonical_sha256: str
    native_dataset_sha256_basis: str
    dataset_task_count: int
    verified_task_count: int
    python_version: str
    python_implementation: str
    platform_system: str
    platform_machine: str
    output_integrity_strategy: str
    official_parent_forced_overwrite: bool
    official_parent_nofollow: bool
    reliability_guard_security_sandbox: bool


@dataclass(frozen=True, slots=True)
class PreflightResult:
    image: ImageInspection
    runtime: RuntimeMetadata
    requested_platform: str
    host_system: str
    host_machine: str

    def as_manifest_dict(self) -> dict[str, Any]:
        return {
            "image": asdict(self.image),
            "runtime": asdict(self.runtime),
            "requested_platform": self.requested_platform,
            "host_system": self.host_system,
            "host_machine": self.host_machine,
        }


@dataclass(frozen=True, slots=True)
class TaskExecutionResult:
    problem_id: str
    infrastructure_status: Literal["ok", "error"]
    raw_result_path: Path | None
    duration_seconds: float
    error_type: str | None
    container_name: str
    official_override_hash: str | None = None
    cleanup_status: Literal["not_needed", "removed", "not_found", "failed"] = "not_needed"
    diagnostic: str | None = None


@dataclass(frozen=True, slots=True)
class DockerLimits:
    memory: str = "4g"
    cpus: str = "1"
    pids: int = 128
    tmpfs_size: str = "1g"
    inspect_timeout_seconds: float = 30.0
    preflight_timeout_seconds: float = 180.0
    per_task_timeout_seconds: float = 180.0
    cleanup_timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not self.memory or not self.cpus or not self.tmpfs_size:
            raise ValueError("Docker resource limits must be non-empty")
        if self.pids <= 0:
            raise ValueError("Docker pids limit must be positive")
        for value in (
            self.inspect_timeout_seconds,
            self.preflight_timeout_seconds,
            self.per_task_timeout_seconds,
            self.cleanup_timeout_seconds,
        ):
            if value <= 0:
                raise ValueError("Docker timeouts must be positive")


class EvalPlusDockerRunner:
    """Run one candidate per hardened, explicitly ``linux/amd64`` container."""

    mode: Literal["docker"] = "docker"

    def __init__(
        self,
        *,
        image: str = DEFAULT_EVALPLUS_IMAGE,
        requested_platform: str = DEFAULT_PLATFORM,
        limits: DockerLimits | None = None,
        command_runner: CommandRunner = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
        clock: Clock = time.monotonic,
        uuid_factory: UuidFactory = uuid.uuid4,
    ) -> None:
        if image != DEFAULT_EVALPLUS_IMAGE:
            raise ValueError("only the pinned official EvalPlus image is allowed")
        if requested_platform != DEFAULT_PLATFORM:
            raise ValueError("only the pinned linux/amd64 platform is allowed")
        self.image = image
        self.requested_platform = requested_platform
        self.limits = limits or DockerLimits()
        self._command_runner = command_runner
        self._uses_native_subprocess = command_runner is subprocess.run
        self._which = which
        self._clock = clock
        self._uuid_factory = uuid_factory
        self._image_cache: ImageInspection | None = None
        self._runtime_cache: RuntimeMetadata | None = None
        self._verified_tasks: dict[str, PublicTaskIdentity] | None = None
        self._activity_lock = threading.Lock()
        self._active_containers: set[str] = set()
        self._cancel_event = threading.Event()

    def public_identity(self) -> Mapping[str, Any]:
        """Return the immutable executor identity and its enforced isolation policy."""

        return {
            "name": "official_evalplus_docker",
            "version": 1,
            "candidate_execution": True,
            "image": self.image,
            "image_digest": _IMAGE_DIGEST,
            "requested_platform": self.requested_platform,
            "evalplus_version": EVALPLUS_VERSION,
            "evalplus_commit": EVALPLUS_COMMIT,
            "evalplus_commit_basis": "git_C_evalplus_rev_parse_HEAD",
            "evalplus_evaluate_py_sha256": EVALPLUS_EVALUATE_PY_SHA256,
            "evalplus_evaluate_py_sha256_basis": ("installed_evalplus_evaluate_py_exact_bytes"),
            "humaneval_plus_version": HUMANEVAL_PLUS_VERSION,
            "python_version": IMAGE_PYTHON_VERSION,
            "official_dataset_hash": (
                self._runtime_cache.official_dataset_hash
                if self._runtime_cache is not None
                else None
            ),
            "native_dataset_canonical_sha256": (
                self._runtime_cache.native_dataset_canonical_sha256
                if self._runtime_cache is not None
                else None
            ),
            "official_dataset_file_sha256": (
                self._runtime_cache.official_dataset_file_sha256
                if self._runtime_cache is not None
                else None
            ),
            "official_dataset_file_sha256_basis": ("exact_bytes_from_pinned_release_ready_path"),
            "official_dataset_file_size_bytes": (
                self._runtime_cache.official_dataset_file_size_bytes
                if self._runtime_cache is not None
                else None
            ),
            "native_dataset_sha256_basis": (
                "loaded_native_corpus_canonical_json_utf8_sort_keys_compact"
            ),
            "official_command": {
                "dataset": "humaneval",
                "parallel": 1,
                "min_time_limit_seconds": 4.0,
                "gt_time_limit_factor": 4.0,
                "test_details": True,
            },
            "isolation": {
                "pull_policy": "never",
                "network": "none",
                "log_driver": "none",
                "read_only_root_filesystem": True,
                "control_mount_read_only": True,
                "control_source_directory_mode": "0555",
                "control_source_file_mode": "0444",
                "host_writable_bind_mounts": True,
                "writable_mount_scope": "two_exact_precreated_host_files",
                "output_transport": "exact_host_file_binds",
                "output_directory_mounted": False,
                "output_file_bind_count": 2,
                "output_staging_directory_mode": "0700",
                "output_staging_file_mode": "0666",
                "hard_disk_quota": False,
                "per_file_fsize_limit": "128MiB",
                "per_file_fsize_limit_bytes": _RAW_RESULT_SIZE_LIMIT,
                "output_copy_policy": "read_exact_files_after_container_exit_zero",
                "output_control_size_limit_bytes": _CONTROL_OUTPUT_LIMIT,
                "output_raw_size_limit_bytes": _RAW_RESULT_SIZE_LIMIT,
                "output_integrity_strategy": ("official_parent_nofollow_forced_overwrite_v1"),
                "reliability_guard_security_sandbox": False,
                "security_boundary": "basic_non_adversarial",
                "adversarial_candidate_integrity_guarantee": False,
                "known_limitations": [
                    (
                        "Candidate and wrapper share a container UID; the forced parent overwrite "
                        "is integrity hardening for a basic non-adversarial execution boundary"
                    ),
                    "EvalPlus reliability_guard is not a security sandbox",
                ],
                "memory": self.limits.memory,
                "memory_swap": self.limits.memory,
                "cpus": self.limits.cpus,
                "pids_limit": self.limits.pids,
                "cap_drop": "ALL",
                "no_new_privileges": True,
                "tmpfs_size": self.limits.tmpfs_size,
                "docker_socket_mounted": False,
                "host_environment_forwarded": False,
                "docker_proxy_environment_cleared": True,
            },
            "timeouts": {
                "image_inspect_seconds": self.limits.inspect_timeout_seconds,
                "preflight_seconds": self.limits.preflight_timeout_seconds,
                "per_task_outer_seconds": self.limits.per_task_timeout_seconds,
                "cleanup_seconds": self.limits.cleanup_timeout_seconds,
            },
        }

    def is_available(self) -> tuple[bool, str | None]:
        """Probe only the Docker CLI/daemon and return a bounded safe reason."""

        if self._which("docker") is None:
            return False, "docker CLI not found"
        command = ["docker", "info", "--format", "{{.ServerVersion}}"]
        try:
            completed = self._invoke(command, timeout=self.limits.inspect_timeout_seconds)
        except (OSError, subprocess.TimeoutExpired, _OutputLimitExceeded):
            return False, "docker daemon probe failed"
        if completed.returncode != 0 or not self._bounded_text(completed.stdout).strip():
            return False, "docker daemon unavailable"
        return True, None

    def cancel_all(
        self,
    ) -> dict[str, Literal["removed", "not_found", "failed"]]:
        """Cancel this runner and report each active container's cleanup status."""

        with self._activity_lock:
            self._cancel_event.set()
            container_names = tuple(sorted(self._active_containers))
        if not container_names:
            return {}
        with ThreadPoolExecutor(
            max_workers=min(16, len(container_names)),
            thread_name_prefix="evalplus-cleanup",
        ) as cleanup_pool:
            cleanup_results = tuple(cleanup_pool.map(self._force_remove, container_names))
        statuses: dict[str, Literal["removed", "not_found", "failed"]] = {}
        for container_name, cleanup_status in zip(
            container_names,
            cleanup_results,
            strict=True,
        ):
            statuses[container_name] = cleanup_status
            if cleanup_status != "failed":
                self._deactivate_container(container_name)
        return statuses

    def inspect_image(self) -> ImageInspection:
        """Verify the locally present immutable image and its amd64 platform."""

        if self._image_cache is not None:
            return self._image_cache
        available, reason = self.is_available()
        if not available:
            raise DockerRunnerError("docker_unavailable", reason or "docker unavailable")
        format_string = "{{.Id}}\t{{json .RepoDigests}}\t{{.Os}}\t{{.Architecture}}"
        command = ["docker", "image", "inspect", "--format", format_string, self.image]
        try:
            completed = self._invoke(command, timeout=self.limits.inspect_timeout_seconds)
        except subprocess.TimeoutExpired:
            raise DockerRunnerError("image_mismatch", "pinned image inspection timed out") from None
        except (OSError, _OutputLimitExceeded):
            raise DockerRunnerError(
                "docker_unavailable", "could not inspect pinned image"
            ) from None
        if completed.returncode != 0:
            raise DockerRunnerError("image_mismatch", "pinned image is not locally available")
        output = self._bounded_text(completed.stdout).strip()
        parts = output.split("\t")
        if len(parts) != 4:
            raise DockerRunnerError("image_mismatch", "pinned image metadata is malformed")
        image_id, repo_json, operating_system, architecture = parts
        try:
            repo_value = json.loads(repo_json)
        except json.JSONDecodeError:
            raise DockerRunnerError(
                "image_mismatch", "pinned image digests are malformed"
            ) from None
        if (
            not _IMAGE_ID_RE.fullmatch(image_id)
            or not isinstance(repo_value, list)
            or any(not isinstance(item, str) for item in repo_value)
        ):
            raise DockerRunnerError("image_mismatch", "pinned image identity is malformed")
        repo_digests = tuple(repo_value)
        if self.image not in repo_digests or _IMAGE_DIGEST not in self.image:
            raise DockerRunnerError("image_mismatch", "pinned image digest does not match")
        inspected_platform = f"{operating_system}/{architecture}"
        if inspected_platform != self.requested_platform:
            raise DockerRunnerError("image_mismatch", "pinned image platform does not match")
        inspection = ImageInspection(
            image=self.image,
            image_id=image_id,
            repo_digests=repo_digests,
            operating_system=operating_system,
            architecture=architecture,
            platform=inspected_platform,
        )
        self._image_cache = inspection
        return inspection

    def preflight(
        self,
        *,
        task_metadata: Sequence[HumanEvalPlusTaskMetadata],
        workspace: Path,
    ) -> ExecutorPreflight:
        """Implement the phase-two executor preflight protocol."""

        # Imported lazily to keep runner.py independent of this concrete
        # executor and avoid a module-import cycle.
        from .runner import ExecutorPreflight

        try:
            identities = tuple(
                PublicTaskIdentity(
                    task_id=item.problem_id,
                    prompt_sha256=item.prompt_sha256,
                    entry_point=item.entry_point,
                )
                for item in task_metadata
            )
            result = self.preflight_tasks(workspace, identities)
        except DockerRunnerError as exc:
            return ExecutorPreflight(
                ready=False,
                runtime=self._failed_runtime_metadata(),
                infrastructure_error_type=exc.error_type,
                diagnostics={"cleanup_status": exc.cleanup_status},
            )
        except (AttributeError, TypeError, ValueError):
            return ExecutorPreflight(
                ready=False,
                runtime=self._failed_runtime_metadata(),
                infrastructure_error_type="executor_error",
                diagnostics={},
            )
        return ExecutorPreflight(
            ready=True,
            runtime=result.as_manifest_dict(),
            infrastructure_error_type=None,
            diagnostics=None,
        )

    def preflight_tasks(
        self,
        workspace: str | Path,
        tasks: Sequence[PublicTaskIdentity],
    ) -> PreflightResult:
        """Verify image runtime, native dataset, and the selected public task identities."""

        identities = self._validate_preflight_tasks(tasks)
        workspace_path = self._workspace(workspace)
        image = self.inspect_image()
        request = {"schema_version": 1, "tasks": [asdict(task) for task in identities]}
        with self._staging_directory(workspace_path) as staging_value:
            staging = Path(staging_value)
            control, _output = self._staging_mounts(staging)
            self._stage_control_files(control, request=request)
            container_name = self._container_name("inspect")
            command = self._container_command(
                control,
                container_name,
                ["inspect", "/control/request.json"],
                detached=False,
            )
            try:
                completed = self._invoke_container(
                    command,
                    container_name=container_name,
                    timeout=self.limits.preflight_timeout_seconds,
                )
            except _CancellationRequested:
                cleanup_status = self._force_remove(container_name)
                raise DockerRunnerError(
                    "container_timeout",
                    "EvalPlus preflight was cancelled",
                    cleanup_status=cleanup_status,
                ) from None
            except subprocess.TimeoutExpired:
                cleanup_status = self._force_remove(container_name)
                raise DockerRunnerError(
                    "container_timeout",
                    "EvalPlus preflight timed out",
                    cleanup_status=cleanup_status,
                ) from None
            except OSError:
                cleanup_status = self._force_remove(container_name)
                raise DockerRunnerError(
                    "container_start_error",
                    "could not start EvalPlus preflight",
                    cleanup_status=cleanup_status,
                ) from None
            except _OutputLimitExceeded:
                cleanup_status = self._force_remove(container_name)
                raise DockerRunnerError(
                    "executor_error",
                    "EvalPlus preflight output exceeded its limit",
                    cleanup_status=cleanup_status,
                ) from None
            except BaseException:
                self._force_remove(container_name)
                raise
            if completed.returncode != 0:
                cleanup_status = self._force_remove(container_name)
                error_type = self._preflight_error_type(completed.stdout)
                raise DockerRunnerError(
                    error_type,
                    "EvalPlus preflight failed",
                    cleanup_status=cleanup_status,
                )
            try:
                payload = self._control_payload(completed.stdout, expected_mode="inspect")
            except DockerRunnerError:
                cleanup_status = self._force_remove(container_name)
                raise DockerRunnerError(
                    "executor_error",
                    "EvalPlus preflight control output was invalid",
                    cleanup_status=cleanup_status,
                ) from None
        try:
            runtime = self._runtime_metadata(payload, expected_task_count=len(identities))
        except DockerRunnerError:
            cleanup_status = self._force_remove(container_name)
            raise DockerRunnerError(
                "image_mismatch",
                "EvalPlus runtime metadata did not match",
                cleanup_status=cleanup_status,
            ) from None
        self._verified_tasks = {task.task_id: task for task in identities}
        self._runtime_cache = runtime
        return PreflightResult(
            image=image,
            runtime=runtime,
            requested_platform=self.requested_platform,
            host_system=platform.system(),
            host_machine=platform.machine(),
        )

    def run_task(
        self,
        *,
        sample: EvalPlusSample,
        task_metadata: HumanEvalPlusTaskMetadata,
        workspace: Path,
    ) -> ExecutorTaskOutcome:
        """Implement the phase-two executor protocol for one candidate."""

        from .parser import EvalPlusParseError, load_official_raw_result, parse_official_result
        from .runner import ExecutorTaskOutcome

        started_at = self._utc_timestamp()
        try:
            identity = PublicTaskIdentity(
                task_id=task_metadata.problem_id,
                prompt_sha256=task_metadata.prompt_sha256,
                entry_point=task_metadata.entry_point,
            )
            if sample.task_id != identity.task_id:
                raise ValueError("sample and task metadata differ")
            self._require_verified_task(identity)
            workspace_path = self._workspace(workspace)
            sample_path = workspace_path / "sample.jsonl"
            sample_payload = (
                json.dumps(
                    {"task_id": sample.task_id, "solution": sample.solution},
                    ensure_ascii=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            self._exclusive_write(sample_path, sample_payload, mode=0o600)
            raw_path = workspace_path / "official_evalplus_raw_result.json"
            result = self.run_sample_file(workspace_path, identity, sample_path, raw_path)
        except DockerRunnerError as exc:
            return ExecutorTaskOutcome(
                problem_id=getattr(sample, "task_id", "HumanEval/0"),
                started_at=started_at,
                ended_at=self._utc_timestamp(),
                duration_seconds=0.0,
                raw_result=None,
                infrastructure_error_type=exc.error_type,
                diagnostics={"cleanup_status": exc.cleanup_status},
            )
        except (AttributeError, OSError, TypeError, ValueError):
            return ExecutorTaskOutcome(
                problem_id=getattr(sample, "task_id", "HumanEval/0"),
                started_at=started_at,
                ended_at=self._utc_timestamp(),
                duration_seconds=0.0,
                raw_result=None,
                infrastructure_error_type="executor_error",
                diagnostics={},
            )

        if result.infrastructure_status != "ok" or result.raw_result_path is None:
            return ExecutorTaskOutcome(
                problem_id=result.problem_id,
                started_at=started_at,
                ended_at=self._utc_timestamp(),
                duration_seconds=result.duration_seconds,
                raw_result=None,
                infrastructure_error_type=result.error_type or "executor_error",
                diagnostics={"cleanup_status": result.cleanup_status},
            )
        try:
            raw_result = load_official_raw_result(result.raw_result_path)
        except EvalPlusParseError:
            cleanup_status = self._force_remove(result.container_name)
            return ExecutorTaskOutcome(
                problem_id=result.problem_id,
                started_at=started_at,
                ended_at=self._utc_timestamp(),
                duration_seconds=result.duration_seconds,
                raw_result=None,
                infrastructure_error_type="invalid_raw_result",
                diagnostics={"cleanup_status": cleanup_status},
            )
        if not isinstance(raw_result, Mapping):
            cleanup_status = self._force_remove(result.container_name)
            return ExecutorTaskOutcome(
                problem_id=result.problem_id,
                started_at=started_at,
                ended_at=self._utc_timestamp(),
                duration_seconds=result.duration_seconds,
                raw_result=None,
                infrastructure_error_type="invalid_raw_result",
                diagnostics={"cleanup_status": cleanup_status},
            )
        if (
            not isinstance(result.official_override_hash, str)
            or not re.fullmatch(r"[0-9a-f]{32}", result.official_override_hash)
            or raw_result.get("hash") != result.official_override_hash
        ):
            cleanup_status = self._force_remove(result.container_name)
            return ExecutorTaskOutcome(
                problem_id=result.problem_id,
                started_at=started_at,
                ended_at=self._utc_timestamp(),
                duration_seconds=result.duration_seconds,
                raw_result=None,
                infrastructure_error_type="invalid_raw_result",
                diagnostics={"cleanup_status": cleanup_status},
            )
        try:
            parse_official_result(
                raw_result,
                expected_problem_id=result.problem_id,
                expected_solution_sha256=hashlib.sha256(
                    sample.solution.encode("utf-8")
                ).hexdigest(),
            )
        except EvalPlusParseError:
            cleanup_status = self._force_remove(result.container_name)
            return ExecutorTaskOutcome(
                problem_id=result.problem_id,
                started_at=started_at,
                ended_at=self._utc_timestamp(),
                duration_seconds=result.duration_seconds,
                raw_result=None,
                infrastructure_error_type="invalid_raw_result",
                diagnostics={"cleanup_status": cleanup_status},
            )
        return ExecutorTaskOutcome(
            problem_id=result.problem_id,
            started_at=started_at,
            ended_at=self._utc_timestamp(),
            duration_seconds=result.duration_seconds,
            raw_result=raw_result,
            infrastructure_error_type=None,
            diagnostics={"cleanup_status": result.cleanup_status},
        )

    def run_sample_file(
        self,
        workspace: str | Path,
        task: PublicTaskIdentity,
        sample_path: str | Path,
        raw_result_path: str | Path,
    ) -> TaskExecutionResult:
        """Run one preflight-verified sample and publish only its private raw JSON."""

        self._require_verified_task(task)
        workspace_path = self._workspace(workspace)
        sample = self._sample_file(sample_path)
        destination = self._result_destination(workspace_path, raw_result_path)
        container_name = self._container_name("task")
        started = self._clock()
        if self._cancel_event.is_set():
            return self._task_error(
                task.task_id,
                container_name,
                started,
                "container_timeout",
                "EvalPlus task was cancelled before container start",
            )
        try:
            with self._staging_directory(workspace_path) as staging_value:
                staging = Path(staging_value)
                control, output = self._staging_mounts(staging)
                request = {"schema_version": 1, "task": asdict(task)}
                self._stage_control_files(control, request=request, sample=sample)
                staged_result = output / "sample_eval_results.json"
                staged_control = output / "control.json"
                self._stage_output_files(staged_result, staged_control)
                command = self._container_command(
                    control,
                    container_name,
                    [
                        "run",
                        "/control/request.json",
                        "/control/sample.jsonl",
                        "/output/sample_eval_results.json",
                        "/output/control.json",
                    ],
                    detached=True,
                    output_files=(staged_result, staged_control),
                )
                if not self._activate_container(container_name):
                    return self._cleanup_task_error(
                        task.task_id,
                        container_name,
                        started,
                        "container_timeout",
                        "EvalPlus task was cancelled before container start",
                    )
                try:
                    completed = self._invoke(
                        command,
                        timeout=min(
                            self.limits.inspect_timeout_seconds,
                            self.limits.per_task_timeout_seconds,
                        ),
                    )
                except _CancellationRequested:
                    return self._cleanup_task_error(
                        task.task_id,
                        container_name,
                        started,
                        "container_timeout",
                        "EvalPlus task was cancelled by the batch deadline",
                    )
                except subprocess.TimeoutExpired:
                    return self._cleanup_task_error(
                        task.task_id,
                        container_name,
                        started,
                        "container_timeout",
                        "EvalPlus task container exceeded its outer timeout",
                    )
                except OSError:
                    return self._cleanup_task_error(
                        task.task_id,
                        container_name,
                        started,
                        "container_start_error",
                        "could not start EvalPlus task container",
                    )
                except _OutputLimitExceeded:
                    return self._cleanup_task_error(
                        task.task_id,
                        container_name,
                        started,
                        "executor_error",
                        "EvalPlus task control output exceeded its limit",
                    )
                if completed.returncode != 0:
                    error_type = (
                        "container_timeout"
                        if self._cancel_event.is_set()
                        else "container_exit_error"
                    )
                    return self._cleanup_task_error(
                        task.task_id,
                        container_name,
                        started,
                        error_type,
                        "EvalPlus task container exited unsuccessfully",
                    )
                # ``cancel_all`` may race a slow Docker create: its first
                # removal can legitimately observe no container.  Once create
                # reports success, re-check cancellation and remove again so a
                # late-created container can never escape the batch deadline.
                if self._cancel_event.is_set():
                    return self._cleanup_task_error(
                        task.task_id,
                        container_name,
                        started,
                        "container_timeout",
                        "EvalPlus task was cancelled during container creation",
                    )
                remaining = self.limits.per_task_timeout_seconds - (self._clock() - started)
                if remaining <= 0:
                    return self._cleanup_task_error(
                        task.task_id,
                        container_name,
                        started,
                        "container_timeout",
                        "EvalPlus task container exceeded its outer timeout",
                    )
                wait_command = ["docker", "wait", container_name]
                try:
                    completed = self._invoke(wait_command, timeout=remaining)
                except subprocess.TimeoutExpired:
                    return self._cleanup_task_error(
                        task.task_id,
                        container_name,
                        started,
                        "container_timeout",
                        "EvalPlus task container exceeded its outer timeout",
                    )
                except OSError:
                    return self._cleanup_task_error(
                        task.task_id,
                        container_name,
                        started,
                        "container_exit_error",
                        "could not await EvalPlus task control response",
                    )
                except _OutputLimitExceeded:
                    return self._cleanup_task_error(
                        task.task_id,
                        container_name,
                        started,
                        "executor_error",
                        "EvalPlus task control output exceeded its limit",
                    )
                container_exit = self._bounded_text(completed.stdout).strip()
                if (
                    completed.returncode != 0
                    or container_exit != "0"
                    or self._cancel_event.is_set()
                ):
                    error_type = (
                        "container_timeout"
                        if self._cancel_event.is_set()
                        else "container_exit_error"
                    )
                    return self._cleanup_task_error(
                        task.task_id,
                        container_name,
                        started,
                        error_type,
                        "EvalPlus task container exited unsuccessfully",
                    )
                remaining = self.limits.per_task_timeout_seconds - (self._clock() - started)
                if remaining <= 0:
                    return self._cleanup_task_error(
                        task.task_id,
                        container_name,
                        started,
                        "container_timeout",
                        "EvalPlus task container exceeded its outer timeout",
                    )
                if not self._is_bounded_regular_file(
                    staged_control,
                    size_limit=_CONTROL_OUTPUT_LIMIT - 1,
                ) or not self._is_bounded_regular_file(
                    staged_result,
                    size_limit=_RAW_RESULT_SIZE_LIMIT,
                ):
                    return self._cleanup_task_error(
                        task.task_id,
                        container_name,
                        started,
                        "missing_raw_result",
                        "official EvalPlus raw result is missing",
                    )
                try:
                    payload = self._control_payload(
                        staged_control.read_bytes(),
                        expected_mode="run",
                    )
                except (DockerRunnerError, OSError):
                    return self._cleanup_task_error(
                        task.task_id,
                        container_name,
                        started,
                        "executor_error",
                        "EvalPlus task control response was invalid",
                    )
                if (
                    payload.get("task_id") != task.task_id
                    or payload.get("result_available") is not True
                    or not isinstance(payload.get("official_override_hash"), str)
                    or not re.fullmatch(r"[0-9a-f]{32}", payload["official_override_hash"])
                ):
                    return self._cleanup_task_error(
                        task.task_id,
                        container_name,
                        started,
                        "executor_error",
                        "EvalPlus task control response did not match",
                    )
                cleanup_status = self._force_remove(container_name)
                if cleanup_status == "failed":
                    return self._task_error(
                        task.task_id,
                        container_name,
                        started,
                        "container_cleanup_failed",
                        "EvalPlus task container cleanup failed",
                        cleanup_status=cleanup_status,
                    )
                self._deactivate_container(container_name)
                self._publish_private_result(staged_result, destination)
        except OSError:
            return self._cleanup_task_error(
                task.task_id,
                container_name,
                started,
                "executor_error",
                "EvalPlus result transport failed",
            )
        except BaseException:
            cleanup_status = self._force_remove(container_name)
            if cleanup_status != "failed":
                self._deactivate_container(container_name)
            raise
        return TaskExecutionResult(
            problem_id=task.task_id,
            infrastructure_status="ok",
            raw_result_path=destination,
            duration_seconds=max(0.0, self._clock() - started),
            error_type=None,
            container_name=container_name,
            official_override_hash=str(payload["official_override_hash"]),
            cleanup_status=cleanup_status,
        )

    def _invoke(
        self,
        command: Sequence[str],
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        if self._uses_native_subprocess:
            return self._bounded_subprocess_run(command, timeout=timeout)
        return self._command_runner(
            list(command),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    @staticmethod
    def _bounded_subprocess_run(
        command: Sequence[str],
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        """Drain both Docker streams concurrently and retain at most 16 KiB each."""

        process = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_buffer = bytearray()
        stderr_buffer = bytearray()
        exceeded = threading.Event()

        def drain(stream: Any, buffer: bytearray) -> None:
            try:
                while chunk := stream.read(4096):
                    remaining = _SUBPROCESS_STREAM_LIMIT - len(buffer)
                    if remaining > 0:
                        buffer.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        exceeded.set()
                        try:
                            # A control-stream overflow is a protocol
                            # violation.  Stop the Docker client immediately;
                            # the caller force-removes the known container.
                            process.kill()
                        except OSError:
                            pass
                        return
            except OSError:
                return

        readers = (
            threading.Thread(target=drain, args=(process.stdout, stdout_buffer), daemon=True),
            threading.Thread(target=drain, args=(process.stderr, stderr_buffer), daemon=True),
        )
        for reader in readers:
            reader.start()
        timed_out = False
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            returncode = process.wait()
        except BaseException:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait()
            except BaseException:
                pass
            for reader in readers:
                reader.join(timeout=1.0)
            process.stdout.close()
            process.stderr.close()
            raise
        for reader in readers:
            reader.join(timeout=1.0)
        process.stdout.close()
        process.stderr.close()
        if timed_out:
            raise subprocess.TimeoutExpired(list(command), timeout) from None
        if exceeded.is_set():
            if process.poll() is None:
                process.kill()
                process.wait()
            raise _OutputLimitExceeded
        return subprocess.CompletedProcess(
            list(command),
            returncode,
            stdout_buffer.decode("utf-8", errors="replace"),
            stderr_buffer.decode("utf-8", errors="replace"),
        )

    def _invoke_container(
        self,
        command: Sequence[str],
        *,
        container_name: str,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        if not self._activate_container(container_name):
            raise _CancellationRequested
        try:
            if self._cancel_event.is_set():
                raise _CancellationRequested
            completed = self._invoke(command, timeout=timeout)
            if self._cancel_event.is_set():
                raise _CancellationRequested
            return completed
        finally:
            self._deactivate_container(container_name)

    def _container_command(
        self,
        control: Path,
        container_name: str,
        entrypoint_args: Sequence[str],
        *,
        detached: bool,
        output_files: tuple[Path, Path] | None = None,
    ) -> list[str]:
        control_source = str(control)
        if any(character in control_source for character in (",", "\n", "\r")):
            raise DockerRunnerError("container_start_error", "workspace path is not mount-safe")
        if detached != (output_files is not None):
            raise DockerRunnerError("container_start_error", "output transport is invalid")
        command = [
            "docker",
            "run",
        ]
        command.append("-d" if detached else "--rm")
        command.extend(
            [
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
                "--workdir",
                "/tmp",
                self.image,
                "python3",
                "-B",
                "-u",
                "/control/entrypoint.py",
                *entrypoint_args,
            ]
        )
        output_index = command.index("--workdir")
        if detached:
            assert output_files is not None
            result_source, control_output_source = (str(path) for path in output_files)
            if any(
                character in source
                for source in (result_source, control_output_source)
                for character in (",", "\n", "\r")
            ):
                raise DockerRunnerError("container_start_error", "workspace path is not mount-safe")
            command[output_index:output_index] = [
                "--mount",
                (f"type=bind,src={result_source},dst=/output/sample_eval_results.json"),
                "--mount",
                f"type=bind,src={control_output_source},dst=/output/control.json",
                "--ulimit",
                f"fsize={_RAW_RESULT_SIZE_LIMIT}:{_RAW_RESULT_SIZE_LIMIT}",
            ]
        return command

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

    def _workspace(self, value: str | Path) -> Path:
        candidate = Path(value).expanduser()
        try:
            metadata = candidate.lstat()
            resolved = candidate.resolve(strict=True)
        except OSError:
            raise DockerRunnerError(
                "container_start_error", "dedicated workspace is unavailable"
            ) from None
        if candidate.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise DockerRunnerError(
                "container_start_error", "dedicated workspace must be a real directory"
            )
        if resolved in {Path("/"), Path.home().resolve()}:
            raise DockerRunnerError("container_start_error", "refusing a broad workspace mount")
        return resolved

    def _sample_file(self, value: str | Path) -> Path:
        candidate = Path(value).expanduser()
        try:
            metadata = candidate.lstat()
            resolved = candidate.resolve(strict=True)
        except OSError:
            raise DockerRunnerError("executor_error", "sample file is unavailable") from None
        if (
            candidate.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > _SAMPLE_SIZE_LIMIT
        ):
            raise DockerRunnerError("executor_error", "sample file is invalid")
        return resolved

    def _result_destination(self, workspace: Path, value: str | Path) -> Path:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = workspace / candidate
        try:
            parent = candidate.parent.resolve(strict=True)
        except OSError:
            raise DockerRunnerError(
                "executor_error", "raw result directory is unavailable"
            ) from None
        try:
            parent.relative_to(workspace)
        except ValueError:
            raise DockerRunnerError(
                "executor_error", "raw result must stay in the workspace"
            ) from None
        if candidate.exists() or candidate.is_symlink():
            raise DockerRunnerError("executor_error", "raw result already exists")
        return parent / candidate.name

    def _staging_directory(self, workspace: Path) -> tempfile.TemporaryDirectory[str]:
        return tempfile.TemporaryDirectory(prefix=".evalplus_container_", dir=workspace)

    @staticmethod
    def _staging_mounts(staging: Path) -> tuple[Path, Path]:
        control = staging / "control"
        output = staging / "output"
        control.mkdir(mode=0o700)
        output.mkdir(mode=0o700)
        return control, output

    def _stage_control_files(
        self,
        staging_value: str | Path,
        *,
        request: Mapping[str, Any],
        sample: Path | None = None,
    ) -> None:
        staging = Path(staging_value)
        source = Path(__file__).with_name("container_entrypoint.py")
        shutil.copyfile(source, staging / "entrypoint.py")
        os.chmod(staging / "entrypoint.py", 0o444)
        request_bytes = (
            json.dumps(request, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        self._exclusive_write(staging / "request.json", request_bytes, mode=0o444)
        os.chmod(staging / "request.json", 0o444)
        if sample is not None:
            shutil.copyfile(sample, staging / "sample.jsonl")
            os.chmod(staging / "sample.jsonl", 0o444)
        # A root process with every capability dropped cannot bypass host DAC.
        # Make the read-only bind source traversable/readable by that fixed
        # container user only after all trusted staging writes are complete.
        os.chmod(staging, 0o555)

    def _stage_output_files(self, result: Path, control: Path) -> None:
        """Create only the two host-owned files exposed writable to the container."""

        if result.parent != control.parent or result.parent.is_symlink():
            raise OSError("output staging is invalid")
        for path in (result, control):
            self._exclusive_write(path, b"", mode=0o666)
            # Apply the intended mode explicitly because process umask is
            # otherwise allowed to make a native-Linux, capability-less
            # container unable to write its exact file bind.
            os.chmod(path, 0o666)

    @staticmethod
    def _exclusive_write(path: Path, payload: bytes, *, mode: int) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())

    def _publish_private_result(self, source: Path, destination: Path) -> None:
        if not self._is_private_regular_result(source):
            raise OSError("staged result is invalid")
        temporary = destination.with_name(f".{destination.name}.{self._uuid_factory().hex}.tmp")
        if temporary.exists() or temporary.is_symlink():
            raise OSError("temporary result collision")
        source_metadata = source.lstat()
        read_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        write_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        source_descriptor = os.open(source, read_flags)
        temporary_descriptor: int | None = None
        try:
            opened_source = os.fstat(source_descriptor)
            if (
                not stat.S_ISREG(opened_source.st_mode)
                or (opened_source.st_dev, opened_source.st_ino)
                != (source_metadata.st_dev, source_metadata.st_ino)
                or opened_source.st_size != source_metadata.st_size
            ):
                raise OSError("staged result changed during publication")
            temporary_descriptor = os.open(temporary, write_flags, 0o600)
            os.fchmod(temporary_descriptor, 0o600)
            copied = 0
            while chunk := os.read(source_descriptor, 64 * 1024):
                copied += len(chunk)
                if copied > _RAW_RESULT_SIZE_LIMIT:
                    raise OSError("staged result exceeded its size limit")
                view = memoryview(chunk)
                while view:
                    written = os.write(temporary_descriptor, view)
                    if written <= 0:
                        raise OSError("could not publish staged result")
                    view = view[written:]
            if copied != opened_source.st_size:
                raise OSError("staged result changed during publication")
            os.fsync(temporary_descriptor)
            published = os.fstat(temporary_descriptor)
            if (
                not stat.S_ISREG(published.st_mode)
                or published.st_size != copied
                or stat.S_IMODE(published.st_mode) != 0o600
                or published.st_uid != os.geteuid()
                or (published.st_dev, published.st_ino)
                == (opened_source.st_dev, opened_source.st_ino)
            ):
                raise OSError("published result identity is invalid")
        except BaseException:
            if temporary_descriptor is not None:
                os.close(temporary_descriptor)
                temporary_descriptor = None
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise
        finally:
            os.close(source_descriptor)
            if temporary_descriptor is not None:
                os.close(temporary_descriptor)
        os.replace(temporary, destination)
        directory_descriptor = os.open(
            destination.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)

    @staticmethod
    def _is_private_regular_result(path: Path) -> bool:
        return EvalPlusDockerRunner._is_bounded_regular_file(
            path,
            size_limit=_RAW_RESULT_SIZE_LIMIT,
        )

    @staticmethod
    def _is_bounded_regular_file(path: Path, *, size_limit: int) -> bool:
        try:
            metadata = path.lstat()
        except OSError:
            return False
        return (
            not path.is_symlink()
            and stat.S_ISREG(metadata.st_mode)
            and 0 < metadata.st_size <= size_limit
        )

    def _control_payload(self, output: str | bytes | None, *, expected_mode: str) -> dict[str, Any]:
        text = self._bounded_text(output)
        if not text or len(text.encode("utf-8")) >= _CONTROL_OUTPUT_LIMIT:
            raise DockerRunnerError("executor_error", "container control output is invalid")
        lines = [line for line in text.splitlines() if line.strip()]
        if len(lines) != 1:
            raise DockerRunnerError("executor_error", "container control output is invalid")
        try:
            payload = json.loads(lines[0])
        except json.JSONDecodeError:
            raise DockerRunnerError(
                "executor_error", "container control output is invalid"
            ) from None
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or payload.get("status") != "ok"
            or payload.get("mode") != expected_mode
        ):
            raise DockerRunnerError("executor_error", "container control operation failed")
        return payload

    def _preflight_error_type(self, output: str | bytes | None) -> str:
        """Map only allowlisted entrypoint failures to a host classification."""

        text = self._bounded_text(output)
        lines = [line for line in text.splitlines() if line.strip()]
        if len(lines) != 1:
            return "executor_error"
        try:
            payload = json.loads(lines[0])
        except json.JSONDecodeError:
            return "executor_error"
        if not isinstance(payload, dict) or payload.get("status") != "error":
            return "executor_error"
        if payload.get("error_type") in {
            "runtime_unavailable",
            "runtime_identity_mismatch",
            "dataset_identity_mismatch",
            "task_identity_mismatch",
        }:
            return "image_mismatch"
        return "executor_error"

    @staticmethod
    def _bounded_text(value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        encoded = value.encode("utf-8", errors="replace")[:_CONTROL_OUTPUT_LIMIT]
        return encoded.decode("utf-8", errors="replace")

    @staticmethod
    def _runtime_metadata(
        payload: Mapping[str, Any], *, expected_task_count: int
    ) -> RuntimeMetadata:
        fields = {
            "evalplus_version",
            "evalplus_commit",
            "evalplus_commit_basis",
            "evalplus_evaluate_py_sha256",
            "evalplus_evaluate_py_sha256_basis",
            "humaneval_plus_version",
            "official_dataset_hash",
            "official_dataset_file_sha256",
            "official_dataset_file_sha256_basis",
            "official_dataset_file_size_bytes",
            "native_dataset_canonical_sha256",
            "native_dataset_sha256_basis",
            "dataset_task_count",
            "verified_task_count",
            "python_version",
            "python_implementation",
            "platform_system",
            "platform_machine",
            "output_integrity_strategy",
            "official_parent_forced_overwrite",
            "official_parent_nofollow",
            "reliability_guard_security_sandbox",
        }
        if not fields.issubset(payload):
            raise DockerRunnerError("image_mismatch", "runtime metadata is incomplete")
        if (
            payload.get("evalplus_version") != EVALPLUS_VERSION
            or payload.get("evalplus_commit") != EVALPLUS_COMMIT
            or payload.get("evalplus_commit_basis") != "git_C_evalplus_rev_parse_HEAD"
            or payload.get("evalplus_evaluate_py_sha256") != EVALPLUS_EVALUATE_PY_SHA256
            or payload.get("evalplus_evaluate_py_sha256_basis")
            != "installed_evalplus_evaluate_py_exact_bytes"
            or payload.get("humaneval_plus_version") != HUMANEVAL_PLUS_VERSION
            or payload.get("dataset_task_count") != 164
            or payload.get("verified_task_count") != expected_task_count
            or payload.get("native_dataset_sha256_basis")
            != "loaded_native_corpus_canonical_json_utf8_sort_keys_compact"
            or payload.get("official_dataset_file_sha256_basis")
            != "exact_bytes_from_pinned_release_ready_path"
            or payload.get("python_version") != IMAGE_PYTHON_VERSION
            or payload.get("platform_system") != "Linux"
            or payload.get("platform_machine") not in {"x86_64", "AMD64"}
            or payload.get("output_integrity_strategy")
            != "official_parent_nofollow_forced_overwrite_v1"
            or payload.get("official_parent_forced_overwrite") is not True
            or payload.get("official_parent_nofollow") is not True
            or payload.get("reliability_guard_security_sandbox") is not False
        ):
            raise DockerRunnerError("image_mismatch", "runtime metadata does not match the pin")
        official_dataset_hash = payload.get("official_dataset_hash")
        dataset_file_sha256 = payload.get("official_dataset_file_sha256")
        dataset_file_size = payload.get("official_dataset_file_size_bytes")
        dataset_sha256 = payload.get("native_dataset_canonical_sha256")
        string_fields = (
            "python_version",
            "python_implementation",
            "platform_system",
            "platform_machine",
        )
        if (
            not isinstance(official_dataset_hash, str)
            or not re.fullmatch(r"[0-9a-f]{32}", official_dataset_hash)
            or not isinstance(dataset_file_sha256, str)
            or not _SHA256_RE.fullmatch(dataset_file_sha256)
            or isinstance(dataset_file_size, bool)
            or not isinstance(dataset_file_size, int)
            or dataset_file_size <= 0
            or not isinstance(dataset_sha256, str)
            or not _SHA256_RE.fullmatch(dataset_sha256)
            or any(not isinstance(payload.get(field), str) for field in string_fields)
        ):
            raise DockerRunnerError("image_mismatch", "runtime metadata is malformed")
        return RuntimeMetadata(
            evalplus_version=str(payload["evalplus_version"]),
            evalplus_commit=str(payload["evalplus_commit"]),
            evalplus_commit_basis=str(payload["evalplus_commit_basis"]),
            evalplus_evaluate_py_sha256=str(payload["evalplus_evaluate_py_sha256"]),
            evalplus_evaluate_py_sha256_basis=str(payload["evalplus_evaluate_py_sha256_basis"]),
            humaneval_plus_version=HUMANEVAL_PLUS_VERSION,
            official_dataset_hash=official_dataset_hash,
            official_dataset_file_sha256=dataset_file_sha256,
            official_dataset_file_sha256_basis=("exact_bytes_from_pinned_release_ready_path"),
            official_dataset_file_size_bytes=dataset_file_size,
            native_dataset_canonical_sha256=dataset_sha256,
            native_dataset_sha256_basis=(
                "loaded_native_corpus_canonical_json_utf8_sort_keys_compact"
            ),
            dataset_task_count=164,
            verified_task_count=expected_task_count,
            python_version=str(payload["python_version"]),
            python_implementation=str(payload["python_implementation"]),
            platform_system=str(payload["platform_system"]),
            platform_machine=str(payload["platform_machine"]),
            output_integrity_strategy="official_parent_nofollow_forced_overwrite_v1",
            official_parent_forced_overwrite=True,
            official_parent_nofollow=True,
            reliability_guard_security_sandbox=False,
        )

    def _failed_runtime_metadata(self) -> dict[str, Any]:
        """Return stable, safe pins even when the runtime cannot be inspected."""

        return {
            "inspection_status": "unavailable",
            "requested_platform": self.requested_platform,
            "host_system": platform.system(),
            "host_machine": platform.machine(),
            "evalplus_version": EVALPLUS_VERSION,
            "evalplus_commit": EVALPLUS_COMMIT,
            "evalplus_commit_basis": "git_C_evalplus_rev_parse_HEAD",
            "evalplus_evaluate_py_sha256": EVALPLUS_EVALUATE_PY_SHA256,
            "evalplus_evaluate_py_sha256_basis": ("installed_evalplus_evaluate_py_exact_bytes"),
            "humaneval_plus_version": HUMANEVAL_PLUS_VERSION,
            "python_version": IMAGE_PYTHON_VERSION,
            "image": self.image,
            "official_dataset_hash": None,
            "native_dataset_canonical_sha256": None,
            "official_dataset_file_sha256": None,
            "official_dataset_file_sha256_basis": ("exact_bytes_from_pinned_release_ready_path"),
            "official_dataset_file_size_bytes": None,
            "native_dataset_sha256_basis": (
                "loaded_native_corpus_canonical_json_utf8_sort_keys_compact"
            ),
            "output_integrity_strategy": "official_parent_nofollow_forced_overwrite_v1",
            "official_parent_forced_overwrite": True,
            "official_parent_nofollow": True,
            "reliability_guard_security_sandbox": False,
        }

    @staticmethod
    def _utc_timestamp() -> str:
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    @staticmethod
    def _validate_preflight_tasks(
        tasks: Sequence[PublicTaskIdentity],
    ) -> tuple[PublicTaskIdentity, ...]:
        if isinstance(tasks, str | bytes) or not isinstance(tasks, Sequence):
            raise ValueError("preflight tasks must be a sequence")
        identities = tuple(tasks)
        if not 1 <= len(identities) <= MAX_PREFLIGHT_TASK_COUNT or any(
            not isinstance(task, PublicTaskIdentity) for task in identities
        ):
            raise ValueError("preflight requires between one and 164 task identities")
        if len({task.task_id for task in identities}) != len(identities):
            raise ValueError("preflight task IDs must be unique")
        return identities

    def _require_verified_task(self, task: PublicTaskIdentity) -> None:
        if not isinstance(task, PublicTaskIdentity):
            raise TypeError("task must be PublicTaskIdentity")
        if self._verified_tasks is None or self._verified_tasks.get(task.task_id) != task:
            raise DockerRunnerError("image_mismatch", "task has not passed EvalPlus preflight")

    def _container_name(self, purpose: str) -> str:
        return f"tracejudge-evalplus-{purpose}-{self._uuid_factory().hex}"

    def _task_error(
        self,
        problem_id: str,
        container_name: str,
        started: float,
        error_type: str,
        diagnostic: str,
        *,
        cleanup_status: Literal["not_needed", "removed", "not_found", "failed"] = "not_needed",
    ) -> TaskExecutionResult:
        return TaskExecutionResult(
            problem_id=problem_id,
            infrastructure_status="error",
            raw_result_path=None,
            duration_seconds=max(0.0, self._clock() - started),
            error_type=error_type,
            container_name=container_name,
            cleanup_status=cleanup_status,
            diagnostic=diagnostic[:300],
        )

    def _cleanup_task_error(
        self,
        problem_id: str,
        container_name: str,
        started: float,
        error_type: str,
        diagnostic: str,
    ) -> TaskExecutionResult:
        cleanup_status = self._force_remove(container_name)
        if cleanup_status == "failed":
            error_type = "container_cleanup_failed"
            diagnostic = "EvalPlus task container cleanup failed"
        else:
            self._deactivate_container(container_name)
        return self._task_error(
            problem_id,
            container_name,
            started,
            error_type,
            diagnostic,
            cleanup_status=cleanup_status,
        )


DockerRunner = EvalPlusDockerRunner

__all__ = [
    "DEFAULT_EVALPLUS_IMAGE",
    "DEFAULT_PLATFORM",
    "DockerLimits",
    "DockerRunner",
    "DockerRunnerError",
    "EVALPLUS_COMMIT",
    "EVALPLUS_EVALUATE_PY_SHA256",
    "EVALPLUS_VERSION",
    "EvalPlusDockerRunner",
    "HUMANEVAL_PLUS_VERSION",
    "ImageInspection",
    "PreflightResult",
    "PublicTaskIdentity",
    "RuntimeMetadata",
    "TaskExecutionResult",
]
