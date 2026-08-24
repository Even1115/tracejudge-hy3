from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import types
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from tracejudge_hy3.dataset.loader import load_problems
from tracejudge_hy3.evalplus import container_entrypoint
from tracejudge_hy3.evalplus import docker_runner as docker_runner_module
from tracejudge_hy3.evalplus.docker_runner import (
    DEFAULT_EVALPLUS_IMAGE,
    DEFAULT_PLATFORM,
    EVALPLUS_COMMIT,
    EVALPLUS_VERSION,
    HUMANEVAL_PLUS_VERSION,
    IMAGE_PYTHON_VERSION,
    DockerLimits,
    EvalPlusDockerRunner,
    PublicTaskIdentity,
)
from tracejudge_hy3.evalplus.schemas import EvalPlusSample, HumanEvalPlusTaskMetadata

DATASET_MD5 = "a" * 32
OVERRIDE_HASH = "e" * 32
DATASET_SHA256 = "b" * 64
DATASET_FILE_SHA256 = "d" * 64
DATASET_FILE_SIZE = 12_345
IMAGE_ID = "sha256:" + "c" * 64
HIDDEN_CANARY = "PRIVATE_EVALPLUS_FAILURE_INPUT_MUST_NOT_REACH_LOGS"
CANDIDATE_CANARY = "CANDIDATE_SOURCE_MUST_NOT_REACH_INFRASTRUCTURE_LOGS"
REPOSITORY = Path(__file__).resolve().parents[1]
PILOT_PROBLEMS = REPOSITORY / "artifacts/datasets/processed/humanevalplus-pilot-10/problems.jsonl"


def _completed(command: list[str], stdout: str = "", *, returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=stderr)


def _runtime_payload(*, machine: str = "x86_64") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "mode": "inspect",
        "status": "ok",
        "evalplus_version": EVALPLUS_VERSION,
        "evalplus_commit": EVALPLUS_COMMIT,
        "humaneval_plus_version": HUMANEVAL_PLUS_VERSION,
        "official_dataset_hash": DATASET_MD5,
        "official_dataset_file_sha256": DATASET_FILE_SHA256,
        "official_dataset_file_sha256_basis": "exact_bytes_from_pinned_release_ready_path",
        "official_dataset_file_size_bytes": DATASET_FILE_SIZE,
        "native_dataset_canonical_sha256": DATASET_SHA256,
        "native_dataset_sha256_basis": (
            "loaded_native_corpus_canonical_json_utf8_sort_keys_compact"
        ),
        "dataset_task_count": 164,
        "verified_task_count": 10,
        "python_version": IMAGE_PYTHON_VERSION,
        "python_implementation": "CPython",
        "platform_system": "Linux",
        "platform_machine": machine,
        "output_integrity_strategy": "official_parent_nofollow_forced_overwrite_v1",
        "official_parent_forced_overwrite": True,
        "official_parent_nofollow": True,
        "reliability_guard_security_sandbox": False,
    }


def _raw_result(
    task_id: str,
    solution: str,
    *,
    official_override_hash: str = OVERRIDE_HASH,
) -> dict[str, Any]:
    return {
        "date": "2026-08-24 12:00",
        "hash": official_override_hash,
        "eval": {
            task_id: [
                {
                    "task_id": task_id,
                    "solution": solution,
                    "base_status": "pass",
                    "plus_status": "fail",
                    "base_fail_tests": [],
                    "plus_fail_tests": [[HIDDEN_CANARY]],
                }
            ]
        },
    }


class _UUIDSequence:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> uuid.UUID:
        self.value += 1
        return uuid.UUID(int=self.value)


