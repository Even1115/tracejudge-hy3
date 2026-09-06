"""Offline MBPP+ ingestion for public-prompt generation.

Source of truth is the official EvalPlus release corpus
(``evalplus/mbppplus_release`` tag ``v0.2.0``, file ``MbppPlus.jsonl``), i.e.
the exact corpus the pinned EvalPlus evaluator consumes inside the isolated
container.  The adapter deliberately creates a *public projection*: it never
executes candidates and never copies ``canonical_solution``, ``contract``,
``base_input``, ``plus_input``, ``atol``, or ``assertion`` into the
``ProblemSpec`` output.  Those raw fields stay in the pinned snapshot for the
later container-only EvalPlus execution stage.

Unlike HumanEval+, the official MBPP+ ``prompt`` field is a docstring-style
natural-language requirement; it does not contain a ``def`` line.  EvalPlus
publishes the function *name* as the separate ``entry_point`` field but does
not publish a full parameter signature, so the projection records the
name-only placeholder ``def <entry_point>(...):`` and tags every task with
``signature_not_published``.  MBPP+ also publishes no difficulty labels, so
``difficulty`` stays ``unknown``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tracejudge_hy3.dataset.loader import load_problems
from tracejudge_hy3.exceptions import DatasetError
from tracejudge_hy3.schemas.problem import ProblemSpec, RequirementItem

DATASET_ID = "evalplus/mbppplus"
DATASET_SOURCE = "evalplus_mbppplus"
EXPECTED_RECORD_COUNT = 378
MBPP_PLUS_VERSION = "v0.2.0"
PINNED_MBPPPLUS_REVISION = "64fc4195b858a17cdfdb3324f0baf37939144e14"
PINNED_MBPPPLUS_SOURCE_MANIFEST = "evalplus_mbppplus_64fc4195.json"
ADAPTER_NAME = "tracejudge_mbppplus_public_projection"
ADAPTER_VERSION = 1
PUBLIC_RAW_FIELDS = frozenset({"task_id", "prompt", "entry_point"})
REQUIRED_RAW_FIELDS = frozenset(
    {
        "task_id",
        "prompt",
        "entry_point",
        "canonical_solution",
        "contract",
        "base_input",
        "plus_input",
        "atol",
        "assertion",
    }
)
KNOWN_WITHHELD_FIELDS = (
    "assertion",
    "atol",
    "base_input",
    "canonical_solution",
    "contract",
    "plus_input",
)
ALLOWED_RAW_FIELDS = PUBLIC_RAW_FIELDS | frozenset(KNOWN_WITHHELD_FIELDS)
WITHHELD_REFERENCE_CODE = "# EVALPLUS_REFERENCE_CODE_WITHHELD_FROM_PHASE1\n"
SIGNATURE_NOT_PUBLISHED_TAG = "signature_not_published"
FULL_EXPERIMENT_LABEL = "mbppplus_378_public_prompt_generation_full"
SELECTION_MANIFEST_KIND = "tracejudge_mbppplus_dataset_selection"
FULL_PROJECTION_KIND = "tracejudge_mbppplus_public_projection"
SELECTION_ALGORITHM = "sha256(seed\\0problem_id)-lowest-v1"
FULL_SELECTION_ALGORITHM = "all-pinned-task-ids-numeric-order-v1"
SELECTION_LIMITATIONS = (
    "generation_and_parsing_only",
    "no_candidate_execution",
    "no_mbppplus_score_or_pass_at_k",
    "public_benchmark_training_contamination_is_possible",
)
ALLOWED_SELECTION_ROLES = frozenset({"pilot"})
DATASET_MANIFEST_SCHEMA_VERSION = 1

_TASK_ID_PATTERN = re.compile(r"^Mbpp/(?:0|[1-9][0-9]*)$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MD5_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_REVISION = PINNED_MBPPPLUS_REVISION


@dataclass(frozen=True, slots=True)
class ConversionResult:
    output_dir: Path
    dataset_path: Path
    manifest_path: Path
    record_count: int
    dataset_sha256: str
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class SampleResult:
    output_dir: Path
    dataset_path: Path
    manifest_path: Path
    selected_problem_ids: tuple[str, ...]
    dataset_sha256: str
    manifest_sha256: str


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        return _sha256_bytes(path.read_bytes())
    except FileNotFoundError as exc:
        raise DatasetError(f"required dataset file not found: {path}") from exc
    except OSError as exc:
        raise DatasetError(f"cannot read required dataset file: {path}") from exc


def _json_bytes(payload: Any) -> bytes:
    try:
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise DatasetError("dataset metadata cannot be encoded as strict UTF-8 JSON") from exc


def _jsonl_bytes(rows: Sequence[Mapping[str, Any]]) -> bytes:
    encoded: list[bytes] = []
    for row in rows:
        try:
            line = json.dumps(
                row,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise DatasetError("dataset row cannot be encoded as strict UTF-8 JSON") from exc
        encoded.append(line + b"\n")
    return b"".join(encoded)


def ordered_problem_ids_sha256(problem_ids: Sequence[str]) -> str:
    """Hash an ordered public task identity list using the manifest encoding."""

    return _sha256_bytes(_json_bytes(list(problem_ids)))


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_file(path: Path, payload: bytes) -> None:
    with path.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _publish_bundle(output_dir: Path, files: Mapping[str, bytes]) -> None:
    """Publish a complete immutable directory with one atomic rename.

    Re-running an identical conversion is idempotent.  A differing existing
    directory is never overwritten because it may be referenced by an
    experiment manifest.
    """

    output_dir = output_dir.resolve()
    temporary: Path | None = None
    try:
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))
        for relative_name, payload in files.items():
            destination = temporary / relative_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            _write_file(destination, payload)
        assert temporary is not None
        _fsync_directory(temporary)

        if output_dir.exists():
            existing_files = {
                str(path.relative_to(output_dir)): path.read_bytes()
                for path in output_dir.rglob("*")
                if path.is_file()
            }
            expected_files = dict(files)
            if existing_files != expected_files:
                raise DatasetError(
                    f"output directory already exists with different contents: {output_dir}"
                )
            return

        os.replace(temporary, output_dir)
        temporary = None
        _fsync_directory(output_dir.parent)
    except OSError as exc:
        raise DatasetError(f"cannot publish dataset bundle: {output_dir}") from exc
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary)


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DatasetError(f"{label} not found: {path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise DatasetError(f"{label} is not valid UTF-8 JSON: {path}") from None
    if not isinstance(payload, dict):
        raise DatasetError(f"{label} must contain a JSON object: {path}")
    return payload


def _manifest_raw_file(manifest: Mapping[str, Any], relative_path: str) -> Mapping[str, Any]:
    raw_files = manifest.get("raw_files")
    if not isinstance(raw_files, list):
        raise DatasetError("source manifest raw_files must be a list")
    matches = [
        item
        for item in raw_files
        if isinstance(item, Mapping) and item.get("path") == relative_path
    ]
    if len(matches) != 1:
        raise DatasetError(f"source manifest must identify raw file {relative_path!r} exactly once")
    return matches[0]


def _validate_expected_task_ids(value: Any) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) != EXPECTED_RECORD_COUNT
        or any(not isinstance(item, str) or not _TASK_ID_PATTERN.fullmatch(item) for item in value)
    ):
        raise DatasetError("source manifest expected_task_ids are invalid")
    if len(set(value)) != len(value):
        raise DatasetError("source manifest expected_task_ids contain duplicates")
    if value != sorted(value, key=_safe_task_id_number):
        raise DatasetError("source manifest expected_task_ids must use numeric task order")
    return list(value)


def _verify_source_manifest(
    *, input_path: Path, manifest_path: Path, revision: str
) -> tuple[dict[str, Any], str, str, list[str]]:
    manifest = _read_json_object(manifest_path, label="source manifest")
    allowed_manifest_fields = {
        "schema_version",
        "dataset_id",
        "dataset_card_url",
        "revision",
        "release_tag",
        "license",
        "record_count",
        "retrieved_at",
        "retrieved_with",
        "checksum_verification",
        "published_fields",
        "phase1_public_fields",
        "phase1_withheld_fields",
        "task_id_pattern",
        "expected_task_ids",
        "official_dataset_md5",
        "release_asset",
        "raw_files",
    }
    if set(manifest) - allowed_manifest_fields:
        raise DatasetError("source manifest contains fields outside the pinned schema")
    if manifest.get("schema_version") != 1:
        raise DatasetError("source manifest schema_version must be 1")
    if manifest.get("dataset_id") != DATASET_ID:
        raise DatasetError(f"source manifest dataset_id must be {DATASET_ID!r}")
    if not isinstance(revision, str) or _GIT_COMMIT_PATTERN.fullmatch(revision) is None:
        raise DatasetError("MBPP+ revision must be a full lowercase 40-character commit SHA")
    if revision != _REVISION:
        raise DatasetError(f"this adapter only supports the pinned MBPP+ revision {_REVISION!r}")
    if manifest.get("revision") != revision:
        raise DatasetError("source manifest revision does not match --revision")
    if manifest.get("release_tag") != MBPP_PLUS_VERSION:
        raise DatasetError(f"source manifest release_tag must be {MBPP_PLUS_VERSION!r}")
    if manifest.get("license") != "apache-2.0":
        raise DatasetError("source manifest license must be 'apache-2.0'")
    if manifest.get("record_count") != EXPECTED_RECORD_COUNT:
        raise DatasetError(
            f"source manifest record_count must be {EXPECTED_RECORD_COUNT} for this adapter"
        )
    if manifest.get("task_id_pattern") != _TASK_ID_PATTERN.pattern:
        raise DatasetError("source manifest task_id_pattern is invalid")
    expected_ids = _validate_expected_task_ids(manifest.get("expected_task_ids"))
    official_md5 = manifest.get("official_dataset_md5")
    if not isinstance(official_md5, str) or _MD5_PATTERN.fullmatch(official_md5) is None:
        raise DatasetError("source manifest official_dataset_md5 is invalid")
    published = manifest.get("published_fields")
    if not isinstance(published, list) or set(published) != set(ALLOWED_RAW_FIELDS):
        raise DatasetError("source manifest published_fields do not match the pinned schema")
    public_fields = manifest.get("phase1_public_fields")
    if not isinstance(public_fields, list) or set(public_fields) != set(PUBLIC_RAW_FIELDS):
        raise DatasetError("source manifest phase1_public_fields are invalid")
    withheld = manifest.get("phase1_withheld_fields")
    if not isinstance(withheld, list) or set(withheld) != set(KNOWN_WITHHELD_FIELDS):
        raise DatasetError("source manifest phase1_withheld_fields are invalid")

    release_asset = manifest.get("release_asset")
    if not isinstance(release_asset, Mapping) or set(release_asset) != {
        "name",
        "size",
        "sha256",
    }:
        raise DatasetError("source manifest release_asset fields are invalid")
    if (
        release_asset.get("name") != "MbppPlus.jsonl.gz"
        or not isinstance(release_asset.get("size"), int)
        or release_asset["size"] <= 0
        or not isinstance(release_asset.get("sha256"), str)
        or _SHA256_PATTERN.fullmatch(release_asset["sha256"]) is None
    ):
        raise DatasetError("source manifest release_asset identity is invalid")

    raw_files = manifest.get("raw_files")
    if not isinstance(raw_files, list) or not raw_files:
        raise DatasetError("source manifest raw_files must be a non-empty list")
    snapshot_root = input_path.parent
    for entry in raw_files:
        if not isinstance(entry, Mapping):
            raise DatasetError("source manifest raw_files entries must be JSON objects")
        if set(entry) != {"path", "size", "sha256"}:
            raise DatasetError("source manifest raw file entry fields are invalid")
        relative_text = entry.get("path")
        if not isinstance(relative_text, str) or not relative_text:
            raise DatasetError("source manifest raw file path must be a non-empty string")
        relative_path = Path(relative_text)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise DatasetError("source manifest raw file path must stay within the snapshot")
        local_path = snapshot_root / relative_path
        try:
            local_path.resolve().relative_to(snapshot_root.resolve())
        except ValueError as exc:
            raise DatasetError("source manifest raw file must resolve within the snapshot") from exc
        actual_size = local_path.stat().st_size if local_path.exists() else None
        actual_hash = _sha256_file(local_path)
        recorded_hash = entry.get("sha256")
        if (
            not isinstance(recorded_hash, str)
            or _SHA256_PATTERN.fullmatch(recorded_hash) is None
            or entry.get("size") != actual_size
            or recorded_hash != actual_hash
        ):
            raise DatasetError("raw MBPP+ snapshot does not match the pinned source manifest")

    source_entry = _manifest_raw_file(manifest, input_path.name)
    actual_hash = _sha256_file(input_path)
    if source_entry.get("sha256") != actual_hash:
        raise DatasetError("raw MBPP+ JSONL does not match the pinned source manifest")
    return manifest, _sha256_file(manifest_path), actual_hash, expected_ids


def _safe_task_id_number(task_id: str) -> int:
    prefix = "Mbpp/"
    if not task_id.startswith(prefix):
        raise DatasetError("MBPP+ task_id must use the 'Mbpp/<number>' form")
    suffix = task_id.removeprefix(prefix)
    if not suffix.isdigit() or str(int(suffix)) != suffix:
        raise DatasetError("MBPP+ task_id must end in a canonical decimal integer")
    return int(suffix)


def _read_raw_rows(path: Path, *, expected_ids: Sequence[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    try:
        stream = path.open(encoding="utf-8")
    except (FileNotFoundError, OSError) as exc:
        raise DatasetError(f"cannot open MBPP+ snapshot: {path}") from exc

    with stream:
        for line_number, raw_line in enumerate(stream, start=1):
            if not raw_line.strip():
                continue
            try:
                value = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise DatasetError(f"MBPP+ row {line_number} is not valid JSON") from None
            if not isinstance(value, dict):
                raise DatasetError(f"MBPP+ row {line_number} must be a JSON object")
            if not REQUIRED_RAW_FIELDS <= value.keys():
                raise DatasetError(f"MBPP+ row {line_number} is missing required fields")
            unknown_fields = set(value) - ALLOWED_RAW_FIELDS
            if unknown_fields:
                raise DatasetError(
                    f"MBPP+ row {line_number} contains fields outside the pinned schema"
                )
            for field in PUBLIC_RAW_FIELDS:
                if not isinstance(value.get(field), str) or not value[field]:
                    raise DatasetError(
                        f"MBPP+ row {line_number} has an invalid public field {field!r}"
                    )
            entry_point = value["entry_point"]
            if not entry_point.isascii() or not entry_point.isidentifier():
                raise DatasetError(f"MBPP+ row {line_number} has an invalid entry_point")
            # Validate private field shapes without evaluating or copying values.
            for field in ("canonical_solution", "contract", "assertion"):
                if not isinstance(value.get(field), str):
                    raise DatasetError(
                        f"MBPP+ row {line_number} has an invalid withheld field {field!r}"
                    )
            if not isinstance(value.get("base_input"), list):
                raise DatasetError(
                    f"MBPP+ row {line_number} has an invalid withheld field 'base_input'"
                )
            # Official quirk verified against the pinned v0.2.0 corpus: Mbpp/793
            # ships ``plus_input: {}``.  The official evaluator iterates it as
            # zero plus tests (vacuous pass), so only an *empty* mapping is an
            # acceptable list substitute; any other shape stays rejected.
            plus_input = value.get("plus_input")
            if isinstance(plus_input, Mapping):
                if plus_input:
                    raise DatasetError(
                        f"MBPP+ row {line_number} has an invalid withheld field 'plus_input'"
                    )
            elif not isinstance(plus_input, list):
                raise DatasetError(
                    f"MBPP+ row {line_number} has an invalid withheld field 'plus_input'"
                )
            atol = value.get("atol")
            if isinstance(atol, bool) or not isinstance(atol, int | float):
                raise DatasetError(f"MBPP+ row {line_number} has an invalid withheld field 'atol'")
            task_id = value["task_id"]
            if _TASK_ID_PATTERN.fullmatch(task_id) is None:
                raise DatasetError(f"MBPP+ row {line_number} has an invalid task_id")
            _safe_task_id_number(task_id)
            if task_id in seen_ids:
                raise DatasetError(f"duplicate MBPP+ task_id at row {line_number}")
            seen_ids.add(task_id)
            rows.append(value)

    expected_set = set(expected_ids)
    if len(rows) != len(expected_ids) or seen_ids != expected_set:
        raise DatasetError("MBPP+ snapshot must contain exactly the pinned expected_task_ids set")
    return sorted(rows, key=lambda row: _safe_task_id_number(row["task_id"]))


def _problem_from_public_fields(row: Mapping[str, Any]) -> ProblemSpec:
    task_id = str(row["task_id"])
    prompt = str(row["prompt"])
    entry_point = str(row["entry_point"])
    if entry_point not in prompt:
        raise DatasetError("public MBPP+ prompt must mention its entry_point")

    return ProblemSpec(
        problem_id=task_id,
        title=f"{task_id}: {entry_point}",
        requirement=prompt,
        # MBPP+ publishes only the function name, never a parameter signature.
        # This name-only placeholder keeps ``function_name`` honest without
        # fabricating parameters.
        function_signature=f"def {entry_point}(...):",
        requirements=[
            RequirementItem(
                requirement_id="R1",
                content=prompt,
                verification_hint=None,
            )
        ],
        visible_test_cases=[],
        hidden_test_cases=[],
        challenge_test_cases=[],
        reference_code=WITHHELD_REFERENCE_CODE,
        difficulty="unknown",
        source=DATASET_SOURCE,
        tags=[
            "public_benchmark",
            "mbppplus",
            "phase1_public_projection",
            SIGNATURE_NOT_PUBLISHED_TAG,
        ],
    )


def validate_mbppplus_public_problems(
    problems: Sequence[ProblemSpec],
    *,
    require_complete_snapshot: bool = False,
    expected_ids: Sequence[str] | None = None,
) -> None:
    """Enforce the MBPP+ public projection contract without executing any code."""

    if not problems:
        raise DatasetError("MBPP+ public projection must not be empty")
    problem_ids: list[str] = []
    for problem in problems:
        _safe_task_id_number(problem.problem_id)
        problem_ids.append(problem.problem_id)
        if problem.source != DATASET_SOURCE:
            raise DatasetError("MBPP+ public projection has an unexpected source")
        if problem.difficulty != "unknown":
            raise DatasetError("MBPP+ public projection difficulty must be 'unknown'")
        if problem.reference_code != WITHHELD_REFERENCE_CODE:
            raise DatasetError("MBPP+ public projection must withhold reference code")
        if problem.visible_test_cases or problem.hidden_test_cases or problem.challenge_test_cases:
            raise DatasetError("MBPP+ public projection must not contain tests")
        if len(problem.requirements) != 1 or problem.requirements[0].requirement_id != "R1":
            raise DatasetError("MBPP+ public projection must contain the public R1 clause")
        if problem.requirements[0].verification_hint is not None:
            raise DatasetError("MBPP+ public projection must not contain verification hints")
        entry_point = problem.function_name
        if not entry_point or f"{problem.problem_id}: {entry_point}" != problem.title:
            raise DatasetError("MBPP+ public projection title/entry_point identity mismatch")
        if problem.function_signature != f"def {entry_point}(...):":
            raise DatasetError("MBPP+ public projection signature placeholder is invalid")
        if SIGNATURE_NOT_PUBLISHED_TAG not in problem.tags:
            raise DatasetError("MBPP+ public projection must record that no signature is published")
    if len(problem_ids) != len(set(problem_ids)):
        raise DatasetError("MBPP+ public projection contains duplicate problem IDs")
    if problem_ids != sorted(problem_ids, key=_safe_task_id_number):
        raise DatasetError("MBPP+ public projection must use numeric task order")
    if require_complete_snapshot:
        if expected_ids is None:
            raise DatasetError("complete-snapshot validation requires the pinned expected IDs")
        if problem_ids != list(expected_ids):
            raise DatasetError("MBPP+ full projection must contain exactly the pinned task IDs")


def _validate_problem_bytes(payload: bytes, expected_ids: Sequence[str]) -> None:
    """Validate the exact serialized projection before publishing the bundle."""

    try:
        problems = [
            ProblemSpec.model_validate(json.loads(line))
            for line in payload.decode("utf-8").splitlines()
            if line
        ]
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise DatasetError("serialized public projection failed pre-publish validation") from None
    if [problem.problem_id for problem in problems] != list(expected_ids):
        raise DatasetError("serialized public projection changed task identity or order")
    validate_mbppplus_public_problems(problems)


def _aggregate_raw_snapshot_hash(raw_files: Sequence[Mapping[str, Any]]) -> str:
    identity = [
        {"path": item.get("path"), "size": item.get("size"), "sha256": item.get("sha256")}
        for item in raw_files
    ]
    return _sha256_bytes(_json_bytes(identity))


def convert_mbppplus(
    *,
    input_path: str | Path,
    revision: str,
    source_manifest_path: str | Path,
    output_dir: str | Path,
) -> ConversionResult:
    """Convert the pinned 378-row official release into a public projection."""

    resolved_input = Path(input_path).expanduser().resolve()
    resolved_source_manifest = Path(source_manifest_path).expanduser().resolve()
    resolved_output = Path(output_dir).expanduser().resolve()
    (
        source_manifest,
        source_manifest_hash,
        raw_jsonl_hash,
        expected_ids,
    ) = _verify_source_manifest(
        input_path=resolved_input,
        manifest_path=resolved_source_manifest,
        revision=revision,
    )
    raw_rows = _read_raw_rows(resolved_input, expected_ids=expected_ids)

    problems: list[ProblemSpec] = []
    withheld_field_names: set[str] = set()
    for row in raw_rows:
        problem = _problem_from_public_fields(row)
        problems.append(problem)
        withheld_field_names.update((set(row) - PUBLIC_RAW_FIELDS) & set(KNOWN_WITHHELD_FIELDS))

    problem_rows = [problem.model_dump(mode="json") for problem in problems]
    problems_bytes = _jsonl_bytes(problem_rows)
    raw_files = source_manifest.get("raw_files")
    assert isinstance(raw_files, list)
    ordered_ids = [problem.problem_id for problem in problems]
    validate_mbppplus_public_problems(
        problems,
        require_complete_snapshot=True,
        expected_ids=expected_ids,
    )
    _validate_problem_bytes(problems_bytes, ordered_ids)
    manifest: dict[str, Any] = {
        "schema_version": DATASET_MANIFEST_SCHEMA_VERSION,
        "kind": FULL_PROJECTION_KIND,
        "experiment_label": FULL_EXPERIMENT_LABEL,
        "metrics_scope": "generation_and_parsing_only",
        "dataset_id": DATASET_ID,
        "source": DATASET_SOURCE,
        "revision": revision,
        "release_tag": source_manifest.get("release_tag"),
        "license": source_manifest.get("license"),
        "adapter": {"name": ADAPTER_NAME, "version": ADAPTER_VERSION},
        "source_manifest_sha256": source_manifest_hash,
        "raw_snapshot": {
            "aggregate_sha256": _aggregate_raw_snapshot_hash(raw_files),
            "jsonl_sha256": raw_jsonl_hash,
            "official_dataset_md5": source_manifest.get("official_dataset_md5"),
            "record_count": len(raw_rows),
        },
        "public_projection": {
            "path": "problems.jsonl",
            "sha256": _sha256_bytes(problems_bytes),
            "record_count": len(problems),
            "ordered_problem_ids_sha256": ordered_problem_ids_sha256(ordered_ids),
        },
        "selection": {
            "algorithm": FULL_SELECTION_ALGORITHM,
            "count": len(problems),
            "selected_problem_ids": ordered_ids,
        },
        "withheld_fields": sorted(withheld_field_names),
    }
    manifest_bytes = _json_bytes(manifest)
    _publish_bundle(
        resolved_output,
        {
            "problems.jsonl": problems_bytes,
            "dataset_manifest.json": manifest_bytes,
        },
    )
    return ConversionResult(
        output_dir=resolved_output,
        dataset_path=resolved_output / "problems.jsonl",
        manifest_path=resolved_output / "dataset_manifest.json",
        record_count=len(problems),
        dataset_sha256=_sha256_bytes(problems_bytes),
        manifest_sha256=_sha256_bytes(manifest_bytes),
    )


def _load_bundle_manifest(path: Path) -> dict[str, Any]:
    payload = _read_json_object(path, label="dataset bundle manifest")
    expected_top_level = {
        "schema_version",
        "kind",
        "experiment_label",
        "metrics_scope",
        "dataset_id",
        "source",
        "revision",
        "release_tag",
        "license",
        "adapter",
        "source_manifest_sha256",
        "raw_snapshot",
        "public_projection",
        "selection",
        "withheld_fields",
    }
    if set(payload) != expected_top_level:
        raise DatasetError("sample input manifest contains fields outside the pinned schema")
    if payload.get("schema_version") != DATASET_MANIFEST_SCHEMA_VERSION:
        raise DatasetError("sample input manifest schema_version must be 1")
    if payload.get("kind") != FULL_PROJECTION_KIND:
        raise DatasetError("sample input manifest is not an MBPP+ public projection")
    if payload.get("dataset_id") != DATASET_ID:
        raise DatasetError("sample input manifest has an unexpected dataset_id")
    if payload.get("source") != DATASET_SOURCE:
        raise DatasetError("sample input manifest has an unexpected source")
    if payload.get("experiment_label") != FULL_EXPERIMENT_LABEL:
        raise DatasetError("sample input manifest has an unexpected experiment label")
    if payload.get("metrics_scope") != "generation_and_parsing_only":
        raise DatasetError("sample input manifest has an unexpected metrics scope")
    if payload.get("release_tag") != MBPP_PLUS_VERSION or payload.get("license") != "apache-2.0":
        raise DatasetError("sample input manifest release/license identity is invalid")
    revision = payload.get("revision")
    if not isinstance(revision, str) or revision != _REVISION:
        raise DatasetError("sample input manifest revision is invalid")
    if payload.get("adapter") != {"name": ADAPTER_NAME, "version": ADAPTER_VERSION}:
        raise DatasetError("sample input manifest adapter identity is invalid")
    for field in ("source_manifest_sha256",):
        value = payload.get(field)
        if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
            raise DatasetError(f"sample input manifest {field} is invalid")

    projection = payload.get("public_projection")
    selection = payload.get("selection")
    raw_snapshot = payload.get("raw_snapshot")
    if not all(isinstance(item, Mapping) for item in (projection, selection, raw_snapshot)):
        raise DatasetError("sample input manifest is missing structured identity fields")
    assert isinstance(projection, Mapping)
    assert isinstance(selection, Mapping)
    assert isinstance(raw_snapshot, Mapping)
    if set(projection) != {
        "path",
        "sha256",
        "record_count",
        "ordered_problem_ids_sha256",
    }:
        raise DatasetError("sample input manifest projection fields are invalid")
    if set(selection) != {"algorithm", "count", "selected_problem_ids"}:
        raise DatasetError("sample input manifest selection fields are invalid")
    if set(raw_snapshot) != {
        "aggregate_sha256",
        "jsonl_sha256",
        "official_dataset_md5",
        "record_count",
    }:
        raise DatasetError("sample input manifest raw snapshot fields are invalid")
    if projection.get("path") != "problems.jsonl" or projection.get("record_count") != (
        EXPECTED_RECORD_COUNT
    ):
        raise DatasetError("sample input manifest public projection identity is invalid")
    if (
        selection.get("algorithm") != FULL_SELECTION_ALGORITHM
        or selection.get("count") != EXPECTED_RECORD_COUNT
        or not isinstance(selection.get("selected_problem_ids"), list)
        or len(selection["selected_problem_ids"]) != EXPECTED_RECORD_COUNT
        or selection["selected_problem_ids"]
        != sorted(selection["selected_problem_ids"], key=_safe_task_id_number)
    ):
        raise DatasetError("sample input manifest full selection identity is invalid")
    expected_ids = list(selection["selected_problem_ids"])
    if projection.get("ordered_problem_ids_sha256") != ordered_problem_ids_sha256(expected_ids):
        raise DatasetError("sample input manifest public projection identity is invalid")
    if raw_snapshot.get("record_count") != EXPECTED_RECORD_COUNT:
        raise DatasetError("sample input manifest raw record count is invalid")
    for field in ("aggregate_sha256", "jsonl_sha256"):
        value = raw_snapshot.get(field)
        if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
            raise DatasetError(f"sample input manifest raw snapshot {field} is invalid")
    official_md5 = raw_snapshot.get("official_dataset_md5")
    if not isinstance(official_md5, str) or _MD5_PATTERN.fullmatch(official_md5) is None:
        raise DatasetError("sample input manifest official dataset MD5 is invalid")
    withheld_fields = payload.get("withheld_fields")
    if (
        not isinstance(withheld_fields, list)
        or not all(isinstance(field, str) and field for field in withheld_fields)
        or not {"canonical_solution", "base_input", "plus_input"} <= set(withheld_fields)
        or not set(withheld_fields) <= set(KNOWN_WITHHELD_FIELDS)
    ):
        raise DatasetError("sample input manifest withheld field identity is invalid")
    payload["_expected_task_ids"] = expected_ids
    return payload


def _selection_rank(*, seed: int, problem_id: str) -> str:
    return hashlib.sha256(f"{seed}\0{problem_id}".encode()).hexdigest()


def select_mbppplus_problem_ids(
    *,
    available_ids: Sequence[str],
    count: int,
    seed: int,
    exclude_ids: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Return the reproducible public-ID-only selection for a given universe.

    The universe is the ordered public ID list of a validated projection, so
    no dataset content is needed to reproduce a cohort.  When ``exclude_ids``
    is supplied, those public IDs are removed from the universe before
    ranking; the same seed therefore produces disjoint cohorts for disjoint
    exclusion sets.
    """

    if isinstance(available_ids, str | bytes) or not isinstance(available_ids, Sequence):
        raise DatasetError("available MBPP+ IDs must be a sequence")
    universe = list(available_ids)
    if not universe or any(
        not isinstance(problem_id, str) or not _TASK_ID_PATTERN.fullmatch(problem_id)
        for problem_id in universe
    ):
        raise DatasetError("available MBPP+ IDs are invalid")
    if len(universe) != len(set(universe)):
        raise DatasetError("available MBPP+ IDs contain duplicates")
    if universe != sorted(universe, key=_safe_task_id_number):
        raise DatasetError("available MBPP+ IDs must use numeric task order")
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise DatasetError("sample count must be greater than zero")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise DatasetError("sample seed must be a non-negative integer")
    excluded = frozenset(exclude_ids) if exclude_ids is not None else frozenset()
    unknown = excluded - set(universe)
    if unknown:
        raise DatasetError(f"exclusion set contains unknown problem IDs: {sorted(unknown)}")
    available = len(universe) - len(excluded)
    if count > available:
        raise DatasetError(
            f"sample count {count} exceeds the available problem count "
            f"after excluding {len(excluded)} tasks"
        )
    ranked = sorted(
        (problem_id for problem_id in universe if problem_id not in excluded),
        key=lambda problem_id: (
            _selection_rank(seed=seed, problem_id=problem_id),
            problem_id,
        ),
    )
    return tuple(sorted(ranked[:count], key=_safe_task_id_number))


