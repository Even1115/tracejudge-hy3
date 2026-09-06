"""Frozen experiment manifest for the CodeJudge-Eval judge-only run.

The manifest binds, before any held-out sample is judged: the dataset
descriptor (revision + source file manifest hash), the ordered 90-sample
selection, the frozen judge-only **prompt bundle** hash (v2: system prompt +
user/repair template versions + output schema + parse-repair budget), the git
identity, and the allowed metric scope.  Forbidden metrics are recorded as
explicit limitations.

Historical note: the full-v1 run recorded the v1 system-prompt-only hash
(``judge_only_prompt_sha256()``); that binding stays verifiable via the v1
function, while new manifests record the v2 bundle hash.

Beyond the frozen shared ``BenchmarkExperimentManifest``, this module builds
two CodeJudge-specific artifacts:

- :func:`build_run_sidecar` — written BEFORE any model call; binds the actual
  judged subset (ordered task/candidate/candidate-hash triples), source-file
  hashes, dependency-lock hashes, Python version, data/selection hashes, and
  the non-sensitive provider configuration.
- :func:`build_completion_receipt` — written AFTER the run; binds start and
  completion times, duration, provider call/retry and parse-repair audit
  counts, and the hashes of every output artifact.

A ``--limit`` (smoke) run binds the judged subset only and is marked with the
``-smoke`` experiment id so it can never be confused with the formal run.
"""

from __future__ import annotations

import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .codejudge_eval import (
    CodeJudgeEvalError,
    CodeJudgeEvalSelection,
    SelectionEntry,
    file_sha256,
)
from .contracts import (
    BenchmarkDataset,
    BenchmarkExperimentManifest,
    GitCommit,
    Identifier,
    canonical_sha256,
    ordered_ids_sha256,
)
from .judge_only_metrics import ALLOWED_METRICS, FORBIDDEN_METRICS
from .judge_only_prompt import judge_only_prompt_bundle_sha256

# The completed full-v1 run is a historical artifact: its experiment id is
# immutable and must never be minted again.  New runs bind the v2 prompt
# bundle (repair-template binding), so they use the v2 experiment id — a new
# run can never masquerade as a re-run of full-v1.
HISTORICAL_EXPERIMENT_ID_V1 = "codejudge-eval-judge-only-v1"
EXPERIMENT_ID = "codejudge-eval-judge-only-v2"
SMOKE_EXPERIMENT_ID = f"{EXPERIMENT_ID}-smoke"

# Source files whose content hash a run binds for reproducibility.
CODEJUDGE_RUN_SOURCE_FILES: tuple[str, ...] = (
    "src/tracejudge_hy3/benchmark/codejudge_eval.py",
    "src/tracejudge_hy3/benchmark/judge_only_prompt.py",
    "src/tracejudge_hy3/benchmark/judge_only_runner.py",
    "src/tracejudge_hy3/benchmark/judge_only_metrics.py",
    "src/tracejudge_hy3/benchmark/judge_only_manifest.py",
    "src/tracejudge_hy3/benchmark/hy3_judge_only.py",
    "scripts/run_codejudge_eval_judge_only.py",
)

DEPENDENCY_LOCK_FILES: tuple[str, ...] = (
    "uv.lock",
    "requirements/phase4-py312-constraints.txt",
)

_LIMITATIONS: tuple[str, ...] = tuple(f"no-{metric}" for metric in FORBIDDEN_METRICS) + (
    "held-out-never-used-for-prompt-or-threshold-tuning",
    "judge-only-no-hy3-native-trace",
)