class _FakeDocker:
    def __init__(self, *, architecture: str = "amd64") -> None:
        self.architecture = architecture
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.requests: list[dict[str, Any]] = []
        self.samples: list[dict[str, Any]] = []
        self.control_sample_bytes: list[bytes] = []
        self.mount_specs: list[str] = []
        self.output_mount_specs: list[str] = []
        self.mounts_within_workspace: list[bool] = []
        self.control_modes: list[dict[str, int]] = []
        self.output_modes: list[tuple[int, int]] = []
        self.output_owners: list[tuple[int, int]] = []
        self.copied_result_inodes: list[tuple[int, int]] = []
        self.container_results: dict[str, bytes] = {}
        self.container_controls: dict[str, str] = {}
        self.removed_containers: set[str] = set()
        self.expected_workspace: Path | None = None
        self.timeout_task = False
        self.malformed_task_control = False
        self.control_override_hash = OVERRIDE_HASH
        self.raw_override_hash = OVERRIDE_HASH
        self.cleanup_returncode = 0
        self.cleanup_stderr = ""
        self.cleanup_sequence: list[tuple[int, str]] = []
        self.block_task = False
        self.block_start = False
        self.task_started = threading.Event()
        self.task_release = threading.Event()
        self.start_requested = threading.Event()
        self.start_release = threading.Event()

    def __call__(self, command: list[str], **kwargs: Any):
        command = list(command)
        self.calls.append((command, dict(kwargs)))
        if command[:2] == ["docker", "info"]:
            return _completed(command, "26.1.0\n")
        if command[:3] == ["docker", "image", "inspect"]:
            metadata = (
                f"{IMAGE_ID}\t{json.dumps([DEFAULT_EVALPLUS_IMAGE])}\tlinux\t{self.architecture}\n"
            )
            return _completed(command, metadata)
        if command[:3] == ["docker", "rm", "-f"]:
            self.task_release.set()
            container_name = command[-1]
            self.removed_containers.add(container_name)
            if self.cleanup_sequence:
                cleanup_returncode, cleanup_stderr = self.cleanup_sequence.pop(0)
            else:
                cleanup_returncode, cleanup_stderr = (
                    self.cleanup_returncode,
                    self.cleanup_stderr,
                )
            if cleanup_returncode == 0:
                self.container_results.pop(container_name, None)
                self.container_controls.pop(container_name, None)
            return _completed(
                command,
                returncode=cleanup_returncode,
                stderr=cleanup_stderr,
            )
        if command[:2] == ["docker", "wait"]:
            container_name = command[2]
            if self.timeout_task:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            if self.block_task:
                self.task_started.set()
                if not self.task_release.wait(timeout=5):
                    raise AssertionError("fake task was not released")
                exit_code = 137 if container_name in self.removed_containers else 0
                return _completed(command, f"{exit_code}\n")
            if container_name not in self.container_controls:
                return _completed(command, returncode=1)
            return _completed(command, "0\n")
        if command[:2] == ["docker", "cp"]:
            raise AssertionError("exact output file binds must not use docker cp")
        if command[:2] != ["docker", "run"]:
            raise AssertionError("unexpected fake Docker command")

        mount_values = [
            command[index + 1] for index, argument in enumerate(command) if argument == "--mount"
        ]
        control_suffix = ",dst=/control,ro"
        control_mount = next(value for value in mount_values if value.endswith(control_suffix))
        output_mounts = [value for value in mount_values if value != control_mount]
        prefix = "type=bind,src="
        control = Path(control_mount.removeprefix(prefix).removesuffix(control_suffix))
        self.mount_specs.append(control_mount)
        self.output_mount_specs.extend(output_mounts)
        if self.expected_workspace is not None:
            try:
                control.resolve().relative_to(self.expected_workspace.resolve())
            except ValueError:
                self.mounts_within_workspace.append(False)
            else:
                self.mounts_within_workspace.append(True)

        mode = command[command.index("/control/entrypoint.py") + 1]
        expected_names = {"entrypoint.py", "request.json"}
        if mode == "run":
            expected_names.add("sample.jsonl")
        self.control_modes.append(
            {
                "directory": control.stat().st_mode & 0o777,
                **{name: (control / name).stat().st_mode & 0o777 for name in expected_names},
            }
        )
        request = json.loads((control / "request.json").read_text(encoding="utf-8"))
        self.requests.append(request)
        if mode == "inspect":
            assert output_mounts == []
            return _completed(command, json.dumps(_runtime_payload()) + "\n")
        if mode != "run":
            raise AssertionError("unexpected entrypoint mode")
        if self.block_start:
            self.start_requested.set()
            if not self.start_release.wait(timeout=5):
                raise AssertionError("fake container creation was not released")
        output_by_target: dict[str, Path] = {}
        for mount in output_mounts:
            prefix = "type=bind,src="
            source, target = mount.removeprefix(prefix).split(",dst=", 1)
            output_by_target[target] = Path(source)
        assert set(output_by_target) == {
            "/output/control.json",
            "/output/sample_eval_results.json",
        }
        result_output = output_by_target["/output/sample_eval_results.json"]
        control_output = output_by_target["/output/control.json"]
        self.output_modes.append(
            (result_output.stat().st_mode & 0o777, control_output.stat().st_mode & 0o777)
        )
        self.output_owners.append((result_output.stat().st_uid, control_output.stat().st_uid))
        sample_bytes = (control / "sample.jsonl").read_bytes()
        self.control_sample_bytes.append(sample_bytes)
        sample = json.loads(sample_bytes)
        self.samples.append(sample)
        container_name = command[command.index("--name") + 1]
        self.container_results[container_name] = json.dumps(
            _raw_result(
                sample["task_id"],
                sample["solution"],
                official_override_hash=self.raw_override_hash,
            )
        ).encode("utf-8")
        if self.malformed_task_control:
            self.container_controls[container_name] = HIDDEN_CANARY + "\n"
        else:
            self.container_controls[container_name] = (
                json.dumps(
                    {
                        "schema_version": 1,
                        "mode": "run",
                        "status": "ok",
                        "task_id": sample["task_id"],
                        "official_override_hash": self.control_override_hash,
                        "result_available": True,
                    }
                )
                + "\n"
            )
        result_output.write_bytes(self.container_results[container_name])
        control_output.write_text(self.container_controls[container_name], encoding="utf-8")
        result_metadata = result_output.stat()
        self.copied_result_inodes.append((result_metadata.st_dev, result_metadata.st_ino))
        return _completed(command, "fake-container-id\n")


def _metadata() -> tuple[HumanEvalPlusTaskMetadata, ...]:
    return tuple(
        HumanEvalPlusTaskMetadata(
            problem_id=f"HumanEval/{index}",
            prompt_sha256=hashlib.sha256(f"prompt-{index}".encode()).hexdigest(),
            entry_point=f"candidate_{index}",
        )
        for index in range(10)
    )


def _runner(fake: _FakeDocker) -> EvalPlusDockerRunner:
    return EvalPlusDockerRunner(
        command_runner=fake,
        which=lambda executable: "/usr/bin/docker" if executable == "docker" else None,
        uuid_factory=_UUIDSequence(),
    )


def _docker_runs(fake: _FakeDocker) -> list[list[str]]:
    return [command for command, _kwargs in fake.calls if command[:2] == ["docker", "run"]]


def test_public_identity_records_exact_reproducibility_and_isolation_pins():
    identity = dict(EvalPlusDockerRunner(which=lambda _executable: None).public_identity())

    assert identity["image"] == DEFAULT_EVALPLUS_IMAGE
    assert identity["image_digest"] in DEFAULT_EVALPLUS_IMAGE
    assert identity["requested_platform"] == "linux/amd64"
    assert identity["evalplus_version"] == "0.3.1"
    assert identity["evalplus_commit"] == EVALPLUS_COMMIT
    assert identity["humaneval_plus_version"] == "v0.1.10"
    assert identity["python_version"] == "3.11.10"
    assert identity["official_dataset_hash"] is None
    assert identity["official_command"] == {
        "dataset": "humaneval",
        "parallel": 1,
        "min_time_limit_seconds": 4.0,
        "gt_time_limit_factor": 4.0,
        "test_details": True,
    }
    assert identity["isolation"]["pull_policy"] == "never"
    assert identity["isolation"]["network"] == "none"
    assert identity["isolation"]["log_driver"] == "none"
    assert identity["isolation"]["read_only_root_filesystem"] is True
    assert identity["isolation"]["control_mount_read_only"] is True
    assert identity["isolation"]["host_writable_bind_mounts"] is True
    assert identity["isolation"]["writable_mount_scope"] == ("two_exact_precreated_host_files")
    assert identity["isolation"]["output_transport"] == "exact_host_file_binds"
    assert identity["isolation"]["output_directory_mounted"] is False
    assert identity["isolation"]["hard_disk_quota"] is False
    assert identity["isolation"]["per_file_fsize_limit"] == "128MiB"
    assert identity["isolation"]["output_copy_policy"] == (
        "read_exact_files_after_container_exit_zero"
    )
    assert identity["isolation"]["security_boundary"] == "basic_non_adversarial"
    assert identity["isolation"]["adversarial_candidate_integrity_guarantee"] is False
    assert identity["isolation"]["reliability_guard_security_sandbox"] is False
    assert identity["isolation"]["docker_socket_mounted"] is False
    assert identity["isolation"]["host_environment_forwarded"] is False


