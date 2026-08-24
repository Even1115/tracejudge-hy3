"""Reproducible phase-one Solver generation runs.

The runner treats ``responses.jsonl`` as an append-only event log.  Appends are
implemented by atomically replacing the complete file so a process interrupt
can never expose a half-written JSON object.  A resumed run adds ``skipped``
events for previously successful problems and retries only non-successful ones.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, Literal, Protocol

from tracejudge_hy3.dataset.humanevalplus import (
    ADAPTER_NAME as HUMANEVALPLUS_ADAPTER_NAME,
)
from tracejudge_hy3.dataset.humanevalplus import (
    ADAPTER_VERSION as HUMANEVALPLUS_ADAPTER_VERSION,
)
from tracejudge_hy3.dataset.humanevalplus import (
    DATASET_ID as HUMANEVALPLUS_DATASET_ID,
)
from tracejudge_hy3.dataset.humanevalplus import (
    DATASET_SOURCE as HUMANEVALPLUS_DATASET_SOURCE,
)
from tracejudge_hy3.dataset.humanevalplus import (
    EXPECTED_RECORD_COUNT as HUMANEVALPLUS_RECORD_COUNT,
)
from tracejudge_hy3.dataset.humanevalplus import (
    FULL_EXPERIMENT_LABEL as HUMANEVALPLUS_FULL_LABEL,
)
from tracejudge_hy3.dataset.humanevalplus import (
    FULL_SELECTION_ALGORITHM as HUMANEVALPLUS_FULL_SELECTION,
)
from tracejudge_hy3.dataset.humanevalplus import (
    KNOWN_WITHHELD_FIELDS as HUMANEVALPLUS_WITHHELD_FIELDS,
)
from tracejudge_hy3.dataset.humanevalplus import (
    PILOT_EXPERIMENT_LABEL as HUMANEVALPLUS_PILOT_LABEL,
)
from tracejudge_hy3.dataset.humanevalplus import (
    PILOT_LIMITATIONS as HUMANEVALPLUS_PILOT_LIMITATIONS,
)
from tracejudge_hy3.dataset.humanevalplus import (
    SELECTION_ALGORITHM as HUMANEVALPLUS_SELECTION_ALGORITHM,
)
from tracejudge_hy3.dataset.humanevalplus import (
    ordered_problem_ids_sha256,
    select_humanevalplus_problem_ids,
    validate_humanevalplus_public_problems,
)
from tracejudge_hy3.dataset.loader import load_problems
from tracejudge_hy3.exceptions import DatasetError
from tracejudge_hy3.redaction import (
    is_sensitive_key as _is_sensitive_key,
)
from tracejudge_hy3.redaction import (
    redact_error_text as _redact_error_text,
)
from tracejudge_hy3.redaction import (
    redact_sensitive_text as _redact_text,
)
from tracejudge_hy3.schemas.problem import ProblemSpec

ResponseStatus = Literal["success", "parse_error", "provider_error", "skipped"]

_ALLOWED_PROVIDER_STATUSES = {"success", "parse_error", "provider_error"}
_DIRECT_DEPENDENCIES = ("pydantic", "pydantic-settings", "openai", "typer", "rich")
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_EXPERIMENT_LABEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class GenerationDetails(Protocol):
    """Structural type returned by phase-one-capable providers."""

    status: str
    raw_output: str | None
    solution: Any
    attempt_count: int
    retry_count: int
    error: Any
    raw_output_attempt: int | None
    parse_attempted: bool


class BaselineProvider(Protocol):
    """Minimal provider surface used by this module."""

    name: str

    async def generate_solution_with_details(self, problem: ProblemSpec) -> GenerationDetails: ...

    def public_generation_config(self) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


class BaselineExperimentError(ValueError):
    """Raised when a run cannot be created or safely resumed."""


@dataclass(frozen=True, slots=True)
class BaselineRunResult:
    """Paths and final summary returned after a completed baseline run."""

    run_id: str
    run_dir: Path
    manifest_path: Path
    responses_path: Path
    summary_path: Path
    manifest: dict[str, Any]
    summary: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _RunPaths:
    run_dir: Path
    manifest: Path
    responses: Path
    summary: Path


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_utc(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_baseline_run_id() -> str:
    """Return a collision-resistant phase-one run identifier."""

    timestamp = _utc_now().strftime("%Y%m%dT%H%M%S%fZ")
    return f"phase1_{timestamp}_{uuid.uuid4().hex[:12]}"


def _new_invocation_id() -> str:
    return uuid.uuid4().hex


def _validate_run_id(run_id: str) -> None:
    if not _RUN_ID_PATTERN.fullmatch(run_id):
        raise BaselineExperimentError(
            "run_id must contain only letters, digits, '.', '_' or '-' and be at most "
            "128 characters"
        )


def _run_paths(output_dir: str | Path, run_id: str) -> _RunPaths:
    base = Path(output_dir).expanduser().resolve()
    run_dir = base / run_id
    return _RunPaths(
        run_dir=run_dir,
        manifest=run_dir / "manifest.json",
        responses=run_dir / "responses.jsonl",
        summary=run_dir / "summary.json",
    )


def _fsync_directory(path: Path) -> None:
    """Best-effort directory sync after replace (unsupported on some platforms)."""

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


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Flush, fsync, and atomically replace ``path`` from its own directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
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
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        _fsync_directory(path.parent)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _json_bytes(payload: Any) -> bytes:
    safe_payload = _safe_json_value(payload, filter_sensitive_keys=False)
    return (
        json.dumps(
            safe_payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _atomic_write_json(path: Path, payload: Any) -> None:
    _atomic_write_bytes(path, _json_bytes(payload))


def _atomic_append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    """Atomically append one complete UTF-8 JSON line.

    Rewriting is intentionally preferred over a normal append: the pilot
    dataset is small, while interruption safety is an explicit experiment
    requirement.
    """

    existing = path.read_bytes() if path.exists() else b""
    if existing and not existing.endswith(b"\n"):
        raise BaselineExperimentError(f"responses file has an incomplete final line: {path}")
    safe_record = _safe_json_value(record, filter_sensitive_keys=False)
    line = json.dumps(
        safe_record,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    _atomic_write_bytes(path, existing + line + b"\n")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BaselineExperimentError(f"required run artifact is missing: {path}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BaselineExperimentError(f"invalid JSON artifact {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise BaselineExperimentError(f"expected a JSON object in {path}")
    return payload


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise BaselineExperimentError(f"required run artifact is missing: {path}")
    records: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line_number, raw_line in enumerate(stream, start=1):
                if not raw_line.strip():
                    continue
                value = json.loads(raw_line)
                if not isinstance(value, dict):
                    raise BaselineExperimentError(f"expected a JSON object at {path}:{line_number}")
                records.append(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BaselineExperimentError(f"invalid JSONL artifact {path}: {exc}") from exc
    return records


def _safe_json_value(value: Any, *, filter_sensitive_keys: bool = True) -> Any:
    """Convert a public value to JSON while recursively removing sensitive keys."""

    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else str(value)
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = _redact_text(str(raw_key))
            if filter_sensitive_keys and _is_sensitive_key(key):
                continue
            result[key] = _safe_json_value(item, filter_sensitive_keys=filter_sensitive_keys)
        return result
    if isinstance(value, set | frozenset):
        return sorted(
            (_safe_json_value(item, filter_sensitive_keys=filter_sensitive_keys) for item in value),
            key=str,
        )
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [
            _safe_json_value(item, filter_sensitive_keys=filter_sensitive_keys) for item in value
        ]
    return _redact_text(str(value))


def _public_provider_config(provider: BaselineProvider) -> dict[str, Any]:
    """Read only the provider's explicit public allowlist; never inspect its state."""

    raw_config = provider.public_generation_config()
    if not isinstance(raw_config, Mapping):
        raise BaselineExperimentError("provider.public_generation_config() must return a mapping")
    config = _safe_json_value(raw_config)
    assert isinstance(config, dict)
    config.setdefault("provider", _redact_text(str(getattr(provider, "name", "unknown"))))
    return config


