from __future__ import annotations

import hashlib
import json
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tracejudge_hy3.cli import app
from tracejudge_hy3.phase3 import (
    PUBLIC_CERTIFICATE_CLAIMS_SHA256,
    FrozenCohortManifest,
    MethodId,
    NaturalTrace,
    Phase1BundleIdentity,
    Phase1ResponseReference,
    Phase2BundleIdentity,
    Phase2FunctionalEvidenceRef,
    Phase3ErrorCertificate,
    Phase3PublicCertificateManifest,
    PublicCertificateClaimsBundle,
    ResearchDatasetIdentity,
    SelectionRule,
    SourceAccounting,
    SourceOutcome,
    deterministic_probe_inputs,
    execute_public_counterfactual_evidence,
    freeze_counterfactual_cohort,
    generate_public_certificates,
    preflight_public_certificates,
    replay_public_certificate,
    search_public_counterexample,
)
from tracejudge_hy3.phase3.public_evidence import Phase3PublicEvidenceError
from tracejudge_hy3.resources import data_path

NOW = datetime(2026, 8, 28, 2, 3, 4, tzinfo=UTC)
SOURCE_PATH = data_path("phase3/public_counterfactuals_v1.json")
CLAIMS_PATH = data_path("phase3/public_certificate_claims_v1.json")


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True).encode(
            "utf-8"
        )
        + b"\n"
    )


def _natural_manifest(path: Path, *, count: int = 30) -> Path:
    phase1 = Phase1BundleIdentity(
        run_id="phase1_gate_d_fixture",
        manifest_sha256=_hash("phase1-manifest"),
        summary_sha256=_hash("phase1-summary"),
        responses_sha256=_hash("phase1-responses"),
    )
    phase2 = Phase2BundleIdentity(
        run_id="phase2_gate_d_fixture",
        manifest_sha256=_hash("phase2-manifest"),
        summary_sha256=_hash("phase2-summary"),
        results_sha256=_hash("phase2-results"),
        execution_log_sha256=_hash("phase2-log"),
    )
    traces: list[NaturalTrace] = []
    outcomes: list[SourceOutcome] = []
    for index in range(count):
        problem_id = f"GateD/{index}"
        trace_id = f"natural:{problem_id}"
        code_sha256 = _hash(f"code-{index}")
        evidence_sha256 = _hash(f"evidence-{index}")
        traces.append(
            NaturalTrace(
                trace_id=trace_id,
                problem_id=problem_id,
                public_problem_sha256=_hash(f"problem-{index}"),
                solution_trace_sha256=_hash(f"solution-{index}"),
                structured_explanation_sha256=_hash(f"explanation-{index}"),
                code_sha256=code_sha256,
                functional_evidence=Phase2FunctionalEvidenceRef(
                    phase2_run_id=phase2.run_id,
                    problem_id=problem_id,
                    result_line_number=index + 1,
                    result_record_sha256=evidence_sha256,
                    functional_evidence_sha256=evidence_sha256,
                    code_sha256=code_sha256,
                    base_status="pass",
                    plus_status="pass",
                    passed_base=True,
                    passed_plus=True,
                ),
                phase1_response=Phase1ResponseReference(
                    phase1_run_id=phase1.run_id,
                    problem_id=problem_id,
                    invocation_id=f"invocation_{index}",
                    response_line_number=index + 1,
                    response_record_sha256=_hash(f"response-{index}"),
                    code_sha256=code_sha256,
                ),
            )
        )
        outcomes.append(
            SourceOutcome(
                problem_id=problem_id,
                final_status="success",
                included_trace_id=trace_id,
            )
        )
    manifest = FrozenCohortManifest(
        freeze_id="phase3_natural_30_gate_d_fixture_v1",
        experiment_label="phase3_natural_30_gate_d_fixture_v1",
        created_at=NOW,
        dataset=ResearchDatasetIdentity(
            manifest_sha256=_hash("dataset-manifest"),
            dataset_id="fixture/gate-d",
            source="self_constructed_fixture",
            revision="a" * 40,
            license="MIT",
            problems_sha256=_hash("problems"),
            ordered_problem_ids_sha256=_hash("ordered-problems"),
            selection_algorithm="fixture-order-v1",
            selection_seed=20260828,
            source_problem_count=count,
        ),
        phase1=phase1,
        phase2=phase2,
        selection_rule=SelectionRule(
            rule_id="all_gate_d_fixture_successes_v1",
            policy="all_phase1_successes",
            description="Include every public Gate-D unit-test natural trace.",
            minimum_natural_count=30,
            target_natural_count=count,
            maximum_natural_count=45,
            stop_rule="Stop after the fixed public engineering fixture order.",
        ),
        source_accounting=SourceAccounting(
            source_problem_count=count,
            success_count=count,
            parse_error_count=0,
            provider_error_count=0,
            included_natural_trace_count=count,
        ),
        source_outcomes=tuple(outcomes),
        traces=tuple(traces),
        ordered_trace_ids=tuple(item.trace_id for item in traces),
        paired_method_ids=tuple(MethodId),
        privacy_policy_version="phase3_public_allowlist_v1",
    )
    path.write_bytes(_json_bytes(manifest.model_dump(mode="json")))
    return path