def test_preflight_uses_only_hardened_amd64_container_and_records_runtime(tmp_path: Path):
    fake = _FakeDocker()
    fake.expected_workspace = tmp_path
    runner = _runner(fake)

    result = runner.preflight(task_metadata=_metadata(), workspace=tmp_path)

    assert result.ready is True
    runtime = result.runtime
    assert runtime["requested_platform"] == DEFAULT_PLATFORM
    assert runtime["image"]["platform"] == "linux/amd64"
    assert runtime["runtime"]["platform_system"] == "Linux"
    assert runtime["runtime"]["platform_machine"] == "x86_64"
    assert runtime["runtime"]["python_version"] == IMAGE_PYTHON_VERSION
    assert runtime["runtime"]["official_dataset_hash"] == DATASET_MD5
    assert runtime["runtime"]["native_dataset_canonical_sha256"] == DATASET_SHA256
    assert runtime["runtime"]["official_dataset_file_sha256"] == DATASET_FILE_SHA256
    assert runtime["runtime"]["official_dataset_file_size_bytes"] == DATASET_FILE_SIZE
    assert runtime["host_system"]
    assert runtime["host_machine"]
    assert fake.mounts_within_workspace == [True]
    assert len(fake.requests) == 1
    assert set(fake.requests[0]) == {"schema_version", "tasks"}
    assert len(fake.requests[0]["tasks"]) == 10

    command = _docker_runs(fake)[0]
    assert command[command.index("--pull") + 1] == "never"
    assert command[command.index("--platform") + 1] == "linux/amd64"
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--log-driver") + 1] == "none"
    assert "--read-only" in command
    assert command[command.index("--memory") + 1] == "4g"
    assert command[command.index("--memory-swap") + 1] == "4g"
    assert command[command.index("--cpus") + 1] == "1"
    assert command[command.index("--pids-limit") + 1] == "128"
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert command[command.index("--security-opt") + 1] == "no-new-privileges"
    assert command[command.index("--tmpfs") + 1].startswith("/tmp:rw,noexec,nosuid,nodev,size=1g")
    assert command.count("--mount") == 1
    control_mount = fake.mount_specs[0]
    assert control_mount.endswith(",dst=/control,ro")
    assert "dst=/output" not in control_mount
    tmpfs_values = [
        command[index + 1] for index, argument in enumerate(command) if argument == "--tmpfs"
    ]
    assert all(not value.startswith("/output:") for value in tmpfs_values)
    assert "/workspace" not in control_mount
    assert command[command.index("--workdir") + 1] == "/tmp"
    assert DEFAULT_EVALPLUS_IMAGE in command
    assert "--env-file" not in command and "-e" not in command
    cleared_environment = [
        command[index + 1] for index, argument in enumerate(command) if argument == "--env"
    ]
    assert cleared_environment == [
        "HTTP_PROXY=",
        "HTTPS_PROXY=",
        "ALL_PROXY=",
        "NO_PROXY=",
        "http_proxy=",
        "https_proxy=",
        "all_proxy=",
        "no_proxy=",
    ]
    serialized_command = " ".join(command)
    assert "/var/run/docker.sock" not in serialized_command
    assert ".env" not in serialized_command

    postflight_identity = dict(runner.public_identity())
    assert postflight_identity["official_dataset_hash"] == DATASET_MD5
    assert postflight_identity["native_dataset_canonical_sha256"] == DATASET_SHA256
    assert postflight_identity["official_dataset_file_sha256"] == DATASET_FILE_SHA256


def test_preflight_rejects_non_amd64_image_with_safe_failure(tmp_path: Path):
    fake = _FakeDocker(architecture="arm64")
    runner = _runner(fake)

    result = runner.preflight(task_metadata=_metadata(), workspace=tmp_path)

    assert result.ready is False
    assert result.infrastructure_error_type == "image_mismatch"
    assert result.runtime["requested_platform"] == "linux/amd64"
    assert result.runtime["host_machine"]
    assert HIDDEN_CANARY not in json.dumps(result.runtime)
    assert _docker_runs(fake) == []