def _dataset_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _count_values(values: Sequence[str]) -> dict[str, int]:
    return dict(sorted(Counter(_redact_text(value) for value in values).items()))


def _dataset_summary(problems: Sequence[ProblemSpec]) -> dict[str, Any]:
    return {
        "problem_count": len(problems),
        "sources": _count_values([problem.source for problem in problems]),
        "difficulties": _count_values([problem.difficulty for problem in problems]),
        "visible_tests": {
            "total_count": sum(len(problem.visible_test_cases) for problem in problems),
            "per_problem": {
                _redact_text(problem.problem_id): {
                    "count": len(problem.visible_test_cases),
                    "case_ids": [_redact_text(case.case_id) for case in problem.visible_test_cases],
                }
                for problem in problems
            },
        },
    }


def _experiment_label(problems: Sequence[ProblemSpec]) -> str:
    sources = {problem.source for problem in problems}
    if sources == {"self_constructed_mvp_fixture"}:
        return "self_constructed_mvp_fixture_pilot"
    return "phase1_baseline_generation"


def _manifest_text(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value or len(value) > 512:
        raise BaselineExperimentError(f"dataset manifest field {field!r} must be a safe string")
    return _redact_text(value)


def _manifest_sha256(payload: Mapping[str, Any], field: str) -> str:
    value = _manifest_text(payload, field)
    if not _SHA256_PATTERN.fullmatch(value):
        raise BaselineExperimentError(f"dataset manifest field {field!r} must be SHA256")
    return value


def _load_dataset_provenance(
    manifest_path: str | Path,
    *,
    dataset_hash: str,
    problems: Sequence[ProblemSpec],
) -> tuple[str, dict[str, Any]]:
    """Validate and allowlist a public-projection manifest for phase one.

    The baseline artifact stores identity metadata and cryptographic hashes,
    never arbitrary manifest fields or evaluation material.
    """

    resolved = Path(manifest_path).expanduser().resolve()
    try:
        manifest_bytes = resolved.read_bytes()
        payload = json.loads(manifest_bytes.decode("utf-8"))
    except FileNotFoundError as exc:
        raise BaselineExperimentError(f"dataset manifest not found: {resolved}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise BaselineExperimentError("dataset manifest is not valid UTF-8 JSON") from None
    if not isinstance(payload, Mapping):
        raise BaselineExperimentError("dataset manifest must contain a JSON object")
    if payload.get("schema_version") != 1:
        raise BaselineExperimentError("dataset manifest schema_version must be 1")

    experiment_label = _manifest_text(payload, "experiment_label")
    if not _EXPERIMENT_LABEL_PATTERN.fullmatch(experiment_label):
        raise BaselineExperimentError("dataset manifest experiment_label is invalid")
    if payload.get("metrics_scope") != "generation_and_parsing_only":
        raise BaselineExperimentError(
            "dataset manifest metrics_scope must be 'generation_and_parsing_only'"
        )
    kind = _manifest_text(payload, "kind")
    if kind not in {
        "tracejudge_humanevalplus_public_projection",
        "tracejudge_dataset_selection",
    }:
        raise BaselineExperimentError("dataset manifest kind is not supported by baseline")
    common_fields = {
        "schema_version",
        "kind",
        "experiment_label",
        "metrics_scope",
        "dataset_id",
        "source",
        "revision",
        "split",
        "license",
        "adapter",
        "source_manifest_sha256",
        "raw_snapshot",
        "public_projection",
        "selection",
        "withheld_fields",
    }
    expected_fields = (
        common_fields
        if kind == "tracejudge_humanevalplus_public_projection"
        else common_fields | {"parent_manifest_sha256", "limitations"}
    )
    if set(payload) != expected_fields:
        raise BaselineExperimentError("dataset manifest contains fields outside its schema")

    projection = payload.get("public_projection")
    selection = payload.get("selection")
    adapter = payload.get("adapter")
    raw_snapshot = payload.get("raw_snapshot")
    if not all(
        isinstance(item, Mapping) for item in (projection, selection, adapter, raw_snapshot)
    ):
        raise BaselineExperimentError("dataset manifest is missing structured provenance fields")
    assert isinstance(projection, Mapping)
    assert isinstance(selection, Mapping)
    assert isinstance(adapter, Mapping)
    assert isinstance(raw_snapshot, Mapping)
    if set(projection) != {
        "path",
        "sha256",
        "record_count",
        "ordered_problem_ids_sha256",
    }:
        raise BaselineExperimentError("dataset manifest projection fields are invalid")
    expected_selection_fields = (
        {"algorithm", "count", "selected_problem_ids"}
        if kind == "tracejudge_humanevalplus_public_projection"
        else {"algorithm", "seed", "count", "selected_problem_ids"}
    )
    if set(selection) != expected_selection_fields:
        raise BaselineExperimentError("dataset manifest selection fields are invalid")
    if set(raw_snapshot) != {"aggregate_sha256", "test_jsonl_sha256", "record_count"}:
        raise BaselineExperimentError("dataset manifest raw snapshot fields are invalid")

    try:
        validate_humanevalplus_public_problems(
            problems,
            require_complete_snapshot=(kind == "tracejudge_humanevalplus_public_projection"),
        )
    except DatasetError as exc:
        raise BaselineExperimentError(str(exc)) from None

    if _manifest_sha256(projection, "sha256") != dataset_hash:
        raise BaselineExperimentError(
            "dataset manifest public projection SHA256 differs from --dataset"
        )
    if projection.get("record_count") != len(problems):
        raise BaselineExperimentError(
            "dataset manifest public projection count differs from --dataset"
        )
    if projection.get("path") != "problems.jsonl":
        raise BaselineExperimentError("dataset manifest public projection path is invalid")
    problem_ids = [problem.problem_id for problem in problems]
    expected_order_hash = ordered_problem_ids_sha256(problem_ids)
    if _manifest_sha256(projection, "ordered_problem_ids_sha256") != expected_order_hash:
        raise BaselineExperimentError(
            "dataset manifest ordered problem ID hash differs from --dataset"
        )
    selected_ids = selection.get("selected_problem_ids")
    if selected_ids != problem_ids or selection.get("count") != len(problems):
        raise BaselineExperimentError(
            "dataset manifest selected problem IDs differ from --dataset order"
        )

    dataset_id = _manifest_text(payload, "dataset_id")
    revision = _manifest_text(payload, "revision")
    source = _manifest_text(payload, "source")
    license_name = _manifest_text(payload, "license")
    adapter_name = _manifest_text(adapter, "name")
    adapter_version = adapter.get("version")
    if dataset_id != HUMANEVALPLUS_DATASET_ID or source != HUMANEVALPLUS_DATASET_SOURCE:
        raise BaselineExperimentError("dataset manifest HumanEval+ identity is invalid")
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise BaselineExperimentError("dataset manifest HumanEval+ revision is invalid")
    if license_name != "apache-2.0" or payload.get("split") != "test":
        raise BaselineExperimentError("dataset manifest HumanEval+ split/license is invalid")
    if (
        adapter_name != HUMANEVALPLUS_ADAPTER_NAME
        or adapter_version != HUMANEVALPLUS_ADAPTER_VERSION
    ):
        raise BaselineExperimentError("dataset manifest HumanEval+ adapter identity is invalid")
    raw_aggregate = _manifest_sha256(raw_snapshot, "aggregate_sha256")
    raw_test_jsonl = _manifest_sha256(raw_snapshot, "test_jsonl_sha256")
    if raw_snapshot.get("record_count") != HUMANEVALPLUS_RECORD_COUNT:
        raise BaselineExperimentError("HumanEval+ raw snapshot must record 164 tasks")

    algorithm = _manifest_text(selection, "algorithm")
    seed = selection.get("seed")
    if kind == "tracejudge_humanevalplus_public_projection":
        expected_label = HUMANEVALPLUS_FULL_LABEL
        if len(problems) != HUMANEVALPLUS_RECORD_COUNT:
            raise BaselineExperimentError("full HumanEval+ projection must contain 164 tasks")
        if algorithm != HUMANEVALPLUS_FULL_SELECTION or seed is not None:
            raise BaselineExperimentError("full HumanEval+ selection identity is invalid")
    else:
        expected_label = (
            HUMANEVALPLUS_PILOT_LABEL
            if len(problems) == 10
            else f"humanevalplus_{len(problems)}_public_prompt_generation_pilot"
        )
        if algorithm != HUMANEVALPLUS_SELECTION_ALGORITHM or not isinstance(seed, int):
            raise BaselineExperimentError("HumanEval+ pilot selection identity is invalid")
        try:
            expected_selected_ids = list(
                select_humanevalplus_problem_ids(count=len(problems), seed=seed)
            )
        except DatasetError as exc:
            raise BaselineExperimentError(str(exc)) from None
        if problem_ids != expected_selected_ids:
            raise BaselineExperimentError(
                "HumanEval+ pilot selection does not match its deterministic seed"
            )
    if experiment_label != expected_label:
        raise BaselineExperimentError(
            "dataset manifest experiment label does not match its selection"
        )
    if kind == "tracejudge_dataset_selection" and payload.get("limitations") != list(
        HUMANEVALPLUS_PILOT_LIMITATIONS
    ):
        raise BaselineExperimentError("HumanEval+ pilot limitations identity is invalid")
    withheld_fields = payload.get("withheld_fields")
    if (
        not isinstance(withheld_fields, list)
        or not all(isinstance(field, str) and field for field in withheld_fields)
        or not {"canonical_solution", "test"} <= set(withheld_fields)
        or not set(withheld_fields) <= set(HUMANEVALPLUS_WITHHELD_FIELDS)
    ):
        raise BaselineExperimentError("dataset manifest withheld_fields must be a string list")

    source_manifest_hash = _manifest_sha256(payload, "source_manifest_sha256")
    parent_manifest_hash = (
        _manifest_sha256(payload, "parent_manifest_sha256")
        if kind == "tracejudge_dataset_selection"
        else None
    )

    identity: dict[str, Any] = {
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "kind": kind,
        "dataset_id": dataset_id,
        "revision": revision,
        "source": source,
        "license": license_name,
        "adapter": {"name": adapter_name, "version": adapter_version},
        "raw_snapshot": {
            "aggregate_sha256": raw_aggregate,
            "test_jsonl_sha256": raw_test_jsonl,
            "record_count": raw_snapshot.get("record_count"),
        },
        "public_projection": {
            "sha256": dataset_hash,
            "record_count": len(problems),
            "ordered_problem_ids_sha256": expected_order_hash,
        },
        "selection": {
            "algorithm": algorithm,
            "seed": seed,
            "count": len(problems),
            "selected_problem_ids": [_redact_text(problem_id) for problem_id in problem_ids],
        },
        "withheld_fields": sorted(_redact_text(field) for field in withheld_fields),
        "metrics_scope": "generation_and_parsing_only",
        "source_manifest_sha256": source_manifest_hash,
    }
    if parent_manifest_hash is not None:
        identity["parent_manifest_sha256"] = parent_manifest_hash
    return experiment_label, identity


def _git_command(repository_hint: Path, *arguments: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository_hint), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip()


def _git_command_bytes(repository_hint: Path, *arguments: str) -> bytes | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository_hint), *arguments],
            check=True,
            capture_output=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    return completed.stdout


