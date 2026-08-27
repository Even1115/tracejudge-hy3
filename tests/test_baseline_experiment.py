from __future__ import annotations

import hashlib
import json
import platform
import re
import socket
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import tracejudge_hy3.baseline.runner as baseline_runner
from tracejudge_hy3.baseline import BaselineExperimentError, run_baseline_experiment
from tracejudge_hy3.cli import app
from tracejudge_hy3.dataset.loader import load_problems
from tracejudge_hy3.exceptions import ParsingError, ProviderAuthError, ProviderResponseError
from tracejudge_hy3.providers.base import SolutionGeneration
from tracejudge_hy3.providers.mock import MockProvider
from tracejudge_hy3.schemas.problem import ProblemSpec
from tracejudge_hy3.schemas.solution import ImplementationStep, SolutionTrace

REPOSITORY = Path(__file__).resolve().parents[1]
SAMPLE_DATASET = REPOSITORY / "data" / "sample_problems.jsonl"


def _solution(problem: ProblemSpec, *, marker: str = "可审查中文解答") -> SolutionTrace:
    requirement_ids = [item.requirement_id for item in problem.requirements]
    return SolutionTrace(
        problem_id=problem.problem_id,
        requirement_understanding=f"{marker}：实现公开题面的函数。",
        design_summary="按公开需求分步构造返回值。",
        edge_cases_considered=["空输入", "边界值"],
        implementation_steps=[
            ImplementationStep(
                step_id="S1",
                content="实现与公开需求相对应的函数主体。",
                related_requirements=requirement_ids,
                expected_code_behavior="返回题面规定的结果。",
            )
        ],
        declared_time_complexity="O(n)",
        declared_space_complexity="O(1)",
        code=(
            f"{problem.function_signature}\n"
            '    headers = {"Content-Type": "application/json"}\n'
            "    return headers\n"
        ),
    )


def _success(
    problem: ProblemSpec,
    *,
    attempt_count: int = 1,
    attempt_outcomes: tuple[str, ...] | None = None,
    marker: str = "可审查中文解答",
) -> SolutionGeneration:
    solution = _solution(problem, marker=marker)
    raw = "  " + solution.model_dump_json() + "\n"
    outcomes = attempt_outcomes or ("provider_error",) * (attempt_count - 1) + ("success",)
    return SolutionGeneration(
        status="success",
        raw_output=raw,
        solution=solution,
        attempt_count=attempt_count,
        attempt_outcomes=outcomes,
        raw_output_attempt=attempt_count,
        parse_attempted=True,
    )


def _parse_error(
    *,
    raw_output: str = "BROKEN_RAW_OUTPUT",
    message: str = "invalid structured output",
    attempt_count: int = 2,
    attempt_outcomes: tuple[str, ...] | None = None,
) -> SolutionGeneration:
    outcomes = attempt_outcomes or tuple("parse_error" for _ in range(attempt_count))
    return SolutionGeneration(
        status="parse_error",
        raw_output=raw_output,
        solution=None,
        attempt_count=attempt_count,
        attempt_outcomes=outcomes,
        error=ParsingError(message),
        raw_output_attempt=attempt_count,
        parse_attempted=True,
    )


def _provider_error(
    *,
    message: str = "temporary provider failure",
    attempt_count: int = 2,
    attempt_outcomes: tuple[str, ...] | None = None,
) -> SolutionGeneration:
    outcomes = attempt_outcomes or tuple("provider_error" for _ in range(attempt_count))
    return SolutionGeneration(
        status="provider_error",
        raw_output=None,
        solution=None,
        attempt_count=attempt_count,
        attempt_outcomes=outcomes,
        error=ProviderResponseError(message),
    )


class _ScriptedProvider:
    name = "scripted"

    def __init__(
        self,
        scripts: dict[str, list[SolutionGeneration | Exception]] | None = None,
        *,
        public_config: dict[str, Any] | None = None,
        default_attempt_count: int = 1,
    ) -> None:
        self.scripts = {
            problem_id: list(actions) for problem_id, actions in (scripts or {}).items()
        }
        self.calls: list[str] = []
        self.closed = False
        self.default_attempt_count = default_attempt_count
        self._public_config = public_config or {
            "provider": self.name,
            "model": "offline-scripted-model",
            "reasoning_effort": "low",
            "reasoning_effort_enabled": True,
            "timeout_seconds": 3.5,
            "max_retries": 3,
        }

    async def generate_solution_with_details(self, problem: ProblemSpec) -> SolutionGeneration:
        self.calls.append(problem.problem_id)
        actions = self.scripts.get(problem.problem_id)
        action = (
            actions.pop(0)
            if actions
            else _success(
                problem,
                attempt_count=self.default_attempt_count,
            )
        )
        if isinstance(action, Exception):
            raise action
        return action

    def public_generation_config(self) -> dict[str, Any]:
        return self._public_config

    async def aclose(self) -> None:
        self.closed = True


