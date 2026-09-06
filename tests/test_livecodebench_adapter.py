"""Offline LiveCodeBench adapter tests; every record is a synthetic fixture."""

from __future__ import annotations

import hashlib
import json
import random
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tracejudge_hy3.benchmark import (
    BenchmarkDifficulty,
    DatasetAdapter,
    EvaluationMode,
    ExecutionResultAdapter,
    ExecutionStatus,
    TaskInterface,
    TestVisibility,
    canonical_sha256,
    ordered_ids_sha256,
)
from tracejudge_hy3.benchmark.livecodebench import (
    LCB60_PROTOCOL,
    LCB_CHECKER_COMMIT,
    LCB_DATASET_ID,
    LCB_EXECUTOR_ID,
    LCB_HF_REVISION,
    LCB_LOADER_SCRIPT_NAME,
    LCB_PIN,
    LCB_RELEASE_TAG,
    LCBFilePin,
    LiveCodeBenchAdapterError,
    LiveCodeBenchBenchmarkAdapter,
    LiveCodeBenchPin,
    build_experiment_manifest,
    build_public_prompt,
    build_selection_lock,
    candidate_from_code,
    load_source_records,
    parse_source_record,
    run_smoke_check,
    select_lcb60_task_ids,
    verify_selection_lock,
)

PRIVATE_CANARY = "PRIVATE_HIDDEN_TEST_CANARY_NEVER_DISCLOSE"
CHECKER_COMMIT = "f" * 40
HF_REVISION = "a" * 40


def _raw_record(
    question_id: str,
    *,
    difficulty: str = "easy",
    contest_date: str = "2024-08-15T00:00:00",
    platform: str = "atcoder",
    call_based: bool = False,
) -> dict[str, Any]:
    return {
        "question_title": f"Problem {question_id}",
        "question_content": f"Public statement for {question_id}.\n\n### Sample Input\n1\n",
        "platform": platform,
        "question_id": question_id,
        "contest_id": f"contest-{question_id}",
        "contest_date": contest_date,
        "starter_code": "def solve():\n    pass\n" if call_based else "",
        "difficulty": difficulty,
        "public_test_cases": json.dumps(
            [{"input": "1\n", "output": "1\n", "testtype": "functional" if call_based else "stdin"}]
        ),
        "private_test_cases": f"{PRIVATE_CANARY}-{question_id}",
        "metadata": json.dumps({"func_name": "solve"} if call_based else {}),
    }


def _fixture_pin(files: dict[str, bytes], checker_files: dict[str, bytes]) -> LiveCodeBenchPin:
    return LiveCodeBenchPin(
        hf_revision=HF_REVISION,
        release_tag="release_v6",
        license="fixture-license",
        source_uri="https://example.test/livecodebench",
        source_files=tuple(
            LCBFilePin(name, hashlib.sha256(payload).hexdigest(), len(payload))
            for name, payload in files.items()
            if name.endswith(".jsonl")
        ),
        loader_script_sha256=hashlib.sha256(files[LCB_LOADER_SCRIPT_NAME]).hexdigest(),
        checker_repo_uri="https://example.test/checker",
        checker_commit=CHECKER_COMMIT,
        checker_files=tuple(
            LCBFilePin(name, hashlib.sha256(payload).hexdigest())
            for name, payload in checker_files.items()
        ),
    )


def _write_dataset_dir(
    tmp_path: Path, records: list[dict[str, Any]]
) -> tuple[Path, LiveCodeBenchPin]:
    payload = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records).encode(
        "utf-8"
    )
    files = {"test.jsonl": payload, LCB_LOADER_SCRIPT_NAME: b"# fixture loader\n"}
    checker_files = {
        "lcb_runner/evaluation/testing_util.py": b"# fixture checker\n",
    }
    dataset_dir = tmp_path / "dataset"
    dataset_dir.mkdir()
    for name, content in files.items():
        (dataset_dir / name).write_bytes(content)
    return dataset_dir, _fixture_pin(files, checker_files)


