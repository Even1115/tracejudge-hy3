from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

import tracejudge_hy3.cli as cli_module
from tracejudge_hy3.cli import app
from tracejudge_hy3.evalplus.runner import EvalPlusExperimentError, MockEvalPlusExecutor


def _fake_run_result(output_dir: str, run_id: str, *, mode: str):
    run_dir = Path(output_dir).resolve() / run_id
    return SimpleNamespace(
        run_id=run_id,
        run_dir=run_dir,
        manifest_path=run_dir / "manifest.json",
        samples_path=run_dir / "samples.jsonl",
        results_path=run_dir / "results.jsonl",
        summary_path=run_dir / "summary.json",
        summary={
            "total_problem_count": 10,
            "actual_execution_count": 0 if mode == "mock" else 10,
            "base_pass_count": 0 if mode == "mock" else 8,
            "base_pass_rate": None if mode == "mock" else 0.8,
            "base_plus_pass_count": 0 if mode == "mock" else 6,
            "base_plus_pass_rate": None if mode == "mock" else 0.6,
            "timeout_count": 0,
            "wrong_answer_or_candidate_exception_count": 0 if mode == "mock" else 4,
            "infrastructure_error_count": 0,
            "average_duration_seconds": None if mode == "mock" else 1.25,
        },
    )


def test_evalplus_help_exposes_independent_phase_two_options():
    result = CliRunner().invoke(app, ["evalplus", "--help"])

    assert result.exit_code == 0
    for option in (
        "--baseline-run",
        "--dataset-manifest",
        "--executor",
        "--resume-run-id",
        "--parallel",
        "--per-task-timeout",
        "--batch-timeout",
    ):
        assert option in result.output


def test_mock_cli_never_loads_settings_provider_docker_or_candidate_executor(tmp_path, monkeypatch):
    calls: dict[str, object] = {}

    def forbidden(*_args, **_kwargs):
        raise AssertionError("phase two crossed a forbidden provider/config boundary")

    def fake_run_evalplus_experiment(**kwargs):
        calls.update(kwargs)
        assert isinstance(kwargs["executor"], MockEvalPlusExecutor)
        return _fake_run_result(kwargs["output_dir"], kwargs["run_id"], mode="mock")

    monkeypatch.setattr(cli_module, "get_settings", forbidden)
    monkeypatch.setattr(cli_module, "_make_provider", forbidden)
    monkeypatch.setattr(cli_module, "EvalPlusDockerRunner", forbidden)
    monkeypatch.setattr(cli_module, "run_evalplus_experiment", fake_run_evalplus_experiment)

    result = CliRunner().invoke(
        app,
        [
            "evalplus",
            "--baseline-run",
            "phase1-run",
            "--dataset-manifest",
            "dataset-manifest.json",
            "--output-dir",
            str(tmp_path),
            "--executor",
            "mock",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls["baseline_run_dir"] == "phase1-run"
    assert calls["dataset_manifest_path"] == "dataset-manifest.json"
    assert calls["resume"] is False
    assert "Mock dry run" in result.output
    assert "未执行任何候选代码" in result.output


def test_docker_cli_wires_explicit_limits_and_reports_safe_counts(tmp_path, monkeypatch):
    created: dict[str, object] = {}

    class FakeDockerExecutor:
        mode = "docker"

    def fake_docker_runner(*, limits):
        created["limits"] = limits
        return FakeDockerExecutor()

    def fake_run_evalplus_experiment(**kwargs):
        created["kwargs"] = kwargs
        return _fake_run_result(kwargs["output_dir"], kwargs["run_id"], mode="docker")

    monkeypatch.setattr(cli_module, "EvalPlusDockerRunner", fake_docker_runner)
    monkeypatch.setattr(cli_module, "run_evalplus_experiment", fake_run_evalplus_experiment)

    result = CliRunner().invoke(
        app,
        [
            "evalplus",
            "--baseline-run",
            "phase1-run",
            "--dataset-manifest",
            "dataset-manifest.json",
            "--output-dir",
            str(tmp_path),
            "--parallel",
            "2",
            "--per-task-timeout",
            "123",
            "--batch-timeout",
            "456",
        ],
    )

    assert result.exit_code == 0, result.output
    assert created["limits"].per_task_timeout_seconds == 123.0
    kwargs = created["kwargs"]
    assert kwargs["max_workers"] == 2
    assert kwargs["per_task_timeout_seconds"] == 123.0
    assert kwargs["batch_timeout_seconds"] == 456.0
    assert "8/10" in result.output
    assert "6/10" in result.output
    assert "固定 EvalPlus raw schema" in result.output


def test_evalplus_cli_rejects_unknown_executor_before_creating_a_run(tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "evalplus",
            "--baseline-run",
            "phase1-run",
            "--dataset-manifest",
            "dataset-manifest.json",
            "--output-dir",
            str(tmp_path),
            "--executor",
            "host",
        ],
    )

    assert result.exit_code != 0
    assert list(tmp_path.iterdir()) == []


def test_evalplus_cli_never_echoes_exception_or_summary_canaries(tmp_path, monkeypatch):
    canaries = (
        "PRIVATE_HIDDEN_TEST_CANARY",
        "sk-live-PRIVATE_API_CANARY",
        "Authorization: Bearer PRIVATE_AUTH_CANARY",
        ".env=PRIVATE_ENV_CANARY",
    )

    def fail_safely(**_kwargs):
        raise EvalPlusExperimentError(" | ".join(canaries))

    monkeypatch.setattr(cli_module, "run_evalplus_experiment", fail_safely)
    result = CliRunner().invoke(
        app,
        [
            "evalplus",
            "--baseline-run",
            "phase1-run",
            "--dataset-manifest",
            "dataset-manifest.json",
            "--output-dir",
            str(tmp_path),
            "--executor",
            "mock",
        ],
    )

    assert result.exit_code == 1
    assert "未输出原始异常详情" in result.output
    assert all(canary not in result.output for canary in canaries)