def _working_tree_fingerprint(
    root: Path,
    status: bytes,
    pathspecs: Sequence[str],
) -> str | None:
    """Hash tracked diffs and untracked contents without persisting either."""

    if not status:
        return None
    diff = _git_command_bytes(
        root,
        "diff",
        "--binary",
        "--no-ext-diff",
        "HEAD",
        "--",
        *pathspecs,
    )
    untracked = _git_command_bytes(
        root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        *pathspecs,
    )
    if diff is None or untracked is None:
        return None

    digest = hashlib.sha256()
    digest.update(b"tracejudge-worktree-v1\0status\0")
    digest.update(status)
    digest.update(b"\0diff\0")
    digest.update(diff)
    for relative_bytes in sorted(item for item in untracked.split(b"\0") if item):
        digest.update(b"\0untracked\0")
        digest.update(relative_bytes)
        relative_path = Path(os.fsdecode(relative_bytes))
        candidate = root / relative_path
        try:
            if candidate.is_symlink():
                digest.update(b"\0symlink\0")
                digest.update(os.fsencode(os.readlink(candidate)))
            elif candidate.is_file():
                digest.update(b"\0file\0")
                with candidate.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            else:
                digest.update(b"\0non-file\0")
        except OSError:
            return None
    return digest.hexdigest()


