from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from tracejudge_hy3.phase3.contracts import (
    AnnotationSetManifest,
    CounterfactualKind,
    CounterfactualMutation,
    CounterfactualTrace,
    EvidenceReference,
    EvidenceStrategy,
    ForbiddenInput,
    FrozenCohortManifest,
    MethodId,
    MethodJudgment,
    MethodOutcome,
    MethodSpec,
    MethodUsage,
    NaturalTrace,
    PairedEvaluationIndex,
    PairedMethodResultReference,
    Phase1BundleIdentity,
    Phase1ResponseReference,
    Phase2BundleIdentity,
    Phase2FunctionalEvidenceRef,
    Phase3ErrorCertificate,
    Phase3Invocation,
    Phase3ResumeIdentity,
    Phase3RunManifest,
    PublicCounterexample,
    PublicFixtureFunctionalEvidenceRef,
    ResearchDatasetIdentity,
    SelectionRule,
    SourceAccounting,
    SourceOutcome,
    VisibleInput,
)

H0 = "0" * 64
H1 = "1" * 64
H2 = "2" * 64
H3 = "3" * 64
H4 = "4" * 64
H5 = "5" * 64
NOW = datetime(2026, 8, 28, tzinfo=UTC)


def _phase2_evidence(*, code_sha256: str = H2) -> Phase2FunctionalEvidenceRef:
    return Phase2FunctionalEvidenceRef(
        phase2_run_id="phase2_formal",
        problem_id="HumanEval/1",
        result_line_number=1,
        result_record_sha256=H3,
        functional_evidence_sha256=H3,
        code_sha256=code_sha256,
        base_status="pass",
        plus_status="pass",
        passed_base=True,
        passed_plus=True,
    )


def _natural_trace() -> NaturalTrace:
    return NaturalTrace(
        trace_id="natural:HumanEval/1",
        problem_id="HumanEval/1",
        public_problem_sha256=H0,
        solution_trace_sha256=H1,
        structured_explanation_sha256=H4,
        code_sha256=H2,
        functional_evidence=_phase2_evidence(),
        phase1_response=Phase1ResponseReference(
            phase1_run_id="phase1_formal",
            problem_id="HumanEval/1",
            invocation_id="invocation_1",
            response_line_number=1,
            response_record_sha256=H5,
            code_sha256=H2,
        ),
    )


def _method_spec(method_id: MethodId) -> MethodSpec:
    common = (
        VisibleInput.PUBLIC_PROBLEM,
        VisibleInput.SOLUTION_TRACE,
        VisibleInput.CANDIDATE_CODE,
        VisibleInput.FUNCTIONAL_EVIDENCE,
    )
    if method_id == MethodId.TEST_ONLY:
        return MethodSpec(
            method_id=method_id,
            visible_inputs=(VisibleInput.FUNCTIONAL_EVIDENCE,),
            forbidden_inputs=tuple(ForbiddenInput),
            uses_llm=False,
            uses_ast=False,
            uses_public_dynamic_evidence=False,
            max_parse_repairs=0,
            parse_policy="not_applicable",
        )
    visible = common
    uses_ast = method_id in {MethodId.FOUR_LAYER_AST, MethodId.FULL_TRACEJUDGE}
    uses_dynamic = method_id == MethodId.FULL_TRACEJUDGE
    if uses_ast:
        visible += (VisibleInput.AST_EVIDENCE,)
    if uses_dynamic:
        visible += (VisibleInput.PUBLIC_DYNAMIC_EVIDENCE,)
    return MethodSpec(
        method_id=method_id,
        visible_inputs=visible,
        forbidden_inputs=tuple(ForbiddenInput),
        uses_llm=True,
        uses_ast=uses_ast,
        uses_public_dynamic_evidence=uses_dynamic,
        prompt_version=f"{method_id.value}_v1",
        prompt_sha256=H0,
        output_schema_sha256=H1,
        provider="hy3",
        model="frozen-model-id",
        temperature=0.0,
        timeout_seconds=60.0,
        max_parse_repairs=1,
        parse_policy="strict_json_schema_one_repair_v1",
    )