def _write_checker_dir(tmp_path: Path) -> tuple[Path, dict[str, bytes]]:
    checker_files = {"lcb_runner/evaluation/testing_util.py": b"# fixture checker\n"}
    checker_dir = tmp_path / "checker"
    for name, content in checker_files.items():
        path = checker_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return checker_dir, checker_files


def _cohort_records(per_stratum: int = 25) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for difficulty in ("easy", "medium", "hard"):
        for index in range(per_stratum):
            records.append(_raw_record(f"{difficulty}-{index:03d}", difficulty=difficulty))
    # Out-of-window and call-based records are present but must be excluded.
    records.append(_raw_record("old-001", contest_date="2024-05-31T00:00:00"))
    records.append(_raw_record("future-001", contest_date="2025-05-01T00:00:00"))
    records.append(_raw_record("call-001", call_based=True))
    return records


def _parsed_cohort(per_stratum: int = 25):
    return tuple(
        parse_source_record(record, source_file="test.jsonl")
        for record in _cohort_records(per_stratum)
    )


def _adapter() -> LiveCodeBenchBenchmarkAdapter:
    return LiveCodeBenchBenchmarkAdapter()


def _projected_task():
    record = parse_source_record(_raw_record("task-001"), source_file="test.jsonl")
    return _adapter().project_record(record)


def _candidate(task):
    return candidate_from_code(task=task, candidate_id="run-1", code="print(1)\n")


def _raw_result(task, candidate, **overrides) -> dict[str, Any]:
    result: dict[str, Any] = {
        "question_id": task.identity.task_id,
        "candidate_sha256": candidate.code_sha256,
        "infrastructure_status": "ok",
        "error_type": None,
        "compile_ok": True,
        "official_test_results": [True, True, True],
        "official_error_code": None,
        "public_test_count": 1,
        "duration_seconds": 1.25,
        "checker_commit": LCB_CHECKER_COMMIT,
        "executor_id": LCB_EXECUTOR_ID,
    }
    result.update(overrides)
    return result


def test_official_pin_matches_verified_external_identity() -> None:
    assert LCB_PIN.hf_revision == LCB_HF_REVISION
    assert LCB_PIN.release_tag == LCB_RELEASE_TAG
    assert LCB_PIN.checker_commit == LCB_CHECKER_COMMIT
    assert len(LCB_PIN.source_files) == 6
    assert LCB_PIN.revision == f"{LCB_RELEASE_TAG}@{LCB_HF_REVISION}"
    descriptor = _adapter().descriptor
    assert descriptor.dataset_id == LCB_DATASET_ID
    assert descriptor.revision == LCB_PIN.revision
    assert descriptor.source_manifest_sha256 == LCB_PIN.source_manifest_sha256()
    assert descriptor.task_interfaces == (TaskInterface.STANDARD_IO,)
    assert descriptor.evaluation_modes == (EvaluationMode.GENERATION,)


def test_minimal_fixture_projects_into_frozen_benchmark_task() -> None:
    adapter = _adapter()
    assert isinstance(adapter, DatasetAdapter)
    assert isinstance(adapter, ExecutionResultAdapter)
    record = parse_source_record(_raw_record("abc123_a"), source_file="test.jsonl")
    task = adapter.project_record(record)
    assert task.identity.interface is TaskInterface.STANDARD_IO
    assert task.identity.evaluation_mode is EvaluationMode.GENERATION
    assert task.identity.task_id == "abc123_a"
    assert task.entry_point is None
    assert task.difficulty is BenchmarkDifficulty.EASY
    assert task.tags == ("atcoder", "livecodebench", "standard_io")
    assert "Public statement for abc123_a." in task.prompt
    assert "### Format: Read the inputs from stdin" in task.prompt
    assert "### Sample Input 1" in task.prompt and "### Sample Output 1" in task.prompt
    serialized = task.model_dump_json()
    assert PRIVATE_CANARY not in serialized
    assert "private_test_cases" not in serialized