def _git_metadata(
    repository_hint: Path,
    *,
    excluded_paths: Sequence[Path] = (),
) -> dict[str, Any]:
    root: str | None = None
    # Record the TraceJudge source checkout even when the dataset belongs to a
    # different repository; fall back to the current working tree, then data.
    source_checkout_hint = Path(__file__).resolve().parents[3]
    for candidate in (source_checkout_hint, Path.cwd(), repository_hint):
        root = _git_command(candidate, "rev-parse", "--show-toplevel")
        if root is not None:
            break
    if root is None:
        return {
            "available": False,
            "commit": None,
            "branch": None,
            "dirty": None,
            "working_tree_sha256": None,
        }
    root_path = Path(root)
    pathspecs = ["."]
    for excluded_path in excluded_paths:
        try:
            relative = excluded_path.resolve().relative_to(root_path.resolve())
        except ValueError:
            continue
        pathspecs.append(f":(exclude,literal){relative.as_posix()}")
    commit = _git_command(root_path, "rev-parse", "HEAD")
    branch = _git_command(root_path, "branch", "--show-current")
    status = _git_command_bytes(
        root_path,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        *pathspecs,
    )
    dirty = None if status is None else bool(status)
    return {
        "available": commit is not None,
        "commit": commit,
        "branch": branch or None,
        "dirty": dirty,
        "working_tree_sha256": (
            _working_tree_fingerprint(root_path, status, pathspecs) if status else None
        ),
    }


def _dependency_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for distribution_name in _DIRECT_DEPENDENCIES:
        try:
            versions[distribution_name] = metadata.version(distribution_name)
        except metadata.PackageNotFoundError:
            versions[distribution_name] = None
    return versions


def _environment_metadata() -> dict[str, Any]:
    try:
        project_version = metadata.version("tracejudge-hy3")
    except metadata.PackageNotFoundError:
        project_version = None
    return {
        "project": {"name": "tracejudge-hy3", "version": project_version},
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": str(Path(sys.executable).resolve()),
        },
        "direct_dependencies": _dependency_versions(),
    }


