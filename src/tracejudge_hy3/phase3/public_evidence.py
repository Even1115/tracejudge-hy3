"""Gate-D public probes and public error-certificate fixture generation.

The formal certificate fixture consumes only the exact Gate-B public source,
its already validated execution bundle, and the frozen Gate-B/C cohort.  It
does not call a Provider, Docker, EvalPlus, or the network.  The independent
probe helper executes only code recovered from the exact public allowlist.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shlex
import shutil
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from tracejudge_hy3.counterexample.differential import run_differential
from tracejudge_hy3.counterexample.minimizer import minimize_counterexample_args
from tracejudge_hy3.sandbox.trusted_local import TrustedLocalSandbox

from .contracts import (
    CertificateArtifactReference,
    CounterfactualTrace,
    EvidenceReference,
    Phase3ErrorCertificate,
    Phase3PublicCertificateManifest,
    PublicCertificateClaim,
    PublicCertificateClaimsBundle,
    PublicCounterexample,
    PublicFixtureCase,
    PublicFixtureDefinition,
    PublicFixtureExecutionCaseResult,
    PublicFixtureExecutionResult,
)
from .counterfactual import (
    _decode_json,
    _fsync_directory,
    _function_name,
    _load_execution_evidence,
    _load_source_bundle,
    _public_problem_payload,
    _read_regular_file,
    _solution_hashes,
    _to_test_cases,
)
from .privacy import assert_public_payload_safe, canonical_sha256
from .runner import LoadedPairedCohort, Phase3RunnerError, load_paired_cohort

PUBLIC_CERTIFICATE_CLAIMS_RELATIVE_PATH = "phase3/public_certificate_claims_v1.json"
PUBLIC_CERTIFICATE_CLAIMS_SHA256 = (
    "3b1df5e5a1e43c1b91e626c8656495a03d332bd4a5231550eb88c8928b93bb5f"
)
PUBLIC_PROBE_POLICY = {
    "version": "phase3_public_probe_policy_v1",
    "priority": ["public_challenge_test", "deterministic_probe", "minimized"],
    "maximum_deterministic_probes": 32,
    "maximum_minimization_iterations": 16,
    "maximum_repeated_list_size": 32,
    "execution": "trusted_local_exact_public_allowlist_v1",
}
PUBLIC_PROBE_POLICY_SHA256 = canonical_sha256(PUBLIC_PROBE_POLICY)
CERTIFICATE_POLICY_SHA256 = canonical_sha256(
    {
        "version": "phase3_public_certificate_policy_v1",
        "levels": ["confirmed_bug", "strongly_supported", "unverified_suspicion"],
        "probe_policy_sha256": PUBLIC_PROBE_POLICY_SHA256,
        "confirmed_requires": "matching_public_execution_and_replay",
        "strong_requires": "recomputable_public_static_rule",
        "unverified_forbids": "public_reproducible_evidence",
    }
)

_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RETURN_LITERAL_PATTERN = re.compile(
    r"\breturns?\s+(-?(?:\d+(?:\.\d*)?|\.\d+))\b",
    flags=re.IGNORECASE,
)


class Phase3PublicEvidenceError(ValueError):
    def __init__(self, message: str, *, safe_stage: str = "P3D_PUBLIC_EVIDENCE") -> None:
        super().__init__(message)
        self.safe_stage = safe_stage


@dataclass(frozen=True, slots=True)
class PublicProbeInput:
    args: tuple[Any, ...]
    kwargs: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class PublicProbeSearchResult:
    trace_id: str
    status: Literal["found", "not_found", "timeout", "infrastructure_error"]
    attempted_public_challenges: int
    attempted_deterministic_probes: int
    counterexample: PublicCounterexample | None


@dataclass(frozen=True, slots=True)
class PublicCertificatePreflightResult:
    run_id: str
    certificate_count: int
    confirmed_bug_count: int
    strongly_supported_count: int
    unverified_suspicion_count: int
    claims_bundle_sha256: str
    certificate_policy_sha256: str
    certificate_payloads_sha256: str
    public_evidence_run_id: str


@dataclass(frozen=True, slots=True)
class PublicCertificateRunResult(PublicCertificatePreflightResult):
    run_dir: Path
    manifest_path: Path
    certificate_paths: tuple[Path, ...]
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class _ClaimContext:
    claim: PublicCertificateClaim
    trace: CounterfactualTrace
    fixture: PublicFixtureDefinition
    solution: Any
    execution_subject_id: str


@dataclass(frozen=True, slots=True)
class _PreparedCertificates:
    run_id: str
    output_dir: Path
    certificate_paths: tuple[Path, ...]
    certificates: tuple[Phase3ErrorCertificate, ...]
    certificate_payloads: tuple[bytes, ...]
    certificate_payloads_sha256: str
    claims: PublicCertificateClaimsBundle
    claims_sha256: str
    cohort: LoadedPairedCohort
    public_source_bundle_id: str
    public_source_sha256: str
    public_evidence_run_id: str
    public_evidence_manifest_sha256: str
    public_evidence_results_sha256: str


def _probe_key(args: Sequence[Any], kwargs: Mapping[str, Any]) -> str:
    return canonical_sha256({"args": list(args), "kwargs": dict(kwargs)})


def _list_variants(value: list[Any]) -> tuple[list[Any], ...]:
    variants: list[list[Any]] = [[]]
    if value:
        variants.extend(([value[0]], [value[0], value[0]], list(reversed(value))))
        try:
            variants.append(sorted(value))
        except TypeError:
            pass
        if all(isinstance(item, int | float) and not isinstance(item, bool) for item in value):
            variants.extend(
                (
                    [0 for _item in value],
                    [-abs(item) for item in value],
                    [value[0]] * min(32, max(2, len(value) * 2)),
                )
            )
    return tuple(variants)


def deterministic_probe_inputs(
    fixture: PublicFixtureDefinition,
    *,
    maximum_probes: int = 32,
) -> tuple[PublicProbeInput, ...]:
    """Derive a deterministic, bounded public probe order from public examples."""

    if maximum_probes < 1 or maximum_probes > 32:
        raise ValueError("maximum_probes must be within [1, 32]")
    frozen_keys = {_probe_key(item.args, item.kwargs) for item in fixture.test_cases}
    seen = set(frozen_keys)
    probes: list[PublicProbeInput] = []
    for case in fixture.test_cases:
        base = list(case.args)
        for index, value in enumerate(base):
            variants: Sequence[Any]
            if isinstance(value, bool):
                variants = ()
            elif isinstance(value, list):
                variants = _list_variants(value)
            elif isinstance(value, int | float):
                variants = (0, -1, 1, -value, value - 1, value + 1)
            elif isinstance(value, str):
                variants = ("", value[:1], value[::-1], value * 2)
            else:
                variants = ()
            for replacement in variants:
                args = list(base)
                args[index] = replacement
                key = _probe_key(args, case.kwargs)
                if key in seen:
                    continue
                seen.add(key)
                probes.append(PublicProbeInput(args=tuple(args), kwargs=dict(case.kwargs)))
                if len(probes) >= maximum_probes:
                    return tuple(probes)
    return tuple(probes)


def _expected_from_case(case: PublicFixtureCase) -> tuple[Any, str | None]:
    if (
        isinstance(case.expected, dict)
        and set(case.expected) == {"raises"}
        and isinstance(case.expected["raises"], str)
    ):
        return case.expected, case.expected["raises"]
    return case.expected, None


def public_execution_evidence_payload(
    *,
    trace_id: str,
    case_id: str,
    args: Sequence[Any],
    kwargs: Mapping[str, Any],
    expected: Any,
    candidate_output: Any,
    candidate_exception: str | None,
    timed_out: bool,
    code_sha256: str,
    public_source_sha256: str,
    replay_spec_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "trace_id": trace_id,
        "case_id": case_id,
        "args": list(args),
        "kwargs": dict(kwargs),
        "expected": expected,
        "candidate_output": candidate_output,
        "candidate_exception": candidate_exception,
        "timed_out": timed_out,
        "code_sha256": code_sha256,
        "public_source_sha256": public_source_sha256,
        "replay_spec_sha256": replay_spec_sha256,
    }


def _counterexample_from_case_result(
    *,
    trace_id: str,
    case: PublicFixtureCase,
    result: PublicFixtureExecutionCaseResult,
    code_sha256: str,
    public_source_sha256: str,
    replay_spec_sha256: str,
    source: Literal["public_challenge_test", "deterministic_probe", "minimized"],
) -> PublicCounterexample:
    payload = public_execution_evidence_payload(
        trace_id=trace_id,
        case_id=case.case_id,
        args=case.args,
        kwargs=case.kwargs,
        expected=case.expected,
        candidate_output=result.actual_output,
        candidate_exception=result.exception_type,
        timed_out=result.timed_out,
        code_sha256=code_sha256,
        public_source_sha256=public_source_sha256,
        replay_spec_sha256=replay_spec_sha256,
    )
    return PublicCounterexample(
        source=source,
        args=case.args,
        kwargs=dict(case.kwargs),
        expected=case.expected,
        candidate_output=result.actual_output,
        candidate_exception=result.exception_type,
        timed_out=result.timed_out,
        minimized=source == "minimized",
        public_source_sha256=public_source_sha256,
        replay_spec_sha256=replay_spec_sha256,
        execution_evidence_sha256=canonical_sha256(payload),
    )


def _source_context(prepared: Any, trace_id: str) -> tuple[Any, Any, PublicFixtureDefinition]:
    parents = {item.parent_trace_id: item for item in prepared.bundle.parents}
    for parent in prepared.bundle.parents:
        if parent.parent_trace_id == trace_id:
            return parent, parent.solution_trace, parent.fixture
    for variant in prepared.bundle.counterfactuals:
        if variant.trace_id == trace_id:
            parent = parents[variant.parent_trace_id]
            return parent, variant.solution_trace, parent.fixture
    raise Phase3PublicEvidenceError(
        "trace is absent from the exact public source", safe_stage="P3D_SOURCE"
    )


def search_public_counterexample(
    *,
    source_bundle_path: str | Path,
    trace_id: str,
    violated_requirement_id: str,
    per_test_timeout_seconds: float = 2.0,
    maximum_probes: int = 32,
) -> PublicProbeSearchResult:
    """Search only the exact public allowlist, with a fixed deterministic budget."""

    if per_test_timeout_seconds <= 0 or per_test_timeout_seconds > 10:
        raise Phase3PublicEvidenceError("public probe timeout must be within (0, 10]")
    prepared = _load_source_bundle(source_bundle_path)
    parent, solution, fixture = _source_context(prepared, trace_id)
    candidate = next(
        (
            item
            for item in prepared.candidates
            if item.subject.execution_subject_id
            == (parent.parent_trace_id if solution.code == parent.solution_trace.code else trace_id)
        ),
        None,
    )
    if candidate is None:
        raise Phase3PublicEvidenceError(
            "public execution subject is absent", safe_stage="P3D_SOURCE"
        )
    sandbox = TrustedLocalSandbox(
        per_test_timeout_seconds=per_test_timeout_seconds,
        allow_untrusted_code=False,
    )
    function_name = _function_name(fixture.function_signature)
    challenges = [
        item
        for item in fixture.test_cases
        if item.category == "challenge" and violated_requirement_id in item.related_requirements
    ]
    attempted_challenges = 0
    for case in challenges:
        attempted_challenges += 1
        summary = sandbox.run(
            solution.code,
            function_name,
            _to_test_cases(fixture)[
                list(fixture.test_cases).index(case) : list(fixture.test_cases).index(case) + 1
            ],
        )
        if summary.runtime_status != "completed" or not summary.results:
            return PublicProbeSearchResult(
                trace_id, "infrastructure_error", attempted_challenges, 0, None
            )
        observed = summary.results[0]
        if observed.timed_out:
            return PublicProbeSearchResult(trace_id, "timeout", attempted_challenges, 0, None)
        if not observed.passed:
            result = PublicFixtureExecutionCaseResult(
                case_id=case.case_id,
                category=case.category,
                passed=False,
                actual_output=observed.actual_output,
                expected_output=case.expected,
                exception_type=observed.exception_type,
                timed_out=False,
                related_requirements=case.related_requirements,
            )
            return PublicProbeSearchResult(
                trace_id,
                "found",
                attempted_challenges,
                0,
                _counterexample_from_case_result(
                    trace_id=trace_id,
                    case=case,
                    result=result,
                    code_sha256=candidate.subject.code_sha256,
                    public_source_sha256=prepared.source_sha256,
                    replay_spec_sha256=candidate.subject.replay_spec_sha256,
                    source="public_challenge_test",
                ),
            )

    probes = deterministic_probe_inputs(fixture, maximum_probes=maximum_probes)
    for probe_number, probe in enumerate(probes, start=1):
        diff = run_differential(
            sandbox,
            parent.solution_trace.code,
            solution.code,
            function_name,
            list(probe.args),
            dict(probe.kwargs),
        )
        if diff.candidate_exception == "TimeoutError":
            return PublicProbeSearchResult(
                trace_id, "timeout", attempted_challenges, probe_number, None
            )
        if not diff.differs:
            continue
        if diff.reference_exception and diff.reference_exception.startswith("SandboxSetupError"):
            return PublicProbeSearchResult(
                trace_id, "infrastructure_error", attempted_challenges, probe_number, None
            )
        args = list(probe.args)
        source: Literal["deterministic_probe", "minimized"] = "deterministic_probe"
        minimized_args, shrunk = minimize_counterexample_args(
            sandbox,
            parent.solution_trace.code,
            solution.code,
            function_name,
            args,
            dict(probe.kwargs),
            max_iterations=16,
        )
        if shrunk:
            minimized_diff = run_differential(
                sandbox,
                parent.solution_trace.code,
                solution.code,
                function_name,
                minimized_args,
                dict(probe.kwargs),
            )
            if minimized_diff.differs:
                args = minimized_args
                diff = minimized_diff
                source = "minimized"
        expected: Any = (
            {"raises": diff.reference_exception}
            if diff.reference_exception is not None
            else diff.reference_output
        )
        case = PublicFixtureCase(
            case_id=f"deterministic-probe-{_probe_key(args, probe.kwargs)[:12]}",
            args=tuple(args),
            kwargs=dict(probe.kwargs),
            expected=expected,
            category="challenge",
            related_requirements=(violated_requirement_id,),
        )
        result = PublicFixtureExecutionCaseResult(
            case_id=case.case_id,
            category="challenge",
            passed=False,
            actual_output=diff.candidate_output,
            expected_output=expected,
            exception_type=diff.candidate_exception,
            timed_out=False,
            related_requirements=(violated_requirement_id,),
        )
        return PublicProbeSearchResult(
            trace_id,
            "found",
            attempted_challenges,
            probe_number,
            _counterexample_from_case_result(
                trace_id=trace_id,
                case=case,
                result=result,
                code_sha256=candidate.subject.code_sha256,
                public_source_sha256=prepared.source_sha256,
                replay_spec_sha256=candidate.subject.replay_spec_sha256,
                source=source,
            ),
        )
    return PublicProbeSearchResult(trace_id, "not_found", attempted_challenges, len(probes), None)


def _load_claims(
    path: str | Path,
    *,
    expected_sha256: str,
    privacy_canaries: Sequence[str | bytes],
) -> tuple[PublicCertificateClaimsBundle, str]:
    payload = _read_regular_file(
        Path(path).expanduser().resolve(), label="public certificate claims"
    )
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256 != expected_sha256:
        raise Phase3PublicEvidenceError(
            "public certificate claims are not the exact engineering allowlist",
            safe_stage="P3D_CLAIMS",
        )
    try:
        claims = PublicCertificateClaimsBundle.model_validate(
            _decode_json(payload, label="public certificate claims")
        )
        assert_public_payload_safe(claims, canaries=privacy_canaries)
    except (ValidationError, ValueError):
        raise Phase3PublicEvidenceError(
            "public certificate claims failed contract or privacy validation",
            safe_stage="P3D_CLAIMS",
        ) from None
    return claims, actual_sha256


def _resolve_new_run(output_dir: str | Path, run_id: str) -> Path:
    if not _RUN_ID_PATTERN.fullmatch(run_id):
        raise Phase3PublicEvidenceError("certificate run ID is unsafe", safe_stage="P3D_OUTPUT")
    root = Path(output_dir).expanduser()
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise Phase3PublicEvidenceError(
            "certificate output root is unsafe", safe_stage="P3D_OUTPUT"
        )
    run_dir = root.resolve() / run_id
    if run_dir.exists() or run_dir.is_symlink():
        raise Phase3PublicEvidenceError(
            "certificate output run already exists", safe_stage="P3D_OUTPUT"
        )
    return root.resolve()


def _claim_contexts(
    *,
    claims: PublicCertificateClaimsBundle,
    cohort: LoadedPairedCohort,
    prepared_source: Any,
) -> tuple[_ClaimContext, ...]:
    parents = {item.parent_trace_id: item for item in prepared_source.bundle.parents}
    variants = {item.trace_id: item for item in prepared_source.bundle.counterfactuals}
    contexts: list[_ClaimContext] = []
    for claim in claims.claims:
        frozen = cohort.traces_by_id.get(claim.trace_id)
        variant = variants.get(claim.trace_id)
        if not isinstance(frozen, CounterfactualTrace) or variant is None:
            raise Phase3PublicEvidenceError(
                "certificate claim is absent from the frozen public counterfactual cohort",
                safe_stage="P3D_BINDING",
            )
        parent = parents[variant.parent_trace_id]
        fixture = parent.fixture
        solution_sha, explanation_sha, code_sha = _solution_hashes(variant.solution_trace)
        if (
            claim.problem_id != frozen.problem_id
            or fixture.problem_id != frozen.problem_id
            or solution_sha != frozen.solution_trace_sha256
            or explanation_sha != frozen.structured_explanation_sha256
            or code_sha != frozen.code_sha256
            or canonical_sha256(_public_problem_payload(fixture)) != frozen.public_problem_sha256
        ):
            raise Phase3PublicEvidenceError(
                "certificate claim material differs from the frozen trace",
                safe_stage="P3D_BINDING",
            )
        requirement_ids = {item.requirement_id for item in fixture.requirements}
        if claim.violated_requirement_id not in requirement_ids:
            raise Phase3PublicEvidenceError(
                "certificate claim references an unknown public requirement",
                safe_stage="P3D_BINDING",
            )
        evidence = frozen.functional_evidence
        if not hasattr(evidence, "execution_subject_id"):
            raise Phase3PublicEvidenceError(
                "public certificate trace lacks replayable public evidence",
                safe_stage="P3D_BINDING",
            )
        contexts.append(
            _ClaimContext(
                claim=claim,
                trace=frozen,
                fixture=fixture,
                solution=variant.solution_trace,
                execution_subject_id=evidence.execution_subject_id,
            )
        )
    return tuple(contexts)


def _numeric_return_literal(text: str) -> int | float | None:
    match = _RETURN_LITERAL_PATTERN.search(text)
    if match is None:
        return None
    value = float(match.group(1))
    return int(value) if value.is_integer() else value


def _empty_guard_return_literal(code: str, function_name: str) -> int | float | None:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    function = next(
        (
            item
            for item in tree.body
            if isinstance(item, ast.FunctionDef) and item.name == function_name
        ),
        None,
    )
    if function is None:
        return None
    parameters = {item.arg for item in function.args.args}
    for node in function.body:
        if not isinstance(node, ast.If):
            continue
        is_empty_guard = (
            isinstance(node.test, ast.UnaryOp)
            and isinstance(node.test.op, ast.Not)
            and isinstance(node.test.operand, ast.Name)
            and node.test.operand.id in parameters
        )
        if not is_empty_guard:
            continue
        for statement in node.body:
            if (
                isinstance(statement, ast.Return)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, int | float)
                and not isinstance(statement.value.value, bool)
            ):
                return statement.value.value
    return None


def _static_rule_payload(context: _ClaimContext) -> dict[str, Any]:
    claim = context.claim
    if claim.static_rule_id != "empty_guard_return_literal_mismatch_v1":
        raise Phase3PublicEvidenceError("unsupported public static rule", safe_stage="P3D_STATIC")
    step = next(
        (
            item
            for item in context.solution.implementation_steps
            if item.step_id == claim.first_faulty_step
        ),
        None,
    )
    requirement = next(
        item
        for item in context.fixture.requirements
        if item.requirement_id == claim.violated_requirement_id
    )
    if step is None:
        raise Phase3PublicEvidenceError("static rule step is absent", safe_stage="P3D_STATIC")
    explanation_literal = _numeric_return_literal(step.content)
    requirement_literal = _numeric_return_literal(requirement.content)
    code_literal = _empty_guard_return_literal(
        context.solution.code,
        _function_name(context.fixture.function_signature),
    )
    if (
        explanation_literal is None
        or requirement_literal is None
        or code_literal is None
        or explanation_literal == requirement_literal
        or code_literal != requirement_literal
    ):
        raise Phase3PublicEvidenceError(
            "public static rule did not reproduce the declared mismatch",
            safe_stage="P3D_STATIC",
        )
    return {
        "schema_version": 1,
        "rule_id": claim.static_rule_id,
        "trace_id": claim.trace_id,
        "requirement_id": claim.violated_requirement_id,
        "explanation_return_literal": explanation_literal,
        "public_requirement_return_literal": requirement_literal,
        "code_empty_guard_return_literal": code_literal,
        "code_sha256": context.trace.code_sha256,
        "structured_explanation_sha256": context.trace.structured_explanation_sha256,
    }


def _certificate_for_claim(
    *,
    context: _ClaimContext,
    evidence_results: Mapping[str, PublicFixtureExecutionResult],
    public_source_sha256: str,
    cohort_manifest_sha256: str,
    certificate_path_for_command: Path,
    cohort_path_for_command: str | Path,
    natural_path_for_command: str | Path,
    source_path_for_command: str | Path,
) -> Phase3ErrorCertificate:
    claim = context.claim
    counterexample: PublicCounterexample | None = None
    replay_command: str | None = None
    if claim.evidence_mode == "public_execution":
        execution = evidence_results.get(context.execution_subject_id)
        case = next(
            (item for item in context.fixture.test_cases if item.case_id == claim.public_case_id),
            None,
        )
        observed = (
            next(
                (item for item in execution.case_results if item.case_id == claim.public_case_id),
                None,
            )
            if execution is not None
            else None
        )
        if (
            case is None
            or observed is None
            or case.category != "challenge"
            or claim.violated_requirement_id not in case.related_requirements
            or observed.passed
            or observed.timed_out
        ):
            raise Phase3PublicEvidenceError(
                "confirmed claim lacks a matching failed public challenge",
                safe_stage="P3D_EXECUTION_EVIDENCE",
            )
        counterexample = _counterexample_from_case_result(
            trace_id=claim.trace_id,
            case=case,
            result=observed,
            code_sha256=context.trace.code_sha256,
            public_source_sha256=public_source_sha256,
            replay_spec_sha256=context.trace.functional_evidence.replay_spec_sha256,
            source="public_challenge_test",
        )
        supporting = (
            EvidenceReference(
                evidence_kind="public_execution",
                evidence_sha256=counterexample.execution_evidence_sha256,
                publicly_reproducible=True,
                summary="A frozen public challenge reproduces the candidate failure.",
            ),
        )
        replay_command = " ".join(
            (
                "tracejudge phase3 replay",
                f"--certificate {shlex.quote(str(certificate_path_for_command))}",
                f"--cohort-manifest {shlex.quote(str(cohort_path_for_command))}",
                f"--natural-manifest {shlex.quote(str(natural_path_for_command))}",
                f"--source-bundle {shlex.quote(str(source_path_for_command))}",
            )
        )
    elif claim.evidence_mode == "public_static_rule":
        static_payload = _static_rule_payload(context)
        supporting = (
            EvidenceReference(
                evidence_kind="ast_rule",
                evidence_sha256=canonical_sha256(static_payload),
                publicly_reproducible=True,
                summary="A frozen public AST/alignment rule reproduces the mismatch.",
            ),
        )
    else:
        supporting = (
            EvidenceReference(
                evidence_kind="judge_claim",
                evidence_sha256=canonical_sha256(
                    {
                        "claim_id": claim.claim_id,
                        "trace_id": claim.trace_id,
                        "claim_summary": claim.claim_summary,
                    }
                ),
                publicly_reproducible=False,
                summary="The engineering claim has no reproducible public evidence.",
            ),
        )
    certificate = Phase3ErrorCertificate(
        certificate_id=f"certificate:{claim.claim_id}",
        trace_id=claim.trace_id,
        problem_id=claim.problem_id,
        verdict=claim.expected_verdict,
        violated_requirement_id=claim.violated_requirement_id,
        violated_public_requirement=next(
            item.content
            for item in context.fixture.requirements
            if item.requirement_id == claim.violated_requirement_id
        ),
        first_faulty_layer=claim.first_faulty_layer,
        first_faulty_step=claim.first_faulty_step,
        code_span=claim.code_span,
        error_type=claim.error_type,
        supporting_evidence=supporting,
        counterexample=counterexample,
        replay_command=replay_command,
        frozen_manifest_sha256=cohort_manifest_sha256,
        code_sha256=context.trace.code_sha256,
        structured_explanation_sha256=context.trace.structured_explanation_sha256,
        functional_evidence_sha256=context.trace.functional_evidence.functional_evidence_sha256,
    )
    assert_public_payload_safe(certificate)
    return certificate


def _prepare_certificates(
    *,
    run_id: str,
    cohort_manifest_path: str | Path,
    natural_manifest_path: str | Path,
    source_bundle_path: str | Path,
    execution_run_dir: str | Path,
    claims_bundle_path: str | Path,
    output_dir: str | Path,
    expected_claims_sha256: str,
    privacy_canaries: Sequence[str | bytes],
) -> _PreparedCertificates:
    output_root = _resolve_new_run(output_dir, run_id)
    claims, claims_sha256 = _load_claims(
        claims_bundle_path,
        expected_sha256=expected_claims_sha256,
        privacy_canaries=privacy_canaries,
    )
    prepared_source = _load_source_bundle(
        source_bundle_path,
        privacy_canaries=privacy_canaries,
    )
    evidence = _load_execution_evidence(
        execution_run_dir,
        prepared_source=prepared_source,
        privacy_canaries=privacy_canaries,
    )
    try:
        cohort = load_paired_cohort(
            overlay_manifest_path=cohort_manifest_path,
            natural_manifest_path=natural_manifest_path,
        )
    except Phase3RunnerError as exc:
        raise Phase3PublicEvidenceError(
            "frozen cohort failed Gate-D binding",
            safe_stage="P3D_COHORT",
        ) from exc
    contexts = _claim_contexts(
        claims=claims,
        cohort=cohort,
        prepared_source=prepared_source,
    )
    run_dir_for_command = Path(output_dir) / run_id
    certificate_paths: list[Path] = []
    certificates: list[Phase3ErrorCertificate] = []
    payloads: list[bytes] = []
    for index, context in enumerate(contexts, start=1):
        relative = Path("certificates") / f"certificate_{index:03d}.json"
        certificate_path = output_root / run_id / relative
        certificate = _certificate_for_claim(
            context=context,
            evidence_results=evidence.results_by_subject,
            public_source_sha256=prepared_source.source_sha256,
            cohort_manifest_sha256=cohort.overlay_manifest_sha256,
            certificate_path_for_command=run_dir_for_command / relative,
            cohort_path_for_command=cohort_manifest_path,
            natural_path_for_command=natural_manifest_path,
            source_path_for_command=source_bundle_path,
        )
        assert_public_payload_safe(certificate, canaries=privacy_canaries)
        payload = (
            json.dumps(
                certificate.model_dump(mode="json"),
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        certificate_paths.append(certificate_path)
        certificates.append(certificate)
        payloads.append(payload)
    return _PreparedCertificates(
        run_id=run_id,
        output_dir=output_root,
        certificate_paths=tuple(certificate_paths),
        certificates=tuple(certificates),
        certificate_payloads=tuple(payloads),
        certificate_payloads_sha256=hashlib.sha256(b"".join(payloads)).hexdigest(),
        claims=claims,
        claims_sha256=claims_sha256,
        cohort=cohort,
        public_source_bundle_id=prepared_source.bundle.bundle_id,
        public_source_sha256=prepared_source.source_sha256,
        public_evidence_run_id=evidence.identity.run_id,
        public_evidence_manifest_sha256=evidence.identity.manifest_sha256,
        public_evidence_results_sha256=evidence.identity.results_sha256,
    )


def _preflight_result(prepared: _PreparedCertificates) -> PublicCertificatePreflightResult:
    counts = Counter(item.verdict for item in prepared.certificates)
    return PublicCertificatePreflightResult(
        run_id=prepared.run_id,
        certificate_count=len(prepared.certificates),
        confirmed_bug_count=counts["confirmed_bug"],
        strongly_supported_count=counts["strongly_supported"],
        unverified_suspicion_count=counts["unverified_suspicion"],
        claims_bundle_sha256=prepared.claims_sha256,
        certificate_policy_sha256=CERTIFICATE_POLICY_SHA256,
        certificate_payloads_sha256=prepared.certificate_payloads_sha256,
        public_evidence_run_id=prepared.public_evidence_run_id,
    )


def preflight_public_certificates(
    *,
    run_id: str,
    cohort_manifest_path: str | Path,
    natural_manifest_path: str | Path,
    source_bundle_path: str | Path,
    execution_run_dir: str | Path,
    claims_bundle_path: str | Path,
    output_dir: str | Path,
    expected_claims_sha256: str = PUBLIC_CERTIFICATE_CLAIMS_SHA256,
    privacy_canaries: Sequence[str | bytes] = (),
) -> PublicCertificatePreflightResult:
    prepared = _prepare_certificates(
        run_id=run_id,
        cohort_manifest_path=cohort_manifest_path,
        natural_manifest_path=natural_manifest_path,
        source_bundle_path=source_bundle_path,
        execution_run_dir=execution_run_dir,
        claims_bundle_path=claims_bundle_path,
        output_dir=output_dir,
        expected_claims_sha256=expected_claims_sha256,
        privacy_canaries=privacy_canaries,
    )
    return _preflight_result(prepared)


def _publish_certificate_bundle(
    *,
    prepared: _PreparedCertificates,
    manifest_payload: bytes,
) -> tuple[Path, tuple[Path, ...]]:
    root = prepared.output_dir
    try:
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
    except OSError:
        raise Phase3PublicEvidenceError(
            "cannot create certificate output root", safe_stage="P3D_PUBLISH"
        ) from None
    run_dir = root / prepared.run_id
    temporary: Path | None = None
    try:
        temporary = Path(tempfile.mkdtemp(prefix=f".{prepared.run_id}.", dir=root))
        temporary.chmod(0o700)
        certificates_dir = temporary / "certificates"
        certificates_dir.mkdir(mode=0o700)
        for path, payload in zip(
            prepared.certificate_paths,
            prepared.certificate_payloads,
            strict=True,
        ):
            target = certificates_dir / path.name
            with target.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            target.chmod(0o600)
        manifest_path = temporary / "manifest.json"
        with manifest_path.open("xb") as stream:
            stream.write(manifest_payload)
            stream.flush()
            os.fsync(stream.fileno())
        manifest_path.chmod(0o600)
        os.replace(temporary, run_dir)
        temporary = None
        _fsync_directory(root)
    except OSError:
        raise Phase3PublicEvidenceError(
            "cannot atomically publish certificate bundle", safe_stage="P3D_PUBLISH"
        ) from None
    finally:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
    paths = tuple(run_dir / "certificates" / item.name for item in prepared.certificate_paths)
    return run_dir / "manifest.json", paths


def generate_public_certificates(
    *,
    run_id: str,
    cohort_manifest_path: str | Path,
    natural_manifest_path: str | Path,
    source_bundle_path: str | Path,
    execution_run_dir: str | Path,
    claims_bundle_path: str | Path,
    output_dir: str | Path,
    expected_claims_sha256: str = PUBLIC_CERTIFICATE_CLAIMS_SHA256,
    privacy_canaries: Sequence[str | bytes] = (),
    created_at: datetime | None = None,
) -> PublicCertificateRunResult:
    prepared = _prepare_certificates(
        run_id=run_id,
        cohort_manifest_path=cohort_manifest_path,
        natural_manifest_path=natural_manifest_path,
        source_bundle_path=source_bundle_path,
        execution_run_dir=execution_run_dir,
        claims_bundle_path=claims_bundle_path,
        output_dir=output_dir,
        expected_claims_sha256=expected_claims_sha256,
        privacy_canaries=privacy_canaries,
    )
    timestamp = created_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        raise Phase3PublicEvidenceError(
            "certificate timestamp must be timezone-aware", safe_stage="P3D_INPUT"
        )
    counts = Counter(item.verdict for item in prepared.certificates)
    references = tuple(
        CertificateArtifactReference(
            certificate_id=certificate.certificate_id,
            relative_path=f"certificates/{path.name}",
            certificate_sha256=hashlib.sha256(payload).hexdigest(),
        )
        for certificate, path, payload in zip(
            prepared.certificates,
            prepared.certificate_paths,
            prepared.certificate_payloads,
            strict=True,
        )
    )
    manifest = Phase3PublicCertificateManifest(
        run_id=run_id,
        created_at=timestamp,
        frozen_manifest_sha256=prepared.cohort.overlay_manifest_sha256,
        natural_manifest_sha256=prepared.cohort.natural_manifest_sha256,
        public_source_bundle_id=prepared.public_source_bundle_id,
        public_source_sha256=prepared.public_source_sha256,
        public_evidence_run_id=prepared.public_evidence_run_id,
        public_evidence_manifest_sha256=prepared.public_evidence_manifest_sha256,
        public_evidence_results_sha256=prepared.public_evidence_results_sha256,
        claims_bundle_id=prepared.claims.bundle_id,
        claims_bundle_sha256=prepared.claims_sha256,
        certificate_policy_sha256=CERTIFICATE_POLICY_SHA256,
        ordered_certificate_ids=tuple(item.certificate_id for item in prepared.certificates),
        certificate_artifacts=references,
        certificate_count=len(prepared.certificates),
        confirmed_bug_count=counts["confirmed_bug"],
        strongly_supported_count=counts["strongly_supported"],
        unverified_suspicion_count=counts["unverified_suspicion"],
        certificate_payloads_sha256=prepared.certificate_payloads_sha256,
    )
    assert_public_payload_safe(manifest, canaries=privacy_canaries)
    manifest_payload = (
        json.dumps(
            manifest.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    manifest_path, certificate_paths = _publish_certificate_bundle(
        prepared=prepared,
        manifest_payload=manifest_payload,
    )
    preflight = _preflight_result(prepared)
    return PublicCertificateRunResult(
        run_id=preflight.run_id,
        certificate_count=preflight.certificate_count,
        confirmed_bug_count=preflight.confirmed_bug_count,
        strongly_supported_count=preflight.strongly_supported_count,
        unverified_suspicion_count=preflight.unverified_suspicion_count,
        claims_bundle_sha256=preflight.claims_bundle_sha256,
        certificate_policy_sha256=preflight.certificate_policy_sha256,
        certificate_payloads_sha256=preflight.certificate_payloads_sha256,
        public_evidence_run_id=preflight.public_evidence_run_id,
        run_dir=manifest_path.parent,
        manifest_path=manifest_path,
        certificate_paths=certificate_paths,
        manifest_sha256=hashlib.sha256(manifest_payload).hexdigest(),
    )
