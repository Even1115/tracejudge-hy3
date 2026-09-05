from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from tracejudge_hy3.benchmark import (
    BENCHMARK_CONTRACT_VERSION,
    BenchmarkCandidate,
    BenchmarkDataset,
    BenchmarkDifficulty,
    BenchmarkExecutionResult,
    BenchmarkExperimentManifest,
    BenchmarkJudgeRecord,
    BenchmarkTask,
    BenchmarkTaskIdentity,
    CandidateOrigin,
    DatasetAdapter,
    EvaluationMode,
    ExecutionResultAdapter,
    ExecutionStatus,
    JudgeStatus,
    PublicRequirement,
    TaskInterface,
    canonical_sha256,
    ordered_ids_sha256,
    task_public_payload_sha256,
)
from tracejudge_hy3.benchmark.humanevalplus import (
    HumanEvalPlusBenchmarkAdapter,
    candidate_from_code,
)
from tracejudge_hy3.schemas.problem import ProblemSpec, RequirementItem

REVISION = "d32357cf319e50e9c8d8dab5ea876c72b0fd321b"
ZERO_SHA = "0" * 64


def _identity(
    *,
    interface: TaskInterface = TaskInterface.FUNCTION,
    mode: EvaluationMode = EvaluationMode.GENERATION,
) -> BenchmarkTaskIdentity:
    return BenchmarkTaskIdentity(
        dataset_id="example/benchmark",
        dataset_revision="release-v1",
        task_id="task/1",
        interface=interface,
        evaluation_mode=mode,
        language="python",
    )


def _task(*, identity: BenchmarkTaskIdentity | None = None) -> BenchmarkTask:
    identity = identity or _identity()
    requirements = (PublicRequirement(requirement_id="R1", content="Return the input."),)
    kwargs = {
        "identity": identity,
        "title": "Identity",
        "prompt": "Implement identity(x).",
        "requirements": requirements,
        "entry_point": "identity" if identity.interface is TaskInterface.FUNCTION else None,
        "difficulty": BenchmarkDifficulty.EASY,
        "tags": ("unit",),
    }
    return BenchmarkTask(
        **kwargs,
        source_record_sha256=ZERO_SHA,
        public_payload_sha256=task_public_payload_sha256(**kwargs),
    )


def _candidate(task: BenchmarkTask) -> BenchmarkCandidate:
    code = "def identity(x):\n    return x\n"
    return BenchmarkCandidate(
        task=task.identity,
        candidate_id="candidate-1",
        origin=CandidateOrigin.GENERATED,
        code=code,
        code_sha256=hashlib.sha256(code.encode()).hexdigest(),
    )


def _adapter() -> HumanEvalPlusBenchmarkAdapter:
    return HumanEvalPlusBenchmarkAdapter(
        revision=REVISION,
        license="apache-2.0",
        source_uri="https://huggingface.co/datasets/evalplus/humanevalplus",
        source_manifest_sha256=ZERO_SHA,
    )


def _humaneval_problem() -> ProblemSpec:
    return ProblemSpec(
        problem_id="HumanEval/8",
        title="sum_product (HumanEval/8)",
        requirement="def sum_product(numbers):\n    pass\n",
        function_signature="def sum_product(numbers):",
        requirements=(RequirementItem(requirement_id="R1", content="Return sum and product."),),
        reference_code="# EVALPLUS_REFERENCE_CODE_WITHHELD_FROM_PHASE1\n",
        difficulty="unknown",
        source="evalplus_humanevalplus",
        tags=("public_benchmark", "humanevalplus", "phase1_public_projection"),
    )


def test_contract_version_and_model_fields_are_frozen() -> None:
    assert BENCHMARK_CONTRACT_VERSION == 1
    assert tuple(BenchmarkDataset.model_fields) == (
        "schema_version",
        "dataset_id",
        "revision",
        "license",
        "source_uri",
        "source_manifest_sha256",
        "adapter_id",
        "adapter_version",
        "task_interfaces",
        "evaluation_modes",
        "languages",
        "capabilities",
    )
    assert tuple(BenchmarkTask.model_fields) == (
        "schema_version",
        "identity",
        "title",
        "prompt",
        "requirements",
        "entry_point",
        "difficulty",
        "tags",
        "source_record_sha256",
        "public_payload_sha256",
    )
    assert tuple(BenchmarkExecutionResult.model_fields) == (
        "schema_version",
        "task",
        "candidate_sha256",
        "executor_id",
        "status",
        "groups",
        "failure_kind",
        "duration_seconds",
        "source_result_sha256",
    )
    assert tuple(BenchmarkJudgeRecord.model_fields) == (
        "schema_version",
        "task",
        "candidate_sha256",
        "method_id",
        "status",
        "functional_correct",
        "process_correct",
        "first_faulty_layer",
        "first_faulty_step",
        "normalized_error_type",
        "source_error_type",
        "certificate_verdict",
        "confidence",
        "source_judgment_sha256",
    )
    assert tuple(BenchmarkExperimentManifest.model_fields) == (
        "schema_version",
        "contract_version",
        "experiment_id",
        "dataset",
        "selected_task_ids",
        "selected_task_ids_sha256",
        "selection_algorithm",
        "generation_prompt_sha256",
        "judge_prompt_sha256",
        "git_commit",
        "git_dirty",
        "provider",
        "model",
        "metrics_scope",
        "limitations",
    )
    with pytest.raises(ValidationError):
        BenchmarkDataset(
            dataset_id="example/data",
            revision="v1",
            license="MIT",
            source_uri="https://example.test/data",
            source_manifest_sha256=ZERO_SHA,
            adapter_id="example_adapter",
            adapter_version=1,
            task_interfaces=(TaskInterface.FUNCTION,),
            evaluation_modes=(EvaluationMode.GENERATION,),
            languages=("python",),
            unexpected=True,
        )


