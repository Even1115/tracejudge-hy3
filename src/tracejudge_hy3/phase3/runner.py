"""Gate-C unified paired-method interface and resumable offline writer.

This module never loads credentials, calls Docker, executes candidate code, or
opens EvalPlus raw artifacts.  A caller supplies already validated, public
trace material and an explicit judge provider.  The same bound material is
projected through the five frozen method visibility policies in trace-major
order.  Provider failures, parse failures, AST failures, public-evidence
timeouts, and infrastructure failures remain explicit denominator rows.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, Self

from pydantic import Field, ValidationError, model_validator

from tracejudge_hy3.prompts.phase3 import (
    build_method_user_prompt,
    build_repair_user_prompt,
    method_prompt_sha256,
    method_prompt_version,
    method_system_prompt,
    prompt_bundle_sha256,
)
from tracejudge_hy3.schemas.execution import StaticEvidence
from tracejudge_hy3.schemas.solution import SolutionTrace
from tracejudge_hy3.static_analysis.ast_analyzer import analyze_code

from .contracts import (
    CounterfactualCohortManifest,
    ForbiddenInput,
    FrozenCohortManifest,
    FrozenTrace,
    MethodId,
    MethodJudgment,
    MethodOutcome,
    MethodOutcomeStatus,
    MethodSpec,
    MethodUsage,
    NaturalTrace,
    PairedEvaluationIndex,
    PairedMethodResultReference,
    Phase2FunctionalEvidenceRef,
    Phase3Invocation,
    Phase3ResumeIdentity,
    Phase3RunManifest,
    PublicFixtureFunctionalEvidenceRef,
    StrictFrozenModel,
    VisibleInput,
)
from .parser import StrictJudgmentParseError, parse_method_judgment
from .privacy import (
    assert_public_payload_safe,
    canonical_json_bytes,
    canonical_sha256,
    jsonl_record_sha256,
)

_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_OUTPUT_SCHEMA_SHA256 = canonical_sha256(MethodJudgment.model_json_schema())
_PUBLIC_EVIDENCE_POLICY_SHA256 = canonical_sha256(
    {
        "version": "phase3_public_dynamic_evidence_v1",
        "states": ["available", "timeout", "infrastructure_error"],
        "forbidden": [item.value for item in ForbiddenInput],
    }
)


class Phase3RunnerError(ValueError):
    def __init__(self, message: str, *, safe_stage: str = "P3C_INTERFACE") -> None:
        super().__init__(message)
        self.safe_stage = safe_stage


class Phase3ProviderCallError(Exception):
    """Safe provider failure classification without provider-controlled text."""

    def __init__(self, diagnostic_code: str = "provider_error") -> None:
        super().__init__(diagnostic_code)
        self.diagnostic_code = diagnostic_code


class _DuplicateJsonKey(ValueError):
    pass


class PublicDynamicEvidenceInput(StrictFrozenModel):
    status: Literal["available", "timeout", "infrastructure_error"]
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.status == "available":
            if self.payload is None:
                raise ValueError("available public evidence requires a payload")
            assert_public_payload_safe(self.payload)
            if canonical_sha256(self.payload) != self.evidence_sha256:
                raise ValueError("public dynamic evidence hash is inconsistent")
        elif self.payload is not None:
            raise ValueError("failed public evidence may not carry a payload")
        return self


class Phase3TraceMaterial(StrictFrozenModel):
    """Private in-process material whose hashes must match one frozen trace."""

    trace_id: str
    public_problem: dict[str, Any]
    solution_trace: SolutionTrace
    functional_evidence: Phase2FunctionalEvidenceRef | PublicFixtureFunctionalEvidenceRef
    public_dynamic_evidence: PublicDynamicEvidenceInput | None = None

    @model_validator(mode="after")
    def validate_public_boundaries(self) -> Self:
        assert_public_payload_safe(self.public_problem)
        assert_public_payload_safe(self.solution_trace)
        assert_public_payload_safe(self.functional_evidence)
        if self.solution_trace.problem_id != self.functional_evidence.problem_id:
            raise ValueError("trace material problem and functional evidence differ")
        return self


@dataclass(frozen=True, slots=True)
class LoadedPairedCohort:
    overlay_manifest_sha256: str
    natural_manifest_sha256: str
    ordered_trace_ids: tuple[str, ...]
    traces_by_id: Mapping[str, FrozenTrace]
    natural_trace_count: int
    counterfactual_trace_count: int


@dataclass(frozen=True, slots=True)
class Phase3InterfacePreflight:
    freeze_id: str
    natural_trace_count: int
    counterfactual_trace_count: int
    trace_count: int
    method_count: int
    pair_count: int
    method_specs_sha256: str
    prompt_bundle_sha256: str
    output_schema_sha256: str
    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class ProviderCallResult:
    raw_text: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reported_cost_microusd: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.raw_text, str) or not self.raw_text:
            raise ValueError("provider raw_text must be non-empty text")
        for value in (self.prompt_tokens, self.completion_tokens, self.reported_cost_microusd):
            if value is not None and (isinstance(value, bool) or value < 0):
                raise ValueError("provider usage values must be non-negative integers")


class Phase3JudgeProvider(Protocol):
    name: str
    model: str

    def public_configuration(self) -> Mapping[str, Any]: ...

    async def complete(
        self,
        *,
        method_id: MethodId,
        messages: tuple[dict[str, str], ...],
        temperature: float,
        timeout_seconds: float,
    ) -> ProviderCallResult: ...


@dataclass(frozen=True, slots=True)
class _RawAttempt:
    trace_id: str
    method_id: MethodId
    attempt_number: int
    raw_text: str


@dataclass(frozen=True, slots=True)
class _PairEvaluation:
    outcome: MethodOutcome
    raw_attempts: tuple[_RawAttempt, ...]


@dataclass(frozen=True, slots=True)
class Phase3RunResult:
    run_id: str
    run_dir: Path
    manifest_path: Path
    results_path: Path
    index_path: Path
    result_count: int
    reused_count: int
    status_counts: Mapping[MethodOutcomeStatus, int]
    results_sha256: str
    index_sha256: str


@dataclass(frozen=True, slots=True)
class Phase3ExecutionBindings:
    """Exact non-method inputs that make one formal paired run reproducible."""

    natural_manifest_sha256: str
    material_payloads_sha256: str
    provider_config_sha256: str
    annotation_set_manifest_sha256: str
    completed_labels_sha256: str
    annotation_records_sha256: str


def provider_config_sha256(provider: Phase3JudgeProvider) -> str:
    try:
        configuration = dict(provider.public_configuration())
    except Exception:
        raise Phase3RunnerError(
            "judge provider public configuration is unavailable",
            safe_stage="P3E_PROVIDER_IDENTITY",
        ) from None
    if configuration.get("provider") != provider.name or configuration.get("model") != (
        provider.model
    ):
        raise Phase3RunnerError(
            "judge provider public configuration identity differs",
            safe_stage="P3E_PROVIDER_IDENTITY",
        )
    assert_public_payload_safe(configuration)
    return canonical_sha256(configuration)


def output_schema_sha256() -> str:
    return _OUTPUT_SCHEMA_SHA256


def public_evidence_policy_sha256() -> str:
    return _PUBLIC_EVIDENCE_POLICY_SHA256


def method_specs_sha256(specs: Sequence[MethodSpec]) -> str:
    return canonical_sha256([item.model_dump(mode="json") for item in specs])


def implementation_sha256() -> str:
    paths = (
        Path(__file__),
        Path(__file__).with_name("contracts.py"),
        Path(__file__).with_name("execution.py"),
        Path(__file__).with_name("materials.py"),
        Path(__file__).with_name("parser.py"),
        Path(__file__).parents[1] / "prompts" / "phase3.py",
    )
    digest = hashlib.sha256()
    for path in paths:
        relative_path = path.relative_to(Path(__file__).parents[1])
        digest.update(relative_path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def ast_implementation_sha256() -> str:
    from tracejudge_hy3.static_analysis import ast_analyzer

    return hashlib.sha256(Path(ast_analyzer.__file__).read_bytes()).hexdigest()


def build_method_specs(
    *,
    provider: str,
    model: str,
    temperature: float = 0.0,
    timeout_seconds: float = 120.0,
) -> tuple[MethodSpec, ...]:
    if not provider.strip() or not model.strip():
        raise Phase3RunnerError("provider and model names must be non-empty")
    if temperature < 0 or timeout_seconds <= 0:
        raise Phase3RunnerError("temperature and timeout are outside the allowed range")

    forbidden = tuple(ForbiddenInput)
    common = (
        VisibleInput.PUBLIC_PROBLEM,
        VisibleInput.SOLUTION_TRACE,
        VisibleInput.CANDIDATE_CODE,
        VisibleInput.FUNCTIONAL_EVIDENCE,
    )
    visible = {
        MethodId.TEST_ONLY: (VisibleInput.FUNCTIONAL_EVIDENCE,),
        MethodId.DIRECT_LLM_JUDGE: common,
        MethodId.FOUR_LAYER_STRUCTURED_JUDGE: common,
        MethodId.FOUR_LAYER_AST: (*common, VisibleInput.AST_EVIDENCE),
        MethodId.FULL_TRACEJUDGE: (
            *common,
            VisibleInput.AST_EVIDENCE,
            VisibleInput.PUBLIC_DYNAMIC_EVIDENCE,
        ),
    }
    specs: list[MethodSpec] = []
    for method_id in MethodId:
        if method_id == MethodId.TEST_ONLY:
            specs.append(
                MethodSpec(
                    method_id=method_id,
                    visible_inputs=visible[method_id],
                    forbidden_inputs=forbidden,
                    uses_llm=False,
                    uses_ast=False,
                    uses_public_dynamic_evidence=False,
                    max_parse_repairs=0,
                    parse_policy="not_applicable",
                )
            )
            continue
        specs.append(
            MethodSpec(
                method_id=method_id,
                visible_inputs=visible[method_id],
                forbidden_inputs=forbidden,
                uses_llm=True,
                uses_ast=method_id in {MethodId.FOUR_LAYER_AST, MethodId.FULL_TRACEJUDGE},
                uses_public_dynamic_evidence=method_id == MethodId.FULL_TRACEJUDGE,
                prompt_version=method_prompt_version(method_id),
                prompt_sha256=method_prompt_sha256(method_id),
                output_schema_sha256=_OUTPUT_SCHEMA_SHA256,
                provider=provider,
                model=model,
                temperature=temperature,
                timeout_seconds=timeout_seconds,
                max_parse_repairs=1,
                parse_policy="strict_json_schema_one_repair_v1",
            )
        )
    frozen = tuple(specs)
    validate_method_specs(frozen)
    return frozen


def validate_method_specs(specs: Sequence[MethodSpec]) -> None:
    if tuple(item.method_id for item in specs) != tuple(MethodId):
        raise Phase3RunnerError("method specs must contain the five methods in frozen order")
    for spec in specs:
        if spec.method_id == MethodId.TEST_ONLY:
            continue
        if spec.prompt_version != method_prompt_version(spec.method_id):
            raise Phase3RunnerError("method prompt version differs from the frozen implementation")
        if spec.prompt_sha256 != method_prompt_sha256(spec.method_id):
            raise Phase3RunnerError("method prompt hash differs from the frozen implementation")
        if spec.output_schema_sha256 != _OUTPUT_SCHEMA_SHA256:
            raise Phase3RunnerError("method output schema hash differs from MethodJudgment")


def _read_manifest(path: Path, *, label: str) -> tuple[bytes, dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise Phase3RunnerError(
            f"{label} must be a regular non-symlink file", safe_stage="P3C_INPUT"
        )
    try:
        if path.stat().st_size > _MAX_MANIFEST_BYTES:
            raise Phase3RunnerError(f"{label} exceeds the size limit", safe_stage="P3C_INPUT")
        payload = path.read_bytes()
    except OSError:
        raise Phase3RunnerError(f"cannot read {label}", safe_stage="P3C_INPUT") from None
    if len(payload) > _MAX_MANIFEST_BYTES:
        raise Phase3RunnerError(f"{label} exceeds the size limit", safe_stage="P3C_INPUT")

    def strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise _DuplicateJsonKey
            value[key] = item
        return value

    def reject_constant(_value: str) -> None:
        raise ValueError

    try:
        value = json.loads(
            payload,
            object_pairs_hook=strict_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKey, ValueError):
        raise Phase3RunnerError(
            f"{label} is not strict UTF-8 JSON", safe_stage="P3C_INPUT"
        ) from None
    if not isinstance(value, dict):
        raise Phase3RunnerError(f"{label} must contain one JSON object", safe_stage="P3C_INPUT")
    return payload, value


def load_paired_cohort(
    *,
    overlay_manifest_path: str | Path,
    natural_manifest_path: str | Path,
) -> LoadedPairedCohort:
    natural_payload, natural_value = _read_manifest(
        Path(natural_manifest_path), label="natural manifest"
    )
    overlay_payload, overlay_value = _read_manifest(
        Path(overlay_manifest_path), label="overlay manifest"
    )
    try:
        natural = FrozenCohortManifest.model_validate(natural_value)
        overlay = CounterfactualCohortManifest.model_validate(overlay_value)
    except ValidationError:
        raise Phase3RunnerError(
            "phase-three cohort manifest failed schema validation", safe_stage="P3C_COHORT"
        ) from None

    natural_sha = hashlib.sha256(natural_payload).hexdigest()
    overlay_sha = hashlib.sha256(overlay_payload).hexdigest()
    if overlay.natural_cohort.manifest_sha256 != natural_sha:
        raise Phase3RunnerError(
            "overlay does not reference the exact natural manifest", safe_stage="P3C_COHORT"
        )
    if overlay.natural_cohort.freeze_id != natural.freeze_id:
        raise Phase3RunnerError("overlay natural freeze identity differs", safe_stage="P3C_COHORT")
    if overlay.natural_cohort.ordered_trace_ids != natural.ordered_trace_ids:
        raise Phase3RunnerError("overlay natural trace order differs", safe_stage="P3C_COHORT")
    if any(not isinstance(trace, NaturalTrace) for trace in natural.traces):
        raise Phase3RunnerError(
            "referenced natural manifest contains non-natural traces", safe_stage="P3C_COHORT"
        )

    traces = tuple(natural.traces) + tuple(overlay.counterfactuals)
    trace_ids = tuple(item.trace_id for item in traces)
    if trace_ids != overlay.paired_ordered_trace_ids:
        raise Phase3RunnerError(
            "combined trace records differ from the frozen paired order", safe_stage="P3C_COHORT"
        )
    if tuple(overlay.paired_method_ids) != tuple(MethodId):
        raise Phase3RunnerError("cohort does not declare all five methods", safe_stage="P3C_COHORT")
    return LoadedPairedCohort(
        overlay_manifest_sha256=overlay_sha,
        natural_manifest_sha256=natural_sha,
        ordered_trace_ids=trace_ids,
        traces_by_id={item.trace_id: item for item in traces},
        natural_trace_count=len(natural.traces),
        counterfactual_trace_count=len(overlay.counterfactuals),
    )


def preflight_paired_interface(
    *,
    overlay_manifest_path: str | Path,
    natural_manifest_path: str | Path,
    provider: str,
    model: str,
    temperature: float = 0.0,
    timeout_seconds: float = 120.0,
) -> Phase3InterfacePreflight:
    cohort = load_paired_cohort(
        overlay_manifest_path=overlay_manifest_path,
        natural_manifest_path=natural_manifest_path,
    )
    specs = build_method_specs(
        provider=provider,
        model=model,
        temperature=temperature,
        timeout_seconds=timeout_seconds,
    )
    overlay_payload, overlay_value = _read_manifest(
        Path(overlay_manifest_path), label="overlay manifest"
    )
    del overlay_payload
    freeze_id = str(overlay_value["freeze_id"])
    return Phase3InterfacePreflight(
        freeze_id=freeze_id,
        natural_trace_count=cohort.natural_trace_count,
        counterfactual_trace_count=cohort.counterfactual_trace_count,
        trace_count=len(cohort.ordered_trace_ids),
        method_count=len(specs),
        pair_count=len(cohort.ordered_trace_ids) * len(specs),
        method_specs_sha256=method_specs_sha256(specs),
        prompt_bundle_sha256=prompt_bundle_sha256(),
        output_schema_sha256=_OUTPUT_SCHEMA_SHA256,
        provider=provider,
        model=model,
    )


def _bind_material(trace: FrozenTrace, material: Phase3TraceMaterial) -> None:
    if material.trace_id != trace.trace_id:
        raise Phase3RunnerError(
            "trace material ID differs from frozen trace", safe_stage="P3C_MATERIAL"
        )
    if material.solution_trace.problem_id != trace.problem_id:
        raise Phase3RunnerError("trace material problem_id differs", safe_stage="P3C_MATERIAL")
    solution_payload = material.solution_trace.model_dump(mode="json")
    explanation_payload = {key: value for key, value in solution_payload.items() if key != "code"}
    bindings = (
        (canonical_sha256(material.public_problem), trace.public_problem_sha256),
        (canonical_sha256(solution_payload), trace.solution_trace_sha256),
        (canonical_sha256(explanation_payload), trace.structured_explanation_sha256),
        (
            hashlib.sha256(material.solution_trace.code.encode("utf-8")).hexdigest(),
            trace.code_sha256,
        ),
    )
    if any(actual != expected for actual, expected in bindings):
        raise Phase3RunnerError(
            "trace material hash differs from frozen trace", safe_stage="P3C_MATERIAL"
        )
    if material.functional_evidence != trace.functional_evidence:
        raise Phase3RunnerError(
            "trace functional evidence differs from frozen reference", safe_stage="P3C_MATERIAL"
        )


def validate_materials(
    cohort: LoadedPairedCohort,
    materials: Mapping[str, Phase3TraceMaterial],
    *,
    privacy_canaries: Sequence[str | bytes] = (),
) -> None:
    if set(materials) != set(cohort.ordered_trace_ids):
        raise Phase3RunnerError(
            "trace material set differs from frozen cohort", safe_stage="P3C_MATERIAL"
        )
    for trace_id in cohort.ordered_trace_ids:
        material = materials[trace_id]
        _bind_material(cohort.traces_by_id[trace_id], material)
        assert_public_payload_safe(material.public_problem, canaries=privacy_canaries)
        assert_public_payload_safe(material.solution_trace, canaries=privacy_canaries)
        assert_public_payload_safe(material.functional_evidence, canaries=privacy_canaries)
        if material.public_dynamic_evidence is not None:
            assert_public_payload_safe(
                material.public_dynamic_evidence,
                canaries=privacy_canaries,
            )


def _function_name(public_problem: Mapping[str, Any]) -> str | None:
    signature = public_problem.get("function_signature")
    if not isinstance(signature, str):
        return None
    normalized = signature.strip()
    if normalized.startswith("def "):
        normalized = normalized[4:]
    name = normalized.split("(", 1)[0].strip()
    return name or None


def _solution_explanation(solution: SolutionTrace) -> dict[str, Any]:
    payload = solution.model_dump(mode="json")
    payload.pop("code")
    return payload


def functional_evidence_payload(
    evidence: Phase2FunctionalEvidenceRef | PublicFixtureFunctionalEvidenceRef,
) -> dict[str, Any]:
    """Project frozen status evidence without semantic variant identifiers.

    Public-Fixture ``execution_subject_id`` values are provenance identifiers and
    may encode the counterfactual construction type.  That identity remains bound
    in the frozen manifest, but it is not a method-visible research feature.
    """

    payload = evidence.model_dump(mode="json")
    payload.pop("execution_subject_id", None)
    return payload


def _base_payload(material: Phase3TraceMaterial) -> dict[str, Any]:
    return {
        "public_problem": material.public_problem,
        "solution_trace": _solution_explanation(material.solution_trace),
        "candidate_code": material.solution_trace.code,
        "functional_evidence": functional_evidence_payload(material.functional_evidence),
    }


def _input_projection(
    *,
    spec: MethodSpec,
    material: Phase3TraceMaterial,
    static_evidence: StaticEvidence | None,
) -> dict[str, Any]:
    if spec.method_id == MethodId.TEST_ONLY:
        return {"functional_evidence": functional_evidence_payload(material.functional_evidence)}
    payload = _base_payload(material)
    if spec.uses_ast:
        assert static_evidence is not None
        payload["ast_evidence"] = static_evidence.model_dump(mode="json")
    if spec.uses_public_dynamic_evidence:
        dynamic = material.public_dynamic_evidence
        assert dynamic is not None and dynamic.status == "available" and dynamic.payload is not None
        payload["public_dynamic_evidence"] = dynamic.payload
    if set(payload) != {item.value for item in spec.visible_inputs}:
        raise Phase3RunnerError("method input projection differs from frozen visibility policy")
    assert_public_payload_safe(payload)
    return payload


def project_method_input(
    *,
    spec: MethodSpec,
    material: Phase3TraceMaterial,
) -> dict[str, Any]:
    """Public helper used by tests and audits to inspect exact visibility."""

    static_evidence = None
    if spec.uses_ast:
        static_evidence = analyze_code(
            material.solution_trace.code,
            function_name=_function_name(material.public_problem),
        )
        if not static_evidence.ast_parse_ok:
            raise Phase3RunnerError("AST input is unavailable", safe_stage="P3C_AST")
    return _input_projection(spec=spec, material=material, static_evidence=static_evidence)


def _test_only_judgment(material: Phase3TraceMaterial) -> MethodJudgment | None:
    evidence = material.functional_evidence
    if isinstance(evidence, Phase2FunctionalEvidenceRef):
        functional_correct = evidence.passed_plus
        timed_out = evidence.base_status == "timeout" or evidence.plus_status == "timeout"
        public = False
        summary = "Frozen phase-two safe status reports Base+Extra pass."
        if not functional_correct:
            summary = (
                "Frozen phase-two safe status reports a functional failure without hidden inputs."
            )
    else:
        if evidence.execution_status == "timeout":
            return None
        functional_correct = evidence.execution_status == "pass"
        timed_out = False
        public = evidence.publicly_replayable
        summary = f"Frozen public Fixture status is {evidence.execution_status}."
    if functional_correct:
        return MethodJudgment(functional_correct=True, has_error=False)
    return MethodJudgment(
        functional_correct=False,
        has_error=True,
        first_faulty_layer="execution",
        code_span="candidate function",
        error_type="E02_TIMEOUT_OR_RESOURCE_ERROR" if timed_out else "E03_WRONG_OUTPUT",
        verdict="confirmed_bug" if public else "unverified_suspicion",
        evidence_summary=(summary,),
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _duration(start: float) -> float:
    return max(0.0, time.perf_counter() - start)


def _usage(call_results: Sequence[ProviderCallResult]) -> MethodUsage:
    prompt_values = [item.prompt_tokens for item in call_results]
    completion_values = [item.completion_tokens for item in call_results]
    costs = [item.reported_cost_microusd for item in call_results]
    prompt_tokens = (
        sum(value for value in prompt_values if value is not None)
        if any(value is not None for value in prompt_values)
        else None
    )
    completion_tokens = (
        sum(value for value in completion_values if value is not None)
        if any(value is not None for value in completion_values)
        else None
    )
    if call_results and all(value is not None for value in costs):
        return MethodUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reported_cost_microusd=sum(value for value in costs if value is not None),
            cost_status="provider_reported",
        )
    return MethodUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_status="unavailable",
    )


async def _provider_call(
    provider: Phase3JudgeProvider,
    *,
    spec: MethodSpec,
    messages: tuple[dict[str, str], ...],
) -> ProviderCallResult:
    assert spec.temperature is not None and spec.timeout_seconds is not None
    try:
        async with asyncio.timeout(spec.timeout_seconds):
            return await provider.complete(
                method_id=spec.method_id,
                messages=messages,
                temperature=spec.temperature,
                timeout_seconds=spec.timeout_seconds,
            )
    except TimeoutError:
        raise Phase3ProviderCallError("provider_timeout") from None


def _failure_outcome(
    *,
    run_id: str,
    trace_id: str,
    method_id: MethodId,
    status: MethodOutcomeStatus,
    method_input_sha256: str,
    started_at: datetime,
    started_perf: float,
    diagnostic_code: str,
    attempt_count: int = 0,
    parse_repair_count: int = 0,
    raw_output_sha256: str | None = None,
    usage: MethodUsage | None = None,
) -> MethodOutcome:
    return MethodOutcome(
        run_id=run_id,
        trace_id=trace_id,
        method_id=method_id,
        status=status,
        method_input_sha256=method_input_sha256,
        attempt_count=attempt_count,
        parse_repair_count=parse_repair_count,
        raw_output_sha256=raw_output_sha256,
        usage=usage or MethodUsage(cost_status="unavailable"),
        diagnostic_code=diagnostic_code,
        started_at=started_at,
        ended_at=_now(),
        duration_seconds=_duration(started_perf),
    )


async def evaluate_method(
    *,
    run_id: str,
    spec: MethodSpec,
    material: Phase3TraceMaterial,
    provider: Phase3JudgeProvider,
) -> _PairEvaluation:
    started_at = _now()
    started_perf = time.perf_counter()

    if spec.provider is not None and (
        provider.name != spec.provider or provider.model != spec.model
    ):
        payload_hash = canonical_sha256(
            {
                "trace_id": material.trace_id,
                "method_id": spec.method_id,
                "input_status": "provider_identity_mismatch",
            }
        )
        return _PairEvaluation(
            _failure_outcome(
                run_id=run_id,
                trace_id=material.trace_id,
                method_id=spec.method_id,
                status=MethodOutcomeStatus.INFRASTRUCTURE_ERROR,
                method_input_sha256=payload_hash,
                started_at=started_at,
                started_perf=started_perf,
                diagnostic_code="provider_identity_mismatch",
            ),
            (),
        )

    static_evidence: StaticEvidence | None = None
    if spec.uses_ast:
        static_evidence = analyze_code(
            material.solution_trace.code,
            function_name=_function_name(material.public_problem),
        )
        if not static_evidence.ast_parse_ok:
            payload_hash = canonical_sha256(
                {
                    "trace_id": material.trace_id,
                    "method_id": spec.method_id,
                    "input_status": "ast_error",
                }
            )
            return _PairEvaluation(
                _failure_outcome(
                    run_id=run_id,
                    trace_id=material.trace_id,
                    method_id=spec.method_id,
                    status=MethodOutcomeStatus.AST_ERROR,
                    method_input_sha256=payload_hash,
                    started_at=started_at,
                    started_perf=started_perf,
                    diagnostic_code="ast_parse_failed",
                ),
                (),
            )

    if spec.uses_public_dynamic_evidence:
        dynamic = material.public_dynamic_evidence
        if dynamic is None or dynamic.status == "infrastructure_error":
            payload_hash = canonical_sha256(
                {
                    "trace_id": material.trace_id,
                    "method_id": spec.method_id,
                    "input_status": "public_evidence_infrastructure_error",
                }
            )
            return _PairEvaluation(
                _failure_outcome(
                    run_id=run_id,
                    trace_id=material.trace_id,
                    method_id=spec.method_id,
                    status=MethodOutcomeStatus.INFRASTRUCTURE_ERROR,
                    method_input_sha256=payload_hash,
                    started_at=started_at,
                    started_perf=started_perf,
                    diagnostic_code="public_evidence_unavailable",
                ),
                (),
            )
        if dynamic.status == "timeout":
            payload_hash = canonical_sha256(
                {
                    "trace_id": material.trace_id,
                    "method_id": spec.method_id,
                    "input_status": "public_execution_timeout",
                }
            )
            return _PairEvaluation(
                _failure_outcome(
                    run_id=run_id,
                    trace_id=material.trace_id,
                    method_id=spec.method_id,
                    status=MethodOutcomeStatus.PUBLIC_EXECUTION_TIMEOUT,
                    method_input_sha256=payload_hash,
                    started_at=started_at,
                    started_perf=started_perf,
                    diagnostic_code="public_execution_timeout",
                ),
                (),
            )

    payload = _input_projection(spec=spec, material=material, static_evidence=static_evidence)
    input_sha = canonical_sha256(payload)
    if spec.method_id == MethodId.TEST_ONLY:
        judgment = _test_only_judgment(material)
        if judgment is None:
            return _PairEvaluation(
                _failure_outcome(
                    run_id=run_id,
                    trace_id=material.trace_id,
                    method_id=spec.method_id,
                    status=MethodOutcomeStatus.PUBLIC_EXECUTION_TIMEOUT,
                    method_input_sha256=input_sha,
                    started_at=started_at,
                    started_perf=started_perf,
                    diagnostic_code="frozen_public_execution_timeout",
                    usage=MethodUsage(cost_status="not_applicable"),
                ),
                (),
            )
        return _PairEvaluation(
            MethodOutcome(
                run_id=run_id,
                trace_id=material.trace_id,
                method_id=spec.method_id,
                status=MethodOutcomeStatus.VALID_JUDGMENT,
                method_input_sha256=input_sha,
                judgment=judgment,
                attempt_count=0,
                parse_repair_count=0,
                usage=MethodUsage(cost_status="not_applicable"),
                started_at=started_at,
                ended_at=_now(),
                duration_seconds=_duration(started_perf),
            ),
            (),
        )

    system_prompt = method_system_prompt(spec.method_id)
    user_prompt = build_method_user_prompt(spec.method_id, payload)
    messages: tuple[dict[str, str], ...] = (
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    )
    call_results: list[ProviderCallResult] = []
    raw_attempts: list[_RawAttempt] = []
    parse_error: StrictJudgmentParseError | None = None
    for attempt_number in (1, 2):
        try:
            result = await _provider_call(provider, spec=spec, messages=messages)
        except Phase3ProviderCallError as exc:
            return _PairEvaluation(
                _failure_outcome(
                    run_id=run_id,
                    trace_id=material.trace_id,
                    method_id=spec.method_id,
                    status=MethodOutcomeStatus.PROVIDER_ERROR,
                    method_input_sha256=input_sha,
                    started_at=started_at,
                    started_perf=started_perf,
                    diagnostic_code=exc.diagnostic_code,
                    attempt_count=attempt_number,
                    parse_repair_count=attempt_number - 1,
                    raw_output_sha256=(
                        hashlib.sha256(call_results[-1].raw_text.encode("utf-8")).hexdigest()
                        if call_results
                        else None
                    ),
                    usage=_usage(call_results),
                ),
                tuple(raw_attempts),
            )
        except Exception:
            return _PairEvaluation(
                _failure_outcome(
                    run_id=run_id,
                    trace_id=material.trace_id,
                    method_id=spec.method_id,
                    status=MethodOutcomeStatus.PROVIDER_ERROR,
                    method_input_sha256=input_sha,
                    started_at=started_at,
                    started_perf=started_perf,
                    diagnostic_code="provider_exception",
                    attempt_count=attempt_number,
                    parse_repair_count=attempt_number - 1,
                    raw_output_sha256=(
                        hashlib.sha256(call_results[-1].raw_text.encode("utf-8")).hexdigest()
                        if call_results
                        else None
                    ),
                    usage=_usage(call_results),
                ),
                tuple(raw_attempts),
            )
        call_results.append(result)
        raw_attempts.append(
            _RawAttempt(material.trace_id, spec.method_id, attempt_number, result.raw_text)
        )
        raw_sha = hashlib.sha256(result.raw_text.encode("utf-8")).hexdigest()
        try:
            judgment = parse_method_judgment(result.raw_text)
        except StrictJudgmentParseError as exc:
            parse_error = exc
            if attempt_number == 1:
                messages = (
                    *messages,
                    {"role": "user", "content": build_repair_user_prompt(exc.safe_diagnostic)},
                )
                continue
            return _PairEvaluation(
                _failure_outcome(
                    run_id=run_id,
                    trace_id=material.trace_id,
                    method_id=spec.method_id,
                    status=MethodOutcomeStatus.PARSE_ERROR,
                    method_input_sha256=input_sha,
                    started_at=started_at,
                    started_perf=started_perf,
                    diagnostic_code=exc.diagnostic_code,
                    attempt_count=2,
                    parse_repair_count=1,
                    raw_output_sha256=raw_sha,
                    usage=_usage(call_results),
                ),
                tuple(raw_attempts),
            )
        return _PairEvaluation(
            MethodOutcome(
                run_id=run_id,
                trace_id=material.trace_id,
                method_id=spec.method_id,
                status=MethodOutcomeStatus.VALID_JUDGMENT,
                method_input_sha256=input_sha,
                judgment=judgment,
                attempt_count=attempt_number,
                parse_repair_count=attempt_number - 1,
                raw_output_sha256=raw_sha,
                usage=_usage(call_results),
                started_at=started_at,
                ended_at=_now(),
                duration_seconds=_duration(started_perf),
            ),
            tuple(raw_attempts),
        )
    assert parse_error is not None
    raise AssertionError("unreachable phase-three parse state")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        path.chmod(0o600)
    except BaseException:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _append_record(
    path: Path,
    value: Any,
    *,
    public: bool,
    privacy_canaries: Sequence[str | bytes] = (),
) -> bytes:
    if public:
        assert_public_payload_safe(value, canaries=privacy_canaries)
    raw = canonical_json_bytes(value) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return raw


def _manifest_bytes(
    manifest: Phase3RunManifest,
    *,
    privacy_canaries: Sequence[str | bytes] = (),
) -> bytes:
    assert_public_payload_safe(manifest, canaries=privacy_canaries)
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


def _load_run_manifest(path: Path) -> Phase3RunManifest:
    payload, value = _read_manifest(path, label="phase-three run manifest")
    del payload
    try:
        return Phase3RunManifest.model_validate(value)
    except ValidationError:
        raise Phase3RunnerError(
            "phase-three run manifest failed validation", safe_stage="P3C_RESUME"
        ) from None


def _read_invocation_rows(path: Path, *, run_id: str) -> list[tuple[MethodOutcome, str]]:
    if not path.exists():
        return []
    if path.is_symlink() or not path.is_file():
        raise Phase3RunnerError("invocation results path is unsafe", safe_stage="P3C_RESUME")
    payload = path.read_bytes()
    if payload and not payload.endswith(b"\n"):
        raise Phase3RunnerError("invocation results are truncated", safe_stage="P3C_RESUME")
    rows: list[tuple[MethodOutcome, str]] = []
    for raw in payload.splitlines(keepends=True):
        try:
            outcome = MethodOutcome.model_validate_json(raw)
        except ValidationError:
            raise Phase3RunnerError(
                "invocation result row failed validation", safe_stage="P3C_RESUME"
            ) from None
        if outcome.run_id != run_id:
            raise Phase3RunnerError("invocation result run_id differs", safe_stage="P3C_RESUME")
        rows.append((outcome, jsonl_record_sha256(raw)))
    return rows


def _expected_pairs(cohort: LoadedPairedCohort) -> tuple[tuple[str, MethodId], ...]:
    return tuple(
        (trace_id, method_id) for trace_id in cohort.ordered_trace_ids for method_id in MethodId
    )


def _prior_results(
    *,
    run_dir: Path,
    manifest: Phase3RunManifest,
    expected_pairs: Sequence[tuple[str, MethodId]],
) -> dict[tuple[str, MethodId], tuple[MethodOutcome, str]]:
    latest: dict[tuple[str, MethodId], tuple[MethodOutcome, str]] = {}
    for invocation in manifest.invocations:
        rows = _read_invocation_rows(
            run_dir / "invocations" / invocation.invocation_id / "results.jsonl",
            run_id=manifest.run_id,
        )
        pairs = tuple((outcome.trace_id, outcome.method_id) for outcome, _sha in rows)
        if pairs != tuple(expected_pairs[: len(pairs)]):
            raise Phase3RunnerError(
                "invocation results are not a trace-major prefix", safe_stage="P3C_RESUME"
            )
        for outcome, row_sha in rows:
            pair = (outcome.trace_id, outcome.method_id)
            previous = latest.get(pair)
            if outcome.status == MethodOutcomeStatus.REUSED:
                if previous is None or outcome.reused_from_result_sha256 != previous[1]:
                    raise Phase3RunnerError(
                        "reused result does not reference the exact prior row",
                        safe_stage="P3C_RESUME",
                    )
            elif previous is not None:
                raise Phase3RunnerError(
                    "resume history re-executed an already terminal pair",
                    safe_stage="P3C_RESUME",
                )
            latest[pair] = (outcome, row_sha)
    completed_pairs = tuple(pair for pair in expected_pairs if pair in latest)
    if completed_pairs != tuple(expected_pairs[: len(completed_pairs)]):
        raise Phase3RunnerError(
            "resume history contains a non-prefix pair set", safe_stage="P3C_RESUME"
        )
    return latest


def _validate_resume_identity(
    *,
    identity: Phase3ResumeIdentity,
    cohort: LoadedPairedCohort,
    specs: Sequence[MethodSpec],
    bindings: Phase3ExecutionBindings,
) -> str:
    expected = {
        "frozen_manifest_sha256": cohort.overlay_manifest_sha256,
        "natural_manifest_sha256": cohort.natural_manifest_sha256,
        "ordered_trace_ids_sha256": canonical_sha256(cohort.ordered_trace_ids),
        "material_payloads_sha256": bindings.material_payloads_sha256,
        "method_specs_sha256": method_specs_sha256(specs),
        "prompt_bundle_sha256": prompt_bundle_sha256(),
        "output_schema_sha256": _OUTPUT_SCHEMA_SHA256,
        "implementation_sha256": implementation_sha256(),
        "provider_config_sha256": bindings.provider_config_sha256,
        "annotation_set_manifest_sha256": bindings.annotation_set_manifest_sha256,
        "completed_labels_sha256": bindings.completed_labels_sha256,
        "annotation_records_sha256": bindings.annotation_records_sha256,
        "ast_implementation_sha256": ast_implementation_sha256(),
        "public_evidence_policy_sha256": _PUBLIC_EVIDENCE_POLICY_SHA256,
    }
    actual = identity.model_dump(mode="json")
    if any(actual[key] != value for key, value in expected.items()):
        raise Phase3RunnerError(
            "resume identity differs from current paired interface", safe_stage="P3C_IDENTITY"
        )
    return canonical_sha256(identity)


async def run_paired_evaluation(
    *,
    run_id: str,
    cohort: LoadedPairedCohort,
    materials: Mapping[str, Phase3TraceMaterial],
    method_specs: Sequence[MethodSpec],
    provider: Phase3JudgeProvider,
    resume_identity: Phase3ResumeIdentity,
    execution_bindings: Phase3ExecutionBindings,
    output_dir: str | Path,
    resume: bool = False,
    privacy_canaries: Sequence[str | bytes] = (),
) -> Phase3RunResult:
    """Run or resume the complete trace-major product without automatic retries."""

    specs = tuple(method_specs)
    validate_method_specs(specs)
    validate_materials(cohort, materials, privacy_canaries=privacy_canaries)
    material_payloads_sha256 = canonical_sha256(
        [materials[trace_id].model_dump(mode="json") for trace_id in cohort.ordered_trace_ids]
    )
    if execution_bindings.material_payloads_sha256 != material_payloads_sha256:
        raise Phase3RunnerError(
            "execution material payload binding differs",
            safe_stage="P3E_EXECUTION_IDENTITY",
        )
    if execution_bindings.natural_manifest_sha256 != cohort.natural_manifest_sha256:
        raise Phase3RunnerError(
            "execution natural manifest binding differs",
            safe_stage="P3E_EXECUTION_IDENTITY",
        )
    if execution_bindings.provider_config_sha256 != provider_config_sha256(provider):
        raise Phase3RunnerError(
            "execution provider configuration binding differs",
            safe_stage="P3E_PROVIDER_IDENTITY",
        )
    llm_specs = tuple(spec for spec in specs if spec.uses_llm)
    if any(spec.provider != provider.name or spec.model != provider.model for spec in llm_specs):
        raise Phase3RunnerError(
            "judge provider differs from frozen method specs",
            safe_stage="P3E_PROVIDER_IDENTITY",
        )
    identity_sha = _validate_resume_identity(
        identity=resume_identity,
        cohort=cohort,
        specs=specs,
        bindings=execution_bindings,
    )
    expected_pairs = _expected_pairs(cohort)
    root = Path(output_dir)
    if not _RUN_ID_PATTERN.fullmatch(run_id):
        raise Phase3RunnerError(
            "run_id is not a safe directory identifier", safe_stage="P3C_OUTPUT"
        )
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise Phase3RunnerError("phase-three output root is unsafe", safe_stage="P3C_OUTPUT")
    run_dir = root / run_id
    manifest_path = run_dir / "manifest.json"
    now = _now()

    if resume:
        if not run_dir.is_dir() or run_dir.is_symlink():
            raise Phase3RunnerError(
                "resume run directory is missing or unsafe", safe_stage="P3C_RESUME"
            )
        manifest = _load_run_manifest(manifest_path)
        if manifest.run_id != run_id or manifest.status != "running":
            raise Phase3RunnerError(
                "only an incomplete matching run may be resumed", safe_stage="P3C_RESUME"
            )
        if (
            manifest.resume_identity_sha256 != identity_sha
            or manifest.resume_identity != resume_identity
        ):
            raise Phase3RunnerError("resume identity changed", safe_stage="P3C_RESUME")
        previous = list(manifest.invocations)
        final = previous[-1]
        if final.status == "running":
            previous[-1] = Phase3Invocation(
                invocation_id=final.invocation_id,
                resume=final.resume,
                status="interrupted",
                resume_identity_sha256=final.resume_identity_sha256,
                started_at=final.started_at,
                ended_at=now,
            )
        prior = _prior_results(
            run_dir=run_dir,
            manifest=manifest,
            expected_pairs=expected_pairs,
        )
        invocation_id = f"invocation_{len(previous) + 1:03d}_{uuid.uuid4().hex[:12]}"
        invocation = Phase3Invocation(
            invocation_id=invocation_id,
            resume=True,
            status="running",
            resume_identity_sha256=identity_sha,
            started_at=now,
        )
        manifest = Phase3RunManifest(
            run_id=run_id,
            status="running",
            created_at=manifest.created_at,
            frozen_manifest_sha256=cohort.overlay_manifest_sha256,
            resume_identity=resume_identity,
            resume_identity_sha256=identity_sha,
            invocations=(*previous, invocation),
        )
    else:
        if run_dir.exists():
            raise Phase3RunnerError(
                "phase-three run directory already exists", safe_stage="P3C_OUTPUT"
            )
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        run_dir.mkdir(mode=0o700)
        prior = {}
        invocation_id = f"invocation_001_{uuid.uuid4().hex[:12]}"
        invocation = Phase3Invocation(
            invocation_id=invocation_id,
            resume=False,
            status="running",
            resume_identity_sha256=identity_sha,
            started_at=now,
        )
        manifest = Phase3RunManifest(
            run_id=run_id,
            status="running",
            created_at=now,
            frozen_manifest_sha256=cohort.overlay_manifest_sha256,
            resume_identity=resume_identity,
            resume_identity_sha256=identity_sha,
            invocations=(invocation,),
        )

    invocation_dir = run_dir / "invocations" / invocation_id
    invocation_dir.mkdir(parents=True, mode=0o700)
    invocation_dir.chmod(0o700)
    results_path = invocation_dir / "results.jsonl"
    raw_path = invocation_dir / "provider_raw.jsonl"
    _atomic_write(
        manifest_path,
        _manifest_bytes(manifest, privacy_canaries=privacy_canaries),
    )

    current_rows: list[tuple[MethodOutcome, str]] = []
    for trace_id, method_id in expected_pairs:
        prior_item = prior.get((trace_id, method_id))
        if prior_item is not None:
            previous_outcome, previous_sha = prior_item
            timestamp = _now()
            outcome = MethodOutcome(
                run_id=run_id,
                trace_id=trace_id,
                method_id=method_id,
                status=MethodOutcomeStatus.REUSED,
                method_input_sha256=previous_outcome.method_input_sha256,
                attempt_count=0,
                parse_repair_count=0,
                usage=MethodUsage(cost_status="not_applicable"),
                reused_from_result_sha256=previous_sha,
                started_at=timestamp,
                ended_at=timestamp,
                duration_seconds=0.0,
            )
            raw = _append_record(
                results_path,
                outcome.model_dump(mode="json"),
                public=True,
                privacy_canaries=privacy_canaries,
            )
            current_rows.append((outcome, jsonl_record_sha256(raw)))
            continue

        spec = specs[list(MethodId).index(method_id)]
        evaluated = await evaluate_method(
            run_id=run_id,
            spec=spec,
            material=materials[trace_id],
            provider=provider,
        )
        for raw_attempt in evaluated.raw_attempts:
            _append_record(
                raw_path,
                {
                    "trace_id": raw_attempt.trace_id,
                    "method_id": raw_attempt.method_id.value,
                    "attempt_number": raw_attempt.attempt_number,
                    "raw_output": raw_attempt.raw_text,
                },
                public=False,
            )
        raw = _append_record(
            results_path,
            evaluated.outcome.model_dump(mode="json"),
            public=True,
            privacy_canaries=privacy_canaries,
        )
        current_rows.append((evaluated.outcome, jsonl_record_sha256(raw)))

    if len(current_rows) != len(expected_pairs):
        raise Phase3RunnerError("paired invocation did not produce the full product")
    final_results_path = run_dir / "results.jsonl"
    shutil.copyfile(results_path, final_results_path)
    final_results_path.chmod(0o600)
    results_payload = final_results_path.read_bytes()
    results_sha = hashlib.sha256(results_payload).hexdigest()
    references = tuple(
        PairedMethodResultReference(
            trace_id=outcome.trace_id,
            method_id=outcome.method_id,
            status=outcome.status,
            result_line_number=line_number,
            result_record_sha256=row_sha,
        )
        for line_number, (outcome, row_sha) in enumerate(current_rows, start=1)
    )
    index = PairedEvaluationIndex(
        run_id=run_id,
        frozen_manifest_sha256=cohort.overlay_manifest_sha256,
        resume_identity_sha256=identity_sha,
        ordered_trace_ids=cohort.ordered_trace_ids,
        ordered_method_ids=tuple(MethodId),
        result_references=references,
        results_sha256=results_sha,
    )
    assert_public_payload_safe(index, canaries=privacy_canaries)
    index_path = run_dir / "index.json"
    index_payload = (
        json.dumps(
            index.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    _atomic_write(index_path, index_payload)

    completed_at = _now()
    completed_invocation = Phase3Invocation(
        invocation_id=invocation.invocation_id,
        resume=invocation.resume,
        status="completed",
        resume_identity_sha256=identity_sha,
        started_at=invocation.started_at,
        ended_at=completed_at,
    )
    completed_manifest = Phase3RunManifest(
        run_id=run_id,
        status="completed",
        created_at=manifest.created_at,
        completed_at=completed_at,
        frozen_manifest_sha256=cohort.overlay_manifest_sha256,
        resume_identity=resume_identity,
        resume_identity_sha256=identity_sha,
        invocations=(*manifest.invocations[:-1], completed_invocation),
    )
    _atomic_write(
        manifest_path,
        _manifest_bytes(completed_manifest, privacy_canaries=privacy_canaries),
    )

    status_counts = {
        status: sum(outcome.status == status for outcome, _sha in current_rows)
        for status in MethodOutcomeStatus
    }
    return Phase3RunResult(
        run_id=run_id,
        run_dir=run_dir,
        manifest_path=manifest_path,
        results_path=final_results_path,
        index_path=index_path,
        result_count=len(current_rows),
        reused_count=status_counts[MethodOutcomeStatus.REUSED],
        status_counts=status_counts,
        results_sha256=results_sha,
        index_sha256=hashlib.sha256(index_payload).hexdigest(),
    )
