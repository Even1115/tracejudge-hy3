from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

import tracejudge_hy3.cli as cli_module
from tracejudge_hy3.cli import app
from tracejudge_hy3.evalplus_mbpp import MbppExperimentError, MockMbppEvalPlusExecutor


def _fake_run_result(output_dir: str, run_id: str, *, mode: str) -> SimpleNamespace:
    run_dir = Path(output_dir).resolve() / run_id
    return SimpleNamespace(
        run_id=run_id,
        run_dir=run_dir,
        manifest_path=run_dir / "manifest.json",
        samples_path=run_dir / "samples.jsonl",
        results_path=run_dir / "results.jsonl",
        summary_path=run_dir / "summary.json",
        summary={
            "total_problem_count": 120,
            "actual_execution_count": 0 if mode == "mock" else 120,
            "mock_not_executed_count": 120 if mode == "mock" else 0,
            "base_pass_count": 0 if mode == "mock" else 100,
            "base_pass_rate": None if mode == "mock" else 100 / 120,
            "base_plus_pass_count": 0 if mode == "mock" else 80,
            "base_plus_pass_rate": None if mode == "mock" else 80 / 120,
            "timeout_count": 0 if mode == "mock" else 3,
            "wrong_answer_or_candidate_exception_count": 0 if mode == "mock" else 37,
            "infrastructure_error_count": 0,
            "average_duration_seconds": None if mode == "mock" else 1.25,
        },
    )


def test_evalplus_mbpp_help_exposes_options():
    result = CliRunner().invoke(app, ["evalplus-mbpp", "--help"])

    assert result.exit_code == 0
    for option in (
        "--dataset-manifest",
        "--candidates",
        "--executor",
        "--resume-run-id",
        "--parallel",
        "--per-task-timeout",
        "--batch-timeout",
    ):
        assert option in result.output


def test_mock_cli_never_loads_settings_provider_or_docker(tmp_path, monkeypatch):
    calls: dict[str, object] = {}

    def forbidden(*_args, **_kwargs):
        raise AssertionError("MBPP+ phase two crossed a forbidden provider/config boundary")

    def fake_run_mbpp_experiment(*args, **kwargs):
        calls["args"] = args
        calls.update(kwargs)
        assert isinstance(kwargs["executor"], MockMbppEvalPlusExecutor)
        return _fake_run_result(kwargs["output_dir"], kwargs["run_id"], mode="mock")

    monkeypatch.setattr(cli_module, "get_settings", forbidden)
    monkeypatch.setattr(cli_module, "_make_provider", forbidden)
    monkeypatch.setattr(cli_module, "MbppPlusDockerRunner", forbidden)
    monkeypatch.setattr(cli_module, "run_mbpp_experiment", fake_run_mbpp_experiment)

    result = CliRunner().invoke(
        app,
        [
            "evalplus-mbpp",
            "--dataset-manifest",
            "selection/dataset_manifest.json",
            "--candidates",
            "candidates.jsonl",
            "--output-dir",
            str(tmp_path),
            "--executor",
            "mock",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls["dataset_manifest_path"] == "selection/dataset_manifest.json"
    assert calls["candidates_path"] == "candidates.jsonl"
    assert calls["resume"] is False
    assert "Mock dry run" in result.output
    assert "未执行任何候选代码" in result.output
    assert "不是完整 MBPP+ 成绩" in result.output


def test_docker_cli_wires_limits_and_resume(tmp_path, monkeypatch):
    created: dict[str, object] = {}

    class FakeDockerExecutor:
        mode = "docker"

    def fake_docker_runner(*, limits):
        created["limits"] = limits
        return FakeDockerExecutor()

    def fake_run_mbpp_experiment(*args, **kwargs):
        created["kwargs"] = kwargs
        return _fake_run_result(kwargs["output_dir"], kwargs["run_id"], mode="docker")

    monkeypatch.setattr(cli_module, "MbppPlusDockerRunner", fake_docker_runner)
    monkeypatch.setattr(cli_module, "run_mbpp_experiment", fake_run_mbpp_experiment)

    result = CliRunner().invoke(
        app,
        [
            "evalplus-mbpp",
            "--dataset-manifest",
            "selection/dataset_manifest.json",
            "--candidates",
            "candidates.jsonl",
            "--output-dir",
            str(tmp_path),
            "--executor",
            "docker",
            "--resume-run-id",
            "phase2_mbpp_existing",
            "--parallel",
            "4",
            "--per-task-timeout",
            "60",
            "--batch-timeout",
            "300",
        ],
    )

    assert result.exit_code == 0, result.output
    assert created["limits"].per_task_timeout_seconds == 60.0
    kwargs = created["kwargs"]
    assert kwargs["run_id"] == "phase2_mbpp_existing"
    assert kwargs["resume"] is True
    assert kwargs["max_workers"] == 4
    assert kwargs["per_task_timeout_seconds"] == 60.0
    assert kwargs["batch_timeout_seconds"] == 300.0
    assert "Base+Extra 通过" in result.output


def test_cli_rejects_invalid_option_combinations(tmp_path):
    runner = CliRunner()
    bad_executor = runner.invoke(
        app,
        [
            "evalplus-mbpp",
            "--dataset-manifest",
            "m.json",
            "--candidates",
            "c.jsonl",
            "--output-dir",
            str(tmp_path),
            "--executor",
            "local",
        ],
    )
    assert bad_executor.exit_code != 0

    bad_timeouts = runner.invoke(
        app,
        [
            "evalplus-mbpp",
            "--dataset-manifest",
            "m.json",
            "--candidates",
            "c.jsonl",
            "--output-dir",
            str(tmp_path),
            "--executor",
            "mock",
            "--per-task-timeout",
            "100",
            "--batch-timeout",
            "10",
        ],
    )
    assert bad_timeouts.exit_code != 0


def test_cli_failure_hides_raw_exception_details(tmp_path, monkeypatch):
    def fake_run_mbpp_experiment(*_args, **_kwargs):
        raise MbppExperimentError("PRIVATE_CANDIDATE_OR_TEST_CANARY should never be printed")

    monkeypatch.setattr(cli_module, "run_mbpp_experiment", fake_run_mbpp_experiment)

    result = CliRunner().invoke(
        app,
        [
            "evalplus-mbpp",
            "--dataset-manifest",
            "m.json",
            "--candidates",
            "c.jsonl",
            "--output-dir",
            str(tmp_path),
            "--executor",
            "mock",
        ],
    )

    assert result.exit_code == 1
    assert "未输出原始异常详情" in result.output
    assert "PRIVATE_CANDIDATE_OR_TEST_CANARY" not in result.output


def test_cli_infrastructure_error_exits_nonzero(tmp_path, monkeypatch):
    def fake_run_mbpp_experiment(*args, **kwargs):
        result = _fake_run_result(kwargs["output_dir"], kwargs["run_id"], mode="docker")
        result.summary["infrastructure_error_count"] = 2
        return result

    monkeypatch.setattr(cli_module, "run_mbpp_experiment", fake_run_mbpp_experiment)

    result = CliRunner().invoke(
        app,
        [
            "evalplus-mbpp",
            "--dataset-manifest",
            "m.json",
            "--candidates",
            "c.jsonl",
            "--output-dir",
            str(tmp_path),
            "--executor",
            "mock",
        ],
    )
    assert result.exit_code == 1
    assert "基础设施错误" in result.output
