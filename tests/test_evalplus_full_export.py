"""Full-corpus export must preserve phase-one bytes and reject altered provenance."""

from __future__ import annotations

import hashlib
import json

import pytest
from test_evalplus_exporter import (
    _json_bytes,
    _set_terminal_failures,
    _upgrade_fixture_to_v2,
    _write_fixture,
)

from tracejudge_hy3.dataset.humanevalplus import FULL_SELECTION_ALGORITHM
from tracejudge_hy3.evalplus.exporter import EvalPlusExportError, load_validated_phase1_export
from tracejudge_hy3.evalplus.runner import MockEvalPlusExecutor, run_evalplus_experiment


@pytest.mark.parametrize("with_skipped", [False, True])
def test_full_164_export_preserves_code_and_source_bytes(tmp_path, with_skipped):
    fixture = _write_fixture(tmp_path, full_snapshot=True, with_skipped=with_skipped)
    _upgrade_fixture_to_v2(fixture)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    exported = load_validated_phase1_export(fixture.run_dir, fixture.dataset_manifest)

    ids = tuple(f"HumanEval/{index}" for index in range(164))
    assert tuple(sample.task_id for sample in exported.samples) == ids
    assert exported.dataset.selected_problem_ids == ids
    assert exported.dataset.selection_role == "full"
    assert exported.dataset.selection_algorithm == FULL_SELECTION_ALGORITHM
    assert exported.dataset.selection_seed is None
    assert exported.dataset.parent_manifest_sha256 is None
    assert len(exported.response_references) == len(exported.task_metadata) == 164
    for sample, reference in zip(exported.samples, exported.response_references, strict=True):
        assert sample.solution == fixture.codes[sample.task_id]
        assert reference.code_sha256 == hashlib.sha256(sample.solution.encode()).hexdigest()
    assert (
        exported.phase1.responses_sha256
        == hashlib.sha256(before[fixture.responses_path]).hexdigest()
    )
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
    assert not (tmp_path / "candidate_must_not_execute").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "tracejudge_dataset_selection"),
        ("schema_version", 2),
        ("parent_manifest_sha256", "a" * 64),
        ("limitations", []),
        ("experiment_label", "humanevalplus_10_public_prompt_generation_pilot"),
        ("selection.seed", None),
        ("selection.algorithm", "sha256(seed\\0problem_id)-lowest-v1"),
        ("selection.count", 163),
        ("selection.selected_problem_ids", [f"HumanEval/{i}" for i in range(163)]),
        ("selection.selected_problem_ids", ["HumanEval/0"] * 164),
        ("selection.selected_problem_ids", [f"HumanEval/{i}" for i in reversed(range(164))]),
        ("selection.selected_problem_ids", [f"HumanEval/{i}" for i in range(1, 165)]),
        ("public_projection.sha256", "0" * 64),
        ("public_projection.ordered_problem_ids_sha256", "0" * 64),
        ("source_manifest_sha256", "0" * 64),
        ("raw_snapshot.test_jsonl_sha256", "0" * 64),
    ],
)
def test_full_manifest_tampering_is_rejected(tmp_path, field, value):
    fixture = _write_fixture(tmp_path, full_snapshot=True)
    payload = json.loads(fixture.dataset_manifest.read_bytes())
    target = payload
    keys = field.split(".")
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value
    fixture.dataset_manifest.write_bytes(_json_bytes(payload))

    with pytest.raises(EvalPlusExportError):
        load_validated_phase1_export(fixture.run_dir, fixture.dataset_manifest)


def test_full_all_policy_rejects_missing_success(tmp_path):
    fixture = _write_fixture(tmp_path, full_snapshot=True)
    _upgrade_fixture_to_v2(fixture)
    _set_terminal_failures(
        fixture, parse_error_ids={"HumanEval/162"}, provider_error_ids={"HumanEval/163"}
    )
    with pytest.raises(EvalPlusExportError):
        load_validated_phase1_export(fixture.run_dir, fixture.dataset_manifest)
    exported = load_validated_phase1_export(
        fixture.run_dir,
        fixture.dataset_manifest,
        selection_policy="phase1-success-only",
        min_success_count=162,
    )
    assert len(exported.samples) == 162
    assert exported.export_selection.excluded_parse_error_count == 1
    assert exported.export_selection.excluded_provider_error_count == 1


def test_full_mock_and_resume_keep_full_scope_without_functional_scores(tmp_path, monkeypatch):
    fixture = _write_fixture(tmp_path, full_snapshot=True, with_skipped=True)
    _upgrade_fixture_to_v2(fixture)
    # Only the output-location guard is bypassed for pytest's temporary directory.
    monkeypatch.setattr(
        "tracejudge_hy3.evalplus.runner._require_non_trackable_run_directory", lambda _: None
    )
    options = dict(
        baseline_run_dir=fixture.run_dir,
        dataset_manifest_path=fixture.dataset_manifest,
        output_dir=tmp_path / "phase2",
        executor=MockEvalPlusExecutor(),
        run_id="phase2_full_mock",
    )
    result = run_evalplus_experiment(**options)
    assert result.manifest["dataset"]["selection_role"] == "full"
    assert result.manifest["metrics_scope"] == "full_164_task_single_sample_generation_to_execution"
    assert result.summary["source_problem_count"] == result.summary["exported_success_count"] == 164
    assert result.summary["mock_not_executed_count"] == 164
    assert result.summary["actual_execution_count"] == 0
    assert result.summary["base_plus_pass_rate"] is None
    assert result.summary["metrics_scope"] == "mock_dry_run_only"
    assert not any("subset_not_full" in item for item in result.summary["limitations"])
    assert "pilot_description" not in result.summary
    samples_bytes = result.samples_path.read_bytes()
    resumed = run_evalplus_experiment(**options, resume=True)
    assert resumed.samples_path.read_bytes() == samples_bytes
    assert not (tmp_path / "candidate_must_not_execute").exists()
