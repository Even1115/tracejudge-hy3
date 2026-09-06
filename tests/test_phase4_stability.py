from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from tracejudge_hy3.phase3.contracts import MethodId
from tracejudge_hy3.phase3.runner import (
    Phase3ProviderCallError,
    ProviderCallResult,
)
from tracejudge_hy3.phase4.stability import (
    Phase4StabilityError,
    StabilityProtocol,
    StabilityReport,
    StabilityRunManifest,
    execute_hy3_judge_stability,
    preflight_judge_stability,
    run_judge_stability,
)

REPO_ROOT = Path(__file__).parents[1]
SOURCE_BUNDLE = REPO_ROOT / "data/phase3/public_counterfactuals_v1.json"
EXECUTION_RUN = REPO_ROOT / "artifacts/experiments/phase3-public-evidence/phase3_cf_public_15_v1"


class ScriptedStabilityProvider:
    name = "mock"
    model = "phase4-stability-scripted-v1"

    def __init__(self, responses: Sequence[ProviderCallResult | BaseException]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def public_configuration(self):
        return {
            "provider": self.name,
            "model": self.model,
            "transport": "offline-scripted-stability-v1",
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


class SimulatedInterruption(BaseException):
    pass


def _raw_judgment(case_id: str) -> ProviderCallResult:
    if case_id == "reasoning_swap":
        value = {
            "functional_correct": True,
            "has_error": True,
            "reasoning_correct": False,
            "plan_code_aligned": False,
            "process_correct": False,
            "first_faulty_layer": "requirement",
            "first_faulty_step": "S1",
            "violated_requirement": "R1",
            "code_span": "solution trace",
            "error_type": "R01_REQUIREMENT_MISREAD",
            "verdict": "strongly_supported",
            "evidence_summary": ["The explanation contradicts the public requirement."],
        }
    elif case_id == "boundary_error":
        value = {
            "functional_correct": False,
            "has_error": True,
            "reasoning_correct": True,
            "plan_code_aligned": False,
            "process_correct": False,
            "first_faulty_layer": "alignment",
            "first_faulty_step": "S1",
            "violated_requirement": "R1",
            "code_span": "candidate function",
            "error_type": "A01_PLAN_CODE_MISMATCH",
            "verdict": "confirmed_bug",
            "evidence_summary": ["The public empty-list case raises instead of returning 0.0."],
        }
    else:
        value = {
            "functional_correct": True,
            "has_error": False,
            "reasoning_correct": True,
            "plan_code_aligned": True,
            "process_correct": True,
            "evidence_summary": [],
        }
    return ProviderCallResult(raw_text=json.dumps(value))


def _stable_responses() -> list[ProviderCallResult]:
    case_order = (
        "normal_correct",
        "reasoning_swap",
        "boundary_error",
        "equivalent_implementation",
    )
    return [_raw_judgment(case_id) for _round in range(5) for case_id in case_order]


def _preflight_kwargs(tmp_path: Path) -> dict[str, object]:
    return {
        "run_id": "stability_unit_v1",
        "provider_configuration": {
            "provider": "mock",
            "model": "phase4-stability-scripted-v1",
            "transport": "offline-scripted-stability-v1",
        },
        "source_bundle_path": SOURCE_BUNDLE,
        "execution_run_dir": EXECUTION_RUN,
        "output_dir": tmp_path / "runs",
        "repo_root": REPO_ROOT,
        "temperature": 0.0,
        "timeout_seconds": 5.0,
        "resume": False,
        "allow_dirty": True,
        "privacy_canaries": (),
    }


def test_stability_preflight_freezes_public_four_by_five_without_writes(tmp_path: Path):
    result = preflight_judge_stability(**_preflight_kwargs(tmp_path))

    assert result.case_count == 4
    assert result.repetition_count == 5
    assert result.scheduled_evaluation_count == 20
    assert result.nominal_provider_call_count == 20
    assert result.maximum_provider_call_count == 40
    assert len(result.protocol_sha256) == 64
    assert not (tmp_path / "runs").exists()


@pytest.mark.asyncio
async def test_stability_run_writes_twenty_independent_trials_and_report(tmp_path: Path):
    provider = ScriptedStabilityProvider(_stable_responses())
    result = await run_judge_stability(
        run_id="stability_complete_v1",
        provider=provider,
        source_bundle_path=SOURCE_BUNDLE,
        execution_run_dir=EXECUTION_RUN,
        output_dir=tmp_path / "runs",
        repo_root=REPO_ROOT,
        temperature=0.0,
        timeout_seconds=5.0,
        allow_dirty=True,
    )

    assert result.valid_judgment_count == 20
    assert result.provider_failure_count == 0
    assert result.parse_failure_count == 0
    assert result.observed_provider_call_count == 20
    assert len(provider.calls) == 20
    assert len(list((result.run_dir / "trials").glob("trial_*.json"))) == 20
    assert len(result.results_path.read_text(encoding="utf-8").splitlines()) == 20

    protocol = StabilityProtocol.model_validate_json(result.protocol_path.read_bytes())
    manifest = StabilityRunManifest.model_validate_json(result.manifest_path.read_bytes())
    report = StabilityReport.model_validate_json(result.report_json_path.read_bytes())
    assert protocol.main_experiment_merge_allowed is False
    assert protocol.provider_configuration == provider.public_configuration()
    assert manifest.status == "completed"
    assert manifest.completed_evaluation_count == 20
    assert report.all_twenty_valid is True
    assert all(item.pairwise_agreement == 1.0 for item in report.overall_fields)
    assert all(item.all_five_consistent_case_count == 4 for item in report.overall_fields)
    assert "不得并入冻结的 57×5 主实验" in result.report_markdown_path.read_text(encoding="utf-8")
    assert result.run_dir.stat().st_mode & 0o077 == 0
    assert result.manifest_path.stat().st_mode & 0o077 == 0


@pytest.mark.asyncio
async def test_stability_report_keeps_provider_and_parse_failures_explicit(tmp_path: Path):
    responses: list[ProviderCallResult | BaseException] = [
        Phase3ProviderCallError("provider_timeout"),
        ProviderCallResult(raw_text="not-json"),
        ProviderCallResult(raw_text="still-not-json"),
        *_stable_responses()[2:],
    ]
    provider = ScriptedStabilityProvider(responses)
    result = await run_judge_stability(
        run_id="stability_failures_v1",
        provider=provider,
        source_bundle_path=SOURCE_BUNDLE,
        execution_run_dir=EXECUTION_RUN,
        output_dir=tmp_path / "runs",
        repo_root=REPO_ROOT,
        temperature=0.0,
        timeout_seconds=5.0,
        allow_dirty=True,
    )
    report = StabilityReport.model_validate_json(result.report_json_path.read_bytes())
    manifest = StabilityRunManifest.model_validate_json(result.manifest_path.read_bytes())

    assert report.valid_judgment_count == 18
    assert report.provider_failure_count == 1
    assert report.parse_failure_count == 1
    assert report.parse_repair_trial_count == 1
    assert report.observed_provider_call_count == 21
    assert manifest.status == "completed_with_failures"
    assert len(provider.calls) == 21
    assert sum(item.valid_judgment_count for item in report.cases) == 18


@pytest.mark.asyncio
async def test_stability_resume_continues_after_last_atomic_trial(tmp_path: Path):
    first_provider = ScriptedStabilityProvider([_stable_responses()[0], SimulatedInterruption()])
    with pytest.raises(SimulatedInterruption):
        await run_judge_stability(
            run_id="stability_resume_v1",
            provider=first_provider,
            source_bundle_path=SOURCE_BUNDLE,
            execution_run_dir=EXECUTION_RUN,
            output_dir=tmp_path / "runs",
            repo_root=REPO_ROOT,
            temperature=0.0,
            timeout_seconds=5.0,
            allow_dirty=True,
        )

    run_dir = tmp_path / "runs/stability_resume_v1"
    interrupted = StabilityRunManifest.model_validate_json((run_dir / "manifest.json").read_bytes())
    assert interrupted.status == "running"
    assert interrupted.completed_evaluation_count == 1
    assert len(list((run_dir / "trials").glob("trial_*.json"))) == 1

    resume_provider = ScriptedStabilityProvider(_stable_responses()[1:])
    result = await run_judge_stability(
        run_id="stability_resume_v1",
        provider=resume_provider,
        source_bundle_path=SOURCE_BUNDLE,
        execution_run_dir=EXECUTION_RUN,
        output_dir=tmp_path / "runs",
        repo_root=REPO_ROOT,
        temperature=0.0,
        timeout_seconds=5.0,
        resume=True,
        allow_dirty=True,
    )

    report = StabilityReport.model_validate_json(result.report_json_path.read_bytes())
    assert report.valid_judgment_count == 20
    assert len(first_provider.calls) == 2
    assert len(resume_provider.calls) == 19
    assert len(list((run_dir / "trials").glob("trial_*.json"))) == 20


@pytest.mark.asyncio
async def test_real_stability_execution_requires_explicit_confirmation(tmp_path: Path):
    with pytest.raises(Phase4StabilityError) as exc_info:
        await execute_hy3_judge_stability(
            confirm_real_provider=False,
            run_id="stability_real_refused_v1",
            source_bundle_path=SOURCE_BUNDLE,
            execution_run_dir=EXECUTION_RUN,
            output_dir=tmp_path / "runs",
        )

    assert exc_info.value.safe_stage == "P4_STABILITY_REAL_PROVIDER_CONFIRMATION"
    assert not (tmp_path / "runs").exists()
