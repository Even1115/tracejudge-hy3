"""Offline HumanEval+ ingestion for phase-one public-prompt generation.

The adapter deliberately creates a *public projection*.  It never executes or
copies ``canonical_solution``/``test`` into the ProblemSpec output.  Those raw
fields stay in the pinned Hugging Face snapshot for a later EvalPlus execution
stage.  This keeps phase one independent of evaluation-only material even if a
future prompt builder accidentally broadens its field selection.
"""

from __future__ import annotations

import ast
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

DATASET_ID = "evalplus/humanevalplus"
DATASET_SOURCE = "evalplus_humanevalplus"
EXPECTED_RECORD_COUNT = 164
ADAPTER_NAME = "tracejudge_humanevalplus_public_projection"
ADAPTER_VERSION = 1
PUBLIC_RAW_FIELDS = frozenset({"task_id", "prompt", "entry_point"})
REQUIRED_RAW_FIELDS = frozenset({"task_id", "prompt", "entry_point", "canonical_solution", "test"})
KNOWN_WITHHELD_FIELDS = (
    "canonical_solution",
    "test",
    "base_input",
    "plus_input",
    "contract",
    "atol",
    "expected",
    "expected_output",
    "oracle",
    "human_annotation",
)
ALLOWED_RAW_FIELDS = PUBLIC_RAW_FIELDS | frozenset(KNOWN_WITHHELD_FIELDS)
WITHHELD_REFERENCE_CODE = "# EVALPLUS_REFERENCE_CODE_WITHHELD_FROM_PHASE1\n"
FULL_EXPERIMENT_LABEL = "humanevalplus_164_public_prompt_generation_pilot"
PILOT_EXPERIMENT_LABEL = "humanevalplus_10_public_prompt_generation_pilot"
FULL_SELECTION_ALGORITHM = "all-pinned-task-ids-numeric-order-v1"
SELECTION_ALGORITHM = "sha256(seed\\0problem_id)-lowest-v1"
PILOT_LIMITATIONS = (
    "generation_and_parsing_only",
    "no_candidate_execution",
    "no_humanevalplus_score_or_pass_at_k",
    "public_benchmark_training_contamination_is_possible",
)


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


@dataclass(frozen=True, slots=True)
class ValidationResult:
    dataset_path: Path
    problem_count: int
    dataset_sha256: str
    sources: tuple[str, ...]
    difficulties: tuple[str, ...]


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


def _verify_source_manifest(
    *, input_path: Path, manifest_path: Path, revision: str
) -> tuple[dict[str, Any], str, str]:
    manifest = _read_json_object(manifest_path, label="source manifest")
    allowed_manifest_fields = {
        "schema_version",
        "dataset_id",
        "dataset_card_url",
        "revision",
        "split",
        "license",
        "record_count",
        "retrieved_at",
        "retrieved_with",
        "checksum_verification",
        "published_fields",
        "phase1_public_fields",
        "phase1_withheld_fields",
        "raw_files",
    }
    if set(manifest) - allowed_manifest_fields:
        raise DatasetError("source manifest contains fields outside the pinned schema")
    if manifest.get("schema_version") != 1:
        raise DatasetError("source manifest schema_version must be 1")
    if manifest.get("dataset_id") != DATASET_ID:
        raise DatasetError(f"source manifest dataset_id must be {DATASET_ID!r}")
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise DatasetError("HumanEval+ revision must be a full lowercase 40-character commit SHA")
    if manifest.get("revision") != revision:
        raise DatasetError("source manifest revision does not match --revision")
    if manifest.get("split") != "test":
        raise DatasetError("source manifest split must be 'test'")
    if manifest.get("license") != "apache-2.0":
        raise DatasetError("source manifest license must be 'apache-2.0'")
    if manifest.get("record_count") != EXPECTED_RECORD_COUNT:
        raise DatasetError(
            f"source manifest record_count must be {EXPECTED_RECORD_COUNT} for this adapter"
        )

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
            or re.fullmatch(r"[0-9a-f]{64}", recorded_hash) is None
            or entry.get("size") != actual_size
            or recorded_hash != actual_hash
        ):
            raise DatasetError("raw HumanEval+ snapshot does not match the pinned source manifest")

    source_entry = _manifest_raw_file(manifest, input_path.name)
    actual_hash = _sha256_file(input_path)
    if source_entry.get("sha256") != actual_hash:
        raise DatasetError("raw HumanEval+ JSONL does not match the pinned source manifest")
    return manifest, _sha256_file(manifest_path), actual_hash