def _load_exclude_manifest(path: Path) -> dict[str, Any]:
    """Validate an MBPP+ selection manifest used to exclude tasks from a cohort."""

    payload = _read_json_object(path, label="exclusion dataset manifest")
    expected_top_level = {
        "schema_version",
        "kind",
        "experiment_label",
        "metrics_scope",
        "dataset_id",
        "source",
        "revision",
        "release_tag",
        "license",
        "adapter",
        "source_manifest_sha256",
        "parent_manifest_sha256",
        "raw_snapshot",
        "public_projection",
        "selection",
        "withheld_fields",
        "limitations",
        "excluded_manifests",
    }
    if set(payload) != expected_top_level:
        raise DatasetError("exclusion manifest contains fields outside the pinned schema")
    if payload.get("schema_version") != DATASET_MANIFEST_SCHEMA_VERSION:
        raise DatasetError("exclusion manifest schema_version must be 1")
    if payload.get("kind") != SELECTION_MANIFEST_KIND:
        raise DatasetError("exclusion manifest is not an MBPP+ dataset selection")
    if payload.get("dataset_id") != DATASET_ID:
        raise DatasetError("exclusion manifest dataset_id does not match the parent projection")
    if payload.get("source") != DATASET_SOURCE:
        raise DatasetError("exclusion manifest source does not match the parent projection")
    if payload.get("metrics_scope") != "generation_and_parsing_only":
        raise DatasetError("exclusion manifest metrics_scope is invalid")
    if payload.get("release_tag") != MBPP_PLUS_VERSION or payload.get("license") != "apache-2.0":
        raise DatasetError("exclusion manifest release/license identity is invalid")
    if payload.get("revision") != _REVISION:
        raise DatasetError("exclusion manifest revision is invalid")
    if payload.get("adapter") != {"name": ADAPTER_NAME, "version": ADAPTER_VERSION}:
        raise DatasetError("exclusion manifest adapter identity is invalid")
    selection = payload.get("selection")
    if not isinstance(selection, Mapping):
        raise DatasetError("exclusion manifest selection is invalid")
    if set(selection) != {
        "algorithm",
        "seed",
        "count",
        "selected_problem_ids",
        "selected_problem_ids_sha256",
        "excluded_problem_ids",
        "excluded_problem_ids_sha256",
        "excluded_manifests_count",
        "excluded_manifests_sha256",
    }:
        raise DatasetError("exclusion manifest selection fields are invalid")
    if selection.get("algorithm") != SELECTION_ALGORITHM:
        raise DatasetError("exclusion manifest selection algorithm is invalid")
    selected_ids = selection.get("selected_problem_ids")
    if not isinstance(selected_ids, list) or not all(
        isinstance(problem_id, str) for problem_id in selected_ids
    ):
        raise DatasetError("exclusion manifest selected_problem_ids are invalid")
    return payload


