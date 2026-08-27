"""Phase-two orchestration for isolated official EvalPlus execution.

The orchestration boundary is intentionally independent from every Provider
and from the process-evaluation pipeline.  Candidate source is only copied to
``samples.jsonl`` and handed to an injected executor; this module never
imports, compiles, evaluates, or executes it on the host.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed, wait
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from .exporter import (
    RESEARCH_NATURAL_COUNT,
    SelectionPolicy,
    load_validated_phase1_export,
    serialize_samples_jsonl,
)
from .parser import (
    INFRASTRUCTURE_ERROR_TYPES,
    RAW_BUNDLE_KIND,
    EvalPlusParseError,
    build_summary,
    infrastructure_error_result,
    parse_official_result,
)
from .schemas import (
    EvalPlusSample,
    HumanEvalPlusTaskMetadata,
    ValidatedSampleExport,
)

RAW_MOCK_BUNDLE_KIND = "tracejudge_evalplus_mock_no_execution_bundle"


_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ISO_UTC_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_DIRECT_DEPENDENCIES = ("pydantic", "pydantic-settings", "openai", "typer", "rich")
_MAX_EXECUTION_LOG_BYTES = 64 * 1024
_MAX_SAFE_ARTIFACT_BYTES = 128 * 1024 * 1024
_BATCH_CLEANUP_GRACE_SECONDS = 5.0
_CLEANUP_STATUSES = frozenset({"not_needed", "removed", "not_found", "failed"})
_RESUME_IDENTITY_ERROR = (
    "resume refused because provenance, code, EvalPlus, image, or execution config changed"
)
_RUNTIME_DISCOVERED_EXECUTOR_FIELDS = {
    "native_dataset_canonical_sha256",
    "official_dataset_hash",
    "official_dataset_file_sha256",
    "official_dataset_file_size_bytes",
}
_MANIFEST_FIELDS = {
    "schema_version",
    "phase",
    "experiment_label",
    "metrics_scope",
    "run_id",
    "status",
    "created_at",
    "completed_at",
    "execution_mode",
    "phase1_source",
    "dataset",
    "input",
    "executor",
    "executor_runtime",
    "execution_config",
    "git",
    "environment",
    "resume_fingerprint",
    "preflight",
    "output",
    "invocations",
    "limitations",
}
_RESULT_FIELDS = {
    "schema_version",
    "run_id",
    "problem_id",
    "base_status",
    "plus_status",
    "base_fail_test_count",
    "plus_fail_test_count",
    "passed_base",
    "passed_plus",
    "error_type",
    "infrastructure_status",
    "solution_sha256",
    "official_override_hash",
    "duration_seconds",
    "started_at",
    "ended_at",
    "failure_count_scope",
    "source_response",
}


def _phase2_identity(
    exported: ValidatedSampleExport,
) -> tuple[str, str, list[str], str]:
    """Return (experiment_label, metrics_scope, limitations, cohort_description)."""

    dataset_identity = exported.dataset
    selection = exported.export_selection
    source_count = selection.source_problem_count
    exported_count = selection.exported_success_count
    shared_limitations = [
        "not_an_official_benchmark_ranking",
        "public_benchmark_training_contamination_or_memorization_is_possible",
        "phase1_parse_success_is_not_phase2_functional_success",
        "pinned_evalplus_fail_combines_wrong_answers_and_candidate_exceptions",
    ]
    if dataset_identity.selection_role == "pilot":
        limitations = [
            f"fixed_{source_count}_problem_subset_not_full_humanevalplus",
            "single_sample_generation_to_execution_engineering_pilot",
            *shared_limitations,
        ]
        if exported_count != source_count:
            limitations.append("phase2_conditioned_on_phase1_success")
        if exported_count == source_count:
            return (
                f"humanevalplus_{source_count}_evalplus_execution_pilot",
                f"fixed_{source_count}_task_generation_to_execution_engineering_pilot",
                limitations,
                f"fixed_{source_count}_problem_single_sample_generation_to_execution_pilot",
            )
        return (
            f"humanevalplus_{exported_count}_of_{source_count}_evalplus_execution_pilot",
            f"fixed_{exported_count}_of_{source_count}_task_generation_to_execution_pilot",
            limitations,
            f"fixed_{exported_count}_of_{source_count}_problem_single_sample_execution_pilot",
        )
    if (
        dataset_identity.selection_role == "research_natural"
        and source_count == RESEARCH_NATURAL_COUNT
    ):
        limitations = [
            "research_natural_45_source_task_subset_not_full_humanevalplus",
            "single_sample_per_exported_phase1_success",
            *shared_limitations,
        ]
        if selection.selection_policy == "phase1-success-only":
            limitations.append("phase2_conditioned_on_phase1_success")
        if exported_count == source_count:
            return (
                "humanevalplus_45_evalplus_execution_research_natural",
                "research_natural_45_task_generation_to_execution",
                limitations,
                "research_natural_45_task_single_sample_generation_to_execution",
            )
        return (
            f"humanevalplus_{exported_count}_of_45_evalplus_execution_research_natural",
            f"research_natural_{exported_count}_of_45_phase1_success_conditioned_execution",
            limitations,
            f"research_natural_{exported_count}_of_45_phase1_successful_tasks_execution",
        )
    return (
        f"humanevalplus_{exported_count}_of_{source_count}_evalplus_execution",
        f"fixed_{exported_count}_of_{source_count}_task_generation_to_execution",
        list(shared_limitations),
        f"fixed_{exported_count}_of_{source_count}_problem_single_sample_execution",
    )


class EvalPlusExperimentError(ValueError):
    """Raised when a phase-two run cannot be created or safely resumed."""


@dataclass(frozen=True, slots=True)
class ExecutorTaskOutcome:
    """One executor invocation, containing raw data or an infrastructure error."""

    problem_id: str
    started_at: str
    ended_at: str
    duration_seconds: float | None
    raw_result: Mapping[str, Any] | None
    infrastructure_error_type: str | None
    diagnostics: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ExecutorPreflight:
    """Disclosure-safe runtime identity discovered before candidate execution."""

    ready: bool
    runtime: Mapping[str, Any]
    infrastructure_error_type: str | None = None
    diagnostics: Mapping[str, Any] | None = None


class EvalPlusExecutor(Protocol):
    """Minimal executor surface used by the phase-two runner."""

    mode: Literal["docker", "mock"]

    def public_identity(self) -> Mapping[str, Any]: ...

    def preflight(
        self,
        *,
        task_metadata: Sequence[HumanEvalPlusTaskMetadata],
        workspace: Path,
    ) -> ExecutorPreflight: ...

    def run_task(
        self,
        *,
        sample: EvalPlusSample,
        task_metadata: HumanEvalPlusTaskMetadata,
        workspace: Path,
    ) -> ExecutorTaskOutcome: ...


@dataclass(frozen=True, slots=True)
class EvalPlusRunResult:
    """Paths and final safe summary returned by a phase-two run."""

    run_id: str
    run_dir: Path
    manifest_path: Path
    samples_path: Path
    raw_results_path: Path
    results_path: Path
    summary_path: Path
    execution_log_path: Path
    manifest: dict[str, Any]
    summary: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _RunPaths:
    run_dir: Path
    manifest: Path
    samples: Path
    raw_results: Path
    results: Path
    summary: Path
    execution_log: Path


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_utc(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_evalplus_run_id() -> str:
    """Return a collision-resistant phase-two run identifier."""

    timestamp = _utc_now().strftime("%Y%m%dT%H%M%S%fZ")
    return f"phase2_{timestamp}_{uuid.uuid4().hex[:12]}"


def _validate_run_id(run_id: str) -> None:
    if not _RUN_ID_PATTERN.fullmatch(run_id):
        raise EvalPlusExperimentError(
            "run_id must contain only letters, digits, '.', '_' or '-' and be at most "
            "128 characters"
        )


def _run_paths(output_dir: str | Path, run_id: str) -> _RunPaths:
    base = Path(output_dir).expanduser().resolve()
    run_dir = base / run_id
    return _RunPaths(
        run_dir=run_dir,
        manifest=run_dir / "manifest.json",
        samples=run_dir / "samples.jsonl",
        raw_results=run_dir / "evalplus_raw_results.json",
        results=run_dir / "results.jsonl",
        summary=run_dir / "summary.json",
        execution_log=run_dir / "execution.log",
    )


def _require_non_trackable_run_directory(run_dir: Path) -> None:
    """Refuse a repository-local output path unless Git ignores it."""

    repository = Path(__file__).resolve().parents[3]
    try:
        relative = run_dir.relative_to(repository)
    except ValueError:
        raise EvalPlusExperimentError(
            "phase-two output must be inside this repository and covered by .gitignore"
        ) from None
    try:
        completed = subprocess.run(
            ["git", "check-ignore", "-q", "--", str(relative)],
            cwd=repository,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise EvalPlusExperimentError(
            "cannot verify that the repository-local phase-two output is Git-ignored"
        ) from None
    if completed.returncode != 0:
        raise EvalPlusExperimentError(
            "repository-local phase-two output must be covered by .gitignore"
        )


def _fsync_directory(path: Path) -> None:
    try:
        directory_fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        pass
    finally:
        os.close(directory_fd)


def _atomic_write_bytes(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    """Atomically replace one artifact and keep it private to the current user."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.chmod(temporary_path, mode)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        os.chmod(path, mode)
        _fsync_directory(path.parent)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _json_bytes(value: Any) -> bytes:
    try:
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
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise EvalPlusExperimentError("phase-two safe artifact is not valid UTF-8 JSON") from exc


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_bytes(path, _json_bytes(value))


