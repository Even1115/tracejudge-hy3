from __future__ import annotations

import hashlib
import json
import stat
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import tracejudge_hy3.evalplus.runner as phase2_runner
from tracejudge_hy3.evalplus.exporter import EvalPlusExportError, serialize_samples_jsonl
from tracejudge_hy3.evalplus.runner import (
    EvalPlusExperimentError,
    ExecutorPreflight,
    ExecutorTaskOutcome,
    MockEvalPlusExecutor,
    run_evalplus_experiment,
)
from tracejudge_hy3.evalplus.schemas import (
    EvalPlusSample,
    HumanEvalPlusDatasetIdentity,
    HumanEvalPlusTaskMetadata,
    Phase1ResponseReference,
    Phase1SourceIdentity,
    ValidatedSampleExport,
)

IDS = ("HumanEval/8", "HumanEval/26")
FAILURE_CANARY = "PRIVATE_EVALPLUS_FAILURE_INPUT_CANARY"
SOLUTION_CANARY = "CANDIDATE_SOURCE_BODY_CANARY"


def _export(*, code_suffix: str = "") -> ValidatedSampleExport:
    samples = tuple(
        EvalPlusSample(
            task_id=problem_id,
            solution=(
                f"def candidate_{index}(value):\n"
                f"    marker = {SOLUTION_CANARY!r}\n"
                f"    return value{code_suffix}\n"
            ),
        )
        for index, problem_id in enumerate(IDS)
    )
    references = tuple(
        Phase1ResponseReference(
            phase1_run_id="phase1_fixture",
            problem_id=sample.task_id,
            invocation_id="a" * 32,
            response_line_number=index + 1,
            response_record_sha256=hashlib.sha256(f"record-{index}".encode()).hexdigest(),
            code_sha256=hashlib.sha256(sample.solution.encode()).hexdigest(),
        )
        for index, sample in enumerate(samples)
    )
    task_metadata = tuple(
        HumanEvalPlusTaskMetadata(
            problem_id=problem_id,
            prompt_sha256=hashlib.sha256(f"prompt-{problem_id}".encode()).hexdigest(),
            entry_point=f"candidate_{index}",
        )
        for index, problem_id in enumerate(IDS)
    )
    sample_bytes = serialize_samples_jsonl(samples)
    return ValidatedSampleExport(
        phase1=Phase1SourceIdentity(
            run_id="phase1_fixture",
            experiment_label="humanevalplus_10_public_prompt_generation_pilot",
            manifest_sha256="1" * 64,
            summary_sha256="2" * 64,
            responses_sha256="3" * 64,
            git_commit="4" * 40,
            git_branch="codex/phase1",
            git_dirty=False,
            provider="hy3",
            model="fixture-model",
        ),
        dataset=HumanEvalPlusDatasetIdentity(
            manifest_sha256="5" * 64,
            dataset_id="evalplus/humanevalplus",
            source="evalplus_humanevalplus",
            revision="6" * 40,
            license="apache-2.0",
            adapter_name="tracejudge_humanevalplus_public_projection",
            adapter_version=1,
            source_manifest_sha256="7" * 64,
            parent_manifest_sha256="8" * 64,
            raw_snapshot_aggregate_sha256="9" * 64,
            raw_test_jsonl_sha256="a" * 64,
            problems_sha256="b" * 64,
            ordered_problem_ids_sha256="c" * 64,
            selection_algorithm="fixture-selection",
            selection_seed=20260824,
            selected_problem_ids=IDS,
        ),
        samples=samples,
        response_references=references,
        task_metadata=task_metadata,
        samples_sha256=hashlib.sha256(sample_bytes).hexdigest(),
    )


def _patch_export(
    monkeypatch,
    exported: ValidatedSampleExport,
    *,
    bypass_output_guard: bool = True,
) -> None:
    monkeypatch.setattr(
        phase2_runner,
        "load_validated_phase1_export",
        lambda *_args, **_kwargs: exported,
    )
    if bypass_output_guard:
        monkeypatch.setattr(
            phase2_runner,
            "_require_non_trackable_run_directory",
            lambda _run_dir: None,
        )


def _official_raw(sample: EvalPlusSample, *, fail: bool) -> dict[str, Any]:
    return {
        "date": "2026-08-24 13:00",
        "hash": "d" * 32,
        "eval": {
            sample.task_id: [
                {
                    "task_id": sample.task_id,
                    "solution": sample.solution,
                    "base_status": "pass",
                    "plus_status": "fail" if fail else "pass",
                    "base_fail_tests": [],
                    "plus_fail_tests": [[FAILURE_CANARY]] if fail else [],
                }
            ]
        },
    }


