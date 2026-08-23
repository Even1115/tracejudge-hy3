from __future__ import annotations

import subprocess

from tracejudge_hy3.sandbox.docker_backend import DockerSandbox
from tracejudge_hy3.schemas.problem import TestCase as CaseSpec


def test_docker_availability_reports_missing_cli(monkeypatch):
    monkeypatch.setattr(
        "tracejudge_hy3.sandbox.docker_backend.shutil.which",
        lambda executable: None,
    )
    available, reason = DockerSandbox().is_available()
    assert available is False
    assert reason == "docker CLI not found on PATH"


def test_docker_availability_is_cached_per_backend(monkeypatch):
    calls = 0

    monkeypatch.setattr(
        "tracejudge_hy3.sandbox.docker_backend.shutil.which",
        lambda executable: "/usr/bin/docker",
    )

    def fake_run(command, **kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(
        "tracejudge_hy3.sandbox.docker_backend.subprocess.run",
        fake_run,
    )
    backend = DockerSandbox()

    assert backend.is_available() == (True, None)
    assert backend.is_available() == (True, None)
    assert calls == 1


def test_docker_timeout_uses_hardening_flags_and_forced_cleanup(monkeypatch):
    backend = DockerSandbox(per_test_timeout_seconds=0.1)
    monkeypatch.setattr(backend, "is_available", lambda: (True, None))
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["docker", "run"]:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0, stdout="removed", stderr="")

    monkeypatch.setattr(
        "tracejudge_hy3.sandbox.docker_backend.subprocess.run",
        fake_run,
    )
    summary = backend.run(
        "def identity(value):\n    return value\n",
        "identity",
        [CaseSpec(case_id="v1", args=[1], expected=1, category="visible")],
    )

    assert summary.runtime_status == "backend_error"
    docker_run = calls[0]
    assert "--network" in docker_run and "none" in docker_run
    assert "--read-only" in docker_run
    assert "--cap-drop" in docker_run and "ALL" in docker_run
    assert calls[1][:3] == ["docker", "rm", "-f"]
    assert calls[1][3].startswith("tracejudge-")
