"""Diagnostic exit/OOM evidence must distinguish natural exits from cleanup."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

from tracejudge_hy3.evalplus.docker_runner import EvalPlusDockerRunner

SPEC = importlib.util.spec_from_file_location(
    "he_diagnostic",
    Path(__file__).resolve().parents[1] / "scripts/diagnose_humanevalplus_pending.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_running_container_does_not_have_final_exit_code():
    state = MODULE.safe_state(
        {"Running": True, "ExitCode": 0, "OOMKilled": False, "Error": "PRIVATE"}
    )
    assert state["ExitCode"] is None
    assert state["OOMKilled"] is False
    assert "PRIVATE" not in str(state)


def test_diagnostic_kill_keeps_before_and_after_evidence(monkeypatch):
    runner = MODULE.DiagnosticRunner()
    runner.task_controls["owned"] = Path("unused")
    runner.problem_id = "HumanEval/103"
    states = iter(
        [
            {"available": True, "Running": True, "ExitCode": None, "OOMKilled": False},
            {"available": True, "Running": False, "ExitCode": 137, "OOMKilled": False},
        ]
    )
    monkeypatch.setattr(runner, "inspect_state", lambda _: next(states))
    monkeypatch.setattr(runner, "control_status", lambda _: {"available": False})
    calls = []

    def invoke(self, command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "137", "")

    monkeypatch.setattr(EvalPlusDockerRunner, "_invoke", invoke)
    assert runner._force_remove("owned") == "removed"
    item = runner.observations[0]
    assert item["diagnostic_kill_requested"] is True
    assert item["before_cleanup"]["ExitCode"] is None
    assert item["after_diagnostic_kill"]["ExitCode"] == 137
    assert item["after_diagnostic_kill"]["OOMKilled"] is False
    assert calls == [
        ["docker", "kill", "owned"],
        ["docker", "wait", "owned"],
        ["docker", "rm", "-f", "-v", "owned"],
    ]


def test_natural_oom_exit_is_not_killed_again(monkeypatch):
    runner = MODULE.DiagnosticRunner()
    runner.task_controls["owned"] = Path("unused")
    monkeypatch.setattr(
        runner, "inspect_state", lambda _: {"Running": False, "ExitCode": 137, "OOMKilled": True}
    )
    monkeypatch.setattr(runner, "control_status", lambda _: {"available": False})
    calls = []

    def invoke(self, command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(EvalPlusDockerRunner, "_invoke", invoke)
    assert runner._force_remove("owned") == "removed"
    assert runner.observations[0]["diagnostic_kill_requested"] is False
    assert calls == [["docker", "rm", "-f", "-v", "owned"]]
