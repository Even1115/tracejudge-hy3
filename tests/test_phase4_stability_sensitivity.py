from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

import pytest

from tracejudge_hy3.phase3.contracts import (
    MethodId,
    MethodJudgment,
    MethodOutcome,
    MethodOutcomeStatus,
)
from tracejudge_hy3.phase3.privacy import canonical_sha256
from tracejudge_hy3.phase4.stability import (
    StabilityCaseDefinition,
    StabilityGitIdentity,
    StabilityProtocol,
    StabilityRunManifest,
    StabilityTrialRecord,
    _build_report,
)
from tracejudge_hy3.phase4.stability_sensitivity import (
    Phase4StabilitySensitivityError,
    analyze_stability_sensitivity,
    publish_stability_sensitivity_release,
)


def _json_bytes(value: object) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")  # type: ignore[union-attr]
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n"
    ).encode()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _judgment(case_id: str, repetition: int) -> MethodJudgment:
    if case_id == "reasoning_swap":
        step = (
            "solution_trace.requirement_understanding"
            if repetition == 4
            else "requirement_understanding"
        )
        return MethodJudgment(
            functional_correct=True,
            has_error=True,
            reasoning_correct=False,
            plan_code_aligned=False,
            process_correct=False,
            first_faulty_layer="reasoning",
            first_faulty_step=step,
            violated_requirement="R1",
            error_type="R01_REQUIREMENT_MISREAD",
            verdict="strongly_supported",
            evidence_summary=("public fixture evidence",),
        )
    if case_id == "boundary_error":
        return MethodJudgment(
            functional_correct=False,
            has_error=True,
            reasoning_correct=True,
            plan_code_aligned=False,
            process_correct=False,
            first_faulty_layer="alignment",
            first_faulty_step="S1",
            violated_requirement="R1",
            error_type="A01_PLAN_CODE_MISMATCH",
            verdict="confirmed_bug",
            evidence_summary=("public fixture evidence",),
        )
    return MethodJudgment(
        functional_correct=True,
        has_error=False,
        reasoning_correct=True,
        plan_code_aligned=True,
        process_correct=True,
        evidence_summary=(),
    )