def _jsonl_bytes(records: Sequence[Mapping[str, Any]]) -> bytes:
    encoded: list[bytes] = []
    try:
        for record in records:
            encoded.append(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise EvalPlusExperimentError("phase-two safe JSONL record is invalid") from exc
    return b"".join(encoded)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


class _AmbiguousJSON(ValueError):
    pass


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _AmbiguousJSON
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise _AmbiguousJSON


def _strict_json_loads(value: str) -> Any:
    return json.loads(
        value,
        object_pairs_hook=_strict_json_object,
        parse_constant=_reject_json_constant,
    )


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError:
        raise EvalPlusExperimentError(f"required {label} is missing or unsafe") from None
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise EvalPlusExperimentError(f"required {label} is missing or unsafe")
    try:
        if path.stat().st_size > _MAX_SAFE_ARTIFACT_BYTES:
            raise EvalPlusExperimentError(f"required {label} exceeds the size limit")
        value = _strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, _AmbiguousJSON):
        raise EvalPlusExperimentError(f"required {label} is not valid UTF-8 JSON") from None
    if not isinstance(value, dict):
        raise EvalPlusExperimentError(f"required {label} must contain a JSON object")
    return value


def _read_jsonl(path: Path, *, label: str) -> list[dict[str, Any]]:
    try:
        metadata = path.lstat()
    except OSError:
        raise EvalPlusExperimentError(f"required {label} is missing or unsafe") from None
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise EvalPlusExperimentError(f"required {label} is missing or unsafe")
    records: list[dict[str, Any]] = []
    try:
        if path.stat().st_size > _MAX_SAFE_ARTIFACT_BYTES:
            raise EvalPlusExperimentError(f"required {label} exceeds the size limit")
        with path.open(encoding="utf-8") as stream:
            for raw_line in stream:
                if not raw_line.strip():
                    continue
                value = _strict_json_loads(raw_line)
                if not isinstance(value, dict):
                    raise EvalPlusExperimentError(f"required {label} contains a non-object")
                records.append(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, _AmbiguousJSON):
        raise EvalPlusExperimentError(f"required {label} is not valid UTF-8 JSONL") from None
    return records