def _solution_payload(solution: Any) -> dict[str, Any] | None:
    if solution is None:
        return None
    if hasattr(solution, "model_dump"):
        payload = solution.model_dump(mode="json")
    elif isinstance(solution, Mapping):
        payload = dict(solution)
    elif is_dataclass(solution) and not isinstance(solution, type):
        payload = asdict(solution)
    else:
        raise TypeError("provider solution must be a Pydantic model, mapping, or dataclass")
    safe_payload = _safe_json_value(payload)
    if not isinstance(safe_payload, dict):
        raise TypeError("provider solution must serialize to a JSON object")
    return safe_payload


def _error_payload(error: Any, *, default_type: str, default_message: str) -> dict[str, str]:
    if isinstance(error, Mapping):
        error_type = error.get("type") or error.get("error_type") or default_type
        message = error.get("message") or error.get("detail") or default_message
    elif error is None:
        error_type = default_type
        message = default_message
    else:
        error_type = type(error).__name__
        message = str(error) or default_message
    return {
        "type": _redact_error_text(str(error_type)),
        "message": _redact_error_text(str(message)),
    }


def _parse_status(status: ResponseStatus, *, parse_attempted: bool) -> str:
    return {
        "success": "parsed",
        "parse_error": "failed",
        "provider_error": "failed" if parse_attempted else "not_attempted",
        "skipped": "not_attempted",
    }[status]


def _base_response(
    *,
    run_id: str,
    invocation_id: str,
    problem_id: str,
    provider_config: Mapping[str, Any],
    status: ResponseStatus,
    started_at: datetime,
    ended_at: datetime,
    duration_seconds: float,
    attempt_count: int,
    retry_count: int,
    raw_output_attempt: int | None,
    parse_attempted: bool,
    raw_output: str | None,
    solution_trace: dict[str, Any] | None,
    error: dict[str, str] | None,
) -> dict[str, Any]:
    provider_name = provider_config.get("provider", "unknown")
    return {
        "run_id": run_id,
        "invocation_id": invocation_id,
        "problem_id": _redact_text(problem_id),
        "provider": provider_name,
        "model": provider_config.get("model"),
        "status": status,
        "parse_status": _parse_status(status, parse_attempted=parse_attempted),
        "started_at": _iso_utc(started_at),
        "ended_at": _iso_utc(ended_at),
        "duration_seconds": round(max(0.0, duration_seconds), 6),
        "attempt_count": max(0, attempt_count),
        "retry_count": max(0, retry_count),
        "raw_output_attempt": raw_output_attempt,
        "parse_attempted": parse_attempted,
        "raw_output": raw_output,
        "solution_trace": solution_trace,
        "error_type": error["type"] if error is not None else None,
        "error": error,
    }


def _skipped_response(
    *,
    run_id: str,
    invocation_id: str,
    problem_id: str,
    provider_config: Mapping[str, Any],
) -> dict[str, Any]:
    now = _utc_now()
    return _base_response(
        run_id=run_id,
        invocation_id=invocation_id,
        problem_id=problem_id,
        provider_config=provider_config,
        status="skipped",
        started_at=now,
        ended_at=now,
        duration_seconds=0.0,
        attempt_count=0,
        retry_count=0,
        raw_output_attempt=None,
        parse_attempted=False,
        raw_output=None,
        solution_trace=None,
        error=None,
    )


def _generation_response(
    *,
    run_id: str,
    invocation_id: str,
    problem_id: str,
    provider_config: Mapping[str, Any],
    generation: GenerationDetails,
    started_at: datetime,
    ended_at: datetime,
    duration_seconds: float,
) -> dict[str, Any]:
    raw_status = str(generation.status)
    if raw_status not in _ALLOWED_PROVIDER_STATUSES:
        raise ValueError(f"provider returned unsupported generation status: {raw_status!r}")
    status: ResponseStatus = raw_status  # type: ignore[assignment]
    attempt_count = int(generation.attempt_count)
    retry_count = int(generation.retry_count)
    if attempt_count < 0 or retry_count < 0:
        raise ValueError("provider returned a negative attempt/retry count")

    raw_output = None
    if generation.raw_output is not None:
        raw_output = _redact_text(str(generation.raw_output))
    parse_attempted = bool(
        getattr(
            generation,
            "parse_attempted",
            status in {"success", "parse_error"} or raw_output is not None,
        )
    )
    if status in {"success", "parse_error"}:
        parse_attempted = True
    raw_output_attempt_value = getattr(generation, "raw_output_attempt", None)
    raw_output_attempt = (
        int(raw_output_attempt_value) if raw_output_attempt_value is not None else None
    )
    if raw_output is not None and raw_output_attempt is None and attempt_count > 0:
        raw_output_attempt = attempt_count
    if raw_output_attempt is not None and not 1 <= raw_output_attempt <= attempt_count:
        raise ValueError("provider returned an invalid raw_output_attempt")

    solution_trace = _solution_payload(generation.solution)
    error: dict[str, str] | None = None
    if status == "success":
        if solution_trace is None:
            status = "parse_error"
            parse_attempted = True
            error = _error_payload(
                None,
                default_type="MissingSolutionTrace",
                default_message="provider reported success without a parsed SolutionTrace",
            )
    else:
        error = _error_payload(
            generation.error,
            default_type="ParseError" if status == "parse_error" else "ProviderError",
            default_message=(
                "provider output could not be parsed"
                if status == "parse_error"
                else "provider generation failed"
            ),
        )
        # A failed result must not accidentally carry a stale parsed trace.
        solution_trace = None

    return _base_response(
        run_id=run_id,
        invocation_id=invocation_id,
        problem_id=problem_id,
        provider_config=provider_config,
        status=status,
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=duration_seconds,
        attempt_count=attempt_count,
        retry_count=retry_count,
        raw_output_attempt=raw_output_attempt,
        parse_attempted=parse_attempted,
        raw_output=raw_output,
        solution_trace=solution_trace,
        error=error,
    )


