"""Validate phase-one artifacts and export minimal official EvalPlus samples.

This module is deliberately a pure, static input boundary.  It reads existing
artifacts, validates their complete provenance chain, and returns in-memory
objects.  It never creates an output path, imports candidate source, invokes a
provider, or starts an executor.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from tracejudge_hy3.dataset.humanevalplus import (
    ADAPTER_NAME,
    ADAPTER_VERSION,
    ALLOWED_SELECTION_ROLES,
    DATASET_ID,
    DATASET_MANIFEST_SCHEMA_VERSION,
    DATASET_SOURCE,
    EXPECTED_RECORD_COUNT,
    KNOWN_WITHHELD_FIELDS,
    PILOT_EXPERIMENT_LABEL,
    PILOT_LIMITATIONS,
    RESEARCH_NATURAL_DATASET_MANIFEST_SCHEMA_VERSION,
    RESEARCH_NATURAL_EXPERIMENT_LABEL,
    RESEARCH_NATURAL_LIMITATIONS,
    SELECTION_ALGORITHM,
    _safe_task_id_number,
    ordered_problem_ids_sha256,
    select_humanevalplus_problem_ids,
    validate_humanevalplus_public_problems,
)
from tracejudge_hy3.dataset.loader import load_problems
from tracejudge_hy3.exceptions import ConfigurationError, DatasetError
from tracejudge_hy3.redaction import redact_sensitive_text
from tracejudge_hy3.resources import data_path

from .schemas import (
    EvalPlusSample,
    HumanEvalPlusDatasetIdentity,
    HumanEvalPlusTaskMetadata,
    Phase1ExportSelectionIdentity,
    Phase1ResponseReference,
    Phase1SourceIdentity,
    ValidatedSampleExport,
)

PINNED_HUMANEVALPLUS_REVISION = "d32357cf319e50e9c8d8dab5ea876c72b0fd321b"
PINNED_HUMANEVALPLUS_SOURCE_MANIFEST = "evalplus_humanevalplus_d32357cf.json"
PILOT_COUNT = 10
PILOT_SEED = 20260824
RESEARCH_NATURAL_COUNT = 45
RESEARCH_NATURAL_SEED = 20260825

SelectionPolicy = Literal["all", "phase1-success-only"]

_MAX_STATIC_INPUT_BYTES = 128 * 1024 * 1024
_MAX_SOLUTION_BYTES = 2 * 1024 * 1024

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_RESPONSE_STATUSES = {"success", "parse_error", "provider_error", "skipped"}
_ATTEMPT_OUTCOMES = {"success", "parse_error", "provider_error"}
_PHASE1_ARTIFACT_SCHEMA_V1 = 1
_PHASE1_ARTIFACT_SCHEMA_V2 = 2
_SUPPORTED_PHASE1_ARTIFACT_SCHEMAS = {
    _PHASE1_ARTIFACT_SCHEMA_V1,
    _PHASE1_ARTIFACT_SCHEMA_V2,
}
_RESPONSE_KEYS_V1 = {
    "run_id",
    "invocation_id",
    "problem_id",
    "provider",
    "model",
    "status",
    "parse_status",
    "started_at",
    "ended_at",
    "duration_seconds",
    "attempt_count",
    "retry_count",
    "raw_output_attempt",
    "parse_attempted",
    "raw_output",
    "solution_trace",
    "error_type",
    "error",
}
_RESPONSE_KEYS_V2 = _RESPONSE_KEYS_V1 | {"attempt_outcomes"}
_SUMMARY_KEYS_V1 = {
    "run_id",
    "experiment_label",
    "updated_at",
    "completed_at",
    "total_problem_count",
    "dataset_problem_count",
    "final_outcome_counts",
    "success_count",
    "parse_error_count",
    "provider_error_count",
    "failure_count",
    "pending_count",
    "parse_attempted_count",
    "parse_success_count",
    "parse_failure_count",
    "parse_success_rate",
    "average_duration_seconds",
    "record_count",
    "record_status_counts",
    "status_counts",
    "invocation",
    "skipped_count",
    "metrics_scope",
}
_SUMMARY_OBSERVABILITY_KEYS_V2 = {
    "first_attempt_parse_success_count",
    "parse_failure_encountered_count",
    "repair_attempted_count",
    "repair_success_count",
    "terminal_parse_error_count",
    "average_attempt_count",
    "average_retry_count",
}
_SUMMARY_KEYS_V2 = _SUMMARY_KEYS_V1 | _SUMMARY_OBSERVABILITY_KEYS_V2
_SOLUTION_TRACE_KEYS = {
    "problem_id",
    "requirement_understanding",
    "design_summary",
    "edge_cases_considered",
    "implementation_steps",
    "declared_time_complexity",
    "declared_space_complexity",
    "code",
}


class EvalPlusExportError(ValueError):
    """Raised when phase-one inputs are not safe and reproducible to export."""


class _DuplicateJsonKey(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _DatasetValidation:
    identity: HumanEvalPlusDatasetIdentity
    manifest_payload: dict[str, Any]
    expected_provenance: dict[str, Any]
    problem_ids: tuple[str, ...]
    task_metadata: tuple[HumanEvalPlusTaskMetadata, ...]


@dataclass(frozen=True, slots=True)
class _ManifestValidation:
    source: Phase1SourceIdentity
    payload: dict[str, Any]
    artifact_schema_version: int
    invocation_ids: frozenset[str]
    invocation_by_id: dict[str, dict[str, Any]]


@dataclass(frozen=True, slots=True)
class _ResponseValidation:
    samples: tuple[EvalPlusSample, ...]
    references: tuple[Phase1ResponseReference, ...]
    records: tuple[dict[str, Any], ...]
    final_non_skipped: dict[str, dict[str, Any]]


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _decode_json(payload: bytes, *, label: str) -> Any:
    try:
        text = payload.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKey, ValueError):
        raise EvalPlusExportError(f"{label} is not strict UTF-8 JSON") from None


def _read_regular_file(path: Path, *, label: str) -> bytes:
    if path.is_symlink():
        raise EvalPlusExportError(f"{label} must not be a symbolic link")
    if not path.is_file():
        raise EvalPlusExportError(f"required {label} is missing")
    try:
        if path.stat().st_size > _MAX_STATIC_INPUT_BYTES:
            raise EvalPlusExportError(f"{label} exceeds the static input size limit")
        # Bound the read as well as the preceding stat check.  This keeps a
        # concurrently growing/replaced input from causing an unbounded read.
        with path.open("rb") as stream:
            payload = stream.read(_MAX_STATIC_INPUT_BYTES + 1)
    except OSError:
        raise EvalPlusExportError(f"cannot read required {label}") from None
    if len(payload) > _MAX_STATIC_INPUT_BYTES:
        raise EvalPlusExportError(f"{label} exceeds the static input size limit")
    return payload


def _json_object(payload: bytes, *, label: str) -> dict[str, Any]:
    value = _decode_json(payload, label=label)
    if not isinstance(value, dict):
        raise EvalPlusExportError(f"{label} must contain a JSON object")
    return value


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvalPlusExportError(f"{label} must be a JSON object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], *, label: str) -> None:
    if set(value) != expected:
        raise EvalPlusExportError(f"{label} fields do not match the declared schema")


def _text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise EvalPlusExportError(f"{label} must be a non-empty string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise EvalPlusExportError(f"{label} must be valid UTF-8 text") from None
    return value


def _sha256_text(value: Any, *, label: str) -> str:
    text = _text(value, label=label)
    if _SHA256_PATTERN.fullmatch(text) is None:
        raise EvalPlusExportError(f"{label} must be a lowercase SHA256")
    return text


def _integer(value: Any, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise EvalPlusExportError(f"{label} must be an integer >= {minimum}")
    return value


def _number(value: Any, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise EvalPlusExportError(f"{label} must be a finite non-negative number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise EvalPlusExportError(f"{label} must be a finite non-negative number")
    return number


def _metadata_json_bytes(value: Any) -> bytes:
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


def _task_metadata_from_public_prompt(problem: Any) -> HumanEvalPlusTaskMetadata:
    """Derive the public entry point without importing or executing the prompt."""

    prompt = problem.requirement
    try:
        prompt_bytes = prompt.encode("utf-8")
        tree = ast.parse(prompt, filename="<public-humaneval-prompt>", mode="exec")
    except (UnicodeEncodeError, SyntaxError, ValueError):
        raise EvalPlusExportError("public HumanEval+ prompt failed static AST validation") from None
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    if len(functions) != 1 or functions[0].name != problem.function_name:
        raise EvalPlusExportError("public HumanEval+ prompt entry_point is inconsistent")
    return HumanEvalPlusTaskMetadata(
        problem_id=problem.problem_id,
        prompt_sha256=_sha256(prompt_bytes),
        entry_point=functions[0].name,
    )


def serialize_samples_jsonl(samples: Sequence[EvalPlusSample]) -> bytes:
    """Serialize samples without redacting or otherwise rewriting source code.

    Phase one has already applied its artifact-safety policy.  Applying the
    general text redactor again here could silently change executable source,
    so this serializer performs strict JSON/UTF-8 encoding only.
    """

    if not samples:
        raise EvalPlusExportError("at least one EvalPlus sample is required")
    seen: set[str] = set()
    encoded: list[bytes] = []
    for sample in samples:
        if sample.task_id in seen:
            raise EvalPlusExportError("EvalPlus samples contain a duplicate task_id")
        seen.add(sample.task_id)
        try:
            solution_bytes = sample.solution.encode("utf-8")
        except UnicodeEncodeError:
            raise EvalPlusExportError("EvalPlus sample solution is not valid UTF-8") from None
        if len(solution_bytes) > _MAX_SOLUTION_BYTES:
            raise EvalPlusExportError("EvalPlus sample solution exceeds the size limit")
        row = {"task_id": sample.task_id, "solution": sample.solution}
        try:
            line = json.dumps(
                row,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError):
            raise EvalPlusExportError("EvalPlus samples cannot be encoded as UTF-8 JSONL") from None
        encoded.append(line + b"\n")
    return b"".join(encoded)


def _controlled_source_manifest() -> tuple[dict[str, Any], str]:
    try:
        path = data_path("manifests", PINNED_HUMANEVALPLUS_SOURCE_MANIFEST)
    except ConfigurationError:
        raise EvalPlusExportError("controlled HumanEval+ source manifest is unavailable") from None
    payload_bytes = _read_regular_file(path, label="controlled HumanEval+ source manifest")
    payload = _json_object(payload_bytes, label="controlled HumanEval+ source manifest")
    if (
        payload.get("schema_version") != 1
        or payload.get("dataset_id") != DATASET_ID
        or payload.get("revision") != PINNED_HUMANEVALPLUS_REVISION
        or payload.get("split") != "test"
        or payload.get("license") != "apache-2.0"
        or payload.get("record_count") != EXPECTED_RECORD_COUNT
    ):
        raise EvalPlusExportError("controlled HumanEval+ source manifest identity is invalid")
    return payload, _sha256(payload_bytes)


def _validate_dataset_manifest(path: Path) -> _DatasetValidation:
    if path.name != "dataset_manifest.json":
        raise EvalPlusExportError("dataset manifest must be named dataset_manifest.json")
    manifest_bytes = _read_regular_file(path, label="dataset manifest")
    payload = _json_object(manifest_bytes, label="dataset manifest")
    expected_top_level_v1 = {
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
        "parent_manifest_sha256",
        "raw_snapshot",
        "public_projection",
        "selection",
        "withheld_fields",
        "limitations",
    }
    schema_version = payload.get("schema_version")
    if schema_version == DATASET_MANIFEST_SCHEMA_VERSION:
        _exact_keys(payload, expected_top_level_v1, label="dataset manifest")
        selection_role = "pilot"
        excluded_manifests: list[dict[str, Any]] = []
    elif schema_version == RESEARCH_NATURAL_DATASET_MANIFEST_SCHEMA_VERSION:
        _exact_keys(
            payload,
            expected_top_level_v1 | {"selection_role", "excluded_manifests"},
            label="dataset manifest",
        )
        selection_role = _text(payload.get("selection_role"), label="dataset selection_role")
        if selection_role not in ALLOWED_SELECTION_ROLES:
            raise EvalPlusExportError("dataset manifest selection_role is invalid")
        if selection_role != "research_natural":
            raise EvalPlusExportError("dataset manifest v2 selection_role must be research_natural")
        raw_excluded = payload.get("excluded_manifests")
        if not isinstance(raw_excluded, list):
            raise EvalPlusExportError("dataset manifest excluded_manifests must be a list")
        for item in raw_excluded:
            if not isinstance(item, Mapping):
                raise EvalPlusExportError(
                    "dataset manifest excluded_manifests entries must be objects"
                )
            _exact_keys(
                item,
                {
                    "manifest_sha256",
                    "kind",
                    "experiment_label",
                    "selection_role",
                    "selected_problem_ids",
                },
                label="dataset excluded manifest record",
            )
            _sha256_text(item.get("manifest_sha256"), label="excluded manifest manifest_sha256")
            _text(item.get("kind"), label="excluded manifest kind")
            _text(item.get("experiment_label"), label="excluded manifest experiment_label")
            if item.get("selection_role") not in ALLOWED_SELECTION_ROLES:
                raise EvalPlusExportError("excluded manifest selection_role is invalid")
            raw_ids = item.get("selected_problem_ids")
            if not isinstance(raw_ids, list) or not all(isinstance(pid, str) for pid in raw_ids):
                raise EvalPlusExportError("excluded manifest selected_problem_ids are invalid")
        excluded_manifests = [dict(item) for item in raw_excluded]
    else:
        raise EvalPlusExportError("dataset manifest schema_version must be 1 or 2")

    if payload.get("kind") != "tracejudge_dataset_selection":
        raise EvalPlusExportError("dataset manifest is not a pinned selection bundle")
    if payload.get("metrics_scope") != "generation_and_parsing_only":
        raise EvalPlusExportError("dataset manifest source metrics scope is invalid")
    if payload.get("dataset_id") != DATASET_ID or payload.get("source") != DATASET_SOURCE:
        raise EvalPlusExportError("dataset manifest HumanEval+ identity is invalid")
    if payload.get("revision") != PINNED_HUMANEVALPLUS_REVISION:
        raise EvalPlusExportError("dataset manifest HumanEval+ revision is invalid")
    if payload.get("split") != "test" or payload.get("license") != "apache-2.0":
        raise EvalPlusExportError("dataset manifest split/license identity is invalid")

    adapter = _mapping(payload.get("adapter"), label="dataset manifest adapter")
    _exact_keys(adapter, {"name", "version"}, label="dataset manifest adapter")
    if adapter != {"name": ADAPTER_NAME, "version": ADAPTER_VERSION}:
        raise EvalPlusExportError("dataset manifest adapter identity is invalid")

    source_manifest, controlled_source_hash = _controlled_source_manifest()
    source_manifest_hash = _sha256_text(
        payload.get("source_manifest_sha256"),
        label="dataset manifest source_manifest_sha256",
    )
    if source_manifest_hash != controlled_source_hash:
        raise EvalPlusExportError("dataset manifest does not match the controlled source manifest")
    parent_manifest_hash = _sha256_text(
        payload.get("parent_manifest_sha256"),
        label="dataset manifest parent_manifest_sha256",
    )

    raw_snapshot = _mapping(payload.get("raw_snapshot"), label="dataset raw_snapshot")
    _exact_keys(
        raw_snapshot,
        {"aggregate_sha256", "test_jsonl_sha256", "record_count"},
        label="dataset raw_snapshot",
    )
    raw_aggregate = _sha256_text(
        raw_snapshot.get("aggregate_sha256"), label="raw snapshot aggregate_sha256"
    )
    raw_test_hash = _sha256_text(
        raw_snapshot.get("test_jsonl_sha256"), label="raw snapshot test_jsonl_sha256"
    )
    if raw_snapshot.get("record_count") != EXPECTED_RECORD_COUNT:
        raise EvalPlusExportError("dataset raw snapshot record count is invalid")
    raw_files = source_manifest.get("raw_files")
    if not isinstance(raw_files, list) or not raw_files:
        raise EvalPlusExportError("controlled source manifest raw file identity is invalid")
    raw_file_identity: list[dict[str, Any]] = []
    test_hashes: list[str] = []
    for entry in raw_files:
        mapping = _mapping(entry, label="controlled source manifest raw file")
        _exact_keys(mapping, {"path", "size", "sha256"}, label="controlled raw file")
        relative_path = _text(mapping.get("path"), label="controlled raw file path")
        size = _integer(mapping.get("size"), label="controlled raw file size")
        digest = _sha256_text(mapping.get("sha256"), label="controlled raw file SHA256")
        raw_file_identity.append({"path": relative_path, "size": size, "sha256": digest})
        if relative_path == "test.jsonl":
            test_hashes.append(digest)
    if len(test_hashes) != 1 or test_hashes[0] != raw_test_hash:
        raise EvalPlusExportError("dataset raw JSONL hash differs from controlled provenance")
    if _sha256(_metadata_json_bytes(raw_file_identity)) != raw_aggregate:
        raise EvalPlusExportError("dataset raw snapshot aggregate hash is inconsistent")

    projection = _mapping(payload.get("public_projection"), label="dataset public_projection")
    _exact_keys(
        projection,
        {"path", "sha256", "record_count", "ordered_problem_ids_sha256"},
        label="dataset public_projection",
    )
    if projection.get("path") != "problems.jsonl":
        raise EvalPlusExportError("dataset public projection path is invalid")
    problems_hash = _sha256_text(projection.get("sha256"), label="dataset public projection SHA256")
    ordered_ids_hash = _sha256_text(
        projection.get("ordered_problem_ids_sha256"),
        label="dataset ordered problem IDs SHA256",
    )

    selection = _mapping(payload.get("selection"), label="dataset selection")
    if schema_version == DATASET_MANIFEST_SCHEMA_VERSION:
        _exact_keys(
            selection,
            {"algorithm", "seed", "count", "selected_problem_ids"},
            label="dataset selection",
        )
    else:
        _exact_keys(
            selection,
            {
                "algorithm",
                "seed",
                "count",
                "selected_problem_ids",
                "selected_problem_ids_sha256",
                "excluded_problem_ids",
                "excluded_problem_ids_sha256",
                "excluded_manifests_count",
                "excluded_manifests_sha256",
            },
            label="dataset selection",
        )
    if selection.get("algorithm") != SELECTION_ALGORITHM:
        raise EvalPlusExportError("dataset selection algorithm is invalid")
    count = _integer(selection.get("count"), label="dataset selection count", minimum=1)
    seed = _integer(selection.get("seed"), label="dataset selection seed")
    selected_ids = selection.get("selected_problem_ids")
    if not isinstance(selected_ids, list) or not all(
        isinstance(problem_id, str) for problem_id in selected_ids
    ):
        raise EvalPlusExportError("dataset selected problem IDs are invalid")

    exclude_ids: list[str] = []
    if schema_version == RESEARCH_NATURAL_DATASET_MANIFEST_SCHEMA_VERSION:
        raw_excluded_ids = selection.get("excluded_problem_ids")
        if not isinstance(raw_excluded_ids, list):
            raise EvalPlusExportError("dataset manifest excluded_problem_ids are invalid")
        for problem_id in raw_excluded_ids:
            if not isinstance(problem_id, str):
                raise EvalPlusExportError(
                    "dataset manifest excluded_problem_ids contain a non-string"
                )
        exclude_ids = list(raw_excluded_ids)
        sorted_excluded = sorted(exclude_ids, key=_safe_task_id_number)
        if selection.get("excluded_problem_ids") != sorted_excluded:
            raise EvalPlusExportError("dataset excluded problem IDs are not in canonical order")
        if selection.get("excluded_problem_ids_sha256") != ordered_problem_ids_sha256(
            sorted_excluded
        ):
            raise EvalPlusExportError("dataset excluded problem IDs hash is invalid")
        union_excluded = set()
        for record in excluded_manifests:
            union_excluded.update(record["selected_problem_ids"])
        if sorted(union_excluded, key=_safe_task_id_number) != sorted_excluded:
            raise EvalPlusExportError(
                "dataset excluded problem IDs do not match excluded manifests"
            )
        if selection.get("excluded_manifests_count") != len(excluded_manifests):
            raise EvalPlusExportError("dataset excluded manifests count is invalid")
        expected_manifests_hash = _sha256(
            _metadata_json_bytes(
                [{"manifest_sha256": record["manifest_sha256"]} for record in excluded_manifests]
            )
        )
        if selection.get("excluded_manifests_sha256") != expected_manifests_hash:
            raise EvalPlusExportError("dataset excluded manifests hash is invalid")

    expected_ids = list(
        select_humanevalplus_problem_ids(count=count, seed=seed, exclude_ids=exclude_ids)
    )
    if selected_ids != expected_ids:
        raise EvalPlusExportError("dataset selected problem IDs are invalid")
    assert isinstance(selected_ids, list)
    if projection.get("record_count") != count:
        raise EvalPlusExportError("dataset public projection record count is invalid")
    if ordered_ids_hash != ordered_problem_ids_sha256(selected_ids):
        raise EvalPlusExportError("dataset ordered problem IDs hash is inconsistent")

    if schema_version == RESEARCH_NATURAL_DATASET_MANIFEST_SCHEMA_VERSION:
        if selection.get("selected_problem_ids_sha256") != ordered_problem_ids_sha256(selected_ids):
            raise EvalPlusExportError("dataset selected problem IDs hash is invalid")

    if selection_role == "pilot":
        allowed_labels = {
            PILOT_EXPERIMENT_LABEL,
            f"humanevalplus_{count}_public_prompt_generation_pilot",
        }
        if payload.get("experiment_label") not in allowed_labels:
            raise EvalPlusExportError("dataset manifest experiment label is invalid")
        if payload.get("limitations") != list(PILOT_LIMITATIONS):
            raise EvalPlusExportError("dataset manifest pilot limitations are invalid")
    else:
        if payload.get("experiment_label") != RESEARCH_NATURAL_EXPERIMENT_LABEL:
            raise EvalPlusExportError("dataset manifest experiment label is invalid")
        if count != RESEARCH_NATURAL_COUNT or seed != RESEARCH_NATURAL_SEED:
            raise EvalPlusExportError("dataset manifest research_natural seed/count is invalid")
        if payload.get("limitations") != list(RESEARCH_NATURAL_LIMITATIONS):
            raise EvalPlusExportError("dataset manifest research_natural limitations are invalid")

    withheld_fields = payload.get("withheld_fields")
    if (
        not isinstance(withheld_fields, list)
        or not all(isinstance(field, str) and field for field in withheld_fields)
        or not {"canonical_solution", "test"} <= set(withheld_fields)
        or not set(withheld_fields) <= set(KNOWN_WITHHELD_FIELDS)
    ):
        raise EvalPlusExportError("dataset withheld field identity is invalid")

    problems_path = path.parent / "problems.jsonl"
    problems_bytes = _read_regular_file(problems_path, label="adjacent problems.jsonl")
    if _sha256(problems_bytes) != problems_hash:
        raise EvalPlusExportError("adjacent problems.jsonl SHA256 differs from dataset manifest")
    try:
        problems = load_problems(problems_path)
        validate_humanevalplus_public_problems(problems)
    except DatasetError:
        raise EvalPlusExportError(
            "adjacent problems.jsonl failed public projection validation"
        ) from None
    problem_ids = tuple(problem.problem_id for problem in problems)
    if problem_ids != tuple(selected_ids):
        raise EvalPlusExportError("adjacent problems.jsonl IDs differ from dataset selection")
    task_metadata = tuple(_task_metadata_from_public_prompt(problem) for problem in problems)

    manifest_hash = _sha256(manifest_bytes)
    expected_provenance: dict[str, Any] = {
        "manifest_sha256": manifest_hash,
        "kind": payload["kind"],
        "dataset_id": payload["dataset_id"],
        "revision": payload["revision"],
        "source": payload["source"],
        "license": payload["license"],
        "adapter": {"name": adapter["name"], "version": adapter["version"]},
        "raw_snapshot": {
            "aggregate_sha256": raw_aggregate,
            "test_jsonl_sha256": raw_test_hash,
            "record_count": raw_snapshot["record_count"],
        },
        "public_projection": {
            "sha256": problems_hash,
            "record_count": count,
            "ordered_problem_ids_sha256": ordered_ids_hash,
        },
        "selection": {
            "algorithm": selection["algorithm"],
            "seed": selection["seed"],
            "count": selection["count"],
            "selected_problem_ids": list(selected_ids),
        },
        "withheld_fields": sorted(withheld_fields),
        "metrics_scope": payload["metrics_scope"],
        "source_manifest_sha256": source_manifest_hash,
        "parent_manifest_sha256": parent_manifest_hash,
    }
    if schema_version == RESEARCH_NATURAL_DATASET_MANIFEST_SCHEMA_VERSION:
        expected_provenance["selection_role"] = selection_role
        expected_provenance["excluded_manifests"] = excluded_manifests
    identity = HumanEvalPlusDatasetIdentity(
        manifest_sha256=manifest_hash,
        dataset_id=DATASET_ID,
        source=DATASET_SOURCE,
        revision=PINNED_HUMANEVALPLUS_REVISION,
        license="apache-2.0",
        adapter_name=ADAPTER_NAME,
        adapter_version=ADAPTER_VERSION,
        source_manifest_sha256=source_manifest_hash,
        parent_manifest_sha256=parent_manifest_hash,
        raw_snapshot_aggregate_sha256=raw_aggregate,
        raw_test_jsonl_sha256=raw_test_hash,
        problems_sha256=problems_hash,
        ordered_problem_ids_sha256=ordered_ids_hash,
        selection_algorithm=SELECTION_ALGORITHM,
        selection_seed=seed,
        selected_problem_ids=problem_ids,
        selection_role=selection_role,
        excluded_manifests=tuple(excluded_manifests),
    )
    return _DatasetValidation(
        identity=identity,
        manifest_payload=payload,
        expected_provenance=expected_provenance,
        problem_ids=problem_ids,
        task_metadata=task_metadata,
    )


def _validate_phase1_manifest(
    run_dir: Path,
    manifest_bytes: bytes,
    dataset: _DatasetValidation,
) -> _ManifestValidation:
    payload = _json_object(manifest_bytes, label="phase-one manifest")
    _exact_keys(
        payload,
        {
            "schema_version",
            "phase",
            "experiment_label",
            "run_id",
            "created_at",
            "status",
            "completed_at",
            "dataset",
            "git",
            "environment",
            "provider_config",
            "invocations",
        },
        label="phase-one manifest",
    )
    artifact_schema_version = payload.get("schema_version")
    if (
        isinstance(artifact_schema_version, bool)
        or not isinstance(artifact_schema_version, int)
        or artifact_schema_version not in _SUPPORTED_PHASE1_ARTIFACT_SCHEMAS
        or payload.get("phase") != "phase1_baseline_generation"
    ):
        raise EvalPlusExportError("phase-one manifest schema/phase is invalid")
    if payload.get("status") != "completed" or not isinstance(payload.get("completed_at"), str):
        raise EvalPlusExportError("phase-one manifest is not completed")
    run_id = _text(payload.get("run_id"), label="phase-one run_id")
    if run_dir.name != run_id:
        raise EvalPlusExportError("phase-one run directory does not match manifest run_id")
    selection_role = dataset.expected_provenance.get("selection_role", "pilot")
    problem_count = len(dataset.problem_ids)
    if selection_role == "research_natural":
        expected_label = RESEARCH_NATURAL_EXPERIMENT_LABEL
    else:
        expected_label = (
            PILOT_EXPERIMENT_LABEL
            if problem_count == PILOT_COUNT
            else f"humanevalplus_{problem_count}_public_prompt_generation_pilot"
        )
    if payload.get("experiment_label") != expected_label:
        raise EvalPlusExportError("phase-one experiment label differs from dataset manifest")

    manifest_dataset = _mapping(payload.get("dataset"), label="phase-one manifest dataset")
    _exact_keys(
        manifest_dataset,
        {
            "path",
            "sha256",
            "problem_count",
            "sources",
            "difficulties",
            "visible_tests",
            "provenance",
        },
        label="phase-one manifest dataset",
    )
    _text(manifest_dataset.get("path"), label="phase-one dataset path")
    if manifest_dataset.get("sha256") != dataset.identity.problems_sha256:
        raise EvalPlusExportError("phase-one dataset SHA256 differs from dataset manifest")
    if manifest_dataset.get("problem_count") != len(dataset.problem_ids):
        raise EvalPlusExportError("phase-one dataset problem count is invalid")
    if manifest_dataset.get("sources") != {DATASET_SOURCE: len(dataset.problem_ids)}:
        raise EvalPlusExportError("phase-one dataset source counts are invalid")
    if manifest_dataset.get("difficulties") != {"unknown": len(dataset.problem_ids)}:
        raise EvalPlusExportError("phase-one dataset difficulty counts are invalid")
    visible_tests = _mapping(
        manifest_dataset.get("visible_tests"), label="phase-one visible test summary"
    )
    _exact_keys(visible_tests, {"total_count", "per_problem"}, label="visible test summary")
    expected_visible = {
        problem_id: {"count": 0, "case_ids": []} for problem_id in dataset.problem_ids
    }
    if (
        visible_tests.get("total_count") != 0
        or visible_tests.get("per_problem") != expected_visible
    ):
        raise EvalPlusExportError("phase-one public projection unexpectedly records visible tests")
    if manifest_dataset.get("provenance") != dataset.expected_provenance:
        raise EvalPlusExportError("phase-one dataset provenance differs from dataset manifest")

    git = _mapping(payload.get("git"), label="phase-one Git metadata")
    _exact_keys(
        git,
        {"available", "branch", "commit", "dirty", "working_tree_sha256"},
        label="phase-one Git metadata",
    )
    if git.get("available") is not True:
        raise EvalPlusExportError("phase-one Git metadata is unavailable")
    git_commit = _text(git.get("commit"), label="phase-one Git commit")
    if _GIT_COMMIT_PATTERN.fullmatch(git_commit) is None:
        raise EvalPlusExportError("phase-one Git commit must be a lowercase full SHA")
    git_branch_value = git.get("branch")
    if git_branch_value is not None and not isinstance(git_branch_value, str):
        raise EvalPlusExportError("phase-one Git branch metadata is invalid")
    if not isinstance(git.get("dirty"), bool):
        raise EvalPlusExportError("phase-one Git dirty metadata is invalid")
    fingerprint = git.get("working_tree_sha256")
    if git["dirty"]:
        _sha256_text(fingerprint, label="phase-one working tree fingerprint")
    elif fingerprint is not None:
        raise EvalPlusExportError("clean phase-one Git metadata must not have a fingerprint")

    if not isinstance(payload.get("environment"), Mapping):
        raise EvalPlusExportError("phase-one environment metadata is invalid")
    provider_config = _mapping(payload.get("provider_config"), label="phase-one provider config")
    provider = _text(provider_config.get("provider"), label="phase-one provider")
    model = _text(provider_config.get("model"), label="phase-one model")

    raw_invocations = payload.get("invocations")
    if not isinstance(raw_invocations, list) or not raw_invocations:
        raise EvalPlusExportError("phase-one manifest invocations are invalid")
    invocation_by_id: dict[str, dict[str, Any]] = {}
    for raw_invocation in raw_invocations:
        invocation = _mapping(raw_invocation, label="phase-one invocation")
        required = {
            "invocation_id",
            "started_at",
            "resume",
            "status",
            "completed_at",
            "git",
            "environment",
        }
        if not required <= set(invocation) or set(invocation) - (required | {"interrupted_at"}):
            raise EvalPlusExportError("phase-one invocation fields are invalid")
        invocation_id = _text(invocation.get("invocation_id"), label="phase-one invocation_id")
        if invocation_id in invocation_by_id:
            raise EvalPlusExportError("phase-one manifest contains duplicate invocation IDs")
        status = invocation.get("status")
        if status not in {"completed", "interrupted"}:
            raise EvalPlusExportError("completed phase-one run contains an unfinished invocation")
        if not isinstance(invocation.get("resume"), bool):
            raise EvalPlusExportError("phase-one invocation resume flag is invalid")
        invocation_by_id[invocation_id] = dict(invocation)
    last_invocation = invocation_by_id[
        _text(raw_invocations[-1]["invocation_id"], label="last invocation")
    ]
    if last_invocation.get("status") != "completed":
        raise EvalPlusExportError("phase-one final invocation is not completed")
    if last_invocation.get("completed_at") != payload.get("completed_at"):
        raise EvalPlusExportError("phase-one completion timestamps are inconsistent")

    source = Phase1SourceIdentity(
        run_id=run_id,
        experiment_label=payload.get("experiment_label"),
        manifest_sha256=_sha256(manifest_bytes),
        summary_sha256="",  # Filled only after the summary is validated.
        responses_sha256="",  # Filled only after the response log is validated.
        git_commit=git_commit,
        git_branch=git_branch_value,
        git_dirty=git["dirty"],
        provider=provider,
        model=model,
    )
    return _ManifestValidation(
        source=source,
        payload=payload,
        artifact_schema_version=artifact_schema_version,
        invocation_ids=frozenset(invocation_by_id),
        invocation_by_id=invocation_by_id,
    )


def _response_records(payload: bytes) -> list[tuple[int, bytes, dict[str, Any]]]:
    if payload and not payload.endswith(b"\n"):
        raise EvalPlusExportError("phase-one responses.jsonl has an incomplete final line")
    parsed: list[tuple[int, bytes, dict[str, Any]]] = []
    for line_number, line in enumerate(payload.splitlines(keepends=True), start=1):
        if not line.strip():
            raise EvalPlusExportError("phase-one responses.jsonl contains a blank record")
        value = _decode_json(line, label=f"phase-one response line {line_number}")
        if not isinstance(value, dict):
            raise EvalPlusExportError("phase-one response record must be a JSON object")
        parsed.append((line_number, line, value))
    if not parsed:
        raise EvalPlusExportError("phase-one responses.jsonl is empty")
    return parsed


def _validate_response_common(
    record: Mapping[str, Any],
    *,
    manifest: _ManifestValidation,
    expected_ids: frozenset[str],
) -> tuple[str, str, str]:
    response_keys = (
        _RESPONSE_KEYS_V1
        if manifest.artifact_schema_version == _PHASE1_ARTIFACT_SCHEMA_V1
        else _RESPONSE_KEYS_V2
    )
    _exact_keys(record, response_keys, label="phase-one response")
    if record.get("run_id") != manifest.source.run_id:
        raise EvalPlusExportError("phase-one response run_id differs from manifest")
    invocation_id = _text(record.get("invocation_id"), label="response invocation_id")
    if invocation_id not in manifest.invocation_ids:
        raise EvalPlusExportError("phase-one response references an unknown invocation")
    problem_id = _text(record.get("problem_id"), label="response problem_id")
    if problem_id not in expected_ids:
        raise EvalPlusExportError("phase-one responses contain a problem ID outside the dataset")
    status = record.get("status")
    if status not in _RESPONSE_STATUSES:
        raise EvalPlusExportError("phase-one response status is invalid")
    if (
        record.get("provider") != manifest.source.provider
        or record.get("model") != manifest.source.model
    ):
        raise EvalPlusExportError("phase-one response provider/model differs from manifest")
    _text(record.get("started_at"), label="response started_at")
    _text(record.get("ended_at"), label="response ended_at")
    _number(record.get("duration_seconds"), label="response duration_seconds")
    attempt_count = _integer(record.get("attempt_count"), label="response attempt_count")
    retry_count = _integer(record.get("retry_count"), label="response retry_count")
    if retry_count > attempt_count:
        raise EvalPlusExportError("phase-one response retry count exceeds attempt count")
    if manifest.artifact_schema_version == _PHASE1_ARTIFACT_SCHEMA_V2:
        raw_outcomes = record.get("attempt_outcomes")
        if not isinstance(raw_outcomes, list) or any(
            not isinstance(outcome, str) or outcome not in _ATTEMPT_OUTCOMES
            for outcome in raw_outcomes
        ):
            raise EvalPlusExportError("phase-one response attempt outcomes are invalid")
        if attempt_count != len(raw_outcomes):
            raise EvalPlusExportError(
                "phase-one response attempt count differs from attempt outcomes"
            )
        if status == "skipped":
            if raw_outcomes:
                raise EvalPlusExportError("phase-one skipped response has attempt outcomes")
        else:
            if not raw_outcomes or raw_outcomes[-1] != status:
                raise EvalPlusExportError(
                    "phase-one response final attempt outcome differs from status"
                )
            if "success" in raw_outcomes[:-1]:
                raise EvalPlusExportError("phase-one response continues after a successful attempt")
            if retry_count != attempt_count - 1:
                raise EvalPlusExportError(
                    "phase-one response retry count differs from its attempt sequence"
                )
    raw_output_attempt = record.get("raw_output_attempt")
    if raw_output_attempt is not None:
        raw_output_attempt = _integer(
            raw_output_attempt, label="response raw_output_attempt", minimum=1
        )
        if raw_output_attempt > attempt_count:
            raise EvalPlusExportError("phase-one response raw output attempt is invalid")
        if (
            manifest.artifact_schema_version == _PHASE1_ARTIFACT_SCHEMA_V2
            and record["attempt_outcomes"][raw_output_attempt - 1] == "provider_error"
        ):
            raise EvalPlusExportError("phase-one raw output attempt references a provider error")
    if manifest.artifact_schema_version == _PHASE1_ARTIFACT_SCHEMA_V2 and (
        record.get("raw_output") is None
    ) is not (raw_output_attempt is None):
        raise EvalPlusExportError("phase-one raw output and attempt metadata are inconsistent")
    if (
        manifest.artifact_schema_version == _PHASE1_ARTIFACT_SCHEMA_V2
        and record.get("raw_output") is not None
        and not isinstance(record.get("raw_output"), str)
    ):
        raise EvalPlusExportError("phase-one raw output must be text or null")
    if not isinstance(record.get("parse_attempted"), bool):
        raise EvalPlusExportError("phase-one response parse_attempted is invalid")
    if manifest.artifact_schema_version == _PHASE1_ARTIFACT_SCHEMA_V2:
        expected_parse_attempted = any(
            outcome in {"parse_error", "success"} for outcome in record["attempt_outcomes"]
        )
        if record.get("parse_attempted") is not expected_parse_attempted:
            raise EvalPlusExportError(
                "phase-one response parse_attempted differs from attempt outcomes"
            )
        if expected_parse_attempted and not isinstance(record.get("raw_output"), str):
            raise EvalPlusExportError("phase-one parse attempt is missing its raw output")
    return problem_id, invocation_id, str(status)


def _validated_sample(problem_id: str, solution: Any) -> tuple[EvalPlusSample, str]:
    solution_mapping = _mapping(solution, label="successful solution_trace")
    _exact_keys(solution_mapping, _SOLUTION_TRACE_KEYS, label="successful solution_trace")
    if solution_mapping.get("problem_id") != problem_id:
        raise EvalPlusExportError("solution_trace.problem_id differs from response problem_id")
    code = solution_mapping.get("code")
    if not isinstance(code, str) or not code.strip():
        raise EvalPlusExportError("successful phase-one response has empty code")
    try:
        code_bytes = code.encode("utf-8")
    except UnicodeEncodeError:
        raise EvalPlusExportError("successful phase-one response code is not valid UTF-8") from None
    if len(code_bytes) > _MAX_SOLUTION_BYTES:
        raise EvalPlusExportError("successful phase-one response code exceeds the size limit")
    # Never mutate executable source at this boundary.  If applying the shared
    # artifact redactor would change it, phase one's persisted code still looks
    # credential-bearing and is therefore unsafe to export.
    if redact_sensitive_text(code) != code:
        raise EvalPlusExportError("successful phase-one response code failed credential safety")
    try:
        sample = EvalPlusSample(task_id=problem_id, solution=code)
    except ValidationError:
        raise EvalPlusExportError("successful phase-one response code is not valid UTF-8") from None
    return sample, _sha256(code_bytes)


def _validate_responses(
    responses_bytes: bytes,
    *,
    manifest: _ManifestValidation,
    dataset: _DatasetValidation,
    selection_policy: SelectionPolicy = "all",
    min_success_count: int = 0,
) -> _ResponseValidation:
    parsed = _response_records(responses_bytes)
    expected_ids = frozenset(dataset.problem_ids)
    success_by_id: dict[str, tuple[EvalPlusSample, Phase1ResponseReference]] = {}
    records: list[dict[str, Any]] = []
    final_non_skipped: dict[str, dict[str, Any]] = {}

    for line_number, raw_line, record in parsed:
        problem_id, invocation_id, status = _validate_response_common(
            record,
            manifest=manifest,
            expected_ids=expected_ids,
        )
        if status == "success":
            if problem_id in success_by_id:
                raise EvalPlusExportError("phase-one responses contain duplicate success records")
            if (
                record.get("parse_status") != "parsed"
                or record.get("parse_attempted") is not True
                or record.get("error_type") is not None
                or record.get("error") is not None
            ):
                raise EvalPlusExportError("successful phase-one response fields are inconsistent")
            if manifest.artifact_schema_version == _PHASE1_ARTIFACT_SCHEMA_V2 and not isinstance(
                record.get("raw_output"), str
            ):
                raise EvalPlusExportError(
                    "successful phase-one v2 response is missing its raw output"
                )
            sample, code_hash = _validated_sample(problem_id, record.get("solution_trace"))
            reference = Phase1ResponseReference(
                phase1_run_id=manifest.source.run_id,
                problem_id=problem_id,
                invocation_id=invocation_id,
                response_line_number=line_number,
                response_record_sha256=_sha256(raw_line),
                code_sha256=code_hash,
            )
            success_by_id[problem_id] = (sample, reference)
            final_non_skipped[problem_id] = record
        elif status == "skipped":
            if problem_id not in success_by_id:
                raise EvalPlusExportError("phase-one skipped record has no earlier success")
            if (
                record.get("parse_status") != "not_attempted"
                or record.get("parse_attempted") is not False
                or record.get("attempt_count") != 0
                or record.get("retry_count") != 0
                or record.get("raw_output_attempt") is not None
                or record.get("raw_output") is not None
                or record.get("solution_trace") is not None
                or record.get("error_type") is not None
                or record.get("error") is not None
            ):
                raise EvalPlusExportError("phase-one skipped response fields are inconsistent")
        else:
            if problem_id in success_by_id:
                raise EvalPlusExportError("phase-one non-skipped response occurs after success")
            if record.get("solution_trace") is not None:
                raise EvalPlusExportError("failed phase-one response carries a solution_trace")
            expected_parse_status = (
                "failed"
                if status == "parse_error" or record.get("parse_attempted") is True
                else "not_attempted"
            )
            if record.get("parse_status") != expected_parse_status:
                raise EvalPlusExportError("failed phase-one response parse status is inconsistent")
            if not isinstance(record.get("error"), Mapping) or not isinstance(
                record.get("error_type"), str
            ):
                raise EvalPlusExportError("failed phase-one response error fields are inconsistent")
            final_non_skipped[problem_id] = record
        records.append(record)

    if selection_policy == "all":
        if frozenset(success_by_id) != expected_ids:
            raise EvalPlusExportError(
                "phase-one success problem IDs do not exactly match the dataset"
            )
        ordered_success_ids = list(dataset.problem_ids)
    else:
        if len(success_by_id) < min_success_count:
            raise EvalPlusExportError(
                f"phase-one success count {len(success_by_id)} is below min_success_count {min_success_count}"
            )
        ordered_success_ids = [
            problem_id for problem_id in dataset.problem_ids if problem_id in success_by_id
        ]
    samples = tuple(success_by_id[problem_id][0] for problem_id in ordered_success_ids)
    references = tuple(success_by_id[problem_id][1] for problem_id in ordered_success_ids)
    return _ResponseValidation(
        samples=samples,
        references=references,
        records=tuple(records),
        final_non_skipped=final_non_skipped,
    )


def _counts(values: Sequence[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _validate_summary(
    summary_bytes: bytes,
    *,
    manifest: _ManifestValidation,
    responses: _ResponseValidation,
    expected_count: int,
    selection_policy: SelectionPolicy = "all",
    min_success_count: int = 0,
) -> None:
    summary = _json_object(summary_bytes, label="phase-one summary")
    summary_keys = (
        _SUMMARY_KEYS_V1
        if manifest.artifact_schema_version == _PHASE1_ARTIFACT_SCHEMA_V1
        else _SUMMARY_KEYS_V2
    )
    _exact_keys(
        summary,
        summary_keys,
        label="phase-one summary",
    )
    if summary.get("run_id") != manifest.source.run_id:
        raise EvalPlusExportError("phase-one summary run_id differs from manifest")
    if summary.get("experiment_label") != manifest.source.experiment_label:
        raise EvalPlusExportError("phase-one summary experiment label differs from manifest")
    if summary.get("completed_at") != manifest.payload.get("completed_at"):
        raise EvalPlusExportError("phase-one summary completion timestamp differs from manifest")
    if summary.get("updated_at") != summary.get("completed_at"):
        raise EvalPlusExportError("completed phase-one summary has inconsistent timestamps")
    if (
        summary.get("total_problem_count") != expected_count
        or summary.get("dataset_problem_count") != expected_count
    ):
        raise EvalPlusExportError("phase-one summary dataset counts are invalid")

    records = responses.records
    history_counts = _counts([str(record["status"]) for record in records])
    if summary.get("record_count") != len(records):
        raise EvalPlusExportError("phase-one summary record count differs from responses")
    if (
        summary.get("record_status_counts") != history_counts
        or summary.get("status_counts") != history_counts
    ):
        raise EvalPlusExportError("phase-one summary status counts differ from responses")

    final_records = responses.final_non_skipped
    final_counts = Counter(str(record["status"]) for record in final_records.values())
    expected_final_counts = {
        "success": final_counts["success"],
        "parse_error": final_counts["parse_error"],
        "provider_error": final_counts["provider_error"],
        "failure": final_counts["parse_error"] + final_counts["provider_error"],
    }
    if summary.get("final_outcome_counts") != expected_final_counts:
        raise EvalPlusExportError("phase-one summary final outcomes differ from responses")
    if selection_policy == "all":
        if (
            summary.get("success_count") != expected_count
            or summary.get("parse_error_count") != 0
            or summary.get("provider_error_count") != 0
            or summary.get("failure_count") != 0
            or summary.get("pending_count") != 0
        ):
            raise EvalPlusExportError("phase-one summary does not describe ten final successes")
    else:
        if (
            summary.get("success_count") != final_counts["success"]
            or summary.get("parse_error_count") != final_counts["parse_error"]
            or summary.get("provider_error_count") != final_counts["provider_error"]
            or summary.get("failure_count")
            != final_counts["parse_error"] + final_counts["provider_error"]
            or summary.get("pending_count") != 0
            or summary.get("success_count") < min_success_count
        ):
            raise EvalPlusExportError("phase-one summary success counts are inconsistent")

    parse_counts = Counter(str(record["parse_status"]) for record in final_records.values())
    parsed = parse_counts["parsed"]
    failed = parse_counts["failed"]
    if selection_policy == "all":
        expected_parse_success_rate = 1.0
    else:
        expected_parse_success_rate = parsed / (parsed + failed) if parsed + failed else 0.0
    if (
        summary.get("parse_attempted_count") != parsed + failed
        or summary.get("parse_success_count") != parsed
        or summary.get("parse_failure_count") != failed
        or summary.get("parse_success_rate") != expected_parse_success_rate
    ):
        raise EvalPlusExportError("phase-one summary parse metrics differ from responses")
    durations = [float(record["duration_seconds"]) for record in final_records.values()]
    expected_average = sum(durations) / len(durations)
    actual_average = summary.get("average_duration_seconds")
    if (
        isinstance(actual_average, bool)
        or not isinstance(actual_average, int | float)
        or not math.isclose(float(actual_average), expected_average, rel_tol=1e-12, abs_tol=1e-12)
    ):
        raise EvalPlusExportError("phase-one summary average duration differs from responses")

    if manifest.artifact_schema_version == _PHASE1_ARTIFACT_SCHEMA_V2:
        final_values = list(final_records.values())
        attempt_histories = [record["attempt_outcomes"] for record in final_values]
        expected_observability_counts = {
            "first_attempt_parse_success_count": sum(
                bool(outcomes) and outcomes[0] == "success" for outcomes in attempt_histories
            ),
            "parse_failure_encountered_count": sum(
                "parse_error" in outcomes for outcomes in attempt_histories
            ),
            "repair_attempted_count": sum(
                "parse_error" in outcomes[:-1] for outcomes in attempt_histories
            ),
            "repair_success_count": sum(
                record["status"] == "success" and "parse_error" in record["attempt_outcomes"][:-1]
                for record in final_values
            ),
            "terminal_parse_error_count": sum(
                record["status"] == "parse_error" for record in final_values
            ),
        }
        for field, expected_value in expected_observability_counts.items():
            if _integer(summary.get(field), label=f"phase-one summary {field}") != expected_value:
                raise EvalPlusExportError(
                    "phase-one summary parse observability metrics differ from responses"
                )

        expected_attempt_average = sum(
            int(record["attempt_count"]) for record in final_values
        ) / len(final_values)
        expected_retry_average = sum(int(record["retry_count"]) for record in final_values) / len(
            final_values
        )
        for field, expected_value in (
            ("average_attempt_count", expected_attempt_average),
            ("average_retry_count", expected_retry_average),
        ):
            actual_value = summary.get(field)
            if (
                isinstance(actual_value, bool)
                or not isinstance(actual_value, int | float)
                or not math.isclose(
                    float(actual_value), expected_value, rel_tol=1e-12, abs_tol=1e-12
                )
            ):
                raise EvalPlusExportError(
                    "phase-one summary attempt averages differ from responses"
                )

    invocation = _mapping(summary.get("invocation"), label="phase-one summary invocation")
    _exact_keys(
        invocation,
        {"invocation_id", "started_at", "completed_at", "status_counts", "skipped_count"},
        label="phase-one summary invocation",
    )
    invocation_id = _text(invocation.get("invocation_id"), label="summary invocation_id")
    source_invocation = manifest.invocation_by_id.get(invocation_id)
    if source_invocation is None or source_invocation.get("status") != "completed":
        raise EvalPlusExportError("phase-one summary references an invalid invocation")
    invocation_records = [record for record in records if record["invocation_id"] == invocation_id]
    invocation_counts = _counts([str(record["status"]) for record in invocation_records])
    skipped_count = invocation_counts.get("skipped", 0)
    if (
        invocation.get("started_at") != source_invocation.get("started_at")
        or invocation.get("completed_at") != source_invocation.get("completed_at")
        or invocation.get("status_counts") != invocation_counts
        or invocation.get("skipped_count") != skipped_count
        or summary.get("skipped_count") != skipped_count
    ):
        raise EvalPlusExportError("phase-one summary invocation metrics differ from responses")
    if summary.get("metrics_scope") != "generation_and_parsing_only":
        raise EvalPlusExportError("phase-one summary metrics scope is invalid")


def load_validated_phase1_export(
    baseline_run_dir: str | Path,
    dataset_manifest_path: str | Path,
    *,
    selection_policy: SelectionPolicy = "all",
    min_success_count: int = 0,
) -> ValidatedSampleExport:
    """Return validated samples without creating files or executing code.

    Args:
        baseline_run_dir: Completed phase-one run directory.
        dataset_manifest_path: dataset_manifest.json for the selected cohort.
        selection_policy: ``all`` requires every dataset problem to have a
            successful phase-one response; ``phase1-success-only`` exports only
            successful responses.
        min_success_count: Minimum number of distinct successful problem IDs
            required when ``selection_policy`` is ``phase1-success-only``.
    """

    if selection_policy not in {"all", "phase1-success-only"}:
        raise EvalPlusExportError("selection_policy must be 'all' or 'phase1-success-only'")
    if (
        isinstance(min_success_count, bool)
        or not isinstance(min_success_count, int)
        or min_success_count < 0
    ):
        raise EvalPlusExportError("min_success_count must be a non-negative integer")
    if selection_policy == "phase1-success-only" and min_success_count < 1:
        raise EvalPlusExportError(
            "phase1-success-only requires min_success_count to be at least one"
        )

    raw_run_dir = Path(baseline_run_dir).expanduser()
    if raw_run_dir.is_symlink() or not raw_run_dir.is_dir():
        raise EvalPlusExportError("baseline run must be an existing non-symlink directory")
    run_dir = raw_run_dir.resolve()
    raw_dataset_manifest = Path(dataset_manifest_path).expanduser()
    if raw_dataset_manifest.is_symlink():
        raise EvalPlusExportError("dataset manifest must not be a symbolic link")
    dataset_manifest = raw_dataset_manifest.resolve()

    manifest_bytes = _read_regular_file(run_dir / "manifest.json", label="phase-one manifest")
    summary_bytes = _read_regular_file(run_dir / "summary.json", label="phase-one summary")
    responses_bytes = _read_regular_file(
        run_dir / "responses.jsonl", label="phase-one responses.jsonl"
    )
    dataset = _validate_dataset_manifest(dataset_manifest)
    manifest = _validate_phase1_manifest(run_dir, manifest_bytes, dataset)
    responses = _validate_responses(
        responses_bytes,
        manifest=manifest,
        dataset=dataset,
        selection_policy=selection_policy,
        min_success_count=min_success_count,
    )
    _validate_summary(
        summary_bytes,
        manifest=manifest,
        responses=responses,
        expected_count=len(dataset.problem_ids),
        selection_policy=selection_policy,
        min_success_count=min_success_count,
    )

    samples_bytes = serialize_samples_jsonl(responses.samples)
    source = Phase1SourceIdentity(
        run_id=manifest.source.run_id,
        experiment_label=manifest.source.experiment_label,
        manifest_sha256=manifest.source.manifest_sha256,
        summary_sha256=_sha256(summary_bytes),
        responses_sha256=_sha256(responses_bytes),
        git_commit=manifest.source.git_commit,
        git_branch=manifest.source.git_branch,
        git_dirty=manifest.source.git_dirty,
        provider=manifest.source.provider,
        model=manifest.source.model,
    )
    if selection_policy == "all":
        task_metadata = dataset.task_metadata
    else:
        success_ids = [sample.task_id for sample in responses.samples]
        metadata_by_id = {meta.problem_id: meta for meta in dataset.task_metadata}
        task_metadata = tuple(metadata_by_id[problem_id] for problem_id in success_ids)
    final_counts = Counter(str(record["status"]) for record in responses.final_non_skipped.values())
    source_problem_count = len(dataset.problem_ids)
    exported_success_count = len(responses.samples)
    excluded_parse_error_count = final_counts["parse_error"]
    excluded_provider_error_count = final_counts["provider_error"]
    if (
        exported_success_count + excluded_parse_error_count + excluded_provider_error_count
        != source_problem_count
    ):
        raise EvalPlusExportError("phase-one final outcomes do not cover the source cohort")
    return ValidatedSampleExport(
        phase1=source,
        dataset=dataset.identity,
        samples=responses.samples,
        response_references=responses.references,
        task_metadata=task_metadata,
        samples_sha256=_sha256(samples_bytes),
        export_selection=Phase1ExportSelectionIdentity(
            selection_policy=selection_policy,
            min_success_count=min_success_count,
            source_problem_count=source_problem_count,
            exported_success_count=exported_success_count,
            excluded_parse_error_count=excluded_parse_error_count,
            excluded_provider_error_count=excluded_provider_error_count,
        ),
    )


def export_phase1_samples(
    baseline_run_dir: str | Path,
    dataset_manifest_path: str | Path,
    *,
    selection_policy: SelectionPolicy = "all",
    min_success_count: int = 0,
) -> ValidatedSampleExport:
    """Compatibility alias emphasizing that the export remains in memory."""

    return load_validated_phase1_export(
        baseline_run_dir,
        dataset_manifest_path,
        selection_policy=selection_policy,
        min_success_count=min_success_count,
    )


__all__ = [
    "EvalPlusExportError",
    "PINNED_HUMANEVALPLUS_REVISION",
    "SelectionPolicy",
    "export_phase1_samples",
    "load_validated_phase1_export",
    "serialize_samples_jsonl",
]
