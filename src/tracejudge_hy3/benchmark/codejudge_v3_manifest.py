"""Reproducibility manifest and completion receipt for v3-A/v3-B runs."""

from __future__ import annotations

import platform
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from .codejudge_eval import CodeJudgeEvalError, CodeJudgeEvalSample, file_sha256
from .codejudge_v3 import CodeJudgeV3Selection
from .codejudge_v3_execution import PlannedV3Call, call_plan_sha256
from .codejudge_v3_metrics import (
    A_BOOTSTRAP_SEED,
    B_BOOTSTRAP_SEED,
    B_FAILURE_RATE_LIMIT,
    BOOTSTRAP_RESAMPLES,
)
from .contracts import GitCommit, Sha256, StrictFrozenModel, canonical_sha256
from .judge_only_prompt_v4 import (
    JUDGE_ONLY_PROMPT_V4_BUNDLE_VERSION,
    judge_only_prompt_v4_bundle_sha256,
)

V3_PROTOCOL_PATH = "docs/codejudge_eval_v3_protocol.md"

V3_RUN_SOURCE_FILES: tuple[str, ...] = (
    "src/tracejudge_hy3/config.py",
    "src/tracejudge_hy3/exceptions.py",
    "src/tracejudge_hy3/logging_config.py",
    "src/tracejudge_hy3/schemas/evaluation.py",
    "src/tracejudge_hy3/benchmark/contracts.py",
    "src/tracejudge_hy3/benchmark/codejudge_eval.py",
    "src/tracejudge_hy3/benchmark/codejudge_v3.py",
    "src/tracejudge_hy3/benchmark/judge_only_prompt.py",
    "src/tracejudge_hy3/benchmark/judge_only_prompt_v3.py",
    "src/tracejudge_hy3/benchmark/judge_only_prompt_v4.py",
    "src/tracejudge_hy3/benchmark/codejudge_v3_execution.py",
    "src/tracejudge_hy3/benchmark/codejudge_v3_metrics.py",
    "src/tracejudge_hy3/benchmark/codejudge_v3_manifest.py",
    "src/tracejudge_hy3/benchmark/hy3_judge_only.py",
    "scripts/run_codejudge_eval_v3.py",
)

DEPENDENCY_LOCK_FILES: tuple[str, ...] = (
    "uv.lock",
    "requirements/phase4-py312-constraints.txt",
)


class V3RunManifest(StrictFrozenModel):
    manifest_schema: Literal["codejudge-eval-v3-run-manifest-v1"] = (
        "codejudge-eval-v3-run-manifest-v1"
    )
    run_id: str = Field(min_length=1, max_length=128)
    experiment_id: str = Field(min_length=1)
    experiment: Literal["a", "b"]
    dataset_id: str
    dataset_revision: str
    dataset_file_sha256: dict[str, Sha256]
    selection_file: str
    selection_file_sha256: Sha256
    selection_entries_sha256: Sha256
    selection_algorithm: str
    prompt_bundle_version: Literal["codejudge_eval_judge_only_v4"] = (
        JUDGE_ONLY_PROMPT_V4_BUNDLE_VERSION
    )
    prompt_bundle_sha256: Sha256
    protocol_file: Literal["docs/codejudge_eval_v3_protocol.md"] = V3_PROTOCOL_PATH
    protocol_sha256: Sha256
    git_commit: GitCommit
    git_global_dirty: bool
    git_experiment_scope_clean: bool
    code_sha256: dict[str, Sha256]
    dependency_lock_sha256: dict[str, Sha256]
    python_version: str
    provider: dict[str, Any]
    planned_calls: tuple[PlannedV3Call, ...]
    planned_calls_sha256: Sha256
    planned_call_n: int = Field(ge=1)
    metrics_policy: dict[str, Any]
    started_at: str
    resume_supported: Literal[True] = True

    @model_validator(mode="after")
    def validate_bindings(self) -> V3RunManifest:
        expected = canonical_sha256([call.model_dump(mode="json") for call in self.planned_calls])
        if self.planned_calls_sha256 != expected:
            raise ValueError("planned_calls_sha256 does not bind planned_calls")
        if self.planned_call_n != len(self.planned_calls):
            raise ValueError("planned_call_n does not match planned_calls")
        expected_n = 120 if self.experiment == "a" else 150
        if self.planned_call_n != expected_n:
            raise ValueError(f"formal v3-{self.experiment} requires {expected_n} calls")
        if not self.git_experiment_scope_clean:
            raise ValueError("formal v3 manifest requires a clean tracked experiment scope")
        return self


def current_git_state(repo: Path) -> tuple[str, bool]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise CodeJudgeEvalError(f"cannot resolve git state: {exc}") from None
    return commit, dirty


def experiment_scope_status(
    repo: Path, relative_paths: tuple[str, ...]
) -> tuple[bool, tuple[str, ...]]:
    """Require every experiment file to be tracked and byte-clean at HEAD."""

    dirty: list[str] = []
    for relative in relative_paths:
        path = repo / relative
        if not path.is_file():
            raise CodeJudgeEvalError(f"experiment-scope file missing: {relative}")
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", relative],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        if tracked.returncode != 0:
            dirty.append(f"untracked:{relative}")
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", *relative_paths],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    dirty.extend(line.strip() for line in status if line.strip())
    return not dirty, tuple(dirty)


