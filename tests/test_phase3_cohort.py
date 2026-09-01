from __future__ import annotations

import hashlib
import json
import stat
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import tracejudge_hy3.cli as cli_module
import tracejudge_hy3.phase3.cohort as cohort_module
from tracejudge_hy3.cli import app
from tracejudge_hy3.dataset.humanevalplus import (
    DATASET_SOURCE,
    RESEARCH_NATURAL_EXPERIMENT_LABEL,
    WITHHELD_REFERENCE_CODE,
)
from tracejudge_hy3.evalplus.exporter import serialize_samples_jsonl
from tracejudge_hy3.evalplus.schemas import (
    EvalPlusSample,
    HumanEvalPlusDatasetIdentity,
    HumanEvalPlusTaskMetadata,
    Phase1ExportSelectionIdentity,
    Phase1ResponseReference,
    Phase1SourceIdentity,
    ValidatedSampleExport,
)
from tracejudge_hy3.phase3.cohort import (
    Phase3FreezeError,
    Phase3FreezeResult,
    Phase3PreflightResult,
    freeze_natural_cohort,
    preflight_natural_cohort,
)
from tracejudge_hy3.phase3.contracts import FrozenCohortManifest, MethodId
from tracejudge_hy3.schemas.problem import ProblemSpec, RequirementItem

NOW = datetime(2026, 8, 28, tzinfo=UTC)
RAW_CANARY = "PRIVATE_PROVIDER_RAW_CANARY_MUST_NOT_LEAK"
CODE_CANARY = "CANDIDATE_CODE_BODY_MUST_NOT_BE_PUBLISHED"
SOURCE_IDS = tuple(f"HumanEval/{index}" for index in range(45))
SUCCESS_IDS = SOURCE_IDS[:42]


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
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