def test_protocol_run_returns_private_raw_dict_without_exposing_it_in_diagnostics(
    tmp_path: Path,
):
    fake = _FakeDocker()
    fake.expected_workspace = tmp_path
    runner = _runner(fake)
    metadata = _metadata()
    assert runner.preflight(task_metadata=metadata, workspace=tmp_path).ready is True
    sample = EvalPlusSample(
        task_id=metadata[0].problem_id,
        solution=f"def candidate_0():\n    return {CANDIDATE_CANARY!r}\n",
    )

    outcome = runner.run_task(sample=sample, task_metadata=metadata[0], workspace=tmp_path)

    assert outcome.problem_id == "HumanEval/0"
    assert outcome.infrastructure_error_type is None
    assert outcome.raw_result is not None
    assert outcome.raw_result["eval"]["HumanEval/0"][0]["plus_fail_tests"] == [[HIDDEN_CANARY]]
    assert outcome.started_at.endswith("Z") and outcome.ended_at.endswith("Z")
    assert outcome.duration_seconds >= 0
    assert outcome.diagnostics == {"cleanup_status": "removed"}
    assert fake.samples == [sample.model_dump()]
    task_command = _docker_runs(fake)[1]
    assert task_command[task_command.index("--platform") + 1] == "linux/amd64"
    assert task_command[task_command.index("--pull") + 1] == "never"
    assert CANDIDATE_CANARY not in " ".join(task_command)
    assert HIDDEN_CANARY not in " ".join(task_command)
    assert "-d" in task_command and "--rm" not in task_command
    task_mounts = [
        task_command[index + 1]
        for index, argument in enumerate(task_command)
        if argument == "--mount"
    ]
    assert len(task_mounts) == 3
    assert task_mounts[0] == fake.mount_specs[1]
    assert task_mounts[0].endswith(",dst=/control,ro")
    assert task_mounts[1].startswith("type=bind,src=")
    assert task_mounts[1].endswith(",dst=/output/sample_eval_results.json")
    assert task_mounts[2].startswith("type=bind,src=")
    assert task_mounts[2].endswith(",dst=/output/control.json")
    assert all(",dst=/output," not in mount for mount in task_mounts)
    assert task_command[task_command.index("--ulimit") + 1] == ("fsize=134217728:134217728")
    assert task_command[-5:] == [
        "run",
        "/control/request.json",
        "/control/sample.jsonl",
        "/output/sample_eval_results.json",
        "/output/control.json",
    ]
    assert fake.control_sample_bytes == [
        (
            json.dumps(
                {"task_id": sample.task_id, "solution": sample.solution},
                ensure_ascii=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    ]
    raw_path = tmp_path / "official_evalplus_raw_result.json"
    assert raw_path.is_file()
    assert raw_path.stat().st_mode & 0o777 == 0o600
    assert raw_path.stat().st_uid == os.geteuid()
    assert (raw_path.stat().st_dev, raw_path.stat().st_ino) != fake.copied_result_inodes[0]
    assert fake.output_modes == [(0o666, 0o666)]
    assert fake.output_owners == [(os.geteuid(), os.geteuid())]
    assert fake.control_modes == [
        {"directory": 0o555, "entrypoint.py": 0o444, "request.json": 0o444},
        {
            "directory": 0o555,
            "entrypoint.py": 0o444,
            "request.json": 0o444,
            "sample.jsonl": 0o444,
        },
    ]
    wait_index = next(
        index
        for index, (command, _kwargs) in enumerate(fake.calls)
        if command[:2] == ["docker", "wait"]
    )
    cleanup_index = next(
        index
        for index, (command, _kwargs) in enumerate(fake.calls)
        if index > wait_index and command[:3] == ["docker", "rm", "-f"]
    )
    assert cleanup_index > wait_index
    assert not any(command[:2] == ["docker", "cp"] for command, _kwargs in fake.calls)


def test_outer_task_timeout_forces_named_container_removal_and_is_safely_classified(
    tmp_path: Path,
):
    fake = _FakeDocker()
    runner = _runner(fake)
    metadata = _metadata()
    assert runner.preflight(task_metadata=metadata, workspace=tmp_path).ready is True
    fake.timeout_task = True
    sample = EvalPlusSample(task_id="HumanEval/0", solution="def candidate_0():\n    return 0\n")

    outcome = runner.run_task(sample=sample, task_metadata=metadata[0], workspace=tmp_path)

    assert outcome.infrastructure_error_type == "container_timeout"
    assert outcome.raw_result is None
    task_run = _docker_runs(fake)[1]
    container_name = task_run[task_run.index("--name") + 1]
    cleanup_commands = [
        command for command, _kwargs in fake.calls if command[:3] == ["docker", "rm", "-f"]
    ]
    assert ["docker", "rm", "-f", "-v", container_name] in cleanup_commands
    assert outcome.diagnostics == {"cleanup_status": "removed"}
    task_call_kwargs = next(
        kwargs for command, kwargs in fake.calls if command[:2] == ["docker", "wait"]
    )
    assert 0 < task_call_kwargs["timeout"] <= DockerLimits().per_task_timeout_seconds
    assert CANDIDATE_CANARY not in json.dumps(outcome.diagnostics)


def test_malformed_control_output_is_bounded_never_returned_and_forces_cleanup(tmp_path: Path):
    fake = _FakeDocker()
    runner = _runner(fake)
    metadata = _metadata()
    assert runner.preflight(task_metadata=metadata, workspace=tmp_path).ready is True
    fake.malformed_task_control = True
    sample = EvalPlusSample(task_id="HumanEval/0", solution="def candidate_0():\n    return 0\n")

    outcome = runner.run_task(sample=sample, task_metadata=metadata[0], workspace=tmp_path)

    assert outcome.infrastructure_error_type == "executor_error"
    assert HIDDEN_CANARY not in json.dumps(outcome.diagnostics)
    assert any(command[:3] == ["docker", "rm", "-f"] for command, _kwargs in fake.calls)


def test_host_rejects_raw_hash_that_differs_from_safe_override_identity(tmp_path: Path):
    fake = _FakeDocker()
    runner = _runner(fake)
    metadata = _metadata()
    assert runner.preflight(task_metadata=metadata, workspace=tmp_path).ready is True
    fake.raw_override_hash = "f" * 32
    sample = EvalPlusSample(task_id="HumanEval/0", solution="    return 0\n")

    outcome = runner.run_task(sample=sample, task_metadata=metadata[0], workspace=tmp_path)

    assert outcome.infrastructure_error_type == "invalid_raw_result"
    assert outcome.raw_result is None
    assert outcome.diagnostics == {"cleanup_status": "removed"}


def test_cancel_all_force_removes_active_task_and_prevents_late_starts(tmp_path: Path):
    fake = _FakeDocker()
    runner = _runner(fake)
    metadata = _metadata()
    assert runner.preflight(task_metadata=metadata, workspace=tmp_path).ready is True
    fake.block_task = True
    task_workspace = tmp_path / "active-task"
    task_workspace.mkdir()
    sample = EvalPlusSample(task_id="HumanEval/0", solution="    return 0\n")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            runner.run_task,
            sample=sample,
            task_metadata=metadata[0],
            workspace=task_workspace,
        )
        assert fake.task_started.wait(timeout=2)
        cleanup = runner.cancel_all()
        assert len(cleanup) == 1
        assert set(cleanup.values()) == {"removed"}
        outcome = future.result(timeout=2)

    assert outcome.infrastructure_error_type == "container_timeout"
    task_run = _docker_runs(fake)[1]
    container_name = task_run[task_run.index("--name") + 1]
    assert ["docker", "rm", "-f", "-v", container_name] in [
        command for command, _kwargs in fake.calls
    ]

    run_count = len(_docker_runs(fake))
    late_workspace = tmp_path / "late-task"
    late_workspace.mkdir()
    late = runner.run_task(
        sample=sample,
        task_metadata=metadata[0],
        workspace=late_workspace,
    )
    assert late.infrastructure_error_type == "container_timeout"
    assert len(_docker_runs(fake)) == run_count


def test_wait_completes_before_host_publishes_any_bound_result(tmp_path: Path):
    fake = _FakeDocker()
    runner = _runner(fake)
    metadata = _metadata()
    assert runner.preflight(task_metadata=metadata, workspace=tmp_path).ready is True
    fake.block_task = True
    task_workspace = tmp_path / "wait-before-trust"
    task_workspace.mkdir()
    sample = EvalPlusSample(task_id="HumanEval/0", solution="    return 0\n")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            runner.run_task,
            sample=sample,
            task_metadata=metadata[0],
            workspace=task_workspace,
        )
        assert fake.task_started.wait(timeout=2)
        assert not (task_workspace / "official_evalplus_raw_result.json").exists()
        fake.task_release.set()
        outcome = future.result(timeout=2)

    assert outcome.infrastructure_error_type is None
    assert (task_workspace / "official_evalplus_raw_result.json").is_file()