class _InvalidGenerationDetails:
    def __init__(
        self,
        *,
        attempt_count: int,
        attempt_outcomes: tuple[str, ...],
        status: str = "provider_error",
        parse_attempted: bool = False,
    ) -> None:
        self.status = status
        self.raw_output = None
        self.solution = None
        self.attempt_count = attempt_count
        self.attempt_outcomes = attempt_outcomes
        self.retry_count = max(0, attempt_count - 1)
        self.error = ProviderResponseError("invalid provider contract")
        self.raw_output_attempt = None
        self.parse_attempted = parse_attempted


def _write_dataset(tmp_path: Path, *, count: int) -> tuple[Path, list[ProblemSpec]]:
    problems = load_problems(SAMPLE_DATASET)[:count]
    dataset = tmp_path / "小规模题目.jsonl"
    dataset.write_text(
        "".join(problem.model_dump_json() + "\n" for problem in problems),
        encoding="utf-8",
    )
    return dataset, problems


def _write_observability_dataset(tmp_path: Path) -> tuple[Path, list[ProblemSpec]]:
    template = load_problems(SAMPLE_DATASET)[0]
    problems = [
        template.model_copy(update={"problem_id": f"observability_{index}"}) for index in range(5)
    ]
    dataset = tmp_path / "observability-problems.jsonl"
    dataset.write_text(
        "".join(problem.model_dump_json() + "\n" for problem in problems),
        encoding="utf-8",
    )
    return dataset, problems


def _read_records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _all_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _all_keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _all_keys(item)}
    return set()


def _assert_valid_timing(record: dict[str, Any]) -> None:
    started_at = datetime.fromisoformat(record["started_at"].replace("Z", "+00:00"))
    ended_at = datetime.fromisoformat(record["ended_at"].replace("Z", "+00:00"))
    assert ended_at >= started_at
    assert record["duration_seconds"] >= 0


async def test_success_artifacts_preserve_raw_parsed_utf8_and_reproducibility_metadata(tmp_path):
    provider = _ScriptedProvider(default_attempt_count=2)

    first = await run_baseline_experiment(SAMPLE_DATASET, provider, tmp_path / "runs")
    second = await run_baseline_experiment(
        SAMPLE_DATASET,
        _ScriptedProvider(),
        tmp_path / "runs",
    )

    assert first.run_id != second.run_id
    assert re.fullmatch(r"phase1_[A-Za-z0-9_-]+", first.run_id)
    assert first.run_dir == (tmp_path / "runs" / first.run_id).resolve()
    assert {path.name for path in first.run_dir.iterdir()} == {
        "manifest.json",
        "responses.jsonl",
        "summary.json",
    }
    assert provider.closed is True

    records = _read_records(first.responses_path)
    problems = load_problems(SAMPLE_DATASET)
    assert len(records) == len(problems) == 3
    for record, problem in zip(records, problems, strict=True):
        expected = _success(problem, attempt_count=2)
        assert record["status"] == "success"
        assert record["parse_status"] == "parsed"
        assert record["raw_output"] == expected.raw_output
        assert isinstance(record["raw_output"], str)
        assert record["solution_trace"] == expected.solution.model_dump(mode="json")
        assert isinstance(record["solution_trace"], dict)
        assert record["attempt_count"] == 2
        assert record["retry_count"] == 1
        assert record["attempt_outcomes"] == ["provider_error", "success"]
        assert record["raw_output_attempt"] == 2
        assert record["parse_attempted"] is True
        assert record["run_id"] == first.run_id
        assert record["model"] == "offline-scripted-model"
        _assert_valid_timing(record)

    response_bytes = first.responses_path.read_bytes()
    assert response_bytes.decode("utf-8").encode("utf-8") == response_bytes
    assert "可审查中文解答" in response_bytes.decode("utf-8")

    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    assert manifest["run_id"] == first.run_id
    assert manifest["experiment_label"] == "self_constructed_mvp_fixture_pilot"
    assert manifest["dataset"]["path"] == str(SAMPLE_DATASET.resolve())
    assert manifest["dataset"]["sha256"] == hashlib.sha256(SAMPLE_DATASET.read_bytes()).hexdigest()
    assert manifest["dataset"]["problem_count"] == 3
    assert re.fullmatch(r"[0-9a-f]{40}", manifest["git"]["commit"])
    current_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=REPOSITORY,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert manifest["git"]["branch"] == current_branch
    assert manifest["environment"]["python"]["version"] == platform.python_version()
    assert set(manifest["environment"]["direct_dependencies"]) == {
        "openai",
        "pydantic",
        "pydantic-settings",
        "rich",
        "typer",
    }
    assert manifest["provider_config"] == provider.public_generation_config()
    if manifest["git"]["dirty"]:
        assert re.fullmatch(r"[0-9a-f]{64}", manifest["git"]["working_tree_sha256"])
    else:
        assert manifest["git"]["working_tree_sha256"] is None
    first_invocation = manifest["invocations"][0]
    assert first_invocation["git"] == manifest["git"]
    assert first_invocation["environment"] == manifest["environment"]

    summary = json.loads(first.summary_path.read_text(encoding="utf-8"))
    assert summary["metrics_scope"] == "generation_and_parsing_only"
    assert summary["total_problem_count"] == 3
    assert summary["record_count"] == 3
    assert summary["success_count"] == 3
    assert summary["failure_count"] == 0
    assert summary["first_attempt_parse_success_count"] == 0
    assert summary["parse_failure_encountered_count"] == 0
    assert summary["repair_attempted_count"] == 0
    assert summary["repair_success_count"] == 0
    assert summary["terminal_parse_error_count"] == 0
    assert summary["average_attempt_count"] == 2.0
    assert summary["average_retry_count"] == 1.0
    forbidden_metric_keys = {
        key
        for key in _all_keys(summary)
        if key.startswith("functional_") or key.startswith("error_detection_")
    }
    assert forbidden_metric_keys == set()