def _all_method_specs() -> tuple[MethodSpec, ...]:
    return tuple(_method_spec(method_id) for method_id in MethodId)


def _minimal_manifest(*traces) -> FrozenCohortManifest:
    natural = _natural_trace()
    frozen = traces or (natural,)
    return FrozenCohortManifest(
        freeze_id="phase3_freeze_v1",
        experiment_label="phase3_research_v1",
        created_at=NOW,
        dataset=ResearchDatasetIdentity(
            manifest_sha256=H5,
            dataset_id="evalplus/humanevalplus",
            source="evalplus_humanevalplus",
            revision="a" * 40,
            license="apache-2.0",
            problems_sha256=H4,
            ordered_problem_ids_sha256=H3,
            selection_algorithm=r"sha256(seed\\0problem_id)-lowest-v1",
            selection_seed=20260825,
            source_problem_count=1,
        ),
        phase1=Phase1BundleIdentity(
            run_id="phase1_formal",
            manifest_sha256=H0,
            summary_sha256=H1,
            responses_sha256=H2,
        ),
        phase2=Phase2BundleIdentity(
            run_id="phase2_formal",
            manifest_sha256=H0,
            summary_sha256=H1,
            results_sha256=H2,
            execution_log_sha256=H3,
        ),
        selection_rule=SelectionRule(
            rule_id="all_successes_v1",
            policy="all_phase1_successes",
            description="Include every complete phase-one success.",
            minimum_natural_count=1,
            target_natural_count=1,
            maximum_natural_count=1,
            stop_rule="Stop after the frozen source cohort is exhausted.",
        ),
        source_accounting=SourceAccounting(
            source_problem_count=1,
            success_count=1,
            parse_error_count=0,
            provider_error_count=0,
            included_natural_trace_count=1,
        ),
        source_outcomes=(
            SourceOutcome(
                problem_id="HumanEval/1",
                final_status="success",
                included_trace_id=natural.trace_id,
            ),
        ),
        traces=frozen,
        ordered_trace_ids=tuple(trace.trace_id for trace in frozen),
        paired_method_ids=tuple(MethodId),
        privacy_policy_version="phase3_public_allowlist_v1",
    )


def test_phase2_evidence_binds_exact_safe_row_and_base_plus_semantics():
    evidence = _phase2_evidence()
    assert evidence.publicly_replayable is False

    with pytest.raises(ValidationError, match="exact safe result row"):
        _phase2_evidence().model_copy(
            update={"functional_evidence_sha256": H4},
        ).__class__.model_validate(
            {
                **_phase2_evidence().model_dump(),
                "functional_evidence_sha256": H4,
            }
        )

    with pytest.raises(ValidationError, match="both Base and Plus"):
        Phase2FunctionalEvidenceRef.model_validate(
            {
                **evidence.model_dump(),
                "plus_status": "fail",
                "passed_plus": True,
            }
        )


def test_trace_rejects_functional_evidence_from_different_code():
    with pytest.raises(ValidationError, match="not bound to this trace code_sha256"):
        NaturalTrace.model_validate(
            {
                **_natural_trace().model_dump(),
                "functional_evidence": _phase2_evidence(code_sha256=H5).model_dump(),
            }
        )

    with pytest.raises(ValidationError, match="phase-one response is not bound"):
        NaturalTrace.model_validate(
            {
                **_natural_trace().model_dump(),
                "phase1_response": {
                    **_natural_trace().phase1_response.model_dump(),
                    "code_sha256": H5,
                },
            }
        )


def test_reasoning_counterfactual_may_reuse_only_identical_code_evidence():
    mutation = CounterfactualMutation(
        mutation_kind="reasoning_swap",
        sole_change="Replace only the structured explanation.",
        expected_impact="Code behavior remains unchanged.",
        before_solution_trace_sha256=H1,
        after_solution_trace_sha256=H3,
        before_structured_explanation_sha256=H4,
        after_structured_explanation_sha256=H5,
        before_code_sha256=H2,
        after_code_sha256=H2,
        evidence_strategy="reuse_same_code",
    )
    assert mutation.evidence_strategy == EvidenceStrategy.REUSE_SAME_CODE

    with pytest.raises(ValidationError, match="preserve code bytes"):
        CounterfactualMutation.model_validate({**mutation.model_dump(), "after_code_sha256": H3})