class _FakeDockerExecutor:
    mode = "docker"

    def __init__(
        self,
        *,
        identity_marker: str = "same",
        runtime_marker: str = "same-runtime",
        ready: bool = True,
    ) -> None:
        self.identity_marker = identity_marker
        self.runtime_marker = runtime_marker
        self.ready = ready
        self.preflight_calls = 0
        self.task_calls: list[str] = []

    def public_identity(self):
        return {
            "name": "fake-pinned-evalplus",
            "image_digest": "sha256:" + "e" * 64,
            "identity_marker": self.identity_marker,
        }

    def preflight(self, *, task_metadata, workspace):
        self.preflight_calls += 1
        assert workspace.is_dir()
        assert [item.problem_id for item in task_metadata] == list(IDS)
        return ExecutorPreflight(
            ready=self.ready,
            runtime={
                "evalplus_version": "0.3.1",
                "evalplus_commit": "e5d0ed0bab96280b60b637ec7f15b5e4841b0cb2",
                "humaneval_plus_version": "v0.1.10",
                "python_version": "3.11.10",
                "runtime_marker": self.runtime_marker,
            },
            infrastructure_error_type=None if self.ready else "docker_unavailable",
            diagnostics={
                "stderr_sha256": hashlib.sha256(b"safe diagnostic").hexdigest(),
                "stderr_bytes": 15,
            },
        )

    def run_task(self, *, sample, task_metadata, workspace):
        assert task_metadata.problem_id == sample.task_id
        assert workspace.is_dir()
        self.task_calls.append(sample.task_id)
        fail = sample.task_id == "HumanEval/26"
        return ExecutorTaskOutcome(
            problem_id=sample.task_id,
            started_at="2026-08-24T13:00:00.000Z",
            ended_at="2026-08-24T13:00:01.000Z",
            duration_seconds=1.0,
            raw_result=_official_raw(sample, fail=fail),
            infrastructure_error_type=None,
            diagnostics={
                "exit_code": 0,
                "stdout_bytes": 50,
                "stdout_sha256": hashlib.sha256(b"entrypoint result").hexdigest(),
                # This unexpected value must never be copied to execution.log.
                "untrusted_text": FAILURE_CANARY,
            },
        )


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_mock_dry_run_creates_private_complete_artifacts_without_execution(tmp_path, monkeypatch):
    exported = _export()
    _patch_export(monkeypatch, exported)

    result = run_evalplus_experiment(
        "unused-phase1",
        "unused-manifest",
        tmp_path / "phase2",
        executor=MockEvalPlusExecutor(),
        run_id="phase2_mock_fixture",
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
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in result.run_dir.iterdir())
    assert result.samples_path.read_bytes() == serialize_samples_jsonl(exported.samples)
    assert result.manifest["input"]["public_task_identity"] == [
        {
            "problem_id": metadata.problem_id,
            "prompt_sha256": metadata.prompt_sha256,
            "entry_point": metadata.entry_point,
        }
        for metadata in exported.task_metadata
    ]
    records = _records(result.results_path)
    assert [record["problem_id"] for record in records] == list(IDS)
    assert all(record["infrastructure_status"] == "mocked" for record in records)
    assert all(record["error_type"] == "mock_not_executed" for record in records)
    assert result.summary["actual_execution_count"] == 0
    assert result.summary["mock_not_executed_count"] == 2
    assert result.summary["infrastructure_error_count"] == 0
    assert result.summary["base_pass_rate"] is None
    assert result.manifest["execution_mode"] == "mock"
    assert result.manifest["output"]["samples_sha256"] == exported.samples_sha256


def test_mock_dry_run_can_resume_without_changing_artifacts_identity(tmp_path, monkeypatch):
    exported = _export()
    _patch_export(monkeypatch, exported)
    output = tmp_path / "phase2"
    run_evalplus_experiment(
        "unused-phase1",
        "unused-manifest",
        output,
        executor=MockEvalPlusExecutor(),
        run_id="phase2_mock_resume",
    )

    resumed = run_evalplus_experiment(
        "unused-phase1",
        "unused-manifest",
        output,
        executor=MockEvalPlusExecutor(),
        run_id="phase2_mock_resume",
        resume=True,
    )

    assert resumed.summary["mock_not_executed_count"] == len(IDS)
    assert resumed.summary["resume_skipped_count"] == len(IDS)


