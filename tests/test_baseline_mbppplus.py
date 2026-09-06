from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from mbppplus_fixtures import REVISION, write_snapshot

from tracejudge_hy3.baseline import (
    BaselineExperimentError,
    run_baseline_experiment,
)
from tracejudge_hy3.baseline.runner import _load_dataset_provenance
from tracejudge_hy3.dataset.loader import load_problems
from tracejudge_hy3.dataset.mbppplus import (
    FULL_EXPERIMENT_LABEL,
    FULL_PROJECTION_KIND,
    SELECTION_MANIFEST_KIND,
    convert_mbppplus,
    sample_mbppplus,
)
from tracejudge_hy3.providers.base import SolutionGeneration
from tracejudge_hy3.schemas.problem import ProblemSpec
from tracejudge_hy3.schemas.solution import ImplementationStep, SolutionTrace

SEED = 20260905


def _solution(problem: ProblemSpec) -> SolutionTrace:
    return SolutionTrace(
        problem_id=problem.problem_id,
        requirement_understanding=f"复述公开题面 {problem.problem_id}。",
        design_summary="按公开需求分步构造返回值。",
        edge_cases_considered=["空输入", "边界值"],
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
        code=f"{problem.function_signature}\n    return None\n",
    )


class _StubProvider:
    name = "stub"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def generate_solution_with_details(self, problem: ProblemSpec) -> SolutionGeneration:
        self.calls.append(problem.problem_id)
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
def sample_bundle(tmp_path: Path) -> dict[str, Any]:
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
        "full_manifest": conversion.manifest_path,
        "full_dataset": conversion.dataset_path,
        "manifest": sample.manifest_path,
        "dataset": sample.dataset_path,
        "selected_ids": list(sample.selected_problem_ids),
    }


def _dataset_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_full_projection_provenance_validates(sample_bundle):
    problems = load_problems(sample_bundle["full_dataset"])
    label, identity = _load_dataset_provenance(
        sample_bundle["full_manifest"],
        dataset_hash=_dataset_hash(sample_bundle["full_dataset"]),
        problems=problems,
    )
    assert label == FULL_EXPERIMENT_LABEL
    assert identity["kind"] == FULL_PROJECTION_KIND
    assert identity["revision"] == REVISION
    assert identity["release_tag"] == "v0.2.0"
    assert identity["raw_snapshot"]["record_count"] == 378
    assert identity["selection"]["algorithm"] == "all-pinned-task-ids-numeric-order-v1"
    assert identity["selection"]["seed"] is None


def test_selection_provenance_validates_exclusion_block(sample_bundle):
    # Build a disjoint second cohort so the exclusion block is non-trivial.
    second = sample_mbppplus(
        dataset_path=sample_bundle["full_dataset"],
        source_manifest_path=sample_bundle["full_manifest"],
        count=2,
        seed=SEED,
        output_dir=sample_bundle["tmp_path"] / "sample2b",
        exclude_manifests=[sample_bundle["manifest"]],
    )
    problems = load_problems(second.dataset_path)
    label, identity = _load_dataset_provenance(
        second.manifest_path,
        dataset_hash=_dataset_hash(second.dataset_path),
        problems=problems,
    )
    assert label == "mbppplus_2_public_prompt_generation_pilot"
    assert identity["kind"] == SELECTION_MANIFEST_KIND
    assert identity["selection"]["seed"] == SEED
    assert len(identity["excluded_manifests"]) == 1
    assert (
        identity["excluded_manifests"][0]["selected_problem_ids"] == (sample_bundle["selected_ids"])
    )
    assert identity["parent_manifest_sha256"]


def test_provenance_rejects_tampered_manifest(sample_bundle):
    problems = load_problems(sample_bundle["dataset"])
    payload = json.loads(sample_bundle["manifest"].read_text(encoding="utf-8"))
    payload["revision"] = "0" * 40
    tampered = sample_bundle["tmp_path"] / "tampered.json"
    tampered.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BaselineExperimentError, match="revision"):
        _load_dataset_provenance(
            tampered,
            dataset_hash=_dataset_hash(sample_bundle["dataset"]),
            problems=problems,
        )

    payload = json.loads(sample_bundle["manifest"].read_text(encoding="utf-8"))
    payload["public_projection"]["sha256"] = "0" * 64
    tampered.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BaselineExperimentError, match="differs from --dataset"):
        _load_dataset_provenance(
            tampered,
            dataset_hash=_dataset_hash(sample_bundle["dataset"]),
            problems=problems,
        )


def test_provenance_rejects_humaneval_manifest_for_mbpp_branch(sample_bundle):
    problems = load_problems(sample_bundle["dataset"])
    payload = json.loads(sample_bundle["manifest"].read_text(encoding="utf-8"))
    payload["kind"] = "tracejudge_dataset_selection"
    tampered = sample_bundle["tmp_path"] / "wrong_kind.json"
    tampered.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BaselineExperimentError):
        _load_dataset_provenance(
            tampered,
            dataset_hash=_dataset_hash(sample_bundle["dataset"]),
            problems=problems,
        )


def test_baseline_run_binds_mbpp_provenance(sample_bundle):
    provider = _StubProvider()
    import asyncio

    result = asyncio.run(
        run_baseline_experiment(
            dataset_path=sample_bundle["dataset"],
            provider=provider,
            output_dir=sample_bundle["tmp_path"] / "phase1",
            run_id="phase1_mbpp_fixture",
            dataset_manifest_path=sample_bundle["manifest"],
        )
    )
    assert sorted(provider.calls) == sorted(sample_bundle["selected_ids"])
    assert result.summary["success_count"] == 2
    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    provenance = manifest["dataset"]["provenance"]
    assert provenance["kind"] == SELECTION_MANIFEST_KIND
    assert provenance["dataset_id"] == "evalplus/mbppplus"
    assert manifest["experiment_label"] == "mbppplus_2_public_prompt_generation_pilot"


def test_baseline_requires_manifest_for_mbpp_source(sample_bundle):
    provider = _StubProvider()
    import asyncio

    with pytest.raises(BaselineExperimentError, match="require --dataset-manifest"):
        asyncio.run(
            run_baseline_experiment(
                dataset_path=sample_bundle["dataset"],
                provider=provider,
                output_dir=sample_bundle["tmp_path"] / "phase1_nomanifest",
                run_id="phase1_mbpp_nomanifest",
            )
        )
    assert provider.calls == []