def test_changed_code_counterfactual_requires_independent_functional_evidence():
    with pytest.raises(ValidationError, match="may not reuse parent"):
        CounterfactualMutation(
            mutation_kind=CounterfactualKind.CODE_DEFECT,
            sole_change="Delete one branch.",
            expected_impact="The public boundary case fails.",
            before_solution_trace_sha256=H1,
            after_solution_trace_sha256=H3,
            before_structured_explanation_sha256=H4,
            after_structured_explanation_sha256=H4,
            before_code_sha256=H2,
            after_code_sha256=H5,
            evidence_strategy="reuse_same_code",
        )


def test_frozen_manifest_binds_counterfactual_to_parent_hashes_and_own_evidence():
    parent = _natural_trace()
    public_evidence = PublicFixtureFunctionalEvidenceRef(
        phase3_execution_run_id="phase3_fixture_run",
        execution_subject_id="counterfactual:HumanEval/1:boundary",
        problem_id=parent.problem_id,
        result_line_number=1,
        result_record_sha256=H5,
        functional_evidence_sha256=H5,
        code_sha256=H3,
        public_fixture_id="fixture_safe_mean_v1",
        public_fixture_sha256=H0,
        replay_spec_sha256=H1,
        execution_status="fail",
    )
    mutation = CounterfactualMutation(
        mutation_kind="boundary_deletion",
        sole_change="Delete one public boundary guard.",
        expected_impact="The public empty-input case fails.",
        before_solution_trace_sha256=parent.solution_trace_sha256,
        after_solution_trace_sha256=H5,
        before_structured_explanation_sha256=parent.structured_explanation_sha256,
        after_structured_explanation_sha256=parent.structured_explanation_sha256,
        before_code_sha256=parent.code_sha256,
        after_code_sha256=H3,
        evidence_strategy="independent_public_fixture",
    )
    variant = CounterfactualTrace(
        trace_id="counterfactual:HumanEval/1:boundary",
        problem_id=parent.problem_id,
        public_problem_sha256=parent.public_problem_sha256,
        solution_trace_sha256=H5,
        structured_explanation_sha256=parent.structured_explanation_sha256,
        code_sha256=H3,
        functional_evidence=public_evidence,
        parent_trace_id=parent.trace_id,
        mutation=mutation,
    )

    manifest = _minimal_manifest(parent, variant)
    assert len(manifest.traces) == 2
    assert variant.functional_evidence.code_sha256 == variant.code_sha256


def test_all_five_method_specs_have_frozen_visibility_and_strict_parsing():
    specs = _all_method_specs()
    assert {spec.method_id for spec in specs} == set(MethodId)
    assert _method_spec(MethodId.TEST_ONLY).max_parse_repairs == 0
    assert _method_spec(MethodId.FULL_TRACEJUDGE).max_parse_repairs == 1

    direct = _method_spec(MethodId.DIRECT_LLM_JUDGE)
    with pytest.raises(ValidationError, match="visible inputs"):
        MethodSpec.model_validate(
            {**direct.model_dump(), "visible_inputs": [*direct.visible_inputs, "ast_evidence"]}
        )

    with pytest.raises(ValidationError, match="forbid all"):
        MethodSpec.model_validate(
            {**direct.model_dump(), "forbidden_inputs": ["canonical_solution"]}
        )


def test_method_outcome_preserves_failure_and_limits_parse_repair():
    failure = MethodOutcome(
        run_id="phase3_run_1",
        trace_id="natural:HumanEval/1",
        method_id="direct_llm_judge",
        status="parse_error",
        method_input_sha256=H0,
        attempt_count=2,
        parse_repair_count=1,
        raw_output_sha256=H1,
        diagnostic_code="schema_validation_failed",
        started_at=NOW,
        ended_at=NOW + timedelta(seconds=1),
        duration_seconds=1.0,
    )
    assert failure.judgment is None

    with pytest.raises(ValidationError, match="later provider attempt"):
        MethodOutcome.model_validate(
            {**failure.model_dump(), "attempt_count": 1, "parse_repair_count": 1}
        )

    with pytest.raises(ValidationError, match="non-valid"):
        MethodOutcome.model_validate(
            {
                **failure.model_dump(),
                "judgment": MethodJudgment(functional_correct=True, has_error=False),
            }
        )