def test_docker_results_are_incremental_sanitized_and_summary_matches(tmp_path, monkeypatch):
    exported = _export()
    _patch_export(monkeypatch, exported)
    executor = _FakeDockerExecutor()

    result = run_evalplus_experiment(
        "unused-phase1",
        "unused-manifest",
        tmp_path / "phase2",
        executor=executor,
        run_id="phase2_docker_fixture",
    )

    assert sorted(executor.task_calls) == sorted(IDS)
    safe_text = result.results_path.read_text(encoding="utf-8")
    summary_text = result.summary_path.read_text(encoding="utf-8")
    manifest_text = result.manifest_path.read_text(encoding="utf-8")
    log_text = result.execution_log_path.read_text(encoding="utf-8")
    for public_text in (safe_text, summary_text, manifest_text, log_text):
        assert FAILURE_CANARY not in public_text
        assert SOLUTION_CANARY not in public_text
    raw_text = result.raw_results_path.read_text(encoding="utf-8")
    assert FAILURE_CANARY in raw_text
    assert SOLUTION_CANARY in raw_text
    assert stat.S_IMODE(result.raw_results_path.stat().st_mode) == 0o600

    records = _records(result.results_path)
    assert records[0]["passed_base"] is True
    assert records[0]["passed_plus"] is True
    assert records[1]["passed_base"] is True
    assert records[1]["passed_plus"] is False
    assert records[1]["plus_fail_test_count"] == 1
    assert result.summary["base_pass_count"] == 2
    assert result.summary["base_plus_pass_count"] == 1
    assert result.summary["wrong_answer_or_candidate_exception_count"] == 1
    assert result.summary["execution_error_count"] is None
    assert result.summary["infrastructure_error_count"] == 0


def test_static_export_failure_happens_before_executor_preflight(tmp_path, monkeypatch):
    executor = _FakeDockerExecutor()

    def reject(*_args, **_kwargs):
        raise EvalPlusExportError("static provenance mismatch")

    monkeypatch.setattr(phase2_runner, "load_validated_phase1_export", reject)
    with pytest.raises(EvalPlusExportError, match="provenance"):
        run_evalplus_experiment(
            "bad-phase1",
            "bad-manifest",
            tmp_path / "phase2",
            executor=executor,
        )
    assert executor.preflight_calls == 0
    assert not (tmp_path / "phase2").exists()


def test_repository_local_output_must_be_git_ignored_before_preflight(monkeypatch):
    exported = _export()
    _patch_export(monkeypatch, exported, bypass_output_guard=False)
    repository = Path(phase2_runner.__file__).resolve().parents[3]
    output = repository / "phase2-unignored-unit-output"
    assert not output.exists()
    executor = _FakeDockerExecutor()

    with pytest.raises(EvalPlusExperimentError, match="covered by .gitignore"):
        run_evalplus_experiment(
            "unused",
            "unused",
            output,
            executor=executor,
            run_id="phase2_unignored_fixture",
        )

    assert executor.preflight_calls == 0
    assert not output.exists()


def test_output_outside_repository_is_rejected_before_preflight(tmp_path, monkeypatch):
    exported = _export()
    _patch_export(monkeypatch, exported, bypass_output_guard=False)
    executor = _FakeDockerExecutor()

    with pytest.raises(EvalPlusExperimentError, match="inside this repository"):
        run_evalplus_experiment(
            "unused",
            "unused",
            tmp_path / "phase2",
            executor=executor,
            run_id="phase2_external_output",
        )

    assert executor.preflight_calls == 0


def test_preflight_failure_is_infrastructure_not_candidate_failure(tmp_path, monkeypatch):
    exported = _export()
    _patch_export(monkeypatch, exported)
    executor = _FakeDockerExecutor(ready=False)

    result = run_evalplus_experiment(
        "unused",
        "unused",
        tmp_path / "phase2",
        executor=executor,
        run_id="phase2_preflight_failure",
    )

    assert executor.task_calls == []
    records = _records(result.results_path)
    assert all(record["base_status"] is None for record in records)
    assert all(record["plus_status"] is None for record in records)
    assert all(record["error_type"] == "docker_unavailable" for record in records)
    assert result.summary["infrastructure_error_count"] == 2
    assert result.summary["wrong_answer_or_candidate_exception_count"] == 0
    assert result.summary["timeout_count"] == 0


def test_normal_batch_retries_retained_cleanup_targets_without_rewriting_outcome(
    tmp_path,
    monkeypatch,
):
    exported = _export()
    _patch_export(monkeypatch, exported)

    class RetainedCleanupExecutor(_FakeDockerExecutor):
        def __init__(self):
            super().__init__()
            self.cancel_calls = 0

        def run_task(self, *, sample, task_metadata, workspace):
            del task_metadata, workspace
            self.task_calls.append(sample.task_id)
            return ExecutorTaskOutcome(
                problem_id=sample.task_id,
                started_at="2026-08-24T13:00:00.000Z",
                ended_at="2026-08-24T13:00:01.000Z",
                duration_seconds=1.0,
                raw_result=None,
                infrastructure_error_type="container_cleanup_failed",
                diagnostics={"cleanup_status": "failed"},
            )

        def cancel_all(self):
            self.cancel_calls += 1
            return {"retained-container": "removed"}

    executor = RetainedCleanupExecutor()
    result = run_evalplus_experiment(
        "unused",
        "unused",
        tmp_path / "phase2",
        executor=executor,
        run_id="phase2_final_cleanup_retry",
    )

    assert executor.cancel_calls == 1
    assert all(
        record["error_type"] == "container_cleanup_failed"
        for record in _records(result.results_path)
    )
    assert result.summary["container_cleanup_failed_count"] == len(IDS)


