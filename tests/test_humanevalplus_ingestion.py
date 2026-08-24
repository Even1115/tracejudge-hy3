from __future__ import annotations

import builtins
import hashlib
import importlib
import json
import os
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

from tracejudge_hy3.baseline import BaselineExperimentError, run_baseline_experiment
from tracejudge_hy3.cli import app
from tracejudge_hy3.config import Settings
from tracejudge_hy3.dataset.humanevalplus import (
    ADAPTER_NAME,
    ADAPTER_VERSION,
    DATASET_SOURCE,
    EXPECTED_RECORD_COUNT,
    PILOT_EXPERIMENT_LABEL,
    SELECTION_ALGORITHM,
    WITHHELD_REFERENCE_CODE,
    ConversionResult,
    convert_humanevalplus,
    ordered_problem_ids_sha256,
    sample_humanevalplus,
)
from tracejudge_hy3.dataset.loader import load_problems
from tracejudge_hy3.exceptions import DatasetError
from tracejudge_hy3.providers.base import SolutionGeneration
from tracejudge_hy3.providers.hy3_openai import Hy3OpenAIProvider
from tracejudge_hy3.schemas.problem import ProblemSpec
from tracejudge_hy3.schemas.solution import ImplementationStep, SolutionTrace

REVISION = "d32357cf319e50e9c8d8dab5ea876c72b0fd321b"
SEED = 20260824
EXPECTED_TEN_IDS = (
    "HumanEval/8",
    "HumanEval/26",
    "HumanEval/41",
    "HumanEval/51",
    "HumanEval/70",
    "HumanEval/81",
    "HumanEval/95",
    "HumanEval/96",
    "HumanEval/105",
    "HumanEval/120",
)


@dataclass(frozen=True)
class _Snapshot:
    root: Path
    input_path: Path
    source_manifest_path: Path
    raw_bytes: bytes
    sentinel_path: Path
    private_canaries: tuple[str, ...]


@dataclass(frozen=True)
class _FullBundle:
    snapshot: _Snapshot
    conversion: ConversionResult


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _raw_row(index: int, *, private_variant: str, sentinel_path: Path) -> dict[str, Any]:
    entry_point = f"task_{index}"
    public_canary = f"PUBLIC_PROMPT_CANARY_{index}"
    canonical_canary = f"PRIVATE_CANONICAL_CANARY_{private_variant}_{index}"
    test_canary = f"PRIVATE_TEST_CANARY_{private_variant}_{index}"
    base_canary = f"PRIVATE_BASE_INPUT_CANARY_{private_variant}_{index}"
    plus_canary = f"PRIVATE_PLUS_INPUT_CANARY_{private_variant}_{index}"
    contract_canary = f"PRIVATE_CONTRACT_CANARY_{private_variant}_{index}"
    if index == 0:
        canonical_solution = (
            f"__import__('pathlib').Path({str(sentinel_path)!r}).write_text({canonical_canary!r})"
        )
        official_test = (
            f"__import__('pathlib').Path({str(sentinel_path)!r}).write_text({test_canary!r})"
        )
    else:
        canonical_solution = f"{canonical_canary} = {index}"
        official_test = f"{test_canary} = {index}"
    return {
        "task_id": f"HumanEval/{index}",
        "prompt": (
            "from typing import Any\n\n"
            f"def {entry_point}(value: int, *, delta: int = 1) -> int:\n"
            f'    """{public_canary}\n'
            "    Return ``value`` adjusted by the public ``delta`` argument.\n"
            '    """\n'
        ),
        "entry_point": entry_point,
        "canonical_solution": canonical_solution,
        "test": official_test,
        "base_input": base_canary,
        "plus_input": plus_canary,
        "contract": contract_canary,
    }


