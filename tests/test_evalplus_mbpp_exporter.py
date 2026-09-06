from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path
from typing import Any

import pytest
from mbppplus_fixtures import REVISION, write_snapshot
from typer.testing import CliRunner

from tracejudge_hy3.baseline import run_baseline_experiment
from tracejudge_hy3.cli import app
from tracejudge_hy3.dataset.mbppplus import convert_mbppplus, sample_mbppplus
from tracejudge_hy3.evalplus_mbpp.exporter import (
    MbppCandidateExportError,
    export_mbpp_candidates,
)
from tracejudge_hy3.evalplus_mbpp.runner import load_validated_mbpp_inputs
from tracejudge_hy3.exceptions import ProviderResponseError
from tracejudge_hy3.providers.base import SolutionGeneration
from tracejudge_hy3.schemas.problem import ProblemSpec
from tracejudge_hy3.schemas.solution import ImplementationStep, SolutionTrace

SEED = 20260905
RUN_ID = "phase1_mbpp_export_fixture"


def _solution(problem: ProblemSpec, *, body: str = "    return None\n") -> SolutionTrace:
    entry_point = problem.function_name
    return SolutionTrace(
        problem_id=problem.problem_id,
        requirement_understanding=f"复述公开题面 {problem.problem_id}。",
        design_summary="按公开需求分步构造返回值。",
        edge_cases_considered=["空输入"],
        implementation_steps=[
            ImplementationStep(
                step_id="S1",
                content="实现与公开需求相对应的函数主体。",
                related_requirements=["R1"],
                expected_code_behavior="返回题面规定的结果。",
            )
        ],
        declared_time_complexity="O(n)",
        declared_space_complexity="O(1)",
        code=f"def {entry_point}(value):\n{body}",
    )


class _StubProvider:
    name = "stub"

    def __init__(self, *, fail_ids: set[str] | None = None) -> None:
        self.fail_ids = fail_ids or set()

    async def generate_solution_with_details(self, problem: ProblemSpec) -> SolutionGeneration:
        if problem.problem_id in self.fail_ids:
            return SolutionGeneration(
                status="provider_error",
                raw_output=None,
                solution=None,
                attempt_count=1,
                attempt_outcomes=("provider_error",),
                error=ProviderResponseError("synthetic provider failure"),
            )
        solution = _solution(problem)
        return SolutionGeneration(
            status="success",
            raw_output=solution.model_dump_json(),
            solution=solution,
            attempt_count=1,
            attempt_outcomes=("success",),
            raw_output_attempt=1,
            parse_attempted=True,
        )

    def public_generation_config(self) -> dict[str, Any]:
        return {"provider": self.name, "model": "offline-stub"}

    async def aclose(self) -> None:
        return None


@pytest.fixture()
def workspace(tmp_path: Path) -> dict[str, Any]:
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
        count=2,
        seed=SEED,
        output_dir=tmp_path / "sample2",
    )
    return {
        "tmp_path": tmp_path,
        "manifest": sample.manifest_path,
        "dataset": sample.dataset_path,
        "selected_ids": list(sample.selected_problem_ids),
    }


def _run_phase1(workspace: dict[str, Any], *, fail_ids: set[str] | None = None) -> Path:
    asyncio.run(
        run_baseline_experiment(
            dataset_path=workspace["dataset"],
            provider=_StubProvider(fail_ids=fail_ids),
            output_dir=workspace["tmp_path"] / "phase1",
            run_id=RUN_ID,
            dataset_manifest_path=workspace["manifest"],
        )
    )
    return workspace["tmp_path"] / "phase1" / RUN_ID


def test_export_happy_path_feeds_phase2_loader(workspace):
    run_dir = _run_phase1(workspace)
    output = workspace["tmp_path"] / "candidates.jsonl"
    result = export_mbpp_candidates(
        phase1_run_dir=run_dir,
        dataset_manifest_path=workspace["manifest"],
        output_path=output,
    )

    assert result.candidate_count == 2
    assert result.phase1_run_id == RUN_ID
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [row["task_id"] for row in rows] == workspace["selected_ids"]
    assert all(set(row) == {"task_id", "candidate_id", "code"} for row in rows)
    assert rows[0]["candidate_id"].startswith("Mbpp_")
    assert RUN_ID in rows[0]["candidate_id"]

    # The exported file is accepted unchanged by the phase-two input boundary.
    inputs = load_validated_mbpp_inputs(workspace["manifest"], output)
    assert [sample.task_id for sample in inputs.samples] == workspace["selected_ids"]

    # Re-export with identical contents is idempotent.
    again = export_mbpp_candidates(
        phase1_run_dir=run_dir,
        dataset_manifest_path=workspace["manifest"],
        output_path=output,
    )
    assert again.candidates_sha256 == result.candidates_sha256