async def test_parse_and_provider_failures_do_not_stop_later_problems(tmp_path):
    dataset, problems = _write_dataset(tmp_path, count=3)
    provider = _ScriptedProvider(
        {
            problems[0].problem_id: [_parse_error(raw_output="UNPARSEABLE_BUT_PRESERVED")],
            problems[1].problem_id: [ProviderResponseError("upstream unavailable")],
            problems[2].problem_id: [_success(problems[2])],
        }
    )

    result = await run_baseline_experiment(dataset, provider, tmp_path / "runs")

    records = _read_records(result.responses_path)
    assert provider.calls == [problem.problem_id for problem in problems]
    assert [record["status"] for record in records] == [
        "parse_error",
        "provider_error",
        "success",
    ]
    assert records[0]["raw_output"] == "UNPARSEABLE_BUT_PRESERVED"
    assert records[0]["attempt_outcomes"] == ["parse_error", "parse_error"]
    assert records[0]["raw_output_attempt"] == 2
    assert records[0]["parse_attempted"] is True
    assert records[0]["solution_trace"] is None
    assert records[0]["error"]["type"] == "ParsingError"
    assert records[1]["raw_output"] is None
    assert records[1]["attempt_outcomes"] == ["provider_error"]
    assert records[1]["raw_output_attempt"] is None
    assert records[1]["parse_attempted"] is False
    assert records[1]["solution_trace"] is None
    assert records[1]["error"]["type"] == "ProviderResponseError"
    assert records[2]["solution_trace"]["problem_id"] == problems[2].problem_id
    assert all(record["run_id"] == result.run_id for record in records)
    assert all(record["attempt_count"] >= 1 for record in records)
    assert records[0]["retry_count"] == 1
    assert all(record["retry_count"] <= 3 for record in records)
    assert all(_assert_valid_timing(record) is None for record in records)

    summary = result.summary
    assert summary["final_outcome_counts"] == {
        "success": 1,
        "parse_error": 1,
        "provider_error": 1,
        "failure": 2,
    }
    assert summary["parse_success_rate"] == pytest.approx(0.5)
    assert summary["first_attempt_parse_success_count"] == 1
    assert summary["parse_failure_encountered_count"] == 1
    assert summary["repair_attempted_count"] == 1
    assert summary["repair_success_count"] == 0
    assert summary["terminal_parse_error_count"] == 1
    assert summary["average_attempt_count"] == pytest.approx(4 / 3)
    assert summary["average_retry_count"] == pytest.approx(1 / 3)
    assert summary["record_status_counts"] == {
        "parse_error": 1,
        "provider_error": 1,
        "success": 1,
    }


async def test_mixed_retry_records_which_raw_was_parsed_and_counts_parse_failure(tmp_path):
    dataset, problems = _write_dataset(tmp_path, count=1)
    mixed_failure = SolutionGeneration(
        status="provider_error",
        raw_output="first attempt was malformed",
        solution=None,
        attempt_count=2,
        attempt_outcomes=("parse_error", "provider_error"),
        error=ProviderResponseError("second attempt timed out"),
        raw_output_attempt=1,
        parse_attempted=True,
    )
    result = await run_baseline_experiment(
        dataset,
        _ScriptedProvider({problems[0].problem_id: [mixed_failure]}),
        tmp_path / "runs",
    )

    record = _read_records(result.responses_path)[0]
    assert record["status"] == "provider_error"
    assert record["parse_status"] == "failed"
    assert record["raw_output"] == "first attempt was malformed"
    assert record["raw_output_attempt"] == 1
    assert record["attempt_count"] == 2
    assert record["attempt_outcomes"] == ["parse_error", "provider_error"]
    assert result.summary["parse_attempted_count"] == 1
    assert result.summary["parse_success_count"] == 0
    assert result.summary["parse_failure_count"] == 1
    assert result.summary["parse_success_rate"] == 0.0
    assert result.summary["parse_failure_encountered_count"] == 1
    assert result.summary["repair_attempted_count"] == 1
    assert result.summary["repair_success_count"] == 0
    assert result.summary["terminal_parse_error_count"] == 0