def test_preflight_interruption_leaves_manifest_and_can_resume(tmp_path, monkeypatch):
    exported = _export()
    _patch_export(monkeypatch, exported)
    output = tmp_path / "phase2"

    class InterruptingPreflight(_FakeDockerExecutor):
        def preflight(self, *, task_metadata, workspace):
            del task_metadata, workspace
            self.preflight_calls += 1
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_evalplus_experiment(
            "unused",
            "unused",
            output,
            executor=InterruptingPreflight(),
            run_id="phase2_preflight_interrupted",
        )

    run_dir = output / "phase2_preflight_interrupted"
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "running"
    assert manifest["preflight"]["status"] == "pending"
    assert (run_dir / "samples.jsonl").is_file()

    resumed_executor = _FakeDockerExecutor()
    resumed = run_evalplus_experiment(
        "unused",
        "unused",
        output,
        executor=resumed_executor,
        run_id="phase2_preflight_interrupted",
        resume=True,
    )
    assert resumed.manifest["status"] == "completed"
    assert resumed.manifest["preflight"]["status"] == "ready"
    assert set(resumed_executor.task_calls) == set(IDS)


def test_repeated_interrupted_resume_cannot_bypass_runtime_fingerprint(tmp_path, monkeypatch):
    exported = _export()
    _patch_export(monkeypatch, exported)
    output = tmp_path / "phase2"
    run_evalplus_experiment(
        "unused",
        "unused",
        output,
        executor=_FakeDockerExecutor(runtime_marker="trusted-runtime"),
        run_id="phase2_runtime_resume_guard",
    )

    class InterruptingResumePreflight(_FakeDockerExecutor):
        def preflight(self, *, task_metadata, workspace):
            del task_metadata, workspace
            self.preflight_calls += 1
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_evalplus_experiment(
            "unused",
            "unused",
            output,
            executor=InterruptingResumePreflight(runtime_marker="trusted-runtime"),
            run_id="phase2_runtime_resume_guard",
            resume=True,
        )

    changed = _FakeDockerExecutor(runtime_marker="changed-runtime")
    with pytest.raises(EvalPlusExperimentError, match="provenance, code, EvalPlus, image"):
        run_evalplus_experiment(
            "unused",
            "unused",
            output,
            executor=changed,
            run_id="phase2_runtime_resume_guard",
            resume=True,
        )
    assert changed.preflight_calls == 1


def test_resume_skips_completed_tasks_and_rejects_changed_identity(tmp_path, monkeypatch):
    exported = _export()
    _patch_export(monkeypatch, exported)
    output = tmp_path / "phase2"
    first_executor = _FakeDockerExecutor()
    run_evalplus_experiment(
        "unused",
        "unused",
        output,
        executor=first_executor,
        run_id="phase2_resume_fixture",
    )

    resumed_executor = _FakeDockerExecutor()
    resumed = run_evalplus_experiment(
        "unused",
        "unused",
        output,
        executor=resumed_executor,
        run_id="phase2_resume_fixture",
        resume=True,
    )
    assert resumed_executor.task_calls == []
    assert len(resumed.manifest["invocations"]) == 2
    assert resumed.manifest["invocations"][-1]["resume"] is True
    assert resumed.summary["resume_skipped_count"] == len(IDS)
    assert resumed.summary["current_invocation_official_result_count"] == 0
    resume_events = _records(resumed.execution_log_path)
    assert sum(event["event"] == "task_reused_on_resume" for event in resume_events) == len(IDS)

    with pytest.raises(EvalPlusExperimentError, match="provenance, code, EvalPlus, image"):
        run_evalplus_experiment(
            "unused",
            "unused",
            output,
            executor=_FakeDockerExecutor(identity_marker="changed-image"),
            run_id="phase2_resume_fixture",
            resume=True,
        )


def test_resume_repairs_cross_file_checkpoint_interruption_by_rerunning_missing_raw(
    tmp_path, monkeypatch
):
    exported = _export()
    _patch_export(monkeypatch, exported)
    output = tmp_path / "phase2"
    original_write_json = phase2_runner._atomic_write_json
    raw_write_count = 0

    def interrupt_between_result_and_raw(path, value):
        nonlocal raw_write_count
        if path.name == "evalplus_raw_results.json":
            raw_write_count += 1
            if raw_write_count == 2:
                raise KeyboardInterrupt
        original_write_json(path, value)

    monkeypatch.setattr(
        phase2_runner,
        "_atomic_write_json",
        interrupt_between_result_and_raw,
    )
    with pytest.raises(KeyboardInterrupt):
        run_evalplus_experiment(
            "unused",
            "unused",
            output,
            executor=_FakeDockerExecutor(),
            run_id="phase2_partial_raw_checkpoint",
            max_workers=1,
        )

    run_dir = output / "phase2_partial_raw_checkpoint"
    assert len(_records(run_dir / "results.jsonl")) == len(IDS)
    partial_raw = json.loads((run_dir / "evalplus_raw_results.json").read_text(encoding="utf-8"))
    assert len(partial_raw["raw_results"]) == 1
    assert (
        json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))["status"] == "running"
    )

    resumed_executor = _FakeDockerExecutor()
    resumed = run_evalplus_experiment(
        "unused",
        "unused",
        output,
        executor=resumed_executor,
        run_id="phase2_partial_raw_checkpoint",
        resume=True,
        max_workers=1,
    )

    assert resumed_executor.task_calls == [IDS[1]]
    repaired_raw = json.loads(resumed.raw_results_path.read_text(encoding="utf-8"))
    assert len(repaired_raw["raw_results"]) == len(IDS)
    assert len(_records(resumed.results_path)) == len(IDS)