def _implementation_sha256() -> str:
    """Fingerprint the phase-two implementation without inspecting user files."""

    package_dir = Path(__file__).resolve().parent
    package_root = package_dir.parent
    files = set(package_dir.glob("*.py"))
    files.update(
        {
            package_root / "cli.py",
            package_root / "dataset" / "humanevalplus.py",
            package_root / "dataset" / "loader.py",
            package_root / "redaction.py",
            package_root / "resources.py",
        }
    )
    digest = hashlib.sha256()
    for path in sorted((item for item in files if item.is_file()), key=lambda item: str(item)):
        digest.update(path.relative_to(package_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _git_command(*arguments: str) -> str | None:
    repository = Path(__file__).resolve().parents[3]
    try:
        proc = subprocess.run(
            ["git", *arguments],
            cwd=repository,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _git_metadata() -> dict[str, Any]:
    commit = _git_command("rev-parse", "HEAD")
    branch = _git_command("branch", "--show-current")
    status = _git_command("status", "--porcelain", "--untracked-files=all")
    return {
        "available": bool(commit and re.fullmatch(r"[0-9a-f]{40}", commit)),
        "commit": commit,
        "branch": branch or None,
        "dirty": None if status is None else bool(status),
        "implementation_sha256": _implementation_sha256(),
    }


def _environment_metadata() -> dict[str, Any]:
    dependencies: dict[str, str | None] = {}
    for package in _DIRECT_DEPENDENCIES:
        try:
            dependencies[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            dependencies[package] = None
    return {
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
        },
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
        },
        "direct_dependencies": dependencies,
    }


def _canonical_fingerprint(value: Mapping[str, Any]) -> str:
    return _sha256_bytes(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def _source_reference(exported: ValidatedSampleExport, problem_id: str) -> dict[str, Any]:
    return asdict(exported.reference_for(problem_id))


def _task_metadata_by_id(
    exported: ValidatedSampleExport,
) -> dict[str, HumanEvalPlusTaskMetadata]:
    result = {item.problem_id: item for item in exported.task_metadata}
    if set(result) != {sample.task_id for sample in exported.samples}:
        raise EvalPlusExperimentError("public task metadata differs from exported samples")
    return result


def _input_identity(exported: ValidatedSampleExport) -> dict[str, Any]:
    return {
        "record_count": len(exported.samples),
        "samples_sha256": exported.samples_sha256,
        "ordered_problem_ids": [sample.task_id for sample in exported.samples],
        "public_task_identity": [asdict(metadata) for metadata in exported.task_metadata],
        "code_sha256": {
            reference.problem_id: reference.code_sha256
            for reference in exported.response_references
        },
        "phase1_export_selection": asdict(exported.export_selection),
    }


def _execution_config(
    *,
    max_workers: int,
    per_task_timeout_seconds: float,
    batch_timeout_seconds: float,
) -> dict[str, Any]:
    return {
        "task_parallelism": max_workers,
        "official_parallel_per_task": 1,
        "per_task_container_timeout_seconds": per_task_timeout_seconds,
        "batch_timeout_seconds": batch_timeout_seconds,
        "batch_cleanup_grace_seconds": _BATCH_CLEANUP_GRACE_SECONDS,
        "official_test_details": True,
        "official_min_time_limit_seconds": 4.0,
        "official_gt_time_limit_factor": 4.0,
        "official_internal_timeout_cap_seconds": 60,
    }


def _static_executor_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: nested
        for key, nested in value.items()
        if key not in _RUNTIME_DISCOVERED_EXECUTOR_FIELDS
    }


def _validate_resume_manifest_schema(
    manifest: Mapping[str, Any],
    *,
    run_id: str,
    execution_mode: str,
    exported: ValidatedSampleExport,
) -> None:
    expected_label, expected_scope, expected_limitations, _ = _phase2_identity(exported)
    if set(manifest) != _MANIFEST_FIELDS:
        raise EvalPlusExperimentError("resume manifest schema is invalid")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("phase") != "phase2_evalplus_execution"
        or manifest.get("experiment_label") != expected_label
        or manifest.get("metrics_scope") != expected_scope
        or manifest.get("run_id") != run_id
        or manifest.get("execution_mode") != execution_mode
        or manifest.get("status") not in {"running", "completed"}
        or manifest.get("limitations") != expected_limitations
        or not isinstance(manifest.get("resume_fingerprint"), str)
        or not _SHA256_PATTERN.fullmatch(str(manifest.get("resume_fingerprint")))
    ):
        raise EvalPlusExperimentError("resume manifest identity is invalid")
    for field in ("created_at",):
        value = manifest.get(field)
        if not isinstance(value, str) or not _ISO_UTC_PATTERN.fullmatch(value):
            raise EvalPlusExperimentError("resume manifest timestamp is invalid")
    completed_at = manifest.get("completed_at")
    if completed_at is not None and (
        not isinstance(completed_at, str) or not _ISO_UTC_PATTERN.fullmatch(completed_at)
    ):
        raise EvalPlusExperimentError("resume manifest timestamp is invalid")

    git = manifest.get("git")
    if not isinstance(git, Mapping) or set(git) != {
        "available",
        "commit",
        "branch",
        "dirty",
        "implementation_sha256",
    }:
        raise EvalPlusExperimentError("resume manifest Git metadata is invalid")
    environment = manifest.get("environment")
    if not isinstance(environment, Mapping) or set(environment) != {
        "python",
        "platform",
        "direct_dependencies",
    }:
        raise EvalPlusExperimentError("resume manifest environment metadata is invalid")
    python_identity = environment.get("python")
    platform_identity = environment.get("platform")
    dependencies = environment.get("direct_dependencies")
    if (
        not isinstance(python_identity, Mapping)
        or set(python_identity) != {"version", "implementation", "executable"}
        or not isinstance(platform_identity, Mapping)
        or set(platform_identity) != {"system", "machine"}
        or not isinstance(dependencies, Mapping)
        or set(dependencies) != set(_DIRECT_DEPENDENCIES)
    ):
        raise EvalPlusExperimentError("resume manifest environment metadata is invalid")

    preflight = manifest.get("preflight")
    if not isinstance(preflight, Mapping) or preflight.get("status") not in {
        "pending",
        "ready",
        "failed",
    }:
        raise EvalPlusExperimentError("resume manifest preflight metadata is invalid")
    allowed_preflight_fields = {
        "status",
        "ready",
        "infrastructure_error_type",
    }
    if preflight.get("status") == "pending" and "prior_status" in preflight:
        allowed_preflight_fields.add("prior_status")
    if set(preflight) != allowed_preflight_fields:
        raise EvalPlusExperimentError("resume manifest preflight metadata is invalid")

    invocations = manifest.get("invocations")
    if not isinstance(invocations, list) or not invocations:
        raise EvalPlusExperimentError("resume manifest invocation history is invalid")
    for invocation in invocations:
        if not isinstance(invocation, Mapping) or set(invocation) != {
            "invocation_id",
            "resume",
            "started_at",
            "completed_at",
            "status",
        }:
            raise EvalPlusExperimentError("resume manifest invocation history is invalid")
        invocation_id = invocation.get("invocation_id")
        started_at = invocation.get("started_at")
        invocation_completed_at = invocation.get("completed_at")
        if (
            not isinstance(invocation_id, str)
            or not re.fullmatch(r"[0-9a-f]{32}", invocation_id)
            or not isinstance(invocation.get("resume"), bool)
            or not isinstance(started_at, str)
            or not _ISO_UTC_PATTERN.fullmatch(started_at)
            or invocation.get("status") not in {"running", "completed", "interrupted"}
            or (
                invocation_completed_at is not None
                and (
                    not isinstance(invocation_completed_at, str)
                    or not _ISO_UTC_PATTERN.fullmatch(invocation_completed_at)
                )
            )
        ):
            raise EvalPlusExperimentError("resume manifest invocation history is invalid")


def _validate_resume_before_preflight(
    manifest: Mapping[str, Any],
    exported: ValidatedSampleExport,
    executor: EvalPlusExecutor,
    *,
    run_id: str,
    max_workers: int,
    per_task_timeout_seconds: float,
    batch_timeout_seconds: float,
) -> None:
    """Reject every statically detectable resume change before Docker starts."""

    _validate_resume_manifest_schema(
        manifest,
        run_id=run_id,
        execution_mode=executor.mode,
        exported=exported,
    )

    recorded_static = {
        "phase1_source": manifest.get("phase1_source"),
        "dataset": manifest.get("dataset"),
        "input": manifest.get("input"),
        "execution_config": manifest.get("execution_config"),
        "git": manifest.get("git"),
        "environment": manifest.get("environment"),
    }
    expected_static = {
        "phase1_source": asdict(exported.phase1),
        "dataset": asdict(exported.dataset),
        "input": _input_identity(exported),
        "execution_config": _execution_config(
            max_workers=max_workers,
            per_task_timeout_seconds=per_task_timeout_seconds,
            batch_timeout_seconds=batch_timeout_seconds,
        ),
        "git": _git_metadata(),
        "environment": _environment_metadata(),
    }
    if (
        manifest.get("schema_version") != 1
        or manifest.get("phase") != "phase2_evalplus_execution"
        or manifest.get("run_id") != run_id
        or _canonical_fingerprint(recorded_static) != _canonical_fingerprint(expected_static)
    ):
        raise EvalPlusExperimentError(_RESUME_IDENTITY_ERROR)
    recorded_executor = manifest.get("executor")
    if not isinstance(recorded_executor, Mapping) or _canonical_fingerprint(
        _static_executor_identity(recorded_executor)
    ) != _canonical_fingerprint(_static_executor_identity(executor.public_identity())):
        raise EvalPlusExperimentError(_RESUME_IDENTITY_ERROR)
    git = manifest.get("git")
    if not isinstance(git, Mapping) or git.get("implementation_sha256") != _implementation_sha256():
        raise EvalPlusExperimentError(_RESUME_IDENTITY_ERROR)


def _validate_completed_output_integrity(
    manifest: Mapping[str, Any],
    paths: _RunPaths,
    *,
    expected_result_count: int,
) -> None:
    """Bind a completed run's public/private artifacts to its final manifest."""

    status = manifest.get("status")
    output = manifest.get("output")
    if status == "running":
        if output is not None:
            raise EvalPlusExperimentError("running resume manifest has final output metadata")
        return
    if status != "completed" or not isinstance(output, Mapping):
        raise EvalPlusExperimentError("resume manifest status/output metadata is invalid")
    expected_paths = {
        "samples_sha256": paths.samples,
        "raw_results_sha256": paths.raw_results,
        "results_sha256": paths.results,
        "summary_sha256": paths.summary,
        "execution_log_sha256": paths.execution_log,
    }
    if (
        set(output) != {*expected_paths, "result_count"}
        or output.get("result_count") != expected_result_count
    ):
        raise EvalPlusExperimentError("resume manifest status/output metadata is invalid")
    for field, path in expected_paths.items():
        expected_hash = output.get(field)
        if (
            not isinstance(expected_hash, str)
            or not _SHA256_PATTERN.fullmatch(expected_hash)
            or path.is_symlink()
            or not path.is_file()
            or stat.S_IMODE(path.stat().st_mode) != 0o600
            or _sha256_file(path) != expected_hash
        ):
            raise EvalPlusExperimentError("completed phase-two output hash validation failed")


def _manifest_identity(
    exported: ValidatedSampleExport,
    executor: EvalPlusExecutor,
    preflight: ExecutorPreflight,
    *,
    max_workers: int,
    per_task_timeout_seconds: float,
    batch_timeout_seconds: float,
) -> dict[str, Any]:
    identity = {
        "phase1_source": asdict(exported.phase1),
        "dataset": asdict(exported.dataset),
        "input": _input_identity(exported),
        "executor": dict(executor.public_identity()),
        "executor_runtime": dict(preflight.runtime),
        "execution_config": _execution_config(
            max_workers=max_workers,
            per_task_timeout_seconds=per_task_timeout_seconds,
            batch_timeout_seconds=batch_timeout_seconds,
        ),
        "git": _git_metadata(),
        "environment": _environment_metadata(),
        "implementation_sha256": _implementation_sha256(),
    }
    return identity


def _new_manifest(
    run_id: str,
    exported: ValidatedSampleExport,
    executor: EvalPlusExecutor,
    identity: Mapping[str, Any],
    *,
    created_at: str,
    initial_resume: bool = False,
) -> dict[str, Any]:
    experiment_label, metrics_scope, limitations, _ = _phase2_identity(exported)
    invocation_id = uuid.uuid4().hex
    return {
        "schema_version": 1,
        "phase": "phase2_evalplus_execution",
        "experiment_label": experiment_label,
        "metrics_scope": metrics_scope,
        "run_id": run_id,
        "status": "running",
        "created_at": created_at,
        "completed_at": None,
        "execution_mode": executor.mode,
        "phase1_source": asdict(exported.phase1),
        "dataset": asdict(exported.dataset),
        "input": identity["input"],
        "executor": identity["executor"],
        "executor_runtime": identity["executor_runtime"],
        "execution_config": identity["execution_config"],
        "git": identity["git"],
        "environment": identity["environment"],
        "resume_fingerprint": _canonical_fingerprint(identity),
        "preflight": {
            "status": "pending",
            "ready": None,
            "infrastructure_error_type": None,
        },
        "output": None,
        "invocations": [
            {
                "invocation_id": invocation_id,
                "resume": initial_resume,
                "started_at": created_at,
                "completed_at": None,
                "status": "running",
            }
        ],
        "limitations": list(limitations),
    }


def _begin_resume_manifest(
    manifest: dict[str, Any],
    *,
    started_at: str,
) -> dict[str, Any]:
    if manifest.get("schema_version") != 1 or manifest.get("phase") != "phase2_evalplus_execution":
        raise EvalPlusExperimentError("resume manifest identity is invalid")
    previous_preflight = manifest.get("preflight")
    previous_preflight_status = None
    if isinstance(previous_preflight, Mapping):
        previous_preflight_status = previous_preflight.get("status")
        if previous_preflight_status == "pending":
            # Preserve the last trusted terminal preflight across repeated
            # interruptions; otherwise a second resume could bypass the
            # runtime fingerprint comparison by replacing `ready` with
            # `pending`.
            previous_preflight_status = previous_preflight.get(
                "prior_status",
                "pending",
            )
    invocations = manifest.get("invocations")
    if not isinstance(invocations, list):
        raise EvalPlusExperimentError("resume manifest invocation history is invalid")
    for invocation in invocations:
        if isinstance(invocation, dict) and invocation.get("status") == "running":
            invocation["status"] = "interrupted"
            invocation["completed_at"] = started_at
    invocations.append(
        {
            "invocation_id": uuid.uuid4().hex,
            "resume": True,
            "started_at": started_at,
            "completed_at": None,
            "status": "running",
        }
    )
    manifest["status"] = "running"
    manifest["completed_at"] = None
    manifest["preflight"] = {
        "status": "pending",
        "prior_status": previous_preflight_status,
        "ready": None,
        "infrastructure_error_type": None,
    }
    manifest["output"] = None
    return manifest


def _apply_preflight_to_manifest(
    manifest: dict[str, Any],
    *,
    identity: Mapping[str, Any],
    preflight: ExecutorPreflight,
    resumed: bool,
) -> dict[str, Any]:
    """Bind discovered runtime identity before any candidate task can start."""

    expected_fingerprint = _canonical_fingerprint(identity)
    preflight_record = manifest.get("preflight")
    if not isinstance(preflight_record, Mapping) or preflight_record.get("status") != "pending":
        raise EvalPlusExperimentError("phase-two preflight checkpoint is invalid")
    prior_status = preflight_record.get("prior_status") if resumed else None
    if resumed and prior_status in {"ready", "failed"}:
        if manifest.get("resume_fingerprint") != expected_fingerprint:
            raise EvalPlusExperimentError(_RESUME_IDENTITY_ERROR)
    elif resumed and prior_status not in {None, "pending"}:
        raise EvalPlusExperimentError("resume preflight checkpoint is invalid")

    manifest["phase1_source"] = identity["phase1_source"]
    manifest["dataset"] = identity["dataset"]
    manifest["input"] = identity["input"]
    manifest["executor"] = identity["executor"]
    manifest["executor_runtime"] = identity["executor_runtime"]
    manifest["execution_config"] = identity["execution_config"]
    manifest["git"] = identity["git"]
    manifest["environment"] = identity["environment"]
    manifest["resume_fingerprint"] = expected_fingerprint
    manifest["preflight"] = {
        "status": "ready" if preflight.ready else "failed",
        "ready": preflight.ready,
        "infrastructure_error_type": preflight.infrastructure_error_type,
    }
    return manifest


def _safe_diagnostic_event(
    event: str,
    *,
    problem_id: str | None = None,
    outcome: ExecutorTaskOutcome | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {"timestamp": _iso_utc(_utc_now()), "event": event}
    if problem_id is not None:
        record["problem_id"] = problem_id
    if outcome is not None:
        record.update(
            {
                "duration_seconds": outcome.duration_seconds,
                "infrastructure_error_type": outcome.infrastructure_error_type,
            }
        )
        # Executor diagnostics are required to be a small positive allowlist.
        for key in ("exit_code", "stdout_bytes", "stderr_bytes"):
            value = outcome.diagnostics.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                record[key] = value
        for key in ("stdout_sha256", "stderr_sha256"):
            value = outcome.diagnostics.get(key)
            if isinstance(value, str) and _SHA256_PATTERN.fullmatch(value):
                record[key] = value
        cleanup_status = outcome.diagnostics.get("cleanup_status")
        if cleanup_status in {"not_needed", "removed", "not_found", "failed"}:
            record["cleanup_status"] = cleanup_status
    return record


def _write_execution_log(path: Path, events: Sequence[Mapping[str, Any]]) -> None:
    payload = _jsonl_bytes(events)
    if len(payload) > _MAX_EXECUTION_LOG_BYTES:
        marker = _jsonl_bytes(
            [
                {
                    "timestamp": _iso_utc(_utc_now()),
                    "event": "log_length_limit_reached",
                    "limit_bytes": _MAX_EXECUTION_LOG_BYTES,
                }
            ]
        )
        payload = payload[: max(0, _MAX_EXECUTION_LOG_BYTES - len(marker))]
        newline = payload.rfind(b"\n")
        payload = (payload[: newline + 1] if newline >= 0 else b"") + marker
    _atomic_write_bytes(path, payload)


def _validated_existing_events(paths: _RunPaths) -> list[dict[str, Any]]:
    if not paths.execution_log.exists():
        return []
    records = _read_jsonl(paths.execution_log, label="phase-two execution log")
    allowed_keys = {
        "timestamp",
        "event",
        "problem_id",
        "duration_seconds",
        "infrastructure_error_type",
        "exit_code",
        "stdout_bytes",
        "stderr_bytes",
        "stdout_sha256",
        "stderr_sha256",
        "cleanup_status",
        "ready",
        "candidate_execution",
        "record_count",
        "limit_bytes",
    }
    allowed_events = {
        "preflight_started",
        "preflight_completed",
        "task_completed",
        "batch_timeout",
        "batch_deadline_not_started",
        "container_cleanup_failed",
        "mock_dry_run_completed",
        "log_length_limit_reached",
        "task_reused_on_resume",
    }
    for record in records:
        timestamp = record.get("timestamp")
        if (
            not set(record).issubset(allowed_keys)
            or not isinstance(timestamp, str)
            or not _ISO_UTC_PATTERN.fullmatch(timestamp)
            or record.get("event") not in allowed_events
            or any(isinstance(value, Mapping | list) for value in record.values())
        ):
            raise EvalPlusExperimentError("resume execution log is invalid")
        problem_id = record.get("problem_id")
        if problem_id is not None and (
            not isinstance(problem_id, str)
            or not re.fullmatch(r"HumanEval/(?:0|[1-9][0-9]*)", problem_id)
        ):
            raise EvalPlusExperimentError("resume execution log is invalid")
        for field in ("stdout_sha256", "stderr_sha256"):
            value = record.get(field)
            if value is not None and (
                not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value)
            ):
                raise EvalPlusExperimentError("resume execution log is invalid")
        infrastructure_error_type = record.get("infrastructure_error_type")
        if (
            infrastructure_error_type is not None
            and infrastructure_error_type not in INFRASTRUCTURE_ERROR_TYPES
        ):
            raise EvalPlusExperimentError("resume execution log is invalid")
        cleanup_status = record.get("cleanup_status")
        if cleanup_status is not None and cleanup_status not in {
            "not_needed",
            "removed",
            "not_found",
            "failed",
        }:
            raise EvalPlusExperimentError("resume execution log is invalid")
        for field in (
            "duration_seconds",
            "exit_code",
            "stdout_bytes",
            "stderr_bytes",
            "record_count",
            "limit_bytes",
        ):
            value = record.get(field)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(float(value))
                or value < 0
            ):
                raise EvalPlusExperimentError("resume execution log is invalid")
        for field in ("ready", "candidate_execution"):
            value = record.get(field)
            if value is not None and not isinstance(value, bool):
                raise EvalPlusExperimentError("resume execution log is invalid")
    return records


