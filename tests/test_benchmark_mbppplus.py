from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from mbppplus_fixtures import REVISION, expected_task_ids, write_snapshot

from tracejudge_hy3.benchmark.contracts import (
    DatasetAdapter,
    EvaluationMode,
    ExecutionResultAdapter,
    ExecutionStatus,
    TaskInterface,
)
from tracejudge_hy3.benchmark.mbppplus import MbppPlusBenchmarkAdapter, candidate_from_code
from tracejudge_hy3.dataset.mbppplus import convert_mbppplus, sample_mbppplus
from tracejudge_hy3.exceptions import DatasetError

SOURCE_MANIFEST_SHA256 = "b" * 64
SEED = 20260905


def _adapter() -> MbppPlusBenchmarkAdapter:
    return MbppPlusBenchmarkAdapter(
        revision=REVISION,
        license="apache-2.0",
        source_uri="https://github.com/evalplus/mbppplus_release",
        source_manifest_sha256=SOURCE_MANIFEST_SHA256,
    )


@pytest.fixture(scope="module")
def sampled_projection(tmp_path_factory) -> Path:
    tmp_path = tmp_path_factory.mktemp("mbppplus_benchmark")
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
        count=4,
        seed=SEED,
        output_dir=tmp_path / "sample4",
    )
    return sample.dataset_path


def _safe_result(
    task_id: str,
    solution: str,
    *,
    base_status: str = "pass",
    plus_status: str = "pass",
    base_fail: int = 0,
    plus_fail: int = 0,
    error_type: str | None = None,
    infrastructure_status: str = "ok",
) -> dict[str, Any]:
    return {
        "problem_id": task_id,
        "base_status": base_status,
        "plus_status": plus_status,
        "base_fail_test_count": base_fail,
        "plus_fail_test_count": plus_fail,
        "passed_base": base_status == "pass",
        "passed_plus": base_status == plus_status == "pass",
        "error_type": error_type,
        "infrastructure_status": infrastructure_status,
        "solution_sha256": hashlib.sha256(solution.encode()).hexdigest(),
        "official_override_hash": "f" * 32,
        "duration_seconds": 1.5,
    }


def test_adapter_satisfies_frozen_protocols_and_descriptor():
    adapter = _adapter()
    assert isinstance(adapter, DatasetAdapter)
    assert isinstance(adapter, ExecutionResultAdapter)
    descriptor = adapter.descriptor
    assert descriptor.dataset_id == "evalplus/mbppplus"
    assert descriptor.revision == REVISION
    assert descriptor.task_interfaces == (TaskInterface.FUNCTION,)
    assert descriptor.evaluation_modes == (EvaluationMode.GENERATION,)
    assert descriptor.languages == ("python",)
    # MBPP+ publishes no difficulty labels, so PUBLISHED_DIFFICULTY is absent.
    assert "published_difficulty" not in {
        capability.value for capability in descriptor.capabilities
    }


def test_load_tasks_projects_only_public_fields(sampled_projection):
    adapter = _adapter()
    tasks = adapter.load_tasks(sampled_projection)

    assert len(tasks) == 4
    expected_ids = set(expected_task_ids())
    for task in tasks:
        assert task.identity.dataset_id == "evalplus/mbppplus"
        assert task.identity.dataset_revision == REVISION
        assert task.identity.interface is TaskInterface.FUNCTION
        assert task.identity.evaluation_mode is EvaluationMode.GENERATION
        assert task.identity.task_id in expected_ids
        assert task.entry_point and task.entry_point in task.prompt
        assert task.difficulty.value == "unknown"
        assert "signature_not_published" in task.tags
        serialized = task.model_dump_json()
        for canary in ("CANONICAL", "BASE_INPUT", "PLUS_INPUT", "ASSERTION", "CONTRACT"):
            assert f"PRIVATE_{canary}_CANARY" not in serialized


