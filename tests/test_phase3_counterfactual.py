from __future__ import annotations

import hashlib
import json
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from tracejudge_hy3.phase3 import (
    PUBLIC_COUNTERFACTUAL_SOURCE_SHA256,
    CounterfactualCohortManifest,
    CounterfactualKind,
    FrozenCohortManifest,
    MethodId,
    NaturalTrace,
    Phase1BundleIdentity,
    Phase1ResponseReference,
    Phase2BundleIdentity,
    Phase2FunctionalEvidenceRef,
    PublicCounterfactualSourceBundle,
    PublicFixtureExecutionManifest,
    ResearchDatasetIdentity,
    SelectionRule,
    SourceAccounting,
    SourceOutcome,
    execute_public_counterfactual_evidence,
    freeze_counterfactual_cohort,
    preflight_counterfactual_freeze,
    preflight_public_counterfactual_source,
)
from tracejudge_hy3.phase3.cohort import Phase3FreezeError
from tracejudge_hy3.resources import data_path

NOW = datetime(2026, 8, 28, 1, 2, 3, tzinfo=UTC)
SOURCE_PATH = data_path("phase3/public_counterfactuals_v1.json")


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
        run_id="phase1_natural_fixture",
        manifest_sha256=_hash("phase1-manifest"),
        summary_sha256=_hash("phase1-summary"),
        responses_sha256=_hash("phase1-responses"),
    )
    phase2 = Phase2BundleIdentity(
        run_id="phase2_natural_fixture",
        manifest_sha256=_hash("phase2-manifest"),
        summary_sha256=_hash("phase2-summary"),
        results_sha256=_hash("phase2-results"),
        execution_log_sha256=_hash("phase2-log"),
    )
    traces: list[NaturalTrace] = []
    outcomes: list[SourceOutcome] = []
    for index in range(count):
        problem_id = f"Fixture/{index}"
        trace_id = f"natural:{problem_id}"
        code_sha256 = _hash(f"code-{index}")
        evidence_hash = _hash(f"evidence-{index}")
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
                    result_record_sha256=evidence_hash,
                    functional_evidence_sha256=evidence_hash,
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
        freeze_id="phase3_natural_30_fixture_v1",
        experiment_label="phase3_research_natural_30_fixture_v1",
        created_at=NOW,
        dataset=ResearchDatasetIdentity(
            manifest_sha256=_hash("dataset-manifest"),
            dataset_id="fixture/natural",
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
            rule_id="all_fixture_successes_v1",
            policy="all_phase1_successes",
            description="Include all synthetic natural traces used by this unit test.",
            minimum_natural_count=30,
            target_natural_count=count,
            maximum_natural_count=45,
            stop_rule="Stop after the fixed synthetic source order is exhausted.",
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


def test_public_counterfactual_source_has_target_quotas_and_exact_allowlist(tmp_path: Path):
    payload = SOURCE_PATH.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == PUBLIC_COUNTERFACTUAL_SOURCE_SHA256
    bundle = PublicCounterfactualSourceBundle.model_validate_json(payload)
    assert len(bundle.parents) == 3
    assert len(bundle.counterfactuals) == 15
    assert {
        kind: sum(item.mutation_kind == kind for item in bundle.counterfactuals)
        for kind in CounterfactualKind
    } == {kind: 3 for kind in CounterfactualKind}

    result = preflight_public_counterfactual_source(
        source_bundle_path=SOURCE_PATH,
        output_dir=tmp_path / "evidence",
        execution_run_id="phase3_cf_evidence_fixture_v1",
    )
    assert result.execution_subject_count == 15
    assert result.expected_pass_count == 6
    assert result.expected_fail_count == 9
    assert not (tmp_path / "evidence").exists()


def test_source_contract_rejects_multi_factor_or_incomplete_variants():
    source = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))

    changed_explanation = json.loads(json.dumps(source))
    changed_explanation["counterfactuals"][3]["solution_trace"]["design_summary"] = (
        "Also change the explanation."
    )
    with pytest.raises(ValidationError, match="preserve the parent explanation"):
        PublicCounterfactualSourceBundle.model_validate(changed_explanation)

    changed_reasoning_code = json.loads(json.dumps(source))
    changed_reasoning_code["counterfactuals"][0]["solution_trace"]["code"] += "\n"
    with pytest.raises(ValidationError, match="preserve parent code"):
        PublicCounterfactualSourceBundle.model_validate(changed_reasoning_code)

    incomplete = json.loads(json.dumps(source))
    incomplete["counterfactuals"] = incomplete["counterfactuals"][:-3]
    with pytest.raises(ValidationError, match="two or three variants"):
        PublicCounterfactualSourceBundle.model_validate(incomplete)