def _write_snapshot(root: Path, *, private_variant: str, reverse: bool = True) -> _Snapshot:
    root.mkdir(parents=True, exist_ok=True)
    sentinel_path = root / "MUST_NOT_BE_CREATED_BY_DATASET_CODE"
    rows = [
        _raw_row(index, private_variant=private_variant, sentinel_path=sentinel_path)
        for index in range(EXPECTED_RECORD_COUNT)
    ]
    if reverse:
        rows.reverse()
    raw_bytes = b"".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n" for row in rows
    )
    input_path = root / "test.jsonl"
    input_path.write_bytes(raw_bytes)
    source_manifest = {
        "schema_version": 1,
        "dataset_id": "evalplus/humanevalplus",
        "revision": REVISION,
        "split": "test",
        "license": "apache-2.0",
        "record_count": EXPECTED_RECORD_COUNT,
        "raw_files": [
            {
                "path": input_path.name,
                "size": len(raw_bytes),
                "sha256": _sha256(raw_bytes),
            }
        ],
    }
    source_manifest_path = root / "source_manifest.json"
    source_manifest_path.write_text(
        json.dumps(source_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return _Snapshot(
        root=root,
        input_path=input_path,
        source_manifest_path=source_manifest_path,
        raw_bytes=raw_bytes,
        sentinel_path=sentinel_path,
        private_canaries=(
            f"PRIVATE_CANONICAL_CANARY_{private_variant}_",
            f"PRIVATE_TEST_CANARY_{private_variant}_",
            f"PRIVATE_BASE_INPUT_CANARY_{private_variant}_",
            f"PRIVATE_PLUS_INPUT_CANARY_{private_variant}_",
            f"PRIVATE_CONTRACT_CANARY_{private_variant}_",
        ),
    )


@pytest.fixture(scope="module")
def full_bundle(tmp_path_factory) -> _FullBundle:
    root = tmp_path_factory.mktemp("humanevalplus_snapshot")
    snapshot = _write_snapshot(root / "raw", private_variant="A")
    conversion = convert_humanevalplus(
        input_path=snapshot.input_path,
        revision=REVISION,
        source_manifest_path=snapshot.source_manifest_path,
        output_dir=root / "full_bundle",
    )
    assert not snapshot.sentinel_path.exists()
    return _FullBundle(snapshot=snapshot, conversion=conversion)


def _solution(problem: ProblemSpec) -> SolutionTrace:
    return SolutionTrace(
        problem_id=problem.problem_id,
        requirement_understanding="Summarize the public docstring requirement.",
        design_summary="Return a deterministic value using only public inputs.",
        edge_cases_considered=["boundary values"],
        implementation_steps=[
            ImplementationStep(
                step_id="S1",
                content="Implement the function described by the public prompt.",
                related_requirements=["R1"],
                expected_code_behavior="Return the documented result.",
            )
        ],
        declared_time_complexity="O(1)",
        declared_space_complexity="O(1)",
        code=(
            f"def {problem.function_name}(value: int, *, delta: int = 1) -> int:\n"
            "    return value + delta\n"
        ),
    )


class _OfflineProvider:
    name = "offline-humanevalplus-test"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.closed = False

    async def generate_solution_with_details(self, problem: ProblemSpec) -> SolutionGeneration:
        self.calls.append(problem.problem_id)
        solution = _solution(problem)
        return SolutionGeneration(
            status="success",
            raw_output=solution.model_dump_json(),
            solution=solution,
            attempt_count=1,
            raw_output_attempt=1,
            parse_attempted=True,
        )

    def public_generation_config(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "model": "deterministic-offline-test",
            "reasoning_effort": None,
            "reasoning_effort_enabled": False,
            "timeout_seconds": 1,
            "max_retries": 0,
            "endpoint_sha256": None,
        }

    async def aclose(self) -> None:
        self.closed = True


class _FakeClient:
    async def close(self) -> None:
        return None


def _hy3_settings() -> Settings:
    return Settings(
        _env_file=None,
        hy3_base_url="https://hy3.invalid/v1",
        hy3_api_key="unused-unit-test-key",
        hy3_model="unit-test-model",
        hy3_max_retries=1,
    )


def _artifact_texts(*paths: Path) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def test_full_164_projection_is_complete_public_only_and_private_byte_independent(
    full_bundle,
    tmp_path,
):
    conversion = full_bundle.conversion
    problems = load_problems(conversion.dataset_path)

    assert conversion.record_count == EXPECTED_RECORD_COUNT == 164
    assert [problem.problem_id for problem in problems] == [
        f"HumanEval/{index}" for index in range(EXPECTED_RECORD_COUNT)
    ]
    assert all(problem.source == DATASET_SOURCE for problem in problems)
    assert all(problem.difficulty == "unknown" for problem in problems)
    assert all(problem.reference_code == WITHHELD_REFERENCE_CODE for problem in problems)
    assert all(
        not problem.visible_test_cases
        and not problem.hidden_test_cases
        and not problem.challenge_test_cases
        for problem in problems
    )
    first = problems[0]
    assert "PUBLIC_PROMPT_CANARY_0" in first.requirement
    assert "PUBLIC_PROMPT_CANARY_0" in first.requirements[0].content
    assert first.requirements[0].verification_hint is None
    assert first.function_signature.startswith("def task_0(")
    assert first.function_name == "task_0"

    projection_manifest = json.loads(conversion.manifest_path.read_text(encoding="utf-8"))
    assert projection_manifest["public_projection"]["sha256"] == _sha256(
        conversion.dataset_path.read_bytes()
    )
    assert projection_manifest["raw_snapshot"]["test_jsonl_sha256"] == _sha256(
        full_bundle.snapshot.raw_bytes
    )
    assert projection_manifest["adapter"] == {
        "name": ADAPTER_NAME,
        "version": ADAPTER_VERSION,
    }
    assert projection_manifest["license"] == "apache-2.0"
    assert projection_manifest["selection"]["selected_problem_ids"] == [
        f"HumanEval/{index}" for index in range(EXPECTED_RECORD_COUNT)
    ]
    assert not (conversion.output_dir / "evaluation_index.jsonl").exists()

    second_snapshot = _write_snapshot(tmp_path / "raw_b", private_variant="B")
    second_conversion = convert_humanevalplus(
        input_path=second_snapshot.input_path,
        revision=REVISION,
        source_manifest_path=second_snapshot.source_manifest_path,
        output_dir=tmp_path / "full_bundle_b",
    )
    assert conversion.dataset_path.read_bytes() == second_conversion.dataset_path.read_bytes()
    assert conversion.dataset_sha256 == second_conversion.dataset_sha256

    published = _artifact_texts(
        conversion.dataset_path,
        conversion.manifest_path,
        second_conversion.dataset_path,
        second_conversion.manifest_path,
    )
    for canary in (*full_bundle.snapshot.private_canaries, *second_snapshot.private_canaries):
        assert canary not in published


def test_deterministic_ten_problem_sample_has_a_stable_public_id_golden(full_bundle, tmp_path):
    first = sample_humanevalplus(
        dataset_path=full_bundle.conversion.dataset_path,
        source_manifest_path=full_bundle.conversion.manifest_path,
        count=10,
        seed=SEED,
        output_dir=tmp_path / "sample_a",
    )
    second = sample_humanevalplus(
        dataset_path=full_bundle.conversion.dataset_path,
        source_manifest_path=full_bundle.conversion.manifest_path,
        count=10,
        seed=SEED,
        output_dir=tmp_path / "sample_b",
    )

    assert first.selected_problem_ids == second.selected_problem_ids == EXPECTED_TEN_IDS
    assert first.dataset_path.read_bytes() == second.dataset_path.read_bytes()
    assert first.dataset_sha256 == second.dataset_sha256
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert manifest["experiment_label"] == PILOT_EXPERIMENT_LABEL
    assert manifest["selection"] == {
        "algorithm": SELECTION_ALGORITHM,
        "seed": SEED,
        "count": 10,
        "selected_problem_ids": list(EXPECTED_TEN_IDS),
    }
    assert manifest["parent_manifest_sha256"] == _sha256(
        full_bundle.conversion.manifest_path.read_bytes()
    )
    assert [problem.problem_id for problem in load_problems(first.dataset_path)] == list(
        EXPECTED_TEN_IDS
    )

    credential_canary = "RAW_SNAPSHOT_AUTHORIZATION_CANARY_84e137"
    forged_parent_payload = json.loads(full_bundle.conversion.manifest_path.read_text("utf-8"))
    forged_parent_payload["raw_snapshot"]["Authorization"] = f"Bearer {credential_canary}"
    forged_parent = tmp_path / "forged_parent_manifest.json"
    forged_parent.write_text(
        json.dumps(forged_parent_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    forged_output = tmp_path / "must_not_publish_sensitive_parent"
    with pytest.raises(DatasetError, match="raw snapshot fields") as caught:
        sample_humanevalplus(
            dataset_path=full_bundle.conversion.dataset_path,
            source_manifest_path=forged_parent,
            count=10,
            seed=SEED,
            output_dir=forged_output,
        )
    assert credential_canary not in str(caught.value)
    assert not forged_output.exists()


def test_malicious_official_test_and_canonical_solution_are_never_executed(
    full_bundle,
    tmp_path,
    monkeypatch,
):
    calls: list[str] = []

    def forbidden(*args, **kwargs):
        calls.append("forbidden")
        raise AssertionError("HumanEval+ ingestion executed dataset-controlled code")

    monkeypatch.setattr(builtins, "exec", forbidden)
    monkeypatch.setattr(builtins, "eval", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(importlib, "import_module", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)

    result = convert_humanevalplus(
        input_path=full_bundle.snapshot.input_path,
        revision=REVISION,
        source_manifest_path=full_bundle.snapshot.source_manifest_path,
        output_dir=tmp_path / "safe_conversion",
    )

    assert result.record_count == EXPECTED_RECORD_COUNT
    assert calls == []
    assert not full_bundle.snapshot.sentinel_path.exists()


async def test_initial_and_repair_solver_messages_from_projection_exclude_private_canaries(
    full_bundle,
    monkeypatch,
):
    problem = load_problems(full_bundle.conversion.dataset_path)[0]
    monkeypatch.setattr(
        "tracejudge_hy3.providers.hy3_openai.openai.AsyncOpenAI",
        lambda **kwargs: _FakeClient(),
    )
    provider = Hy3OpenAIProvider(_hy3_settings())
    provider._call_model = AsyncMock(side_effect=["not JSON", _solution(problem).model_dump_json()])

    generation = await provider.generate_solution_with_details(problem)
    await provider.aclose()

    assert generation.status == "success"
    assert generation.attempt_count == 2
    all_messages = [
        message for call in provider._call_model.await_args_list for message in call.args[0]
    ]
    serialized = json.dumps(all_messages, ensure_ascii=False)
    assert "PUBLIC_PROMPT_CANARY_0" in serialized
    for canary in full_bundle.snapshot.private_canaries:
        assert canary not in serialized
    for forbidden_field in (
        "canonical_solution",
        "base_input",
        "plus_input",
        "official_test",
        "human_annotation",
    ):
        assert forbidden_field not in serialized
    repair_messages = provider._call_model.await_args_list[1].args[0]
    assert repair_messages[-2] == {"role": "assistant", "content": "not JSON"}
    assert "公开上下文校验" in repair_messages[-1]["content"]


async def test_baseline_manifest_label_and_resume_are_bound_to_dataset_provenance(
    full_bundle,
    tmp_path,
):
    sample = sample_humanevalplus(
        dataset_path=full_bundle.conversion.dataset_path,
        source_manifest_path=full_bundle.conversion.manifest_path,
        count=10,
        seed=SEED,
        output_dir=tmp_path / "pilot",
    )
    run_id = "humanevalplus_provenance"
    first_provider = _OfflineProvider()
    first = await run_baseline_experiment(
        sample.dataset_path,
        first_provider,
        tmp_path / "runs",
        run_id=run_id,
        dataset_manifest_path=sample.manifest_path,
    )

    assert first_provider.calls == list(EXPECTED_TEN_IDS)
    assert first_provider.closed is True
    assert first.manifest["experiment_label"] == PILOT_EXPERIMENT_LABEL
    assert first.summary["experiment_label"] == PILOT_EXPERIMENT_LABEL
    assert first.manifest["dataset"]["sha256"] == sample.dataset_sha256
    assert first.manifest["dataset"]["sources"] == {DATASET_SOURCE: 10}
    provenance = first.manifest["dataset"]["provenance"]
    assert provenance["manifest_sha256"] == _sha256(sample.manifest_path.read_bytes())
    assert provenance["revision"] == REVISION
    assert provenance["adapter"] == {"name": ADAPTER_NAME, "version": ADAPTER_VERSION}
    assert provenance["selection"] == {
        "algorithm": SELECTION_ALGORITHM,
        "seed": SEED,
        "count": 10,
        "selected_problem_ids": list(EXPECTED_TEN_IDS),
    }
    assert provenance["raw_snapshot"]["record_count"] == EXPECTED_RECORD_COUNT

    artifact_text = _artifact_texts(
        first.manifest_path,
        first.responses_path,
        first.summary_path,
    )
    for canary in full_bundle.snapshot.private_canaries:
        assert canary not in artifact_text
    assert "pass@1" not in artifact_text
    assert "functional_accuracy" not in artifact_text

    resumed_provider = _OfflineProvider()
    resumed = await run_baseline_experiment(
        sample.dataset_path,
        resumed_provider,
        tmp_path / "runs",
        run_id=run_id,
        resume=True,
        dataset_manifest_path=sample.manifest_path,
    )
    assert resumed_provider.calls == []
    assert resumed_provider.closed is True
    resumed_records = [
        json.loads(line) for line in resumed.responses_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["status"] for record in resumed_records[-10:]] == ["skipped"] * 10
    assert resumed.summary["skipped_count"] == 10

    changed_manifest_payload = json.loads(sample.manifest_path.read_text(encoding="utf-8"))
    changed_manifest_payload["revision"] = "f" * 40
    changed_manifest = tmp_path / "changed_dataset_manifest.json"
    changed_manifest.write_text(
        json.dumps(changed_manifest_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    rejected_provider = _OfflineProvider()
    with pytest.raises(BaselineExperimentError, match="dataset provenance manifest differs"):
        await run_baseline_experiment(
            sample.dataset_path,
            rejected_provider,
            tmp_path / "runs",
            run_id=run_id,
            resume=True,
            dataset_manifest_path=changed_manifest,
        )
    assert rejected_provider.calls == []
    assert rejected_provider.closed is True


async def test_humanevalplus_requires_authentic_manifest_before_provider_call(
    full_bundle,
    tmp_path,
):
    sample = sample_humanevalplus(
        dataset_path=full_bundle.conversion.dataset_path,
        source_manifest_path=full_bundle.conversion.manifest_path,
        count=10,
        seed=SEED,
        output_dir=tmp_path / "pilot",
    )

    missing_provider = _OfflineProvider()
    with pytest.raises(BaselineExperimentError, match="require --dataset-manifest"):
        await run_baseline_experiment(
            sample.dataset_path,
            missing_provider,
            tmp_path / "missing_runs",
        )
    assert missing_provider.calls == []
    assert missing_provider.closed is True

    original = json.loads(sample.manifest_path.read_text(encoding="utf-8"))
    mutations = (
        (
            {"dataset_id": "forged/example-dataset"},
            r"HumanEval\+ identity",
        ),
        (
            {"source": "forged_humanevalplus_source"},
            r"HumanEval\+ identity",
        ),
        (
            {
                "adapter": {
                    **original["adapter"],
                    "name": "forged_public_projection_adapter",
                }
            },
            "adapter identity",
        ),
        (
            {
                "selection": {
                    **original["selection"],
                    "algorithm": "forged-selection-v1",
                }
            },
            "selection identity",
        ),
        (
            {
                "public_projection": {
                    **original["public_projection"],
                    "sha256": "0" * 64,
                }
            },
            "public projection SHA256",
        ),
        (
            {"experiment_label": "humanevalplus_999_public_prompt_generation_pilot"},
            "experiment label",
        ),
        (
            {
                "public_projection": {
                    **original["public_projection"],
                    "ordered_problem_ids_sha256": "f" * 64,
                }
            },
            "ordered problem ID hash",
        ),
    )
    for index, (mutation, message) in enumerate(mutations):
        forged_payload = {**original, **mutation}
        forged = tmp_path / f"forged_{index}.json"
        forged.write_text(
            json.dumps(forged_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        provider = _OfflineProvider()
        with pytest.raises(BaselineExperimentError, match=message):
            await run_baseline_experiment(
                sample.dataset_path,
                provider,
                tmp_path / "forged_runs",
                dataset_manifest_path=forged,
            )
        assert provider.calls == []
        assert provider.closed is True

    non_seed_problems = load_problems(full_bundle.conversion.dataset_path)[:10]
    non_seed_bytes = b"".join(
        json.dumps(
            problem.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
        for problem in non_seed_problems
    )
    non_seed_dataset = tmp_path / "non_seed_problems.jsonl"
    non_seed_dataset.write_bytes(non_seed_bytes)
    non_seed_ids = [problem.problem_id for problem in non_seed_problems]
    non_seed_manifest_payload = json.loads(sample.manifest_path.read_text(encoding="utf-8"))
    non_seed_manifest_payload["public_projection"] = {
        **non_seed_manifest_payload["public_projection"],
        "sha256": _sha256(non_seed_bytes),
        "ordered_problem_ids_sha256": ordered_problem_ids_sha256(non_seed_ids),
    }
    non_seed_manifest_payload["selection"] = {
        **non_seed_manifest_payload["selection"],
        "selected_problem_ids": non_seed_ids,
    }
    non_seed_manifest = tmp_path / "non_seed_manifest.json"
    non_seed_manifest.write_text(
        json.dumps(non_seed_manifest_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    non_seed_provider = _OfflineProvider()
    with pytest.raises(BaselineExperimentError, match="deterministic seed"):
        await run_baseline_experiment(
            non_seed_dataset,
            non_seed_provider,
            tmp_path / "non_seed_runs",
            dataset_manifest_path=non_seed_manifest,
        )
    assert non_seed_provider.calls == []
    assert non_seed_provider.closed is True


def test_schema_errors_do_not_echo_private_values_or_keep_validation_cause(tmp_path):
    private_canary = "PRIVATE_CANONICAL_SOLUTION_ERROR_CANARY_91fc0d"
    row = {
        "problem_id": "safe",
        "title": "safe",
        "requirement": "public",
        "function_signature": "def safe():",
        "requirements": [],
        "visible_test_cases": [],
        "hidden_test_cases": [],
        "challenge_test_cases": [],
        "reference_code": "pass",
        "difficulty": "unknown",
        "source": "synthetic",
        "tags": [],
        "canonical_solution": private_canary,
    }
    path = tmp_path / "invalid.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")

    with pytest.raises(DatasetError) as caught:
        load_problems(path)
    assert private_canary not in str(caught.value)
    assert caught.value.__cause__ is None

    result = CliRunner().invoke(app, ["dataset", "validate", "--dataset", str(path)])
    assert result.exit_code == 1
    assert "validation failed" in result.output
    assert private_canary not in result.output
    assert "Traceback" not in result.output


def test_dataset_cli_and_baseline_dataset_manifest_option_are_wired(
    full_bundle,
    tmp_path,
    monkeypatch,
):
    runner = CliRunner()
    full_output = tmp_path / "cli_full"
    converted = runner.invoke(
        app,
        [
            "dataset",
            "convert-humanevalplus",
            "--input",
            str(full_bundle.snapshot.input_path),
            "--revision",
            REVISION,
            "--manifest",
            str(full_bundle.snapshot.source_manifest_path),
            "--output-dir",
            str(full_output),
        ],
    )
    assert converted.exit_code == 0, converted.output
    assert "164" in converted.output
    assert "执行数据集代码" in converted.output
    assert (full_output / "problems.jsonl").exists()
    assert (full_output / "dataset_manifest.json").exists()

    sample_output = tmp_path / "cli_sample"
    sampled = runner.invoke(
        app,
        [
            "dataset",
            "sample",
            "--dataset",
            str(full_output / "problems.jsonl"),
            "--manifest",
            str(full_output / "dataset_manifest.json"),
            "--count",
            "10",
            "--seed",
            str(SEED),
            "--output-dir",
            str(sample_output),
        ],
    )
    assert sampled.exit_code == 0, sampled.output
    assert "HumanEval/120" in sampled.output
    assert "不代表 HumanEval+ 功能分数" in sampled.output

    baseline_help = runner.invoke(app, ["baseline", "--help"])
    assert baseline_help.exit_code == 0
    assert "--dataset-manifest" in baseline_help.output

    forbidden_execution_calls: list[str] = []

    def forbid_execution_boundary(*args, **kwargs):
        forbidden_execution_calls.append("called")
        raise AssertionError("phase-one projection crossed into an execution boundary")

    monkeypatch.setattr("tracejudge_hy3.cli.select_backend", forbid_execution_boundary)
    monkeypatch.setattr("tracejudge_hy3.cli._make_provider", forbid_execution_boundary)

    rejected_run = runner.invoke(
        app,
        [
            "run",
            "--dataset",
            str(sample_output / "problems.jsonl"),
            "--problem-id",
            EXPECTED_TEN_IDS[0],
            "--provider",
            "mock",
        ],
    )
    assert rejected_run.exit_code == 1
    assert "仅支持" in rejected_run.output
    rejected_batch = runner.invoke(
        app,
        [
            "batch",
            "--dataset",
            str(sample_output / "problems.jsonl"),
            "--provider",
            "mock",
            "--output",
            str(tmp_path / "must_not_exist.jsonl"),
        ],
    )
    assert rejected_batch.exit_code == 1
    assert "仅支持" in rejected_batch.output
    assert not (tmp_path / "must_not_exist.jsonl").exists()
    assert forbidden_execution_calls == []

    provider = _OfflineProvider()
    monkeypatch.setattr("tracejudge_hy3.cli._make_provider", lambda provider_name: provider)
    baseline_output = tmp_path / "cli_runs"
    baseline = runner.invoke(
        app,
        [
            "baseline",
            "--dataset",
            str(sample_output / "problems.jsonl"),
            "--dataset-manifest",
            str(sample_output / "dataset_manifest.json"),
            "--provider",
            "mock",
            "--output-dir",
            str(baseline_output),
        ],
    )
    assert baseline.exit_code == 0, baseline.output
    assert provider.calls == list(EXPECTED_TEN_IDS)
    assert provider.closed is True
    run_dirs = [path for path in baseline_output.iterdir() if path.is_dir()]
    assert len(run_dirs) == 1
    manifest = json.loads((run_dirs[0] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["experiment_label"] == PILOT_EXPERIMENT_LABEL
    assert manifest["dataset"]["provenance"]["selection"]["selected_problem_ids"] == list(
        EXPECTED_TEN_IDS
    )


def test_dataset_cli_output_path_oserror_is_reported_without_traceback(full_bundle, tmp_path):
    blocked_parent = tmp_path / "output-parent-is-a-file"
    blocked_parent.write_text("not a directory", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "dataset",
            "convert-humanevalplus",
            "--input",
            str(full_bundle.snapshot.input_path),
            "--revision",
            REVISION,
            "--manifest",
            str(full_bundle.snapshot.source_manifest_path),
            "--output-dir",
            str(blocked_parent / "bundle"),
        ],
    )

    assert result.exit_code == 1
    assert "HumanEval+ 转换失败" in result.output
    assert "Traceback" not in result.output
    assert not (blocked_parent / "bundle").exists()
