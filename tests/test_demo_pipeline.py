from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tracejudge_hy3.cli import app
from tracejudge_hy3.config import get_settings
from tracejudge_hy3.dataset.loader import load_problem_by_id, load_problems
from tracejudge_hy3.exceptions import ConfigurationError, UnsafeExecutionError
from tracejudge_hy3.pipeline.runner import run_pipeline, select_backend
from tracejudge_hy3.providers.mock import MockProvider
from tracejudge_hy3.sandbox.docker_backend import DockerSandbox
from tracejudge_hy3.sandbox.trusted_local import TrustedLocalSandbox
from tracejudge_hy3.schemas.problem import ProblemSpec, RequirementItem
from tracejudge_hy3.schemas.problem import TestCase as CaseSpec

DATASET = Path(__file__).resolve().parents[1] / "data" / "sample_problems.jsonl"


@pytest.mark.parametrize("case", ["correct", "faulty"])
async def test_full_mock_pipeline_safe_mean(case):
    problem = load_problem_by_id(DATASET, "safe_mean")
    provider = MockProvider(case=case)
    backend = TrustedLocalSandbox(per_test_timeout_seconds=2.0)

    result = await run_pipeline(problem, provider, backend)

    visible_ok = all(r.passed for r in result.execution_result.results if r.category == "visible")
    assert visible_ok

    if case == "faulty":
        assert result.process_assessment.functional_correct is False
        assert result.process_assessment.process_correct is False
        assert result.process_assessment.error_type is not None
        assert result.process_assessment.first_faulty_step == "S1"
        assert result.process_assessment.violated_requirement == "R1"
        assert result.error_certificate is not None
        assert result.error_certificate.verdict == "confirmed_bug"
        assert result.counterexample is not None
        assert result.counterexample.args == [[]]
    else:
        assert result.process_assessment.functional_correct is True
        assert result.process_assessment.error_type is None
        assert result.error_certificate is None


async def test_full_mock_pipeline_for_all_sample_problems():
    problems = load_problems(DATASET)
    backend = TrustedLocalSandbox(per_test_timeout_seconds=2.0)
    provider = MockProvider()
    for problem in problems:
        result = await run_pipeline(problem, provider, backend)
        assert result.execution_result.runtime_status == "completed"
        assert result.process_assessment.functional_correct is True


def test_cli_demo_faulty_reports_confirmed_bug(tmp_path, monkeypatch):
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    runner = CliRunner()
    result = runner.invoke(app, ["demo", "--mock", "--case", "faulty"])
    assert result.exit_code == 0, result.output
    assert "confirmed_bug" in result.output
    assert "A01_PLAN_CODE_MISMATCH" in result.output


def test_cli_demo_correct_does_not_report_confirmed_bug(monkeypatch):
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    runner = CliRunner()
    result = runner.invoke(app, ["demo", "--mock", "--case", "correct"])
    assert result.exit_code == 0, result.output
    assert "confirmed_bug" not in result.output


def test_cli_demo_finds_bundled_data_outside_repository(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["demo", "--mock", "--case", "correct"])
    assert result.exit_code == 0, result.output
    assert "safe_mean" in result.output


def test_cli_doctor_runs(monkeypatch):
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "TraceJudge-Hy3" in result.output


