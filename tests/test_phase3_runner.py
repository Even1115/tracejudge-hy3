from __future__ import annotations

import asyncio
import hashlib
import json
import stat
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

import tracejudge_hy3.cli as cli_module
from tracejudge_hy3.cli import app
from tracejudge_hy3.phase3.contracts import (
    MethodId,
    MethodJudgment,
    MethodOutcome,
    MethodOutcomeStatus,
    NaturalTrace,
    Phase1ResponseReference,
    Phase2FunctionalEvidenceRef,
    Phase3ResumeIdentity,
    Phase3RunManifest,
)
from tracejudge_hy3.phase3.privacy import PublicPayloadError, canonical_sha256
from tracejudge_hy3.phase3.runner import (
    LoadedPairedCohort,
    Phase3ExecutionBindings,
    Phase3InterfacePreflight,
    Phase3ProviderCallError,
    Phase3RunnerError,
    Phase3TraceMaterial,
    ProviderCallResult,
    PublicDynamicEvidenceInput,
    ast_implementation_sha256,
    build_method_specs,
    evaluate_method,
    implementation_sha256,
    method_specs_sha256,
    output_schema_sha256,
    project_method_input,
    provider_config_sha256,
    public_evidence_policy_sha256,
    run_paired_evaluation,
    validate_materials,
)
from tracejudge_hy3.prompts.phase3 import prompt_bundle_sha256
from tracejudge_hy3.schemas.solution import ImplementationStep, SolutionTrace

H0 = "0" * 64
H1 = "1" * 64
H2 = "2" * 64
H3 = "3" * 64
H4 = "4" * 64