def _provider_exception_response(
    *,
    run_id: str,
    invocation_id: str,
    problem_id: str,
    provider_config: Mapping[str, Any],
    error: Exception,
    started_at: datetime,
    ended_at: datetime,
    duration_seconds: float,
) -> dict[str, Any]:
    return _base_response(
        run_id=run_id,
        invocation_id=invocation_id,
        problem_id=problem_id,
        provider_config=provider_config,
        status="provider_error",
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=duration_seconds,
        attempt_count=1,
        retry_count=0,
        raw_output_attempt=None,
        parse_attempted=False,
        raw_output=None,
        solution_trace=None,
        error=_error_payload(
            error,
            default_type="ProviderError",
            default_message="provider generation raised an exception",
        ),
    )


def _successful_problem_ids(records: Sequence[Mapping[str, Any]]) -> set[str]:
    return {
        str(record.get("problem_id"))
        for record in records
        if record.get("status") == "success" and record.get("problem_id") is not None
    }


def _summary_payload(
    *,
    run_id: str,
    experiment_label: str,
    problems: Sequence[ProblemSpec],
    records: Sequence[Mapping[str, Any]],
    invocation_id: str,
    invocation_started_at: datetime,
    invocation_completed_at: datetime | None,
) -> dict[str, Any]:
    problem_ids = {_redact_text(problem.problem_id) for problem in problems}
    status_counts = Counter(str(record.get("status", "unknown")) for record in records)
    invocation_records = [
        record for record in records if record.get("invocation_id") == invocation_id
    ]

    final_events: dict[str, Mapping[str, Any]] = {}
    for record in records:
        problem_id = record.get("problem_id")
        status = record.get("status")
        if problem_id in problem_ids and status in _ALLOWED_PROVIDER_STATUSES:
            final_events[str(problem_id)] = record

    final_outcome_counts = Counter(
        str(record.get("status", "provider_error")) for record in final_events.values()
    )
    success_count = final_outcome_counts["success"]
    parse_error_count = final_outcome_counts["parse_error"]
    provider_error_count = final_outcome_counts["provider_error"]
    failure_count = parse_error_count + provider_error_count
    final_parse_counts = Counter(
        str(record.get("parse_status", "not_attempted")) for record in final_events.values()
    )
    parsed_count = final_parse_counts["parsed"]
    parse_failed_count = final_parse_counts["failed"]
    parse_denominator = parsed_count + parse_failed_count
    durations = [float(record.get("duration_seconds", 0.0)) for record in final_events.values()]
    completed_or_updated = invocation_completed_at or _utc_now()
    invocation_status_counts = Counter(
        str(record.get("status", "unknown")) for record in invocation_records
    )
    return {
        "run_id": run_id,
        "experiment_label": experiment_label,
        "updated_at": _iso_utc(completed_or_updated),
        "completed_at": (
            _iso_utc(invocation_completed_at) if invocation_completed_at is not None else None
        ),
        "total_problem_count": len(problems),
        "dataset_problem_count": len(problems),
        "final_outcome_counts": {
            "success": success_count,
            "parse_error": parse_error_count,
            "provider_error": provider_error_count,
            "failure": failure_count,
        },
        "success_count": success_count,
        "parse_error_count": parse_error_count,
        "provider_error_count": provider_error_count,
        "failure_count": failure_count,
        "pending_count": len(problems) - len(final_events),
        "parse_attempted_count": parse_denominator,
        "parse_success_count": parsed_count,
        "parse_failure_count": parse_failed_count,
        "parse_success_rate": (parsed_count / parse_denominator if parse_denominator else None),
        "average_duration_seconds": sum(durations) / len(durations) if durations else None,
        "record_count": len(records),
        "record_status_counts": dict(sorted(status_counts.items())),
        # Retained as a concise alias for humans inspecting an artifact directly.
        "status_counts": dict(sorted(status_counts.items())),
        "invocation": {
            "invocation_id": invocation_id,
            "started_at": _iso_utc(invocation_started_at),
            "completed_at": (
                _iso_utc(invocation_completed_at) if invocation_completed_at is not None else None
            ),
            "status_counts": dict(sorted(invocation_status_counts.items())),
            "skipped_count": invocation_status_counts["skipped"],
        },
        "skipped_count": invocation_status_counts["skipped"],
        "metrics_scope": "generation_and_parsing_only",
    }


def _initial_manifest(
    *,
    run_id: str,
    dataset_path: Path,
    dataset_hash: str,
    dataset_provenance: Mapping[str, Any] | None,
    experiment_label: str,
    problems: Sequence[ProblemSpec],
    provider_config: Mapping[str, Any],
    git_metadata: Mapping[str, Any],
    environment_metadata: Mapping[str, Any],
    invocation_id: str,
    started_at: datetime,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "phase": "phase1_baseline_generation",
        "experiment_label": experiment_label,
        "run_id": run_id,
        "created_at": _iso_utc(started_at),
        "status": "running",
        "completed_at": None,
        "dataset": {
            "path": _redact_text(str(dataset_path)),
            "sha256": dataset_hash,
            **_dataset_summary(problems),
            **({"provenance": dict(dataset_provenance)} if dataset_provenance is not None else {}),
        },
        "git": dict(git_metadata),
        "environment": dict(environment_metadata),
        "provider_config": dict(provider_config),
        "invocations": [
            {
                "invocation_id": invocation_id,
                "started_at": _iso_utc(started_at),
                "resume": False,
                "status": "running",
                "completed_at": None,
                "git": dict(git_metadata),
                "environment": dict(environment_metadata),
            }
        ],
    }