def test_parse_record_rejects_hidden_or_inconsistent_material() -> None:
    with pytest.raises(LiveCodeBenchAdapterError, match="schema"):
        parse_source_record(
            {k: v for k, v in _raw_record("x-1").items() if k != "metadata"},
            source_file="test.jsonl",
        )
    with pytest.raises(LiveCodeBenchAdapterError, match="difficulty"):
        parse_source_record(_raw_record("x-2", difficulty="expert"), source_file="test.jsonl")
    with pytest.raises(LiveCodeBenchAdapterError, match="starter code"):
        record = _raw_record("x-3")
        record["starter_code"] = "import sys\n"
        parse_source_record(record, source_file="test.jsonl")
    with pytest.raises(LiveCodeBenchAdapterError, match="stdin"):
        record = _raw_record("x-4")
        record["public_test_cases"] = json.dumps(
            [{"input": "1", "output": "1", "testtype": "functional"}]
        )
        parse_source_record(record, source_file="test.jsonl")
    call_based = parse_source_record(_raw_record("x-5", call_based=True), source_file="test.jsonl")
    with pytest.raises(LiveCodeBenchAdapterError, match="standard-I/O"):
        _adapter().project_record(call_based)


def test_load_tasks_verifies_pin_and_rejects_tampering(tmp_path: Path) -> None:
    records = [_raw_record("task-001")]
    dataset_dir, pin = _write_dataset_dir(tmp_path, records)
    adapter = LiveCodeBenchBenchmarkAdapter(pin=pin)
    tasks = adapter.load_tasks(dataset_dir)
    assert [item.identity.task_id for item in tasks] == ["task-001"]
    assert tasks[0].source_record_sha256 == canonical_sha256(records[0])

    (dataset_dir / "test.jsonl").write_bytes((dataset_dir / "test.jsonl").read_bytes() + b" ")
    with pytest.raises(LiveCodeBenchAdapterError, match="size mismatch|hash mismatch"):
        adapter.load_tasks(dataset_dir)


def test_empty_public_samples_in_historical_row_do_not_block_cohort(tmp_path: Path) -> None:
    historical = _raw_record("abc350_c")
    historical["contest_date"] = "2024-04-20T00:00:00"
    historical["public_test_cases"] = "[]"
    source, pin = _write_dataset_dir(tmp_path, [historical, _raw_record("eligible")])
    tasks = LiveCodeBenchBenchmarkAdapter(pin=pin).load_tasks(source)
    assert [task.identity.task_id for task in tasks] == ["eligible"]
    historical["public_test_cases"] = "{}"
    with pytest.raises(LiveCodeBenchAdapterError, match="must be a list"):
        parse_source_record(historical, source_file="test2.jsonl")


def test_selective_private_retention_preserves_identity_and_selection(tmp_path: Path):
    from tracejudge_hy3.benchmark.livecodebench import load_source_records

    source, pin = _write_dataset_dir(tmp_path, [_raw_record("keep"), _raw_record("drop")])
    full = load_source_records(source, pin=pin)
    small = load_source_records(source, pin=pin, retain_private_task_ids=frozenset({"keep"}))
    assert [r.source_record_sha256 for r in full] == [r.source_record_sha256 for r in small]
    assert small[0].private_test_cases_raw == full[0].private_test_cases_raw
    assert small[1].private_test_cases_raw == ""


def test_load_tasks_rejects_duplicate_question_ids(tmp_path: Path) -> None:
    dataset_dir, pin = _write_dataset_dir(tmp_path, [_raw_record("dup-1"), _raw_record("dup-1")])
    adapter = LiveCodeBenchBenchmarkAdapter(pin=pin)
    with pytest.raises(LiveCodeBenchAdapterError, match="duplicate"):
        adapter.load_tasks(dataset_dir)


def test_official_pin_never_skips_hash_verification(tmp_path: Path) -> None:
    dataset_dir, _pin = _write_dataset_dir(tmp_path, [_raw_record("task-001")])
    with pytest.raises(LiveCodeBenchAdapterError, match="requires source hash verification"):
        load_source_records(dataset_dir, verify_hashes=False)