async def test_summary_rebuilds_parse_and_repair_metrics_from_final_attempt_histories(
    tmp_path,
    caplog,
):
    dataset, problems = _write_observability_dataset(tmp_path)
    raw_canary = "RAW_OBSERVABILITY_CANARY_93c1"
    error_canary = "ERROR_DETAIL_CANARY_93c1"
    parse_then_provider = SolutionGeneration(
        status="provider_error",
        raw_output=raw_canary,
        solution=None,
        attempt_count=2,
        attempt_outcomes=("parse_error", "provider_error"),
        error=ProviderResponseError(error_canary),
        raw_output_attempt=1,
        parse_attempted=True,
    )
    provider = _ScriptedProvider(
        {
            problems[0].problem_id: [_success(problems[0])],
            problems[1].problem_id: [
                _success(
                    problems[1],
                    attempt_count=2,
                    attempt_outcomes=("parse_error", "success"),
                )
            ],
            problems[2].problem_id: [
                _parse_error(
                    raw_output=raw_canary,
                    message=error_canary,
                    attempt_outcomes=("parse_error", "parse_error"),
                )
            ],
            problems[3].problem_id: [
                _success(
                    problems[3],
                    attempt_count=2,
                    attempt_outcomes=("provider_error", "success"),
                )
            ],
            problems[4].problem_id: [parse_then_provider],
        }
    )

    result = await run_baseline_experiment(dataset, provider, tmp_path / "runs")

    records = _read_records(result.responses_path)
    assert [record["attempt_outcomes"] for record in records] == [
        ["success"],
        ["parse_error", "success"],
        ["parse_error", "parse_error"],
        ["provider_error", "success"],
        ["parse_error", "provider_error"],
    ]
    assert all(record["attempt_count"] == len(record["attempt_outcomes"]) for record in records)
    summary = result.summary
    assert summary["first_attempt_parse_success_count"] == 1
    assert summary["parse_failure_encountered_count"] == 3
    assert summary["repair_attempted_count"] == 3
    assert summary["repair_success_count"] == 1
    assert summary["terminal_parse_error_count"] == 1
    assert summary["average_attempt_count"] == pytest.approx(1.8)
    assert summary["average_retry_count"] == pytest.approx(0.8)
    assert summary["parse_attempted_count"] == 5
    assert summary["parse_success_count"] == 3
    assert summary["parse_failure_count"] == 2
    assert summary["parse_success_rate"] == pytest.approx(0.6)

    safe_artifacts = result.manifest_path.read_text(
        encoding="utf-8"
    ) + result.summary_path.read_text(encoding="utf-8")
    assert raw_canary not in safe_artifacts
    assert error_canary not in safe_artifacts
    assert raw_canary not in caplog.text
    assert error_canary not in caplog.text


@pytest.mark.parametrize(
    ("case_name", "attempt_count", "attempt_outcomes", "status", "parse_attempted"),
    [
        ("count_mismatch", 2, ("provider_error",), "provider_error", False),
        ("illegal_enum", 1, ("not_an_outcome",), "provider_error", False),
        ("missing_raw", 1, ("parse_error",), "parse_error", True),
    ],
)
async def test_runner_rejects_invalid_structural_provider_attempt_metadata(
    tmp_path,
    case_name,
    attempt_count,
    attempt_outcomes,
    status,
    parse_attempted,
):
    dataset, problems = _write_dataset(tmp_path, count=1)
    invalid = _InvalidGenerationDetails(
        attempt_count=attempt_count,
        attempt_outcomes=attempt_outcomes,
        status=status,
        parse_attempted=parse_attempted,
    )
    provider = _ScriptedProvider(
        {problems[0].problem_id: [invalid]},  # type: ignore[list-item]
    )

    with pytest.raises(BaselineExperimentError, match="attempt"):
        await run_baseline_experiment(
            dataset,
            provider,
            tmp_path / "runs",
            run_id=f"invalid_attempt_metadata_{case_name}",
        )

    assert provider.closed is True
    responses = tmp_path / "runs" / f"invalid_attempt_metadata_{case_name}" / "responses.jsonl"
    assert responses.read_bytes() == b""