def sample_mbppplus(
    *,
    dataset_path: str | Path,
    source_manifest_path: str | Path,
    count: int,
    seed: int,
    output_dir: str | Path,
    exclude_manifests: Sequence[str | Path] | None = None,
) -> SampleResult:
    """Select MBPP+ task IDs deterministically using only public identifiers.

    Args:
        dataset_path: Complete public projection ``problems.jsonl``.
        source_manifest_path: The full public projection manifest.
        count: Number of tasks to select.
        seed: Fixed seed combined with public ``problem_id`` for ranking.
        output_dir: Atomic-publish target directory.
        exclude_manifests: Optional MBPP+ selection manifests whose
            ``selected_problem_ids`` are removed from the universe before
            ranking.
    """

    resolved_dataset = Path(dataset_path).expanduser().resolve()
    resolved_manifest = Path(source_manifest_path).expanduser().resolve()
    resolved_output = Path(output_dir).expanduser().resolve()
    parent_manifest = _load_bundle_manifest(resolved_manifest)
    expected_ids = parent_manifest.pop("_expected_task_ids")
    parent_raw_snapshot = parent_manifest.get("raw_snapshot")
    assert isinstance(parent_raw_snapshot, Mapping)
    dataset_hash = _sha256_file(resolved_dataset)
    projection = parent_manifest.get("public_projection")
    if not isinstance(projection, Mapping) or projection.get("sha256") != dataset_hash:
        raise DatasetError("full public projection does not match its dataset manifest")

    excluded_manifest_records: list[dict[str, Any]] = []
    excluded_problem_ids: list[str] = []
    seen_excluded_ids: set[str] = set()
    exclude_paths = [Path(item) for item in (exclude_manifests or [])]
    for exclude_path in exclude_paths:
        resolved_exclude = exclude_path.expanduser().resolve()
        exclude_payload = _load_exclude_manifest(resolved_exclude)
        exclude_selection = exclude_payload.get("selection")
        assert isinstance(exclude_selection, Mapping)
        ids = list(exclude_selection.get("selected_problem_ids", []))
        for problem_id in ids:
            if problem_id in seen_excluded_ids:
                raise DatasetError(f"duplicate problem ID {problem_id} across exclusion manifests")
            seen_excluded_ids.add(problem_id)
            excluded_problem_ids.append(problem_id)
        excluded_manifest_records.append(
            {
                "manifest_sha256": _sha256_file(resolved_exclude),
                "kind": exclude_payload.get("kind"),
                "experiment_label": exclude_payload.get("experiment_label"),
                "selected_problem_ids": ids,
            }
        )

    selected_id_list = list(
        select_mbppplus_problem_ids(
            available_ids=expected_ids,
            count=count,
            seed=seed,
            exclude_ids=excluded_problem_ids,
        )
    )
    selected_set = set(selected_id_list)
    overlap = selected_set & seen_excluded_ids
    if overlap:
        raise DatasetError(f"selected problem IDs overlap with excluded set: {sorted(overlap)}")

    problems = load_problems(resolved_dataset)
    validate_mbppplus_public_problems(
        problems,
        require_complete_snapshot=True,
        expected_ids=expected_ids,
    )
    problems_by_id = {problem.problem_id: problem for problem in problems}
    selected = [problems_by_id[problem_id] for problem_id in selected_id_list]
    selected_bytes = _jsonl_bytes([problem.model_dump(mode="json") for problem in selected])
    _validate_problem_bytes(selected_bytes, selected_id_list)

    sorted_excluded = sorted(excluded_problem_ids, key=_safe_task_id_number)
    selection_block: dict[str, Any] = {
        "algorithm": SELECTION_ALGORITHM,
        "seed": seed,
        "count": count,
        "selected_problem_ids": selected_id_list,
        "selected_problem_ids_sha256": ordered_problem_ids_sha256(selected_id_list),
        "excluded_problem_ids": sorted_excluded,
        "excluded_problem_ids_sha256": (
            ordered_problem_ids_sha256(sorted_excluded) if sorted_excluded else None
        ),
        "excluded_manifests_count": len(excluded_manifest_records),
        "excluded_manifests_sha256": _sha256_bytes(
            _json_bytes(
                [
                    {"manifest_sha256": record["manifest_sha256"]}
                    for record in excluded_manifest_records
                ]
            )
        ),
    }

    manifest: dict[str, Any] = {
        "schema_version": DATASET_MANIFEST_SCHEMA_VERSION,
        "kind": SELECTION_MANIFEST_KIND,
        "experiment_label": f"mbppplus_{count}_public_prompt_generation_pilot",
        "metrics_scope": "generation_and_parsing_only",
        "dataset_id": DATASET_ID,
        "source": DATASET_SOURCE,
        "revision": parent_manifest.get("revision"),
        "release_tag": parent_manifest.get("release_tag"),
        "license": parent_manifest.get("license"),
        "adapter": parent_manifest.get("adapter"),
        "source_manifest_sha256": parent_manifest.get("source_manifest_sha256"),
        "parent_manifest_sha256": _sha256_file(resolved_manifest),
        "raw_snapshot": {
            "aggregate_sha256": parent_raw_snapshot.get("aggregate_sha256"),
            "jsonl_sha256": parent_raw_snapshot.get("jsonl_sha256"),
            "official_dataset_md5": parent_raw_snapshot.get("official_dataset_md5"),
            "record_count": parent_raw_snapshot.get("record_count"),
        },
        "public_projection": {
            "path": "problems.jsonl",
            "sha256": _sha256_bytes(selected_bytes),
            "record_count": len(selected),
            "ordered_problem_ids_sha256": ordered_problem_ids_sha256(selected_id_list),
        },
        "selection": selection_block,
        "withheld_fields": parent_manifest.get("withheld_fields"),
        "limitations": list(SELECTION_LIMITATIONS),
        "excluded_manifests": excluded_manifest_records,
    }
    manifest_bytes = _json_bytes(manifest)
    _publish_bundle(
        resolved_output,
        {"problems.jsonl": selected_bytes, "dataset_manifest.json": manifest_bytes},
    )
    return SampleResult(
        output_dir=resolved_output,
        dataset_path=resolved_output / "problems.jsonl",
        manifest_path=resolved_output / "dataset_manifest.json",
        selected_problem_ids=tuple(selected_id_list),
        dataset_sha256=_sha256_bytes(selected_bytes),
        manifest_sha256=_sha256_bytes(manifest_bytes),
    )