def test_cancel_during_docker_create_removes_late_created_container(tmp_path: Path):
    fake = _FakeDocker()
    runner = _runner(fake)
    metadata = _metadata()
    assert runner.preflight(task_metadata=metadata, workspace=tmp_path).ready is True
    fake.block_start = True
    task_workspace = tmp_path / "cancel-create-race"
    task_workspace.mkdir()
    sample = EvalPlusSample(task_id="HumanEval/0", solution="    return 0\n")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            runner.run_task,
            sample=sample,
            task_metadata=metadata[0],
            workspace=task_workspace,
        )
        assert fake.start_requested.wait(timeout=2)
        cleanup = runner.cancel_all()
        assert len(cleanup) == 1
        assert set(cleanup.values()) == {"removed"}
        fake.start_release.set()
        outcome = future.result(timeout=2)

    assert outcome.infrastructure_error_type == "container_timeout"
    task_run = _docker_runs(fake)[1]
    container_name = task_run[task_run.index("--name") + 1]
    removals = [
        command
        for command, _kwargs in fake.calls
        if command == ["docker", "rm", "-f", "-v", container_name]
    ]
    assert len(removals) >= 2


@pytest.mark.parametrize(
    ("returncode", "stderr", "expected"),
    [
        (0, "", "removed"),
        (1, "Error response from daemon: No such container: safe-name", "not_found"),
        (1, HIDDEN_CANARY, "failed"),
    ],
)
def test_force_remove_returns_only_allowlisted_cleanup_status(
    returncode: int,
    stderr: str,
    expected: str,
):
    fake = _FakeDocker()
    fake.cleanup_returncode = returncode
    fake.cleanup_stderr = stderr
    runner = _runner(fake)

    assert runner._force_remove("safe-name") == expected


def test_cancel_all_returns_each_failed_cleanup_instead_of_discarding_it():
    fake = _FakeDocker()
    fake.cleanup_returncode = 1
    fake.cleanup_stderr = HIDDEN_CANARY
    runner = _runner(fake)
    assert runner._activate_container("safe-one") is True
    assert runner._activate_container("safe-two") is True

    cleanup = runner.cancel_all()

    assert cleanup == {"safe-one": "failed", "safe-two": "failed"}
    assert runner._active_containers == {"safe-one", "safe-two"}
    assert HIDDEN_CANARY not in json.dumps(cleanup)


def test_cleanup_failure_is_visible_without_returning_docker_stderr(tmp_path: Path):
    fake = _FakeDocker()
    runner = _runner(fake)
    metadata = _metadata()
    assert runner.preflight(task_metadata=metadata, workspace=tmp_path).ready is True
    fake.timeout_task = True
    fake.cleanup_sequence = [(1, HIDDEN_CANARY), (0, "")]
    sample = EvalPlusSample(task_id="HumanEval/0", solution="    return 0\n")

    outcome = runner.run_task(sample=sample, task_metadata=metadata[0], workspace=tmp_path)

    assert outcome.infrastructure_error_type == "container_cleanup_failed"
    assert outcome.diagnostics == {"cleanup_status": "failed"}
    assert HIDDEN_CANARY not in json.dumps(outcome.diagnostics)
    task_run = _docker_runs(fake)[1]
    container_name = task_run[task_run.index("--name") + 1]
    assert container_name in runner._active_containers
    assert runner.cancel_all() == {container_name: "removed"}
    assert container_name not in runner._active_containers


def test_native_subprocess_reader_caps_both_control_streams():
    command = [
        sys.executable,
        "-c",
        "import os; os.write(1, b'x' * 20000); os.write(2, b'y' * 20000)",
    ]

    with pytest.raises(docker_runner_module._OutputLimitExceeded):
        EvalPlusDockerRunner._bounded_subprocess_run(command, timeout=5)


def test_native_subprocess_keyboard_interrupt_kills_and_reaps_client(
    monkeypatch: pytest.MonkeyPatch,
):
    class InterruptingProcess:
        def __init__(self) -> None:
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()
            self.wait_count = 0
            self.killed = False

        def wait(self, timeout: float | None = None) -> int:
            self.wait_count += 1
            if self.wait_count == 1:
                raise KeyboardInterrupt
            return -9

        def kill(self) -> None:
            self.killed = True

        def poll(self) -> int | None:
            return -9 if self.killed else None

    process = InterruptingProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(KeyboardInterrupt):
        EvalPlusDockerRunner._bounded_subprocess_run(["docker", "info"], timeout=5)

    assert process.killed is True
    assert process.wait_count == 2


def test_unexpected_preflight_interrupt_force_removes_known_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake = _FakeDocker()
    runner = _runner(fake)
    identities = tuple(
        PublicTaskIdentity(
            task_id=item.problem_id,
            prompt_sha256=item.prompt_sha256,
            entry_point=item.entry_point,
        )
        for item in _metadata()
    )

    def interrupt(*args: Any, **kwargs: Any):
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "_invoke_container", interrupt)

    with pytest.raises(KeyboardInterrupt):
        runner.preflight_tasks(tmp_path, identities)

    assert any(
        command[:4] == ["docker", "rm", "-f", "-v"]
        and command[-1].startswith("tracejudge-evalplus-inspect-")
        for command, _kwargs in fake.calls
    )