def test_resume_rejects_safe_result_that_disagrees_with_raw_before_preflight(tmp_path, monkeypatch):
    exported = _export()
    _patch_export(monkeypatch, exported)
    output = tmp_path / "phase2"
    first = run_evalplus_experiment(
        "unused",
        "unused",
        output,
        executor=_FakeDockerExecutor(),
        run_id="phase2_raw_safe_mismatch",
    )
    records = _records(first.results_path)
    records[0]["plus_status"] = "fail"
    first.results_path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "running"
    manifest["completed_at"] = None
    manifest["output"] = None
    manifest["invocations"][-1]["status"] = "running"
    manifest["invocations"][-1]["completed_at"] = None
    first.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    executor = _FakeDockerExecutor()

    with pytest.raises(EvalPlusExperimentError, match="differs from its official raw"):
        run_evalplus_experiment(
            "unused",
            "unused",
            output,
            executor=executor,
            run_id="phase2_raw_safe_mismatch",
            resume=True,
        )
    assert executor.preflight_calls == 0


def test_resume_rejects_unknown_manifest_field_without_echoing_it(tmp_path, monkeypatch):
    exported = _export()
    _patch_export(monkeypatch, exported)
    output = tmp_path / "phase2"
    first = run_evalplus_experiment(
        "unused",
        "unused",
        output,
        executor=_FakeDockerExecutor(),
        run_id="phase2_manifest_extra",
    )
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    manifest["PRIVATE_MANIFEST_CANARY"] = "PRIVATE_MANIFEST_CANARY"
    first.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    executor = _FakeDockerExecutor()

    with pytest.raises(EvalPlusExperimentError, match="manifest schema") as caught:
        run_evalplus_experiment(
            "unused",
            "unused",
            output,
            executor=executor,
            run_id="phase2_manifest_extra",
            resume=True,
        )
    assert "PRIVATE_MANIFEST_CANARY" not in str(caught.value)
    assert executor.preflight_calls == 0


@pytest.mark.parametrize("ambiguous_prefix", ['{"schema_version":1,', '{"x":NaN,'])
def test_resume_rejects_duplicate_keys_and_nonfinite_json(
    tmp_path,
    monkeypatch,
    ambiguous_prefix,
):
    exported = _export()
    _patch_export(monkeypatch, exported)
    output = tmp_path / "phase2"
    first = run_evalplus_experiment(
        "unused",
        "unused",
        output,
        executor=_FakeDockerExecutor(),
        run_id="phase2_ambiguous_manifest",
    )
    original = first.manifest_path.read_text(encoding="utf-8")
    first.manifest_path.write_text(ambiguous_prefix + original[1:], encoding="utf-8")

    with pytest.raises(EvalPlusExperimentError, match="not valid UTF-8 JSON"):
        run_evalplus_experiment(
            "unused",
            "unused",
            output,
            executor=_FakeDockerExecutor(),
            run_id="phase2_ambiguous_manifest",
            resume=True,
        )


def test_empty_bootstrap_directory_can_resume_after_early_crash(tmp_path, monkeypatch):
    exported = _export()
    _patch_export(monkeypatch, exported)
    output = tmp_path / "phase2"
    run_dir = output / "phase2_bootstrap_resume"
    run_dir.mkdir(parents=True, mode=0o700)

    result = run_evalplus_experiment(
        "unused",
        "unused",
        output,
        executor=_FakeDockerExecutor(),
        run_id="phase2_bootstrap_resume",
        resume=True,
    )

    assert result.manifest["status"] == "completed"
    assert result.manifest["invocations"][0]["resume"] is True
    assert result.samples_path.read_bytes() == serialize_samples_jsonl(exported.samples)