def test_load_tasks_rejects_wrong_source(sampled_projection, tmp_path):
    adapter = _adapter()
    lines = sampled_projection.read_text(encoding="utf-8").splitlines()
    tampered = tmp_path / "tampered.jsonl"
    tampered.write_text(
        "\n".join(
            line.replace('"evalplus_mbppplus"', '"evalplus_humanevalplus"', 1) for line in lines[:1]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(DatasetError):
        adapter.load_tasks(tampered)


def _task_and_candidate(
    adapter, sampled_projection, code: str = "def solve_task_0(v):\n    return v\n"
):
    task = adapter.load_tasks(sampled_projection)[0]
    candidate = candidate_from_code(task=task, candidate_id="cand-1", code=code)
    return task, candidate


def test_normalize_execution_result_pass(sampled_projection):
    adapter = _adapter()
    task, candidate = _task_and_candidate(adapter, sampled_projection)
    result = adapter.normalize_execution_result(
        task=task,
        candidate=candidate,
        result=_safe_result(task.identity.task_id, candidate.code),
    )
    assert result.status is ExecutionStatus.PASSED
    assert {group.group_id for group in result.groups} == {"base", "plus"}
    assert all(group.status is ExecutionStatus.PASSED for group in result.groups)
    assert result.executor_id == "evalplus_mbppplus"
    assert result.candidate_sha256 == candidate.code_sha256


def test_normalize_execution_result_fail_and_timeout(sampled_projection):
    adapter = _adapter()
    task, candidate = _task_and_candidate(adapter, sampled_projection)

    failed = adapter.normalize_execution_result(
        task=task,
        candidate=candidate,
        result=_safe_result(
            task.identity.task_id,
            candidate.code,
            base_status="pass",
            plus_status="fail",
            plus_fail=3,
            error_type="wrong_answer_or_candidate_exception",
        ),
    )
    assert failed.status is ExecutionStatus.FAILED
    plus_group = next(group for group in failed.groups if group.group_id == "plus")
    assert plus_group.failed_test_count == 3
    # Concrete failing inputs never cross the boundary: only counts survive.
    assert "fail_tests" not in failed.model_dump_json()

    timed_out = adapter.normalize_execution_result(
        task=task,
        candidate=candidate,
        result=_safe_result(
            task.identity.task_id,
            candidate.code,
            base_status="timeout",
            plus_status="timeout",
            error_type="timeout",
        ),
    )
    assert timed_out.status is ExecutionStatus.TIMEOUT
    assert any(group.status is ExecutionStatus.TIMEOUT for group in timed_out.groups)


def test_normalize_execution_result_infrastructure_error_and_not_run(sampled_projection):
    adapter = _adapter()
    task, candidate = _task_and_candidate(adapter, sampled_projection)

    infra = adapter.normalize_execution_result(
        task=task,
        candidate=candidate,
        result={
            "problem_id": task.identity.task_id,
            "infrastructure_status": "error",
            "error_type": "docker_unavailable",
            "solution_sha256": None,
            "duration_seconds": None,
        },
    )
    assert infra.status is ExecutionStatus.INFRASTRUCTURE_ERROR
    assert infra.groups == ()
    assert infra.failure_kind == "docker_unavailable"

    not_run = adapter.normalize_execution_result(
        task=task,
        candidate=candidate,
        result={
            "problem_id": task.identity.task_id,
            "infrastructure_status": "mocked",
            "error_type": "mock_not_executed",
            "solution_sha256": candidate.code_sha256,
        },
    )
    assert not_run.status is ExecutionStatus.NOT_RUN
    assert not_run.failure_kind == "mock_not_executed"


def test_normalize_execution_result_rejects_candidate_hash_mismatch(sampled_projection):
    adapter = _adapter()
    task, candidate = _task_and_candidate(adapter, sampled_projection)
    result = _safe_result(task.identity.task_id, candidate.code)
    result["solution_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="solution hash does not match"):
        adapter.normalize_execution_result(task=task, candidate=candidate, result=result)


def test_normalize_execution_result_rejects_identity_mismatch(sampled_projection):
    adapter = _adapter()
    task, candidate = _task_and_candidate(adapter, sampled_projection)
    other_task_id = next(item for item in expected_task_ids() if item != task.identity.task_id)
    result = _safe_result(other_task_id, candidate.code)
    with pytest.raises(ValueError, match="task identity does not match"):
        adapter.normalize_execution_result(task=task, candidate=candidate, result=result)

    humaneval_identity = task.identity.model_copy(update={"dataset_id": "evalplus/humanevalplus"})
    foreign_task = task.model_copy(update={"identity": humaneval_identity})
    with pytest.raises(ValueError, match="does not belong to this dataset adapter"):
        adapter.normalize_execution_result(
            task=foreign_task,
            candidate=candidate,
            result=_safe_result(task.identity.task_id, candidate.code),
        )


def test_candidate_from_code_binds_hash_without_execution(sampled_projection):
    adapter = _adapter()
    task = adapter.load_tasks(sampled_projection)[0]
    code = "def solve_task_0(values):\n    return sorted(values)\n"
    candidate = candidate_from_code(
        task=task,
        candidate_id="cand-2",
        code=code,
        solution_trace_sha256="c" * 64,
        source_run_id="run-1",
    )
    assert candidate.code_sha256 == hashlib.sha256(code.encode()).hexdigest()
    assert candidate.origin == "generated"
    assert candidate.task == task.identity
    with pytest.raises(ValueError, match="code_sha256"):
        candidate_from_code(task=task, candidate_id="cand-3", code=code).__class__(
            task=candidate.task,
            candidate_id="cand-4",
            origin="generated",
            code=code,
            code_sha256="0" * 64,
        )