def test_output_limit_forces_named_cleanup_without_exposing_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake = _FakeDocker()
    runner = _runner(fake)
    metadata = _metadata()
    assert runner.preflight(task_metadata=metadata, workspace=tmp_path).ready is True
    original_invoke = runner._invoke

    def limited_invoke(command: list[str], *, timeout: float):
        if command[:2] == ["docker", "run"]:
            raise docker_runner_module._OutputLimitExceeded(HIDDEN_CANARY)
        return original_invoke(command, timeout=timeout)

    monkeypatch.setattr(runner, "_invoke", limited_invoke)
    sample = EvalPlusSample(task_id="HumanEval/0", solution="    return 0\n")

    outcome = runner.run_task(sample=sample, task_metadata=metadata[0], workspace=tmp_path)

    assert outcome.infrastructure_error_type == "executor_error"
    assert outcome.diagnostics == {"cleanup_status": "removed"}
    assert HIDDEN_CANARY not in json.dumps(outcome.diagnostics)
    assert any(command[:3] == ["docker", "rm", "-f"] for command, _kwargs in fake.calls)


def test_run_sample_interrupt_retries_failed_cleanup_via_cancel_all(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake = _FakeDocker()
    runner = _runner(fake)
    metadata = _metadata()
    assert runner.preflight(task_metadata=metadata, workspace=tmp_path).ready is True
    fake.cleanup_sequence = [(1, HIDDEN_CANARY), (0, "")]
    task = PublicTaskIdentity(
        task_id=metadata[0].problem_id,
        prompt_sha256=metadata[0].prompt_sha256,
        entry_point=metadata[0].entry_point,
    )
    sample_path = tmp_path / "direct-sample.jsonl"
    sample_path.write_text(
        json.dumps({"task_id": task.task_id, "solution": "    return 0\n"}) + "\n",
        encoding="utf-8",
    )
    original_invoke = runner._invoke

    def interrupt_task(command: list[str], *, timeout: float):
        if command[:2] == ["docker", "run"]:
            raise KeyboardInterrupt
        return original_invoke(command, timeout=timeout)

    monkeypatch.setattr(runner, "_invoke", interrupt_task)

    with pytest.raises(KeyboardInterrupt):
        runner.run_sample_file(
            tmp_path,
            task,
            sample_path,
            tmp_path / "interrupted-raw.json",
        )

    cleanup_command = next(
        command for command, _kwargs in fake.calls if command[:4] == ["docker", "rm", "-f", "-v"]
    )
    container_name = cleanup_command[-1]
    assert container_name in runner._active_containers

    cleanup = runner.cancel_all()

    assert cleanup == {container_name: "removed"}
    assert container_name not in runner._active_containers
    assert HIDDEN_CANARY not in json.dumps(cleanup)


def test_missing_docker_preflight_returns_stable_safe_runtime_identity(tmp_path: Path):
    runner = EvalPlusDockerRunner(which=lambda _executable: None)

    result = runner.preflight(task_metadata=_metadata(), workspace=tmp_path)

    assert result.ready is False
    assert result.infrastructure_error_type == "docker_unavailable"
    assert result.runtime["image"] == DEFAULT_EVALPLUS_IMAGE
    assert result.runtime["evalplus_commit"] == EVALPLUS_COMMIT
    assert result.runtime["native_dataset_canonical_sha256"] is None


@pytest.mark.parametrize(
    "entrypoint_error",
    [
        "runtime_unavailable",
        "runtime_identity_mismatch",
        "dataset_identity_mismatch",
        "task_identity_mismatch",
    ],
)
def test_safe_preflight_identity_failures_map_to_image_mismatch(entrypoint_error: str):
    runner = EvalPlusDockerRunner(which=lambda _executable: None)
    output = json.dumps({"schema_version": 1, "status": "error", "error_type": entrypoint_error})

    assert runner._preflight_error_type(output) == "image_mismatch"
    assert runner._preflight_error_type(HIDDEN_CANARY) == "executor_error"


def test_container_entrypoint_has_only_standard_library_static_imports():
    source_path = Path(container_entrypoint.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])

    assert imported_roots <= sys.stdlib_module_names | {"__future__"}
    assert "evalplus" not in imported_roots
    assert "tracejudge_hy3" not in imported_roots


def test_container_entrypoint_hashes_exact_ready_release_file_bytes(tmp_path: Path):
    release_bytes = b'{"task_id":"HumanEval/0"}\n'
    release_path = tmp_path / "HumanEvalPlus.jsonl"
    release_path.write_bytes(release_bytes)

    digest, size = container_entrypoint._official_file_sha256(str(release_path))

    assert digest == hashlib.sha256(release_bytes).hexdigest()
    assert size == len(release_bytes)