def _validate_resume(
    *,
    manifest: Mapping[str, Any],
    run_id: str,
    dataset_hash: str,
    dataset_provenance: Mapping[str, Any] | None,
    experiment_label: str,
    provider_config: Mapping[str, Any],
    git_metadata: Mapping[str, Any],
    environment_metadata: Mapping[str, Any],
) -> None:
    if manifest.get("run_id") != run_id:
        raise BaselineExperimentError("run_id does not match the existing manifest")
    dataset = manifest.get("dataset")
    recorded_hash = dataset.get("sha256") if isinstance(dataset, Mapping) else None
    if recorded_hash != dataset_hash:
        raise BaselineExperimentError(
            "cannot resume: dataset SHA256 differs from the existing manifest"
        )
    recorded_provenance = dataset.get("provenance") if isinstance(dataset, Mapping) else None
    if recorded_provenance != (
        dict(dataset_provenance) if dataset_provenance is not None else None
    ):
        raise BaselineExperimentError(
            "cannot resume: dataset provenance manifest differs from the existing manifest"
        )
    if manifest.get("experiment_label") != experiment_label:
        raise BaselineExperimentError(
            "cannot resume: experiment label differs from the existing manifest"
        )
    if manifest.get("provider_config") != dict(provider_config):
        raise BaselineExperimentError(
            "cannot resume: provider public generation config differs from the existing manifest"
        )
    recorded_git = manifest.get("git")
    reproducible_git_fields = ("available", "commit", "dirty", "working_tree_sha256")
    recorded_git_identity = {
        field: recorded_git.get(field) if isinstance(recorded_git, Mapping) else None
        for field in reproducible_git_fields
    }
    current_git_identity = {field: git_metadata.get(field) for field in reproducible_git_fields}
    if recorded_git_identity != current_git_identity:
        raise BaselineExperimentError(
            "cannot resume: TraceJudge Git commit or working-tree fingerprint differs from "
            "the existing manifest"
        )
    if git_metadata.get("dirty") and git_metadata.get("working_tree_sha256") is None:
        raise BaselineExperimentError(
            "cannot resume a dirty TraceJudge checkout without a working-tree fingerprint"
        )
    if manifest.get("environment") != dict(environment_metadata):
        raise BaselineExperimentError(
            "cannot resume: Python or direct dependency versions differ from the manifest"
        )