def test_gate_d_cli_commands_are_registered_without_execution():
    result = CliRunner().invoke(app, ["phase3", "--help"])
    assert result.exit_code == 0
    assert "certificate-preflight" in result.stdout
    assert "certificate-generate" in result.stdout
    assert "replay" in result.stdout


@pytest.fixture
def gate_d_inputs(tmp_path: Path) -> dict[str, Path]:
    evidence = execute_public_counterfactual_evidence(
        source_bundle_path=SOURCE_PATH,
        output_dir=tmp_path / "evidence",
        execution_run_id="phase3_gate_d_evidence_fixture_v1",
        per_test_timeout_seconds=2.0,
        created_at=NOW,
    )
    natural = _natural_manifest(tmp_path / "natural.json")
    overlay = freeze_counterfactual_cohort(
        natural_manifest_path=natural,
        source_bundle_path=SOURCE_PATH,
        execution_run_dir=evidence.run_dir,
        output_dir=tmp_path / "freezes",
        freeze_id="phase3_gate_d_cohort_30_plus_15_v1",
        created_at=NOW,
    )
    return {
        "natural": natural,
        "overlay": overlay.manifest_path,
        "evidence": evidence.run_dir,
        "output": tmp_path / "certificates",
    }


def test_public_claims_cover_three_levels_and_probe_order_is_bounded():
    payload = CLAIMS_PATH.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == PUBLIC_CERTIFICATE_CLAIMS_SHA256
    claims = PublicCertificateClaimsBundle.model_validate_json(payload)
    assert {item.expected_verdict for item in claims.claims} == {
        "confirmed_bug",
        "strongly_supported",
        "unverified_suspicion",
    }

    source = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    fixture_payload = source["parents"][0]["fixture"]
    from tracejudge_hy3.phase3 import PublicFixtureDefinition

    fixture = PublicFixtureDefinition.model_validate(fixture_payload)
    first = deterministic_probe_inputs(fixture)
    second = deterministic_probe_inputs(fixture)
    assert first == second
    assert 1 <= len(first) <= 32
    frozen = {
        hashlib.sha256(
            json.dumps(
                {"args": list(item["args"]), "kwargs": item["kwargs"]},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        for item in fixture_payload["test_cases"]
    }
    assert all(
        hashlib.sha256(
            json.dumps(
                {"args": list(probe.args), "kwargs": dict(probe.kwargs)},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        not in frozen
        for probe in first
    )


def test_public_probe_search_prioritizes_related_challenge_without_provider_or_docker():
    result = search_public_counterexample(
        source_bundle_path=SOURCE_PATH,
        trace_id="counterfactual:safe_mean:boundary_deletion:v1",
        violated_requirement_id="R1",
        per_test_timeout_seconds=2.0,
    )
    assert result.status == "found"
    assert result.attempted_public_challenges == 1
    assert result.attempted_deterministic_probes == 0
    assert result.counterexample is not None
    assert result.counterexample.source == "public_challenge_test"
    assert result.counterexample.candidate_exception == "ZeroDivisionError"


def test_public_certificate_preflight_generate_and_replay_are_bound_and_private(
    gate_d_inputs: dict[str, Path],
):
    run_id = "phase3_gate_d_public_certificates_fixture_v1"
    preflight = preflight_public_certificates(
        run_id=run_id,
        cohort_manifest_path=gate_d_inputs["overlay"],
        natural_manifest_path=gate_d_inputs["natural"],
        source_bundle_path=SOURCE_PATH,
        execution_run_dir=gate_d_inputs["evidence"],
        claims_bundle_path=CLAIMS_PATH,
        output_dir=gate_d_inputs["output"],
    )
    assert preflight.certificate_count == 3
    assert (
        preflight.confirmed_bug_count,
        preflight.strongly_supported_count,
        preflight.unverified_suspicion_count,
    ) == (1, 1, 1)
    assert not gate_d_inputs["output"].exists()

    generated = generate_public_certificates(
        run_id=run_id,
        cohort_manifest_path=gate_d_inputs["overlay"],
        natural_manifest_path=gate_d_inputs["natural"],
        source_bundle_path=SOURCE_PATH,
        execution_run_dir=gate_d_inputs["evidence"],
        claims_bundle_path=CLAIMS_PATH,
        output_dir=gate_d_inputs["output"],
        created_at=NOW,
    )
    assert generated.certificate_payloads_sha256 == preflight.certificate_payloads_sha256
    assert stat.S_IMODE(generated.run_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(generated.manifest_path.stat().st_mode) == 0o600
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in generated.certificate_paths)
    manifest = Phase3PublicCertificateManifest.model_validate_json(
        generated.manifest_path.read_bytes()
    )
    assert manifest.execution_mode == "reuse_validated_public_evidence_no_execution_v1"
    certificates = [
        Phase3ErrorCertificate.model_validate_json(path.read_bytes())
        for path in generated.certificate_paths
    ]
    assert [item.verdict for item in certificates] == [
        "confirmed_bug",
        "strongly_supported",
        "unverified_suspicion",
    ]
    all_public_bytes = generated.manifest_path.read_bytes() + b"".join(
        path.read_bytes() for path in generated.certificate_paths
    )
    assert b'"code"' not in all_public_bytes
    assert b"canonical_solution" not in all_public_bytes
    assert b"official_failure_inputs" not in all_public_bytes

    replay = replay_public_certificate(
        certificate_path=generated.certificate_paths[0],
        cohort_manifest_path=gate_d_inputs["overlay"],
        natural_manifest_path=gate_d_inputs["natural"],
        source_bundle_path=SOURCE_PATH,
        per_test_timeout_seconds=2.0,
    )
    assert replay.verified
    assert replay.reproduced_failure
    assert replay.executed_case_count == 1
    assert (
        replay.execution_evidence_sha256 == certificates[0].counterexample.execution_evidence_sha256
    )

    with pytest.raises(Phase3PublicEvidenceError, match="only confirmed_bug"):
        replay_public_certificate(
            certificate_path=generated.certificate_paths[1],
            cohort_manifest_path=gate_d_inputs["overlay"],
            natural_manifest_path=gate_d_inputs["natural"],
            source_bundle_path=SOURCE_PATH,
        )


def test_certificate_tampering_and_canary_fail_closed(
    gate_d_inputs: dict[str, Path], tmp_path: Path
):
    generated = generate_public_certificates(
        run_id="phase3_gate_d_tamper_fixture_v1",
        cohort_manifest_path=gate_d_inputs["overlay"],
        natural_manifest_path=gate_d_inputs["natural"],
        source_bundle_path=SOURCE_PATH,
        execution_run_dir=gate_d_inputs["evidence"],
        claims_bundle_path=CLAIMS_PATH,
        output_dir=gate_d_inputs["output"],
        created_at=NOW,
    )
    changed = json.loads(generated.certificate_paths[0].read_text(encoding="utf-8"))
    changed["code_sha256"] = "f" * 64
    tampered = tmp_path / "tampered.json"
    tampered.write_bytes(_json_bytes(changed))
    with pytest.raises(Phase3PublicEvidenceError, match="hashes differ"):
        replay_public_certificate(
            certificate_path=tampered,
            cohort_manifest_path=gate_d_inputs["overlay"],
            natural_manifest_path=gate_d_inputs["natural"],
            source_bundle_path=SOURCE_PATH,
        )

    with pytest.raises(Phase3PublicEvidenceError, match="contract or privacy"):
        preflight_public_certificates(
            run_id="phase3_gate_d_canary_fixture_v1",
            cohort_manifest_path=gate_d_inputs["overlay"],
            natural_manifest_path=gate_d_inputs["natural"],
            source_bundle_path=SOURCE_PATH,
            execution_run_dir=gate_d_inputs["evidence"],
            claims_bundle_path=CLAIMS_PATH,
            output_dir=tmp_path / "canary-output",
            privacy_canaries=("engineering-only judge claim",),
        )


def test_replay_rejects_invalid_timeout_before_execution(tmp_path: Path):
    with pytest.raises(Phase3PublicEvidenceError, match="timeout"):
        replay_public_certificate(
            certificate_path=tmp_path / "missing.json",
            cohort_manifest_path=tmp_path / "missing-overlay.json",
            natural_manifest_path=tmp_path / "missing-natural.json",
            source_bundle_path=SOURCE_PATH,
            per_test_timeout_seconds=0,
        )