def _problem(problem_id: str) -> ProblemSpec:
    number = problem_id.split("/")[-1]
    entry_point = f"candidate_{number}"
    prompt = (
        "from __future__ import annotations\n\n"
        f"def {entry_point}(value: int) -> int:\n"
        f'    """Return the public deterministic value for task {number}."""\n'
    )
    return ProblemSpec(
        problem_id=problem_id,
        title=f"{problem_id}: {entry_point}",
        requirement=prompt,
        function_signature=f"def {entry_point}(value: int) -> int:",
        requirements=[
            RequirementItem(
                requirement_id="R1",
                content=f"Return the public deterministic value for task {number}.",
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


def _solution(problem_id: str) -> tuple[str, dict[str, Any]]:
    number = problem_id.split("/")[-1]
    code = (
        f"def candidate_{number}(value: int) -> int:\n"
        f"    marker = {CODE_CANARY!r}\n"
        "    return value\n"
    )
    return code, {
        "problem_id": problem_id,
        "requirement_understanding": "Return the documented public value.",
        "design_summary": "Return the input directly.",
        "edge_cases_considered": ["integer input"],
        "implementation_steps": [
            {
                "step_id": "S1",
                "content": "Return the input value.",
                "related_requirements": ["R1"],
                "expected_code_behavior": "The public return contract is preserved.",
            }
        ],
        "declared_time_complexity": "O(1)",
        "declared_space_complexity": "O(1)",
        "code": code,
    }


def _phase1_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, ValidatedSampleExport, cohort_module._ValidatedPhase1FreezeInput]:
    dataset_dir = tmp_path / "dataset"
    dataset_dir.mkdir()
    problems = tuple(_problem(problem_id) for problem_id in SOURCE_IDS)
    problems_bytes = _jsonl_bytes([problem.model_dump(mode="json") for problem in problems])
    (dataset_dir / "problems.jsonl").write_bytes(problems_bytes)
    dataset_manifest_bytes = _json_bytes({"fixture": "research-natural-45"})
    dataset_manifest = dataset_dir / "dataset_manifest.json"
    dataset_manifest.write_bytes(dataset_manifest_bytes)

    run_dir = tmp_path / "phase1_fixture"
    run_dir.mkdir()
    manifest_bytes = _json_bytes({"fixture": "phase1-manifest"})
    summary_bytes = _json_bytes({"fixture": "phase1-summary"})
    (run_dir / "manifest.json").write_bytes(manifest_bytes)
    (run_dir / "summary.json").write_bytes(summary_bytes)

    response_rows: list[dict[str, Any]] = []
    sample_by_id: dict[str, EvalPlusSample] = {}
    for problem_id in SUCCESS_IDS:
        code, solution = _solution(problem_id)
        sample_by_id[problem_id] = EvalPlusSample(task_id=problem_id, solution=code)
        response_rows.append(
            {
                "run_id": "phase1_fixture",
                "invocation_id": "invocation_1",
                "problem_id": problem_id,
                "provider": "hy3",
                "model": "offline-test-model",
                "status": "success",
                "parse_status": "parsed",
                "started_at": "2026-08-28T00:00:00.000Z",
                "ended_at": "2026-08-28T00:00:01.000Z",
                "duration_seconds": 1.0,
                "attempt_count": 1,
                "retry_count": 0,
                "attempt_outcomes": ["success"],
                "raw_output_attempt": 1,
                "parse_attempted": True,
                "raw_output": f"{RAW_CANARY}:{problem_id}",
                "solution_trace": solution,
                "error_type": None,
                "error": None,
            }
        )
    for problem_id in SOURCE_IDS[42:]:
        response_rows.append(
            {
                "run_id": "phase1_fixture",
                "invocation_id": "invocation_1",
                "problem_id": problem_id,
                "provider": "hy3",
                "model": "offline-test-model",
                "status": "provider_error",
                "parse_status": "not_attempted",
                "started_at": "2026-08-28T00:00:00.000Z",
                "ended_at": "2026-08-28T00:00:01.000Z",
                "duration_seconds": 1.0,
                "attempt_count": 1,
                "retry_count": 0,
                "attempt_outcomes": ["provider_error"],
                "raw_output_attempt": None,
                "parse_attempted": False,
                "raw_output": None,
                "solution_trace": None,
                "error_type": "ProviderError",
                "error": {"type": "ProviderError", "message": "safe fixture failure"},
            }
        )
    responses_bytes = _jsonl_bytes(response_rows)
    responses_path = run_dir / "responses.jsonl"
    responses_path.write_bytes(responses_bytes)
    raw_lines = responses_bytes.splitlines(keepends=True)

    samples = tuple(sample_by_id[problem_id] for problem_id in SUCCESS_IDS)
    references = tuple(
        Phase1ResponseReference(
            phase1_run_id="phase1_fixture",
            problem_id=problem_id,
            invocation_id="invocation_1",
            response_line_number=index + 1,
            response_record_sha256=_sha256(raw_lines[index]),
            code_sha256=_sha256(sample_by_id[problem_id].solution.encode("utf-8")),
        )
        for index, problem_id in enumerate(SUCCESS_IDS)
    )
    task_metadata = tuple(
        HumanEvalPlusTaskMetadata(
            problem_id=problem_id,
            prompt_sha256=_sha256(problems[index].requirement.encode("utf-8")),
            entry_point=f"candidate_{index}",
        )
        for index, problem_id in enumerate(SUCCESS_IDS)
    )
    exported = ValidatedSampleExport(
        phase1=Phase1SourceIdentity(
            run_id="phase1_fixture",
            experiment_label=RESEARCH_NATURAL_EXPERIMENT_LABEL,
            manifest_sha256=_sha256(manifest_bytes),
            summary_sha256=_sha256(summary_bytes),
            responses_sha256=_sha256(responses_bytes),
            git_commit="a" * 40,
            git_branch="phase1-fixture",
            git_dirty=False,
            provider="hy3",
            model="offline-test-model",
        ),
        dataset=HumanEvalPlusDatasetIdentity(
            manifest_sha256=_sha256(dataset_manifest_bytes),
            dataset_id="evalplus/humanevalplus",
            source=DATASET_SOURCE,
            revision="b" * 40,
            license="apache-2.0",
            adapter_name="tracejudge_humanevalplus_public_projection",
            adapter_version=1,
            source_manifest_sha256="c" * 64,
            parent_manifest_sha256="d" * 64,
            raw_snapshot_aggregate_sha256="e" * 64,
            raw_test_jsonl_sha256="f" * 64,
            problems_sha256=_sha256(problems_bytes),
            ordered_problem_ids_sha256=_sha256("\n".join(SOURCE_IDS).encode("utf-8")),
            selection_algorithm=r"sha256(seed\\0problem_id)-lowest-v1",
            selection_seed=20260825,
            selected_problem_ids=SOURCE_IDS,
            selection_role="research_natural",
        ),
        samples=samples,
        response_references=references,
        task_metadata=task_metadata,
        samples_sha256=_sha256(serialize_samples_jsonl(samples)),
        export_selection=Phase1ExportSelectionIdentity(
            selection_policy="phase1-success-only",
            min_success_count=30,
            source_problem_count=45,
            exported_success_count=42,
            excluded_parse_error_count=0,
            excluded_provider_error_count=3,
        ),
    )
    monkeypatch.setattr(
        cohort_module,
        "load_validated_phase1_export",
        lambda *_args, **_kwargs: exported,
    )
    frozen_input = cohort_module._load_phase1_freeze_input(
        run_dir,
        dataset_manifest,
        privacy_canaries=(RAW_CANARY,),
    )
    return run_dir, dataset_manifest, exported, frozen_input


def _phase2_fixture(
    tmp_path: Path,
    exported: ValidatedSampleExport,
    *,
    inconsistent_first_row: bool = False,
) -> Path:
    run_dir = tmp_path / ("phase2_inconsistent" if inconsistent_first_row else "phase2_fixture")
    run_dir.mkdir()
    results: list[dict[str, Any]] = []
    for index, (sample, reference) in enumerate(
        zip(exported.samples, exported.response_references, strict=True)
    ):
        results.append(
            {
                "schema_version": 1,
                "run_id": run_dir.name,
                "problem_id": sample.task_id,
                "base_status": "pass",
                "plus_status": "pass",
                "base_fail_test_count": 0,
                "plus_fail_test_count": 0,
                "passed_base": True,
                "passed_plus": False if inconsistent_first_row and index == 0 else True,
                "error_type": None,
                "infrastructure_status": "ok",
                "solution_sha256": reference.code_sha256,
                "official_override_hash": "1" * 32,
                "duration_seconds": 1.0,
                "started_at": "2026-08-28T00:00:00.000Z",
                "ended_at": "2026-08-28T00:00:01.000Z",
                "failure_count_scope": "recorded_by_evalplus_test_details",
                "source_response": asdict(reference),
            }
        )
    results_bytes = _jsonl_bytes(results)
    summary = {
        "schema_version": 1,
        "run_id": run_dir.name,
        "experiment_label": "humanevalplus_42_of_45_evalplus_execution_research_natural",
        "execution_mode": "docker",
        "selection_policy": "phase1-success-only",
        "min_success_count": 30,
        "source_problem_count": 45,
        "exported_success_count": 42,
        "excluded_parse_error_count": 0,
        "excluded_provider_error_count": 3,
        "result_count": 42,
        "actual_execution_count": 42,
        "base_pass_count": 42,
        "base_plus_pass_count": 41 if inconsistent_first_row else 42,
        "timeout_count": 0,
        "infrastructure_error_count": 0,
        "mock_not_executed_count": 0,
        "container_cleanup_failed_count": 0,
        "evaluation_complete": True,
    }
    summary_bytes = _json_bytes(summary)
    execution_log_bytes = _jsonl_bytes([{"event": "completed"}])
    (run_dir / "results.jsonl").write_bytes(results_bytes)
    (run_dir / "summary.json").write_bytes(summary_bytes)
    (run_dir / "execution.log").write_bytes(execution_log_bytes)

    manifest = {
        "schema_version": 1,
        "phase": "phase2_evalplus_execution",
        "status": "completed",
        "run_id": run_dir.name,
        "experiment_label": summary["experiment_label"],
        "execution_mode": "docker",
        "phase1_source": asdict(exported.phase1),
        "dataset": asdict(exported.dataset),
        "input": {
            "record_count": len(exported.samples),
            "ordered_problem_ids": [sample.task_id for sample in exported.samples],
            "code_sha256": {
                reference.problem_id: reference.code_sha256
                for reference in exported.response_references
            },
            "samples_sha256": exported.samples_sha256,
            "phase1_export_selection": asdict(exported.export_selection),
            "public_task_identity": [asdict(item) for item in exported.task_metadata],
        },
        "output": {
            "result_count": len(exported.samples),
            "samples_sha256": exported.samples_sha256,
            "results_sha256": _sha256(results_bytes),
            "summary_sha256": _sha256(summary_bytes),
            "execution_log_sha256": _sha256(execution_log_bytes),
            "raw_results_sha256": "2" * 64,
        },
    }
    (run_dir / "manifest.json").write_bytes(_json_bytes(manifest))
    # If either forbidden file is ever opened, the deliberately broken symlink
    # will fail.  Gate B must use only manifest/summary/results/execution.log.
    (run_dir / "samples.jsonl").symlink_to(run_dir / "missing-private-samples")
    (run_dir / "evalplus_raw_results.json").symlink_to(run_dir / "missing-private-raw")
    return run_dir


def test_gate_b_freezes_all_successes_without_raw_or_code_leakage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    phase1_run, dataset_manifest, exported, phase1 = _phase1_fixture(tmp_path, monkeypatch)
    phase2_run = _phase2_fixture(tmp_path, exported)
    phase2_manifest = json.loads((phase2_run / "manifest.json").read_text(encoding="utf-8"))
    assert set(phase2_manifest["input"]["public_task_identity"][0]) == {
        "problem_id",
        "prompt_sha256",
        "entry_point",
    }
    phase2 = cohort_module._load_phase2_freeze_input(phase2_run, phase1)
    assert len(phase2.evidence_by_problem) == 42

    result = freeze_natural_cohort(
        phase1_run_dir=phase1_run,
        phase2_run_dir=phase2_run,
        dataset_manifest_path=dataset_manifest,
        output_dir=tmp_path / "freezes",
        freeze_id="phase3_natural_fixture_v1",
        privacy_canaries=(RAW_CANARY,),
        created_at=NOW,
    )
    payload = result.manifest_path.read_bytes()
    assert RAW_CANARY.encode() not in payload
    assert CODE_CANARY.encode() not in payload
    assert b"raw_output" not in payload
    manifest = FrozenCohortManifest.model_validate_json(payload)
    assert manifest.source_accounting.source_problem_count == 45
    assert manifest.source_accounting.included_natural_trace_count == 42
    assert manifest.source_accounting.provider_error_count == 3
    assert manifest.ordered_trace_ids == tuple(
        f"natural:{problem_id}" for problem_id in SUCCESS_IDS
    )
    assert manifest.paired_method_ids == tuple(MethodId)
    assert result.manifest_sha256 == _sha256(payload)
    assert stat.S_IMODE(result.run_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(result.manifest_path.stat().st_mode) == 0o600

    with pytest.raises(Phase3FreezeError, match="already exists"):
        freeze_natural_cohort(
            phase1_run_dir=phase1_run,
            phase2_run_dir=phase2_run,
            dataset_manifest_path=dataset_manifest,
            output_dir=tmp_path / "freezes",
            freeze_id="phase3_natural_fixture_v1",
            created_at=NOW,
        )


def test_gate_b_preflight_runs_every_validation_without_publishing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    phase1_run, dataset_manifest, exported, _phase1 = _phase1_fixture(tmp_path, monkeypatch)
    phase2_run = _phase2_fixture(tmp_path, exported)
    output_dir = tmp_path / "preflight-must-not-exist"

    result = preflight_natural_cohort(
        phase1_run_dir=phase1_run,
        phase2_run_dir=phase2_run,
        dataset_manifest_path=dataset_manifest,
        output_dir=output_dir,
        freeze_id="phase3_preflight_fixture_v1",
        privacy_canaries=(RAW_CANARY,),
        created_at=NOW,
    )

    assert result.source_problem_count == 45
    assert result.natural_trace_count == 42
    assert result.provider_error_count == 3
    assert not output_dir.exists()


def test_phase1_response_hash_tampering_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    phase1_run, dataset_manifest, _exported, _phase1 = _phase1_fixture(tmp_path, monkeypatch)
    with (phase1_run / "responses.jsonl").open("ab") as stream:
        stream.write(b"{}\n")

    with pytest.raises(Phase3FreezeError, match="responses hash changed"):
        cohort_module._load_phase1_freeze_input(
            phase1_run,
            dataset_manifest,
            privacy_canaries=(),
        )


def test_phase2_inconsistent_safe_status_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _phase1_run, _dataset_manifest, exported, phase1 = _phase1_fixture(tmp_path, monkeypatch)
    phase2_run = _phase2_fixture(tmp_path, exported, inconsistent_first_row=True)

    with pytest.raises(Phase3FreezeError, match="functional status is inconsistent") as exc_info:
        cohort_module._load_phase2_freeze_input(phase2_run, phase1)
    assert exc_info.value.safe_stage == "P3B_PHASE2_RESULTS"


def test_gate_b_rejects_non_research_or_under_threshold_exports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _run, _manifest, exported, _phase1 = _phase1_fixture(tmp_path, monkeypatch)

    with pytest.raises(Phase3FreezeError, match="not the frozen research-natural"):
        cohort_module._validate_research_export(
            replace(exported, phase1=replace(exported.phase1, experiment_label="pilot"))
        )
    with pytest.raises(Phase3FreezeError, match="below 30"):
        cohort_module._validate_research_export(
            replace(
                exported,
                export_selection=replace(exported.export_selection, min_success_count=29),
            )
        )


def test_phase3_freeze_cli_reports_only_safe_coordinates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    manifest_path = tmp_path / "phase3_freeze" / "manifest.json"
    monkeypatch.setattr(
        cli_module,
        "freeze_natural_cohort",
        lambda **_kwargs: Phase3FreezeResult(
            freeze_id="phase3_freeze_v1",
            run_dir=manifest_path.parent,
            manifest_path=manifest_path,
            manifest_sha256="a" * 64,
            source_problem_count=45,
            natural_trace_count=42,
            parse_error_count=0,
            provider_error_count=3,
        ),
    )
    result = CliRunner().invoke(
        app,
        [
            "phase3",
            "freeze",
            "--phase1-run",
            "phase1",
            "--phase2-run",
            "phase2",
            "--dataset-manifest",
            "dataset_manifest.json",
            "--freeze-id",
            "phase3_freeze_v1",
            "--output-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0
    assert "42" in result.output
    assert "Provider" in result.output
    assert RAW_CANARY not in result.output


def test_phase3_preflight_cli_reports_no_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        cli_module,
        "preflight_natural_cohort",
        lambda **_kwargs: Phase3PreflightResult(
            freeze_id="phase3_preflight_v1",
            source_problem_count=45,
            natural_trace_count=42,
            parse_error_count=0,
            provider_error_count=3,
            phase1_run_id="phase1_fixture",
            phase2_run_id="phase2_fixture",
        ),
    )
    result = CliRunner().invoke(
        app,
        [
            "phase3",
            "preflight",
            "--phase1-run",
            "phase1",
            "--phase2-run",
            "phase2",
            "--dataset-manifest",
            "dataset_manifest.json",
            "--freeze-id",
            "phase3_preflight_v1",
            "--output-dir",
            str(tmp_path / "must-not-be-created"),
        ],
    )
    assert result.exit_code == 0
    assert "42" in result.output
    assert "创建目录 / manifest" in result.output
    assert "否" in result.output


def test_phase3_freeze_cli_does_not_echo_failure_details(
    monkeypatch: pytest.MonkeyPatch,
):
    secret = "PRIVATE_FAILURE_DETAIL"

    def fail(**_kwargs):
        raise Phase3FreezeError(secret)

    monkeypatch.setattr(cli_module, "freeze_natural_cohort", fail)
    result = CliRunner().invoke(
        app,
        [
            "phase3",
            "freeze",
            "--phase1-run",
            "phase1",
            "--phase2-run",
            "phase2",
            "--dataset-manifest",
            "dataset_manifest.json",
            "--freeze-id",
            "phase3_freeze_v1",
        ],
    )
    assert result.exit_code == 1
    assert secret not in result.output
    assert "P3B_VALIDATION" in result.output