def test_selection_is_deterministic_stratified_and_order_independent() -> None:
    records = _parsed_cohort()
    first = select_lcb60_task_ids(records)
    second = select_lcb60_task_ids(records)
    assert first == second
    assert len(first) == 60 and len(set(first)) == 60
    assert ordered_ids_sha256(first) == ordered_ids_sha256(second)

    shuffled = list(records)
    random.Random(1234).shuffle(shuffled)
    assert select_lcb60_task_ids(tuple(shuffled)) == first

    difficulty_by_id = {record.question_id: record.difficulty for record in records}
    counts = {difficulty: 0 for difficulty in ("easy", "medium", "hard")}
    for task_id in first:
        counts[difficulty_by_id[task_id].value] += 1
    assert counts == {"easy": 20, "medium": 20, "hard": 20}
    assert "old-001" not in first and "future-001" not in first and "call-001" not in first
    # Strata are emitted in the fixed easy, medium, hard order.
    assert [difficulty_by_id[task_id].value for task_id in first[:20]] == ["easy"] * 20
    assert [difficulty_by_id[task_id].value for task_id in first[20:40]] == ["medium"] * 20
    assert [difficulty_by_id[task_id].value for task_id in first[40:]] == ["hard"] * 20


def test_selection_fails_closed_on_thin_stratum_or_duplicates() -> None:
    thin = tuple(
        parse_source_record(record, source_file="test.jsonl")
        for record in _cohort_records(per_stratum=25)[:-22]
        # Removing 22 records from the end leaves the hard stratum short.
    )
    with pytest.raises(LiveCodeBenchAdapterError, match="refusing to fabricate"):
        select_lcb60_task_ids(thin)
    with pytest.raises(LiveCodeBenchAdapterError, match="duplicate"):
        select_lcb60_task_ids(_parsed_cohort() + _parsed_cohort()[:1])


def test_selection_lock_binds_protocol_pin_and_cohort() -> None:
    records = _parsed_cohort()
    lock = build_selection_lock(records)
    assert lock["protocol"]["seed"] == LCB60_PROTOCOL.seed
    assert lock["protocol"]["window_start"] == "2024-06-01"
    assert lock["protocol"]["window_end"] == "2025-04-30"
    assert verify_selection_lock(lock, records) == tuple(lock["selected_task_ids"])

    tampered_ids = {**lock, "selected_task_ids": list(reversed(lock["selected_task_ids"]))}
    with pytest.raises(LiveCodeBenchAdapterError, match="do not reproduce"):
        verify_selection_lock(tampered_ids, records)
    tampered_hash = {**lock, "selected_task_ids_sha256": "0" * 64}
    with pytest.raises(LiveCodeBenchAdapterError, match="do not reproduce|hash"):
        verify_selection_lock(tampered_hash, records)
    tampered_protocol = {
        **lock,
        "protocol": {**lock["protocol"], "seed": 1},
    }
    with pytest.raises(LiveCodeBenchAdapterError, match="protocol"):
        verify_selection_lock(tampered_protocol, records)
    with pytest.raises(LiveCodeBenchAdapterError, match="fields"):
        verify_selection_lock({**lock, "unexpected": True}, records)


@pytest.mark.parametrize(
    ("overrides", "expected_status", "expected_kind", "expected_groups"),
    [
        ({}, ExecutionStatus.PASSED, None, {"public": 0, "private": 0}),
        (
            {"official_test_results": [True, True, -2]},
            ExecutionStatus.FAILED,
            "wrong_answer",
            {"public": 0, "private": 1},
        ),
        (
            {"official_test_results": [True, -3]},
            ExecutionStatus.TIMEOUT,
            "time_limit_exceeded",
            {"public": 0, "private": 1},
        ),
        (
            {"official_test_results": [True, True, -4]},
            ExecutionStatus.RUNTIME_ERROR,
            "runtime_error",
            {"public": 0, "private": 1},
        ),
        (
            {"compile_ok": False, "official_test_results": [-4]},
            ExecutionStatus.COMPILE_ERROR,
            "compile_error",
            {},
        ),
        (
            {"official_error_code": -5, "official_test_results": [-2]},
            ExecutionStatus.INFRASTRUCTURE_ERROR,
            "official_test_runner_error",
            {},
        ),
        (
            {"infrastructure_status": "error", "error_type": "container_timeout"},
            ExecutionStatus.INFRASTRUCTURE_ERROR,
            "container_timeout",
            {},
        ),
        (
            {"infrastructure_status": "not_run", "error_type": "batch_deadline_not_started"},
            ExecutionStatus.NOT_RUN,
            "batch_deadline_not_started",
            {},
        ),
    ],
)
def test_normalize_execution_result_status_mapping(
    overrides: dict[str, Any],
    expected_status: ExecutionStatus,
    expected_kind: str | None,
    expected_groups: dict[str, int],
) -> None:
    task = _projected_task()
    candidate = _candidate(task)
    result = _raw_result(task, candidate, **overrides)
    normalized = _adapter().normalize_execution_result(
        task=task, candidate=candidate, result=result
    )
    assert normalized.status is expected_status
    assert normalized.failure_kind == expected_kind
    assert {group.group_id: group.failed_test_count for group in normalized.groups} == (
        expected_groups
    )
    assert normalized.candidate_sha256 == candidate.code_sha256
    assert normalized.source_result_sha256 == canonical_sha256(result)
    serialized = normalized.model_dump_json()
    assert PRIVATE_CANARY not in serialized