def test_method_usage_keeps_unknown_cost_distinct_from_zero_cost():
    unavailable = MethodUsage(
        prompt_tokens=100,
        completion_tokens=20,
        cost_status="unavailable",
    )
    assert unavailable.reported_cost_microusd is None

    reported = MethodUsage(
        prompt_tokens=100,
        completion_tokens=20,
        reported_cost_microusd=0,
        cost_status="provider_reported",
    )
    assert reported.reported_cost_microusd == 0

    with pytest.raises(ValidationError, match="requires a cost"):
        MethodUsage(cost_status="provider_reported")


def test_method_judgment_requires_evidence_for_an_error_and_clean_semantics():
    with pytest.raises(ValidationError, match="requires first layer"):
        MethodJudgment(functional_correct=False, has_error=True)

    with pytest.raises(ValidationError, match="cannot mark"):
        MethodJudgment(functional_correct=False, has_error=False)

    clean = MethodJudgment(functional_correct=True, has_error=False)
    assert clean.verdict is None


def test_confirmed_certificate_requires_public_counterexample_and_replay():
    base = {
        "certificate_id": "certificate_1",
        "trace_id": "natural:HumanEval/1",
        "problem_id": "HumanEval/1",
        "verdict": "confirmed_bug",
        "violated_requirement_id": "R1",
        "violated_public_requirement": "Return a value for the public empty-input case.",
        "first_faulty_layer": "implementation",
        "code_span": "function body",
        "error_type": "C01_BOUNDARY_ERROR",
        "supporting_evidence": [
            EvidenceReference(
                evidence_kind="public_execution",
                evidence_sha256=H2,
                publicly_reproducible=True,
                summary="The public probe reproduces the candidate exception.",
            )
        ],
        "frozen_manifest_sha256": H0,
        "code_sha256": H1,
        "structured_explanation_sha256": H2,
        "functional_evidence_sha256": H3,
    }
    with pytest.raises(ValidationError, match="public counterexample"):
        Phase3ErrorCertificate.model_validate(base)

    counterexample = PublicCounterexample(
        source="deterministic_probe",
        args=([],),
        expected=0.0,
        candidate_exception="ZeroDivisionError",
        public_source_sha256=H0,
        replay_spec_sha256=H1,
        execution_evidence_sha256=H2,
    )
    certificate = Phase3ErrorCertificate.model_validate(
        {
            **base,
            "counterexample": counterexample,
            "replay_command": "tracejudge phase3 replay --certificate certificate_1.json",
        }
    )
    assert certificate.verdict == "confirmed_bug"


def test_strong_support_requires_reproducible_public_static_evidence():
    base = {
        "certificate_id": "certificate_2",
        "trace_id": "natural:HumanEval/1",
        "problem_id": "HumanEval/1",
        "verdict": "strongly_supported",
        "violated_requirement_id": "R1",
        "violated_public_requirement": "Handle the documented boundary case.",
        "first_faulty_layer": "implementation",
        "code_span": "function body",
        "error_type": "C01_BOUNDARY_ERROR",
        "supporting_evidence": [
            EvidenceReference(
                evidence_kind="judge_claim",
                evidence_sha256=H4,
                publicly_reproducible=False,
                summary="The structured judge reports a suspected omitted guard.",
            )
        ],
        "frozen_manifest_sha256": H0,
        "code_sha256": H1,
        "structured_explanation_sha256": H2,
        "functional_evidence_sha256": H3,
    }
    with pytest.raises(ValidationError, match="reproducible public static evidence"):
        Phase3ErrorCertificate.model_validate(base)

    certificate = Phase3ErrorCertificate.model_validate(
        {
            **base,
            "supporting_evidence": [
                EvidenceReference(
                    evidence_kind="ast_rule",
                    evidence_sha256=H4,
                    publicly_reproducible=True,
                    summary="The frozen AST rule can be recomputed from public code.",
                )
            ],
        }
    )
    assert certificate.counterexample is None