def _safe_task_id_number(task_id: str) -> int:
    prefix = "HumanEval/"
    if not task_id.startswith(prefix):
        raise DatasetError("HumanEval+ task_id must use the 'HumanEval/<number>' form")
    suffix = task_id.removeprefix(prefix)
    if not suffix.isdigit() or str(int(suffix)) != suffix:
        raise DatasetError("HumanEval+ task_id must end in a canonical decimal integer")
    return int(suffix)


def _read_raw_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    try:
        stream = path.open(encoding="utf-8")
    except (FileNotFoundError, OSError) as exc:
        raise DatasetError(f"cannot open HumanEval+ snapshot: {path}") from exc

    with stream:
        for line_number, raw_line in enumerate(stream, start=1):
            if not raw_line.strip():
                continue
            try:
                value = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise DatasetError(f"HumanEval+ row {line_number} is not valid JSON") from None
            if not isinstance(value, dict):
                raise DatasetError(f"HumanEval+ row {line_number} must be a JSON object")
            if not REQUIRED_RAW_FIELDS <= value.keys():
                raise DatasetError(f"HumanEval+ row {line_number} is missing required fields")
            unknown_fields = set(value) - ALLOWED_RAW_FIELDS
            if unknown_fields:
                raise DatasetError(
                    f"HumanEval+ row {line_number} contains fields outside the pinned schema"
                )
            for field in PUBLIC_RAW_FIELDS:
                if not isinstance(value.get(field), str) or not value[field]:
                    raise DatasetError(
                        f"HumanEval+ row {line_number} has an invalid public field {field!r}"
                    )
            # Validate private field shape without evaluating or copying its value.
            for field in ("canonical_solution", "test"):
                if not isinstance(value.get(field), str):
                    raise DatasetError(
                        f"HumanEval+ row {line_number} has an invalid withheld field {field!r}"
                    )
            task_id = value["task_id"]
            _safe_task_id_number(task_id)
            if task_id in seen_ids:
                raise DatasetError(f"duplicate HumanEval+ task_id at row {line_number}")
            seen_ids.add(task_id)
            rows.append(value)

    expected_ids = {f"HumanEval/{index}" for index in range(EXPECTED_RECORD_COUNT)}
    if len(rows) != EXPECTED_RECORD_COUNT or seen_ids != expected_ids:
        raise DatasetError(
            "HumanEval+ snapshot must contain exactly the pinned HumanEval/0..HumanEval/163 set"
        )
    return sorted(rows, key=lambda row: _safe_task_id_number(row["task_id"]))


def _function_from_public_prompt(prompt: str, entry_point: str) -> ast.FunctionDef:
    try:
        tree = ast.parse(prompt, filename="<public-humaneval-prompt>", mode="exec")
    except (SyntaxError, ValueError) as exc:
        raise DatasetError("public HumanEval+ prompt is not valid Python syntax") from exc
    functions = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == entry_point
    ]
    if len(functions) != 1:
        raise DatasetError("public HumanEval+ prompt must define its entry_point exactly once")
    return functions[0]


def _function_signature(function: ast.FunctionDef) -> str:
    skeleton = ast.FunctionDef(
        name=function.name,
        args=function.args,
        body=[ast.Pass()],
        decorator_list=[],
        returns=function.returns,
        type_comment=function.type_comment,
    )
    ast.fix_missing_locations(skeleton)
    rendered = ast.unparse(skeleton)
    signature = rendered.splitlines()[0].strip()
    if not signature.startswith("def ") or not signature.endswith(":"):
        raise DatasetError("cannot extract a stable function signature from public prompt")
    return signature