def hash_files(repo: Path, relative_paths: tuple[str, ...]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for relative in relative_paths:
        path = repo / relative
        if not path.is_file():
            raise CodeJudgeEvalError(f"bound file missing: {relative}")
        hashes[relative] = file_sha256(path)
    return hashes


def formal_scope_files(selection_relative_path: str) -> tuple[str, ...]:
    return (
        *V3_RUN_SOURCE_FILES,
        V3_PROTOCOL_PATH,
        "data/codejudge_eval/selection_v2.json",
        selection_relative_path,
        *DEPENDENCY_LOCK_FILES,
    )


def build_v3_run_manifest(
    *,
    repo: Path,
    run_id: str,
    experiment: Literal["a", "b"],
    selection: CodeJudgeV3Selection,
    selection_path: Path,
    plan: tuple[tuple[PlannedV3Call, CodeJudgeEvalSample], ...],
    provider_config: dict[str, Any],
    started_at: str,
) -> V3RunManifest:
    selection_relative = selection_path.relative_to(repo).as_posix()
    scope_clean, dirty_paths = experiment_scope_status(repo, formal_scope_files(selection_relative))
    if not scope_clean:
        preview = "; ".join(dirty_paths[:8])
        raise CodeJudgeEvalError(
            "formal v3 experiment scope is not a clean tracked HEAD snapshot: " + preview
        )
    commit, global_dirty = current_git_state(repo)
    calls = tuple(call for call, _ in plan)
    prompt_hash = judge_only_prompt_v4_bundle_sha256()
    if selection.prompt_bundle_sha256 != prompt_hash:
        raise CodeJudgeEvalError("selection is not bound to the active v4 prompt bundle")
    metrics_policy = {
        "a_primary": "balanced_accuracy_full_gold_class_denominators",
        "a_bootstrap_seed": A_BOOTSTRAP_SEED,
        "b_primary": "three_complete_pair_exact_mcnemar_tests",
        "b_holm_family_size": 3,
        "b_failure_rate_limit": B_FAILURE_RATE_LIMIT,
        "b_missingness": "no_imputation_complete_pairs",
        "b_paired_difference_interval": "candidate_percentile_bootstrap",
        "b_bootstrap_seed": B_BOOTSTRAP_SEED,
        "b_native_label_bootstrap_seed_offset": 100,
        "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
    }
    return V3RunManifest(
        run_id=run_id,
        experiment_id=selection.experiment_id,
        experiment=experiment,
        dataset_id=selection.dataset_id,
        dataset_revision=selection.dataset_revision,
        dataset_file_sha256=dict(sorted(selection.file_sha256.items())),
        selection_file=selection_relative,
        selection_file_sha256=file_sha256(selection_path),
        selection_entries_sha256=selection.entries_sha256,
        selection_algorithm=selection.algorithm,
        prompt_bundle_sha256=prompt_hash,
        protocol_sha256=file_sha256(repo / V3_PROTOCOL_PATH),
        git_commit=commit,
        git_global_dirty=global_dirty,
        git_experiment_scope_clean=True,
        code_sha256=hash_files(repo, V3_RUN_SOURCE_FILES),
        dependency_lock_sha256=hash_files(repo, DEPENDENCY_LOCK_FILES),
        python_version=platform.python_version(),
        provider=provider_config,
        planned_calls=calls,
        planned_calls_sha256=call_plan_sha256(plan),
        planned_call_n=len(calls),
        metrics_policy=metrics_policy,
        started_at=started_at,
    )


def build_v3_completion_receipt(
    *,
    run_dir: Path,
    manifest: V3RunManifest,
    completed_at: str,
    audit: dict[str, int],
) -> dict[str, Any]:
    start = datetime.fromisoformat(manifest.started_at)
    end = datetime.fromisoformat(completed_at)
    if end < start:
        raise CodeJudgeEvalError("completion time precedes start time")
    outputs: dict[str, str] = {}
    for name in (
        "manifest.json",
        "records.jsonl",
        "report.json",
        "provider_raw.jsonl",
        "invocations.jsonl",
    ):
        path = run_dir / name
        if path.is_file():
            outputs[f"{name.replace('.', '_')}_sha256"] = file_sha256(path)
    return {
        "schema": "codejudge-eval-v3-completion-receipt-v1",
        "run_id": manifest.run_id,
        "experiment_id": manifest.experiment_id,
        "started_at": manifest.started_at,
        "completed_at": completed_at,
        "duration_seconds": (end - start).total_seconds(),
        "planned_call_n": manifest.planned_call_n,
        "audit": dict(sorted(audit.items())),
        "outputs": outputs,
    }


__all__ = [
    "V3RunManifest",
    "V3_PROTOCOL_PATH",
    "V3_RUN_SOURCE_FILES",
    "build_v3_completion_receipt",
    "build_v3_run_manifest",
    "current_git_state",
    "experiment_scope_status",
    "formal_scope_files",
]