def test_unverified_certificate_cannot_claim_publicly_reproducible_evidence():
    base = {
        "certificate_id": "certificate_3",
        "trace_id": "natural:HumanEval/1",
        "problem_id": "HumanEval/1",
        "verdict": "unverified_suspicion",
        "violated_requirement_id": "R1",
        "violated_public_requirement": "Handle the documented boundary case.",
        "first_faulty_layer": "reasoning",
        "code_span": "function body",
        "error_type": "P02_UNJUSTIFIED_STEP",
        "supporting_evidence": [
            EvidenceReference(
                evidence_kind="judge_claim",
                evidence_sha256=H4,
                publicly_reproducible=False,
                summary="The judge suspects an unsupported reasoning step.",
            )
        ],
        "frozen_manifest_sha256": H0,
        "code_sha256": H1,
        "structured_explanation_sha256": H2,
        "functional_evidence_sha256": H3,
    }
    certificate = Phase3ErrorCertificate.model_validate(base)
    assert certificate.replay_command is None

    with pytest.raises(ValidationError, match="cannot remain unverified"):
        Phase3ErrorCertificate.model_validate(
            {
                **base,
                "supporting_evidence": [
                    EvidenceReference(
                        evidence_kind="ast_rule",
                        evidence_sha256=H5,
                        publicly_reproducible=True,
                        summary="The AST rule can be recomputed from public code.",
                    )
                ],
            }
        )


def test_inter_rater_label_is_impossible_with_one_rater():
    with pytest.raises(ValidationError, match="at least two raters"):
        AnnotationSetManifest(
            annotation_set_id="annotations_v1",
            annotation_protocol_sha256=H0,
            annotation_guide_sha256=H1,
            frozen_cohort_manifest_sha256=H1,
            source_packet_id="packet_v1",
            source_packet_manifest_sha256=H2,
            source_packet_sha256=H3,
            source_identity_map_sha256=H4,
            source_labels_template_sha256=H5,
            source_completed_labels_sha256=H0,
            completed_labels_sha256=H1,
            ordered_trace_ids=("natural:HumanEval/1",),
            record_count=1,
            natural_trace_count=1,
            counterfactual_trace_count=0,
            annotation_records_sha256=H2,
            rater_ids=("rater_a",),
            annotation_rounds=(1,),
            agreement_kind="inter_rater",
            created_at=NOW,
        )


def test_resume_identity_distinguishes_clean_and_dirty_worktrees():
    base = {
        "frozen_manifest_sha256": H0,
        "natural_manifest_sha256": H1,
        "ordered_trace_ids_sha256": H1,
        "material_payloads_sha256": H2,
        "method_specs_sha256": H2,
        "prompt_bundle_sha256": H3,
        "output_schema_sha256": H4,
        "implementation_sha256": H5,
        "provider_config_sha256": H0,
        "annotation_set_manifest_sha256": H1,
        "completed_labels_sha256": H2,
        "annotation_records_sha256": H3,
        "git_commit": "a" * 40,
        "git_branch": "phase3-process-evaluation",
        "git_dirty": False,
        "python_version": "3.11.10",
        "direct_dependencies_sha256": H0,
        "ast_implementation_sha256": H1,
        "public_evidence_policy_sha256": H2,
        "annotation_protocol_sha256": H3,
        "random_seed": 20260828,
    }
    clean = Phase3ResumeIdentity.model_validate(base)
    assert clean.git_worktree_fingerprint is None

    with pytest.raises(ValidationError, match="requires exactly one"):
        Phase3ResumeIdentity.model_validate(
            {**base, "git_dirty": True, "git_worktree_fingerprint": None}
        )

    dirty = Phase3ResumeIdentity.model_validate(
        {**base, "git_dirty": True, "git_worktree_fingerprint": H4}
    )
    assert dirty.git_worktree_fingerprint == H4