def _problem_from_public_fields(row: Mapping[str, Any]) -> ProblemSpec:
    task_id = str(row["task_id"])
    prompt = str(row["prompt"])
    entry_point = str(row["entry_point"])
    function = _function_from_public_prompt(prompt, entry_point)
    docstring = ast.get_docstring(function, clean=False)
    if not docstring:
        raise DatasetError("public HumanEval+ entry_point must contain a docstring requirement")

    return ProblemSpec(
        problem_id=task_id,
        title=f"{task_id}: {entry_point}",
        requirement=prompt,
        function_signature=_function_signature(function),
        requirements=[
            RequirementItem(
                requirement_id="R1",
                content=docstring,
                verification_hint=None,
            )
        ],
        visible_test_cases=[],
        hidden_test_cases=[],
        challenge_test_cases=[],
        reference_code=WITHHELD_REFERENCE_CODE,
        difficulty="unknown",
        source=DATASET_SOURCE,
        tags=["public_benchmark", "humanevalplus", "phase1_public_projection"],
    )


def validate_humanevalplus_public_problems(
    problems: Sequence[ProblemSpec],
    *,
    require_complete_snapshot: bool = False,
) -> None:
    """Enforce the phase-one projection contract without executing any code."""

    if not problems:
        raise DatasetError("HumanEval+ public projection must not be empty")
    problem_ids: list[str] = []
    for problem in problems:
        _safe_task_id_number(problem.problem_id)
        problem_ids.append(problem.problem_id)
        if problem.source != DATASET_SOURCE:
            raise DatasetError("HumanEval+ public projection has an unexpected source")
        if problem.difficulty != "unknown":
            raise DatasetError("HumanEval+ public projection difficulty must be 'unknown'")
        if problem.reference_code != WITHHELD_REFERENCE_CODE:
            raise DatasetError("HumanEval+ public projection must withhold reference code")
        if problem.visible_test_cases or problem.hidden_test_cases or problem.challenge_test_cases:
            raise DatasetError("HumanEval+ phase-one public projection must not contain tests")
        if len(problem.requirements) != 1 or problem.requirements[0].requirement_id != "R1":
            raise DatasetError("HumanEval+ public projection must contain the public R1 clause")
        if problem.requirements[0].verification_hint is not None:
            raise DatasetError("HumanEval+ public projection must not contain verification hints")
        if problem.function_name not in problem.title:
            raise DatasetError("HumanEval+ public projection title/signature identity mismatch")
    if len(problem_ids) != len(set(problem_ids)):
        raise DatasetError("HumanEval+ public projection contains duplicate problem IDs")
    if problem_ids != sorted(problem_ids, key=_safe_task_id_number):
        raise DatasetError("HumanEval+ public projection must use numeric task order")
    if require_complete_snapshot:
        expected_ids = [f"HumanEval/{index}" for index in range(EXPECTED_RECORD_COUNT)]
        if problem_ids != expected_ids:
            raise DatasetError("HumanEval+ full projection must contain HumanEval/0..HumanEval/163")


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
    validate_humanevalplus_public_problems(problems)


def _aggregate_raw_snapshot_hash(raw_files: Sequence[Mapping[str, Any]]) -> str:
    identity = [
        {"path": item.get("path"), "size": item.get("size"), "sha256": item.get("sha256")}
        for item in raw_files
    ]
    return _sha256_bytes(_json_bytes(identity))