class ScriptedProvider:
    name = "mock"
    model = "phase3-scripted-mock-v1"

    def __init__(self, responses: Sequence[ProviderCallResult | BaseException]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def public_configuration(self):
        return {
            "provider": self.name,
            "model": self.model,
            "transport": "phase3-scripted-mock-v1",
        }

    async def complete(
        self,
        *,
        method_id: MethodId,
        messages: tuple[dict[str, str], ...],
        temperature: float,
        timeout_seconds: float,
    ) -> ProviderCallResult:
        self.calls.append(
            {
                "method_id": method_id,
                "messages": messages,
                "temperature": temperature,
                "timeout_seconds": timeout_seconds,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _fixture() -> tuple[LoadedPairedCohort, Phase3TraceMaterial]:
    public_problem = {
        "problem_id": "public_fixture_1",
        "requirement": "Return the input value.",
        "function_signature": "def identity(value: int) -> int:",
        "requirements": [{"requirement_id": "R1", "content": "Return the input."}],
        "visible_test_cases": [{"args": [1], "kwargs": {}, "expected": 1}],
    }
    solution = SolutionTrace(
        problem_id="public_fixture_1",
        requirement_understanding="Return the input value.",
        design_summary="Use the identity operation.",
        edge_cases_considered=["integer input"],
        implementation_steps=[
            ImplementationStep(
                step_id="S1",
                content="Return the provided value.",
                related_requirements=["R1"],
            )
        ],
        declared_time_complexity="O(1)",
        declared_space_complexity="O(1)",
        code="def identity(value: int) -> int:\n    return value\n",
    )
    evidence = Phase2FunctionalEvidenceRef(
        phase2_run_id="phase2_fixture",
        problem_id=solution.problem_id,
        result_line_number=1,
        result_record_sha256=H2,
        functional_evidence_sha256=H2,
        code_sha256=hashlib.sha256(solution.code.encode()).hexdigest(),
        base_status="pass",
        plus_status="pass",
        passed_base=True,
        passed_plus=True,
    )
    dynamic_payload = {
        "policy": "public_dynamic_fixture_v1",
        "status": "no_counterexample_found_within_frozen_budget",
        "evidence": [],
    }
    dynamic = PublicDynamicEvidenceInput(
        status="available",
        evidence_sha256=canonical_sha256(dynamic_payload),
        payload=dynamic_payload,
    )
    material = Phase3TraceMaterial(
        trace_id="natural:public_fixture_1",
        public_problem=public_problem,
        solution_trace=solution,
        functional_evidence=evidence,
        public_dynamic_evidence=dynamic,
    )
    solution_payload = solution.model_dump(mode="json")
    explanation = {key: value for key, value in solution_payload.items() if key != "code"}
    trace = NaturalTrace(
        trace_id=material.trace_id,
        problem_id=solution.problem_id,
        public_problem_sha256=canonical_sha256(public_problem),
        solution_trace_sha256=canonical_sha256(solution_payload),
        structured_explanation_sha256=canonical_sha256(explanation),
        code_sha256=evidence.code_sha256,
        functional_evidence=evidence,
        phase1_response=Phase1ResponseReference(
            phase1_run_id="phase1_fixture",
            problem_id=solution.problem_id,
            invocation_id="invocation_1",
            response_line_number=1,
            response_record_sha256=H1,
            code_sha256=evidence.code_sha256,
        ),
    )
    cohort = LoadedPairedCohort(
        overlay_manifest_sha256=H0,
        natural_manifest_sha256=H1,
        ordered_trace_ids=(trace.trace_id,),
        traces_by_id={trace.trace_id: trace},
        natural_trace_count=1,
        counterfactual_trace_count=0,
    )
    return cohort, material


def _specs():
    return build_method_specs(
        provider="mock",
        model="phase3-scripted-mock-v1",
        temperature=0.0,
        timeout_seconds=5.0,
    )


def _bindings(
    cohort: LoadedPairedCohort,
    material: Phase3TraceMaterial,
    provider: ScriptedProvider,
) -> Phase3ExecutionBindings:
    return Phase3ExecutionBindings(
        natural_manifest_sha256=cohort.natural_manifest_sha256,
        material_payloads_sha256=canonical_sha256([material.model_dump(mode="json")]),
        provider_config_sha256=provider_config_sha256(provider),
        annotation_set_manifest_sha256=H4,
        completed_labels_sha256=H0,
        annotation_records_sha256=H1,
    )


def _identity(
    cohort: LoadedPairedCohort,
    bindings: Phase3ExecutionBindings,
) -> Phase3ResumeIdentity:
    specs = _specs()
    return Phase3ResumeIdentity(
        frozen_manifest_sha256=cohort.overlay_manifest_sha256,
        natural_manifest_sha256=cohort.natural_manifest_sha256,
        ordered_trace_ids_sha256=canonical_sha256(cohort.ordered_trace_ids),
        material_payloads_sha256=bindings.material_payloads_sha256,
        method_specs_sha256=method_specs_sha256(specs),
        prompt_bundle_sha256=prompt_bundle_sha256(),
        output_schema_sha256=output_schema_sha256(),
        implementation_sha256=implementation_sha256(),
        provider_config_sha256=bindings.provider_config_sha256,
        annotation_set_manifest_sha256=bindings.annotation_set_manifest_sha256,
        completed_labels_sha256=bindings.completed_labels_sha256,
        annotation_records_sha256=bindings.annotation_records_sha256,
        git_commit="a" * 40,
        git_branch="phase3-process-evaluation",
        git_dirty=False,
        python_version="3.11.test",
        direct_dependencies_sha256=H3,
        ast_implementation_sha256=ast_implementation_sha256(),
        public_evidence_policy_sha256=public_evidence_policy_sha256(),
        annotation_protocol_sha256=H4,
        random_seed=20260828,
    )


def _clean_response(*, cost: int | None = 100) -> ProviderCallResult:
    return ProviderCallResult(
        raw_text=MethodJudgment(functional_correct=True, has_error=False).model_dump_json(),
        prompt_tokens=10,
        completion_tokens=5,
        reported_cost_microusd=cost,
    )


def test_method_projection_matches_each_frozen_visibility_policy():
    _cohort, material = _fixture()
    for spec in _specs():
        payload = project_method_input(spec=spec, material=material)
        assert set(payload) == {item.value for item in spec.visible_inputs}
        if spec.method_id == MethodId.TEST_ONLY:
            assert "candidate_code" not in payload
        if spec.method_id == MethodId.DIRECT_LLM_JUDGE:
            assert "ast_evidence" not in payload
            assert "public_dynamic_evidence" not in payload
        if spec.method_id == MethodId.FULL_TRACEJUDGE:
            assert "ast_evidence" in payload
            assert "public_dynamic_evidence" in payload


def test_material_binding_fails_closed_on_any_candidate_byte_change():
    cohort, material = _fixture()
    changed = material.model_copy(
        update={
            "solution_trace": material.solution_trace.model_copy(
                update={"code": material.solution_trace.code + "\n"}
            )
        }
    )
    with pytest.raises(Phase3RunnerError, match="hash differs"):
        validate_materials(cohort, {material.trace_id: changed})


def test_public_dynamic_evidence_requires_exact_payload_hash():
    with pytest.raises(ValidationError, match="hash is inconsistent"):
        PublicDynamicEvidenceInput(
            status="available",
            evidence_sha256=H0,
            payload={"status": "available"},
        )


async def test_strict_parse_repair_is_the_only_second_provider_call():
    _cohort, material = _fixture()
    provider = ScriptedProvider(
        [
            ProviderCallResult(raw_text="```json\n{}\n```", reported_cost_microusd=10),
            _clean_response(cost=25),
        ]
    )
    spec = _specs()[1]

    evaluated = await evaluate_method(
        run_id="phase3_mock_run",
        spec=spec,
        material=material,
        provider=provider,
    )

    assert evaluated.outcome.status == MethodOutcomeStatus.VALID_JUDGMENT
    assert evaluated.outcome.attempt_count == 2
    assert evaluated.outcome.parse_repair_count == 1
    assert evaluated.outcome.usage.reported_cost_microusd == 35
    assert len(provider.calls) == 2
    second_messages = provider.calls[1]["messages"]
    assert isinstance(second_messages, tuple)
    assert len(second_messages) == 3
    assert [item["role"] for item in second_messages] == ["system", "user", "user"]
    assert "严格 JSON/Schema" in second_messages[-1]["content"]


async def test_provider_and_parse_failures_remain_distinct_without_retry():
    _cohort, material = _fixture()
    provider_failure = ScriptedProvider([Phase3ProviderCallError("provider_unavailable")])
    failed = await evaluate_method(
        run_id="phase3_mock_run",
        spec=_specs()[1],
        material=material,
        provider=provider_failure,
    )
    assert failed.outcome.status == MethodOutcomeStatus.PROVIDER_ERROR
    assert failed.outcome.diagnostic_code == "provider_unavailable"
    assert len(provider_failure.calls) == 1

    parse_failure = ScriptedProvider(
        [ProviderCallResult(raw_text="not-json"), ProviderCallResult(raw_text="still-not-json")]
    )
    unparsed = await evaluate_method(
        run_id="phase3_mock_run",
        spec=_specs()[2],
        material=material,
        provider=parse_failure,
    )
    assert unparsed.outcome.status == MethodOutcomeStatus.PARSE_ERROR
    assert unparsed.outcome.attempt_count == 2
    assert unparsed.outcome.parse_repair_count == 1
    assert len(parse_failure.calls) == 2


async def test_ast_error_and_public_timeout_do_not_call_provider():
    _cohort, material = _fixture()
    invalid_material = material.model_copy(
        update={
            "solution_trace": material.solution_trace.model_copy(
                update={"code": "def identity(:\n"}
            )
        }
    )
    provider = ScriptedProvider([])
    ast_result = await evaluate_method(
        run_id="phase3_mock_run",
        spec=_specs()[3],
        material=invalid_material,
        provider=provider,
    )
    assert ast_result.outcome.status == MethodOutcomeStatus.AST_ERROR

    timeout_material = material.model_copy(
        update={
            "public_dynamic_evidence": PublicDynamicEvidenceInput(
                status="timeout",
                evidence_sha256=H2,
            )
        }
    )
    timeout_result = await evaluate_method(
        run_id="phase3_mock_run",
        spec=_specs()[4],
        material=timeout_material,
        provider=provider,
    )
    assert timeout_result.outcome.status == MethodOutcomeStatus.PUBLIC_EXECUTION_TIMEOUT
    assert provider.calls == []


async def test_complete_mock_run_writes_all_five_pairs_and_private_raw(tmp_path: Path):
    cohort, material = _fixture()
    raw_marker = "PRIVATE-\\u004dOCK-RAW-CANARY"
    raw_judgment = MethodJudgment(
        functional_correct=True,
        has_error=False,
        evidence_summary=("PRIVATE-MOCK-RAW-CANARY",),
    ).model_dump_json()
    responses = [
        ProviderCallResult(
            raw_text=raw_judgment.replace("PRIVATE-MOCK", "PRIVATE-\\u004dOCK"),
            reported_cost_microusd=1,
        )
    ]
    # The private raw file must preserve the provider's exact escaped bytes;
    # public results contain only the parsed judgment, never that raw encoding.
    responses.extend(_clean_response(cost=1) for _ in range(3))
    provider = ScriptedProvider(responses)
    bindings = _bindings(cohort, material, provider)

    result = await run_paired_evaluation(
        run_id="phase3_mock_complete",
        cohort=cohort,
        materials={material.trace_id: material},
        method_specs=_specs(),
        provider=provider,
        resume_identity=_identity(cohort, bindings),
        execution_bindings=bindings,
        output_dir=tmp_path,
    )

    assert result.result_count == 5
    assert result.status_counts[MethodOutcomeStatus.VALID_JUDGMENT] == 5
    assert len(result.results_path.read_bytes().splitlines()) == 5
    assert raw_marker not in result.results_path.read_text(encoding="utf-8")
    raw_path = next((result.run_dir / "invocations").glob("*/provider_raw.jsonl"))
    raw_rows = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines()]
    assert raw_marker in raw_rows[0]["raw_output"]
    assert stat.S_IMODE(result.run_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(result.results_path.stat().st_mode) == 0o600


async def test_run_id_cannot_escape_the_declared_output_root(tmp_path: Path):
    cohort, material = _fixture()
    provider = ScriptedProvider([_clean_response() for _ in range(4)])
    bindings = _bindings(cohort, material, provider)

    with pytest.raises(Phase3RunnerError, match="safe directory"):
        await run_paired_evaluation(
            run_id="../outside",
            cohort=cohort,
            materials={material.trace_id: material},
            method_specs=_specs(),
            provider=provider,
            resume_identity=_identity(cohort, bindings),
            execution_bindings=bindings,
            output_dir=tmp_path,
        )

    assert provider.calls == []
    assert not (tmp_path.parent / "outside").exists()


async def test_interruption_then_resume_reuses_exact_rows_without_retry(tmp_path: Path):
    cohort, material = _fixture()
    interrupted_provider = ScriptedProvider([asyncio.CancelledError()])
    bindings = _bindings(cohort, material, interrupted_provider)
    with pytest.raises(asyncio.CancelledError):
        await run_paired_evaluation(
            run_id="phase3_mock_resume",
            cohort=cohort,
            materials={material.trace_id: material},
            method_specs=_specs(),
            provider=interrupted_provider,
            resume_identity=_identity(cohort, bindings),
            execution_bindings=bindings,
            output_dir=tmp_path,
        )

    run_dir = tmp_path / "phase3_mock_resume"
    partial = next((run_dir / "invocations").glob("*/results.jsonl"))
    partial_line = partial.read_bytes()
    assert len(partial_line.splitlines()) == 1
    prior_sha = hashlib.sha256(partial_line).hexdigest()
    running = Phase3RunManifest.model_validate_json((run_dir / "manifest.json").read_bytes())
    assert running.status == "running"

    provider = ScriptedProvider([_clean_response(cost=1) for _ in range(4)])
    resumed_bindings = _bindings(cohort, material, provider)
    result = await run_paired_evaluation(
        run_id="phase3_mock_resume",
        cohort=cohort,
        materials={material.trace_id: material},
        method_specs=_specs(),
        provider=provider,
        resume_identity=_identity(cohort, resumed_bindings),
        execution_bindings=resumed_bindings,
        output_dir=tmp_path,
        resume=True,
    )

    rows = [
        MethodOutcome.model_validate_json(line)
        for line in result.results_path.read_bytes().splitlines()
    ]
    assert rows[0].status == MethodOutcomeStatus.REUSED
    assert rows[0].reused_from_result_sha256 == prior_sha
    assert result.reused_count == 1
    assert len(provider.calls) == 4
    manifest = Phase3RunManifest.model_validate_json(result.manifest_path.read_bytes())
    assert [item.status.value for item in manifest.invocations] == ["interrupted", "completed"]


async def test_public_writer_rejects_provider_canary_but_keeps_private_raw(tmp_path: Path):
    cohort, material = _fixture()
    canary = "PHASE3-PROVIDER-PRIVATE-CANARY"
    provider = ScriptedProvider(
        [
            ProviderCallResult(
                raw_text=MethodJudgment(
                    functional_correct=True,
                    has_error=False,
                    evidence_summary=(canary,),
                ).model_dump_json()
            )
        ]
    )
    bindings = _bindings(cohort, material, provider)

    with pytest.raises(PublicPayloadError) as exc_info:
        await run_paired_evaluation(
            run_id="phase3_mock_canary",
            cohort=cohort,
            materials={material.trace_id: material},
            method_specs=_specs(),
            provider=provider,
            resume_identity=_identity(cohort, bindings),
            execution_bindings=bindings,
            output_dir=tmp_path,
            privacy_canaries=(canary,),
        )

    assert canary not in str(exc_info.value)
    run_dir = tmp_path / "phase3_mock_canary"
    raw_path = next((run_dir / "invocations").glob("*/provider_raw.jsonl"))
    assert canary in raw_path.read_text(encoding="utf-8")
    public_path = next((run_dir / "invocations").glob("*/results.jsonl"))
    assert canary not in public_path.read_text(encoding="utf-8")


async def test_resume_identity_change_is_rejected_before_provider_call(tmp_path: Path):
    cohort, material = _fixture()
    interrupted_provider = ScriptedProvider([asyncio.CancelledError()])
    bindings = _bindings(cohort, material, interrupted_provider)
    with pytest.raises(asyncio.CancelledError):
        await run_paired_evaluation(
            run_id="phase3_mock_identity",
            cohort=cohort,
            materials={material.trace_id: material},
            method_specs=_specs(),
            provider=interrupted_provider,
            resume_identity=_identity(cohort, bindings),
            execution_bindings=bindings,
            output_dir=tmp_path,
        )

    provider = ScriptedProvider([_clean_response() for _ in range(4)])
    resumed_bindings = _bindings(cohort, material, provider)
    changed = _identity(cohort, resumed_bindings).model_copy(update={"random_seed": 20260829})
    with pytest.raises(Phase3RunnerError, match="resume identity changed"):
        await run_paired_evaluation(
            run_id="phase3_mock_identity",
            cohort=cohort,
            materials={material.trace_id: material},
            method_specs=_specs(),
            provider=provider,
            resume_identity=changed,
            execution_bindings=resumed_bindings,
            output_dir=tmp_path,
            resume=True,
        )
    assert provider.calls == []


def test_cli_paired_preflight_is_read_only(monkeypatch: pytest.MonkeyPatch):
    expected = Phase3InterfacePreflight(
        freeze_id="phase3_cohort_42_plus_15_v1",
        natural_trace_count=42,
        counterfactual_trace_count=15,
        trace_count=57,
        method_count=5,
        pair_count=285,
        method_specs_sha256=H0,
        prompt_bundle_sha256=H1,
        output_schema_sha256=H2,
        provider="mock",
        model="phase3-scripted-mock-v1",
    )
    calls: list[dict[str, object]] = []

    def fake_preflight(**kwargs):
        calls.append(kwargs)
        return expected

    monkeypatch.setattr(cli_module, "preflight_paired_interface", fake_preflight)
    result = CliRunner().invoke(
        app,
        [
            "phase3",
            "paired-preflight",
            "--cohort-manifest",
            "overlay.json",
            "--natural-manifest",
            "natural.json",
            "--provider",
            "mock",
            "--model",
            "phase3-scripted-mock-v1",
        ],
    )

    assert result.exit_code == 0
    assert "57 / 5 / 285" in result.stdout
    assert "否 / 否 / 否 / 否" in result.stdout
    assert len(calls) == 1


def test_cli_paired_preflight_reports_only_safe_stage(monkeypatch: pytest.MonkeyPatch):
    secret = "MUST-NOT-BE-PRINTED"

    def fail(**_kwargs):
        raise Phase3RunnerError(secret, safe_stage="P3C_COHORT")

    monkeypatch.setattr(cli_module, "preflight_paired_interface", fail)
    result = CliRunner().invoke(
        app,
        [
            "phase3",
            "paired-preflight",
            "--cohort-manifest",
            "overlay.json",
            "--natural-manifest",
            "natural.json",
        ],
    )

    assert result.exit_code == 1
    assert "P3C_COHORT" in result.stdout
    assert secret not in result.stdout