def test_export_rejects_incomplete_phase1(workspace):
    missing = workspace["selected_ids"][1]
    run_dir = _run_phase1(workspace, fail_ids={missing})
    with pytest.raises(MbppCandidateExportError, match="lacks success records"):
        export_mbpp_candidates(
            phase1_run_dir=run_dir,
            dataset_manifest_path=workspace["manifest"],
            output_path=workspace["tmp_path"] / "candidates.jsonl",
        )
    assert not (workspace["tmp_path"] / "candidates.jsonl").exists()


def test_export_rejects_provenance_mismatch(workspace):
    run_dir = _run_phase1(workspace)
    # A different selection bundle (different seed) must not accept this run.
    other = sample_mbppplus(
        dataset_path=workspace["tmp_path"] / "full" / "problems.jsonl",
        source_manifest_path=workspace["tmp_path"] / "full" / "dataset_manifest.json",
        count=2,
        seed=SEED + 1,
        output_dir=workspace["tmp_path"] / "sample_other",
    )
    with pytest.raises(MbppCandidateExportError, match="different dataset snapshot"):
        export_mbpp_candidates(
            phase1_run_dir=run_dir,
            dataset_manifest_path=other.manifest_path,
            output_path=workspace["tmp_path"] / "candidates.jsonl",
        )


def test_export_rejects_retargeted_manifest_bytes(workspace):
    run_dir = _run_phase1(workspace)
    # Same selection content but different manifest bytes (trailing whitespace)
    # changes the manifest hash, so the run's recorded provenance must reject it.
    bundle_dir = workspace["tmp_path"] / "retargeted"
    bundle_dir.mkdir()
    (bundle_dir / "problems.jsonl").write_bytes(workspace["dataset"].read_bytes())
    retargeted = bundle_dir / "dataset_manifest.json"
    retargeted.write_bytes(workspace["manifest"].read_bytes() + b"  \n")
    with pytest.raises(MbppCandidateExportError, match="provenance"):
        export_mbpp_candidates(
            phase1_run_dir=run_dir,
            dataset_manifest_path=retargeted,
            output_path=workspace["tmp_path"] / "candidates.jsonl",
        )


def test_export_rejects_output_overwrite_with_different_content(workspace):
    run_dir = _run_phase1(workspace)
    output = workspace["tmp_path"] / "candidates.jsonl"
    output.write_text('{"task_id": "Mbpp/2", "candidate_id": "x", "code": "pass"}\n')
    with pytest.raises(MbppCandidateExportError, match="already exists"):
        export_mbpp_candidates(
            phase1_run_dir=run_dir,
            dataset_manifest_path=workspace["manifest"],
            output_path=output,
        )


def test_export_rejects_candidate_missing_entry_point(workspace, monkeypatch):
    run_dir = _run_phase1(workspace)
    responses = run_dir / "responses.jsonl"
    rows = [json.loads(line) for line in responses.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        if row["status"] == "success":
            row["solution_trace"]["code"] = "def unrelated_function(value):\n    return value\n"
    responses.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    with pytest.raises(MbppCandidateExportError, match="entry point"):
        export_mbpp_candidates(
            phase1_run_dir=run_dir,
            dataset_manifest_path=workspace["manifest"],
            output_path=workspace["tmp_path"] / "candidates.jsonl",
        )


def test_cli_export_mbpp_candidates(workspace):
    run_dir = _run_phase1(workspace)
    output = workspace["tmp_path"] / "candidates.jsonl"
    result = CliRunner().invoke(
        app,
        [
            "dataset",
            "export-mbpp-candidates",
            "--phase1-run",
            str(run_dir),
            "--manifest",
            str(workspace["manifest"]),
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "候选数" in result.output
    assert output.exists()


def test_cli_export_reports_failure(workspace):
    run_dir = _run_phase1(workspace, fail_ids={workspace["selected_ids"][0]})
    result = CliRunner().invoke(
        app,
        [
            "dataset",
            "export-mbpp-candidates",
            "--phase1-run",
            str(run_dir),
            "--manifest",
            str(workspace["manifest"]),
            "--output",
            str(workspace["tmp_path"] / "candidates.jsonl"),
        ],
    )
    assert result.exit_code == 1
    assert "MBPP+ 候选导出失败" in result.output