def test_cli_run_single_problem(monkeypatch, tmp_path):
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    runner = CliRunner()
    out_file = tmp_path / "result.json"
    result = runner.invoke(
        app,
        [
            "run",
            "--dataset",
            str(DATASET),
            "--problem-id",
            "clamp",
            "--provider",
            "mock",
            "--sandbox",
            "trusted-local",
            "--allow-unsafe-local-exec",
            "--output",
            str(out_file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert out_file.exists()
    payload = json.loads(out_file.read_text())
    assert payload["problem"]["problem_id"] == "clamp"


def test_cli_run_uses_configured_default_sandbox(monkeypatch, tmp_path):
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    monkeypatch.setenv("TRACEJUDGE_SANDBOX", "trusted-local")
    out_file = tmp_path / "configured-default.json"
    result = CliRunner().invoke(
        app,
        [
            "run",
            "--dataset",
            str(DATASET),
            "--problem-id",
            "clamp",
            "--provider",
            "mock",
            "--output",
            str(out_file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert out_file.exists()


def test_cli_batch_all_mock_problems(monkeypatch, tmp_path):
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    runner = CliRunner()
    out_file = tmp_path / "batch.jsonl"
    result = runner.invoke(
        app,
        [
            "batch",
            "--dataset",
            str(DATASET),
            "--provider",
            "mock",
            "--sandbox",
            "trusted-local",
            "--allow-unsafe-local-exec",
            "--output",
            str(out_file),
        ],
    )
    assert result.exit_code == 0, result.output
    lines = out_file.read_text().strip().splitlines()
    assert len(lines) == 3


def test_select_backend_refuses_trusted_local_for_non_mock_without_flag():
    settings = get_settings()
    with pytest.raises(UnsafeExecutionError):
        select_backend(
            provider_name="hy3",
            sandbox_choice="trusted-local",
            allow_unsafe_local_exec=False,
            settings=settings,
        )


def test_select_backend_allows_trusted_local_with_explicit_flag():
    settings = get_settings()
    backend = select_backend(
        provider_name="hy3",
        sandbox_choice="trusted-local",
        allow_unsafe_local_exec=True,
        settings=settings,
    )
    assert isinstance(backend, TrustedLocalSandbox)


def test_select_backend_allows_trusted_local_for_mock():
    settings = get_settings()
    backend = select_backend(
        provider_name="mock",
        sandbox_choice="trusted-local",
        allow_unsafe_local_exec=False,
        settings=settings,
    )
    assert isinstance(backend, TrustedLocalSandbox)


def test_select_backend_defaults_to_docker():
    settings = get_settings()
    backend = select_backend(
        provider_name="mock",
        sandbox_choice="docker",
        allow_unsafe_local_exec=False,
        settings=settings,
    )
    assert isinstance(backend, DockerSandbox)


def test_select_backend_rejects_unknown_choice():
    settings = get_settings()
    with pytest.raises(ConfigurationError, match="unknown sandbox"):
        select_backend(
            provider_name="mock",
            sandbox_choice="not-a-sandbox",
            allow_unsafe_local_exec=False,
            settings=settings,
        )


def _external_problem() -> ProblemSpec:
    return ProblemSpec(
        problem_id="external_fixture",
        title="external",
        requirement="Return the input unchanged",
        function_signature="def identity(value):",
        requirements=[RequirementItem(requirement_id="R1", content="Return the input")],
        visible_test_cases=[
            CaseSpec(
                case_id="v1",
                args=[1],
                expected=1,
                category="visible",
                related_requirements=["R1"],
            )
        ],
        reference_code="def identity(value):\n    return value\n",
        difficulty="easy",
        source="external",
    )


async def test_external_mock_fallback_requires_local_unsafe_opt_in():
    with pytest.raises(UnsafeExecutionError, match="not a trusted fixture"):
        await run_pipeline(
            _external_problem(),
            MockProvider(),
            TrustedLocalSandbox(per_test_timeout_seconds=2.0),
        )


async def test_spoofed_builtin_problem_cannot_run_dataset_reference_locally():
    built_in = load_problem_by_id(DATASET, "safe_mean")
    spoofed = built_in.model_copy(
        update={"reference_code": "def safe_mean(nums):\n    raise RuntimeError('external')\n"}
    )

    with pytest.raises(UnsafeExecutionError, match="not a trusted fixture"):
        await run_pipeline(
            spoofed,
            MockProvider(case="correct"),
            TrustedLocalSandbox(per_test_timeout_seconds=2.0),
        )


async def test_external_mock_fallback_runs_after_explicit_local_opt_in():
    result = await run_pipeline(
        _external_problem(),
        MockProvider(),
        TrustedLocalSandbox(
            per_test_timeout_seconds=2.0,
            allow_untrusted_code=True,
        ),
    )
    assert result.process_assessment.functional_correct is True


def test_run_command_refuses_unavailable_docker(monkeypatch, tmp_path):
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    monkeypatch.setenv("TRACEJUDGE_SANDBOX", "docker")
    monkeypatch.setattr(
        DockerSandbox,
        "is_available",
        lambda self: (False, "test daemon unavailable"),
    )
    out_file = tmp_path / "must-not-exist.json"
    result = CliRunner().invoke(
        app,
        [
            "run",
            "--dataset",
            str(DATASET),
            "--problem-id",
            "safe_mean",
            "--provider",
            "mock",
            "--output",
            str(out_file),
        ],
    )
    assert result.exit_code != 0
    assert "sandbox unavailable" in result.output
    assert not out_file.exists()


def test_run_command_refuses_trusted_local_for_hy3_without_flag(monkeypatch):
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    monkeypatch.delenv("HY3_API_KEY", raising=False)
    monkeypatch.delenv("HY3_BASE_URL", raising=False)
    monkeypatch.delenv("HY3_MODEL", raising=False)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "run",
            "--dataset",
            str(DATASET),
            "--problem-id",
            "clamp",
            "--provider",
            "hy3",
            "--sandbox",
            "trusted-local",
        ],
    )
    assert result.exit_code != 0