def test_resume_rejects_tampered_completed_output_before_preflight(tmp_path, monkeypatch):
    exported = _export()
    _patch_export(monkeypatch, exported)
    first = run_evalplus_experiment(
        "unused",
        "unused",
        tmp_path / "phase2",
        executor=_FakeDockerExecutor(),
        run_id="phase2_completed_tamper",
    )
    first.results_path.write_bytes(first.results_path.read_bytes() + b"\n")
    executor = _FakeDockerExecutor()

    with pytest.raises(EvalPlusExperimentError, match="output hash validation"):
        run_evalplus_experiment(
            "unused",
            "unused",
            tmp_path / "phase2",
            executor=executor,
            run_id="phase2_completed_tamper",
            resume=True,
        )
    assert executor.preflight_calls == 0


def test_resume_rejects_unknown_running_result_field_without_echoing_it(tmp_path, monkeypatch):
    exported = _export()
    _patch_export(monkeypatch, exported)
    output = tmp_path / "phase2"
    first = run_evalplus_experiment(
        "unused",
        "unused",
        output,
        executor=_FakeDockerExecutor(),
        run_id="phase2_result_extra",
    )
    records = _records(first.results_path)
    records[0]["PRIVATE_RESULT_CANARY"] = "PRIVATE_RESULT_CANARY"
    first.results_path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "running"
    manifest["completed_at"] = None
    manifest["output"] = None
    manifest["invocations"][-1]["status"] = "running"
    manifest["invocations"][-1]["completed_at"] = None
    first.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    executor = _FakeDockerExecutor()

    with pytest.raises(EvalPlusExperimentError, match="result schema") as caught:
        run_evalplus_experiment(
            "unused",
            "unused",
            output,
            executor=executor,
            run_id="phase2_result_extra",
            resume=True,
        )
    assert "PRIVATE_RESULT_CANARY" not in str(caught.value)
    assert executor.preflight_calls == 0


def test_resume_rejects_changed_candidate_code(tmp_path, monkeypatch):
    original = _export()
    _patch_export(monkeypatch, original)
    output = tmp_path / "phase2"
    run_evalplus_experiment(
        "unused",
        "unused",
        output,
        executor=_FakeDockerExecutor(),
        run_id="phase2_changed_code",
    )

    changed = _export(code_suffix=" + 1")
    assert changed.samples_sha256 != original.samples_sha256
    _patch_export(monkeypatch, changed)
    changed_executor = _FakeDockerExecutor()
    with pytest.raises(EvalPlusExperimentError, match="samples.jsonl differs"):
        run_evalplus_experiment(
            "unused",
            "unused",
            output,
            executor=changed_executor,
            run_id="phase2_changed_code",
            resume=True,
        )
    assert changed_executor.preflight_calls == 0


def test_resume_rejects_changed_dataset_or_phase1_provenance(tmp_path, monkeypatch):
    original = _export()
    _patch_export(monkeypatch, original)
    output = tmp_path / "phase2"
    run_evalplus_experiment(
        "unused",
        "unused",
        output,
        executor=_FakeDockerExecutor(),
        run_id="phase2_changed_provenance",
    )

    changed_dataset = replace(original.dataset, revision="f" * 40)
    changed = replace(original, dataset=changed_dataset)
    _patch_export(monkeypatch, changed)
    changed_executor = _FakeDockerExecutor()
    with pytest.raises(EvalPlusExperimentError, match="provenance"):
        run_evalplus_experiment(
            "unused",
            "unused",
            output,
            executor=changed_executor,
            run_id="phase2_changed_provenance",
            resume=True,
        )
    assert changed_executor.preflight_calls == 0


def test_interruption_leaves_readable_incremental_jsonl_and_resume_completes(tmp_path, monkeypatch):
    exported = _export()
    _patch_export(monkeypatch, exported)
    first_checkpoint = threading.Event()
    original_write_checkpoints = phase2_runner._write_checkpoints

    def observe_checkpoint(*args, **kwargs):
        original_write_checkpoints(*args, **kwargs)
        first_checkpoint.set()

    monkeypatch.setattr(phase2_runner, "_write_checkpoints", observe_checkpoint)

    class InterruptingExecutor(_FakeDockerExecutor):
        def __init__(self):
            super().__init__()
            self.cancel_calls = 0

        def cancel_all(self):
            self.cancel_calls += 1

        def run_task(self, *, sample, task_metadata, workspace):
            if sample.task_id == IDS[0]:
                return super().run_task(
                    sample=sample,
                    task_metadata=task_metadata,
                    workspace=workspace,
                )
            assert first_checkpoint.wait(timeout=5)
            raise KeyboardInterrupt

    output = tmp_path / "phase2"
    interrupting_executor = InterruptingExecutor()
    with pytest.raises(KeyboardInterrupt):
        run_evalplus_experiment(
            "unused",
            "unused",
            output,
            executor=interrupting_executor,
            run_id="phase2_interrupted_fixture",
            max_workers=1,
        )
    assert interrupting_executor.cancel_calls == 2

    run_dir = output / "phase2_interrupted_fixture"
    partial = _records(run_dir / "results.jsonl")
    assert [record["problem_id"] for record in partial] == [IDS[0]]
    assert json.loads((run_dir / "evalplus_raw_results.json").read_text(encoding="utf-8"))
    assert json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))["status"] == (
        "running"
    )

    unavailable_executor = _FakeDockerExecutor(ready=False)
    unavailable = run_evalplus_experiment(
        "unused",
        "unused",
        output,
        executor=unavailable_executor,
        run_id="phase2_interrupted_fixture",
        resume=True,
        max_workers=1,
    )
    unavailable_records = _records(unavailable.results_path)
    assert unavailable_records[0]["infrastructure_status"] == "ok"
    assert unavailable_records[1]["error_type"] == "docker_unavailable"
    assert len(json.loads(unavailable.raw_results_path.read_text())["raw_results"]) == 1

    resumed_executor = _FakeDockerExecutor()
    resumed = run_evalplus_experiment(
        "unused",
        "unused",
        output,
        executor=resumed_executor,
        run_id="phase2_interrupted_fixture",
        resume=True,
        max_workers=1,
    )
    assert resumed_executor.task_calls == [IDS[1]]
    assert len(_records(resumed.results_path)) == len(IDS)
    assert resumed.manifest["invocations"][0]["status"] == "interrupted"