def _source_run(tmp_path: Path) -> Path:
    run_id = "stability_fixture_v1"
    provider_configuration = {"provider": "hy3", "model": "tencent/hy3"}
    case_ids = (
        "normal_correct",
        "reasoning_swap",
        "boundary_error",
        "equivalent_implementation",
    )
    cases = tuple(
        StabilityCaseDefinition(
            case_id=case_id,
            case_role=case_id,
            trace_id=f"public:{case_id}",
            execution_subject_id=f"public:{case_id}",
            expected_execution_status=("fail" if case_id == "boundary_error" else "pass"),
            method_input_sha256=f"{index}" * 64,
        )
        for index, case_id in enumerate(case_ids, start=1)
    )
    protocol = StabilityProtocol(
        research_question="Are the fixed public judgments stable?",
        source_git=StabilityGitIdentity(
            commit="a" * 40,
            branch="test",
            dirty=False,
        ),
        python_version="3.12",
        provider="hy3",
        model="tencent/hy3",
        provider_configuration=provider_configuration,
        provider_config_sha256=canonical_sha256(provider_configuration),
        method_id="full_tracejudge",
        prompt_version="test-prompt-v1",
        prompt_sha256="b" * 64,
        output_schema_sha256="c" * 64,
        method_spec_sha256="d" * 64,
        implementation_sha256="e" * 64,
        temperature=0.0,
        timeout_seconds=120.0,
        source_bundle_sha256="f" * 64,
        execution_manifest_sha256="1" * 64,
        execution_results_sha256="2" * 64,
        material_payloads_sha256="3" * 64,
        cases=cases,
    )
    timestamp = datetime(2026, 9, 3, tzinfo=UTC)
    trials = []
    trial_index = 0
    for repetition in range(1, 6):
        for case in cases:
            trial_index += 1
            outcome = MethodOutcome(
                run_id=run_id,
                trace_id=case.trace_id,
                method_id=MethodId.FULL_TRACEJUDGE,
                status=MethodOutcomeStatus.VALID_JUDGMENT,
                method_input_sha256=case.method_input_sha256,
                judgment=_judgment(case.case_id, repetition),
                attempt_count=1,
                parse_repair_count=0,
                raw_output_sha256="4" * 64,
                started_at=timestamp,
                ended_at=timestamp,
                duration_seconds=0.0,
            )
            trials.append(
                StabilityTrialRecord(
                    run_id=run_id,
                    trial_id=f"stability_trial_{trial_index:03d}",
                    trial_index=trial_index,
                    repetition_index=repetition,
                    case_id=case.case_id,
                    trace_id=case.trace_id,
                    outcome=outcome,
                )
            )
    protocol_payload = _json_bytes(protocol)
    report = _build_report(
        run_id=run_id,
        protocol_sha256=_sha256(protocol_payload),
        trials=trials,
    )
    results_payload = b"".join(
        json.dumps(
            trial.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        + b"\n"
        for trial in trials
    )
    report_payload = _json_bytes(report)
    report_markdown_payload = b"# frozen source report\n"
    manifest = StabilityRunManifest(
        run_id=run_id,
        status="completed",
        created_at=timestamp,
        updated_at=timestamp,
        protocol_sha256=_sha256(protocol_payload),
        completed_evaluation_count=20,
        observed_provider_call_count=20,
        results_sha256=_sha256(results_payload),
        report_json_sha256=_sha256(report_payload),
        report_markdown_sha256=_sha256(report_markdown_payload),
    )
    root = tmp_path / run_id
    root.mkdir()
    (root / "protocol.json").write_bytes(protocol_payload)
    (root / "results.jsonl").write_bytes(results_payload)
    (root / "report.json").write_bytes(report_payload)
    (root / "REPORT.md").write_bytes(report_markdown_payload)
    (root / "manifest.json").write_bytes(_json_bytes(manifest))
    return root


def test_sensitivity_preserves_raw_result_and_normalizes_only_exact_alias(tmp_path: Path):
    analysis = analyze_stability_sensitivity(_source_run(tmp_path))
    overall = {field.field_name: field for field in analysis.overall_fields}
    cases = {case.case_id: case for case in analysis.cases}
    reasoning = {field.field_name: field for field in cases["reasoning_swap"].fields}

    assert overall["first_faulty_step"].raw.pairwise_agreement == 0.9
    assert overall["first_faulty_step"].normalized.pairwise_agreement == 1.0
    assert overall["joint_label"].raw.pairwise_agreement == 0.9
    assert overall["joint_label"].normalized.pairwise_agreement == 1.0
    assert overall["has_error"].raw.pairwise_agreement == 1.0
    assert reasoning["first_faulty_step"].raw.pairwise_agreement == 0.6
    assert reasoning["first_faulty_step"].normalized.distribution == {
        "requirement_understanding": 5
    }
    assert analysis.raw_primary_result_preserved is True
    assert analysis.normalized_result_is_post_hoc is True
    assert analysis.provider_call_count_for_sensitivity_analysis == 0


def test_publish_writes_deterministic_public_report_and_svg_card(tmp_path: Path):
    source = _source_run(tmp_path)
    output = tmp_path / "public"
    first = publish_stability_sensitivity_release(run_dir=source, output_dir=output)
    second = publish_stability_sensitivity_release(run_dir=source, output_dir=output)

    assert first.json_sha256 == second.json_sha256
    assert first.markdown_sha256 == second.markdown_sha256
    assert first.card_sha256 == second.card_sha256
    markdown = first.markdown_path.read_text(encoding="utf-8")
    assert "预注册主结果，保持不变" in markdown
    assert "post-hoc 敏感性分析，不替代主结果" in markdown
    assert "90.0%" in markdown
    assert "100.0%" in markdown
    assert "Provider raw" in markdown
    payload = json.loads(first.json_path.read_text(encoding="utf-8"))
    assert payload["contains_trial_level_text"] is False
    assert payload["contains_provider_raw"] is False
    assert "public fixture evidence" not in first.json_path.read_text(encoding="utf-8")
    ElementTree.fromstring(first.card_path.read_bytes())


def test_sensitivity_fails_closed_when_source_results_hash_changes(tmp_path: Path):
    source = _source_run(tmp_path)
    with (source / "results.jsonl").open("ab") as stream:
        stream.write(b"\n")

    with pytest.raises(Phase4StabilitySensitivityError) as exc_info:
        analyze_stability_sensitivity(source)

    assert exc_info.value.safe_stage == "P4_STABILITY_SENSITIVITY_IDENTITY"