__all__ = [
    "ADAPTER_NAME",
    "ADAPTER_VERSION",
    "ALLOWED_RAW_FIELDS",
    "ALLOWED_SELECTION_ROLES",
    "DATASET_ID",
    "DATASET_MANIFEST_SCHEMA_VERSION",
    "DATASET_SOURCE",
    "EXPECTED_RECORD_COUNT",
    "FULL_EXPERIMENT_LABEL",
    "FULL_PROJECTION_KIND",
    "FULL_SELECTION_ALGORITHM",
    "KNOWN_WITHHELD_FIELDS",
    "MBPP_PLUS_VERSION",
    "PINNED_MBPPPLUS_REVISION",
    "PINNED_MBPPPLUS_SOURCE_MANIFEST",
    "PUBLIC_RAW_FIELDS",
    "SELECTION_ALGORITHM",
    "SELECTION_LIMITATIONS",
    "SELECTION_MANIFEST_KIND",
    "SIGNATURE_NOT_PUBLISHED_TAG",
    "WITHHELD_REFERENCE_CODE",
    "ConversionResult",
    "SampleResult",
    "convert_mbppplus",
    "ordered_problem_ids_sha256",
    "sample_mbppplus",
    "select_mbppplus_problem_ids",
    "validate_mbppplus_public_problems",
]