def test_task_hash_and_interface_invariants_are_enforced() -> None:
    task = _task()
    with pytest.raises(ValidationError, match="frozen"):
        task.title = "changed"
    with pytest.raises(ValidationError, match="public_payload_sha256"):
        BenchmarkTask(**{**task.model_dump(), "title": "changed"})

    standard_io = _identity(interface=TaskInterface.STANDARD_IO)
    with pytest.raises(ValidationError, match="only function tasks"):
        payload = _task(identity=standard_io).model_dump()
        BenchmarkTask(**{**payload, "entry_point": "main"})


def test_candidate_and_manifest_hashes_are_bound() -> None:
    task = _task()
    candidate = _candidate(task)
    with pytest.raises(ValidationError, match="code_sha256"):
        BenchmarkCandidate(**{**candidate.model_dump(), "code": "print('changed')"})

    task_ids = ("task/1", "task/2")
    dataset = BenchmarkDataset(
        dataset_id="example/benchmark",
        revision="release-v1",
        license="MIT",
        source_uri="https://example.test/benchmark",
        source_manifest_sha256=ZERO_SHA,
        adapter_id="example_adapter",
        adapter_version=1,
        task_interfaces=(TaskInterface.FUNCTION,),
        evaluation_modes=(EvaluationMode.GENERATION,),
        languages=("python",),
    )
    manifest = BenchmarkExperimentManifest(
        experiment_id="cross-benchmark-v1",
        dataset=dataset,
        selected_task_ids=task_ids,
        selected_task_ids_sha256=ordered_ids_sha256(task_ids),
        selection_algorithm="all-in-source-order-v1",
        generation_prompt_sha256=ZERO_SHA,
        git_commit="1" * 40,
        git_dirty=False,
        provider="hy3",
        model="hunyuan-turbos-latest",
        metrics_scope=("generation", "official_execution"),
    )
    assert manifest.contract_version == 1
    with pytest.raises(ValidationError, match="selected_task_ids_sha256"):
        BenchmarkExperimentManifest(
            **{**manifest.model_dump(), "selected_task_ids_sha256": "f" * 64}
        )
    with pytest.raises(ValueError, match="unique"):
        ordered_ids_sha256(("task/1", "task/1"))


def test_humaneval_bridge_implements_frozen_protocols_without_private_material() -> None:
    adapter = _adapter()
    assert isinstance(adapter, DatasetAdapter)
    assert isinstance(adapter, ExecutionResultAdapter)
    task = adapter.normalize_problem(_humaneval_problem())
    assert task.identity.dataset_id == "evalplus/humanevalplus"
    assert task.identity.dataset_revision == REVISION
    assert task.entry_point == "sum_product"
    assert task.difficulty is BenchmarkDifficulty.UNKNOWN
    serialized = task.model_dump_json()
    assert "canonical_solution" not in serialized
    assert "EVALPLUS_REFERENCE_CODE" not in serialized


def test_humaneval_bridge_loads_an_existing_public_projection(tmp_path) -> None:
    source = tmp_path / "problems.jsonl"
    source.write_text(_humaneval_problem().model_dump_json() + "\n", encoding="utf-8")
    tasks = _adapter().load_tasks(source)
    assert len(tasks) == 1
    assert tasks[0].identity.task_id == "HumanEval/8"