def test_container_inspect_verifies_ten_public_identities_without_disclosure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    problems = {
        f"HumanEval/{index}": {
            "task_id": f"HumanEval/{index}",
            "prompt": f"PROMPT_CANARY_{index}",
            "entry_point": f"candidate_{index}",
            "hidden": f"HIDDEN_DATA_{index}",
        }
        for index in range(164)
    }
    selected = list(problems)[:10]
    request = {
        "schema_version": 1,
        "tasks": [
            {
                "task_id": task_id,
                "prompt_sha256": hashlib.sha256(
                    problems[task_id]["prompt"].encode("utf-8")
                ).hexdigest(),
                "entry_point": problems[task_id]["entry_point"],
            }
            for task_id in selected
        ],
    }
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    monkeypatch.setattr(
        container_entrypoint,
        "_load_official_dataset",
        lambda: (
            problems,
            DATASET_MD5,
            DATASET_FILE_SHA256,
            DATASET_FILE_SIZE,
            DATASET_SHA256,
        ),
    )

    exit_code = container_entrypoint.main(["inspect", str(request_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["verified_task_count"] == 10
    assert payload["native_dataset_canonical_sha256"] == DATASET_SHA256
    assert payload["official_dataset_file_sha256"] == DATASET_FILE_SHA256
    assert "PROMPT_CANARY" not in captured.out
    assert "HIDDEN_DATA" not in captured.out


def test_container_run_filters_one_native_task_and_invokes_fixed_official_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    task_id = "HumanEval/8"
    prompt = "PUBLIC_PROMPT_CANARY"
    problems = {
        task_id: {
            "task_id": task_id,
            "prompt": prompt,
            "entry_point": "candidate",
            "base_input": [HIDDEN_CANARY],
        }
    }
    request = {
        "schema_version": 1,
        "task": {
            "task_id": task_id,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "entry_point": "candidate",
        },
    }
    control_dir = tmp_path / "control"
    output_dir = tmp_path / "output"
    control_dir.mkdir()
    output_dir.mkdir()
    request_path = control_dir / "request.json"
    sample_path = control_dir / "sample.jsonl"
    result_path = output_dir / "sample_eval_results.json"
    control_output_path = output_dir / "control.json"
    result_path.touch(mode=0o666)
    result_path.chmod(0o666)
    control_output_path.touch(mode=0o666)
    control_output_path.chmod(0o666)
    request_path.write_text(json.dumps(request), encoding="utf-8")
    sample_path.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "solution": f"def candidate():\n    return {CANDIDATE_CANARY!r}\n",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    original_sample_bytes = sample_path.read_bytes()
    monkeypatch.setattr(
        container_entrypoint,
        "_load_official_dataset",
        lambda: (
            problems,
            DATASET_MD5,
            DATASET_FILE_SHA256,
            DATASET_FILE_SIZE,
            DATASET_SHA256,
        ),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "HOST_SECRET_MUST_NOT_BE_FORWARDED")
    invocation: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any):
        invocation["command"] = list(command)
        invocation["kwargs"] = dict(kwargs)
        override_path = Path(kwargs["env"]["HUMANEVAL_OVERRIDE_PATH"])
        override_bytes = override_path.read_bytes()
        native_lines = override_bytes.decode("utf-8").splitlines()
        official_override_hash = hashlib.md5(
            override_bytes,
            usedforsecurity=False,
        ).hexdigest()
        invocation["native_line_count"] = len(native_lines)
        invocation["native_task_id"] = json.loads(native_lines[0])["task_id"]
        invocation["official_override_hash"] = official_override_hash
        private_sample_path = Path(command[5])
        invocation["private_sample_path"] = private_sample_path
        invocation["private_sample_bytes"] = private_sample_path.read_bytes()
        invocation["published_size_during_evaluation"] = result_path.stat().st_size
        private_result_path = private_sample_path.with_name("sample_eval_results.json")
        private_result_path.write_text(
            json.dumps(
                _raw_result(
                    task_id,
                    json.loads(original_sample_bytes)["solution"],
                    official_override_hash=official_override_hash,
                )
            ),
            encoding="utf-8",
        )
        return _completed(command)

    monkeypatch.setattr(container_entrypoint.subprocess, "run", fake_run)

    exit_code = container_entrypoint.main(
        [
            "run",
            str(request_path),
            str(sample_path),
            str(result_path),
            str(control_output_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert captured.out == ""
    control = json.loads(control_output_path.read_bytes())
    assert control == {
        "mode": "run",
        "result_available": True,
        "schema_version": 1,
        "status": "ok",
        "task_id": task_id,
        "official_override_hash": invocation["official_override_hash"],
    }
    command = invocation["command"]
    assert command[1:4] == ["-B", "-u", str(Path(container_entrypoint.__file__).resolve())]
    assert command[4] == container_entrypoint._INTERNAL_EVALUATE_MODE
    assert invocation["native_line_count"] == 1
    assert invocation["native_task_id"] == task_id
    assert invocation["private_sample_path"] != sample_path
    assert str(invocation["private_sample_path"]).startswith("/tmp/")
    assert invocation["private_sample_bytes"] == original_sample_bytes
    assert invocation["published_size_during_evaluation"] == 0
    assert sample_path.read_bytes() == original_sample_bytes
    assert result_path.is_file()
    assert json.loads(result_path.read_bytes())["hash"] == invocation["official_override_hash"]
    child_env = invocation["kwargs"]["env"]
    assert set(child_env) == {
        "HOME",
        "XDG_CACHE_HOME",
        "HUMANEVAL_OVERRIDE_PATH",
        "LANG",
        "PATH",
        "PYTHONDONTWRITEBYTECODE",
    }
    assert "OPENAI_API_KEY" not in child_env
    assert CANDIDATE_CANARY not in captured.out
    assert HIDDEN_CANARY not in captured.out
    assert "HOST_SECRET_MUST_NOT_BE_FORWARDED" not in captured.out


def test_official_parent_forcibly_overwrites_malicious_precreated_raw(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    task_id = "HumanEval/8"
    solution = "def candidate():\n    return 0\n"
    private_root = tmp_path / "private"
    private_root.mkdir()
    sample_path = private_root / "sample.jsonl"
    sample_path.write_text(
        json.dumps({"task_id": task_id, "solution": solution}) + "\n",
        encoding="utf-8",
    )
    legitimate = _raw_result(task_id, solution)
    forged = _raw_result(task_id, solution, official_override_hash="f" * 32)
    evaluator_module = types.ModuleType("evalplus.evaluate")
    evaluator_module.__dict__.update(
        {
            "json": json,
            "os": os,
            "legitimate": legitimate,
            "forged_bytes": json.dumps(forged).encode("utf-8"),
        }
    )
    exec(
        """
def evaluate(**kwargs):
    global observed_isfile, observed_kwargs
    observed_kwargs = kwargs
    result_path = kwargs["samples"].replace(".jsonl", "_eval_results.json")
    descriptor = os.open(result_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(descriptor, forged_bytes)
    os.close(descriptor)
    observed_isfile = os.path.isfile(result_path)
    with open(result_path, "w") as stream:
        json.dump(legitimate, stream)
""",
        evaluator_module.__dict__,
    )
    original_import = container_entrypoint.importlib.import_module

    def fake_import(name: str):
        if name == "evalplus.evaluate":
            return evaluator_module
        return original_import(name)

    monkeypatch.setattr(container_entrypoint.importlib, "import_module", fake_import)

    exit_code = container_entrypoint._guarded_official_evaluate(
        sample_path,
        private_root,
        tmp_path / "control",
        tmp_path / "output",
    )

    assert exit_code == 0
    assert evaluator_module.observed_isfile is False
    assert evaluator_module.observed_kwargs == {
        "dataset": "humaneval",
        "samples": str(sample_path),
        "parallel": 1,
        "min_time_limit": 4.0,
        "gt_time_limit_factor": 4.0,
        "test_details": True,
    }
    result_path = private_root / "sample_eval_results.json"
    published_bytes = result_path.read_bytes()
    assert (
        hashlib.sha256(published_bytes).hexdigest()
        == hashlib.sha256(json.dumps(legitimate).encode("utf-8")).hexdigest()
    )
    assert json.loads(published_bytes)["hash"] == OVERRIDE_HASH


def test_container_argument_errors_do_not_echo_untrusted_values(capsys: pytest.CaptureFixture[str]):
    untrusted = "ARGUMENT_CANARY_MUST_NOT_BE_ECHOED"

    exit_code = container_entrypoint.main([untrusted])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.err == ""
    assert untrusted not in captured.out
    assert json.loads(captured.out)["error_type"] == "invalid_request"


def test_public_task_identity_hashes_utf8_prompt():
    identity = PublicTaskIdentity.from_prompt(
        task_id="HumanEval/8",
        prompt="def café():\n    pass\n",
        entry_point="cafe",
    )

    assert identity.prompt_sha256 == hashlib.sha256("def café():\n    pass\n".encode()).hexdigest()


def test_constructor_refuses_unpinned_image_or_implicit_platform():
    with pytest.raises(ValueError, match="pinned official"):
        EvalPlusDockerRunner(image="ganler/evalplus:latest")
    with pytest.raises(ValueError, match="linux/amd64"):
        EvalPlusDockerRunner(requested_platform="linux/arm64")


def test_no_host_environment_is_passed_to_docker_cli_invocations(tmp_path: Path):
    fake = _FakeDocker()
    runner = _runner(fake)

    assert runner.preflight(task_metadata=_metadata(), workspace=tmp_path).ready is True

    for _command, kwargs in fake.calls:
        assert "env" not in kwargs
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["check"] is False


def test_default_platform_constant_is_explicit_amd64():
    assert DEFAULT_PLATFORM == "linux/amd64"
    assert os.path.isabs(str(Path(container_entrypoint.__file__).resolve()))


@pytest.mark.docker
@pytest.mark.skipif(
    os.environ.get("TRACEJUDGE_RUN_DOCKER_INTEGRATION") != "1",
    reason="set TRACEJUDGE_RUN_DOCKER_INTEGRATION=1 for the pinned-image preflight",
)
def test_real_pinned_image_preflight_is_explicitly_opt_in(tmp_path: Path):
    """Verify only safe runtime identity; never execute a candidate or print corpus data."""

    assert PILOT_PROBLEMS.is_file()
    problems = load_problems(PILOT_PROBLEMS)
    task_metadata = tuple(
        HumanEvalPlusTaskMetadata(
            problem_id=problem.problem_id,
            prompt_sha256=hashlib.sha256(problem.requirement.encode("utf-8")).hexdigest(),
            entry_point=problem.function_name,
        )
        for problem in problems
    )
    assert len(task_metadata) == 10

    result = EvalPlusDockerRunner().preflight(task_metadata=task_metadata, workspace=tmp_path)

    assert result.ready, result.infrastructure_error_type
    runtime = result.runtime["runtime"]
    assert result.runtime["image"]["platform"] == "linux/amd64"
    assert runtime["evalplus_version"] == "0.3.1"
    assert runtime["humaneval_plus_version"] == "v0.1.10"
    assert runtime["python_version"] == "3.11.10"
    assert runtime["verified_task_count"] == 10
    assert len(runtime["official_dataset_file_sha256"]) == 64
    assert len(runtime["native_dataset_canonical_sha256"]) == 64


@pytest.mark.docker
@pytest.mark.skipif(
    os.environ.get("TRACEJUDGE_RUN_DOCKER_INTEGRATION") != "1",
    reason="set TRACEJUDGE_RUN_DOCKER_INTEGRATION=1 for the pinned-image task smoke",
)
def test_real_pinned_image_overwrites_malicious_candidate_raw(tmp_path: Path):
    """A candidate-created pass document must be replaced by the official parent."""

    from tracejudge_hy3.evalplus.parser import parse_official_result

    problems = load_problems(PILOT_PROBLEMS)
    task_metadata = tuple(
        HumanEvalPlusTaskMetadata(
            problem_id=problem.problem_id,
            prompt_sha256=hashlib.sha256(problem.requirement.encode("utf-8")).hexdigest(),
            entry_point=problem.function_name,
        )
        for problem in problems
    )
    runner = EvalPlusDockerRunner()
    preflight_workspace = tmp_path / "preflight"
    task_workspace = tmp_path / "task"
    preflight_workspace.mkdir()
    task_workspace.mkdir()
    preflight = runner.preflight(
        task_metadata=task_metadata,
        workspace=preflight_workspace,
    )
    if not preflight.ready:
        pytest.fail(f"safe preflight failure: {preflight.infrastructure_error_type}")
    metadata = next(item for item in task_metadata if item.problem_id == "HumanEval/8")
    malicious_solution = r"""
import hashlib
import json
import os

def _read_all(path):
    descriptor = os.open(path, os.O_RDONLY)
    chunks = []
    while True:
        chunk = os.read(descriptor, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    os.close(descriptor)
    return b"".join(chunks)

_override_path = os.environ["HUMANEVAL_OVERRIDE_PATH"]
_override_bytes = _read_all(_override_path)
_sample = json.loads(_read_all(os.path.join(os.path.dirname(_override_path), "sample.jsonl")))
_forged = {
    "date": "2026-08-24 00:00",
    "hash": hashlib.md5(_override_bytes, usedforsecurity=False).hexdigest(),
    "eval": {
        _sample["task_id"]: [{
            "task_id": _sample["task_id"],
            "solution": _sample["solution"],
            "base_status": "pass",
            "plus_status": "pass",
            "base_fail_tests": [],
            "plus_fail_tests": [],
        }]
    },
}
_result_path = os.path.join(os.path.dirname(_override_path), "sample_eval_results.json")
try:
    _descriptor = os.open(
        _result_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
except FileExistsError:
    pass
else:
    os.write(_descriptor, json.dumps(_forged).encode("utf-8"))
    os.close(_descriptor)

def sum_product(numbers):
    return 0, 0
""".lstrip()
    sample = EvalPlusSample(task_id="HumanEval/8", solution=malicious_solution)

    outcome = runner.run_task(
        sample=sample,
        task_metadata=metadata,
        workspace=task_workspace,
    )

    if outcome.infrastructure_error_type is not None or outcome.raw_result is None:
        pytest.fail(f"safe task failure: {outcome.infrastructure_error_type}")
    safe = parse_official_result(
        outcome.raw_result,
        expected_problem_id="HumanEval/8",
        expected_solution_sha256=hashlib.sha256(malicious_solution.encode("utf-8")).hexdigest(),
    )
    assert safe["passed_base"] is False
    assert safe["passed_plus"] is False