async def test_resume_skips_success_retries_failure_and_summary_matches_event_log(tmp_path):
    dataset, problems = _write_dataset(tmp_path, count=2)
    run_id = "resume_semantics"
    first_provider = _ScriptedProvider(
        {
            problems[0].problem_id: [_success(problems[0])],
            problems[1].problem_id: [_provider_error()],
        }
    )
    await run_baseline_experiment(dataset, first_provider, tmp_path / "runs", run_id=run_id)
    resumed_provider = _ScriptedProvider(
        {
            problems[0].problem_id: [AssertionError("successful problem must be skipped")],
            problems[1].problem_id: [_success(problems[1], attempt_count=2)],
        }
    )

    resumed = await run_baseline_experiment(
        dataset,
        resumed_provider,
        tmp_path / "runs",
        run_id=run_id,
        resume=True,
    )

    records = _read_records(resumed.responses_path)
    assert resumed_provider.calls == [problems[1].problem_id]
    assert [record["status"] for record in records] == [
        "success",
        "provider_error",
        "skipped",
        "success",
    ]
    skipped = records[2]
    assert skipped["problem_id"] == problems[0].problem_id
    assert skipped["attempt_count"] == skipped["retry_count"] == 0
    assert skipped["attempt_outcomes"] == []
    assert skipped["raw_output_attempt"] is None
    assert skipped["parse_attempted"] is False
    assert skipped["raw_output"] is None
    assert skipped["solution_trace"] is None
    assert skipped["error"] is None

    final_by_problem: dict[str, dict[str, Any]] = {}
    generation_statuses = {"success", "parse_error", "provider_error"}
    for record in records:
        if record["status"] in generation_statuses:
            final_by_problem[record["problem_id"]] = record
    final_counts = Counter(record["status"] for record in final_by_problem.values())
    history_counts = dict(sorted(Counter(record["status"] for record in records).items()))
    durations = [record["duration_seconds"] for record in final_by_problem.values()]

    summary = json.loads(resumed.summary_path.read_text(encoding="utf-8"))
    assert summary["total_problem_count"] == len(problems)
    assert summary["record_count"] == len(records)
    assert summary["record_status_counts"] == history_counts
    assert summary["final_outcome_counts"] == {
        "success": final_counts["success"],
        "parse_error": final_counts["parse_error"],
        "provider_error": final_counts["provider_error"],
        "failure": final_counts["parse_error"] + final_counts["provider_error"],
    }
    assert summary["success_count"] == 2
    assert summary["failure_count"] == 0
    assert summary["pending_count"] == 0
    assert summary["parse_success_rate"] == 1.0
    assert summary["first_attempt_parse_success_count"] == 1
    assert summary["parse_failure_encountered_count"] == 0
    assert summary["repair_attempted_count"] == 0
    assert summary["repair_success_count"] == 0
    assert summary["terminal_parse_error_count"] == 0
    assert summary["average_attempt_count"] == 1.5
    assert summary["average_retry_count"] == 0.5
    assert summary["average_duration_seconds"] == pytest.approx(sum(durations) / len(durations))
    assert summary["skipped_count"] == 1
    assert summary["invocation"]["status_counts"] == {"skipped": 1, "success": 1}


async def test_new_writer_refuses_to_resume_legacy_v1_artifact(tmp_path):
    dataset, _ = _write_dataset(tmp_path, count=1)
    first = await run_baseline_experiment(
        dataset,
        _ScriptedProvider(),
        tmp_path / "runs",
        run_id="legacy_resume_guard",
    )
    original_responses = first.responses_path.read_bytes()
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 1
    first.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    resumed_provider = _ScriptedProvider()

    with pytest.raises(BaselineExperimentError, match="artifact schema"):
        await run_baseline_experiment(
            dataset,
            resumed_provider,
            tmp_path / "runs",
            run_id="legacy_resume_guard",
            resume=True,
        )

    assert resumed_provider.calls == []
    assert resumed_provider.closed is True
    assert first.responses_path.read_bytes() == original_responses


async def test_v2_resume_rejects_response_without_attempt_outcomes(tmp_path):
    dataset, _ = _write_dataset(tmp_path, count=1)
    first = await run_baseline_experiment(
        dataset,
        _ScriptedProvider(),
        tmp_path / "runs",
        run_id="mixed_response_schema_guard",
    )
    record = _read_records(first.responses_path)[0]
    record.pop("attempt_outcomes")
    first.responses_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    resumed_provider = _ScriptedProvider()

    with pytest.raises(BaselineExperimentError, match="attempt metadata"):
        await run_baseline_experiment(
            dataset,
            resumed_provider,
            tmp_path / "runs",
            run_id="mixed_response_schema_guard",
            resume=True,
        )

    assert resumed_provider.calls == []
    assert resumed_provider.closed is True