def convert_humanevalplus(
    *,
    input_path: str | Path,
    revision: str,
    source_manifest_path: str | Path,
    output_dir: str | Path,
) -> ConversionResult:
    """Convert the pinned 164-row HF export into a baseline-only public projection."""

    resolved_input = Path(input_path).expanduser().resolve()
    resolved_source_manifest = Path(source_manifest_path).expanduser().resolve()
    resolved_output = Path(output_dir).expanduser().resolve()
    source_manifest, source_manifest_hash, raw_jsonl_hash = _verify_source_manifest(
        input_path=resolved_input,
        manifest_path=resolved_source_manifest,
        revision=revision,
    )
    raw_rows = _read_raw_rows(resolved_input)

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
    validate_humanevalplus_public_problems(problems, require_complete_snapshot=True)
    _validate_problem_bytes(problems_bytes, ordered_ids)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "kind": "tracejudge_humanevalplus_public_projection",
        "experiment_label": FULL_EXPERIMENT_LABEL,
        "metrics_scope": "generation_and_parsing_only",
        "dataset_id": DATASET_ID,
        "source": DATASET_SOURCE,
        "revision": revision,
        "split": source_manifest.get("split"),
        "license": source_manifest.get("license"),
        "adapter": {"name": ADAPTER_NAME, "version": ADAPTER_VERSION},
        "source_manifest_sha256": source_manifest_hash,
        "raw_snapshot": {
            "aggregate_sha256": _aggregate_raw_snapshot_hash(raw_files),
            "test_jsonl_sha256": raw_jsonl_hash,
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
        "split",
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
    if payload.get("schema_version") != 1:
        raise DatasetError("sample input manifest schema_version must be 1")
    if payload.get("kind") != "tracejudge_humanevalplus_public_projection":
        raise DatasetError("sample input manifest is not a HumanEval+ public projection")
    if payload.get("dataset_id") != DATASET_ID:
        raise DatasetError("sample input manifest has an unexpected dataset_id")
    if payload.get("source") != DATASET_SOURCE:
        raise DatasetError("sample input manifest has an unexpected source")
    if payload.get("experiment_label") != FULL_EXPERIMENT_LABEL:
        raise DatasetError("sample input manifest has an unexpected experiment label")
    if payload.get("metrics_scope") != "generation_and_parsing_only":
        raise DatasetError("sample input manifest has an unexpected metrics scope")
    if payload.get("split") != "test" or payload.get("license") != "apache-2.0":
        raise DatasetError("sample input manifest split/license identity is invalid")
    revision = payload.get("revision")
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise DatasetError("sample input manifest revision is invalid")
    if payload.get("adapter") != {"name": ADAPTER_NAME, "version": ADAPTER_VERSION}:
        raise DatasetError("sample input manifest adapter identity is invalid")
    for field in ("source_manifest_sha256",):
        value = payload.get(field)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise DatasetError(f"sample input manifest {field} is invalid")

    expected_ids = [f"HumanEval/{index}" for index in range(EXPECTED_RECORD_COUNT)]
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
    if set(raw_snapshot) != {"aggregate_sha256", "test_jsonl_sha256", "record_count"}:
        raise DatasetError("sample input manifest raw snapshot fields are invalid")
    if (
        projection.get("path") != "problems.jsonl"
        or projection.get("record_count") != EXPECTED_RECORD_COUNT
        or projection.get("ordered_problem_ids_sha256") != ordered_problem_ids_sha256(expected_ids)
    ):
        raise DatasetError("sample input manifest public projection identity is invalid")
    if (
        selection.get("algorithm") != FULL_SELECTION_ALGORITHM
        or selection.get("count") != EXPECTED_RECORD_COUNT
        or selection.get("selected_problem_ids") != expected_ids
    ):
        raise DatasetError("sample input manifest full selection identity is invalid")
    if raw_snapshot.get("record_count") != EXPECTED_RECORD_COUNT:
        raise DatasetError("sample input manifest raw record count is invalid")
    for field in ("aggregate_sha256", "test_jsonl_sha256"):
        value = raw_snapshot.get(field)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise DatasetError(f"sample input manifest raw snapshot {field} is invalid")
    withheld_fields = payload.get("withheld_fields")
    if (
        not isinstance(withheld_fields, list)
        or not all(isinstance(field, str) and field for field in withheld_fields)
        or not {"canonical_solution", "test"} <= set(withheld_fields)
        or not set(withheld_fields) <= set(KNOWN_WITHHELD_FIELDS)
    ):
        raise DatasetError("sample input manifest withheld field identity is invalid")
    return payload


def _selection_rank(*, seed: int, problem_id: str) -> str:
    return hashlib.sha256(f"{seed}\0{problem_id}".encode()).hexdigest()


def select_humanevalplus_problem_ids(*, count: int, seed: int) -> tuple[str, ...]:
    """Return the reproducible public-ID-only selection for the pinned universe."""

    if count <= 0:
        raise DatasetError("sample count must be greater than zero")
    if count > EXPECTED_RECORD_COUNT:
        raise DatasetError("sample count exceeds the available problem count")
    all_ids = [f"HumanEval/{index}" for index in range(EXPECTED_RECORD_COUNT)]
    ranked = sorted(
        all_ids,
        key=lambda problem_id: (
            _selection_rank(seed=seed, problem_id=problem_id),
            problem_id,
        ),
    )
    return tuple(sorted(ranked[:count], key=_safe_task_id_number))


def sample_humanevalplus(
    *,
    dataset_path: str | Path,
    source_manifest_path: str | Path,
    count: int,
    seed: int,
    output_dir: str | Path,
) -> SampleResult:
    """Select task IDs deterministically using only public identifiers."""

    selected_id_list = list(select_humanevalplus_problem_ids(count=count, seed=seed))
    resolved_dataset = Path(dataset_path).expanduser().resolve()
    resolved_manifest = Path(source_manifest_path).expanduser().resolve()
    resolved_output = Path(output_dir).expanduser().resolve()
    parent_manifest = _load_bundle_manifest(resolved_manifest)
    parent_raw_snapshot = parent_manifest.get("raw_snapshot")
    assert isinstance(parent_raw_snapshot, Mapping)
    dataset_hash = _sha256_file(resolved_dataset)
    projection = parent_manifest.get("public_projection")
    if not isinstance(projection, Mapping) or projection.get("sha256") != dataset_hash:
        raise DatasetError("full public projection does not match its dataset manifest")

    problems = load_problems(resolved_dataset)
    validate_humanevalplus_public_problems(problems, require_complete_snapshot=True)
    problems_by_id = {problem.problem_id: problem for problem in problems}
    selected = [problems_by_id[problem_id] for problem_id in selected_id_list]
    selected_bytes = _jsonl_bytes([problem.model_dump(mode="json") for problem in selected])
    _validate_problem_bytes(selected_bytes, selected_id_list)
    label = (
        PILOT_EXPERIMENT_LABEL
        if count == 10
        else f"humanevalplus_{count}_public_prompt_generation_pilot"
    )
    manifest = {
        "schema_version": 1,
        "kind": "tracejudge_dataset_selection",
        "experiment_label": label,
        "metrics_scope": "generation_and_parsing_only",
        "dataset_id": DATASET_ID,
        "source": DATASET_SOURCE,
        "revision": parent_manifest.get("revision"),
        "split": parent_manifest.get("split"),
        "license": parent_manifest.get("license"),
        "adapter": parent_manifest.get("adapter"),
        "source_manifest_sha256": parent_manifest.get("source_manifest_sha256"),
        "parent_manifest_sha256": _sha256_file(resolved_manifest),
        "raw_snapshot": {
            "aggregate_sha256": parent_raw_snapshot.get("aggregate_sha256"),
            "test_jsonl_sha256": parent_raw_snapshot.get("test_jsonl_sha256"),
            "record_count": parent_raw_snapshot.get("record_count"),
        },
        "public_projection": {
            "path": "problems.jsonl",
            "sha256": _sha256_bytes(selected_bytes),
            "record_count": len(selected),
            "ordered_problem_ids_sha256": ordered_problem_ids_sha256(selected_id_list),
        },
        "selection": {
            "algorithm": SELECTION_ALGORITHM,
            "seed": seed,
            "count": count,
            "selected_problem_ids": selected_id_list,
        },
        "withheld_fields": parent_manifest.get("withheld_fields"),
        "limitations": list(PILOT_LIMITATIONS),
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


def validate_problem_dataset(path: str | Path) -> ValidationResult:
    resolved = Path(path).expanduser().resolve()
    problems = load_problems(resolved)
    return ValidationResult(
        dataset_path=resolved,
        problem_count=len(problems),
        dataset_sha256=_sha256_file(resolved),
        sources=tuple(sorted({problem.source for problem in problems})),
        difficulties=tuple(sorted({problem.difficulty for problem in problems})),
    )
