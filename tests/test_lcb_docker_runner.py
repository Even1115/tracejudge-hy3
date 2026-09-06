"""Offline LCB container-runner tests; a fake docker CLI replaces Docker.

No real container, network, candidate execution, or hidden-test decoding
happens here.  The fake command runner writes the staged output file exactly
where the hardened ``docker run`` argv bind-mounts it, which lets every test
exercise the full host flow (staging, argv hardening, cleanup, raw-result
validation, and normalization through the frozen adapter).
"""

from __future__ import annotations

import base64
import hashlib
import json
import pickle
import subprocess
import sys
import types
import zlib
from pathlib import Path
from typing import Any

import pytest

from tracejudge_hy3.benchmark import ExecutionStatus
from tracejudge_hy3.benchmark.livecodebench import (
    _RESULT_FIELDS as ADAPTER_RESULT_FIELDS,
)
from tracejudge_hy3.benchmark.livecodebench import (
    LCB_CHECKER_COMMIT,
    LCB_EXECUTOR_ID,
    LiveCodeBenchBenchmarkAdapter,
    candidate_from_code,
    parse_source_record,
)
from tracejudge_hy3.lcb import container_entrypoint as entrypoint
from tracejudge_hy3.lcb.docker_runner import (
    _RESULT_FIELDS as RUNNER_RESULT_FIELDS,
)
from tracejudge_hy3.lcb.docker_runner import (
    DockerLimits,
    LCBDockerRunner,
    LCBRunnerError,
)

IMAGE = "tracejudge-lcb-checker@sha256:" + "d" * 64
PRIVATE_CANARY = "PRIVATE_HIDDEN_TEST_CANARY_NEVER_DISCLOSE"


def _raw_record(question_id: str = "task-001") -> dict[str, Any]:
    return {
        "question_title": f"Problem {question_id}",
        "question_content": f"Public statement for {question_id}.",
        "platform": "atcoder",
        "question_id": question_id,
        "contest_id": f"contest-{question_id}",
        "contest_date": "2024-08-15T00:00:00",
        "starter_code": "",
        "difficulty": "easy",
        "public_test_cases": json.dumps([{"input": "1\n", "output": "1\n", "testtype": "stdin"}]),
        "private_test_cases": f"{PRIVATE_CANARY}-{question_id}",
        "metadata": json.dumps({}),
    }


def _task_and_candidate(code: str = "print(1)\n"):
    adapter = LiveCodeBenchBenchmarkAdapter()
    record = parse_source_record(_raw_record(), source_file="test.jsonl")
    task = adapter.project_record(record)
    candidate = candidate_from_code(task=task, candidate_id="run-1", code=code)
    return adapter, record, task, candidate


def _raw_result(task, candidate, **overrides) -> dict[str, Any]:
    result: dict[str, Any] = {
        "question_id": task.identity.task_id,
        "candidate_sha256": candidate.code_sha256,
        "infrastructure_status": "ok",
        "error_type": None,
        "compile_ok": True,
        "official_test_results": [True, True],
        "official_error_code": None,
        "public_test_count": 1,
        "duration_seconds": 0.5,
        "checker_commit": LCB_CHECKER_COMMIT,
        "executor_id": LCB_EXECUTOR_ID,
    }
    result.update(overrides)
    return result


