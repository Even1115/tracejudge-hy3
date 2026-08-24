from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pytest

import tracejudge_hy3.evalplus.exporter as exporter_module
from tracejudge_hy3.dataset.humanevalplus import (
    ADAPTER_NAME,
    ADAPTER_VERSION,
    DATASET_ID,
    DATASET_SOURCE,
    EXPECTED_RECORD_COUNT,
    PILOT_EXPERIMENT_LABEL,
    PILOT_LIMITATIONS,
    SELECTION_ALGORITHM,
    WITHHELD_REFERENCE_CODE,
    ordered_problem_ids_sha256,
    select_humanevalplus_problem_ids,
)
from tracejudge_hy3.evalplus.exporter import (
    PINNED_HUMANEVALPLUS_REVISION,
    EvalPlusExportError,
    load_validated_phase1_export,
    serialize_samples_jsonl,
)
from tracejudge_hy3.schemas.problem import ProblemSpec, RequirementItem

REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE_MANIFEST = REPOSITORY / "data" / "manifests" / "evalplus_humanevalplus_d32357cf.json"
EXPECTED_IDS = select_humanevalplus_problem_ids(count=10, seed=20260824)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(payload: Any) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _jsonl_bytes(rows: list[dict[str, Any]]) -> bytes:
    return b"".join(
        json.dumps(
            row,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
        for row in rows
    )


def _source_identity() -> tuple[str, str, str]:
    source_bytes = SOURCE_MANIFEST.read_bytes()
    source = json.loads(source_bytes)
    raw_files = [
        {"path": item["path"], "size": item["size"], "sha256": item["sha256"]}
        for item in source["raw_files"]
    ]
    test_hash = next(item["sha256"] for item in raw_files if item["path"] == "test.jsonl")
    return _sha256(source_bytes), _sha256(_json_bytes(raw_files)), test_hash


def _problem(problem_id: str) -> ProblemSpec:
    number = problem_id.split("/")[-1]
    entry_point = f"candidate_{number}"
    prompt = (
        "from __future__ import annotations\n\n"
        f"def {entry_point}(values: list[int]) -> int:\n"
        f'    """Return a public deterministic value for task {number}."""\n'
    )
    return ProblemSpec(
        problem_id=problem_id,
        title=f"{problem_id}: {entry_point}",
        requirement=prompt,
        function_signature=f"def {entry_point}(values: list[int]) -> int:",
        requirements=[
            RequirementItem(
                requirement_id="R1",
                content=f"Return a public deterministic value for task {number}.",
            )
        ],
        visible_test_cases=[],
        hidden_test_cases=[],
        challenge_test_cases=[],
        reference_code=WITHHELD_REFERENCE_CODE,
        difficulty="unknown",
        source=DATASET_SOURCE,
        tags=["public_benchmark", "humanevalplus", "phase1_public_projection"],
    )


@dataclass
class ExportFixture:
    run_dir: Path
    dataset_manifest: Path
    problems: tuple[ProblemSpec, ...]
    codes: dict[str, str]
    raw_outputs: dict[str, str]

    @property
    def responses_path(self) -> Path:
        return self.run_dir / "responses.jsonl"

    @property
    def manifest_path(self) -> Path:
        return self.run_dir / "manifest.json"

    @property
    def summary_path(self) -> Path:
        return self.run_dir / "summary.json"


def _response(
    problem: ProblemSpec,
    *,
    invocation_id: str,
    code: str,
    raw_output: str,
    duration: float,
) -> dict[str, Any]:
    return {
        "run_id": "phase1_test_export",
        "invocation_id": invocation_id,
        "problem_id": problem.problem_id,
        "provider": "hy3",
        "model": "offline-test-model",
        "status": "success",
        "parse_status": "parsed",
        "started_at": "2026-08-24T00:00:00.000Z",
        "ended_at": "2026-08-24T00:00:01.000Z",
        "duration_seconds": duration,
        "attempt_count": 1,
        "retry_count": 0,
        "raw_output_attempt": 1,
        "parse_attempted": True,
        "raw_output": raw_output,
        "solution_trace": {
            "problem_id": problem.problem_id,
            "requirement_understanding": "公开需求理解",
            "design_summary": "公开设计摘要",
            "edge_cases_considered": [],
            "implementation_steps": [],
            "declared_time_complexity": "O(1)",
            "declared_space_complexity": "O(1)",
            "code": code,
        },
        "error_type": None,
        "error": None,
    }


def _skipped(problem: ProblemSpec, *, invocation_id: str) -> dict[str, Any]:
    return {
        "run_id": "phase1_test_export",
        "invocation_id": invocation_id,
        "problem_id": problem.problem_id,
        "provider": "hy3",
        "model": "offline-test-model",
        "status": "skipped",
        "parse_status": "not_attempted",
        "started_at": "2026-08-24T00:01:00.000Z",
        "ended_at": "2026-08-24T00:01:00.000Z",
        "duration_seconds": 0.0,
        "attempt_count": 0,
        "retry_count": 0,
        "raw_output_attempt": None,
        "parse_attempted": False,
        "raw_output": None,
        "solution_trace": None,
        "error_type": None,
        "error": None,
    }


def _write_fixture(tmp_path: Path, *, with_skipped: bool = False) -> ExportFixture:
    problems = tuple(_problem(problem_id) for problem_id in EXPECTED_IDS)
    dataset_dir = tmp_path / "dataset"
    dataset_dir.mkdir()
    problem_bytes = _jsonl_bytes([problem.model_dump(mode="json") for problem in problems])
    (dataset_dir / "problems.jsonl").write_bytes(problem_bytes)

    source_hash, raw_aggregate, raw_test_hash = _source_identity()
    dataset_payload = {
        "schema_version": 1,
        "kind": "tracejudge_dataset_selection",
        "experiment_label": PILOT_EXPERIMENT_LABEL,
        "metrics_scope": "generation_and_parsing_only",
        "dataset_id": DATASET_ID,
        "source": DATASET_SOURCE,
        "revision": PINNED_HUMANEVALPLUS_REVISION,
        "split": "test",
        "license": "apache-2.0",
        "adapter": {"name": ADAPTER_NAME, "version": ADAPTER_VERSION},
        "source_manifest_sha256": source_hash,
        "parent_manifest_sha256": "b" * 64,
        "raw_snapshot": {
            "aggregate_sha256": raw_aggregate,
            "test_jsonl_sha256": raw_test_hash,
            "record_count": EXPECTED_RECORD_COUNT,
        },
        "public_projection": {
            "path": "problems.jsonl",
            "sha256": _sha256(problem_bytes),
            "record_count": 10,
            "ordered_problem_ids_sha256": ordered_problem_ids_sha256(EXPECTED_IDS),
        },
        "selection": {
            "algorithm": SELECTION_ALGORITHM,
            "seed": 20260824,
            "count": 10,
            "selected_problem_ids": list(EXPECTED_IDS),
        },
        "withheld_fields": ["canonical_solution", "test"],
        "limitations": list(PILOT_LIMITATIONS),
    }
    dataset_manifest = dataset_dir / "dataset_manifest.json"
    dataset_bytes = _json_bytes(dataset_payload)
    dataset_manifest.write_bytes(dataset_bytes)

    expected_provenance = {
        "manifest_sha256": _sha256(dataset_bytes),
        "kind": "tracejudge_dataset_selection",
        "dataset_id": DATASET_ID,
        "revision": PINNED_HUMANEVALPLUS_REVISION,
        "source": DATASET_SOURCE,
        "license": "apache-2.0",
        "adapter": {"name": ADAPTER_NAME, "version": ADAPTER_VERSION},
        "raw_snapshot": {
            "aggregate_sha256": raw_aggregate,
            "test_jsonl_sha256": raw_test_hash,
            "record_count": EXPECTED_RECORD_COUNT,
        },
        "public_projection": {
            "sha256": _sha256(problem_bytes),
            "record_count": 10,
            "ordered_problem_ids_sha256": ordered_problem_ids_sha256(EXPECTED_IDS),
        },
        "selection": dataset_payload["selection"],
        "withheld_fields": ["canonical_solution", "test"],
        "metrics_scope": "generation_and_parsing_only",
        "source_manifest_sha256": source_hash,
        "parent_manifest_sha256": "b" * 64,
    }

    first_invocation = {
        "invocation_id": "invocation-first",
        "started_at": "2026-08-24T00:00:00.000Z",
        "resume": False,
        "status": "completed",
        "completed_at": "2026-08-24T00:00:20.000Z",
        "git": {
            "available": True,
            "branch": "codex/test",
            "commit": "a" * 40,
            "dirty": False,
            "working_tree_sha256": None,
        },
        "environment": {"python": {"version": "3.12.0"}},
    }
    invocations = [first_invocation]
    completed_at = first_invocation["completed_at"]
    if with_skipped:
        completed_at = "2026-08-24T00:01:20.000Z"
        invocations.append(
            {
                **first_invocation,
                "invocation_id": "invocation-resume",
                "started_at": "2026-08-24T00:01:00.000Z",
                "resume": True,
                "completed_at": completed_at,
            }
        )
    manifest = {
        "schema_version": 1,
        "phase": "phase1_baseline_generation",
        "experiment_label": PILOT_EXPERIMENT_LABEL,
        "run_id": "phase1_test_export",
        "created_at": "2026-08-24T00:00:00.000Z",
        "status": "completed",
        "completed_at": completed_at,
        "dataset": {
            "path": str((dataset_dir / "problems.jsonl").resolve()),
            "sha256": _sha256(problem_bytes),
            "problem_count": 10,
            "sources": {DATASET_SOURCE: 10},
            "difficulties": {"unknown": 10},
            "visible_tests": {
                "total_count": 0,
                "per_problem": {
                    problem_id: {"count": 0, "case_ids": []} for problem_id in EXPECTED_IDS
                },
            },
            "provenance": expected_provenance,
        },
        "git": first_invocation["git"],
        "environment": first_invocation["environment"],
        "provider_config": {"provider": "hy3", "model": "offline-test-model"},
        "invocations": invocations,
    }

    run_dir = tmp_path / "phase1_test_export"
    run_dir.mkdir()
    (run_dir / "manifest.json").write_bytes(_json_bytes(manifest))
    sentinel = tmp_path / "candidate_must_not_execute"
    codes: dict[str, str] = {}
    raw_outputs: dict[str, str] = {}
    responses: list[dict[str, Any]] = []
    for index, problem in enumerate(problems):
        entry_point = problem.function_name
        if index == 0:
            code = (
                "import os\n\n"
                f"def {entry_point}(values):\n"
                '    api_key = os.getenv("SAFE_CONFIG_NAME")\n'
                '    marker = "精确保留 UTF-8"\n'
                "    return len(values)\n"
            )
        elif index == 1:
            code = (
                "from pathlib import Path\n"
                f'Path({str(sentinel)!r}).write_text("executed")\n\n'
                f"def {entry_point}(values):\n"
                "    return len(values)\n"
            )
        else:
            code = f"def {entry_point}(values):\n    return len(values)\n"
        raw_output = f"def wrong_raw_{index}():\n    return 'RAW_OUTPUT_IS_NOT_CODE_{index}'\n"
        codes[problem.problem_id] = code
        raw_outputs[problem.problem_id] = raw_output
        responses.append(
            _response(
                problem,
                invocation_id="invocation-first",
                code=code,
                raw_output=raw_output,
                duration=float(index + 1),
            )
        )
    if with_skipped:
        responses.extend(
            _skipped(problem, invocation_id="invocation-resume") for problem in problems
        )
    (run_dir / "responses.jsonl").write_bytes(_jsonl_bytes(responses))

    history_counts = {"success": 10}
    current_counts = {"success": 10}
    current_invocation = first_invocation
    skipped_count = 0
    if with_skipped:
        history_counts = {"skipped": 10, "success": 10}
        current_counts = {"skipped": 10}
        current_invocation = invocations[-1]
        skipped_count = 10
    summary = {
        "run_id": "phase1_test_export",
        "experiment_label": PILOT_EXPERIMENT_LABEL,
        "updated_at": completed_at,
        "completed_at": completed_at,
        "total_problem_count": 10,
        "dataset_problem_count": 10,
        "final_outcome_counts": {
            "success": 10,
            "parse_error": 0,
            "provider_error": 0,
            "failure": 0,
        },
        "success_count": 10,
        "parse_error_count": 0,
        "provider_error_count": 0,
        "failure_count": 0,
        "pending_count": 0,
        "parse_attempted_count": 10,
        "parse_success_count": 10,
        "parse_failure_count": 0,
        "parse_success_rate": 1.0,
        "average_duration_seconds": 5.5,
        "record_count": len(responses),
        "record_status_counts": history_counts,
        "status_counts": history_counts,
        "invocation": {
            "invocation_id": current_invocation["invocation_id"],
            "started_at": current_invocation["started_at"],
            "completed_at": current_invocation["completed_at"],
            "status_counts": current_counts,
            "skipped_count": skipped_count,
        },
        "skipped_count": skipped_count,
        "metrics_scope": "generation_and_parsing_only",
    }
    (run_dir / "summary.json").write_bytes(_json_bytes(summary))
    return ExportFixture(
        run_dir=run_dir,
        dataset_manifest=dataset_manifest,
        problems=problems,
        codes=codes,
        raw_outputs=raw_outputs,
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_bytes(_jsonl_bytes(rows))


def test_export_uses_only_solution_code_and_returns_stable_public_metadata(tmp_path):
    fixture = _write_fixture(tmp_path)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    exported = load_validated_phase1_export(fixture.run_dir, fixture.dataset_manifest)

    assert [sample.task_id for sample in exported.samples] == list(EXPECTED_IDS)
    assert [sample.solution for sample in exported.samples] == [
        fixture.codes[problem_id] for problem_id in EXPECTED_IDS
    ]
    assert all(
        raw_output not in {sample.solution for sample in exported.samples}
        for raw_output in fixture.raw_outputs.values()
    )
    response_lines = fixture.responses_path.read_bytes().splitlines(keepends=True)
    for index, (sample, reference) in enumerate(
        zip(exported.samples, exported.response_references, strict=True)
    ):
        assert reference.problem_id == sample.task_id
        assert reference.response_line_number == index + 1
        assert reference.response_record_sha256 == _sha256(response_lines[index])
        assert reference.code_sha256 == _sha256(sample.solution.encode("utf-8"))
    assert [metadata.problem_id for metadata in exported.task_metadata] == list(EXPECTED_IDS)
    for problem, metadata in zip(fixture.problems, exported.task_metadata, strict=True):
        assert metadata.prompt_sha256 == _sha256(problem.requirement.encode("utf-8"))
        assert metadata.entry_point == problem.function_name
        assert "prompt" not in asdict(metadata)
        assert metadata.to_preflight_dict() == {
            "task_id": problem.problem_id,
            "prompt_sha256": metadata.prompt_sha256,
            "entry_point": problem.function_name,
        }
    serialized = serialize_samples_jsonl(exported.samples)
    rows = [json.loads(line) for line in serialized.decode("utf-8").splitlines()]
    assert rows == [
        {"task_id": sample.task_id, "solution": sample.solution} for sample in exported.samples
    ]
    assert "精确保留 UTF-8" in serialized.decode("utf-8")
    assert exported.samples_sha256 == _sha256(serialized)
    assert not (tmp_path / "candidate_must_not_execute").exists()
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


def test_legal_resume_skipped_records_are_ignored(tmp_path):
    fixture = _write_fixture(tmp_path, with_skipped=True)

    exported = load_validated_phase1_export(fixture.run_dir, fixture.dataset_manifest)

    assert len(exported.samples) == len(exported.response_references) == 10
    assert [reference.response_line_number for reference in exported.response_references] == list(
        range(1, 11)
    )


@pytest.mark.parametrize("mutation", ["duplicate_success", "missing_success", "extra_id"])
def test_response_id_set_and_success_uniqueness_are_strict(tmp_path, mutation):
    fixture = _write_fixture(tmp_path)
    rows = _read_jsonl(fixture.responses_path)
    if mutation == "duplicate_success":
        rows.append(rows[0])
    elif mutation == "missing_success":
        rows.pop()
    else:
        rows[0]["problem_id"] = "HumanEval/999"
        rows[0]["solution_trace"]["problem_id"] = "HumanEval/999"
    _write_jsonl(fixture.responses_path, rows)

    with pytest.raises(EvalPlusExportError):
        load_validated_phase1_export(fixture.run_dir, fixture.dataset_manifest)


@pytest.mark.parametrize("mutation", ["inner_id", "empty", "credential", "authorization"])
def test_solution_code_and_inner_identity_are_strict(tmp_path, mutation):
    fixture = _write_fixture(tmp_path)
    rows = _read_jsonl(fixture.responses_path)
    if mutation == "inner_id":
        rows[0]["solution_trace"]["problem_id"] = EXPECTED_IDS[1]
    elif mutation == "empty":
        rows[0]["solution_trace"]["code"] = " \n\t"
    elif mutation == "credential":
        rows[0]["solution_trace"]["code"] = 'api_key = "API_KEY_CANARY_9842"\n'
    else:
        rows[0]["solution_trace"]["code"] = (
            'headers = {"Authorization": "Bearer AUTH_CANARY_9842"}\n'
        )
    _write_jsonl(fixture.responses_path, rows)

    with pytest.raises(EvalPlusExportError) as caught:
        load_validated_phase1_export(fixture.run_dir, fixture.dataset_manifest)
    assert "API_KEY_CANARY_9842" not in str(caught.value)
    assert "AUTH_CANARY_9842" not in str(caught.value)


def test_credential_canary_in_raw_output_is_not_exported(tmp_path):
    fixture = _write_fixture(tmp_path)
    rows = _read_jsonl(fixture.responses_path)
    canary = "RAW_AUTHORIZATION_CANARY_1519"
    rows[0]["raw_output"] = f"Authorization: Bearer {canary}"
    _write_jsonl(fixture.responses_path, rows)

    exported = load_validated_phase1_export(fixture.run_dir, fixture.dataset_manifest)
    serialized = serialize_samples_jsonl(exported.samples).decode("utf-8")

    assert canary not in serialized
    assert all(canary not in sample.solution for sample in exported.samples)


@pytest.mark.parametrize("mutation", ["revision", "projection_hash", "phase1_provenance"])
def test_hash_revision_and_provenance_mismatches_are_rejected(tmp_path, mutation):
    fixture = _write_fixture(tmp_path)
    if mutation == "revision":
        manifest = json.loads(fixture.dataset_manifest.read_text(encoding="utf-8"))
        manifest["revision"] = "f" * 40
        fixture.dataset_manifest.write_bytes(_json_bytes(manifest))
    elif mutation == "projection_hash":
        problems_path = fixture.dataset_manifest.parent / "problems.jsonl"
        problems_path.write_bytes(problems_path.read_bytes() + b"\n")
    else:
        manifest = json.loads(fixture.manifest_path.read_text(encoding="utf-8"))
        manifest["dataset"]["provenance"]["revision"] = "f" * 40
        fixture.manifest_path.write_bytes(_json_bytes(manifest))

    with pytest.raises(EvalPlusExportError):
        load_validated_phase1_export(fixture.run_dir, fixture.dataset_manifest)


@pytest.mark.parametrize("artifact", ["manifest", "summary"])
def test_completed_manifest_and_consistent_summary_are_required(tmp_path, artifact):
    fixture = _write_fixture(tmp_path)
    path = fixture.manifest_path if artifact == "manifest" else fixture.summary_path
    payload = json.loads(path.read_text(encoding="utf-8"))
    if artifact == "manifest":
        payload["status"] = "running"
        payload["completed_at"] = None
    else:
        payload["success_count"] = 9
    path.write_bytes(_json_bytes(payload))

    with pytest.raises(EvalPlusExportError):
        load_validated_phase1_export(fixture.run_dir, fixture.dataset_manifest)


def test_static_export_does_not_use_execution_network_or_provider_boundaries(tmp_path, monkeypatch):
    fixture = _write_fixture(tmp_path)
    calls: list[str] = []

    def forbidden(*args, **kwargs):
        calls.append("forbidden")
        raise AssertionError("static exporter crossed an execution boundary")

    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)

    exported = load_validated_phase1_export(fixture.run_dir, fixture.dataset_manifest)

    assert len(exported.samples) == 10
    assert calls == []
    assert not (tmp_path / "candidate_must_not_execute").exists()


def test_dataset_task_metadata_order_and_ast_entry_point_must_match(tmp_path):
    fixture = _write_fixture(tmp_path)
    problems_path = fixture.dataset_manifest.parent / "problems.jsonl"
    rows = _read_jsonl(problems_path)
    rows[0]["requirement"] = rows[0]["requirement"].replace(
        fixture.problems[0].function_name, "forged_entry_point"
    )
    changed = _jsonl_bytes(rows)
    problems_path.write_bytes(changed)
    dataset_manifest = json.loads(fixture.dataset_manifest.read_text(encoding="utf-8"))
    dataset_manifest["public_projection"]["sha256"] = _sha256(changed)
    fixture.dataset_manifest.write_bytes(_json_bytes(dataset_manifest))
    phase1_manifest = json.loads(fixture.manifest_path.read_text(encoding="utf-8"))
    phase1_manifest["dataset"]["sha256"] = _sha256(changed)
    # Keep the provenance hash chain otherwise internally consistent so this
    # reaches the AST-derived public entry-point check.
    revised_dataset_bytes = fixture.dataset_manifest.read_bytes()
    phase1_manifest["dataset"]["provenance"]["manifest_sha256"] = _sha256(revised_dataset_bytes)
    phase1_manifest["dataset"]["provenance"]["public_projection"]["sha256"] = _sha256(changed)
    fixture.manifest_path.write_bytes(_json_bytes(phase1_manifest))

    with pytest.raises(EvalPlusExportError, match="entry_point"):
        load_validated_phase1_export(fixture.run_dir, fixture.dataset_manifest)


@pytest.mark.parametrize(
    "artifact",
    ["dataset_manifest", "problems", "manifest", "summary", "responses"],
)
def test_static_input_size_limit_is_checked_before_reading(tmp_path, monkeypatch, artifact):
    fixture = _write_fixture(tmp_path)
    paths = {
        "dataset_manifest": fixture.dataset_manifest,
        "problems": fixture.dataset_manifest.parent / "problems.jsonl",
        "manifest": fixture.manifest_path,
        "summary": fixture.summary_path,
        "responses": fixture.responses_path,
    }
    oversized_path = paths[artifact]
    original_stat = Path.stat
    original_open = Path.open

    def reported_stat(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if path == oversized_path:
            fields = list(result)
            fields[6] = exporter_module._MAX_STATIC_INPUT_BYTES + 1
            return os.stat_result(fields)
        return result

    def guarded_open(path, *args, **kwargs):
        if path == oversized_path:
            raise AssertionError("oversized input was opened")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", reported_stat)
    monkeypatch.setattr(Path, "open", guarded_open)

    with pytest.raises(EvalPlusExportError, match="static input size limit"):
        load_validated_phase1_export(fixture.run_dir, fixture.dataset_manifest)


def test_solution_utf8_size_limit_precedes_serialization(tmp_path, monkeypatch):
    fixture = _write_fixture(tmp_path)
    rows = _read_jsonl(fixture.responses_path)
    prefix = f"def {fixture.problems[0].function_name}(values):\n    return 0\n# "
    remaining = exporter_module._MAX_SOLUTION_BYTES - len(prefix.encode("utf-8"))
    oversized_code = prefix + ("界" * (remaining // 3 + 1))
    assert len(oversized_code.encode("utf-8")) > exporter_module._MAX_SOLUTION_BYTES
    rows[0]["solution_trace"]["code"] = oversized_code
    _write_jsonl(fixture.responses_path, rows)
    serialize_calls: list[object] = []

    def forbidden_serialize(samples):
        serialize_calls.append(samples)
        raise AssertionError("oversized solution reached serialization")

    monkeypatch.setattr(exporter_module, "serialize_samples_jsonl", forbidden_serialize)

    with pytest.raises(EvalPlusExportError, match="code exceeds the size limit"):
        load_validated_phase1_export(fixture.run_dir, fixture.dataset_manifest)
    assert serialize_calls == []
