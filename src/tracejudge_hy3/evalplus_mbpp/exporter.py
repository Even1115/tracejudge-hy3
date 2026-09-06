"""Export phase-one MBPP+ successes as phase-two candidate inputs.

The exporter binds three identities before writing anything: the phase-one
run manifest, the MBPP+ selection bundle manifest, and the bundle's public
projection.  Only ``success`` records contribute candidates, the latest
success per task wins, and the exported set must cover the selection exactly.
Candidate code is never imported, compiled, or executed here; the only static
check is a text-level entry-point presence probe.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tracejudge_hy3.baseline.runner import PHASE1_ARTIFACT_SCHEMA_VERSION
from tracejudge_hy3.dataset.loader import load_problems
from tracejudge_hy3.dataset.mbppplus import (
    DATASET_ID,
    FULL_PROJECTION_KIND,
    SELECTION_MANIFEST_KIND,
    validate_mbppplus_public_problems,
)
from tracejudge_hy3.exceptions import DatasetError

_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TASK_ID_PATTERN = re.compile(r"^Mbpp/(?:0|[1-9][0-9]*)$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_CANDIDATE_CODE_BYTES = 2 * 1024 * 1024
_ALLOWED_MANIFEST_KINDS = frozenset({FULL_PROJECTION_KIND, SELECTION_MANIFEST_KIND})


class MbppCandidateExportError(ValueError):
    """Raised when a phase-one run cannot be exported as MBPP+ candidates."""


@dataclass(frozen=True, slots=True)
class MbppCandidateExportResult:
    output_path: Path
    candidate_count: int
    candidates_sha256: str
    phase1_run_id: str
    selected_problem_ids: tuple[str, ...]


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MbppCandidateExportError(f"{label} not found: {path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise MbppCandidateExportError(f"{label} is not valid UTF-8 JSON: {path}") from None
    if not isinstance(payload, dict):
        raise MbppCandidateExportError(f"{label} must contain a JSON object: {path}")
    return payload


def _selection_identity(manifest_path: Path) -> tuple[dict[str, Any], list[str], str]:
    """Return (manifest, selected ids in order, manifest sha256) for a bundle."""

    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise MbppCandidateExportError(f"dataset manifest not found: {manifest_path}") from exc
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MbppCandidateExportError("dataset manifest is not valid UTF-8 JSON") from None
    if not isinstance(manifest, dict):
        raise MbppCandidateExportError("dataset manifest must contain a JSON object")
    if manifest.get("schema_version") != 1:
        raise MbppCandidateExportError("dataset manifest schema_version must be 1")
    if manifest.get("kind") not in _ALLOWED_MANIFEST_KINDS:
        raise MbppCandidateExportError("dataset manifest is not an MBPP+ projection or selection")
    if manifest.get("dataset_id") != DATASET_ID:
        raise MbppCandidateExportError("dataset manifest dataset_id is invalid")
    selection = manifest.get("selection")
    projection = manifest.get("public_projection")
    if not isinstance(selection, Mapping) or not isinstance(projection, Mapping):
        raise MbppCandidateExportError("dataset manifest is missing selection/projection blocks")
    selected = selection.get("selected_problem_ids")
    if (
        not isinstance(selected, list)
        or not selected
        or any(
            not isinstance(item, str) or not _TASK_ID_PATTERN.fullmatch(item) for item in selected
        )
        or len(selected) != len(set(selected))
    ):
        raise MbppCandidateExportError("dataset manifest selected_problem_ids are invalid")
    projection_hash = projection.get("sha256")
    if not isinstance(projection_hash, str) or not _SHA256_PATTERN.fullmatch(projection_hash):
        raise MbppCandidateExportError("dataset manifest projection hash is invalid")
    problems_path = manifest_path.parent / "problems.jsonl"
    try:
        problems_bytes = problems_path.read_bytes()
    except OSError as exc:
        raise MbppCandidateExportError("selection bundle problems.jsonl is missing") from exc
    if _sha256_bytes(problems_bytes) != projection_hash:
        raise MbppCandidateExportError("selection bundle projection does not match its manifest")
    try:
        problems = load_problems(problems_path)
        validate_mbppplus_public_problems(problems)
    except DatasetError as exc:
        raise MbppCandidateExportError(f"selection bundle projection is invalid: {exc}") from exc
    if [problem.problem_id for problem in problems] != list(selected):
        raise MbppCandidateExportError("selection bundle tasks differ from its selection")
    return manifest, list(selected), _sha256_bytes(manifest_bytes)


def _entry_point_by_task(manifest_path: Path) -> dict[str, str]:
    problems = load_problems(manifest_path.parent / "problems.jsonl")
    result: dict[str, str] = {}
    for problem in problems:
        entry_point = problem.function_name
        if not entry_point:
            raise MbppCandidateExportError("selection bundle is missing an entry point")
        result[problem.problem_id] = entry_point
    return result


def _validate_phase1_manifest(
    manifest: Mapping[str, Any],
    *,
    run_dir: Path,
    dataset_sha256: str,
    selection_manifest_sha256: str,
    selected_ids: list[str],
) -> str:
    if manifest.get("schema_version") != PHASE1_ARTIFACT_SCHEMA_VERSION:
        raise MbppCandidateExportError("phase-one run uses an unsupported artifact schema")
    run_id = manifest.get("run_id")
    if not isinstance(run_id, str) or not _RUN_ID_PATTERN.fullmatch(run_id):
        raise MbppCandidateExportError("phase-one run_id is invalid")
    if run_id != run_dir.name:
        raise MbppCandidateExportError("phase-one run_id does not match its directory")
    dataset = manifest.get("dataset")
    if not isinstance(dataset, Mapping):
        raise MbppCandidateExportError("phase-one manifest is missing its dataset block")
    if dataset.get("sha256") != dataset_sha256:
        raise MbppCandidateExportError(
            "phase-one run was generated from a different dataset snapshot"
        )
    provenance = dataset.get("provenance")
    if not isinstance(provenance, Mapping):
        raise MbppCandidateExportError("phase-one run has no MBPP+ dataset provenance")
    if provenance.get("kind") not in _ALLOWED_MANIFEST_KINDS:
        raise MbppCandidateExportError("phase-one provenance is not an MBPP+ selection")
    if provenance.get("manifest_sha256") != selection_manifest_sha256:
        raise MbppCandidateExportError(
            "phase-one provenance does not match the supplied dataset manifest"
        )
    provenance_selection = provenance.get("selection")
    if not isinstance(provenance_selection, Mapping) or provenance_selection.get(
        "selected_problem_ids"
    ) != list(selected_ids):
        raise MbppCandidateExportError("phase-one provenance selection differs from the bundle")
    return run_id


def _latest_success_code_by_task(
    responses_path: Path,
    *,
    selected_ids: list[str],
    entry_points: Mapping[str, str],
) -> dict[str, str]:
    selected_set = set(selected_ids)
    latest: dict[str, str] = {}
    try:
        stream = responses_path.open(encoding="utf-8")
    except OSError as exc:
        raise MbppCandidateExportError("phase-one responses.jsonl is missing") from exc
    with stream:
        for line_number, raw_line in enumerate(stream, start=1):
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                raise MbppCandidateExportError(
                    f"phase-one responses row {line_number} is not valid JSON"
                ) from None
            if not isinstance(record, dict):
                raise MbppCandidateExportError(
                    f"phase-one responses row {line_number} must be an object"
                )
            if record.get("status") != "success":
                continue
            problem_id = record.get("problem_id")
            if not isinstance(problem_id, str) or problem_id not in selected_set:
                continue
            trace = record.get("solution_trace")
            if not isinstance(trace, Mapping):
                raise MbppCandidateExportError(
                    f"phase-one success record for {problem_id} has no solution trace"
                )
            if trace.get("problem_id") != problem_id:
                raise MbppCandidateExportError(
                    f"phase-one solution trace identity mismatch for {problem_id}"
                )
            code = trace.get("code")
            if not isinstance(code, str) or not code.strip():
                raise MbppCandidateExportError(
                    f"phase-one success record for {problem_id} has empty code"
                )
            code_bytes = code.encode("utf-8")
            if len(code_bytes) > _MAX_CANDIDATE_CODE_BYTES:
                raise MbppCandidateExportError(
                    f"phase-one candidate for {problem_id} exceeds the size limit"
                )
            entry_point = entry_points[problem_id]
            if not re.search(rf"def\s+{re.escape(entry_point)}\s*\(", code):
                raise MbppCandidateExportError(
                    f"phase-one candidate for {problem_id} does not define its entry point"
                )
            latest[problem_id] = code
    missing = [problem_id for problem_id in selected_ids if problem_id not in latest]
    if missing:
        raise MbppCandidateExportError(
            f"phase-one run lacks success records for {len(missing)} selected tasks "
            f"(first missing: {', '.join(missing[:5])}); resume or regenerate before export"
        )
    return latest


def _serialize_candidates(
    *,
    selected_ids: list[str],
    code_by_task: Mapping[str, str],
    run_id: str,
) -> bytes:
    lines: list[bytes] = []
    for problem_id in selected_ids:
        candidate_id = f"{problem_id.replace('/', '_')}--{run_id}"
        row = {
            "task_id": problem_id,
            "candidate_id": candidate_id,
            "code": code_by_task[problem_id],
        }
        lines.append(
            json.dumps(row, ensure_ascii=False, allow_nan=False, sort_keys=True).encode("utf-8")
            + b"\n"
        )
    return b"".join(lines)


def _publish_output(path: Path, payload: bytes) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_file() and path.read_bytes() == payload:
            os.chmod(path, 0o600)
            return
        raise MbppCandidateExportError(
            f"candidates output already exists with different contents: {path}"
        )
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise MbppCandidateExportError(f"cannot publish candidates file: {path}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def export_mbpp_candidates(
    *,
    phase1_run_dir: str | Path,
    dataset_manifest_path: str | Path,
    output_path: str | Path,
) -> MbppCandidateExportResult:
    """Export phase-one successes as a phase-two ``candidates.jsonl``.

    The export is strict: the run must bind the supplied selection bundle, and
    every selected task needs a success record.  No candidate code is executed
    or imported; only a text-level entry-point probe is applied.
    """

    run_dir = Path(phase1_run_dir).expanduser().resolve()
    manifest_path = Path(dataset_manifest_path).expanduser().resolve()
    _manifest, selected_ids, selection_manifest_sha256 = _selection_identity(manifest_path)
    phase1_manifest = _read_json_object(run_dir / "manifest.json", label="phase-one manifest")
    problems_bytes = (manifest_path.parent / "problems.jsonl").read_bytes()
    run_id = _validate_phase1_manifest(
        phase1_manifest,
        run_dir=run_dir,
        dataset_sha256=_sha256_bytes(problems_bytes),
        selection_manifest_sha256=selection_manifest_sha256,
        selected_ids=selected_ids,
    )
    entry_points = _entry_point_by_task(manifest_path)
    code_by_task = _latest_success_code_by_task(
        run_dir / "responses.jsonl",
        selected_ids=selected_ids,
        entry_points=entry_points,
    )
    payload = _serialize_candidates(
        selected_ids=selected_ids,
        code_by_task=code_by_task,
        run_id=run_id,
    )
    resolved_output = Path(output_path).expanduser().resolve()
    _publish_output(resolved_output, payload)
    return MbppCandidateExportResult(
        output_path=resolved_output,
        candidate_count=len(selected_ids),
        candidates_sha256=_sha256_bytes(payload),
        phase1_run_id=run_id,
        selected_problem_ids=tuple(selected_ids),
    )


__all__ = [
    "MbppCandidateExportError",
    "MbppCandidateExportResult",
    "export_mbpp_candidates",
]