def _record_resume_invocation(
    manifest: dict[str, Any],
    *,
    invocation_id: str,
    started_at: datetime,
    git_metadata: Mapping[str, Any],
    environment_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    updated = dict(manifest)
    raw_invocations = manifest.get("invocations", [])
    invocations: list[Any] = []
    for raw_invocation in raw_invocations if isinstance(raw_invocations, list) else []:
        if isinstance(raw_invocation, Mapping):
            previous = dict(raw_invocation)
            if previous.get("status") == "running":
                previous["status"] = "interrupted"
                previous["interrupted_at"] = _iso_utc(started_at)
            invocations.append(previous)
        else:
            invocations.append(raw_invocation)
    invocations.append(
        {
            "invocation_id": invocation_id,
            "started_at": _iso_utc(started_at),
            "resume": True,
            "status": "running",
            "completed_at": None,
            "git": dict(git_metadata),
            "environment": dict(environment_metadata),
        }
    )
    updated["invocations"] = invocations
    updated["status"] = "running"
    updated["completed_at"] = None
    return updated


def _complete_manifest(
    manifest: dict[str, Any], *, invocation_id: str, completed_at: datetime
) -> dict[str, Any]:
    updated = dict(manifest)
    completed_at_text = _iso_utc(completed_at)
    updated["status"] = "completed"
    updated["completed_at"] = completed_at_text
    raw_invocations = manifest.get("invocations", [])
    invocations: list[Any] = []
    for raw_invocation in raw_invocations if isinstance(raw_invocations, list) else []:
        if isinstance(raw_invocation, Mapping):
            invocation = dict(raw_invocation)
            if invocation.get("invocation_id") == invocation_id:
                invocation["status"] = "completed"
                invocation["completed_at"] = completed_at_text
            invocations.append(invocation)
        else:
            invocations.append(raw_invocation)
    updated["invocations"] = invocations
    return updated


async def _run_baseline_experiment_open_provider(
    dataset_path: str | Path,
    provider: BaselineProvider,
    output_dir: str | Path,
    run_id: str | None = None,
    resume: bool = False,
    dataset_manifest_path: str | Path | None = None,
) -> BaselineRunResult:
    """Generate and persist phase-one baselines without executing candidate code.

    Args:
        dataset_path: UTF-8 JSONL dataset accepted by ``load_problems``.
        provider: Provider exposing detailed generation results and a public
            non-sensitive configuration allowlist.
        output_dir: Parent directory under which ``<run_id>/`` is created.
        run_id: Optional explicit identifier.  Required when ``resume`` is true.
        resume: Resume an existing run, skipping every problem that has any
            historical ``success`` event.
        dataset_manifest_path: Optional, hash-bound public provenance manifest.

    Raises:
        BaselineExperimentError: If creation/resume validation is unsafe.
        BaseException: Interruptions are deliberately not converted to per-item
            failures; all records completed before the interruption remain valid.
    """

    if resume and run_id is None:
        raise BaselineExperimentError("run_id is required when resume=True")
    effective_run_id = run_id or new_baseline_run_id()
    _validate_run_id(effective_run_id)

    resolved_dataset_path = Path(dataset_path).expanduser().resolve()
    # Hash the exact input bytes independently of JSON parsing/normalization.
    try:
        dataset_hash = _dataset_sha256(resolved_dataset_path)
    except FileNotFoundError as exc:
        raise BaselineExperimentError(f"dataset file not found: {resolved_dataset_path}") from exc
    problems = load_problems(resolved_dataset_path)
    contains_humanevalplus = any(
        problem.source == HUMANEVALPLUS_DATASET_SOURCE for problem in problems
    )
    if contains_humanevalplus and dataset_manifest_path is None:
        raise BaselineExperimentError(
            "HumanEval+ public projections require --dataset-manifest before provider calls"
        )
    if dataset_manifest_path is None:
        experiment_label = _experiment_label(problems)
        dataset_provenance: dict[str, Any] | None = None
    else:
        experiment_label, dataset_provenance = _load_dataset_provenance(
            dataset_manifest_path,
            dataset_hash=dataset_hash,
            problems=problems,
        )
    paths = _run_paths(output_dir, effective_run_id)
    provider_config = _public_provider_config(provider)
    git_metadata = _git_metadata(
        resolved_dataset_path.parent,
        excluded_paths=(paths.run_dir,),
    )
    environment_metadata = _environment_metadata()
    invocation_id = _new_invocation_id()
    invocation_started_at = _utc_now()

    if resume:
        if not paths.run_dir.is_dir():
            raise BaselineExperimentError(f"cannot resume missing run directory: {paths.run_dir}")
        manifest = _read_json(paths.manifest)
        _validate_resume(
            manifest=manifest,
            run_id=effective_run_id,
            dataset_hash=dataset_hash,
            dataset_provenance=dataset_provenance,
            experiment_label=experiment_label,
            provider_config=provider_config,
            git_metadata=git_metadata,
            environment_metadata=environment_metadata,
        )
        records = _read_jsonl(paths.responses)
        manifest = _record_resume_invocation(
            manifest,
            invocation_id=invocation_id,
            started_at=invocation_started_at,
            git_metadata=git_metadata,
            environment_metadata=environment_metadata,
        )
        _atomic_write_json(paths.manifest, manifest)
    else:
        try:
            paths.run_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise BaselineExperimentError(
                f"run directory already exists; use resume=True to continue it: {paths.run_dir}"
            ) from exc
        manifest = _initial_manifest(
            run_id=effective_run_id,
            dataset_path=resolved_dataset_path,
            dataset_hash=dataset_hash,
            dataset_provenance=dataset_provenance,
            experiment_label=experiment_label,
            problems=problems,
            provider_config=provider_config,
            git_metadata=git_metadata,
            environment_metadata=environment_metadata,
            invocation_id=invocation_id,
            started_at=invocation_started_at,
        )
        _atomic_write_json(paths.manifest, manifest)
        _atomic_write_bytes(paths.responses, b"")
        records = []

    initial_summary = _summary_payload(
        run_id=effective_run_id,
        experiment_label=experiment_label,
        problems=problems,
        records=records,
        invocation_id=invocation_id,
        invocation_started_at=invocation_started_at,
        invocation_completed_at=None,
    )
    _atomic_write_json(paths.summary, initial_summary)
    successful_ids = _successful_problem_ids(records)

    for problem in problems:
        if _redact_text(problem.problem_id) in successful_ids:
            record = _skipped_response(
                run_id=effective_run_id,
                invocation_id=invocation_id,
                problem_id=problem.problem_id,
                provider_config=provider_config,
            )
        else:
            started_at = _utc_now()
            monotonic_started_at = time.perf_counter()
            try:
                generation = await provider.generate_solution_with_details(problem)
                ended_at = _utc_now()
                record = _generation_response(
                    run_id=effective_run_id,
                    invocation_id=invocation_id,
                    problem_id=problem.problem_id,
                    provider_config=provider_config,
                    generation=generation,
                    started_at=started_at,
                    ended_at=ended_at,
                    duration_seconds=time.perf_counter() - monotonic_started_at,
                )
            except Exception as exc:
                ended_at = _utc_now()
                record = _provider_exception_response(
                    run_id=effective_run_id,
                    invocation_id=invocation_id,
                    problem_id=problem.problem_id,
                    provider_config=provider_config,
                    error=exc,
                    started_at=started_at,
                    ended_at=ended_at,
                    duration_seconds=time.perf_counter() - monotonic_started_at,
                )

        _atomic_append_jsonl(paths.responses, record)
        records.append(record)
        current_summary = _summary_payload(
            run_id=effective_run_id,
            experiment_label=experiment_label,
            problems=problems,
            records=records,
            invocation_id=invocation_id,
            invocation_started_at=invocation_started_at,
            invocation_completed_at=None,
        )
        _atomic_write_json(paths.summary, current_summary)

    completed_at = _utc_now()
    summary = _summary_payload(
        run_id=effective_run_id,
        experiment_label=experiment_label,
        problems=problems,
        records=records,
        invocation_id=invocation_id,
        invocation_started_at=invocation_started_at,
        invocation_completed_at=completed_at,
    )
    _atomic_write_json(paths.summary, summary)
    manifest = _complete_manifest(
        manifest,
        invocation_id=invocation_id,
        completed_at=completed_at,
    )
    _atomic_write_json(paths.manifest, manifest)
    return BaselineRunResult(
        run_id=effective_run_id,
        run_dir=paths.run_dir,
        manifest_path=paths.manifest,
        responses_path=paths.responses,
        summary_path=paths.summary,
        manifest=manifest,
        summary=summary,
    )


async def run_baseline_experiment(
    dataset_path: str | Path,
    provider: BaselineProvider,
    output_dir: str | Path,
    run_id: str | None = None,
    resume: bool = False,
    dataset_manifest_path: str | Path | None = None,
) -> BaselineRunResult:
    """Run phase-one generation and always release the supplied provider.

    See :func:`_run_baseline_experiment_open_provider` for artifact and resume
    semantics.  Cleanup also covers dataset, manifest, and resume validation
    failures, not only model-call failures.
    """

    try:
        return await _run_baseline_experiment_open_provider(
            dataset_path=dataset_path,
            provider=provider,
            output_dir=output_dir,
            run_id=run_id,
            resume=resume,
            dataset_manifest_path=dataset_manifest_path,
        )
    finally:
        await provider.aclose()