def test_normalize_result_group_visibility_and_counts() -> None:
    task = _projected_task()
    candidate = _candidate(task)
    result = _raw_result(
        task,
        candidate,
        official_test_results=[True, False],
        public_test_count=1,
    )
    normalized = _adapter().normalize_execution_result(
        task=task, candidate=candidate, result=result
    )
    assert normalized.status is ExecutionStatus.FAILED
    groups = {group.group_id: group for group in normalized.groups}
    assert groups["public"].visibility is TestVisibility.PUBLIC
    assert groups["public"].status is ExecutionStatus.PASSED
    assert groups["public"].total_test_count == 1
    assert groups["private"].visibility is TestVisibility.HIDDEN
    assert groups["private"].status is ExecutionStatus.FAILED
    assert groups["private"].failed_test_count == 1
    assert groups["private"].total_test_count == 1


def test_first_public_failure_prefix_is_candidate_failure_not_infrastructure():
    task = _projected_task()
    candidate = _candidate(task)
    result = _raw_result(task, candidate, official_test_results=[-2], public_test_count=3)
    normalized = _adapter().normalize_execution_result(
        task=task, candidate=candidate, result=result
    )
    assert normalized.status is ExecutionStatus.FAILED
    assert len(normalized.groups) == 1
    assert normalized.groups[0].group_id == "public"
    assert normalized.groups[0].total_test_count == 1


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"question_id": "other-task"}, "task identity"),
        ({"candidate_sha256": "0" * 64}, "candidate hash"),
        ({"checker_commit": "e" * 40}, "pinned checker"),
        ({"executor_id": "other_executor"}, "executor identity"),
        ({"infrastructure_status": "error", "error_type": "weird_error"}, "allowlisted"),
        ({"official_test_results": [True, -7]}, "official_test_results"),
        ({"official_test_results": []}, "official_test_results"),
        ({"public_test_count": 99}, "public_test_count"),
        ({"compile_ok": None}, "compile_ok"),
        ({"duration_seconds": -1.0}, "duration_seconds"),
    ],
)
def test_normalize_result_fails_closed(overrides: dict[str, Any], match: str) -> None:
    task = _projected_task()
    candidate = _candidate(task)
    with pytest.raises(LiveCodeBenchAdapterError, match=match):
        _adapter().normalize_execution_result(
            task=task, candidate=candidate, result=_raw_result(task, candidate, **overrides)
        )


def test_normalize_result_rejects_incomplete_or_unknown_schema() -> None:
    task = _projected_task()
    candidate = _candidate(task)
    incomplete = _raw_result(task, candidate)
    del incomplete["compile_ok"]
    with pytest.raises(LiveCodeBenchAdapterError, match="incomplete or unknown"):
        _adapter().normalize_execution_result(task=task, candidate=candidate, result=incomplete)
    leaked = _raw_result(task, candidate, hidden_inputs=f"{PRIVATE_CANARY}-leak")
    with pytest.raises(LiveCodeBenchAdapterError, match="incomplete or unknown"):
        _adapter().normalize_execution_result(task=task, candidate=candidate, result=leaked)