def test_batch_deadline_cancels_active_work_and_distinguishes_not_started_tasks(
    tmp_path, monkeypatch
):
    exported = _export()
    _patch_export(monkeypatch, exported)
    started = threading.Event()
    cancel_requested = threading.Event()
    allow_worker_exit = threading.Event()

    class CancellableExecutor(_FakeDockerExecutor):
        def __init__(self):
            super().__init__()
            self.cancel_calls = 0

        def run_task(self, *, sample, task_metadata, workspace):
            del task_metadata, workspace
            self.task_calls.append(sample.task_id)
            started.set()
            assert cancel_requested.wait(timeout=5)
            assert allow_worker_exit.wait(timeout=5)
            return super().run_task(
                sample=sample,
                task_metadata=HumanEvalPlusTaskMetadata(
                    problem_id=sample.task_id,
                    prompt_sha256="a" * 64,
                    entry_point="candidate",
                ),
                workspace=tmp_path,
            )

        def cancel_all(self):
            self.cancel_calls += 1
            cancel_requested.set()
            return {"active-container": "removed"}

    executor = CancellableExecutor()

    def immediate_deadline(_futures, *, timeout):
        del timeout
        assert started.wait(timeout=5)
        raise TimeoutError

    monkeypatch.setattr(phase2_runner, "as_completed", immediate_deadline)
    monkeypatch.setattr(phase2_runner, "_BATCH_CLEANUP_GRACE_SECONDS", 0.05)
    try:
        result = run_evalplus_experiment(
            "unused",
            "unused",
            tmp_path / "phase2",
            executor=executor,
            run_id="phase2_batch_deadline",
            max_workers=1,
            per_task_timeout_seconds=1,
            batch_timeout_seconds=1,
        )
    finally:
        allow_worker_exit.set()

    records = _records(result.results_path)
    assert len(records) == len(IDS)
    assert all(record["infrastructure_status"] == "error" for record in records)
    assert [record["error_type"] for record in records] == [
        "container_cleanup_failed",
        "batch_deadline_not_started",
    ]
    assert records[0]["duration_seconds"] is None
    assert records[1]["duration_seconds"] == 0.0
    assert executor.cancel_calls == 2
    assert result.summary["infrastructure_error_count"] == len(IDS)
    assert result.summary["batch_timeout_count"] == 0
    assert result.summary["batch_deadline_not_started_count"] == 1
    assert result.summary["container_cleanup_failed_count"] == 1
    assert result.summary["actual_execution_count"] == 0
    assert result.manifest["execution_config"]["batch_cleanup_grace_seconds"] == 0.05