def test_interrupted_invocation_is_preserved_before_successful_resume():
    resume_identity = Phase3ResumeIdentity(
        frozen_manifest_sha256=H0,
        natural_manifest_sha256=H1,
        ordered_trace_ids_sha256=H1,
        material_payloads_sha256=H2,
        method_specs_sha256=H2,
        prompt_bundle_sha256=H3,
        output_schema_sha256=H4,
        implementation_sha256=H5,
        provider_config_sha256=H0,
        annotation_set_manifest_sha256=H1,
        completed_labels_sha256=H2,
        annotation_records_sha256=H3,
        git_commit="a" * 40,
        git_branch="phase3-process-evaluation",
        git_dirty=False,
        python_version="3.11.10",
        direct_dependencies_sha256=H0,
        ast_implementation_sha256=H1,
        public_evidence_policy_sha256=H2,
        annotation_protocol_sha256=H3,
        random_seed=20260828,
    )
    interrupted = Phase3Invocation(
        invocation_id="invocation_1",
        resume=False,
        status="interrupted",
        resume_identity_sha256=H4,
        started_at=NOW,
        ended_at=NOW + timedelta(seconds=1),
    )
    resumed = Phase3Invocation(
        invocation_id="invocation_2",
        resume=True,
        status="completed",
        resume_identity_sha256=H4,
        started_at=NOW + timedelta(seconds=2),
        ended_at=NOW + timedelta(seconds=3),
    )
    manifest = Phase3RunManifest(
        run_id="phase3_run_1",
        status="completed",
        created_at=NOW,
        completed_at=NOW + timedelta(seconds=3),
        frozen_manifest_sha256=H0,
        resume_identity=resume_identity,
        resume_identity_sha256=H4,
        invocations=(interrupted, resumed),
    )
    assert [item.status.value for item in manifest.invocations] == [
        "interrupted",
        "completed",
    ]

    with pytest.raises(ValidationError, match="must be resumes"):
        Phase3RunManifest.model_validate(
            {
                **manifest.model_dump(),
                "invocations": [
                    interrupted.model_dump(),
                    {**resumed.model_dump(), "resume": False},
                ],
            }
        )


def test_paired_index_requires_every_trace_method_pair_in_frozen_order():
    trace_ids = ("natural:HumanEval/1", "counterfactual:HumanEval/1:boundary")
    references = tuple(
        PairedMethodResultReference(
            trace_id=trace_id,
            method_id=method_id,
            status="infrastructure_error" if line_number == 3 else "valid_judgment",
            result_line_number=line_number,
            result_record_sha256=H5,
        )
        for line_number, (trace_id, method_id) in enumerate(
            ((trace_id, method_id) for trace_id in trace_ids for method_id in MethodId),
            start=1,
        )
    )
    index = PairedEvaluationIndex(
        run_id="phase3_run_1",
        frozen_manifest_sha256=H0,
        resume_identity_sha256=H1,
        ordered_trace_ids=trace_ids,
        ordered_method_ids=tuple(MethodId),
        result_references=references,
        results_sha256=H2,
    )
    assert len(index.result_references) == len(trace_ids) * len(MethodId)
    assert index.result_references[2].status.value == "infrastructure_error"

    with pytest.raises(ValidationError, match="complete trace-major"):
        PairedEvaluationIndex.model_validate(
            {**index.model_dump(), "result_references": index.result_references[:-1]}
        )


def test_infrastructure_error_cannot_carry_candidate_judgment():
    with pytest.raises(ValidationError, match="non-valid"):
        MethodOutcome(
            run_id="phase3_run_1",
            trace_id="natural:HumanEval/1",
            method_id="full_tracejudge",
            status="infrastructure_error",
            method_input_sha256=H0,
            judgment=MethodJudgment(
                functional_correct=False,
                has_error=True,
                first_faulty_layer="execution",
                error_type="E03_WRONG_OUTPUT",
                verdict="unverified_suspicion",
                evidence_summary=("The safe functional status reports a failure.",),
            ),
            attempt_count=0,
            parse_repair_count=0,
            diagnostic_code="sandbox_unavailable",
            started_at=NOW,
            ended_at=NOW,
            duration_seconds=0.0,
        )
