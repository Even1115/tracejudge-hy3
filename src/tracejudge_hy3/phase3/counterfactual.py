"""Gate-B public counterfactual source, evidence, and immutable overlay freezing.

Only the repository-owned source bundle with the exact allowlisted SHA256 may
be executed by the trusted-local backend.  The bundle contains public,
self-constructed fixtures; this module never opens HumanEval+ evaluation-only
files and never calls a Provider or Docker.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from tracejudge_hy3.sandbox.trusted_local import TrustedLocalSandbox
from tracejudge_hy3.schemas.problem import TestCase

from .cohort import Phase3FreezeError, _publish_manifest
from .contracts import (
    CounterfactualCohortManifest,
    CounterfactualKind,
    CounterfactualMutation,
    CounterfactualParentSnapshot,
    CounterfactualSelectionRule,
    CounterfactualTrace,
    EvidenceStrategy,
    FrozenCohortManifest,
    MethodId,
    NaturalCohortReference,
    NaturalTrace,
    PublicCounterfactualSourceBundle,
    PublicCounterfactualSourceIdentity,
    PublicFixtureDefinition,
    PublicFixtureExecutionBundleIdentity,
    PublicFixtureExecutionCaseResult,
    PublicFixtureExecutionManifest,
    PublicFixtureExecutionResult,
    PublicFixtureExecutionSubject,
    PublicFixtureFunctionalEvidenceRef,
)
from .privacy import assert_public_payload_safe, canonical_sha256, jsonl_record_sha256

PUBLIC_COUNTERFACTUAL_SOURCE_SHA256 = (
    "a6195fb0867c69607bfa7a346b8112c49dfbe4d9d85700e2238d5bb1e22731df"
)
PUBLIC_COUNTERFACTUAL_SOURCE_RELATIVE_PATH = "phase3/public_counterfactuals_v1.json"

_MAX_SOURCE_BYTES = 8 * 1024 * 1024
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FORBIDDEN_CALLS = frozenset(
    {
        "__import__",
        "breakpoint",
        "compile",
        "eval",
        "exec",
        "input",
        "open",
    }
)


class _DuplicateJsonKey(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _ExecutionCandidate:
    subject: PublicFixtureExecutionSubject
    fixture: PublicFixtureDefinition
    code: str


@dataclass(frozen=True, slots=True)
class _PreparedSource:
    bundle: PublicCounterfactualSourceBundle
    source_sha256: str
    candidates: tuple[_ExecutionCandidate, ...]


@dataclass(frozen=True, slots=True)
class CounterfactualSourcePreflightResult:
    bundle_id: str
    source_bundle_sha256: str
    parent_count: int
    counterfactual_count: int
    execution_subject_count: int
    expected_pass_count: int
    expected_fail_count: int
    execution_run_id: str


@dataclass(frozen=True, slots=True)
class PublicFixtureExecutionRunResult:
    run_id: str
    run_dir: Path
    manifest_path: Path
    results_path: Path
    manifest_sha256: str
    results_sha256: str
    result_count: int
    pass_count: int
    fail_count: int
    timeout_count: int
    infrastructure_error_count: int
    expectation_mismatch_count: int


@dataclass(frozen=True, slots=True)
class CounterfactualFreezePreflightResult:
    freeze_id: str
    natural_freeze_id: str
    natural_trace_count: int
    counterfactual_trace_count: int
    combined_trace_count: int
    parent_count: int
    evidence_run_id: str


@dataclass(frozen=True, slots=True)
class CounterfactualFreezeResult(CounterfactualFreezePreflightResult):
    run_dir: Path
    manifest_path: Path
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class _LoadedEvidence:
    identity: PublicFixtureExecutionBundleIdentity
    evidence_by_subject: Mapping[str, PublicFixtureFunctionalEvidenceRef]
    results_by_subject: Mapping[str, PublicFixtureExecutionResult]
    result_sha256_by_subject: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _PreparedCounterfactualFreeze:
    manifest: CounterfactualCohortManifest
    payload: bytes
    output_dir: Path


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
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKey, ValueError):
        raise Phase3FreezeError(
            f"{label} is not strict UTF-8 JSON", safe_stage="P3B_CF_INPUT"
        ) from None


def _read_regular_file(path: Path, *, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise Phase3FreezeError(
            f"{label} must be a regular non-symlink file", safe_stage="P3B_CF_INPUT"
        )
    try:
        if path.stat().st_size > _MAX_SOURCE_BYTES:
            raise Phase3FreezeError(f"{label} exceeds the size limit", safe_stage="P3B_CF_INPUT")
        with path.open("rb") as stream:
            payload = stream.read(_MAX_SOURCE_BYTES + 1)
    except OSError:
        raise Phase3FreezeError(f"cannot read {label}", safe_stage="P3B_CF_INPUT") from None
    if len(payload) > _MAX_SOURCE_BYTES:
        raise Phase3FreezeError(f"{label} exceeds the size limit", safe_stage="P3B_CF_INPUT")
    return payload


def _jsonl_rows(payload: bytes, *, label: str) -> list[tuple[int, bytes, dict[str, Any]]]:
    if not payload or not payload.endswith(b"\n"):
        raise Phase3FreezeError(
            f"{label} must be non-empty and end with LF", safe_stage="P3B_CF_EVIDENCE"
        )
    rows: list[tuple[int, bytes, dict[str, Any]]] = []
    for line_number, raw_line in enumerate(payload.splitlines(keepends=True), start=1):
        value = _decode_json(raw_line, label=f"{label} line {line_number}")
        if not isinstance(value, dict):
            raise Phase3FreezeError(
                f"{label} line {line_number} is not an object",
                safe_stage="P3B_CF_EVIDENCE",
            )
        rows.append((line_number, raw_line, value))
    return rows


def _function_name(signature: str) -> str:
    try:
        parsed = ast.parse(signature.rstrip().removesuffix(":") + ":\n    pass\n")
    except SyntaxError:
        raise Phase3FreezeError(
            "public fixture function signature is invalid", safe_stage="P3B_CF_SOURCE"
        ) from None
    if len(parsed.body) != 1 or not isinstance(parsed.body[0], ast.FunctionDef):
        raise Phase3FreezeError(
            "public fixture signature must contain one function",
            safe_stage="P3B_CF_SOURCE",
        )
    return parsed.body[0].name


def _validate_allowlisted_code(code: str, *, function_name: str) -> None:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        raise Phase3FreezeError(
            "public counterfactual code is not valid Python", safe_stage="P3B_CF_SOURCE"
        ) from None
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise Phase3FreezeError(
            "public counterfactual code must contain exactly one top-level function",
            safe_stage="P3B_CF_SOURCE",
        )
    if tree.body[0].name != function_name:
        raise Phase3FreezeError(
            "public counterfactual function differs from fixture signature",
            safe_stage="P3B_CF_SOURCE",
        )
    if any(isinstance(node, ast.Import | ast.ImportFrom) for node in ast.walk(tree)):
        raise Phase3FreezeError(
            "public counterfactual code may not import modules", safe_stage="P3B_CF_SOURCE"
        )
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _FORBIDDEN_CALLS:
                raise Phase3FreezeError(
                    "public counterfactual code contains a forbidden call",
                    safe_stage="P3B_CF_SOURCE",
                )
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise Phase3FreezeError(
                "public counterfactual code contains a dunder attribute",
                safe_stage="P3B_CF_SOURCE",
            )


def _public_problem_payload(fixture: PublicFixtureDefinition) -> dict[str, Any]:
    return {
        "problem_id": fixture.problem_id,
        "title": fixture.title,
        "requirement": fixture.requirement,
        "function_signature": fixture.function_signature,
        "requirements": [item.model_dump(mode="json") for item in fixture.requirements],
        "source": fixture.source,
        "license": fixture.license,
    }


def _solution_hashes(solution: Any) -> tuple[str, str, str]:
    payload = solution.model_dump(mode="json")
    explanation = {key: value for key, value in payload.items() if key != "code"}
    return (
        canonical_sha256(payload),
        canonical_sha256(explanation),
        _sha256(solution.code.encode("utf-8")),
    )


def _candidate(
    *,
    execution_subject_id: str,
    fixture: PublicFixtureDefinition,
    code: str,
    expected_execution_status: str,
) -> _ExecutionCandidate:
    fixture_sha256 = canonical_sha256(fixture)
    code_sha256 = _sha256(code.encode("utf-8"))
    replay_spec_sha256 = canonical_sha256(
        {
            "schema_version": 1,
            "execution_subject_id": execution_subject_id,
            "fixture": fixture.model_dump(mode="json"),
            "code": code,
        }
    )
    subject = PublicFixtureExecutionSubject(
        execution_subject_id=execution_subject_id,
        problem_id=fixture.problem_id,
        public_fixture_id=fixture.public_fixture_id,
        public_fixture_sha256=fixture_sha256,
        code_sha256=code_sha256,
        replay_spec_sha256=replay_spec_sha256,
        expected_execution_status=expected_execution_status,
    )
    return _ExecutionCandidate(subject=subject, fixture=fixture, code=code)


def _load_source_bundle(
    source_bundle_path: str | Path,
    *,
    expected_source_sha256: str = PUBLIC_COUNTERFACTUAL_SOURCE_SHA256,
    privacy_canaries: Sequence[str | bytes] = (),
) -> _PreparedSource:
    raw_path = Path(source_bundle_path).expanduser()
    if raw_path.is_symlink():
        raise Phase3FreezeError(
            "public source bundle must not be a symlink", safe_stage="P3B_CF_INPUT"
        )
    path = raw_path.resolve()
    payload = _read_regular_file(path, label="public counterfactual source bundle")
    source_sha256 = _sha256(payload)
    if source_sha256 != expected_source_sha256:
        raise Phase3FreezeError(
            "public counterfactual source is not the exact executable allowlist",
            safe_stage="P3B_CF_ALLOWLIST",
        )
    raw = _decode_json(payload, label="public counterfactual source bundle")
    try:
        bundle = PublicCounterfactualSourceBundle.model_validate(raw)
    except ValidationError:
        raise Phase3FreezeError(
            "public counterfactual source contract validation failed",
            safe_stage="P3B_CF_SOURCE",
        ) from None
    try:
        assert_public_payload_safe(bundle, canaries=privacy_canaries)
    except ValueError:
        raise Phase3FreezeError(
            "public counterfactual source failed privacy validation",
            safe_stage="P3B_CF_PRIVACY",
        ) from None

    parents_by_id = {item.parent_trace_id: item for item in bundle.parents}
    candidates: list[_ExecutionCandidate] = []
    for parent in bundle.parents:
        function_name = _function_name(parent.fixture.function_signature)
        _validate_allowlisted_code(parent.solution_trace.code, function_name=function_name)
        candidates.append(
            _candidate(
                execution_subject_id=parent.parent_trace_id,
                fixture=parent.fixture,
                code=parent.solution_trace.code,
                expected_execution_status="pass",
            )
        )
    for variant in bundle.counterfactuals:
        if variant.mutation_kind == CounterfactualKind.REASONING_SWAP:
            continue
        parent = parents_by_id[variant.parent_trace_id]
        function_name = _function_name(parent.fixture.function_signature)
        _validate_allowlisted_code(variant.solution_trace.code, function_name=function_name)
        candidates.append(
            _candidate(
                execution_subject_id=variant.trace_id,
                fixture=parent.fixture,
                code=variant.solution_trace.code,
                expected_execution_status=variant.expected_execution_status,
            )
        )
    return _PreparedSource(
        bundle=bundle,
        source_sha256=source_sha256,
        candidates=tuple(candidates),
    )


def _resolve_new_run_dir(output_dir: str | Path, run_id: str) -> tuple[Path, Path]:
    if not _ID_PATTERN.fullmatch(run_id):
        raise Phase3FreezeError(
            "run ID contains unsupported characters", safe_stage="P3B_CF_OUTPUT_TARGET"
        )
    raw_output = Path(output_dir).expanduser()
    if raw_output.is_symlink():
        raise Phase3FreezeError(
            "counterfactual output directory must not be a symlink",
            safe_stage="P3B_CF_OUTPUT_TARGET",
        )
    resolved_output = raw_output.resolve()
    if resolved_output.exists() and not resolved_output.is_dir():
        raise Phase3FreezeError(
            "counterfactual output parent must be a directory",
            safe_stage="P3B_CF_OUTPUT_TARGET",
        )
    run_dir = resolved_output / run_id
    if run_dir.exists() or run_dir.is_symlink():
        raise Phase3FreezeError(
            "counterfactual output run already exists",
            safe_stage="P3B_CF_OUTPUT_TARGET",
        )
    return resolved_output, run_dir


def preflight_public_counterfactual_source(
    *,
    source_bundle_path: str | Path,
    output_dir: str | Path,
    execution_run_id: str,
    expected_source_sha256: str = PUBLIC_COUNTERFACTUAL_SOURCE_SHA256,
    privacy_canaries: Sequence[str | bytes] = (),
) -> CounterfactualSourcePreflightResult:
    """Validate the exact public source and intended output without executing code."""

    prepared = _load_source_bundle(
        source_bundle_path,
        expected_source_sha256=expected_source_sha256,
        privacy_canaries=privacy_canaries,
    )
    _resolve_new_run_dir(output_dir, execution_run_id)
    expected = Counter(item.subject.expected_execution_status for item in prepared.candidates)
    return CounterfactualSourcePreflightResult(
        bundle_id=prepared.bundle.bundle_id,
        source_bundle_sha256=prepared.source_sha256,
        parent_count=len(prepared.bundle.parents),
        counterfactual_count=len(prepared.bundle.counterfactuals),
        execution_subject_count=len(prepared.candidates),
        expected_pass_count=expected["pass"],
        expected_fail_count=expected["fail"],
        execution_run_id=execution_run_id,
    )


def _to_test_cases(fixture: PublicFixtureDefinition) -> list[TestCase]:
    return [
        TestCase(
            case_id=item.case_id,
            args=list(item.args),
            kwargs=dict(item.kwargs),
            expected=item.expected,
            category=item.category,
            related_requirements=list(item.related_requirements),
        )
        for item in fixture.test_cases
    ]


def _execute_candidate(
    candidate: _ExecutionCandidate,
    *,
    sandbox: TrustedLocalSandbox,
    run_id: str,
) -> PublicFixtureExecutionResult:
    function_name = _function_name(candidate.fixture.function_signature)
    summary = sandbox.run(candidate.code, function_name, _to_test_cases(candidate.fixture))
    if summary.runtime_status != "completed":
        return PublicFixtureExecutionResult(
            run_id=run_id,
            **candidate.subject.model_dump(mode="python"),
            execution_status="infrastructure_error",
            expectation_met=False,
            case_count=0,
            pass_count=0,
            fail_count=0,
            timeout_count=0,
            case_results=(),
        )

    case_results = tuple(
        PublicFixtureExecutionCaseResult(
            case_id=item.case_id,
            category=item.category,
            passed=item.passed,
            actual_output=item.actual_output,
            expected_output=item.expected_output,
            exception_type=item.exception_type,
            timed_out=item.timed_out,
            related_requirements=tuple(item.related_requirements),
        )
        for item in summary.results
    )
    timeout_count = sum(item.timed_out for item in case_results)
    fail_count = sum(not item.passed for item in case_results)
    if timeout_count:
        execution_status = "timeout"
    elif fail_count:
        execution_status = "fail"
    else:
        execution_status = "pass"
    return PublicFixtureExecutionResult(
        run_id=run_id,
        **candidate.subject.model_dump(mode="python"),
        execution_status=execution_status,
        expectation_met=execution_status == candidate.subject.expected_execution_status,
        case_count=len(case_results),
        pass_count=len(case_results) - fail_count,
        fail_count=fail_count,
        timeout_count=timeout_count,
        case_results=case_results,
    )


def _json_bytes(value: Any) -> bytes:
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


def _jsonl_bytes(values: Sequence[Any]) -> bytes:
    return b"".join(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
        for value in values
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


def _publish_evidence_bundle(
    output_dir: Path,
    run_id: str,
    *,
    manifest_payload: bytes,
    results_payload: bytes,
) -> tuple[Path, Path]:
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise Phase3FreezeError(
            "cannot create public evidence output directory", safe_stage="P3B_CF_PUBLISH"
        ) from None
    run_dir = output_dir / run_id
    if run_dir.exists() or run_dir.is_symlink():
        raise Phase3FreezeError("public evidence run already exists", safe_stage="P3B_CF_PUBLISH")
    temporary_dir: Path | None = None
    try:
        temporary_dir = Path(tempfile.mkdtemp(prefix=f".{run_id}.", dir=output_dir))
        os.chmod(temporary_dir, 0o700)
        for filename, payload in (
            ("manifest.json", manifest_payload),
            ("results.jsonl", results_payload),
        ):
            path = temporary_dir / filename
            with path.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(path, 0o600)
        os.replace(temporary_dir, run_dir)
        temporary_dir = None
        _fsync_directory(output_dir)
    except OSError:
        raise Phase3FreezeError(
            "cannot atomically publish public evidence bundle", safe_stage="P3B_CF_PUBLISH"
        ) from None
    finally:
        if temporary_dir is not None:
            shutil.rmtree(temporary_dir, ignore_errors=True)
    return run_dir / "manifest.json", run_dir / "results.jsonl"


def execute_public_counterfactual_evidence(
    *,
    source_bundle_path: str | Path,
    output_dir: str | Path,
    execution_run_id: str,
    expected_source_sha256: str = PUBLIC_COUNTERFACTUAL_SOURCE_SHA256,
    privacy_canaries: Sequence[str | bytes] = (),
    per_test_timeout_seconds: float = 2.0,
    created_at: datetime | None = None,
) -> PublicFixtureExecutionRunResult:
    """Execute only the exact repository-owned public counterfactual allowlist."""

    if per_test_timeout_seconds <= 0 or per_test_timeout_seconds > 10:
        raise Phase3FreezeError(
            "public fixture timeout must be within (0, 10] seconds",
            safe_stage="P3B_CF_INPUT",
        )
    prepared = _load_source_bundle(
        source_bundle_path,
        expected_source_sha256=expected_source_sha256,
        privacy_canaries=privacy_canaries,
    )
    resolved_output, _run_dir = _resolve_new_run_dir(output_dir, execution_run_id)
    timestamp = created_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        raise Phase3FreezeError(
            "public evidence timestamp must be timezone-aware", safe_stage="P3B_CF_INPUT"
        )

    sandbox = TrustedLocalSandbox(
        per_test_timeout_seconds=per_test_timeout_seconds,
        allow_untrusted_code=False,
    )
    results = tuple(
        _execute_candidate(candidate, sandbox=sandbox, run_id=execution_run_id)
        for candidate in prepared.candidates
    )
    result_payloads = [item.model_dump(mode="json") for item in results]
    try:
        assert_public_payload_safe(result_payloads, canaries=privacy_canaries)
        results_payload = _jsonl_bytes(result_payloads)
    except (TypeError, ValueError):
        raise Phase3FreezeError(
            "public evidence serialization or privacy validation failed",
            safe_stage="P3B_CF_PRIVACY",
        ) from None
    mismatch_count = sum(not item.expectation_met for item in results)
    manifest = PublicFixtureExecutionManifest(
        status=("completed" if mismatch_count == 0 else "completed_with_expectation_mismatch"),
        run_id=execution_run_id,
        created_at=timestamp,
        execution_mode="trusted_local_exact_public_allowlist_v1",
        source_bundle_id=prepared.bundle.bundle_id,
        source_bundle_sha256=prepared.source_sha256,
        ordered_subjects=tuple(item.subject for item in prepared.candidates),
        result_count=len(results),
        expectation_mismatch_count=mismatch_count,
        results_sha256=_sha256(results_payload),
    )
    try:
        assert_public_payload_safe(manifest, canaries=privacy_canaries)
        manifest_payload = _json_bytes(manifest.model_dump(mode="json"))
    except (TypeError, ValueError):
        raise Phase3FreezeError(
            "public evidence manifest failed privacy validation",
            safe_stage="P3B_CF_PRIVACY",
        ) from None
    manifest_path, results_path = _publish_evidence_bundle(
        resolved_output,
        execution_run_id,
        manifest_payload=manifest_payload,
        results_payload=results_payload,
    )
    statuses = Counter(item.execution_status for item in results)
    return PublicFixtureExecutionRunResult(
        run_id=execution_run_id,
        run_dir=manifest_path.parent,
        manifest_path=manifest_path,
        results_path=results_path,
        manifest_sha256=_sha256(manifest_payload),
        results_sha256=_sha256(results_payload),
        result_count=len(results),
        pass_count=statuses["pass"],
        fail_count=statuses["fail"],
        timeout_count=statuses["timeout"],
        infrastructure_error_count=statuses["infrastructure_error"],
        expectation_mismatch_count=mismatch_count,
    )


def _load_natural_manifest(
    natural_manifest_path: str | Path,
    *,
    privacy_canaries: Sequence[str | bytes],
) -> tuple[FrozenCohortManifest, str]:
    raw_path = Path(natural_manifest_path).expanduser()
    if raw_path.is_symlink():
        raise Phase3FreezeError(
            "natural cohort manifest must not be a symlink",
            safe_stage="P3B_CF_NATURAL",
        )
    payload = _read_regular_file(
        raw_path.resolve(),
        label="natural cohort manifest",
    )
    try:
        manifest = FrozenCohortManifest.model_validate(
            _decode_json(payload, label="natural cohort")
        )
    except ValidationError:
        raise Phase3FreezeError(
            "natural cohort manifest contract validation failed",
            safe_stage="P3B_CF_NATURAL",
        ) from None
    if any(not isinstance(trace, NaturalTrace) for trace in manifest.traces):
        raise Phase3FreezeError(
            "referenced natural cohort must contain only natural traces",
            safe_stage="P3B_CF_NATURAL",
        )
    if len(manifest.traces) != manifest.source_accounting.included_natural_trace_count:
        raise Phase3FreezeError(
            "referenced natural trace count is inconsistent", safe_stage="P3B_CF_NATURAL"
        )
    try:
        assert_public_payload_safe(manifest, canaries=privacy_canaries)
    except ValueError:
        raise Phase3FreezeError(
            "natural cohort failed public privacy validation",
            safe_stage="P3B_CF_PRIVACY",
        ) from None
    return manifest, _sha256(payload)


def _load_execution_evidence(
    execution_run_dir: str | Path,
    *,
    prepared_source: _PreparedSource,
    privacy_canaries: Sequence[str | bytes],
) -> _LoadedEvidence:
    raw_run_dir = Path(execution_run_dir).expanduser()
    if raw_run_dir.is_symlink() or not raw_run_dir.is_dir():
        raise Phase3FreezeError(
            "public evidence run must be a non-symlink directory",
            safe_stage="P3B_CF_EVIDENCE",
        )
    run_dir = raw_run_dir.resolve()
    manifest_payload = _read_regular_file(run_dir / "manifest.json", label="evidence manifest")
    results_payload = _read_regular_file(run_dir / "results.jsonl", label="evidence results")
    try:
        manifest = PublicFixtureExecutionManifest.model_validate(
            _decode_json(manifest_payload, label="evidence manifest")
        )
    except ValidationError:
        raise Phase3FreezeError(
            "public evidence manifest contract validation failed",
            safe_stage="P3B_CF_EVIDENCE",
        ) from None
    if manifest.run_id != run_dir.name:
        raise Phase3FreezeError(
            "public evidence run ID differs from its directory",
            safe_stage="P3B_CF_EVIDENCE",
        )
    if (
        manifest.source_bundle_id != prepared_source.bundle.bundle_id
        or manifest.source_bundle_sha256 != prepared_source.source_sha256
        or manifest.results_sha256 != _sha256(results_payload)
        or manifest.ordered_subjects != tuple(item.subject for item in prepared_source.candidates)
    ):
        raise Phase3FreezeError(
            "public evidence identity differs from frozen source",
            safe_stage="P3B_CF_EVIDENCE",
        )
    rows = _jsonl_rows(results_payload, label="public evidence results")
    if len(rows) != len(prepared_source.candidates):
        raise Phase3FreezeError(
            "public evidence result rows are incomplete", safe_stage="P3B_CF_EVIDENCE"
        )
    evidence_by_subject: dict[str, PublicFixtureFunctionalEvidenceRef] = {}
    results_by_subject: dict[str, PublicFixtureExecutionResult] = {}
    result_sha256_by_subject: dict[str, str] = {}
    mutation_by_subject = {
        item.trace_id: item.mutation_kind
        for item in prepared_source.bundle.counterfactuals
        if item.mutation_kind != CounterfactualKind.REASONING_SWAP
    }
    mismatch_count = 0
    for candidate, (line_number, raw_line, raw_result) in zip(
        prepared_source.candidates, rows, strict=True
    ):
        try:
            result = PublicFixtureExecutionResult.model_validate(raw_result)
        except ValidationError:
            raise Phase3FreezeError(
                "public evidence result contract validation failed",
                safe_stage="P3B_CF_EVIDENCE",
            ) from None
        subject = candidate.subject
        expected_identity = subject.model_dump(mode="python")
        actual_identity = {key: getattr(result, key) for key in expected_identity}
        if result.run_id != manifest.run_id or actual_identity != expected_identity:
            raise Phase3FreezeError(
                "public evidence result is not bound to its execution subject",
                safe_stage="P3B_CF_EVIDENCE",
            )
        expected_cases = candidate.fixture.test_cases
        if len(result.case_results) != len(expected_cases):
            raise Phase3FreezeError(
                "public evidence cases differ from the frozen fixture",
                safe_stage="P3B_CF_EVIDENCE",
            )
        for actual, expected in zip(result.case_results, expected_cases, strict=True):
            if (
                actual.case_id != expected.case_id
                or actual.category != expected.category
                or actual.expected_output != expected.expected
                or actual.related_requirements != expected.related_requirements
            ):
                raise Phase3FreezeError(
                    "public evidence cases differ from the frozen fixture",
                    safe_stage="P3B_CF_EVIDENCE",
                )
            expected_exception = None
            if (
                isinstance(expected.expected, dict)
                and set(expected.expected) == {"raises"}
                and isinstance(expected.expected["raises"], str)
            ):
                expected_exception = expected.expected["raises"]
            if expected_exception is not None:
                recomputed_pass = (
                    not actual.timed_out and actual.exception_type == expected_exception
                )
            else:
                recomputed_pass = (
                    not actual.timed_out
                    and actual.exception_type is None
                    and actual.actual_output == expected.expected
                )
            if actual.passed != recomputed_pass:
                raise Phase3FreezeError(
                    "public evidence pass flag is inconsistent with the frozen fixture",
                    safe_stage="P3B_CF_EVIDENCE",
                )
        mutation_kind = mutation_by_subject.get(subject.execution_subject_id)
        if mutation_kind == CounterfactualKind.SHORTCUT:
            visible = [item for item in result.case_results if item.category == "visible"]
            challenge = [item for item in result.case_results if item.category == "challenge"]
            if (
                not visible
                or not all(item.passed for item in visible)
                or not any(not item.passed for item in challenge)
            ):
                raise Phase3FreezeError(
                    "shortcut evidence must pass visible and fail public challenge cases",
                    safe_stage="P3B_CF_EXPECTATION",
                )
        if mutation_kind == CounterfactualKind.BOUNDARY_DELETION and not any(
            not item.passed and item.category == "challenge" for item in result.case_results
        ):
            raise Phase3FreezeError(
                "boundary deletion must fail a public challenge case",
                safe_stage="P3B_CF_EXPECTATION",
            )
        mismatch_count += not result.expectation_met
        if result.execution_status not in {"pass", "fail"}:
            raise Phase3FreezeError(
                "public evidence contains timeout or infrastructure failure",
                safe_stage="P3B_CF_EVIDENCE",
            )
        result_hash = jsonl_record_sha256(raw_line)
        evidence_by_subject[subject.execution_subject_id] = PublicFixtureFunctionalEvidenceRef(
            phase3_execution_run_id=manifest.run_id,
            execution_subject_id=subject.execution_subject_id,
            problem_id=subject.problem_id,
            result_line_number=line_number,
            result_record_sha256=result_hash,
            functional_evidence_sha256=result_hash,
            code_sha256=subject.code_sha256,
            public_fixture_id=subject.public_fixture_id,
            public_fixture_sha256=subject.public_fixture_sha256,
            replay_spec_sha256=subject.replay_spec_sha256,
            execution_status=result.execution_status,
        )
        results_by_subject[subject.execution_subject_id] = result
        result_sha256_by_subject[subject.execution_subject_id] = result_hash
    if (
        mismatch_count
        or manifest.expectation_mismatch_count != mismatch_count
        or manifest.status != "completed"
    ):
        raise Phase3FreezeError(
            "public evidence does not match frozen expected impacts",
            safe_stage="P3B_CF_EXPECTATION",
        )
    try:
        assert_public_payload_safe(manifest, canaries=privacy_canaries)
        assert_public_payload_safe(
            [value for _number, _line, value in rows], canaries=privacy_canaries
        )
    except ValueError:
        raise Phase3FreezeError(
            "public evidence failed privacy validation", safe_stage="P3B_CF_PRIVACY"
        ) from None
    return _LoadedEvidence(
        identity=PublicFixtureExecutionBundleIdentity(
            run_id=manifest.run_id,
            manifest_sha256=_sha256(manifest_payload),
            results_sha256=_sha256(results_payload),
            source_bundle_sha256=manifest.source_bundle_sha256,
            result_count=manifest.result_count,
            execution_mode=manifest.execution_mode,
        ),
        evidence_by_subject=evidence_by_subject,
        results_by_subject=results_by_subject,
        result_sha256_by_subject=result_sha256_by_subject,
    )


def _build_counterfactual_manifest(
    *,
    freeze_id: str,
    created_at: datetime,
    natural: FrozenCohortManifest,
    natural_sha256: str,
    source: _PreparedSource,
    evidence: _LoadedEvidence,
) -> CounterfactualCohortManifest:
    parents: list[CounterfactualParentSnapshot] = []
    parents_by_id = {item.parent_trace_id: item for item in source.bundle.parents}
    for item in source.bundle.parents:
        solution_sha, explanation_sha, code_sha = _solution_hashes(item.solution_trace)
        parents.append(
            CounterfactualParentSnapshot(
                trace_id=item.parent_trace_id,
                problem_id=item.fixture.problem_id,
                public_problem_sha256=canonical_sha256(_public_problem_payload(item.fixture)),
                solution_trace_sha256=solution_sha,
                structured_explanation_sha256=explanation_sha,
                code_sha256=code_sha,
                functional_evidence=evidence.evidence_by_subject[item.parent_trace_id],
                public_fixture_id=item.fixture.public_fixture_id,
            )
        )
    parent_snapshots = {item.trace_id: item for item in parents}

    counterfactuals: list[CounterfactualTrace] = []
    for item in source.bundle.counterfactuals:
        parent_source = parents_by_id[item.parent_trace_id]
        parent = parent_snapshots[item.parent_trace_id]
        solution_sha, explanation_sha, code_sha = _solution_hashes(item.solution_trace)
        if item.mutation_kind == CounterfactualKind.REASONING_SWAP:
            evidence_ref = evidence.evidence_by_subject[item.parent_trace_id]
            strategy = EvidenceStrategy.REUSE_SAME_CODE
        else:
            evidence_ref = evidence.evidence_by_subject[item.trace_id]
            strategy = EvidenceStrategy.INDEPENDENT_PUBLIC_FIXTURE
        counterfactuals.append(
            CounterfactualTrace(
                trace_id=item.trace_id,
                problem_id=parent.problem_id,
                public_problem_sha256=parent.public_problem_sha256,
                solution_trace_sha256=solution_sha,
                structured_explanation_sha256=explanation_sha,
                code_sha256=code_sha,
                functional_evidence=evidence_ref,
                parent_trace_id=item.parent_trace_id,
                mutation=CounterfactualMutation(
                    mutation_kind=item.mutation_kind,
                    sole_change=item.sole_change,
                    expected_impact=item.expected_impact,
                    before_solution_trace_sha256=parent.solution_trace_sha256,
                    after_solution_trace_sha256=solution_sha,
                    before_structured_explanation_sha256=(parent.structured_explanation_sha256),
                    after_structured_explanation_sha256=explanation_sha,
                    before_code_sha256=parent.code_sha256,
                    after_code_sha256=code_sha,
                    evidence_strategy=strategy,
                ),
            )
        )
        if parent_source.fixture.public_fixture_id != evidence_ref.public_fixture_id:
            raise Phase3FreezeError(
                "counterfactual fixture binding changed", safe_stage="P3B_CF_MANIFEST"
            )

    counterfactual_ids = tuple(item.trace_id for item in counterfactuals)
    paired_ids = natural.ordered_trace_ids + counterfactual_ids
    counts = Counter(item.mutation_kind for item in source.bundle.counterfactuals)
    return CounterfactualCohortManifest(
        freeze_id=freeze_id,
        experiment_label=f"phase3_research_{len(natural.traces)}_natural_15_counterfactual_v1",
        created_at=created_at,
        natural_cohort=NaturalCohortReference(
            freeze_id=natural.freeze_id,
            manifest_sha256=natural_sha256,
            natural_trace_count=len(natural.traces),
            ordered_trace_ids=natural.ordered_trace_ids,
            ordered_trace_ids_sha256=canonical_sha256(natural.ordered_trace_ids),
        ),
        source=PublicCounterfactualSourceIdentity(
            bundle_id=source.bundle.bundle_id,
            source_bundle_sha256=source.source_sha256,
            source="self_constructed_phase3_public_fixture",
            license="MIT",
            parent_count=len(source.bundle.parents),
            counterfactual_count=len(source.bundle.counterfactuals),
        ),
        execution=evidence.identity,
        selection_rule=CounterfactualSelectionRule(
            rule_id="five_types_type_major_parent_order_v1",
            actual_per_type={kind: counts[kind] for kind in CounterfactualKind},
            stop_rule=(
                "Stop after the predeclared type-major variants are exhausted; do not "
                "replace or filter variants using method predictions or human labels."
            ),
        ),
        parents=tuple(parents),
        counterfactuals=tuple(counterfactuals),
        ordered_counterfactual_trace_ids=counterfactual_ids,
        paired_ordered_trace_ids=paired_ids,
        paired_ordered_trace_ids_sha256=canonical_sha256(paired_ids),
        paired_method_ids=tuple(MethodId),
        privacy_policy_version="phase3_public_allowlist_v1",
    )


def _prepare_counterfactual_freeze(
    *,
    natural_manifest_path: str | Path,
    source_bundle_path: str | Path,
    execution_run_dir: str | Path,
    output_dir: str | Path,
    freeze_id: str,
    expected_source_sha256: str,
    privacy_canaries: Sequence[str | bytes],
    created_at: datetime | None,
) -> _PreparedCounterfactualFreeze:
    if not _ID_PATTERN.fullmatch(freeze_id):
        raise Phase3FreezeError(
            "freeze ID contains unsupported characters", safe_stage="P3B_CF_OUTPUT_TARGET"
        )
    raw_output = Path(output_dir).expanduser()
    if raw_output.is_symlink():
        raise Phase3FreezeError(
            "counterfactual freeze output must not be a symlink",
            safe_stage="P3B_CF_OUTPUT_TARGET",
        )
    resolved_output = raw_output.resolve()
    if resolved_output.exists() and not resolved_output.is_dir():
        raise Phase3FreezeError(
            "counterfactual freeze parent must be a directory",
            safe_stage="P3B_CF_OUTPUT_TARGET",
        )
    if (resolved_output / freeze_id).exists() or (resolved_output / freeze_id).is_symlink():
        raise Phase3FreezeError(
            "counterfactual freeze already exists", safe_stage="P3B_CF_OUTPUT_TARGET"
        )
    natural, natural_sha = _load_natural_manifest(
        natural_manifest_path, privacy_canaries=privacy_canaries
    )
    source = _load_source_bundle(
        source_bundle_path,
        expected_source_sha256=expected_source_sha256,
        privacy_canaries=privacy_canaries,
    )
    evidence = _load_execution_evidence(
        execution_run_dir,
        prepared_source=source,
        privacy_canaries=privacy_canaries,
    )
    timestamp = created_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        raise Phase3FreezeError(
            "counterfactual freeze timestamp must be timezone-aware",
            safe_stage="P3B_CF_MANIFEST",
        )
    try:
        manifest = _build_counterfactual_manifest(
            freeze_id=freeze_id,
            created_at=timestamp,
            natural=natural,
            natural_sha256=natural_sha,
            source=source,
            evidence=evidence,
        )
        assert_public_payload_safe(manifest, canaries=privacy_canaries)
        if manifest.natural_cohort.ordered_trace_ids_sha256 != canonical_sha256(
            manifest.natural_cohort.ordered_trace_ids
        ):
            raise ValueError("natural order hash mismatch")
        if manifest.paired_ordered_trace_ids_sha256 != canonical_sha256(
            manifest.paired_ordered_trace_ids
        ):
            raise ValueError("paired order hash mismatch")
        payload = _json_bytes(manifest.model_dump(mode="json"))
    except (KeyError, TypeError, ValidationError, ValueError):
        raise Phase3FreezeError(
            "counterfactual overlay contract validation failed",
            safe_stage="P3B_CF_MANIFEST",
        ) from None
    return _PreparedCounterfactualFreeze(
        manifest=manifest,
        payload=payload,
        output_dir=resolved_output,
    )


def preflight_counterfactual_freeze(
    *,
    natural_manifest_path: str | Path,
    source_bundle_path: str | Path,
    execution_run_dir: str | Path,
    output_dir: str | Path,
    freeze_id: str,
    expected_source_sha256: str = PUBLIC_COUNTERFACTUAL_SOURCE_SHA256,
    privacy_canaries: Sequence[str | bytes] = (),
    created_at: datetime | None = None,
) -> CounterfactualFreezePreflightResult:
    """Validate the complete natural/source/evidence chain without writing."""

    prepared = _prepare_counterfactual_freeze(
        natural_manifest_path=natural_manifest_path,
        source_bundle_path=source_bundle_path,
        execution_run_dir=execution_run_dir,
        output_dir=output_dir,
        freeze_id=freeze_id,
        expected_source_sha256=expected_source_sha256,
        privacy_canaries=privacy_canaries,
        created_at=created_at,
    )
    manifest = prepared.manifest
    return CounterfactualFreezePreflightResult(
        freeze_id=freeze_id,
        natural_freeze_id=manifest.natural_cohort.freeze_id,
        natural_trace_count=manifest.natural_cohort.natural_trace_count,
        counterfactual_trace_count=len(manifest.counterfactuals),
        combined_trace_count=len(manifest.paired_ordered_trace_ids),
        parent_count=len(manifest.parents),
        evidence_run_id=manifest.execution.run_id,
    )


def freeze_counterfactual_cohort(
    *,
    natural_manifest_path: str | Path,
    source_bundle_path: str | Path,
    execution_run_dir: str | Path,
    output_dir: str | Path,
    freeze_id: str,
    expected_source_sha256: str = PUBLIC_COUNTERFACTUAL_SOURCE_SHA256,
    privacy_canaries: Sequence[str | bytes] = (),
    created_at: datetime | None = None,
) -> CounterfactualFreezeResult:
    """Atomically publish the public counterfactual overlay after full validation."""

    prepared = _prepare_counterfactual_freeze(
        natural_manifest_path=natural_manifest_path,
        source_bundle_path=source_bundle_path,
        execution_run_dir=execution_run_dir,
        output_dir=output_dir,
        freeze_id=freeze_id,
        expected_source_sha256=expected_source_sha256,
        privacy_canaries=privacy_canaries,
        created_at=created_at,
    )
    try:
        manifest_path = _publish_manifest(
            prepared.output_dir,
            freeze_id,
            prepared.payload,
        )
    except (OSError, Phase3FreezeError):
        raise Phase3FreezeError(
            "cannot atomically publish counterfactual overlay", safe_stage="P3B_CF_PUBLISH"
        ) from None
    manifest = prepared.manifest
    return CounterfactualFreezeResult(
        freeze_id=freeze_id,
        natural_freeze_id=manifest.natural_cohort.freeze_id,
        natural_trace_count=manifest.natural_cohort.natural_trace_count,
        counterfactual_trace_count=len(manifest.counterfactuals),
        combined_trace_count=len(manifest.paired_ordered_trace_ids),
        parent_count=len(manifest.parents),
        evidence_run_id=manifest.execution.run_id,
        run_dir=manifest_path.parent,
        manifest_path=manifest_path,
        manifest_sha256=_sha256(prepared.payload),
    )