def test_allowlist_hash_rejects_even_schema_valid_source_copy(tmp_path: Path):
    source = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    source["counterfactuals"][0]["expected_impact"] += " Tampered."
    changed_path = tmp_path / "changed.json"
    changed_path.write_bytes(_json_bytes(source))

    with pytest.raises(Phase3FreezeError, match="exact executable allowlist"):
        preflight_public_counterfactual_source(
            source_bundle_path=changed_path,
            output_dir=tmp_path / "evidence",
            execution_run_id="phase3_cf_changed_v1",
        )

    with pytest.raises(Phase3FreezeError, match="privacy validation"):
        preflight_public_counterfactual_source(
            source_bundle_path=SOURCE_PATH,
            output_dir=tmp_path / "evidence",
            execution_run_id="phase3_cf_canary_v1",
            privacy_canaries=("Safe arithmetic mean",),
        )


def test_public_evidence_and_counterfactual_overlay_are_bound_and_immutable(tmp_path: Path):
    evidence = execute_public_counterfactual_evidence(
        source_bundle_path=SOURCE_PATH,
        output_dir=tmp_path / "evidence",
        execution_run_id="phase3_cf_evidence_fixture_v1",
        per_test_timeout_seconds=2.0,
        created_at=NOW,
    )
    assert evidence.result_count == 15
    assert evidence.pass_count == 6
    assert evidence.fail_count == 9
    assert evidence.timeout_count == 0
    assert evidence.infrastructure_error_count == 0
    assert evidence.expectation_mismatch_count == 0
    assert stat.S_IMODE(evidence.run_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(evidence.manifest_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(evidence.results_path.stat().st_mode) == 0o600
    execution_manifest = PublicFixtureExecutionManifest.model_validate_json(
        evidence.manifest_path.read_bytes()
    )
    assert execution_manifest.status == "completed"
    assert b'"code"' not in evidence.results_path.read_bytes()
    with pytest.raises(Phase3FreezeError, match="already exists"):
        execute_public_counterfactual_evidence(
            source_bundle_path=SOURCE_PATH,
            output_dir=tmp_path / "evidence",
            execution_run_id="phase3_cf_evidence_fixture_v1",
            per_test_timeout_seconds=2.0,
            created_at=NOW,
        )

    natural_manifest = _natural_manifest(tmp_path / "natural_manifest.json")
    preflight = preflight_counterfactual_freeze(
        natural_manifest_path=natural_manifest,
        source_bundle_path=SOURCE_PATH,
        execution_run_dir=evidence.run_dir,
        output_dir=tmp_path / "freezes",
        freeze_id="phase3_cohort_30_plus_15_fixture_v1",
        created_at=NOW,
    )
    assert preflight.natural_trace_count == 30
    assert preflight.counterfactual_trace_count == 15
    assert preflight.combined_trace_count == 45
    assert not (tmp_path / "freezes").exists()

    frozen = freeze_counterfactual_cohort(
        natural_manifest_path=natural_manifest,
        source_bundle_path=SOURCE_PATH,
        execution_run_dir=evidence.run_dir,
        output_dir=tmp_path / "freezes",
        freeze_id="phase3_cohort_30_plus_15_fixture_v1",
        created_at=NOW,
    )
    manifest = CounterfactualCohortManifest.model_validate_json(frozen.manifest_path.read_bytes())
    assert len(manifest.parents) == 3
    assert len(manifest.counterfactuals) == 15
    assert len(manifest.paired_ordered_trace_ids) == 45
    assert manifest.paired_ordered_trace_ids[:30] == manifest.natural_cohort.ordered_trace_ids
    assert manifest.paired_ordered_trace_ids[30:] == manifest.ordered_counterfactual_trace_ids
    for trace in manifest.counterfactuals:
        if trace.mutation.mutation_kind == CounterfactualKind.REASONING_SWAP:
            assert trace.functional_evidence.execution_subject_id == trace.parent_trace_id
        else:
            assert trace.functional_evidence.execution_subject_id == trace.trace_id
    assert stat.S_IMODE(frozen.run_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(frozen.manifest_path.stat().st_mode) == 0o600

    changed_order_hash = manifest.model_dump(mode="json")
    changed_order_hash["paired_ordered_trace_ids_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="paired trace order hash"):
        CounterfactualCohortManifest.model_validate(changed_order_hash)

    with pytest.raises(Phase3FreezeError, match="already exists"):
        freeze_counterfactual_cohort(
            natural_manifest_path=natural_manifest,
            source_bundle_path=SOURCE_PATH,
            execution_run_dir=evidence.run_dir,
            output_dir=tmp_path / "freezes",
            freeze_id="phase3_cohort_30_plus_15_fixture_v1",
            created_at=NOW,
        )

    tampered_parent = tmp_path / "tampered"
    tampered_run = tampered_parent / evidence.run_id
    shutil.copytree(evidence.run_dir, tampered_run)
    rows = tampered_run.joinpath("results.jsonl").read_text(encoding="utf-8").splitlines()
    first = json.loads(rows[0])
    first["code_sha256"] = "f" * 64
    rows[0] = json.dumps(first, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    tampered_run.joinpath("results.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
    with pytest.raises(Phase3FreezeError, match="identity differs"):
        preflight_counterfactual_freeze(
            natural_manifest_path=natural_manifest,
            source_bundle_path=SOURCE_PATH,
            execution_run_dir=tampered_run,
            output_dir=tmp_path / "other-freezes",
            freeze_id="phase3_tampered_v1",
            created_at=NOW,
        )

    case_tampered_parent = tmp_path / "case-tampered"
    case_tampered_run = case_tampered_parent / evidence.run_id
    shutil.copytree(evidence.run_dir, case_tampered_run)
    case_rows = case_tampered_run.joinpath("results.jsonl").read_text(encoding="utf-8").splitlines()
    case_first = json.loads(case_rows[0])
    case_first["case_results"][0]["expected_output"] = "changed"
    case_rows[0] = json.dumps(case_first, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    changed_results = ("\n".join(case_rows) + "\n").encode("utf-8")
    case_tampered_run.joinpath("results.jsonl").write_bytes(changed_results)
    changed_execution_manifest = json.loads(
        case_tampered_run.joinpath("manifest.json").read_text(encoding="utf-8")
    )
    changed_execution_manifest["results_sha256"] = hashlib.sha256(changed_results).hexdigest()
    case_tampered_run.joinpath("manifest.json").write_bytes(_json_bytes(changed_execution_manifest))
    with pytest.raises(Phase3FreezeError, match="cases differ"):
        preflight_counterfactual_freeze(
            natural_manifest_path=natural_manifest,
            source_bundle_path=SOURCE_PATH,
            execution_run_dir=case_tampered_run,
            output_dir=tmp_path / "case-tampered-freezes",
            freeze_id="phase3_case_tampered_v1",
            created_at=NOW,
        )
