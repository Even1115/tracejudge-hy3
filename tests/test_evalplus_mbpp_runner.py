from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from mbppplus_fixtures import REVISION, official_raw_document, write_snapshot

import tracejudge_hy3.evalplus_mbpp.runner as mbpp_runner
from tracejudge_hy3.dataset.mbppplus import convert_mbppplus, sample_mbppplus
from tracejudge_hy3.evalplus_mbpp.runner import (
    ExecutorPreflight,
    ExecutorTaskOutcome,
    MbppExperimentError,
    MockMbppEvalPlusExecutor,
    load_validated_mbpp_inputs,
    run_mbpp_experiment,
)

SEED = 20260905
SAMPLE_COUNT = 2
FAILURE_CANARY = "PRIVATE_EVALPLUS_FAILURE_INPUT_CANARY"
PRIVATE_CANARIES = (
    "PRIVATE_CANONICAL_CANARY",
    "PRIVATE_CONTRACT_CANARY",
    "PRIVATE_BASE_INPUT_CANARY",
    "PRIVATE_PLUS_INPUT_CANARY",
    "PRIVATE_ASSERTION_CANARY",
    FAILURE_CANARY,
)


@pytest.fixture()
def selection_bundle(tmp_path: Path, monkeypatch) -> dict[str, Any]:
    monkeypatch.setattr(mbpp_runner, "_require_non_trackable_run_directory", lambda _run_dir: None)
    snapshot = write_snapshot(tmp_path / "raw")
    conversion = convert_mbppplus(
        input_path=snapshot.input_path,
        revision=REVISION,
        source_manifest_path=snapshot.source_manifest_path,
        output_dir=tmp_path / "full",
    )
    sample = sample_mbppplus(
        dataset_path=conversion.dataset_path,
        source_manifest_path=conversion.manifest_path,
        count=SAMPLE_COUNT,
        seed=SEED,
        output_dir=tmp_path / "sample",
    )
    candidates_path = tmp_path / "candidates.jsonl"
    rows = [
        {
            "task_id": task_id,
            "candidate_id": f"candidate-{index}",
            "code": (
                f"def candidate_impl_{index}(values):\n"
                f"    marker = 'CANDIDATE_BODY_CANARY_{index}'\n"
                "    return values\n"
            ),
        }
        for index, task_id in enumerate(sample.selected_problem_ids)
    ]
    candidates_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    candidates_path.chmod(0o600)
    return {
        "tmp_path": tmp_path,
        "manifest_path": sample.manifest_path,
        "candidates_path": candidates_path,
        "selected_ids": list(sample.selected_problem_ids),
        "output_dir": tmp_path / "phase2",
    }


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _write_candidates(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
    )
    path.chmod(0o600)


class _StubDockerExecutor:
    """Deterministic docker-mode executor; never touches Docker or the host."""

    mode = "docker"

    def __init__(self, *, fail_task: str | None = None, ready: bool = True) -> None:
        self.fail_task = fail_task
        self.ready = ready
        self.task_calls: list[str] = []

    def public_identity(self) -> dict[str, Any]:
        return {"name": "stub_docker_executor", "version": 1, "candidate_execution": True}

    def preflight(self, *, task_metadata, workspace) -> ExecutorPreflight:
        return ExecutorPreflight(
            ready=self.ready,
            runtime={"name": "stub", "task_count": len(task_metadata)},
            infrastructure_error_type=None if self.ready else "docker_unavailable",
            diagnostics={},
        )

    def run_task(self, *, sample, task_metadata, workspace) -> ExecutorTaskOutcome:
        assert task_metadata.problem_id == sample.task_id
        self.task_calls.append(sample.task_id)
        fail = sample.task_id == self.fail_task
        raw = official_raw_document(
            sample.task_id,
            sample.solution,
            base_status="pass",
            plus_status="fail" if fail else "pass",
            plus_fail_tests=[FAILURE_CANARY] if fail else [],
        )
        return ExecutorTaskOutcome(
            problem_id=sample.task_id,
            started_at="2026-09-05T00:00:00.000Z",
            ended_at="2026-09-05T00:00:01.000Z",
            duration_seconds=1.0,
            raw_result=raw,
            infrastructure_error_type=None,
            diagnostics={"exit_code": 0},
        )


def test_load_validated_inputs_binds_selection_and_candidates(selection_bundle):
    inputs = load_validated_mbpp_inputs(
        selection_bundle["manifest_path"], selection_bundle["candidates_path"]
    )
    assert [sample.task_id for sample in inputs.samples] == selection_bundle["selected_ids"]
    assert len(inputs.candidates) == SAMPLE_COUNT
    assert inputs.dataset.revision == REVISION
    assert all(item.entry_point for item in inputs.task_metadata)