def current_git_state(repo: Path) -> tuple[str, bool]:
    """Return (commit, dirty) for manifest binding; fails closed outside git."""

    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = (
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            != ""
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise CodeJudgeEvalError(f"cannot resolve git state for manifest: {exc}") from None
    return commit, dirty


def build_experiment_manifest(
    *,
    descriptor: BenchmarkDataset,
    selection: CodeJudgeEvalSelection,
    git_commit: GitCommit,
    git_dirty: bool,
    provider: Identifier | None = None,
    model: str | None = None,
    extra_limitations: tuple[Identifier, ...] = (),
    judged_entries: tuple[SelectionEntry, ...] | None = None,
    smoke: bool = False,
) -> BenchmarkExperimentManifest:
    """Freeze the pre-run manifest; the judge prompt bundle hash is computed here.

    ``judged_entries`` binds the manifest to the *actual* judged subset (for
    ``--limit`` smoke runs); ``None`` binds the full frozen selection.  Every
    judged entry must be part of the frozen selection, in selection order.
    A smoke run gets the distinct ``-smoke`` experiment id and explicit
    limitations so it can never be confused with the formal experiment.
    """

    if selection.dataset_id != descriptor.dataset_id:
        raise CodeJudgeEvalError("selection does not belong to the descriptor dataset")
    if selection.dataset_revision != descriptor.revision:
        raise CodeJudgeEvalError("selection revision does not match the descriptor")
    if (provider is None) != (model is None):
        raise CodeJudgeEvalError("provider and model must be set together")
    if judged_entries is None:
        judged_entries = selection.entries
    selection_index = {entry.candidate_id: entry for entry in selection.entries}
    order_keys = {entry.candidate_id: i for i, entry in enumerate(selection.entries)}
    for entry in judged_entries:
        if selection_index.get(entry.candidate_id) != entry:
            raise CodeJudgeEvalError(
                f"judged entry {entry.candidate_id} is not part of the frozen selection"
            )
    if [order_keys[e.candidate_id] for e in judged_entries] != sorted(
        order_keys[e.candidate_id] for e in judged_entries
    ):
        raise CodeJudgeEvalError("judged entries must follow the frozen selection order")
    limitations = _LIMITATIONS + tuple(extra_limitations)
    experiment_id = EXPERIMENT_ID
    if smoke:
        experiment_id = SMOKE_EXPERIMENT_ID
        limitations += ("smoke-run",)
    if len(judged_entries) != len(selection.entries):
        limitations += (f"judged-n:{len(judged_entries)}",)
    selected_ids = tuple(entry.task_id for entry in judged_entries)
    return BenchmarkExperimentManifest(
        experiment_id=experiment_id,
        dataset=descriptor,
        selected_task_ids=selected_ids,
        selected_task_ids_sha256=ordered_ids_sha256(selected_ids),
        selection_algorithm=selection.algorithm,
        generation_prompt_sha256=None,
        judge_prompt_sha256=judge_only_prompt_bundle_sha256(),
        git_commit=git_commit,
        git_dirty=git_dirty,
        provider=provider,
        model=model,
        metrics_scope=ALLOWED_METRICS,
        limitations=limitations,
    )


def hash_source_files(
    repo: Path, relative_paths: tuple[str, ...] = CODEJUDGE_RUN_SOURCE_FILES
) -> dict[str, str]:
    """Hash every CodeJudge-Eval run source file; missing files fail closed."""

    hashes: dict[str, str] = {}
    for relative in relative_paths:
        path = repo / relative
        if not path.is_file():
            raise CodeJudgeEvalError(f"run source file missing: {relative}")
        hashes[relative] = file_sha256(path)
    return hashes


def hash_dependency_locks(
    repo: Path, relative_paths: tuple[str, ...] = DEPENDENCY_LOCK_FILES
) -> dict[str, str]:
    """Hash dependency lock files that exist; missing ones fail closed."""

    hashes: dict[str, str] = {}
    for relative in relative_paths:
        path = repo / relative
        if not path.is_file():
            raise CodeJudgeEvalError(f"dependency lock file missing: {relative}")
        hashes[relative] = file_sha256(path)
    return hashes


def _iso_utc(moment: datetime | None = None) -> str:
    return (moment or datetime.now(UTC)).isoformat()


def build_run_sidecar(
    *,
    manifest: BenchmarkExperimentManifest,
    provider_config: dict[str, Any],
    judged_entries: tuple[SelectionEntry, ...],
    selection: CodeJudgeEvalSelection,
    selection_file_sha256: str,
    repo: Path,
    started_at: str,
    docker_image_digest: str | None = None,
) -> dict[str, Any]:
    """Pre-run CodeJudge sidecar: binds everything the frozen manifest cannot.

    A dirty worktree is never described as fully reproducible; the source-file
    hashes recorded here are the fallback binding in that case.
    """

    triples = [
        {
            "task_id": entry.task_id,
            "candidate_id": entry.candidate_id,
            "candidate_code_sha256": entry.candidate_code_sha256,
        }
        for entry in judged_entries
    ]
    dirty = manifest.git_dirty
    return {
        "schema": "codejudge-eval-run-sidecar-v1",
        "experiment_id": manifest.experiment_id,
        "smoke": manifest.experiment_id.endswith("-smoke"),
        "git": {"commit": manifest.git_commit, "dirty": dirty},
        "reproducibility_note": (
            "dirty-worktree-not-fully-reproducible; source-file hashes below are the binding"
            if dirty
            else "clean-commit"
        ),
        "code_sha256": hash_source_files(repo),
        "dependency_lock_sha256": hash_dependency_locks(repo),
        "python_version": platform.python_version(),
        "prompt_bundle_sha256": judge_only_prompt_bundle_sha256(),
        "data": {
            "file_sha256": dict(sorted(selection.file_sha256.items())),
            "selection_file_sha256": selection_file_sha256,
            "source_manifest_sha256": manifest.dataset.source_manifest_sha256,
        },
        "judged_samples": {
            "selected_n": len(selection.entries),
            "judged_n": len(judged_entries),
            "ordered": triples,
            "ordered_judged_sha256": canonical_sha256(triples),
        },
        "provider": provider_config,
        "docker_image_digest": docker_image_digest,
        "started_at": started_at,
    }


def build_completion_receipt(
    *,
    run_dir: Path,
    run_id: str,
    started_at: str,
    completed_at: str,
    audit: dict[str, int],
    manifest_sha256: str,
    sidecar_sha256: str,
) -> dict[str, Any]:
    """Post-run receipt: times, retry/repair audit counts, and output hashes."""

    start = datetime.fromisoformat(started_at)
    end = datetime.fromisoformat(completed_at)
    duration = (end - start).total_seconds()
    if duration < 0:
        raise CodeJudgeEvalError("completed_at precedes started_at")
    outputs: dict[str, str] = {
        "manifest_sha256": manifest_sha256,
        "sidecar_sha256": sidecar_sha256,
    }
    for name in ("records.jsonl", "report.json", "provider_raw.jsonl"):
        path = run_dir / name
        if path.is_file():
            outputs[f"{name.replace('.', '_')}_sha256"] = file_sha256(path)
    return {
        "schema": "codejudge-eval-completion-receipt-v1",
        "run_id": run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_seconds": duration,
        "audit": dict(audit),
        "outputs": outputs,
    }