async def test_sensitive_configuration_raw_output_and_errors_are_redacted(tmp_path):
    dataset, problems = _write_dataset(tmp_path, count=1)
    fake_api_key = "FAKE_API_KEY_CANARY_7e61"
    bearer_token = "BEARER_TOKEN_CANARY_7e61"
    authorization_value = "AUTHORIZATION_VALUE_CANARY_7e61"
    raw = json.dumps(
        {
            "api_key": fake_api_key,
            "Authorization": f"Basic {bearer_token}",
            "password": authorization_value,
        }
    )
    provider = _ScriptedProvider(
        {
            problems[0].problem_id: [
                _parse_error(
                    raw_output=raw,
                    message=(
                        "headers={'Authorization': 'Basic "
                        f"{bearer_token}', 'X-Api-Key': '{fake_api_key}'}}; "
                        f"password={authorization_value}"
                    ),
                )
            ]
        },
        public_config={
            "provider": "scripted",
            "model": "safe-model-name",
            "reasoning_effort": None,
            "timeout_seconds": 2,
            "max_retries": 1,
            "api_key": fake_api_key,
            "Authorization": f"Bearer {bearer_token}",
            "nested": {
                "headers": {"Authorization": authorization_value},
                "safe": "retained",
            },
            "endpoint": (
                f"https://user:{authorization_value}@example.invalid/v1?api_key={fake_api_key}"
            ),
        },
    )
    provider.secret_material = fake_api_key

    result = await run_baseline_experiment(dataset, provider, tmp_path / "runs")

    artifact_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (result.manifest_path, result.responses_path, result.summary_path)
    )
    for secret in (fake_api_key, bearer_token, authorization_value):
        assert secret not in artifact_text
    assert f"Authorization': 'Basic {bearer_token}" not in artifact_text
    assert "<redacted>" in artifact_text

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    config = manifest["provider_config"]
    assert "api_key" not in config
    assert "Authorization" not in config
    assert config["nested"] == {"safe": "retained"}
    assert config["model"] == "safe-model-name"
    response = _read_records(result.responses_path)[0]
    assert response["status"] == "parse_error"
    assert response["error"]["type"] == "ParsingError"
    assert response["error"]["message"] != raw