def test_loader_rejects_tampered_projection_and_candidate_rows(selection_bundle):
    bundle_dir = selection_bundle["manifest_path"].parent
    problems_path = bundle_dir / "problems.jsonl"
    problems_path.write_bytes(problems_path.read_bytes() + b"\n")
    with pytest.raises(MbppExperimentError, match="does not match its dataset manifest"):
        load_validated_mbpp_inputs(
            selection_bundle["manifest_path"], selection_bundle["candidates_path"]
        )

    # Rebuild a valid bundle because the previous tampering invalidated it.
    snapshot = write_snapshot(selection_bundle["tmp_path"] / "raw2")
    conversion = convert_mbppplus(
        input_path=snapshot.input_path,
        revision=REVISION,
        source_manifest_path=snapshot.source_manifest_path,
        output_dir=selection_bundle["tmp_path"] / "full2",
    )
    sample = sample_mbppplus(
        dataset_path=conversion.dataset_path,
        source_manifest_path=conversion.manifest_path,
        count=SAMPLE_COUNT,
        seed=SEED,
        output_dir=selection_bundle["tmp_path"] / "sample2",
    )
    rows = [
        {"task_id": task_id, "candidate_id": f"candidate-{i}", "code": "def f(x):\n    return x\n"}
        for i, task_id in enumerate(sample.selected_problem_ids)
    ]
    rows[1]["task_id"] = rows[0]["task_id"]
    _write_candidates(selection_bundle["tmp_path"] / "candidates_dupe.jsonl", rows)
    with pytest.raises(MbppExperimentError, match="invalid or duplicated"):
        load_validated_mbpp_inputs(
            sample.manifest_path, selection_bundle["tmp_path"] / "candidates_dupe.jsonl"
        )


def test_mock_dry_run_publishes_safe_artifacts_without_execution(selection_bundle):
    result = run_mbpp_experiment(
        selection_bundle["manifest_path"],
        selection_bundle["candidates_path"],
        selection_bundle["output_dir"],
        executor=MockMbppEvalPlusExecutor(),
        run_id="phase2_mbpp_mock_fixture",
    )

    assert {path.name for path in result.run_dir.iterdir()} == {
        "manifest.json",
        "samples.jsonl",
        "evalplus_raw_results.json",
        "results.jsonl",
        "summary.json",
        "execution.log",
    }
    assert stat.S_IMODE(result.run_dir.stat().st_mode) == 0o700
    records = _records(result.results_path)
    assert [record["problem_id"] for record in records] == selection_bundle["selected_ids"]
    assert all(record["infrastructure_status"] == "mocked" for record in records)
    assert all(record["error_type"] == "mock_not_executed" for record in records)
    assert result.summary["actual_execution_count"] == 0
    assert result.summary["mock_not_executed_count"] == SAMPLE_COUNT
    assert result.summary["infrastructure_error_count"] == 0
    assert result.manifest["execution_mode"] == "mock"
    assert result.manifest["experiment_label"] == "mbppplus_2_evalplus_execution_pilot"
    assert "fixed_2_problem_subset_not_full_mbppplus" in result.manifest["limitations"]

    # Safe artifacts never carry candidate bodies or withheld dataset content.
    for artifact in (
        result.results_path,
        result.summary_path,
        result.manifest_path,
        result.execution_log_path,
    ):
        payload = artifact.read_bytes()
        for canary in PRIVATE_CANARIES + ("CANDIDATE_BODY_CANARY",):
            assert canary.encode() not in payload


def test_stub_execution_normalizes_pass_fail_and_never_leaks_fail_inputs(selection_bundle):
    fail_task = selection_bundle["selected_ids"][1]
    executor = _StubDockerExecutor(fail_task=fail_task)
    result = run_mbpp_experiment(
        selection_bundle["manifest_path"],
        selection_bundle["candidates_path"],
        selection_bundle["output_dir"],
        executor=executor,
        run_id="phase2_mbpp_stub_fixture",
    )

    assert sorted(executor.task_calls) == sorted(selection_bundle["selected_ids"])
    records = {record["problem_id"]: record for record in _records(result.results_path)}
    passing = records[selection_bundle["selected_ids"][0]]
    failing = records[fail_task]
    assert passing["infrastructure_status"] == "ok"
    assert passing["passed_plus"] is True
    assert failing["plus_status"] == "fail"
    assert failing["plus_fail_test_count"] == 1
    assert failing["error_type"] == "wrong_answer_or_candidate_exception"
    assert result.summary["actual_execution_count"] == SAMPLE_COUNT
    assert result.summary["base_plus_pass_count"] == 1
    assert result.summary["wrong_answer_or_candidate_exception_count"] == 1
    assert result.summary["infrastructure_error_count"] == 0

    for artifact in (
        result.results_path,
        result.summary_path,
        result.manifest_path,
        result.execution_log_path,
    ):
        assert FAILURE_CANARY.encode() not in artifact.read_bytes()