class FakeDocker:
    """Scriptable docker CLI double; writes results through the bind path."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.result_payload: dict[str, Any] | str | None = None
        self.wait_exit: str = "0"
        self.run_returncode: int = 0
        self.timeout_on_wait: bool = False
        self.info_returncode: int = 0
        self.inspect_digests: list[str] | None = None
        self.inspect_id: str = "sha256:" + "0" * 64
        self.control_mode_observed: int | None = None

    def __call__(self, command, **kwargs) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        action = command[1] if len(command) > 1 else ""
        if action == "info":
            return subprocess.CompletedProcess(
                command, self.info_returncode, stdout="27.0.0\n", stderr=""
            )
        if action == "image":
            digests = self.inspect_digests if self.inspect_digests is not None else [IMAGE]
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f"{self.inspect_id} {json.dumps(digests)}",
                stderr="",
            )
        if action == "run":
            for index, token in enumerate(command):
                if token == "--mount" and ",dst=/control,ro" in command[index + 1]:
                    source = command[index + 1].split("src=", 1)[1].split(",", 1)[0]
                    self.control_mode_observed = Path(source).stat().st_mode & 0o777
            return subprocess.CompletedProcess(
                command, self.run_returncode, stdout="container-id\n", stderr=""
            )
        if action == "wait":
            if self.timeout_on_wait:
                raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 0))
            if self.result_payload is not None:
                result_path = self._staged_result_path(command)
                payload = self.result_payload
                if isinstance(payload, dict):
                    text = json.dumps(payload) + "\n"
                else:
                    text = payload
                Path(result_path).write_text(text, encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout=f"{self.wait_exit}\n", stderr="")
        if action == "rm":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected docker invocation: {command}")

    def _staged_result_path(self, wait_command: list[str]) -> str:
        run_command = next(cmd for cmd in self.commands if cmd[1:2] == ["run"])
        for index, token in enumerate(run_command):
            if token == "--mount" and ",dst=/output/result.json" in run_command[index + 1]:
                return run_command[index + 1].split("src=", 1)[1].split(",", 1)[0]
        raise AssertionError("run command lacks the result file bind")

    def ran(self, action: str) -> bool:
        return any(cmd[1:2] == [action] for cmd in self.commands)


def _runner(fake: FakeDocker, **overrides) -> LCBDockerRunner:
    return LCBDockerRunner(image=IMAGE, command_runner=fake, **overrides)


def _run_kwargs(task, candidate, record) -> dict[str, Any]:
    return {
        "task": task,
        "candidate": candidate,
        "public_test_cases_raw": record.public_test_cases_raw,
        "private_test_cases_raw": record.private_test_cases_raw,
        "public_test_count": len(record.public_test_cases),
    }


# ---------------------------------------------------------------------------
# Image and environment identity
# ---------------------------------------------------------------------------


def test_runner_schema_constant_matches_adapter() -> None:
    assert RUNNER_RESULT_FIELDS == ADAPTER_RESULT_FIELDS


def test_entrypoint_constants_match_adapter_pin() -> None:
    assert entrypoint.CHECKER_COMMIT == LCB_CHECKER_COMMIT
    assert entrypoint.EXECUTOR_ID == LCB_EXECUTOR_ID


def test_rejects_non_digest_image() -> None:
    with pytest.raises(ValueError, match="pinned"):
        LCBDockerRunner(image="tracejudge-lcb-checker:latest")
    with pytest.raises(ValueError, match="pinned"):
        LCBDockerRunner(image="other-image@sha256:" + "d" * 64)


def test_rejects_non_pinned_platform() -> None:
    with pytest.raises(ValueError, match="linux/amd64"):
        LCBDockerRunner(image=IMAGE, requested_platform="linux/arm64")


def test_is_available_and_image_identity() -> None:
    fake = FakeDocker()
    runner = _runner(fake)
    assert runner.is_available() == (True, None)
    runner.verify_image_identity()

    fake.info_returncode = 1
    assert runner.is_available()[0] is False

    mismatch = FakeDocker()
    mismatch.inspect_digests = ["tracejudge-lcb-checker@sha256:" + "0" * 64]
    with pytest.raises(LCBRunnerError) as excinfo:
        _runner(mismatch).verify_image_identity()
    assert excinfo.value.error_type == "image_mismatch"


def test_image_identity_accepts_local_build_image_id() -> None:
    # Locally built images have no RepoDigests; the pin then binds .Id.
    local = FakeDocker()
    local.inspect_digests = []
    local.inspect_id = "sha256:" + "d" * 64
    runner = LCBDockerRunner(image=local.inspect_id, command_runner=local)
    runner.verify_image_identity()
    assert local.commands[-1][3] == local.inspect_id
    # A config image ID is never a repository manifest digest.
    with pytest.raises(LCBRunnerError, match="does not match"):
        _runner(local).verify_image_identity()

    wrong = FakeDocker()
    wrong.inspect_digests = []
    wrong.inspect_id = "sha256:" + "1" * 64
    with pytest.raises(LCBRunnerError) as excinfo:
        LCBDockerRunner(image=local.inspect_id, command_runner=wrong).verify_image_identity()
    assert excinfo.value.error_type == "image_mismatch"


# ---------------------------------------------------------------------------
# Hardened container command
# ---------------------------------------------------------------------------


def test_container_command_hardening(tmp_path: Path) -> None:
    fake = FakeDocker()
    _, record, task, candidate = _task_and_candidate()
    fake.result_payload = _raw_result(task, candidate)
    _runner(fake).run_task(workspace=tmp_path, **_run_kwargs(task, candidate, record))

    command = next(cmd for cmd in fake.commands if cmd[1:2] == ["run"])
    rendered = " ".join(command)
    assert "--network none" in rendered
    assert "--read-only" in command
    assert "--pull never" in rendered
    assert "--platform linux/amd64" in rendered
    assert "--memory 4g" in rendered and "--memory-swap 4g" in rendered
    assert "--cpus 1" in rendered
    assert "--pids-limit 128" in rendered
    assert "--cap-drop ALL" in rendered
    assert "--security-opt no-new-privileges" in rendered
    assert "--log-driver none" in rendered
    assert any(token.startswith("/tmp:rw,noexec,nosuid,nodev") for token in command)
    assert any(",dst=/control,ro" in token for token in command)
    assert any(",dst=/output/result.json" in token for token in command)
    assert not any(",dst=/output" in token and "result.json" not in token for token in command)
    assert any(token.startswith("fsize=") for token in command)
    assert command[-4:] == ["python3", "-B", "-u", "/control/entrypoint.py"]
    assert IMAGE in command
    # The control bind source was locked down to 0555 before container start.
    assert fake.control_mode_observed == 0o555
    # Cleanup always removes the container.
    assert fake.ran("rm")


# ---------------------------------------------------------------------------
# End-to-end status mapping through the frozen adapter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected_status", "expected_kind"),
    [
        ({}, ExecutionStatus.PASSED, None),
        (
            {"official_test_results": [True, -2]},
            ExecutionStatus.FAILED,
            "wrong_answer",
        ),
        (
            {"official_test_results": [True, -3]},
            ExecutionStatus.TIMEOUT,
            "time_limit_exceeded",
        ),
        (
            {"official_test_results": [True, -4]},
            ExecutionStatus.RUNTIME_ERROR,
            "runtime_error",
        ),
        (
            {"compile_ok": False, "official_test_results": []},
            ExecutionStatus.COMPILE_ERROR,
            "compile_error",
        ),
        (
            {"official_error_code": -5, "official_test_results": [-2]},
            ExecutionStatus.INFRASTRUCTURE_ERROR,
            "official_test_runner_error",
        ),
    ],
)
def test_run_task_end_to_end_status_mapping(
    tmp_path: Path, overrides: dict[str, Any], expected_status, expected_kind
) -> None:
    fake = FakeDocker()
    adapter, record, task, candidate = _task_and_candidate()
    fake.result_payload = _raw_result(task, candidate, **overrides)
    raw = _runner(fake).run_task(workspace=tmp_path, **_run_kwargs(task, candidate, record))

    assert set(raw) == ADAPTER_RESULT_FIELDS
    normalized = adapter.normalize_execution_result(task=task, candidate=candidate, result=raw)
    assert normalized.status is expected_status
    assert normalized.failure_kind == expected_kind
    assert PRIVATE_CANARY not in normalized.model_dump_json()


# ---------------------------------------------------------------------------
# Infrastructure failure mapping
# ---------------------------------------------------------------------------


def test_wait_timeout_maps_to_container_timeout_and_cleans_up(tmp_path: Path) -> None:
    fake = FakeDocker()
    fake.timeout_on_wait = True
    adapter, record, task, candidate = _task_and_candidate()
    raw = _runner(fake).run_task(workspace=tmp_path, **_run_kwargs(task, candidate, record))

    assert raw["infrastructure_status"] == "error"
    assert raw["error_type"] == "container_timeout"
    assert fake.ran("rm")
    normalized = adapter.normalize_execution_result(task=task, candidate=candidate, result=raw)
    assert normalized.status is ExecutionStatus.INFRASTRUCTURE_ERROR
    assert normalized.failure_kind == "container_timeout"


def test_nonzero_container_exit_maps_to_exit_error(tmp_path: Path) -> None:
    fake = FakeDocker()
    fake.wait_exit = "137"
    _, record, task, candidate = _task_and_candidate()
    raw = _runner(fake).run_task(workspace=tmp_path, **_run_kwargs(task, candidate, record))
    assert raw["error_type"] == "container_exit_error"


def test_container_create_failure_maps_to_start_error(tmp_path: Path) -> None:
    fake = FakeDocker()
    fake.run_returncode = 125
    _, record, task, candidate = _task_and_candidate()
    raw = _runner(fake).run_task(workspace=tmp_path, **_run_kwargs(task, candidate, record))
    assert raw["error_type"] == "container_start_error"
    assert fake.ran("rm")


def test_missing_result_maps_to_invalid_raw_result(tmp_path: Path) -> None:
    fake = FakeDocker()
    fake.result_payload = None  # container exits 0 but never writes a result
    _, record, task, candidate = _task_and_candidate()
    raw = _runner(fake).run_task(workspace=tmp_path, **_run_kwargs(task, candidate, record))
    assert raw["error_type"] == "invalid_raw_result"


def test_garbage_result_maps_to_invalid_raw_result(tmp_path: Path) -> None:
    fake = FakeDocker()
    fake.result_payload = "{not json"
    _, record, task, candidate = _task_and_candidate()
    raw = _runner(fake).run_task(workspace=tmp_path, **_run_kwargs(task, candidate, record))
    assert raw["error_type"] == "invalid_raw_result"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda result: result.update({"hidden_inputs": "leak"}),
        lambda result: result.pop("compile_ok"),
        lambda result: result.update({"question_id": "other-task"}),
        lambda result: result.update({"candidate_sha256": "0" * 64}),
        lambda result: result.update({"checker_commit": "e" * 40}),
        lambda result: result.update({"executor_id": "other_executor"}),
    ],
)
def test_tampered_result_maps_to_invalid_raw_result(tmp_path: Path, mutate) -> None:
    fake = FakeDocker()
    _, record, task, candidate = _task_and_candidate()
    payload = _raw_result(task, candidate)
    mutate(payload)
    fake.result_payload = payload
    raw = _runner(fake).run_task(workspace=tmp_path, **_run_kwargs(task, candidate, record))
    assert raw["error_type"] == "invalid_raw_result"


def test_candidate_identity_mismatch_is_executor_error(tmp_path: Path) -> None:
    fake = FakeDocker()
    _, record, task, candidate = _task_and_candidate()
    other_record = parse_source_record(_raw_record("task-002"), source_file="test.jsonl")
    other_task = LiveCodeBenchBenchmarkAdapter().project_record(other_record)
    raw = _runner(fake).run_task(
        workspace=tmp_path,
        task=other_task,
        candidate=candidate,
        public_test_cases_raw=record.public_test_cases_raw,
        private_test_cases_raw=record.private_test_cases_raw,
        public_test_count=1,
    )
    assert raw["error_type"] == "executor_error"
    assert not fake.ran("run")


def test_custom_limits_are_enforced(tmp_path: Path) -> None:
    fake = FakeDocker()
    _, record, task, candidate = _task_and_candidate()
    fake.result_payload = _raw_result(task, candidate)
    limits = DockerLimits(memory="2g", cpus="0.5", pids=64)
    _runner(fake, limits=limits).run_task(
        workspace=tmp_path, **_run_kwargs(task, candidate, record)
    )
    command = next(cmd for cmd in fake.commands if cmd[1:2] == ["run"])
    rendered = " ".join(command)
    assert "--memory 2g" in rendered
    assert "--cpus 0.5" in rendered
    assert "--pids-limit 64" in rendered


# ---------------------------------------------------------------------------
# Entrypoint pure logic (no candidate execution, no hidden-test decoding of
# dataset payloads; the pickle fixture below is created by this test itself)
# ---------------------------------------------------------------------------


def test_entrypoint_compile_probe() -> None:
    assert entrypoint.compile_probe("print(1)\n") is True
    assert entrypoint.compile_probe("def broken(:\n") is False


def test_entrypoint_load_request_validation(tmp_path: Path) -> None:
    request = {
        "schema_version": 1,
        "question_id": "task-001",
        "candidate_sha256": "a" * 64,
        "public_test_cases": "[]",
        "private_test_cases": "opaque",
        "public_test_count": 0,
        "per_test_timeout_seconds": 6,
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    assert entrypoint.load_request(path)["question_id"] == "task-001"

    for mutation in (
        lambda r: r.update({"extra": True}),
        lambda r: r.pop("candidate_sha256"),
        lambda r: r.update({"public_test_count": -1}),
        lambda r: r.update({"per_test_timeout_seconds": 0}),
        lambda r: r.update({"candidate_sha256": "not-a-hash"}),
    ):
        bad = dict(request)
        mutation(bad)
        path.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(entrypoint._EntrypointError):
            entrypoint.load_request(path)


def test_entrypoint_accepts_request_as_large_as_pinned_lcb60(tmp_path: Path) -> None:
    # The fixed selection's largest request is 69,014,330 bytes. Exercise the
    # actual JSON read/validation boundary with self-authored opaque text only.
    request = {
        "schema_version": 1,
        "question_id": "large-request-fixture",
        "candidate_sha256": "a" * 64,
        "public_test_cases": "[]",
        "private_test_cases": "",
        "public_test_count": 0,
        "per_test_timeout_seconds": 6,
    }
    target_bytes = 69_014_330
    overhead = len(json.dumps(request, separators=(",", ":")).encode())
    request["private_test_cases"] = "A" * (target_bytes - overhead)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request, separators=(",", ":")), encoding="utf-8")
    assert path.stat().st_size == target_bytes
    loaded = entrypoint.load_request(path)
    assert loaded["question_id"] == request["question_id"]
    assert len(loaded["private_test_cases"]) == target_bytes - overhead


def test_entrypoint_rejects_request_over_128_mib_before_reading(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "oversized.json"
    with path.open("wb") as stream:
        stream.truncate(128 * 1024 * 1024 + 1)

    def forbidden_read(*args, **kwargs):
        raise AssertionError("oversized request must be rejected before opening")

    monkeypatch.setattr(Path, "open", forbidden_read)
    with pytest.raises(entrypoint._EntrypointError, match="control file exceeds its size limit"):
        entrypoint.load_request(path)


def test_entrypoint_official_private_decode_chain() -> None:
    cases = [{"input": "7\n", "output": "49\n", "testtype": "stdin"}]
    official_blob = base64.b64encode(zlib.compress(pickle.dumps(json.dumps(cases)))).decode("ascii")
    decoded = entrypoint.decode_private_tests(official_blob)
    assert decoded == cases
    with pytest.raises(entrypoint._EntrypointError):
        entrypoint.decode_private_tests("not-official-encoding")


def test_entrypoint_build_evaluation_sample_merges_public_then_private() -> None:
    sample = entrypoint.build_evaluation_sample(
        [{"input": "a", "output": "1"}],
        [{"input": "b", "output": "2"}],
    )
    decoded = json.loads(sample["input_output"])
    assert decoded == {"inputs": ["a", "b"], "outputs": ["1", "2"]}


def test_entrypoint_sanitize_and_metadata_extraction() -> None:
    assert entrypoint.sanitize_result_codes([True, -2, -3, -4]) == [True, -2, -3, -4]
    with pytest.raises(entrypoint._EntrypointError):
        entrypoint.sanitize_result_codes([True, -1])
    with pytest.raises(entrypoint._EntrypointError):
        entrypoint.sanitize_result_codes([1])
    assert entrypoint.extract_official_error_code({"error_code": -5}) == -5
    assert entrypoint.extract_official_error_code({"error_code": -2}) is None
    assert entrypoint.extract_official_error_code("garbage") is None
    assert entrypoint.extract_official_duration({"execution time": 1.5}, 9.0) == 1.5
    assert entrypoint.extract_official_duration({}, 9.0) == 9.0


def test_entrypoint_fix_official_types_with_stub_numpy(monkeypatch) -> None:
    class FakeBool:
        def __init__(self, value):
            self._value = value

    class FakeArray:
        def __init__(self, value):
            self._value = value

        def item(self, index):
            assert index == 0
            return self._value

    stub = types.SimpleNamespace(ndarray=FakeArray, bool_=FakeBool)
    monkeypatch.setitem(sys.modules, "numpy", stub)
    fixed = entrypoint.fix_official_types([FakeArray(FakeBool(True)), FakeArray(-2)])
    assert fixed == [True, -2]
    assert type(fixed[0]) is bool
    with pytest.raises(entrypoint._EntrypointError):
        entrypoint.fix_official_types("not-a-list")


def test_entrypoint_build_result_exact_fields() -> None:
    result = entrypoint.build_result(
        question_id="task-001",
        candidate_sha256="a" * 64,
        infrastructure_status="ok",
        error_type=None,
        compile_ok=True,
        official_test_results=[True],
        official_error_code=None,
        public_test_count=1,
        duration_seconds=0.25,
    )
    assert set(result) == ADAPTER_RESULT_FIELDS
    assert result["checker_commit"] == LCB_CHECKER_COMMIT
    assert result["executor_id"] == LCB_EXECUTOR_ID


def test_candidate_code_hash_matches_staging(tmp_path: Path) -> None:
    fake = FakeDocker()
    _, record, task, candidate = _task_and_candidate(code="n = int(input())\nprint(n * n)\n")
    staged: dict[str, str] = {}

    class ObservingDocker(FakeDocker):
        def __call__(self, command, **kwargs):
            completed = super().__call__(command, **kwargs)
            if command[1:2] == ["run"]:
                for index, token in enumerate(command):
                    if token == "--mount" and ",dst=/control,ro" in command[index + 1]:
                        source = Path(command[index + 1].split("src=", 1)[1].split(",", 1)[0])
                        staged["candidate"] = (source / "candidate.py").read_text("utf-8")
                        staged["request"] = (source / "request.json").read_text("utf-8")
            return completed

    observing = ObservingDocker()
    observing.result_payload = fake.result_payload = _raw_result(task, candidate)
    _runner(observing).run_task(workspace=tmp_path, **_run_kwargs(task, candidate, record))

    assert staged["candidate"] == candidate.code
    request = json.loads(staged["request"])
    assert request["candidate_sha256"] == hashlib.sha256(candidate.code.encode("utf-8")).hexdigest()
    assert request["public_test_cases"] == record.public_test_cases_raw
    assert request["private_test_cases"] == record.private_test_cases_raw
    assert request["public_test_count"] == 1
