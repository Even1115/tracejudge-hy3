"""Gate-B natural-cohort freezing over validated phase-one/two artifacts.

The freezer is deliberately read-only with respect to upstream runs.  It never
opens EvalPlus ``samples.jsonl`` or ``evalplus_raw_results.json`` and never
copies phase-one provider ``raw_output`` into its public manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from tracejudge_hy3.dataset.humanevalplus import RESEARCH_NATURAL_EXPERIMENT_LABEL
from tracejudge_hy3.dataset.loader import load_problems
from tracejudge_hy3.evalplus.exporter import (
    EvalPlusExportError,
    load_validated_phase1_export,
)
from tracejudge_hy3.evalplus.schemas import ValidatedSampleExport
from tracejudge_hy3.prompts.solver import solver_public_payload
from tracejudge_hy3.schemas.solution import SolutionTrace

from .contracts import (
    FrozenCohortManifest,
    MethodId,
    NaturalTrace,
    Phase1BundleIdentity,
    Phase1ResponseReference,
    Phase2BundleIdentity,
    Phase2FunctionalEvidenceRef,
    ResearchDatasetIdentity,
    SelectionRule,
    SourceAccounting,
    SourceOutcome,
)
from .privacy import (
    assert_public_payload_safe,
    canonical_sha256,
    jsonl_record_sha256,
)

_MAX_INPUT_BYTES = 128 * 1024 * 1024
_FREEZE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
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


class Phase3FreezeError(ValueError):
    """Raised when a source bundle cannot be frozen without ambiguity or leakage."""

    def __init__(self, message: str, *, safe_stage: str = "P3B_VALIDATION") -> None:
        super().__init__(message)
        self.safe_stage = safe_stage


class _DuplicateJsonKey(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _Phase1NaturalSeed:
    problem_id: str
    public_problem: dict[str, Any]
    solution_trace: SolutionTrace
    public_problem_sha256: str
    solution_trace_sha256: str
    structured_explanation_sha256: str
    code_sha256: str
    phase1_response: Phase1ResponseReference


@dataclass(frozen=True, slots=True)
class _ValidatedPhase1FreezeInput:
    exported: ValidatedSampleExport
    dataset: ResearchDatasetIdentity
    bundle: Phase1BundleIdentity
    source_outcomes: tuple[SourceOutcome, ...]
    natural_seeds: tuple[_Phase1NaturalSeed, ...]


@dataclass(frozen=True, slots=True)
class _ValidatedPhase2FreezeInput:
    bundle: Phase2BundleIdentity
    evidence_by_problem: Mapping[str, Phase2FunctionalEvidenceRef]


@dataclass(frozen=True, slots=True)
class Phase3FreezeResult:
    freeze_id: str
    run_dir: Path
    manifest_path: Path
    manifest_sha256: str
    source_problem_count: int
    natural_trace_count: int
    parse_error_count: int
    provider_error_count: int


@dataclass(frozen=True, slots=True)
class Phase3PreflightResult:
    freeze_id: str
    source_problem_count: int
    natural_trace_count: int
    parse_error_count: int
    provider_error_count: int
    phase1_run_id: str
    phase2_run_id: str


@dataclass(frozen=True, slots=True)
class _PreparedNaturalCohort:
    manifest: FrozenCohortManifest
    payload: bytes
    output_dir: Path


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _decode_json(payload: bytes, *, label: str) -> Any:
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKey, ValueError):
        raise Phase3FreezeError(f"{label} is not strict UTF-8 JSON") from None


def _read_regular_file(path: Path, *, label: str) -> bytes:
    if path.is_symlink():
        raise Phase3FreezeError(f"{label} must not be a symbolic link")
    if not path.is_file():
        raise Phase3FreezeError(f"required {label} is missing")
    try:
        if path.stat().st_size > _MAX_INPUT_BYTES:
            raise Phase3FreezeError(f"{label} exceeds the static input size limit")
        with path.open("rb") as stream:
            payload = stream.read(_MAX_INPUT_BYTES + 1)
    except OSError:
        raise Phase3FreezeError(f"cannot read required {label}") from None
    if len(payload) > _MAX_INPUT_BYTES:
        raise Phase3FreezeError(f"{label} exceeds the static input size limit")
    return payload


def _json_object(payload: bytes, *, label: str) -> dict[str, Any]:
    value = _decode_json(payload, label=label)
    if not isinstance(value, dict):
        raise Phase3FreezeError(f"{label} must contain one JSON object")
    return value


def _jsonl_records(
    payload: bytes,
    *,
    label: str,
) -> list[tuple[int, bytes, dict[str, Any]]]:
    if not payload or not payload.endswith(b"\n"):
        raise Phase3FreezeError(f"{label} must be non-empty and end with LF")
    records: list[tuple[int, bytes, dict[str, Any]]] = []
    for line_number, line in enumerate(payload.splitlines(keepends=True), start=1):
        if line == b"\n":
            raise Phase3FreezeError(f"{label} contains a blank record")
        value = _decode_json(line, label=f"{label} line {line_number}")
        if not isinstance(value, dict):
            raise Phase3FreezeError(f"{label} line {line_number} is not an object")
        records.append((line_number, line, value))
    return records


def _json_value(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True))


def _validate_research_export(exported: ValidatedSampleExport) -> None:
    selection = exported.export_selection
    if exported.phase1.experiment_label != RESEARCH_NATURAL_EXPERIMENT_LABEL:
        raise Phase3FreezeError("phase one is not the frozen research-natural experiment")
    if exported.dataset.selection_role != "research_natural":
        raise Phase3FreezeError("phase-one dataset is not research_natural")
    if selection.selection_policy != "phase1-success-only":
        raise Phase3FreezeError("phase one must use phase1-success-only accounting")
    if selection.min_success_count < 30:
        raise Phase3FreezeError("phase-one minimum success threshold is below 30")
    if selection.source_problem_count != 45:
        raise Phase3FreezeError("research-natural source cohort must contain exactly 45 tasks")
    if not 30 <= selection.exported_success_count <= 45:
        raise Phase3FreezeError("research-natural successes must be between 30 and 45")
    if (
        selection.exported_success_count
        + selection.excluded_parse_error_count
        + selection.excluded_provider_error_count
        != selection.source_problem_count
    ):
        raise Phase3FreezeError("phase-one outcomes do not cover all 45 source tasks")


def _load_phase1_freeze_input(
    phase1_run_dir: Path,
    dataset_manifest_path: Path,
    *,
    privacy_canaries: Sequence[str | bytes],
) -> _ValidatedPhase1FreezeInput:
    try:
        exported = load_validated_phase1_export(
            phase1_run_dir,
            dataset_manifest_path,
            selection_policy="phase1-success-only",
            min_success_count=30,
        )
    except EvalPlusExportError as exc:
        raise Phase3FreezeError(f"phase-one validation failed: {exc}") from None
    _validate_research_export(exported)

    for filename, expected_hash in (
        ("manifest.json", exported.phase1.manifest_sha256),
        ("summary.json", exported.phase1.summary_sha256),
    ):
        source_bytes = _read_regular_file(
            phase1_run_dir / filename,
            label=f"phase-one {filename}",
        )
        if _sha256(source_bytes) != expected_hash:
            raise Phase3FreezeError(f"phase-one {filename} changed after validation")
    responses_path = phase1_run_dir / "responses.jsonl"
    responses_bytes = _read_regular_file(
        responses_path,
        label="phase-one responses.jsonl",
    )
    if _sha256(responses_bytes) != exported.phase1.responses_sha256:
        raise Phase3FreezeError("phase-one responses hash changed after validation")
    records = _jsonl_records(responses_bytes, label="phase-one responses.jsonl")

    dataset_manifest_bytes = _read_regular_file(
        dataset_manifest_path,
        label="research-natural dataset manifest",
    )
    if _sha256(dataset_manifest_bytes) != exported.dataset.manifest_sha256:
        raise Phase3FreezeError("dataset manifest changed after phase-one validation")
    problems_path = dataset_manifest_path.parent / "problems.jsonl"
    problems_bytes = _read_regular_file(
        problems_path,
        label="research-natural public problems.jsonl",
    )
    if _sha256(problems_bytes) != exported.dataset.problems_sha256:
        raise Phase3FreezeError("public problem projection changed after validation")
    try:
        problems = load_problems(problems_path)
    except Exception as exc:
        raise Phase3FreezeError(
            f"phase-one public projection validation failed ({type(exc).__name__})"
        ) from None
    problems_by_id = {problem.problem_id: problem for problem in problems}
    selected_problem_ids = tuple(exported.dataset.selected_problem_ids)
    if tuple(problem.problem_id for problem in problems) != selected_problem_ids:
        raise Phase3FreezeError("public problem order differs from the dataset manifest")
    if _sha256(
        _read_regular_file(
            problems_path,
            label="research-natural public problems.jsonl",
        )
    ) != _sha256(problems_bytes):
        raise Phase3FreezeError("public problem projection changed while freezing")

    references_by_id = {
        reference.problem_id: reference for reference in exported.response_references
    }
    samples_by_id = {sample.task_id: sample for sample in exported.samples}
    successful_ids = tuple(sample.task_id for sample in exported.samples)
    natural_seeds: list[_Phase1NaturalSeed] = []
    for problem_id in successful_ids:
        reference = references_by_id[problem_id]
        if not 1 <= reference.response_line_number <= len(records):
            raise Phase3FreezeError("phase-one response reference line is outside the log")
        line_number, raw_line, record = records[reference.response_line_number - 1]
        if (
            line_number != reference.response_line_number
            or jsonl_record_sha256(raw_line) != reference.response_record_sha256
            or record.get("run_id") != exported.phase1.run_id
            or record.get("invocation_id") != reference.invocation_id
            or record.get("problem_id") != problem_id
            or record.get("status") != "success"
        ):
            raise Phase3FreezeError("phase-one response reference no longer matches its exact row")
        raw_solution = record.get("solution_trace")
        if not isinstance(raw_solution, Mapping) or set(raw_solution) != _SOLUTION_TRACE_KEYS:
            raise Phase3FreezeError("phase-one solution trace fields changed")
        try:
            solution = SolutionTrace.model_validate(raw_solution)
        except ValidationError:
            raise Phase3FreezeError("phase-one solution trace no longer validates") from None
        if solution.problem_id != problem_id:
            raise Phase3FreezeError("phase-one solution trace problem_id changed")
        code_sha256 = _sha256(solution.code.encode("utf-8"))
        if code_sha256 != reference.code_sha256 or code_sha256 != _sha256(
            samples_by_id[problem_id].solution.encode("utf-8")
        ):
            raise Phase3FreezeError("phase-one solution code hash changed")

        public_payload = solver_public_payload(problems_by_id[problem_id])
        assert_public_payload_safe(public_payload, canaries=privacy_canaries)
        solution_payload = solution.model_dump(mode="json")
        explanation_payload = {
            key: value for key, value in solution_payload.items() if key != "code"
        }
        natural_seeds.append(
            _Phase1NaturalSeed(
                problem_id=problem_id,
                public_problem=public_payload,
                solution_trace=solution,
                public_problem_sha256=canonical_sha256(public_payload),
                solution_trace_sha256=canonical_sha256(solution_payload),
                structured_explanation_sha256=canonical_sha256(explanation_payload),
                code_sha256=code_sha256,
                phase1_response=Phase1ResponseReference(
                    phase1_run_id=reference.phase1_run_id,
                    problem_id=reference.problem_id,
                    invocation_id=reference.invocation_id,
                    response_line_number=reference.response_line_number,
                    response_record_sha256=reference.response_record_sha256,
                    code_sha256=reference.code_sha256,
                ),
            )
        )

    final_status_by_id: dict[str, str] = {}
    for _line_number, _raw_line, record in records:
        problem_id = record.get("problem_id")
        status = record.get("status")
        if problem_id in problems_by_id and status in {
            "success",
            "parse_error",
            "provider_error",
        }:
            final_status_by_id[str(problem_id)] = str(status)
    if set(final_status_by_id) != set(selected_problem_ids):
        raise Phase3FreezeError("phase-one final statuses do not cover the source cohort")
    expected_success_ids = tuple(
        problem_id
        for problem_id in selected_problem_ids
        if final_status_by_id[problem_id] == "success"
    )
    if expected_success_ids != successful_ids:
        raise Phase3FreezeError("phase-one success order differs from validated export")

    source_outcomes = tuple(
        SourceOutcome(
            problem_id=problem_id,
            final_status=final_status_by_id[problem_id],
            included_trace_id=(
                f"natural:{problem_id}" if final_status_by_id[problem_id] == "success" else None
            ),
        )
        for problem_id in selected_problem_ids
    )
    dataset = ResearchDatasetIdentity(
        manifest_sha256=exported.dataset.manifest_sha256,
        dataset_id=exported.dataset.dataset_id,
        source=exported.dataset.source,
        revision=exported.dataset.revision,
        license=exported.dataset.license,
        problems_sha256=exported.dataset.problems_sha256,
        ordered_problem_ids_sha256=exported.dataset.ordered_problem_ids_sha256,
        selection_algorithm=exported.dataset.selection_algorithm,
        selection_seed=exported.dataset.selection_seed,
        source_problem_count=exported.export_selection.source_problem_count,
    )
    return _ValidatedPhase1FreezeInput(
        exported=exported,
        dataset=dataset,
        bundle=Phase1BundleIdentity(
            run_id=exported.phase1.run_id,
            manifest_sha256=exported.phase1.manifest_sha256,
            summary_sha256=exported.phase1.summary_sha256,
            responses_sha256=exported.phase1.responses_sha256,
        ),
        source_outcomes=source_outcomes,
        natural_seeds=tuple(natural_seeds),
    )


def _expected_phase1_source(exported: ValidatedSampleExport) -> dict[str, Any]:
    return _json_value(asdict(exported.phase1))


def _expected_dataset(exported: ValidatedSampleExport) -> dict[str, Any]:
    return _json_value(asdict(exported.dataset))


def _expected_selection(exported: ValidatedSampleExport) -> dict[str, Any]:
    return _json_value(asdict(exported.export_selection))


def _load_phase2_freeze_input(
    phase2_run_dir: Path,
    phase1: _ValidatedPhase1FreezeInput,
) -> _ValidatedPhase2FreezeInput:
    if phase2_run_dir.is_symlink() or not phase2_run_dir.is_dir():
        raise Phase3FreezeError(
            "phase-two run must be an existing non-symlink directory",
            safe_stage="P3B_PHASE2_FILES",
        )
    manifest_bytes = _read_regular_file(
        phase2_run_dir / "manifest.json", label="phase-two manifest"
    )
    summary_bytes = _read_regular_file(phase2_run_dir / "summary.json", label="phase-two summary")
    results_bytes = _read_regular_file(
        phase2_run_dir / "results.jsonl", label="phase-two safe results.jsonl"
    )
    execution_log_bytes = _read_regular_file(
        phase2_run_dir / "execution.log", label="phase-two safe execution.log"
    )
    manifest = _json_object(manifest_bytes, label="phase-two manifest")
    summary = _json_object(summary_bytes, label="phase-two summary")

    run_id = manifest.get("run_id")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("phase") != "phase2_evalplus_execution"
        or manifest.get("status") != "completed"
        or manifest.get("execution_mode") != "docker"
        or not isinstance(run_id, str)
        or run_id != phase2_run_dir.name
    ):
        raise Phase3FreezeError(
            "phase-two manifest is not a completed official execution",
            safe_stage="P3B_PHASE2_MANIFEST",
        )
    if manifest.get("phase1_source") != _expected_phase1_source(phase1.exported):
        raise Phase3FreezeError(
            "phase-two manifest references a different phase-one bundle",
            safe_stage="P3B_PHASE2_UPSTREAM_PHASE1",
        )
    if manifest.get("dataset") != _expected_dataset(phase1.exported):
        raise Phase3FreezeError(
            "phase-two dataset identity differs from phase one",
            safe_stage="P3B_PHASE2_UPSTREAM_DATASET",
        )

    expected_ids = [sample.task_id for sample in phase1.exported.samples]
    expected_codes = {
        reference.problem_id: reference.code_sha256
        for reference in phase1.exported.response_references
    }
    input_identity = manifest.get("input")
    if not isinstance(input_identity, Mapping):
        raise Phase3FreezeError(
            "phase-two input identity is missing", safe_stage="P3B_PHASE2_INPUT"
        )
    if (
        input_identity.get("record_count") != len(expected_ids)
        or input_identity.get("ordered_problem_ids") != expected_ids
        or input_identity.get("code_sha256") != expected_codes
        or input_identity.get("samples_sha256") != phase1.exported.samples_sha256
        or input_identity.get("phase1_export_selection") != _expected_selection(phase1.exported)
        or input_identity.get("public_task_identity")
        != [_json_value(asdict(item)) for item in phase1.exported.task_metadata]
    ):
        raise Phase3FreezeError(
            "phase-two input identity differs from phase-one export",
            safe_stage="P3B_PHASE2_INPUT",
        )

    output = manifest.get("output")
    if not isinstance(output, Mapping):
        raise Phase3FreezeError(
            "phase-two output identity is missing", safe_stage="P3B_PHASE2_OUTPUT"
        )
    safe_hashes = {
        "summary_sha256": _sha256(summary_bytes),
        "results_sha256": _sha256(results_bytes),
        "execution_log_sha256": _sha256(execution_log_bytes),
    }
    if any(output.get(key) != value for key, value in safe_hashes.items()):
        raise Phase3FreezeError(
            "phase-two safe artifact hash differs from its manifest",
            safe_stage="P3B_PHASE2_HASHES",
        )
    if (
        output.get("result_count") != len(expected_ids)
        or output.get("samples_sha256") != phase1.exported.samples_sha256
        or input_identity.get("samples_sha256") != output.get("samples_sha256")
    ):
        raise Phase3FreezeError(
            "phase-two result count differs from the frozen candidate set",
            safe_stage="P3B_PHASE2_OUTPUT",
        )

    selection = phase1.exported.export_selection
    if (
        summary.get("schema_version") != 1
        or summary.get("run_id") != run_id
        or summary.get("experiment_label") != manifest.get("experiment_label")
        or summary.get("execution_mode") != "docker"
        or summary.get("selection_policy") != selection.selection_policy
        or summary.get("min_success_count") != selection.min_success_count
        or summary.get("source_problem_count") != selection.source_problem_count
        or summary.get("exported_success_count") != selection.exported_success_count
        or summary.get("excluded_parse_error_count") != selection.excluded_parse_error_count
        or summary.get("excluded_provider_error_count") != selection.excluded_provider_error_count
        or summary.get("result_count") != len(expected_ids)
        or summary.get("actual_execution_count") != len(expected_ids)
        or summary.get("infrastructure_error_count") != 0
        or summary.get("mock_not_executed_count") != 0
        or summary.get("container_cleanup_failed_count") != 0
        or summary.get("evaluation_complete") is not True
    ):
        raise Phase3FreezeError(
            "phase-two summary is not a complete real execution",
            safe_stage="P3B_PHASE2_SUMMARY",
        )

    references_by_id = {
        reference.problem_id: reference for reference in phase1.exported.response_references
    }
    result_records = _jsonl_records(results_bytes, label="phase-two safe results.jsonl")
    if len(result_records) != len(expected_ids):
        raise Phase3FreezeError(
            "phase-two safe result rows are incomplete", safe_stage="P3B_PHASE2_RESULTS"
        )
    evidence_by_problem: dict[str, Phase2FunctionalEvidenceRef] = {}
    for expected_problem_id, (line_number, raw_line, record) in zip(
        expected_ids, result_records, strict=True
    ):
        if set(record) != _RESULT_FIELDS:
            raise Phase3FreezeError(
                "phase-two safe result fields changed", safe_stage="P3B_PHASE2_RESULTS"
            )
        if (
            record.get("schema_version") != 1
            or record.get("run_id") != run_id
            or record.get("problem_id") != expected_problem_id
            or record.get("infrastructure_status") != "ok"
            or record.get("solution_sha256") != expected_codes[expected_problem_id]
            or record.get("source_response")
            != _json_value(asdict(references_by_id[expected_problem_id]))
        ):
            raise Phase3FreezeError(
                "phase-two safe result provenance changed",
                safe_stage="P3B_PHASE2_RESULTS",
            )
        result_hash = jsonl_record_sha256(raw_line)
        try:
            evidence = Phase2FunctionalEvidenceRef(
                phase2_run_id=run_id,
                problem_id=expected_problem_id,
                result_line_number=line_number,
                result_record_sha256=result_hash,
                functional_evidence_sha256=result_hash,
                code_sha256=expected_codes[expected_problem_id],
                base_status=record.get("base_status"),
                plus_status=record.get("plus_status"),
                passed_base=record.get("passed_base"),
                passed_plus=record.get("passed_plus"),
            )
        except ValidationError:
            raise Phase3FreezeError(
                "phase-two safe functional status is inconsistent",
                safe_stage="P3B_PHASE2_RESULTS",
            ) from None
        evidence_by_problem[expected_problem_id] = evidence

    base_pass_count = sum(item.passed_base for item in evidence_by_problem.values())
    plus_pass_count = sum(item.passed_plus for item in evidence_by_problem.values())
    timeout_count = sum(
        item.base_status == "timeout" or item.plus_status == "timeout"
        for item in evidence_by_problem.values()
    )
    if (
        summary.get("base_pass_count") != base_pass_count
        or summary.get("base_plus_pass_count") != plus_pass_count
        or summary.get("timeout_count") != timeout_count
    ):
        raise Phase3FreezeError(
            "phase-two summary functional counts differ from safe rows",
            safe_stage="P3B_PHASE2_COUNTS",
        )

    return _ValidatedPhase2FreezeInput(
        bundle=Phase2BundleIdentity(
            run_id=run_id,
            manifest_sha256=_sha256(manifest_bytes),
            summary_sha256=safe_hashes["summary_sha256"],
            results_sha256=safe_hashes["results_sha256"],
            execution_log_sha256=safe_hashes["execution_log_sha256"],
        ),
        evidence_by_problem=evidence_by_problem,
    )


def _build_manifest(
    *,
    freeze_id: str,
    created_at: datetime,
    phase1: _ValidatedPhase1FreezeInput,
    phase2: _ValidatedPhase2FreezeInput,
) -> FrozenCohortManifest:
    status_counts = Counter(item.final_status for item in phase1.source_outcomes)
    traces = tuple(
        NaturalTrace(
            trace_id=f"natural:{seed.problem_id}",
            problem_id=seed.problem_id,
            public_problem_sha256=seed.public_problem_sha256,
            solution_trace_sha256=seed.solution_trace_sha256,
            structured_explanation_sha256=seed.structured_explanation_sha256,
            code_sha256=seed.code_sha256,
            functional_evidence=phase2.evidence_by_problem[seed.problem_id],
            phase1_response=seed.phase1_response,
        )
        for seed in phase1.natural_seeds
    )
    natural_count = len(traces)
    return FrozenCohortManifest(
        freeze_id=freeze_id,
        experiment_label=f"phase3_research_natural_{natural_count}_v1",
        created_at=created_at,
        dataset=phase1.dataset,
        phase1=phase1.bundle,
        phase2=phase2.bundle,
        selection_rule=SelectionRule(
            rule_id="all_phase1_successes_v1",
            policy="all_phase1_successes",
            description=(
                "Include every complete success from the frozen 45-task phase-one "
                "research-natural source cohort, without using phase-two status."
            ),
            minimum_natural_count=30,
            target_natural_count=natural_count,
            maximum_natural_count=45,
            backup_problem_ids=(),
            stop_rule=(
                "Stop after all 45 frozen source outcomes are accounted for; do not "
                "replace parse/provider failures or filter by functional evidence."
            ),
        ),
        source_accounting=SourceAccounting(
            source_problem_count=len(phase1.source_outcomes),
            success_count=status_counts["success"],
            parse_error_count=status_counts["parse_error"],
            provider_error_count=status_counts["provider_error"],
            included_natural_trace_count=natural_count,
        ),
        source_outcomes=phase1.source_outcomes,
        traces=traces,
        ordered_trace_ids=tuple(trace.trace_id for trace in traces),
        paired_method_ids=tuple(MethodId),
        privacy_policy_version="phase3_public_allowlist_v1",
    )


def _manifest_bytes(manifest: FrozenCohortManifest) -> bytes:
    return (
        json.dumps(
            manifest.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


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


def _publish_manifest(output_dir: Path, freeze_id: str, payload: bytes) -> Path:
    if not _FREEZE_ID_PATTERN.fullmatch(freeze_id):
        raise Phase3FreezeError(
            "freeze_id must contain only letters, digits, '.', '_' or '-' and be at most "
            "128 characters"
        )
    if output_dir.is_symlink():
        raise Phase3FreezeError("phase-three output directory must not be a symbolic link")
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise Phase3FreezeError("cannot create phase-three output directory") from None
    run_dir = output_dir / freeze_id
    if run_dir.exists() or run_dir.is_symlink():
        raise Phase3FreezeError("phase-three freeze directory already exists")

    temporary_dir: Path | None = None
    try:
        temporary_dir = Path(tempfile.mkdtemp(prefix=f".{freeze_id}.", dir=output_dir))
        os.chmod(temporary_dir, 0o700)
        manifest_path = temporary_dir / "manifest.json"
        with manifest_path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(manifest_path, 0o600)
        os.replace(temporary_dir, run_dir)
        temporary_dir = None
        _fsync_directory(output_dir)
    except OSError:
        raise Phase3FreezeError("cannot atomically publish phase-three manifest") from None
    finally:
        if temporary_dir is not None:
            shutil.rmtree(temporary_dir, ignore_errors=True)
    return run_dir / "manifest.json"


def _resolve_freeze_paths(
    *,
    phase1_run_dir: str | Path,
    phase2_run_dir: str | Path,
    dataset_manifest_path: str | Path,
    output_dir: str | Path,
    freeze_id: str,
) -> tuple[Path, Path, Path, Path]:
    if not _FREEZE_ID_PATTERN.fullmatch(freeze_id):
        raise Phase3FreezeError(
            "freeze_id must contain only letters, digits, '.', '_' or '-' and be at most "
            "128 characters",
            safe_stage="P3B_INPUT",
        )
    raw_phase1_path = Path(phase1_run_dir).expanduser()
    if raw_phase1_path.is_symlink():
        raise Phase3FreezeError("phase-one run must not be a symbolic link", safe_stage="P3B_INPUT")
    phase1_path = raw_phase1_path.resolve()
    raw_phase2_path = Path(phase2_run_dir).expanduser()
    if raw_phase2_path.is_symlink():
        raise Phase3FreezeError("phase-two run must not be a symbolic link", safe_stage="P3B_INPUT")
    phase2_path = raw_phase2_path.resolve()
    raw_dataset_manifest = Path(dataset_manifest_path).expanduser()
    if raw_dataset_manifest.is_symlink():
        raise Phase3FreezeError(
            "dataset manifest must not be a symbolic link", safe_stage="P3B_INPUT"
        )
    dataset_path = raw_dataset_manifest.resolve()
    raw_output_path = Path(output_dir).expanduser()
    if raw_output_path.is_symlink():
        raise Phase3FreezeError(
            "phase-three output directory must not be a symbolic link",
            safe_stage="P3B_OUTPUT_TARGET",
        )
    output_path = raw_output_path.resolve()
    run_dir = output_path / freeze_id
    if run_dir.exists() or run_dir.is_symlink():
        raise Phase3FreezeError(
            "phase-three freeze directory already exists",
            safe_stage="P3B_OUTPUT_TARGET",
        )
    return phase1_path, phase2_path, dataset_path, output_path


def _prepare_natural_cohort(
    *,
    phase1_run_dir: str | Path,
    phase2_run_dir: str | Path,
    dataset_manifest_path: str | Path,
    output_dir: str | Path,
    freeze_id: str,
    privacy_canaries: Sequence[str | bytes] = (),
    created_at: datetime | None = None,
) -> _PreparedNaturalCohort:
    phase1_path, phase2_path, dataset_path, output_path = _resolve_freeze_paths(
        phase1_run_dir=phase1_run_dir,
        phase2_run_dir=phase2_run_dir,
        dataset_manifest_path=dataset_manifest_path,
        output_dir=output_dir,
        freeze_id=freeze_id,
    )
    try:
        phase1 = _load_phase1_freeze_input(
            phase1_path,
            dataset_path,
            privacy_canaries=privacy_canaries,
        )
    except (Phase3FreezeError, OSError, ValueError) as exc:
        raise Phase3FreezeError(
            "phase-one source validation failed", safe_stage="P3B_PHASE1"
        ) from exc
    try:
        phase2 = _load_phase2_freeze_input(phase2_path, phase1)
    except Phase3FreezeError as exc:
        if exc.safe_stage != "P3B_VALIDATION":
            raise
        raise Phase3FreezeError(
            "phase-two safe evidence validation failed", safe_stage="P3B_PHASE2"
        ) from exc
    except (OSError, ValueError) as exc:
        raise Phase3FreezeError(
            "phase-two safe evidence validation failed", safe_stage="P3B_PHASE2"
        ) from exc
    timestamp = created_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        raise Phase3FreezeError(
            "freeze timestamp must be timezone-aware", safe_stage="P3B_MANIFEST"
        )
    try:
        manifest = _build_manifest(
            freeze_id=freeze_id,
            created_at=timestamp,
            phase1=phase1,
            phase2=phase2,
        )
    except (KeyError, ValidationError, Phase3FreezeError, ValueError) as exc:
        raise Phase3FreezeError(
            "phase-three manifest contract validation failed", safe_stage="P3B_MANIFEST"
        ) from exc
    public_payload = manifest.model_dump(mode="json")
    try:
        assert_public_payload_safe(public_payload, canaries=privacy_canaries)
    except ValueError as exc:
        raise Phase3FreezeError(
            "phase-three public payload privacy validation failed", safe_stage="P3B_PRIVACY"
        ) from exc
    try:
        payload = _manifest_bytes(manifest)
    except (TypeError, ValueError) as exc:
        raise Phase3FreezeError(
            "phase-three manifest serialization failed", safe_stage="P3B_MANIFEST"
        ) from exc
    return _PreparedNaturalCohort(manifest=manifest, payload=payload, output_dir=output_path)


def preflight_natural_cohort(
    *,
    phase1_run_dir: str | Path,
    phase2_run_dir: str | Path,
    dataset_manifest_path: str | Path,
    output_dir: str | Path,
    freeze_id: str,
    privacy_canaries: Sequence[str | bytes] = (),
    created_at: datetime | None = None,
) -> Phase3PreflightResult:
    """Run every Gate-B validation without creating a directory or artifact."""

    prepared = _prepare_natural_cohort(
        phase1_run_dir=phase1_run_dir,
        phase2_run_dir=phase2_run_dir,
        dataset_manifest_path=dataset_manifest_path,
        output_dir=output_dir,
        freeze_id=freeze_id,
        privacy_canaries=privacy_canaries,
        created_at=created_at,
    )
    manifest = prepared.manifest
    accounting = manifest.source_accounting
    return Phase3PreflightResult(
        freeze_id=freeze_id,
        source_problem_count=accounting.source_problem_count,
        natural_trace_count=accounting.included_natural_trace_count,
        parse_error_count=accounting.parse_error_count,
        provider_error_count=accounting.provider_error_count,
        phase1_run_id=manifest.phase1.run_id,
        phase2_run_id=manifest.phase2.run_id,
    )


def freeze_natural_cohort(
    *,
    phase1_run_dir: str | Path,
    phase2_run_dir: str | Path,
    dataset_manifest_path: str | Path,
    output_dir: str | Path,
    freeze_id: str,
    privacy_canaries: Sequence[str | bytes] = (),
    created_at: datetime | None = None,
) -> Phase3FreezeResult:
    """Validate and atomically freeze all research-natural phase-one successes."""

    prepared = _prepare_natural_cohort(
        phase1_run_dir=phase1_run_dir,
        phase2_run_dir=phase2_run_dir,
        dataset_manifest_path=dataset_manifest_path,
        output_dir=output_dir,
        freeze_id=freeze_id,
        privacy_canaries=privacy_canaries,
        created_at=created_at,
    )
    try:
        manifest_path = _publish_manifest(prepared.output_dir, freeze_id, prepared.payload)
    except (Phase3FreezeError, OSError) as exc:
        raise Phase3FreezeError(
            "cannot atomically publish phase-three manifest", safe_stage="P3B_PUBLISH"
        ) from exc
    accounting = prepared.manifest.source_accounting
    return Phase3FreezeResult(
        freeze_id=freeze_id,
        run_dir=manifest_path.parent,
        manifest_path=manifest_path,
        manifest_sha256=_sha256(prepared.payload),
        source_problem_count=accounting.source_problem_count,
        natural_trace_count=accounting.included_natural_trace_count,
        parse_error_count=accounting.parse_error_count,
        provider_error_count=accounting.provider_error_count,
    )
