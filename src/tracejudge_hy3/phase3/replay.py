"""Replay one confirmed Gate-D certificate using only exact public fixtures."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from tracejudge_hy3.sandbox.trusted_local import TrustedLocalSandbox
from tracejudge_hy3.schemas.problem import TestCase

from .contracts import CounterfactualTrace, Phase3ErrorCertificate, PublicFixtureCase
from .counterfactual import (
    _decode_json,
    _function_name,
    _load_source_bundle,
    _public_problem_payload,
    _read_regular_file,
    _solution_hashes,
)
from .privacy import assert_public_payload_safe, canonical_sha256
from .public_evidence import (
    Phase3PublicEvidenceError,
    PublicProbeInput,
    _probe_key,
    _source_context,
    deterministic_probe_inputs,
    public_execution_evidence_payload,
)
from .runner import Phase3RunnerError, load_paired_cohort


@dataclass(frozen=True, slots=True)
class PublicCertificateReplayResult:
    certificate_id: str
    trace_id: str
    problem_id: str
    verified: bool
    reproduced_failure: bool
    execution_evidence_sha256: str
    sandbox_backend: str
    executed_case_count: int


def _same_value(left: Any, right: Any) -> bool:
    return canonical_sha256(left) == canonical_sha256(right)


def _is_subsequence(candidate: Sequence[Any], original: Sequence[Any]) -> bool:
    if len(candidate) > len(original):
        return False
    position = 0
    for item in original:
        if position < len(candidate) and _same_value(candidate[position], item):
            position += 1
    return position == len(candidate)


def _is_minimized_probe(
    args: Sequence[Any],
    kwargs: dict[str, Any],
    probes: Sequence[PublicProbeInput],
) -> bool:
    for probe in probes:
        if canonical_sha256(dict(probe.kwargs)) != canonical_sha256(kwargs):
            continue
        if len(args) != len(probe.args):
            continue
        changed = 0
        valid = True
        for candidate, original in zip(args, probe.args, strict=True):
            if isinstance(candidate, list) and isinstance(original, list):
                if not _is_subsequence(candidate, original):
                    valid = False
                    break
                changed += candidate != original
            elif not _same_value(candidate, original):
                valid = False
                break
        if valid and changed == 1:
            return True
    return False


def _replay_case(
    certificate: Phase3ErrorCertificate,
    fixture_cases: Sequence[PublicFixtureCase],
    *,
    maximum_probes: int,
    fixture: Any,
) -> tuple[str, TestCase]:
    counterexample = certificate.counterexample
    assert counterexample is not None
    if counterexample.source == "public_challenge_test":
        matches = [
            item
            for item in fixture_cases
            if item.category == "challenge"
            and certificate.violated_requirement_id in item.related_requirements
            and _same_value(item.args, counterexample.args)
            and _same_value(item.kwargs, counterexample.kwargs)
            and _same_value(item.expected, counterexample.expected)
        ]
        if len(matches) != 1:
            raise Phase3PublicEvidenceError(
                "certificate public challenge is not unique in the exact fixture",
                safe_stage="P3D_REPLAY_BINDING",
            )
        case_id = matches[0].case_id
    elif counterexample.source in {"deterministic_probe", "minimized"}:
        probes = deterministic_probe_inputs(fixture, maximum_probes=maximum_probes)
        exact = any(
            _probe_key(item.args, item.kwargs)
            == _probe_key(counterexample.args, counterexample.kwargs)
            for item in probes
        )
        minimized = _is_minimized_probe(counterexample.args, counterexample.kwargs, probes)
        if not exact and not (counterexample.source == "minimized" and minimized):
            raise Phase3PublicEvidenceError(
                "certificate probe is outside the frozen deterministic search space",
                safe_stage="P3D_REPLAY_BINDING",
            )
        case_id = (
            f"deterministic-probe-{_probe_key(counterexample.args, counterexample.kwargs)[:12]}"
        )
    else:
        raise Phase3PublicEvidenceError(
            "this Gate-D replay accepts only public challenge or deterministic probe evidence",
            safe_stage="P3D_REPLAY_POLICY",
        )
    return case_id, TestCase(
        case_id=case_id,
        args=list(counterexample.args),
        kwargs=dict(counterexample.kwargs),
        expected=counterexample.expected,
        category="challenge",
        related_requirements=[certificate.violated_requirement_id],
    )


def replay_public_certificate(
    *,
    certificate_path: str | Path,
    cohort_manifest_path: str | Path,
    natural_manifest_path: str | Path,
    source_bundle_path: str | Path,
    per_test_timeout_seconds: float = 2.0,
    maximum_probes: int = 32,
    privacy_canaries: Sequence[str | bytes] = (),
) -> PublicCertificateReplayResult:
    """Reproduce one certificate without trusting any code or fixture in the certificate."""

    if per_test_timeout_seconds <= 0 or per_test_timeout_seconds > 10:
        raise Phase3PublicEvidenceError("replay timeout must be within (0, 10]")
    payload = _read_regular_file(
        Path(certificate_path).expanduser().resolve(),
        label="public error certificate",
    )
    try:
        certificate = Phase3ErrorCertificate.model_validate(
            _decode_json(payload, label="public error certificate")
        )
        assert_public_payload_safe(certificate, canaries=privacy_canaries)
    except (ValidationError, ValueError):
        raise Phase3PublicEvidenceError(
            "public certificate failed contract or privacy validation",
            safe_stage="P3D_REPLAY_CERTIFICATE",
        ) from None
    if certificate.verdict != "confirmed_bug" or certificate.counterexample is None:
        raise Phase3PublicEvidenceError(
            "only confirmed_bug certificates carry replayable evidence",
            safe_stage="P3D_REPLAY_CERTIFICATE",
        )
    prepared = _load_source_bundle(
        source_bundle_path,
        privacy_canaries=privacy_canaries,
    )
    if certificate.counterexample.public_source_sha256 != prepared.source_sha256:
        raise Phase3PublicEvidenceError(
            "certificate references a different public source",
            safe_stage="P3D_REPLAY_BINDING",
        )
    try:
        cohort = load_paired_cohort(
            overlay_manifest_path=cohort_manifest_path,
            natural_manifest_path=natural_manifest_path,
        )
    except Phase3RunnerError as exc:
        raise Phase3PublicEvidenceError(
            "frozen cohort failed replay binding",
            safe_stage="P3D_REPLAY_BINDING",
        ) from exc
    trace = cohort.traces_by_id.get(certificate.trace_id)
    if not isinstance(trace, CounterfactualTrace):
        raise Phase3PublicEvidenceError(
            "certificate trace is absent from the frozen public cohort",
            safe_stage="P3D_REPLAY_BINDING",
        )
    if (
        certificate.frozen_manifest_sha256 != cohort.overlay_manifest_sha256
        or certificate.problem_id != trace.problem_id
        or certificate.code_sha256 != trace.code_sha256
        or certificate.structured_explanation_sha256 != trace.structured_explanation_sha256
        or certificate.functional_evidence_sha256
        != trace.functional_evidence.functional_evidence_sha256
    ):
        raise Phase3PublicEvidenceError(
            "certificate hashes differ from the frozen trace",
            safe_stage="P3D_REPLAY_BINDING",
        )
    _parent, solution, fixture = _source_context(prepared, trace.trace_id)
    solution_sha, explanation_sha, code_sha = _solution_hashes(solution)
    if (
        solution_sha != trace.solution_trace_sha256
        or explanation_sha != trace.structured_explanation_sha256
        or code_sha != trace.code_sha256
        or canonical_sha256(_public_problem_payload(fixture)) != trace.public_problem_sha256
    ):
        raise Phase3PublicEvidenceError(
            "exact public source differs from the frozen trace",
            safe_stage="P3D_REPLAY_BINDING",
        )
    subject_id = trace.functional_evidence.execution_subject_id
    subject = next(
        (
            item.subject
            for item in prepared.candidates
            if item.subject.execution_subject_id == subject_id
        ),
        None,
    )
    if (
        subject is None
        or subject.code_sha256 != trace.code_sha256
        or subject.replay_spec_sha256 != certificate.counterexample.replay_spec_sha256
    ):
        raise Phase3PublicEvidenceError(
            "certificate replay spec differs from the exact public subject",
            safe_stage="P3D_REPLAY_BINDING",
        )
    case_id, test_case = _replay_case(
        certificate,
        fixture.test_cases,
        maximum_probes=maximum_probes,
        fixture=fixture,
    )
    sandbox = TrustedLocalSandbox(
        per_test_timeout_seconds=per_test_timeout_seconds,
        allow_untrusted_code=False,
    )
    summary = sandbox.run(solution.code, _function_name(fixture.function_signature), [test_case])
    if summary.runtime_status != "completed" or len(summary.results) != 1:
        raise Phase3PublicEvidenceError(
            "replay ended in an infrastructure error",
            safe_stage="P3D_REPLAY_INFRASTRUCTURE",
        )
    observed = summary.results[0]
    counterexample = certificate.counterexample
    if (
        observed.passed
        or observed.actual_output != counterexample.candidate_output
        or observed.exception_type != counterexample.candidate_exception
        or observed.timed_out != counterexample.timed_out
    ):
        raise Phase3PublicEvidenceError(
            "public replay did not reproduce the certified failure",
            safe_stage="P3D_REPLAY_MISMATCH",
        )
    evidence_payload = public_execution_evidence_payload(
        trace_id=certificate.trace_id,
        case_id=case_id,
        args=counterexample.args,
        kwargs=counterexample.kwargs,
        expected=counterexample.expected,
        candidate_output=observed.actual_output,
        candidate_exception=observed.exception_type,
        timed_out=observed.timed_out,
        code_sha256=certificate.code_sha256,
        public_source_sha256=prepared.source_sha256,
        replay_spec_sha256=counterexample.replay_spec_sha256,
    )
    evidence_sha256 = canonical_sha256(evidence_payload)
    if evidence_sha256 != counterexample.execution_evidence_sha256:
        raise Phase3PublicEvidenceError(
            "replayed evidence hash differs from the certificate",
            safe_stage="P3D_REPLAY_MISMATCH",
        )
    return PublicCertificateReplayResult(
        certificate_id=certificate.certificate_id,
        trace_id=certificate.trace_id,
        problem_id=certificate.problem_id,
        verified=True,
        reproduced_failure=True,
        execution_evidence_sha256=evidence_sha256,
        sandbox_backend=sandbox.name,
        executed_case_count=1,
    )