def test_preflight_failure_is_infrastructure_not_candidate_failure(selection_bundle):
    executor = _StubDockerExecutor(ready=False)
    result = run_mbpp_experiment(
        selection_bundle["manifest_path"],
        selection_bundle["candidates_path"],
        selection_bundle["output_dir"],
        executor=executor,
        run_id="phase2_mbpp_preflight_fixture",
    )
    assert executor.task_calls == []
    records = _records(result.results_path)
    assert all(record["infrastructure_status"] == "error" for record in records)
    assert all(record["error_type"] == "docker_unavailable" for record in records)
    assert all(record["base_status"] is None for record in records)
    assert result.summary["actual_execution_count"] == 0
    assert result.summary["infrastructure_error_count"] == SAMPLE_COUNT
    assert result.summary["base_pass_rate"] is None


def test_resume_with_identical_inputs_reuses_results(selection_bundle):
    executor = _StubDockerExecutor()
    run_id = "phase2_mbpp_resume_fixture"
    first = run_mbpp_experiment(
        selection_bundle["manifest_path"],
        selection_bundle["candidates_path"],
        selection_bundle["output_dir"],
        executor=executor,
        run_id=run_id,
    )
    second_executor = _StubDockerExecutor()
    second = run_mbpp_experiment(
        selection_bundle["manifest_path"],
        selection_bundle["candidates_path"],
        selection_bundle["output_dir"],
        executor=second_executor,
        run_id=run_id,
        resume=True,
    )
    assert second_executor.task_calls == []
    assert second.summary["actual_execution_count"] == SAMPLE_COUNT
    assert second.manifest["output"]["samples_sha256"] == first.manifest["output"]["samples_sha256"]


def test_resume_rejects_changed_candidates(selection_bundle):
    run_id = "phase2_mbpp_resume_mismatch"
    run_mbpp_experiment(
        selection_bundle["manifest_path"],
        selection_bundle["candidates_path"],
        selection_bundle["output_dir"],
        executor=MockMbppEvalPlusExecutor(),
        run_id=run_id,
    )
    rows = _records(selection_bundle["candidates_path"])
    rows[0]["code"] = "def changed(x):\n    return x + 1\n"
    changed_path = selection_bundle["tmp_path"] / "candidates_changed.jsonl"
    _write_candidates(changed_path, rows)
    with pytest.raises(MbppExperimentError):
        run_mbpp_experiment(
            selection_bundle["manifest_path"],
            changed_path,
            selection_bundle["output_dir"],
            executor=MockMbppEvalPlusExecutor(),
            run_id=run_id,
            resume=True,
        )


def test_resume_rejects_changed_limits(selection_bundle):
    run_id = "phase2_mbpp_resume_limits"
    run_mbpp_experiment(
        selection_bundle["manifest_path"],
        selection_bundle["candidates_path"],
        selection_bundle["output_dir"],
        executor=MockMbppEvalPlusExecutor(),
        run_id=run_id,
    )
    with pytest.raises(MbppExperimentError):
        run_mbpp_experiment(
            selection_bundle["manifest_path"],
            selection_bundle["candidates_path"],
            selection_bundle["output_dir"],
            executor=MockMbppEvalPlusExecutor(),
            run_id=run_id,
            resume=True,
            per_task_timeout_seconds=60.0,
        )


def test_invalid_run_parameters_rejected_before_any_write(selection_bundle):
    with pytest.raises(MbppExperimentError, match="max_workers"):
        run_mbpp_experiment(
            selection_bundle["manifest_path"],
            selection_bundle["candidates_path"],
            selection_bundle["output_dir"],
            executor=MockMbppEvalPlusExecutor(),
            max_workers=0,
        )
    with pytest.raises(MbppExperimentError, match="batch timeout"):
        run_mbpp_experiment(
            selection_bundle["manifest_path"],
            selection_bundle["candidates_path"],
            selection_bundle["output_dir"],
            executor=MockMbppEvalPlusExecutor(),
            per_task_timeout_seconds=100.0,
            batch_timeout_seconds=10.0,
        )
    assert not selection_bundle["output_dir"].exists()