def test_batch_timeout_resume_preserves_raw_and_only_reruns_unfinished_task(
    tmp_path,
    monkeypatch,
):
    exported = _export()
    _patch_export(monkeypatch, exported)
    second_started = threading.Event()
    cancel_requested = threading.Event()
    allow_worker_exit = threading.Event()
    original_as_completed = phase2_runner.as_completed

    class PartiallyBlockingExecutor(_FakeDockerExecutor):
        def run_task(self, *, sample, task_metadata, workspace):
            if sample.task_id == IDS[0]:
                return super().run_task(
                    sample=sample,
                    task_metadata=task_metadata,
                    workspace=workspace,
                )
            second_started.set()
            assert cancel_requested.wait(timeout=5)
            assert allow_worker_exit.wait(timeout=5)
            return super().run_task(
                sample=sample,
                task_metadata=task_metadata,
                workspace=workspace,
            )

        def cancel_all(self):
            cancel_requested.set()
            return {"active-container": "removed"}

    def one_result_then_deadline(futures, *, timeout):
        ordered = list(futures)
        ordered[0].result(timeout=timeout)
        yield ordered[0]
        assert second_started.wait(timeout=5)
        raise TimeoutError

    monkeypatch.setattr(phase2_runner, "as_completed", one_result_then_deadline)
    monkeypatch.setattr(phase2_runner, "_BATCH_CLEANUP_GRACE_SECONDS", 0.05)
    output = tmp_path / "phase2"
    try:
        partial = run_evalplus_experiment(
            "unused",
            "unused",
            output,
            executor=PartiallyBlockingExecutor(),
            run_id="phase2_batch_resume",
            max_workers=1,
            per_task_timeout_seconds=1,
            batch_timeout_seconds=1,
        )
    finally:
        allow_worker_exit.set()

    partial_records = _records(partial.results_path)
    assert partial_records[0]["infrastructure_status"] == "ok"
    assert partial_records[1]["error_type"] == "container_cleanup_failed"
    assert len(json.loads(partial.raw_results_path.read_text())["raw_results"]) == 1

    monkeypatch.setattr(phase2_runner, "as_completed", original_as_completed)
    resumed_executor = _FakeDockerExecutor()
    resumed = run_evalplus_experiment(
        "unused",
        "unused",
        output,
        executor=resumed_executor,
        run_id="phase2_batch_resume",
        resume=True,
        max_workers=1,
        per_task_timeout_seconds=1,
        batch_timeout_seconds=1,
    )
    assert resumed_executor.task_calls == [IDS[1]]
    assert len(json.loads(resumed.raw_results_path.read_text())["raw_results"]) == len(IDS)
    assert resumed.summary["resume_skipped_count"] == 1
    assert resumed.summary["current_invocation_official_result_count"] == 1


def test_batch_cleanup_grace_records_real_outcome_when_worker_exits(tmp_path, monkeypatch):
    exported = _export()
    _patch_export(monkeypatch, exported)
    started = threading.Event()
    release = threading.Event()

    class ExitingExecutor(_FakeDockerExecutor):
        def run_task(self, *, sample, task_metadata, workspace):
            started.set()
            assert release.wait(timeout=5)
            return super().run_task(
                sample=sample,
                task_metadata=task_metadata,
                workspace=workspace,
            )

        def cancel_all(self):
            release.set()
            return {"active-container": "removed"}

    def immediate_deadline(_futures, *, timeout):
        del timeout
        assert started.wait(timeout=5)
        raise TimeoutError

    monkeypatch.setattr(phase2_runner, "as_completed", immediate_deadline)
    monkeypatch.setattr(phase2_runner, "_BATCH_CLEANUP_GRACE_SECONDS", 0.5)

    result = run_evalplus_experiment(
        "unused",
        "unused",
        tmp_path / "phase2",
        executor=ExitingExecutor(),
        run_id="phase2_cleanup_worker_exit",
        max_workers=1,
        per_task_timeout_seconds=1,
        batch_timeout_seconds=1,
    )

    records = _records(result.results_path)
    assert records[0]["infrastructure_status"] == "ok"
    assert records[1]["error_type"] == "batch_deadline_not_started"
    assert result.summary["actual_execution_count"] == 1
    assert result.summary["container_cleanup_failed_count"] == 0


def test_batch_cleanup_failed_overrides_worker_outcome(tmp_path, monkeypatch):
    exported = _export()
    _patch_export(monkeypatch, exported)
    started = threading.Event()
    release = threading.Event()

    class FailedCleanupExecutor(_FakeDockerExecutor):
        def run_task(self, *, sample, task_metadata, workspace):
            started.set()
            assert release.wait(timeout=5)
            return super().run_task(
                sample=sample,
                task_metadata=task_metadata,
                workspace=workspace,
            )

        def cancel_all(self):
            release.set()
            return {"active-container": "failed"}

    def immediate_deadline(_futures, *, timeout):
        del timeout
        assert started.wait(timeout=5)
        raise TimeoutError

    monkeypatch.setattr(phase2_runner, "as_completed", immediate_deadline)
    monkeypatch.setattr(phase2_runner, "_BATCH_CLEANUP_GRACE_SECONDS", 0.5)

    result = run_evalplus_experiment(
        "unused",
        "unused",
        tmp_path / "phase2",
        executor=FailedCleanupExecutor(),
        run_id="phase2_cleanup_failed",
        max_workers=1,
        per_task_timeout_seconds=1,
        batch_timeout_seconds=1,
    )

    records = _records(result.results_path)
    assert records[0]["error_type"] == "container_cleanup_failed"
    assert records[1]["error_type"] == "batch_deadline_not_started"
    assert result.summary["container_cleanup_failed_count"] == 1
    events = _records(result.execution_log_path)
    cleanup_event = next(item for item in events if item["event"] == "container_cleanup_failed")
    assert cleanup_event["cleanup_status"] == "failed"