def _raw_bundle(
    raw_by_problem: Mapping[str, Mapping[str, Any]], expected_ids: Sequence[str]
) -> dict:
    return {
        "schema_version": 1,
        "kind": RAW_BUNDLE_KIND,
        "raw_results": [
            raw_by_problem[problem_id]
            for problem_id in expected_ids
            if problem_id in raw_by_problem
        ],
    }


def _mock_raw_bundle(expected_ids: Sequence[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": RAW_MOCK_BUNDLE_KIND,
        "execution_performed": False,
        "problem_ids": list(expected_ids),
    }


def _mock_safe_result(
    exported: ValidatedSampleExport,
    problem_id: str,
    *,
    run_id: str,
    timestamp: str,
) -> dict[str, Any]:
    reference = exported.reference_for(problem_id)
    return {
        "schema_version": 1,
        "run_id": run_id,
        "problem_id": problem_id,
        "base_status": None,
        "plus_status": None,
        "base_fail_test_count": 0,
        "plus_fail_test_count": 0,
        "passed_base": False,
        "passed_plus": False,
        "error_type": "mock_not_executed",
        "infrastructure_status": "mocked",
        "solution_sha256": reference.code_sha256,
        "official_override_hash": None,
        "duration_seconds": 0.0,
        "started_at": timestamp,
        "ended_at": timestamp,
        "failure_count_scope": "not_applicable_mock",
        "source_response": _source_reference(exported, problem_id),
    }


def _enrich_safe_result(
    safe_result: Mapping[str, Any],
    exported: ValidatedSampleExport,
    outcome: ExecutorTaskOutcome,
    *,
    run_id: str,
) -> dict[str, Any]:
    infrastructure_status = safe_result.get("infrastructure_status")
    return {
        "schema_version": 1,
        "run_id": run_id,
        **dict(safe_result),
        "duration_seconds": outcome.duration_seconds,
        "started_at": outcome.started_at,
        "ended_at": outcome.ended_at,
        "failure_count_scope": (
            "recorded_by_evalplus_test_details"
            if infrastructure_status == "ok"
            else "not_applicable_infrastructure"
        ),
        "source_response": _source_reference(exported, outcome.problem_id),
    }


def _validated_existing_results(
    paths: _RunPaths,
    exported: ValidatedSampleExport,
    *,
    run_id: str,
) -> dict[str, dict[str, Any]]:
    if not paths.results.exists():
        return {}
    expected_ids = {sample.task_id for sample in exported.samples}
    records = _read_jsonl(paths.results, label="phase-two results.jsonl")
    by_problem: dict[str, dict[str, Any]] = {}
    for record in records:
        if set(record) != _RESULT_FIELDS:
            raise EvalPlusExperimentError("resume result schema is invalid")
        problem_id = record.get("problem_id")
        if (
            not isinstance(problem_id, str)
            or problem_id not in expected_ids
            or problem_id in by_problem
        ):
            raise EvalPlusExperimentError(
                "resume results contain an invalid or duplicate problem_id"
            )
        source = record.get("source_response")
        if source != _source_reference(exported, problem_id):
            raise EvalPlusExperimentError("resume result source reference changed")
        duration = record.get("duration_seconds")
        if duration is not None and (
            isinstance(duration, bool)
            or not isinstance(duration, int | float)
            or not math.isfinite(float(duration))
            or duration < 0
        ):
            raise EvalPlusExperimentError("resume result duration is invalid")
        infrastructure_status = record.get("infrastructure_status")
        expected_failure_scope = {
            "ok": "recorded_by_evalplus_test_details",
            "mocked": "not_applicable_mock",
            "error": "not_applicable_infrastructure",
        }.get(str(infrastructure_status))
        if (
            record.get("schema_version") != 1
            or record.get("run_id") != run_id
            or record.get("failure_count_scope") != expected_failure_scope
            or not isinstance(record.get("started_at"), str)
            or not _ISO_UTC_PATTERN.fullmatch(str(record.get("started_at")))
            or not isinstance(record.get("ended_at"), str)
            or not _ISO_UTC_PATTERN.fullmatch(str(record.get("ended_at")))
        ):
            raise EvalPlusExperimentError("resume result identity is invalid")
        expected_code_hash = exported.reference_for(problem_id).code_sha256
        if infrastructure_status in {"ok", "mocked"}:
            if record.get("solution_sha256") != expected_code_hash:
                raise EvalPlusExperimentError("resume result solution fingerprint changed")
        elif infrastructure_status == "error":
            error_type = record.get("error_type")
            try:
                expected_infrastructure = infrastructure_error_result(
                    problem_id,
                    error_type=str(error_type),
                )
            except EvalPlusParseError:
                raise EvalPlusExperimentError("resume infrastructure result is invalid") from None
            for field, expected_value in expected_infrastructure.items():
                if record.get(field) != expected_value:
                    raise EvalPlusExperimentError("resume infrastructure result is invalid")
        else:
            raise EvalPlusExperimentError("resume result infrastructure status is invalid")
        by_problem[problem_id] = record
    return by_problem


def _validated_existing_raw(
    paths: _RunPaths,
    results_by_problem: dict[str, dict[str, Any]],
    exported: ValidatedSampleExport,
) -> dict[str, Mapping[str, Any]]:
    if not paths.raw_results.exists():
        for problem_id in list(results_by_problem):
            if results_by_problem[problem_id].get("infrastructure_status") == "ok":
                del results_by_problem[problem_id]
        return {}
    payload = _read_json(paths.raw_results, label="phase-two raw results")
    if payload.get("kind") == RAW_MOCK_BUNDLE_KIND:
        expected_problem_ids = [sample.task_id for sample in exported.samples]
        if (
            set(payload) != {"schema_version", "kind", "execution_performed", "problem_ids"}
            or payload.get("schema_version") != 1
            or payload.get("execution_performed") is not False
            or payload.get("problem_ids") != expected_problem_ids
            or any(
                record.get("infrastructure_status") != "mocked"
                for record in results_by_problem.values()
            )
        ):
            raise EvalPlusExperimentError("mock raw bundle differs from safe results")
        return {}
    if (
        set(payload) != {"schema_version", "kind", "raw_results"}
        or payload.get("schema_version") != 1
        or payload.get("kind") != RAW_BUNDLE_KIND
        or not isinstance(payload.get("raw_results"), list)
    ):
        raise EvalPlusExperimentError("resume raw result bundle identity is invalid")
    by_problem: dict[str, Mapping[str, Any]] = {}
    for document in payload["raw_results"]:
        if not isinstance(document, Mapping):
            raise EvalPlusExperimentError("resume raw bundle contains a non-object")
        evaluations = document.get("eval")
        if not isinstance(evaluations, Mapping) or len(evaluations) != 1:
            raise EvalPlusExperimentError("resume raw document shape is invalid")
        problem_id = next(iter(evaluations))
        if not isinstance(problem_id, str) or problem_id in by_problem:
            raise EvalPlusExperimentError("resume raw bundle contains duplicate task data")
        safe_record = results_by_problem.get(problem_id)
        if safe_record is None or safe_record.get("infrastructure_status") != "ok":
            raise EvalPlusExperimentError("resume raw bundle has no matching safe result")
        try:
            parsed = parse_official_result(
                document,
                expected_problem_id=problem_id,
                expected_solution_sha256=exported.reference_for(problem_id).code_sha256,
            )
        except EvalPlusParseError:
            raise EvalPlusExperimentError("resume raw result failed strict validation") from None
        for field in (
            "problem_id",
            "base_status",
            "plus_status",
            "base_fail_test_count",
            "plus_fail_test_count",
            "passed_base",
            "passed_plus",
            "error_type",
            "infrastructure_status",
            "solution_sha256",
            "official_override_hash",
        ):
            if safe_record.get(field) != parsed.get(field):
                raise EvalPlusExperimentError(
                    "resume safe result differs from its official raw result"
                )
        by_problem[problem_id] = document
    # A process can stop after atomically publishing results.jsonl but before
    # the corresponding raw bundle replacement.  Treat raw as authoritative
    # for completed official evaluations and rerun any unmatched safe record.
    for problem_id in list(results_by_problem):
        if (
            results_by_problem[problem_id].get("infrastructure_status") == "ok"
            and problem_id not in by_problem
        ):
            del results_by_problem[problem_id]
    return by_problem


def _write_checkpoints(
    paths: _RunPaths,
    expected_ids: Sequence[str],
    results_by_problem: Mapping[str, Mapping[str, Any]],
    raw_by_problem: Mapping[str, Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    *,
    execution_mode: str,
) -> None:
    ordered_results = [
        results_by_problem[problem_id]
        for problem_id in expected_ids
        if problem_id in results_by_problem
    ]
    _atomic_write_bytes(paths.results, _jsonl_bytes(ordered_results))
    if execution_mode == "mock":
        _atomic_write_json(paths.raw_results, _mock_raw_bundle(expected_ids))
    else:
        _atomic_write_json(paths.raw_results, _raw_bundle(raw_by_problem, expected_ids))
    _write_execution_log(paths.execution_log, events)


def _summary_for_run(
    results: Sequence[Mapping[str, Any]],
    expected_ids: Sequence[str],
    *,
    run_id: str,
    execution_mode: Literal["docker", "mock"],
    completed_at: str,
    reused_problem_ids: Sequence[str],
    exported: ValidatedSampleExport,
) -> dict[str, Any]:
    core = build_summary(
        results,
        expected_problem_ids=expected_ids,
        execution_mode=execution_mode,
    )
    reused = set(reused_problem_ids)
    current = [result for result in results if result.get("problem_id") not in reused]
    experiment_label, metrics_scope, limitations, cohort_description = _phase2_identity(exported)
    selection = exported.export_selection
    summary = {
        **core,
        "run_id": run_id,
        "experiment_label": experiment_label,
        "metrics_scope": metrics_scope if execution_mode == "docker" else "mock_dry_run_only",
        "completed_at": completed_at,
        "source_problem_count": selection.source_problem_count,
        "exported_success_count": selection.exported_success_count,
        "excluded_parse_error_count": selection.excluded_parse_error_count,
        "excluded_provider_error_count": selection.excluded_provider_error_count,
        "selection_policy": selection.selection_policy,
        "min_success_count": selection.min_success_count,
        "pipeline_coverage_rate": (
            selection.exported_success_count / selection.source_problem_count
            if selection.source_problem_count
            else None
        ),
        "limitations": limitations,
        "resume_skipped_count": len(reused),
        "current_invocation_official_result_count": sum(
            result.get("infrastructure_status") == "ok" for result in current
        ),
        "current_invocation_infrastructure_error_count": sum(
            result.get("infrastructure_status") == "error" for result in current
        ),
    }
    description_field = (
        "pilot_description" if exported.dataset.selection_role == "pilot" else "cohort_description"
    )
    summary[description_field] = cohort_description
    return summary


def _finalize_manifest(
    manifest: dict[str, Any],
    paths: _RunPaths,
    summary: Mapping[str, Any],
    *,
    completed_at: str,
) -> dict[str, Any]:
    manifest["status"] = "completed"
    manifest["completed_at"] = completed_at
    invocations = manifest.get("invocations")
    if isinstance(invocations, list) and invocations and isinstance(invocations[-1], dict):
        invocations[-1]["status"] = "completed"
        invocations[-1]["completed_at"] = completed_at
    manifest["output"] = {
        "samples_sha256": _sha256_file(paths.samples),
        "raw_results_sha256": _sha256_file(paths.raw_results),
        "results_sha256": _sha256_file(paths.results),
        "summary_sha256": _sha256_bytes(_json_bytes(summary)),
        "execution_log_sha256": _sha256_file(paths.execution_log),
        "result_count": summary.get("result_count"),
    }
    return manifest


def _run_one_task(
    executor: EvalPlusExecutor,
    sample: EvalPlusSample,
    task_metadata: HumanEvalPlusTaskMetadata,
    run_dir: Path,
) -> ExecutorTaskOutcome:
    with tempfile.TemporaryDirectory(prefix=".evalplus-task-", dir=run_dir) as temporary:
        workspace = Path(temporary)
        os.chmod(workspace, 0o700)
        return executor.run_task(
            sample=sample,
            task_metadata=task_metadata,
            workspace=workspace,
        )


def _outcome_from_future(
    future: Future[ExecutorTaskOutcome],
    sample: EvalPlusSample,
) -> ExecutorTaskOutcome:
    try:
        return future.result()
    except Exception:
        now = _iso_utc(_utc_now())
        return ExecutorTaskOutcome(
            problem_id=sample.task_id,
            started_at=now,
            ended_at=now,
            duration_seconds=0.0,
            raw_result=None,
            infrastructure_error_type="executor_error",
            diagnostics={},
        )


def _record_task_outcome(
    outcome: ExecutorTaskOutcome,
    sample: EvalPlusSample,
    exported: ValidatedSampleExport,
    results_by_problem: dict[str, dict[str, Any]],
    raw_by_problem: dict[str, Mapping[str, Any]],
    events: list[dict[str, Any]],
    *,
    run_id: str,
    event: str = "task_completed",
) -> None:
    if outcome.problem_id != sample.task_id:
        raise EvalPlusExperimentError("executor returned a mismatched problem_id")
    if outcome.infrastructure_error_type is not None or outcome.raw_result is None:
        error_type = outcome.infrastructure_error_type or "missing_raw_result"
        safe = infrastructure_error_result(sample.task_id, error_type=error_type)
    else:
        try:
            safe = parse_official_result(
                outcome.raw_result,
                expected_problem_id=sample.task_id,
                expected_solution_sha256=exported.reference_for(sample.task_id).code_sha256,
            )
        except EvalPlusParseError:
            safe = infrastructure_error_result(
                sample.task_id,
                error_type="invalid_raw_result",
            )
        else:
            raw_by_problem[sample.task_id] = outcome.raw_result
    results_by_problem[sample.task_id] = _enrich_safe_result(
        safe,
        exported,
        outcome,
        run_id=run_id,
    )
    events.append(
        _safe_diagnostic_event(
            event,
            problem_id=sample.task_id,
            outcome=outcome,
        )
    )


def _request_executor_cleanup(
    executor: EvalPlusExecutor,
) -> tuple[dict[str, str], bool]:
    """Request cleanup and validate only container-name/status metadata."""

    cancel_all = getattr(executor, "cancel_all", None)
    if not callable(cancel_all):
        return {}, False
    try:
        value = cancel_all()
    except Exception:
        return {}, True
    if not isinstance(value, Mapping):
        return {}, True
    statuses: dict[str, str] = {}
    invalid = False
    for container_name, cleanup_status in value.items():
        if (
            not isinstance(container_name, str)
            or not container_name
            or not isinstance(cleanup_status, str)
            or cleanup_status not in _CLEANUP_STATUSES
        ):
            invalid = True
            continue
        statuses[container_name] = str(cleanup_status)
    return statuses, invalid or any(status == "failed" for status in statuses.values())


def _synthetic_deadline_outcome(
    sample: EvalPlusSample,
    *,
    timestamp: str,
    error_type: str,
    cleanup_status: str | None = None,
) -> ExecutorTaskOutcome:
    diagnostics = {"cleanup_status": cleanup_status} if cleanup_status in _CLEANUP_STATUSES else {}
    return ExecutorTaskOutcome(
        problem_id=sample.task_id,
        started_at=timestamp,
        ended_at=timestamp,
        duration_seconds=0.0 if error_type == "batch_deadline_not_started" else None,
        raw_result=None,
        infrastructure_error_type=error_type,
        diagnostics=diagnostics,
    )


def _aggregate_cleanup_status(statuses: Mapping[str, str]) -> str | None:
    for status in ("failed", "removed", "not_found", "not_needed"):
        if status in statuses.values():
            return status
    return None


def _execute_pending(
    executor: EvalPlusExecutor,
    exported: ValidatedSampleExport,
    paths: _RunPaths,
    results_by_problem: dict[str, dict[str, Any]],
    raw_by_problem: dict[str, Mapping[str, Any]],
    events: list[dict[str, Any]],
    *,
    run_id: str,
    max_workers: int,
    batch_timeout_seconds: float,
) -> None:
    task_metadata = _task_metadata_by_id(exported)
    pending = [
        sample
        for sample in exported.samples
        if results_by_problem.get(sample.task_id, {}).get("infrastructure_status")
        not in {"ok", "mocked"}
    ]
    if not pending:
        return

    executor_pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="evalplus")
    futures: dict[Future[ExecutorTaskOutcome], EvalPlusSample] = {}
    batch_deadline_reached = False
    try:
        for sample in pending:
            future = executor_pool.submit(
                _run_one_task,
                executor,
                sample,
                task_metadata[sample.task_id],
                paths.run_dir,
            )
            futures[future] = sample
        try:
            completed = as_completed(futures, timeout=batch_timeout_seconds)
            for future in completed:
                sample = futures[future]
                _record_task_outcome(
                    _outcome_from_future(future, sample),
                    sample,
                    exported,
                    results_by_problem,
                    raw_by_problem,
                    events,
                    run_id=run_id,
                )
                _write_checkpoints(
                    paths,
                    [item.task_id for item in exported.samples],
                    results_by_problem,
                    raw_by_problem,
                    events,
                    execution_mode=executor.mode,
                )
            # A Docker task retains its active name when cleanup failed so a
            # final bounded pass can retry it after every worker has reported.
            # The task's recorded outcome remains ``container_cleanup_failed``
            # even if this conservative retry later succeeds.
            _request_executor_cleanup(executor)
        except TimeoutError:
            batch_deadline_reached = True
            now = _iso_utc(_utc_now())
            # Cancel queued work before asking the concrete executor to stop
            # active containers.  Docker's implementation tracks container
            # names and force-removes them; a fake executor may omit this API.
            for future, sample in futures.items():
                if sample.task_id not in results_by_problem:
                    future.cancel()
            cleanup_statuses, cleanup_failed = _request_executor_cleanup(executor)
            running = {
                future
                for future, sample in futures.items()
                if sample.task_id not in results_by_problem and not future.cancelled()
            }
            completed_during_cleanup: set[Future[ExecutorTaskOutcome]] = set()
            if running:
                completed_during_cleanup, _unconfirmed = wait(
                    running,
                    timeout=_BATCH_CLEANUP_GRACE_SECONDS,
                )
            aggregate_cleanup = _aggregate_cleanup_status(cleanup_statuses)
            for future, sample in futures.items():
                if sample.task_id in results_by_problem:
                    continue
                if future.cancelled():
                    _record_task_outcome(
                        _synthetic_deadline_outcome(
                            sample,
                            timestamp=now,
                            error_type="batch_deadline_not_started",
                        ),
                        sample,
                        exported,
                        results_by_problem,
                        raw_by_problem,
                        events,
                        run_id=run_id,
                        event="batch_deadline_not_started",
                    )
                    continue
                if future in completed_during_cleanup and not cleanup_failed:
                    _record_task_outcome(
                        _outcome_from_future(future, sample),
                        sample,
                        exported,
                        results_by_problem,
                        raw_by_problem,
                        events,
                        run_id=run_id,
                    )
                    continue
                synthetic = _synthetic_deadline_outcome(
                    sample,
                    timestamp=now,
                    error_type="container_cleanup_failed",
                    cleanup_status=("failed" if cleanup_failed else aggregate_cleanup),
                )
                _record_task_outcome(
                    synthetic,
                    sample,
                    exported,
                    results_by_problem,
                    raw_by_problem,
                    events,
                    run_id=run_id,
                    event="container_cleanup_failed",
                )
            # A worker can enter its own failed cleanup only after the first
            # cancellation snapshot.  Retry every retained target once more
            # after the bounded worker grace; recorded outcomes stay
            # conservative even when this final removal succeeds.
            _request_executor_cleanup(executor)
            _write_checkpoints(
                paths,
                [item.task_id for item in exported.samples],
                results_by_problem,
                raw_by_problem,
                events,
                execution_mode=executor.mode,
            )
    except BaseException:
        batch_deadline_reached = True
        for future in futures:
            future.cancel()
        _request_executor_cleanup(executor)
        running = {future for future in futures if not future.done()}
        if running:
            try:
                wait(running, timeout=_BATCH_CLEANUP_GRACE_SECONDS)
            except Exception:
                pass
        _request_executor_cleanup(executor)
        raise
    finally:
        executor_pool.shutdown(wait=not batch_deadline_reached, cancel_futures=True)


class MockEvalPlusExecutor:
    """Deterministic no-execution executor for artifact-path dry runs."""

    mode: Literal["mock"] = "mock"

    def public_identity(self) -> Mapping[str, Any]:
        return {
            "name": "tracejudge_evalplus_mock_no_execution",
            "version": 1,
            "candidate_execution": False,
            "network_access": False,
        }

    def preflight(
        self,
        *,
        task_metadata: Sequence[HumanEvalPlusTaskMetadata],
        workspace: Path,
    ) -> ExecutorPreflight:
        del workspace
        return ExecutorPreflight(
            ready=True,
            runtime={
                "name": "mock",
                "candidate_execution": False,
                "task_count": len(task_metadata),
            },
        )

    def run_task(
        self,
        *,
        sample: EvalPlusSample,
        task_metadata: HumanEvalPlusTaskMetadata,
        workspace: Path,
    ) -> ExecutorTaskOutcome:
        # Deliberately do not inspect, import, compile, or execute solution.
        del task_metadata, workspace
        now = _iso_utc(_utc_now())
        return ExecutorTaskOutcome(
            problem_id=sample.task_id,
            started_at=now,
            ended_at=now,
            duration_seconds=0.0,
            raw_result=None,
            infrastructure_error_type=None,
            diagnostics={"exit_code": 0},
        )


def run_evalplus_experiment(
    baseline_run_dir: str | Path,
    dataset_manifest_path: str | Path,
    output_dir: str | Path,
    *,
    executor: EvalPlusExecutor,
    run_id: str | None = None,
    resume: bool = False,
    max_workers: int = 2,
    per_task_timeout_seconds: float = 180.0,
    batch_timeout_seconds: float = 900.0,
    selection_policy: str = "all",
    min_success_count: int = 30,
) -> EvalPlusRunResult:
    """Validate phase one, then run only the isolated injected executor.

    All provenance and sample checks happen before ``executor.preflight`` can
    start Docker.  ``resume=True`` requires the exact same source artifacts,
    candidate bytes, implementation, executor/image identity, and limits.
    """

    if selection_policy not in {"all", "phase1-success-only"}:
        raise EvalPlusExperimentError("selection_policy must be 'all' or 'phase1-success-only'")
    if (
        isinstance(min_success_count, bool)
        or not isinstance(min_success_count, int)
        or min_success_count < 0
    ):
        raise EvalPlusExperimentError("min_success_count must be a non-negative integer")
    if selection_policy == "phase1-success-only" and min_success_count < 1:
        raise EvalPlusExperimentError(
            "phase1-success-only requires min_success_count to be at least one"
        )
    if max_workers < 1 or max_workers > 16:
        raise EvalPlusExperimentError("max_workers must be between 1 and 16")
    for value, label in (
        (per_task_timeout_seconds, "per-task timeout"),
        (batch_timeout_seconds, "batch timeout"),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(float(value))
            or value <= 0
        ):
            raise EvalPlusExperimentError(f"{label} must be a positive finite number")
    if batch_timeout_seconds < per_task_timeout_seconds:
        raise EvalPlusExperimentError("batch timeout must be at least the per-task timeout")

    # This is the complete static consistency boundary.  It performs no writes
    # and receives no executor object, so it cannot start Docker or run code.
    exported = load_validated_phase1_export(
        baseline_run_dir,
        dataset_manifest_path,
        selection_policy=cast(SelectionPolicy, selection_policy),
        min_success_count=min_success_count,
    )
    export_selection = exported.export_selection
    if (
        export_selection.selection_policy != selection_policy
        or export_selection.min_success_count != min_success_count
        or export_selection.source_problem_count != len(exported.dataset.selected_problem_ids)
        or export_selection.exported_success_count != len(exported.samples)
        or export_selection.exported_success_count
        + export_selection.excluded_parse_error_count
        + export_selection.excluded_provider_error_count
        != export_selection.source_problem_count
    ):
        raise EvalPlusExperimentError("phase-one export selection accounting is inconsistent")
    if (
        selection_policy == "phase1-success-only"
        and export_selection.exported_success_count < min_success_count
    ):
        raise EvalPlusExperimentError("phase-one export is below min_success_count")

    effective_run_id = run_id or new_evalplus_run_id()
    _validate_run_id(effective_run_id)
    paths = _run_paths(output_dir, effective_run_id)
    _require_non_trackable_run_directory(paths.run_dir)
    bootstrap_resume = False
    if resume:
        if run_id is None:
            raise EvalPlusExperimentError("resume requires an explicit run_id")
        if paths.run_dir.is_symlink() or not paths.run_dir.is_dir():
            raise EvalPlusExperimentError("resume run directory is missing or unsafe")
        if not paths.manifest.exists():
            entries = list(paths.run_dir.iterdir())
            if any(
                entry.name != paths.samples.name or entry.is_symlink() or not entry.is_file()
                for entry in entries
            ):
                raise EvalPlusExperimentError(
                    "resume bootstrap directory contains unexpected artifacts"
                )
            bootstrap_resume = True
    else:
        if paths.run_dir.exists() or paths.run_dir.is_symlink():
            raise EvalPlusExperimentError("phase-two run directory already exists")
        paths.run_dir.parent.mkdir(parents=True, exist_ok=True)
        paths.run_dir.mkdir(mode=0o700)
    os.chmod(paths.run_dir, 0o700)

    sample_bytes = serialize_samples_jsonl(exported.samples)
    if _sha256_bytes(sample_bytes) != exported.samples_sha256:
        raise EvalPlusExperimentError("exported samples changed after static validation")
    if resume and paths.samples.exists():
        if (
            paths.samples.is_symlink()
            or not paths.samples.is_file()
            or stat.S_IMODE(paths.samples.stat().st_mode) != 0o600
            or _sha256_file(paths.samples) != exported.samples_sha256
        ):
            raise EvalPlusExperimentError("resume samples.jsonl differs from validated phase one")
    elif resume and not bootstrap_resume:
        raise EvalPlusExperimentError("resume samples.jsonl differs from validated phase one")
    else:
        _atomic_write_bytes(paths.samples, sample_bytes)

    resume_manifest: dict[str, Any] | None = None
    resume_results: dict[str, dict[str, Any]] | None = None
    resume_raw: dict[str, Mapping[str, Any]] | None = None
    prior_events: list[dict[str, Any]] = []
    if resume and not bootstrap_resume:
        resume_manifest = _read_json(paths.manifest, label="phase-two manifest")
        _validate_resume_before_preflight(
            resume_manifest,
            exported,
            executor,
            run_id=effective_run_id,
            max_workers=max_workers,
            per_task_timeout_seconds=per_task_timeout_seconds,
            batch_timeout_seconds=batch_timeout_seconds,
        )
        _validate_completed_output_integrity(
            resume_manifest,
            paths,
            expected_result_count=len(exported.samples),
        )
        resume_results = _validated_existing_results(
            paths,
            exported,
            run_id=effective_run_id,
        )
        resume_raw = _validated_existing_raw(paths, resume_results, exported)
        prior_events = _validated_existing_events(paths)

    reused_problem_ids = {
        problem_id
        for problem_id, record in (resume_results or {}).items()
        if record.get("infrastructure_status") in {"ok", "mocked"}
    }

    preflight_started = _iso_utc(_utc_now())
    pending_preflight = ExecutorPreflight(
        ready=False,
        runtime={"inspection_status": "pending"},
        infrastructure_error_type=None,
        diagnostics=None,
    )
    pending_identity = _manifest_identity(
        exported,
        executor,
        pending_preflight,
        max_workers=max_workers,
        per_task_timeout_seconds=per_task_timeout_seconds,
        batch_timeout_seconds=batch_timeout_seconds,
    )
    if resume_manifest is not None:
        assert resume_manifest is not None
        assert resume_results is not None
        assert resume_raw is not None
        manifest = _begin_resume_manifest(
            resume_manifest,
            started_at=preflight_started,
        )
        results_by_problem = resume_results
        raw_by_problem = resume_raw
    else:
        manifest = _new_manifest(
            effective_run_id,
            exported,
            executor,
            pending_identity,
            created_at=preflight_started,
            initial_resume=bootstrap_resume,
        )
        results_by_problem = {}
        raw_by_problem = {}
    # Persist a recoverable identity before a Docker image inspection or
    # dataset preflight can be interrupted.
    _atomic_write_json(paths.manifest, manifest)

    events: list[dict[str, Any]] = list(prior_events)
    for problem_id in sorted(reused_problem_ids):
        events.append(
            {
                "timestamp": preflight_started,
                "event": "task_reused_on_resume",
                "problem_id": problem_id,
            }
        )
    events.append({"timestamp": preflight_started, "event": "preflight_started"})
    try:
        with tempfile.TemporaryDirectory(
            prefix=".evalplus-preflight-", dir=paths.run_dir
        ) as temporary:
            preflight = executor.preflight(
                task_metadata=exported.task_metadata,
                workspace=Path(temporary),
            )
    except Exception:
        preflight = ExecutorPreflight(
            ready=False,
            runtime={"inspection_status": "unavailable"},
            infrastructure_error_type="executor_error",
            diagnostics={},
        )
    if (
        not isinstance(preflight, ExecutorPreflight)
        or not isinstance(preflight.ready, bool)
        or not isinstance(preflight.runtime, Mapping)
    ):
        preflight = ExecutorPreflight(
            ready=False,
            runtime={"inspection_status": "unavailable"},
            infrastructure_error_type="executor_error",
            diagnostics={},
        )
    events.append(
        {
            "timestamp": _iso_utc(_utc_now()),
            "event": "preflight_completed",
            "ready": preflight.ready,
            "infrastructure_error_type": preflight.infrastructure_error_type,
        }
    )
    identity = _manifest_identity(
        exported,
        executor,
        preflight,
        max_workers=max_workers,
        per_task_timeout_seconds=per_task_timeout_seconds,
        batch_timeout_seconds=batch_timeout_seconds,
    )

    manifest = _apply_preflight_to_manifest(
        manifest,
        identity=identity,
        preflight=preflight,
        resumed=resume_manifest is not None,
    )
    _atomic_write_json(paths.manifest, manifest)

    expected_ids = [sample.task_id for sample in exported.samples]
    if executor.mode == "mock":
        timestamp = _iso_utc(_utc_now())
        for problem_id in expected_ids:
            results_by_problem[problem_id] = _mock_safe_result(
                exported,
                problem_id,
                run_id=effective_run_id,
                timestamp=timestamp,
            )
        events.append(
            {
                "timestamp": timestamp,
                "event": "mock_dry_run_completed",
                "candidate_execution": False,
                "record_count": len(expected_ids),
            }
        )
        _write_checkpoints(
            paths,
            expected_ids,
            results_by_problem,
            raw_by_problem,
            events,
            execution_mode=executor.mode,
        )
    elif not preflight.ready:
        error_type = preflight.infrastructure_error_type or "executor_error"
        timestamp = _iso_utc(_utc_now())
        for problem_id in expected_ids:
            if problem_id in reused_problem_ids:
                continue
            safe = infrastructure_error_result(problem_id, error_type=error_type)
            synthetic = ExecutorTaskOutcome(
                problem_id=problem_id,
                started_at=timestamp,
                ended_at=timestamp,
                duration_seconds=0.0,
                raw_result=None,
                infrastructure_error_type=error_type,
                diagnostics=preflight.diagnostics or {},
            )
            results_by_problem[problem_id] = _enrich_safe_result(
                safe,
                exported,
                synthetic,
                run_id=effective_run_id,
            )
        _write_checkpoints(
            paths,
            expected_ids,
            results_by_problem,
            raw_by_problem,
            events,
            execution_mode=executor.mode,
        )
    else:
        _execute_pending(
            executor,
            exported,
            paths,
            results_by_problem,
            raw_by_problem,
            events,
            run_id=effective_run_id,
            max_workers=max_workers,
            batch_timeout_seconds=batch_timeout_seconds,
        )

    # Even a no-op resume publishes the new preflight/reuse audit events.
    _write_checkpoints(
        paths,
        expected_ids,
        results_by_problem,
        raw_by_problem,
        events,
        execution_mode=executor.mode,
    )

    ordered_results = [results_by_problem[problem_id] for problem_id in expected_ids]
    completed_at = _iso_utc(_utc_now())
    summary = _summary_for_run(
        ordered_results,
        expected_ids,
        run_id=effective_run_id,
        execution_mode=executor.mode,
        completed_at=completed_at,
        reused_problem_ids=sorted(reused_problem_ids),
        exported=exported,
    )
    _atomic_write_json(paths.summary, summary)
    manifest = _finalize_manifest(
        manifest,
        paths,
        summary,
        completed_at=completed_at,
    )
    _atomic_write_json(paths.manifest, manifest)
    return EvalPlusRunResult(
        run_id=effective_run_id,
        run_dir=paths.run_dir,
        manifest_path=paths.manifest,
        samples_path=paths.samples,
        raw_results_path=paths.raw_results,
        results_path=paths.results,
        summary_path=paths.summary,
        execution_log_path=paths.execution_log,
        manifest=manifest,
        summary=summary,
    )


__all__ = [
    "EvalPlusExecutor",
    "EvalPlusExperimentError",
    "EvalPlusRunResult",
    "ExecutorPreflight",
    "ExecutorTaskOutcome",
    "MockEvalPlusExecutor",
    "new_evalplus_run_id",
    "run_evalplus_experiment",
]