async def test_interrupted_atomic_replace_leaves_completed_jsonl_records_readable(
    tmp_path,
    monkeypatch,
):
    dataset, problems = _write_dataset(tmp_path, count=2)
    provider = _ScriptedProvider()
    original_replace = baseline_runner.os.replace
    response_replace_count = 0

    def interrupt_second_record(source, destination):
        nonlocal response_replace_count
        if Path(destination).name == "responses.jsonl":
            response_replace_count += 1
            # Initial empty file, first complete append, then interrupted second append.
            if response_replace_count == 3:
                raise KeyboardInterrupt("simulated interruption before atomic replacement")
        return original_replace(source, destination)

    monkeypatch.setattr(baseline_runner.os, "replace", interrupt_second_record)

    with pytest.raises(KeyboardInterrupt, match="simulated interruption"):
        await run_baseline_experiment(
            dataset,
            provider,
            tmp_path / "runs",
            run_id="interrupted_run",
        )

    responses_path = tmp_path / "runs" / "interrupted_run" / "responses.jsonl"
    raw_bytes = responses_path.read_bytes()
    assert raw_bytes.endswith(b"\n")
    records = [json.loads(line) for line in raw_bytes.decode("utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["status"] == "success"
    assert provider.closed is True
    assert list(responses_path.parent.glob(".responses.jsonl.*.tmp")) == []

    interrupted_manifest = json.loads(
        (responses_path.parent / "manifest.json").read_text(encoding="utf-8")
    )
    assert interrupted_manifest["status"] == "running"
    assert interrupted_manifest["invocations"][0]["status"] == "running"

    monkeypatch.setattr(baseline_runner.os, "replace", original_replace)
    resumed_provider = _ScriptedProvider(
        {problems[1].problem_id: [_success(problems[1])]},
    )
    resumed = await run_baseline_experiment(
        dataset,
        resumed_provider,
        tmp_path / "runs",
        run_id="interrupted_run",
        resume=True,
    )
    resumed_records = _read_records(resumed.responses_path)
    assert [record["status"] for record in resumed_records] == [
        "success",
        "skipped",
        "success",
    ]
    assert resumed_provider.calls == [problems[1].problem_id]
    invocation_statuses = [item["status"] for item in resumed.manifest["invocations"]]
    assert invocation_statuses == ["interrupted", "completed"]
    assert "interrupted_at" in resumed.manifest["invocations"][0]
    assert all("git" in item and "environment" in item for item in resumed.manifest["invocations"])


async def test_resume_rejects_changed_worktree_fingerprint(tmp_path):
    dataset, _ = _write_dataset(tmp_path, count=1)
    first = await run_baseline_experiment(
        dataset,
        _ScriptedProvider(),
        tmp_path / "runs",
        run_id="fingerprint_guard",
    )
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    manifest["git"]["dirty"] = True
    manifest["git"]["working_tree_sha256"] = "0" * 64
    first.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BaselineExperimentError, match="working-tree fingerprint"):
        await run_baseline_experiment(
            dataset,
            _ScriptedProvider(),
            tmp_path / "runs",
            run_id="fingerprint_guard",
            resume=True,
        )


async def test_resume_rejects_changed_endpoint_fingerprint(tmp_path):
    dataset, _ = _write_dataset(tmp_path, count=1)
    base_config = _ScriptedProvider().public_generation_config()
    first_config = {**base_config, "endpoint_sha256": "1" * 64}
    second_config = {**base_config, "endpoint_sha256": "2" * 64}
    await run_baseline_experiment(
        dataset,
        _ScriptedProvider(public_config=first_config),
        tmp_path / "runs",
        run_id="endpoint_guard",
    )
    resumed_provider = _ScriptedProvider(public_config=second_config)

    with pytest.raises(BaselineExperimentError, match="public generation config differs"):
        await run_baseline_experiment(
            dataset,
            resumed_provider,
            tmp_path / "runs",
            run_id="endpoint_guard",
            resume=True,
        )
    assert resumed_provider.closed is True


def test_cli_prints_recovery_coordinates_before_provider_initialization_failure(monkeypatch):
    def fail_provider_creation(*args, **kwargs):
        raise ProviderAuthError("test provider setup failure")

    monkeypatch.setattr("tracejudge_hy3.cli._make_provider", fail_provider_creation)

    result = CliRunner().invoke(
        app,
        ["baseline", "--provider", "hy3", "--output-dir", "unused-output"],
    )

    assert result.exit_code == 1
    assert re.search(r"phase1_[A-Za-z0-9_-]+", result.output)
    assert "产物目录:" in result.output
    assert result.output.index("run_id:") < result.output.index("基线生成失败")


async def test_invalid_unicode_from_one_provider_response_does_not_abort_batch(tmp_path):
    dataset, problems = _write_dataset(tmp_path, count=2)
    provider = _ScriptedProvider(
        {
            problems[0].problem_id: [_parse_error(raw_output="bad\ud800raw")],
            problems[1].problem_id: [_success(problems[1])],
        }
    )

    result = await run_baseline_experiment(dataset, provider, tmp_path / "runs")

    records = _read_records(result.responses_path)
    assert [record["status"] for record in records] == ["parse_error", "success"]
    assert records[0]["raw_output"] == "bad\ufffdraw"
    assert result.responses_path.read_bytes().decode("utf-8")


async def test_dataset_surrogates_normalize_consistently_and_resume_still_skips(tmp_path):
    problem = load_problems(SAMPLE_DATASET)[0]
    problem = problem.model_copy(
        update={
            "problem_id": "bad\ud800id",
            "source": "source\ud800name",
            "visible_test_cases": [
                problem.visible_test_cases[0].model_copy(update={"case_id": "case\ud800id"})
            ],
        }
    )
    dataset = tmp_path / "surrogate-dataset.jsonl"
    dataset.write_text(
        json.dumps(problem.model_dump(mode="json"), ensure_ascii=True) + "\n",
        encoding="utf-8",
    )

    surrogate_solution = _solution(problem)
    surrogate_generation = SolutionGeneration(
        status="success",
        raw_output=json.dumps(surrogate_solution.model_dump(mode="json"), ensure_ascii=True),
        solution=surrogate_solution,
        attempt_count=1,
        attempt_outcomes=("success",),
        raw_output_attempt=1,
        parse_attempted=True,
    )
    first = await run_baseline_experiment(
        dataset,
        _ScriptedProvider({problem.problem_id: [surrogate_generation]}),
        tmp_path / "runs",
        run_id="surrogate_resume",
    )
    resumed_provider = _ScriptedProvider(
        {problem.problem_id: [AssertionError("normalized successful ID must be skipped")]}
    )
    resumed = await run_baseline_experiment(
        dataset,
        resumed_provider,
        tmp_path / "runs",
        run_id="surrogate_resume",
        resume=True,
    )

    first_record = _read_records(first.responses_path)[0]
    assert first_record["problem_id"] == "bad\ufffdid"
    assert resumed_provider.calls == []
    assert _read_records(resumed.responses_path)[-1]["status"] == "skipped"
    assert resumed.manifest["dataset"]["sources"] == {"source\ufffdname": 1}
    visible_summary = resumed.manifest["dataset"]["visible_tests"]["per_problem"]
    assert visible_summary["bad\ufffdid"]["case_ids"] == ["case\ufffdid"]


async def test_escaped_credentials_inside_valid_solution_are_redacted_from_both_views(tmp_path):
    dataset, problems = _write_dataset(tmp_path, count=1)
    api_canary = "NESTED_API_CANARY_541c"
    auth_canary = "NESTED_AUTH_CANARY_541c"
    solution = _solution(problems[0]).model_copy(
        update={
            "code": (
                f"{problems[0].function_signature}\n"
                f'    config = {{"api_key": "{api_canary}", '
                f'"Authorization": "Basic {auth_canary}"}}\n'
                "    return config\n"
            )
        }
    )
    generation = SolutionGeneration(
        status="success",
        raw_output=solution.model_dump_json(),
        solution=solution,
        attempt_count=1,
        attempt_outcomes=("success",),
        raw_output_attempt=1,
        parse_attempted=True,
    )
    result = await run_baseline_experiment(
        dataset,
        _ScriptedProvider({problems[0].problem_id: [generation]}),
        tmp_path / "runs",
    )

    record = _read_records(result.responses_path)[0]
    serialized = json.dumps(record, ensure_ascii=False)
    assert api_canary not in serialized
    assert auth_canary not in serialized
    assert "<redacted>" in record["raw_output"]
    assert "<redacted>" in record["solution_trace"]["code"]


async def test_mock_baseline_never_uses_network_execution_analysis_or_evaluation(
    tmp_path,
    monkeypatch,
):
    dataset, _ = _write_dataset(tmp_path, count=1)

    def forbidden(*args, **kwargs):
        raise AssertionError("phase-one baseline crossed a forbidden boundary")

    async def forbidden_async(*args, **kwargs):
        forbidden()

    for variable in ("HY3_BASE_URL", "HY3_API_KEY", "HY3_MODEL"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(
        "tracejudge_hy3.providers.hy3_openai.openai.AsyncOpenAI",
        forbidden,
    )
    monkeypatch.setattr("tracejudge_hy3.pipeline.runner.run_pipeline", forbidden_async)
    monkeypatch.setattr("tracejudge_hy3.static_analysis.ast_analyzer.analyze_code", forbidden)
    monkeypatch.setattr(
        "tracejudge_hy3.sandbox.trusted_local.TrustedLocalSandbox.run",
        forbidden,
    )
    monkeypatch.setattr(
        "tracejudge_hy3.sandbox.docker_backend.DockerSandbox.run",
        forbidden,
    )
    monkeypatch.setattr(
        "tracejudge_hy3.evaluator.rule_based.evaluate_alignment_rules",
        forbidden,
    )
    monkeypatch.setattr(MockProvider, "evaluate_process", forbidden_async)

    result = await run_baseline_experiment(
        dataset,
        MockProvider(),
        tmp_path / "runs",
    )

    records = _read_records(result.responses_path)
    assert [record["status"] for record in records] == ["success"]
    assert result.manifest["provider_config"]["provider"] == "mock"
    assert result.summary["metrics_scope"] == "generation_and_parsing_only"


async def test_unknown_mock_problem_never_uses_reference_code_fallback(tmp_path):
    problem = load_problems(SAMPLE_DATASET)[0].model_copy(
        update={
            "problem_id": "unknown_baseline_fixture",
            "reference_code": "REFERENCE_CODE_MUST_NEVER_REACH_BASELINE_OUTPUT",
        }
    )
    dataset = tmp_path / "unknown.jsonl"
    dataset.write_text(problem.model_dump_json() + "\n", encoding="utf-8")

    result = await run_baseline_experiment(dataset, MockProvider(), tmp_path / "runs")

    record = _read_records(result.responses_path)[0]
    assert record["status"] == "provider_error"
    assert record["raw_output"] is None
    assert record["solution_trace"] is None
    artifact_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (result.manifest_path, result.responses_path, result.summary_path)
    )
    assert "REFERENCE_CODE_MUST_NEVER_REACH_BASELINE_OUTPUT" not in artifact_text


async def test_mock_fixture_matching_ignores_private_problem_fields(tmp_path):
    problem = load_problems(SAMPLE_DATASET)[0]
    private_canaries = {
        "PRIVATE_REFERENCE_CANARY",
        "PRIVATE_HIDDEN_EXPECTED_CANARY",
        "PRIVATE_CHALLENGE_EXPECTED_CANARY",
    }
    problem = problem.model_copy(
        update={
            "reference_code": "PRIVATE_REFERENCE_CANARY",
            "hidden_test_cases": [
                problem.hidden_test_cases[0].model_copy(
                    update={"expected": "PRIVATE_HIDDEN_EXPECTED_CANARY"}
                )
            ],
            "challenge_test_cases": [
                problem.challenge_test_cases[0].model_copy(
                    update={"expected": "PRIVATE_CHALLENGE_EXPECTED_CANARY"}
                )
            ],
        }
    )
    dataset = tmp_path / "private-fields-changed.jsonl"
    dataset.write_text(problem.model_dump_json() + "\n", encoding="utf-8")

    result = await run_baseline_experiment(dataset, MockProvider(), tmp_path / "runs")

    record = _read_records(result.responses_path)[0]
    assert record["status"] == "success"
    artifact_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (result.manifest_path, result.responses_path, result.summary_path)
    )
    assert not [canary for canary in private_canaries if canary in artifact_text]