@pytest.mark.parametrize(
    ("base_status", "plus_status", "base_fail", "plus_fail", "expected"),
    [
        ("pass", "pass", 0, 0, ExecutionStatus.PASSED),
        ("pass", "fail", 0, 2, ExecutionStatus.FAILED),
        ("timeout", "timeout", 0, 0, ExecutionStatus.TIMEOUT),
    ],
)
def test_humaneval_bridge_normalizes_safe_evalplus_results(
    base_status: str,
    plus_status: str,
    base_fail: int,
    plus_fail: int,
    expected: ExecutionStatus,
) -> None:
    adapter = _adapter()
    task = adapter.normalize_problem(_humaneval_problem())
    candidate = candidate_from_code(
        task=task,
        candidate_id="run-1",
        code="def sum_product(numbers):\n    return sum(numbers), 0\n",
    )
    result = {
        "problem_id": "HumanEval/8",
        "base_status": base_status,
        "plus_status": plus_status,
        "base_fail_test_count": base_fail,
        "plus_fail_test_count": plus_fail,
        "passed_base": base_status == "pass",
        "passed_plus": base_status == plus_status == "pass",
        "error_type": None if expected is ExecutionStatus.PASSED else "official_failure",
        "infrastructure_status": "ok",
        "solution_sha256": candidate.code_sha256,
        "official_override_hash": "1" * 32,
        "duration_seconds": 0.5,
    }
    normalized = adapter.normalize_execution_result(
        task=task,
        candidate=candidate,
        result=result,
    )
    assert normalized.status is expected
    assert normalized.candidate_sha256 == candidate.code_sha256
    assert normalized.source_result_sha256 == canonical_sha256(result)


def test_humaneval_bridge_rejects_cross_task_or_cross_candidate_results() -> None:
    adapter = _adapter()
    task = adapter.normalize_problem(_humaneval_problem())
    candidate = candidate_from_code(
        task=task,
        candidate_id="run-1",
        code="def sum_product(numbers):\n    return sum(numbers), 0\n",
    )
    base = {
        "problem_id": "HumanEval/8",
        "base_status": "pass",
        "plus_status": "pass",
        "base_fail_test_count": 0,
        "plus_fail_test_count": 0,
        "passed_base": True,
        "passed_plus": True,
        "error_type": None,
        "infrastructure_status": "ok",
        "solution_sha256": candidate.code_sha256,
        "official_override_hash": "1" * 32,
        "duration_seconds": 0.5,
    }
    with pytest.raises(ValueError, match="task identity"):
        adapter.normalize_execution_result(
            task=task,
            candidate=candidate,
            result={**base, "problem_id": "HumanEval/9"},
        )
    with pytest.raises(ValueError, match="solution hash"):
        adapter.normalize_execution_result(
            task=task,
            candidate=candidate,
            result={**base, "solution_sha256": "f" * 64},
        )


@pytest.mark.parametrize(
    ("infrastructure_status", "error_type", "expected"),
    [
        ("error", "container_timeout", ExecutionStatus.INFRASTRUCTURE_ERROR),
        ("mocked", "mock_not_executed", ExecutionStatus.NOT_RUN),
    ],
)
def test_humaneval_bridge_keeps_non_execution_separate_from_candidate_failure(
    infrastructure_status: str,
    error_type: str,
    expected: ExecutionStatus,
) -> None:
    adapter = _adapter()
    task = adapter.normalize_problem(_humaneval_problem())
    candidate = candidate_from_code(
        task=task,
        candidate_id="run-1",
        code="def sum_product(numbers):\n    return sum(numbers), 0\n",
    )
    normalized = adapter.normalize_execution_result(
        task=task,
        candidate=candidate,
        result={
            "problem_id": "HumanEval/8",
            "base_status": None,
            "plus_status": None,
            "base_fail_test_count": 0,
            "plus_fail_test_count": 0,
            "passed_base": False,
            "passed_plus": False,
            "error_type": error_type,
            "infrastructure_status": infrastructure_status,
            "solution_sha256": candidate.code_sha256,
            "official_override_hash": None,
            "duration_seconds": 0.0,
        },
    )
    assert normalized.status is expected
    assert normalized.groups == ()


def test_judge_record_distinguishes_valid_and_transport_outcomes() -> None:
    task = _task()
    candidate = _candidate(task)
    valid = BenchmarkJudgeRecord(
        task=task.identity,
        candidate_sha256=candidate.code_sha256,
        method_id="full_tracejudge",
        status=JudgeStatus.VALID_JUDGMENT,
        functional_correct=True,
        process_correct=True,
        certificate_verdict="cleared",
        confidence=0.9,
        source_judgment_sha256=ZERO_SHA,
    )
    assert valid.process_correct is True
    with pytest.raises(ValidationError, match="non-judgment outcomes"):
        BenchmarkJudgeRecord(
            task=task.identity,
            candidate_sha256=candidate.code_sha256,
            method_id="full_tracejudge",
            status=JudgeStatus.PROVIDER_ERROR,
            functional_correct=False,
            source_judgment_sha256=ZERO_SHA,
        )