def test_experiment_manifest_binds_locked_selection_and_identity() -> None:
    records = _parsed_cohort()
    adapter = _adapter()
    tasks = adapter.project_records(records)
    lock = build_selection_lock(records)
    selected_tasks = adapter.select_tasks(tasks, lock["selected_task_ids"])
    assert [task.identity.task_id for task in selected_tasks] == lock["selected_task_ids"]

    manifest = build_experiment_manifest(
        adapter=adapter,
        tasks=selected_tasks,
        lock=lock,
        records=records,
        experiment_id="lcb60-external-validation",
        generation_prompt_sha256="1" * 64,
        git_commit="b" * 40,
        git_dirty=False,
        provider="hy3",
        model="hunyuan-fixture",
    )
    assert manifest.selected_task_ids == tuple(lock["selected_task_ids"])
    assert manifest.selected_task_ids_sha256 == lock["selected_task_ids_sha256"]
    assert manifest.selection_algorithm == LCB60_PROTOCOL.algorithm
    assert manifest.dataset.revision == LCB_PIN.revision

    with pytest.raises(LiveCodeBenchAdapterError, match="do not cover"):
        build_experiment_manifest(
            adapter=adapter,
            tasks=selected_tasks[:-1],
            lock=lock,
            records=records,
            experiment_id="lcb60-external-validation",
            generation_prompt_sha256="1" * 64,
            git_commit="b" * 40,
            git_dirty=False,
        )


def test_smoke_check_verifies_data_checker_and_container(tmp_path: Path) -> None:
    records = [_raw_record("task-001")]
    dataset_dir, _pin = _write_dataset_dir(tmp_path, records)
    checker_dir, checker_files = _write_checker_dir(tmp_path)
    files = {
        "test.jsonl": (dataset_dir / "test.jsonl").read_bytes(),
        LCB_LOADER_SCRIPT_NAME: (dataset_dir / LCB_LOADER_SCRIPT_NAME).read_bytes(),
    }
    pin = _fixture_pin(files, checker_files)

    def fake_runner(command, **kwargs):
        if command[:2] == ["git", "-C"]:
            return subprocess.CompletedProcess(command, 0, stdout=CHECKER_COMMIT + "\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="27.0.0\n", stderr="")

    report = run_smoke_check(
        dataset_dir=dataset_dir,
        checker_dir=checker_dir,
        pin=pin,
        command_runner=fake_runner,
        which=lambda name: "/usr/bin/docker",
    )
    assert report.ready
    assert report.problems == ()

    (dataset_dir / "test.jsonl").write_bytes(b"tampered\n")
    report = run_smoke_check(
        dataset_dir=dataset_dir,
        checker_dir=checker_dir,
        pin=pin,
        command_runner=fake_runner,
        which=lambda name: "/usr/bin/docker",
    )
    assert not report.ready
    assert not report.data_identity_ok

    def wrong_head_runner(command, **kwargs):
        if command[:2] == ["git", "-C"]:
            return subprocess.CompletedProcess(command, 0, stdout="0" * 40 + "\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="27.0.0\n", stderr="")

    report = run_smoke_check(
        dataset_dir=None,
        checker_dir=checker_dir,
        pin=pin,
        command_runner=wrong_head_runner,
        which=lambda name: None,
    )
    assert not report.ready
    assert not report.checker_identity_ok
    assert not report.container_environment_ok
    assert not report.data_identity_ok


def test_candidate_helper_binds_code_without_executing() -> None:
    task = _projected_task()
    candidate = _candidate(task)
    assert candidate.origin == "generated"
    assert candidate.code_sha256 == hashlib.sha256(b"print(1)\n").hexdigest()


def test_build_public_prompt_uses_official_template_and_public_samples() -> None:
    record = parse_source_record(_raw_record("abc123_a"), source_file="test.jsonl")
    prompt = build_public_prompt(record)
    assert prompt.startswith("### Question:\n")
    assert "```python\n# YOUR CODE HERE\n```" in prompt
    assert prompt.endswith("### Answer: (use the provided format with backticks)\n\n")
    assert PRIVATE_CANARY not in prompt
