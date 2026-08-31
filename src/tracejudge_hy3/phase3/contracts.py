"""Strict, execution-free contracts for phase-three research artifacts.

These models bind frozen traces, safe functional evidence, paired method
outcomes, public counterexamples, certificates, annotations, and resume
identity.  They intentionally contain no file loading, provider call, sandbox
execution, or experiment orchestration.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from tracejudge_hy3.schemas.evaluation import ErrorType, FaultyLayer
from tracejudge_hy3.schemas.solution import SolutionTrace

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
GitCommit = Annotated[
    str,
    StringConstraints(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"),
]
Identifier = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$"),
]
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
AlgorithmDescriptor = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=256),
]


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _canonical_sequence_sha256(values: tuple[str, ...]) -> str:
    payload = json.dumps(
        values,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class MethodId(StrEnum):
    TEST_ONLY = "test_only"
    DIRECT_LLM_JUDGE = "direct_llm_judge"
    FOUR_LAYER_STRUCTURED_JUDGE = "four_layer_structured_judge"
    FOUR_LAYER_AST = "four_layer_ast"
    FULL_TRACEJUDGE = "full_tracejudge"


class VisibleInput(StrEnum):
    PUBLIC_PROBLEM = "public_problem"
    SOLUTION_TRACE = "solution_trace"
    CANDIDATE_CODE = "candidate_code"
    FUNCTIONAL_EVIDENCE = "functional_evidence"
    AST_EVIDENCE = "ast_evidence"
    PUBLIC_DYNAMIC_EVIDENCE = "public_dynamic_evidence"


class ForbiddenInput(StrEnum):
    CANONICAL_SOLUTION = "canonical_solution"
    OFFICIAL_TEST_INPUTS = "official_test_inputs"
    OFFICIAL_FAILURE_INPUTS = "official_failure_inputs"
    EVALPLUS_RAW = "evalplus_raw"
    CREDENTIALS = "credentials"


class MethodOutcomeStatus(StrEnum):
    VALID_JUDGMENT = "valid_judgment"
    PROVIDER_ERROR = "provider_error"
    PARSE_ERROR = "parse_error"
    AST_ERROR = "ast_error"
    PUBLIC_EXECUTION_TIMEOUT = "public_execution_timeout"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    SKIPPED = "skipped"
    REUSED = "reused"


class InvocationStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"


class CounterfactualKind(StrEnum):
    REASONING_SWAP = "reasoning_swap"
    CODE_DEFECT = "code_defect"
    BOUNDARY_DELETION = "boundary_deletion"
    SHORTCUT = "shortcut"
    EQUIVALENT_IMPLEMENTATION = "equivalent_implementation"


class EvidenceStrategy(StrEnum):
    REUSE_SAME_CODE = "reuse_same_code"
    INDEPENDENT_EVALPLUS = "independent_evalplus"
    INDEPENDENT_PUBLIC_FIXTURE = "independent_public_fixture"


class Phase1BundleIdentity(StrictFrozenModel):
    run_id: Identifier
    manifest_sha256: Sha256
    summary_sha256: Sha256
    responses_sha256: Sha256


class Phase2BundleIdentity(StrictFrozenModel):
    run_id: Identifier
    manifest_sha256: Sha256
    summary_sha256: Sha256
    results_sha256: Sha256
    execution_log_sha256: Sha256


class ResearchDatasetIdentity(StrictFrozenModel):
    manifest_sha256: Sha256
    dataset_id: Identifier
    source: Identifier
    revision: GitCommit
    license: NonEmptyText
    problems_sha256: Sha256
    ordered_problem_ids_sha256: Sha256
    # Dataset provenance stores an exact human-readable algorithm expression,
    # not an identifier.  The production value contains punctuation such as
    # parentheses and backslashes and must remain hash-bound verbatim.
    selection_algorithm: AlgorithmDescriptor
    selection_seed: int = Field(ge=0)
    selection_role: Literal["research_natural"] = "research_natural"
    source_problem_count: int = Field(ge=1, le=164)


class Phase1ResponseReference(StrictFrozenModel):
    phase1_run_id: Identifier
    problem_id: Identifier
    invocation_id: Identifier
    response_line_number: int = Field(ge=1)
    response_record_sha256: Sha256
    code_sha256: Sha256


class Phase2FunctionalEvidenceRef(StrictFrozenModel):
    kind: Literal["phase2_evalplus_safe"] = "phase2_evalplus_safe"
    phase2_run_id: Identifier
    problem_id: Identifier
    result_line_number: int = Field(ge=1)
    result_record_sha256: Sha256
    functional_evidence_sha256: Sha256
    code_sha256: Sha256
    base_status: Literal["pass", "fail", "timeout"]
    plus_status: Literal["pass", "fail", "timeout"]
    passed_base: bool
    passed_plus: bool
    infrastructure_status: Literal["ok"] = "ok"
    publicly_replayable: Literal[False] = False

    @model_validator(mode="after")
    def validate_safe_result_semantics(self) -> Self:
        if self.result_record_sha256 != self.functional_evidence_sha256:
            raise ValueError("phase-two functional evidence must hash the exact safe result row")
        if self.passed_base != (self.base_status == "pass"):
            raise ValueError("passed_base disagrees with base_status")
        expected_plus = self.base_status == "pass" and self.plus_status == "pass"
        if self.passed_plus != expected_plus:
            raise ValueError("passed_plus requires both Base and Plus status to be pass")
        return self


class PublicFixtureFunctionalEvidenceRef(StrictFrozenModel):
    kind: Literal["public_fixture_execution"] = "public_fixture_execution"
    phase3_execution_run_id: Identifier
    execution_subject_id: Identifier
    problem_id: Identifier
    result_line_number: int = Field(ge=1)
    result_record_sha256: Sha256
    functional_evidence_sha256: Sha256
    code_sha256: Sha256
    public_fixture_id: Identifier
    public_fixture_sha256: Sha256
    replay_spec_sha256: Sha256
    execution_status: Literal["pass", "fail", "timeout"]
    publicly_replayable: Literal[True] = True

    @model_validator(mode="after")
    def validate_public_result_hash(self) -> Self:
        if self.result_record_sha256 != self.functional_evidence_sha256:
            raise ValueError("public functional evidence must hash the exact safe result row")
        return self


FunctionalEvidenceRef = Annotated[
    Phase2FunctionalEvidenceRef | PublicFixtureFunctionalEvidenceRef,
    Field(discriminator="kind"),
]


class FrozenTraceBase(StrictFrozenModel):
    trace_id: Identifier
    problem_id: Identifier
    public_problem_sha256: Sha256
    solution_trace_sha256: Sha256
    structured_explanation_sha256: Sha256
    code_sha256: Sha256
    functional_evidence: FunctionalEvidenceRef

    @model_validator(mode="after")
    def bind_functional_evidence(self) -> Self:
        if self.functional_evidence.problem_id != self.problem_id:
            raise ValueError("functional evidence problem_id differs from trace")
        if self.functional_evidence.code_sha256 != self.code_sha256:
            raise ValueError("functional evidence is not bound to this trace code_sha256")
        return self


class NaturalTrace(FrozenTraceBase):
    trace_kind: Literal["natural"] = "natural"
    phase1_response: Phase1ResponseReference

    @model_validator(mode="after")
    def bind_phase1_response(self) -> Self:
        if self.phase1_response.problem_id != self.problem_id:
            raise ValueError("phase-one response problem_id differs from natural trace")
        if self.phase1_response.code_sha256 != self.code_sha256:
            raise ValueError("phase-one response is not bound to this trace code_sha256")
        return self


class CounterfactualMutation(StrictFrozenModel):
    mutation_kind: CounterfactualKind
    sole_change: NonEmptyText
    expected_impact: NonEmptyText
    before_solution_trace_sha256: Sha256
    after_solution_trace_sha256: Sha256
    before_structured_explanation_sha256: Sha256
    after_structured_explanation_sha256: Sha256
    before_code_sha256: Sha256
    after_code_sha256: Sha256
    evidence_strategy: EvidenceStrategy

    @model_validator(mode="after")
    def validate_single_factor_hashes(self) -> Self:
        if self.before_solution_trace_sha256 == self.after_solution_trace_sha256:
            raise ValueError("counterfactual must change the solution trace")
        if self.mutation_kind == CounterfactualKind.REASONING_SWAP:
            if self.before_code_sha256 != self.after_code_sha256:
                raise ValueError("reasoning counterfactual must preserve code bytes")
            if (
                self.before_structured_explanation_sha256
                == self.after_structured_explanation_sha256
            ):
                raise ValueError("reasoning counterfactual must change structured explanation")
            if self.evidence_strategy != EvidenceStrategy.REUSE_SAME_CODE:
                raise ValueError(
                    "reasoning counterfactual must explicitly reuse same-code evidence"
                )
        else:
            if self.before_code_sha256 == self.after_code_sha256:
                raise ValueError("code counterfactual must change code_sha256")
            if (
                self.before_structured_explanation_sha256
                != self.after_structured_explanation_sha256
            ):
                raise ValueError("code counterfactual must preserve structured explanation")
            if self.evidence_strategy == EvidenceStrategy.REUSE_SAME_CODE:
                raise ValueError("changed code may not reuse parent functional evidence")
        return self


class CounterfactualTrace(FrozenTraceBase):
    trace_kind: Literal["counterfactual"] = "counterfactual"
    parent_trace_id: Identifier
    mutation: CounterfactualMutation

    @model_validator(mode="after")
    def bind_after_hashes(self) -> Self:
        if self.mutation.after_solution_trace_sha256 != self.solution_trace_sha256:
            raise ValueError("counterfactual solution hash differs from mutation after hash")
        if self.mutation.after_structured_explanation_sha256 != self.structured_explanation_sha256:
            raise ValueError("counterfactual explanation hash differs from mutation after hash")
        if self.mutation.after_code_sha256 != self.code_sha256:
            raise ValueError("counterfactual code hash differs from mutation after hash")
        return self


FrozenTrace = Annotated[NaturalTrace | CounterfactualTrace, Field(discriminator="trace_kind")]


class SourceOutcome(StrictFrozenModel):
    problem_id: Identifier
    final_status: Literal["success", "parse_error", "provider_error"]
    included_trace_id: Identifier | None = None

    @model_validator(mode="after")
    def failed_source_has_no_trace(self) -> Self:
        if self.final_status != "success" and self.included_trace_id is not None:
            raise ValueError("failed phase-one outcome cannot be represented as a complete trace")
        return self


class SourceAccounting(StrictFrozenModel):
    source_problem_count: int = Field(ge=1)
    success_count: int = Field(ge=0)
    parse_error_count: int = Field(ge=0)
    provider_error_count: int = Field(ge=0)
    included_natural_trace_count: int = Field(ge=0)

    @model_validator(mode="after")
    def cover_source_cohort(self) -> Self:
        total = self.success_count + self.parse_error_count + self.provider_error_count
        if total != self.source_problem_count:
            raise ValueError("phase-one final outcomes do not cover the source cohort")
        if self.included_natural_trace_count > self.success_count:
            raise ValueError("included natural traces exceed phase-one successes")
        return self


class SelectionRule(StrictFrozenModel):
    rule_id: Identifier
    policy: Literal["all_phase1_successes", "ordered_prefix"]
    description: NonEmptyText
    minimum_natural_count: int = Field(ge=1)
    target_natural_count: int = Field(ge=1)
    maximum_natural_count: int = Field(ge=1, le=45)
    backup_problem_ids: tuple[Identifier, ...] = ()
    stop_rule: NonEmptyText
    frozen_before_method_predictions: Literal[True] = True
    frozen_before_human_labels: Literal[True] = True

    @model_validator(mode="after")
    def validate_quota_order(self) -> Self:
        if not (
            self.minimum_natural_count <= self.target_natural_count <= self.maximum_natural_count
        ):
            raise ValueError("natural trace quotas must satisfy minimum <= target <= maximum")
        if len(self.backup_problem_ids) != len(set(self.backup_problem_ids)):
            raise ValueError("backup problem IDs must be unique")
        return self


class MethodSpec(StrictFrozenModel):
    method_id: MethodId
    visible_inputs: tuple[VisibleInput, ...]
    forbidden_inputs: tuple[ForbiddenInput, ...]
    uses_llm: bool
    uses_ast: bool
    uses_public_dynamic_evidence: bool
    prompt_version: Identifier | None = None
    prompt_sha256: Sha256 | None = None
    output_schema_sha256: Sha256 | None = None
    provider: NonEmptyText | None = None
    model: NonEmptyText | None = None
    temperature: float | None = Field(default=None, ge=0.0)
    timeout_seconds: float | None = Field(default=None, gt=0.0)
    max_parse_repairs: Literal[0, 1]
    parse_policy: Literal["not_applicable", "strict_json_schema_one_repair_v1"]

    @model_validator(mode="after")
    def validate_method_visibility(self) -> Self:
        required_forbidden = set(ForbiddenInput)
        if len(self.forbidden_inputs) != len(set(self.forbidden_inputs)):
            raise ValueError("forbidden method inputs must be unique")
        if set(self.forbidden_inputs) != required_forbidden:
            raise ValueError("every method must forbid all evaluation-only and secret inputs")
        if len(self.visible_inputs) != len(set(self.visible_inputs)):
            raise ValueError("visible method inputs must be unique")

        common = {
            VisibleInput.PUBLIC_PROBLEM,
            VisibleInput.SOLUTION_TRACE,
            VisibleInput.CANDIDATE_CODE,
            VisibleInput.FUNCTIONAL_EVIDENCE,
        }
        expected = {
            MethodId.TEST_ONLY: {VisibleInput.FUNCTIONAL_EVIDENCE},
            MethodId.DIRECT_LLM_JUDGE: common,
            MethodId.FOUR_LAYER_STRUCTURED_JUDGE: common,
            MethodId.FOUR_LAYER_AST: common | {VisibleInput.AST_EVIDENCE},
            MethodId.FULL_TRACEJUDGE: common
            | {VisibleInput.AST_EVIDENCE, VisibleInput.PUBLIC_DYNAMIC_EVIDENCE},
        }[self.method_id]
        if set(self.visible_inputs) != expected:
            raise ValueError("visible inputs do not match the frozen method definition")

        expected_ast = self.method_id in {MethodId.FOUR_LAYER_AST, MethodId.FULL_TRACEJUDGE}
        expected_dynamic = self.method_id == MethodId.FULL_TRACEJUDGE
        expected_llm = self.method_id != MethodId.TEST_ONLY
        if self.uses_ast != expected_ast or self.uses_public_dynamic_evidence != expected_dynamic:
            raise ValueError("method capability flags disagree with method_id")
        if self.uses_llm != expected_llm:
            raise ValueError("uses_llm disagrees with method_id")

        llm_fields = (
            self.prompt_version,
            self.prompt_sha256,
            self.output_schema_sha256,
            self.provider,
            self.model,
            self.timeout_seconds,
        )
        if expected_llm:
            if any(value is None for value in llm_fields):
                raise ValueError("LLM methods require prompt, schema, provider, model, and timeout")
            if self.max_parse_repairs != 1:
                raise ValueError("LLM methods must freeze exactly one parse repair")
            if self.parse_policy != "strict_json_schema_one_repair_v1":
                raise ValueError("LLM methods must use the strict phase-three parse policy")
        else:
            if any(value is not None for value in llm_fields) or self.temperature is not None:
                raise ValueError("Test-only may not declare LLM configuration")
            if self.max_parse_repairs != 0 or self.parse_policy != "not_applicable":
                raise ValueError("Test-only has no structured model parsing")
        return self


class MethodJudgment(StrictFrozenModel):
    functional_correct: bool
    has_error: bool
    reasoning_correct: bool | None = None
    plan_code_aligned: bool | None = None
    process_correct: bool | None = None
    first_faulty_layer: FaultyLayer | None = None
    first_faulty_step: Identifier | None = None
    violated_requirement: Identifier | None = None
    code_span: NonEmptyText | None = None
    error_type: ErrorType | None = None
    verdict: Literal["confirmed_bug", "strongly_supported", "unverified_suspicion"] | None = None
    evidence_summary: tuple[NonEmptyText, ...] = ()

    @model_validator(mode="after")
    def clean_judgment_has_no_fault_fields(self) -> Self:
        fault_fields = (
            self.first_faulty_layer,
            self.first_faulty_step,
            self.violated_requirement,
            self.code_span,
            self.error_type,
            self.verdict,
        )
        if not self.has_error:
            if not self.functional_correct:
                raise ValueError("no-error judgment cannot mark the candidate functionally wrong")
            if any(value is not None for value in fault_fields):
                raise ValueError("no-error judgment may not retain fault or verdict fields")
        elif (
            self.first_faulty_layer is None
            or self.error_type is None
            or self.verdict is None
            or not self.evidence_summary
        ):
            raise ValueError(
                "error judgment requires first layer, error type, verdict, and evidence"
            )
        return self


class MethodUsage(StrictFrozenModel):
    """Provider-reported accounting; missing price data stays explicitly unknown."""

    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    reported_cost_microusd: int | None = Field(default=None, ge=0)
    cost_status: Literal["not_applicable", "provider_reported", "unavailable"]

    @model_validator(mode="after")
    def validate_cost_status(self) -> Self:
        if self.cost_status == "not_applicable":
            if any(
                value is not None
                for value in (
                    self.prompt_tokens,
                    self.completion_tokens,
                    self.reported_cost_microusd,
                )
            ):
                raise ValueError("non-provider method usage must not report tokens or cost")
        elif self.cost_status == "provider_reported":
            if self.reported_cost_microusd is None:
                raise ValueError("provider-reported cost status requires a cost value")
        elif self.reported_cost_microusd is not None:
            raise ValueError("unavailable cost status may not report a cost value")
        return self


class MethodOutcome(StrictFrozenModel):
    run_id: Identifier
    trace_id: Identifier
    method_id: MethodId
    status: MethodOutcomeStatus
    method_input_sha256: Sha256
    judgment: MethodJudgment | None = None
    attempt_count: int = Field(ge=0)
    parse_repair_count: int = Field(ge=0, le=1)
    raw_output_sha256: Sha256 | None = None
    usage: MethodUsage = Field(default_factory=lambda: MethodUsage(cost_status="unavailable"))
    diagnostic_code: Identifier | None = None
    reused_from_result_sha256: Sha256 | None = None
    started_at: datetime
    ended_at: datetime
    duration_seconds: float = Field(ge=0.0)

    @model_validator(mode="after")
    def validate_terminal_status(self) -> Self:
        if self.started_at.tzinfo is None or self.ended_at.tzinfo is None:
            raise ValueError("method timestamps must be timezone-aware")
        if self.ended_at < self.started_at:
            raise ValueError("method ended_at precedes started_at")
        if self.parse_repair_count and self.parse_repair_count >= self.attempt_count:
            raise ValueError("a parse repair requires a later provider attempt")
        if self.status == MethodOutcomeStatus.VALID_JUDGMENT:
            if self.judgment is None:
                raise ValueError("valid_judgment requires a parsed judgment")
            if self.reused_from_result_sha256 is not None:
                raise ValueError("new valid judgment may not claim reused provenance")
        elif self.judgment is not None:
            raise ValueError("non-valid method event may not carry a judgment")
        if (
            self.status
            in {
                MethodOutcomeStatus.PROVIDER_ERROR,
                MethodOutcomeStatus.PARSE_ERROR,
            }
            and self.attempt_count < 1
        ):
            raise ValueError("provider and parse failures require at least one attempt")
        if self.status == MethodOutcomeStatus.REUSED:
            if self.reused_from_result_sha256 is None:
                raise ValueError("reused event requires exact prior result hash")
        elif self.reused_from_result_sha256 is not None:
            raise ValueError("only reused events may reference a prior result")
        return self


class EvidenceReference(StrictFrozenModel):
    evidence_kind: Literal["ast_rule", "public_execution", "phase2_safe_status", "judge_claim"]
    evidence_sha256: Sha256
    publicly_reproducible: bool
    summary: NonEmptyText


class PublicCounterexample(StrictFrozenModel):
    source: Literal[
        "public_challenge_test",
        "deterministic_probe",
        "bounded_search",
        "hy3_public_input_proposal",
        "minimized",
    ]
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = Field(default_factory=dict)
    expected: Any = None
    candidate_output: Any = None
    candidate_exception: NonEmptyText | None = None
    timed_out: bool = False
    minimized: bool = False
    public_source_sha256: Sha256
    replay_spec_sha256: Sha256
    execution_evidence_sha256: Sha256
    verified_in_restricted_sandbox: Literal[True] = True


class Phase3ErrorCertificate(StrictFrozenModel):
    schema_version: Literal[1] = 1
    certificate_id: Identifier
    trace_id: Identifier
    problem_id: Identifier
    verdict: Literal["confirmed_bug", "strongly_supported", "unverified_suspicion"]
    violated_requirement_id: Identifier
    violated_public_requirement: NonEmptyText
    first_faulty_layer: FaultyLayer
    first_faulty_step: Identifier | None = None
    code_span: NonEmptyText
    error_type: ErrorType
    supporting_evidence: tuple[EvidenceReference, ...] = Field(min_length=1)
    counterexample: PublicCounterexample | None = None
    replay_command: NonEmptyText | None = None
    frozen_manifest_sha256: Sha256
    code_sha256: Sha256
    structured_explanation_sha256: Sha256
    functional_evidence_sha256: Sha256

    @model_validator(mode="after")
    def enforce_public_certificate_levels(self) -> Self:
        reproducible = [item for item in self.supporting_evidence if item.publicly_reproducible]
        if self.verdict == "confirmed_bug":
            if self.counterexample is None or self.replay_command is None:
                raise ValueError(
                    "confirmed_bug requires a public counterexample and replay command"
                )
            if not any(
                item.evidence_kind == "public_execution"
                and item.publicly_reproducible
                and item.evidence_sha256 == self.counterexample.execution_evidence_sha256
                for item in self.supporting_evidence
            ):
                raise ValueError(
                    "confirmed_bug requires matching reproducible public execution evidence"
                )
        elif self.verdict == "strongly_supported":
            if self.counterexample is not None or self.replay_command is not None:
                raise ValueError("replayable counterexample must be classified as confirmed_bug")
            if not any(
                item.publicly_reproducible and item.evidence_kind == "ast_rule"
                for item in self.supporting_evidence
            ):
                raise ValueError("strongly_supported requires reproducible public static evidence")
        else:
            if self.counterexample is not None or self.replay_command is not None:
                raise ValueError("unverified suspicion cannot carry replay evidence")
            if reproducible:
                raise ValueError("reproducible public evidence cannot remain unverified suspicion")
        return self


class PublicCertificateClaim(StrictFrozenModel):
    claim_id: Identifier
    trace_id: Identifier
    problem_id: Identifier
    violated_requirement_id: Identifier
    first_faulty_layer: FaultyLayer
    first_faulty_step: Identifier | None = None
    code_span: NonEmptyText
    error_type: ErrorType
    claim_summary: NonEmptyText
    evidence_mode: Literal[
        "public_execution",
        "public_static_rule",
        "judge_claim_only",
    ]
    public_case_id: Identifier | None = None
    static_rule_id: Literal["empty_guard_return_literal_mismatch_v1"] | None = None
    expected_verdict: Literal[
        "confirmed_bug",
        "strongly_supported",
        "unverified_suspicion",
    ]

    @model_validator(mode="after")
    def validate_evidence_mode(self) -> Self:
        expected = {
            "public_execution": "confirmed_bug",
            "public_static_rule": "strongly_supported",
            "judge_claim_only": "unverified_suspicion",
        }[self.evidence_mode]
        if self.expected_verdict != expected:
            raise ValueError("certificate claim verdict disagrees with its evidence mode")
        if self.evidence_mode == "public_execution":
            if self.public_case_id is None or self.static_rule_id is not None:
                raise ValueError("public execution claim requires only a public case ID")
        elif self.evidence_mode == "public_static_rule":
            if self.static_rule_id is None or self.public_case_id is not None:
                raise ValueError("public static claim requires only a frozen static rule")
        elif self.public_case_id is not None or self.static_rule_id is not None:
            raise ValueError("judge-only claim may not declare reproducible public evidence")
        return self


class PublicCertificateClaimsBundle(StrictFrozenModel):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase3_public_certificate_claims"] = (
        "tracejudge_phase3_public_certificate_claims"
    )
    bundle_id: Identifier
    source: Literal["self_constructed_phase3_gate_d_engineering_fixture"]
    license: Literal["MIT"]
    claims: tuple[PublicCertificateClaim, ...] = Field(min_length=3)

    @model_validator(mode="after")
    def validate_claim_coverage(self) -> Self:
        claim_ids = [item.claim_id for item in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("public certificate claim IDs must be unique")
        if {item.evidence_mode for item in self.claims} != {
            "public_execution",
            "public_static_rule",
            "judge_claim_only",
        }:
            raise ValueError("public certificate fixture must cover all three evidence modes")
        return self


class CertificateArtifactReference(StrictFrozenModel):
    certificate_id: Identifier
    relative_path: NonEmptyText
    certificate_sha256: Sha256


class Phase3PublicCertificateManifest(StrictFrozenModel):
    schema_version: Literal[1] = 1
    phase: Literal["phase3_public_certificate_fixture"] = "phase3_public_certificate_fixture"
    status: Literal["completed"] = "completed"
    run_id: Identifier
    created_at: datetime
    frozen_manifest_sha256: Sha256
    natural_manifest_sha256: Sha256
    public_source_bundle_id: Identifier
    public_source_sha256: Sha256
    public_evidence_run_id: Identifier
    public_evidence_manifest_sha256: Sha256
    public_evidence_results_sha256: Sha256
    claims_bundle_id: Identifier
    claims_bundle_sha256: Sha256
    certificate_policy_sha256: Sha256
    ordered_certificate_ids: tuple[Identifier, ...]
    certificate_artifacts: tuple[CertificateArtifactReference, ...]
    certificate_count: int = Field(ge=1)
    confirmed_bug_count: int = Field(ge=0)
    strongly_supported_count: int = Field(ge=0)
    unverified_suspicion_count: int = Field(ge=0)
    certificate_payloads_sha256: Sha256
    execution_mode: Literal["reuse_validated_public_evidence_no_execution_v1"] = (
        "reuse_validated_public_evidence_no_execution_v1"
    )

    @model_validator(mode="after")
    def validate_certificate_manifest(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("public certificate timestamp must be timezone-aware")
        artifact_ids = tuple(item.certificate_id for item in self.certificate_artifacts)
        if artifact_ids != self.ordered_certificate_ids:
            raise ValueError("certificate artifact order differs from certificate IDs")
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("certificate IDs must be unique")
        if self.certificate_count != len(artifact_ids):
            raise ValueError("certificate count differs from artifact records")
        level_total = (
            self.confirmed_bug_count
            + self.strongly_supported_count
            + self.unverified_suspicion_count
        )
        if level_total != self.certificate_count:
            raise ValueError("certificate level counts do not cover all artifacts")
        relative_paths = [item.relative_path for item in self.certificate_artifacts]
        if len(relative_paths) != len(set(relative_paths)):
            raise ValueError("certificate artifact paths must be unique")
        return self


class AnnotationRecord(StrictFrozenModel):
    trace_id: Identifier
    code_sha256: Sha256
    structured_explanation_sha256: Sha256
    functional_evidence_sha256: Sha256
    annotation_protocol_sha256: Sha256
    rater_id: Identifier
    annotation_round: int = Field(ge=1)
    blinded_to_method_predictions: Literal[True] = True
    blinded_to_other_raters: bool
    process_correct: bool
    has_error: bool
    reasoning_correct: bool
    plan_code_aligned: bool
    first_faulty_layer: FaultyLayer | None = None
    first_faulty_step: Identifier | None = None
    error_type: ErrorType | None = None
    rationale: NonEmptyText

    @model_validator(mode="after")
    def validate_annotation_fault_fields(self) -> Self:
        if self.process_correct == self.has_error:
            raise ValueError("process_correct must be the complement of has_error")
        fault_fields = (self.first_faulty_layer, self.first_faulty_step, self.error_type)
        if not self.has_error:
            if any(value is not None for value in fault_fields):
                raise ValueError("no-error annotation may not retain fault labels")
            if not self.reasoning_correct or not self.plan_code_aligned:
                raise ValueError("no-error annotation requires correct reasoning and alignment")
        elif self.first_faulty_layer is None or self.error_type is None:
            raise ValueError("error annotation requires layer, error type, and rationale")
        return self


class AnnotationSetManifest(StrictFrozenModel):
    schema_version: Literal[2] = 2
    kind: Literal["tracejudge_phase3_annotation_set"] = "tracejudge_phase3_annotation_set"
    status: Literal["completed"] = "completed"
    annotation_set_id: Identifier
    annotation_protocol_sha256: Sha256
    annotation_guide_sha256: Sha256
    frozen_cohort_manifest_sha256: Sha256
    source_packet_id: Identifier
    source_packet_manifest_sha256: Sha256
    source_packet_sha256: Sha256
    source_identity_map_sha256: Sha256
    source_labels_template_sha256: Sha256
    source_completed_labels_sha256: Sha256
    completed_labels_sha256: Sha256
    ordered_trace_ids: tuple[Identifier, ...]
    record_count: int = Field(ge=1)
    natural_trace_count: int = Field(ge=0)
    counterfactual_trace_count: int = Field(ge=0)
    annotation_records_sha256: Sha256
    rater_ids: tuple[Identifier, ...]
    annotation_rounds: tuple[int, ...]
    agreement_kind: Literal["not_computed", "inter_rater", "intra_rater"]
    contains_method_predictions: Literal[False] = False
    contains_official_hidden_inputs: Literal[False] = False
    created_at: datetime

    @model_validator(mode="after")
    def validate_annotation_set(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("annotation manifest timestamp must be timezone-aware")
        if len(self.ordered_trace_ids) != len(set(self.ordered_trace_ids)):
            raise ValueError("annotation trace IDs must be unique")
        if len(self.rater_ids) != len(set(self.rater_ids)) or not self.rater_ids:
            raise ValueError("annotation set requires unique non-empty rater IDs")
        if (
            len(self.annotation_rounds) != len(set(self.annotation_rounds))
            or not self.annotation_rounds
            or any(value < 1 for value in self.annotation_rounds)
        ):
            raise ValueError("annotation set requires unique positive rounds")
        if self.natural_trace_count + self.counterfactual_trace_count != len(
            self.ordered_trace_ids
        ):
            raise ValueError("annotation source counts do not cover ordered traces")
        if self.agreement_kind == "not_computed" and (
            len(self.rater_ids) != 1
            or len(self.annotation_rounds) != 1
            or self.record_count != len(self.ordered_trace_ids)
        ):
            raise ValueError("single blind annotation set requires one full rater round")
        if self.agreement_kind == "inter_rater" and len(self.rater_ids) < 2:
            raise ValueError("inter-rater agreement requires at least two raters")
        if self.agreement_kind == "intra_rater" and (
            len(self.rater_ids) != 1 or len(self.annotation_rounds) < 2
        ):
            raise ValueError("intra-rater consistency requires one rater and at least two rounds")
        return self


class Phase3ResumeIdentity(StrictFrozenModel):
    frozen_manifest_sha256: Sha256
    natural_manifest_sha256: Sha256
    ordered_trace_ids_sha256: Sha256
    material_payloads_sha256: Sha256
    method_specs_sha256: Sha256
    prompt_bundle_sha256: Sha256
    output_schema_sha256: Sha256
    implementation_sha256: Sha256
    provider_config_sha256: Sha256
    annotation_set_manifest_sha256: Sha256
    completed_labels_sha256: Sha256
    annotation_records_sha256: Sha256
    git_commit: GitCommit
    git_branch: NonEmptyText
    git_dirty: bool
    git_worktree_fingerprint: Sha256 | None = None
    python_version: NonEmptyText
    direct_dependencies_sha256: Sha256
    ast_implementation_sha256: Sha256
    public_evidence_policy_sha256: Sha256
    annotation_protocol_sha256: Sha256
    random_seed: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_git_identity(self) -> Self:
        if self.git_dirty != (self.git_worktree_fingerprint is not None):
            raise ValueError("dirty Git state requires exactly one worktree fingerprint")
        return self


class Phase3Invocation(StrictFrozenModel):
    invocation_id: Identifier
    resume: bool
    status: InvocationStatus
    resume_identity_sha256: Sha256
    started_at: datetime
    ended_at: datetime | None = None

    @model_validator(mode="after")
    def validate_invocation_lifecycle(self) -> Self:
        if self.started_at.tzinfo is None:
            raise ValueError("invocation started_at must be timezone-aware")
        if self.status == InvocationStatus.RUNNING:
            if self.ended_at is not None:
                raise ValueError("running invocation may not have ended_at")
        else:
            if self.ended_at is None or self.ended_at.tzinfo is None:
                raise ValueError("terminal invocation requires timezone-aware ended_at")
            if self.ended_at < self.started_at:
                raise ValueError("invocation ended_at precedes started_at")
        return self


class Phase3RunManifest(StrictFrozenModel):
    schema_version: Literal[1] = 1
    phase: Literal["phase3_paired_process_evaluation"] = "phase3_paired_process_evaluation"
    run_id: Identifier
    status: Literal["running", "completed"]
    created_at: datetime
    completed_at: datetime | None = None
    frozen_manifest_sha256: Sha256
    resume_identity: Phase3ResumeIdentity
    resume_identity_sha256: Sha256
    invocations: tuple[Phase3Invocation, ...]

    @model_validator(mode="after")
    def validate_run_lifecycle(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("phase-three run created_at must be timezone-aware")
        if not self.invocations:
            raise ValueError("phase-three run requires at least one invocation")
        invocation_ids = [item.invocation_id for item in self.invocations]
        if len(invocation_ids) != len(set(invocation_ids)):
            raise ValueError("phase-three invocation IDs must be unique")
        if self.invocations[0].resume:
            raise ValueError("first phase-three invocation may not be a resume")
        if any(not item.resume for item in self.invocations[1:]):
            raise ValueError("later phase-three invocations must be resumes")
        if any(
            item.resume_identity_sha256 != self.resume_identity_sha256 for item in self.invocations
        ):
            raise ValueError("invocation resume identity differs from run manifest")
        for previous, current in zip(self.invocations[:-1], self.invocations[1:], strict=True):
            if previous.status == InvocationStatus.RUNNING:
                raise ValueError("only the final invocation may remain running")
            if previous.ended_at is not None and current.started_at < previous.ended_at:
                raise ValueError("phase-three invocations overlap or are out of order")

        final = self.invocations[-1]
        if self.status == "running":
            if self.completed_at is not None or final.status != InvocationStatus.RUNNING:
                raise ValueError("running phase-three run requires one final running invocation")
        else:
            if self.completed_at is None or self.completed_at.tzinfo is None:
                raise ValueError("completed phase-three run requires completed_at")
            if final.status != InvocationStatus.COMPLETED:
                raise ValueError("completed phase-three run requires final completed invocation")
            if self.completed_at < self.created_at:
                raise ValueError("phase-three completed_at precedes created_at")
        return self


class PairedMethodResultReference(StrictFrozenModel):
    trace_id: Identifier
    method_id: MethodId
    status: MethodOutcomeStatus
    result_line_number: int = Field(ge=1)
    result_record_sha256: Sha256


class PairedEvaluationIndex(StrictFrozenModel):
    """Final trace-major index proving that every method saw the same cohort."""

    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase3_paired_evaluation_index"] = (
        "tracejudge_phase3_paired_evaluation_index"
    )
    run_id: Identifier
    frozen_manifest_sha256: Sha256
    resume_identity_sha256: Sha256
    ordered_trace_ids: tuple[Identifier, ...]
    ordered_method_ids: tuple[MethodId, ...]
    result_references: tuple[PairedMethodResultReference, ...]
    results_sha256: Sha256

    @model_validator(mode="after")
    def validate_complete_cartesian_product(self) -> Self:
        if not self.ordered_trace_ids:
            raise ValueError("paired evaluation requires at least one frozen trace")
        if len(self.ordered_trace_ids) != len(set(self.ordered_trace_ids)):
            raise ValueError("paired evaluation trace IDs must be unique")
        if tuple(self.ordered_method_ids) != tuple(MethodId):
            raise ValueError("paired evaluation method order must contain the five frozen methods")
        expected_pairs = [
            (trace_id, method_id)
            for trace_id in self.ordered_trace_ids
            for method_id in self.ordered_method_ids
        ]
        actual_pairs = [
            (reference.trace_id, reference.method_id) for reference in self.result_references
        ]
        if actual_pairs != expected_pairs:
            raise ValueError(
                "paired result index must be the complete trace-major trace/method product"
            )
        line_numbers = [item.result_line_number for item in self.result_references]
        if line_numbers != list(range(1, len(self.result_references) + 1)):
            raise ValueError("paired result line numbers must be contiguous and trace-major")
        return self


class FrozenCohortManifest(StrictFrozenModel):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase3_frozen_cohort"] = "tracejudge_phase3_frozen_cohort"
    freeze_id: Identifier
    experiment_label: Identifier
    created_at: datetime
    dataset: ResearchDatasetIdentity
    phase1: Phase1BundleIdentity
    phase2: Phase2BundleIdentity
    selection_rule: SelectionRule
    source_accounting: SourceAccounting
    source_outcomes: tuple[SourceOutcome, ...]
    traces: tuple[FrozenTrace, ...]
    ordered_trace_ids: tuple[Identifier, ...]
    paired_method_ids: tuple[MethodId, ...]
    privacy_policy_version: Identifier

    @model_validator(mode="after")
    def validate_frozen_cohort(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("frozen cohort timestamp must be timezone-aware")
        if len(self.source_outcomes) != self.source_accounting.source_problem_count:
            raise ValueError("source outcome rows differ from source_problem_count")
        if self.dataset.source_problem_count != self.source_accounting.source_problem_count:
            raise ValueError("dataset and source accounting problem counts differ")
        source_ids = [item.problem_id for item in self.source_outcomes]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source problem IDs must be unique")
        status_counts = {
            status: sum(item.final_status == status for item in self.source_outcomes)
            for status in ("success", "parse_error", "provider_error")
        }
        if status_counts != {
            "success": self.source_accounting.success_count,
            "parse_error": self.source_accounting.parse_error_count,
            "provider_error": self.source_accounting.provider_error_count,
        }:
            raise ValueError("source accounting differs from source outcome rows")

        trace_ids = [trace.trace_id for trace in self.traces]
        if len(trace_ids) != len(set(trace_ids)):
            raise ValueError("frozen trace IDs must be unique")
        if tuple(trace_ids) != self.ordered_trace_ids:
            raise ValueError("ordered_trace_ids must exactly match trace record order")
        traces_by_id = {trace.trace_id: trace for trace in self.traces}
        natural = [trace for trace in self.traces if isinstance(trace, NaturalTrace)]
        if len(natural) != self.source_accounting.included_natural_trace_count:
            raise ValueError("included natural trace count differs from frozen records")
        if not (
            self.selection_rule.minimum_natural_count
            <= len(natural)
            <= self.selection_rule.maximum_natural_count
        ):
            raise ValueError("frozen natural trace count violates the selection rule")

        included_by_problem = {
            item.problem_id: item.included_trace_id
            for item in self.source_outcomes
            if item.included_trace_id is not None
        }
        if len(included_by_problem) != len(natural):
            raise ValueError("source inclusion rows differ from natural trace records")
        for problem_id, trace_id in included_by_problem.items():
            included_trace = traces_by_id.get(trace_id)
            if not isinstance(included_trace, NaturalTrace):
                raise ValueError("source outcome must reference a natural trace")
            if included_trace.problem_id != problem_id:
                raise ValueError("source outcome problem_id differs from included trace")
        for trace in natural:
            if trace.phase1_response.phase1_run_id != self.phase1.run_id:
                raise ValueError("natural trace references a different phase-one run")
            if included_by_problem.get(trace.problem_id) != trace.trace_id:
                raise ValueError("natural trace differs from source outcome inclusion")
        if self.selection_rule.policy == "all_phase1_successes":
            successful = {
                item.problem_id for item in self.source_outcomes if item.final_status == "success"
            }
            if successful != set(included_by_problem):
                raise ValueError("all-success policy must include every phase-one success")

        for trace in self.traces:
            evidence = trace.functional_evidence
            if isinstance(evidence, Phase2FunctionalEvidenceRef):
                if evidence.phase2_run_id != self.phase2.run_id:
                    raise ValueError("trace references a different phase-two run")
            if isinstance(trace, CounterfactualTrace):
                parent = traces_by_id.get(trace.parent_trace_id)
                if parent is None:
                    raise ValueError("counterfactual parent is absent from frozen cohort")
                mutation = trace.mutation
                if mutation.before_solution_trace_sha256 != parent.solution_trace_sha256:
                    raise ValueError("counterfactual before solution hash differs from parent")
                if (
                    mutation.before_structured_explanation_sha256
                    != parent.structured_explanation_sha256
                ):
                    raise ValueError("counterfactual before explanation hash differs from parent")
                if mutation.before_code_sha256 != parent.code_sha256:
                    raise ValueError("counterfactual before code hash differs from parent")
                if trace.problem_id != parent.problem_id:
                    raise ValueError("counterfactual problem_id differs from parent")
                if trace.public_problem_sha256 != parent.public_problem_sha256:
                    raise ValueError("counterfactual public problem hash differs from parent")

        if tuple(self.paired_method_ids) != tuple(MethodId):
            raise ValueError("frozen cohort must declare all five paired methods in order")
        return self


class PublicFixtureRequirement(StrictFrozenModel):
    requirement_id: Identifier
    content: NonEmptyText
    verification_hint: NonEmptyText | None = None


class PublicFixtureCase(StrictFrozenModel):
    case_id: Identifier
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = Field(default_factory=dict)
    expected: Any
    category: Literal["visible", "challenge"]
    related_requirements: tuple[Identifier, ...]


class PublicFixtureDefinition(StrictFrozenModel):
    public_fixture_id: Identifier
    problem_id: Identifier
    title: NonEmptyText
    requirement: NonEmptyText
    function_signature: NonEmptyText
    requirements: tuple[PublicFixtureRequirement, ...]
    test_cases: tuple[PublicFixtureCase, ...]
    source: Literal["self_constructed_phase3_public_fixture"]
    license: Literal["MIT"]

    @model_validator(mode="after")
    def validate_public_fixture(self) -> Self:
        requirement_ids = [item.requirement_id for item in self.requirements]
        if not requirement_ids or len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("public fixture requirements must be non-empty and unique")
        case_ids = [item.case_id for item in self.test_cases]
        if not case_ids or len(case_ids) != len(set(case_ids)):
            raise ValueError("public fixture cases must be non-empty and unique")
        categories = {item.category for item in self.test_cases}
        if categories != {"visible", "challenge"}:
            raise ValueError("public fixture must contain visible and challenge cases")
        known_requirements = set(requirement_ids)
        for case in self.test_cases:
            if not case.related_requirements:
                raise ValueError("every public fixture case must bind a requirement")
            if set(case.related_requirements) - known_requirements:
                raise ValueError("public fixture case references an unknown requirement")
        return self


class PublicCounterfactualParentSource(StrictFrozenModel):
    parent_trace_id: Identifier
    fixture: PublicFixtureDefinition
    solution_trace: SolutionTrace

    @model_validator(mode="after")
    def bind_parent_problem(self) -> Self:
        if self.solution_trace.problem_id != self.fixture.problem_id:
            raise ValueError("public parent solution problem_id differs from fixture")
        return self


class PublicCounterfactualVariantSource(StrictFrozenModel):
    trace_id: Identifier
    parent_trace_id: Identifier
    mutation_kind: CounterfactualKind
    sole_change: NonEmptyText
    expected_impact: NonEmptyText
    expected_execution_status: Literal["pass", "fail"]
    solution_trace: SolutionTrace


class PublicCounterfactualSourceBundle(StrictFrozenModel):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase3_public_counterfactual_source"] = (
        "tracejudge_phase3_public_counterfactual_source"
    )
    bundle_id: Identifier
    selection_policy: Literal["five_types_type_major_parent_order_v1"]
    parents: tuple[PublicCounterfactualParentSource, ...]
    counterfactuals: tuple[PublicCounterfactualVariantSource, ...]

    @model_validator(mode="after")
    def validate_source_bundle(self) -> Self:
        parent_ids = [item.parent_trace_id for item in self.parents]
        if len(parent_ids) < 2 or len(parent_ids) != len(set(parent_ids)):
            raise ValueError("public counterfactual parents must be unique and cover two tasks")
        fixture_ids = [item.fixture.public_fixture_id for item in self.parents]
        problem_ids = [item.fixture.problem_id for item in self.parents]
        if len(fixture_ids) != len(set(fixture_ids)) or len(problem_ids) != len(set(problem_ids)):
            raise ValueError("public counterfactual fixtures and problems must be unique")

        trace_ids = [item.trace_id for item in self.counterfactuals]
        if len(trace_ids) != len(set(trace_ids)):
            raise ValueError("public counterfactual trace IDs must be unique")
        if set(trace_ids) & set(parent_ids):
            raise ValueError("counterfactual trace IDs must differ from parent trace IDs")
        if len({item.sole_change for item in self.counterfactuals}) != len(self.counterfactuals):
            raise ValueError("every counterfactual must declare a unique sole_change")

        parents_by_id = {item.parent_trace_id: item for item in self.parents}
        kind_counts = {kind: 0 for kind in CounterfactualKind}
        seen_parent_kind: set[tuple[str, CounterfactualKind]] = set()
        actual_order: list[tuple[CounterfactualKind, str]] = []

        parent_codes = {item.solution_trace.code for item in self.parents}
        changed_codes: set[str] = set()
        for variant in self.counterfactuals:
            parent = parents_by_id.get(variant.parent_trace_id)
            if parent is None:
                raise ValueError("counterfactual references an unknown public parent")
            if variant.solution_trace.problem_id != parent.fixture.problem_id:
                raise ValueError("counterfactual solution problem_id differs from parent")
            pair = (variant.parent_trace_id, variant.mutation_kind)
            if pair in seen_parent_kind:
                raise ValueError("a parent may have at most one variant of each type")
            seen_parent_kind.add(pair)
            kind_counts[variant.mutation_kind] += 1
            actual_order.append((variant.mutation_kind, variant.parent_trace_id))

            parent_explanation = parent.solution_trace.model_dump(mode="json", exclude={"code"})
            variant_explanation = variant.solution_trace.model_dump(mode="json", exclude={"code"})
            if variant.mutation_kind == CounterfactualKind.REASONING_SWAP:
                if variant.solution_trace.code != parent.solution_trace.code:
                    raise ValueError("reasoning source variant must preserve parent code")
                if variant_explanation == parent_explanation:
                    raise ValueError("reasoning source variant must change the explanation")
                if variant.expected_execution_status != "pass":
                    raise ValueError("same-code reasoning variant must expect a passing execution")
            else:
                if variant.solution_trace.code == parent.solution_trace.code:
                    raise ValueError("code source variant must change parent code")
                if variant_explanation != parent_explanation:
                    raise ValueError("code source variant must preserve the parent explanation")
                if (
                    variant.solution_trace.code in parent_codes
                    or variant.solution_trace.code in changed_codes
                ):
                    raise ValueError("every changed-code variant must have unique code bytes")
                changed_codes.add(variant.solution_trace.code)
                expected = (
                    "pass"
                    if variant.mutation_kind == CounterfactualKind.EQUIVALENT_IMPLEMENTATION
                    else "fail"
                )
                if variant.expected_execution_status != expected:
                    raise ValueError("expected execution status disagrees with mutation type")

        if any(count < 2 or count > 3 for count in kind_counts.values()):
            raise ValueError("each counterfactual type must contain two or three variants")
        if not 10 <= len(self.counterfactuals) <= 15:
            raise ValueError("counterfactual source must contain between 10 and 15 variants")
        expected_order = [
            (kind, parent_id)
            for kind in CounterfactualKind
            for parent_id in parent_ids[: kind_counts[kind]]
        ]
        if actual_order != expected_order:
            raise ValueError("counterfactuals must use fixed type-major parent order")
        return self


class PublicFixtureExecutionSubject(StrictFrozenModel):
    execution_subject_id: Identifier
    problem_id: Identifier
    public_fixture_id: Identifier
    public_fixture_sha256: Sha256
    code_sha256: Sha256
    replay_spec_sha256: Sha256
    expected_execution_status: Literal["pass", "fail"]


class PublicFixtureExecutionCaseResult(StrictFrozenModel):
    case_id: Identifier
    category: Literal["visible", "challenge"]
    passed: bool
    actual_output: Any = None
    expected_output: Any = None
    exception_type: NonEmptyText | None = None
    timed_out: bool = False
    related_requirements: tuple[Identifier, ...]


class PublicFixtureExecutionResult(StrictFrozenModel):
    schema_version: Literal[1] = 1
    run_id: Identifier
    execution_subject_id: Identifier
    problem_id: Identifier
    public_fixture_id: Identifier
    public_fixture_sha256: Sha256
    code_sha256: Sha256
    replay_spec_sha256: Sha256
    execution_status: Literal["pass", "fail", "timeout", "infrastructure_error"]
    expected_execution_status: Literal["pass", "fail"]
    expectation_met: bool
    case_count: int = Field(ge=0)
    pass_count: int = Field(ge=0)
    fail_count: int = Field(ge=0)
    timeout_count: int = Field(ge=0)
    case_results: tuple[PublicFixtureExecutionCaseResult, ...]

    @model_validator(mode="after")
    def validate_execution_result(self) -> Self:
        if len(self.case_results) != self.case_count:
            raise ValueError("public execution case_count differs from result rows")
        if self.pass_count != sum(item.passed for item in self.case_results):
            raise ValueError("public execution pass_count differs from result rows")
        if self.fail_count != self.case_count - self.pass_count:
            raise ValueError("public execution fail_count differs from result rows")
        if self.timeout_count != sum(item.timed_out for item in self.case_results):
            raise ValueError("public execution timeout_count differs from result rows")
        if self.execution_status != "infrastructure_error" and not self.case_results:
            raise ValueError("functional public execution must contain case results")
        if self.execution_status == "pass":
            if self.fail_count or self.timeout_count:
                raise ValueError("passing public execution cannot contain failures")
        elif self.execution_status == "timeout":
            if not self.timeout_count:
                raise ValueError("timeout public execution must contain a timed-out case")
        elif self.execution_status == "fail":
            if not self.fail_count or self.timeout_count:
                raise ValueError("failed public execution must have non-timeout failures")
        elif self.case_results or self.case_count:
            raise ValueError("infrastructure_error cannot carry case results")
        expected_met = self.execution_status == self.expected_execution_status
        if self.expectation_met != expected_met:
            raise ValueError("public execution expectation_met is inconsistent")
        return self


class PublicFixtureExecutionManifest(StrictFrozenModel):
    schema_version: Literal[1] = 1
    phase: Literal["phase3_public_fixture_execution"] = "phase3_public_fixture_execution"
    status: Literal["completed", "completed_with_expectation_mismatch"]
    run_id: Identifier
    created_at: datetime
    execution_mode: Literal["trusted_local_exact_public_allowlist_v1"]
    source_bundle_id: Identifier
    source_bundle_sha256: Sha256
    ordered_subjects: tuple[PublicFixtureExecutionSubject, ...]
    result_count: int = Field(ge=1)
    expectation_mismatch_count: int = Field(ge=0)
    results_sha256: Sha256

    @model_validator(mode="after")
    def validate_execution_manifest(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("public execution timestamp must be timezone-aware")
        subject_ids = [item.execution_subject_id for item in self.ordered_subjects]
        if len(subject_ids) != len(set(subject_ids)):
            raise ValueError("public execution subjects must be unique")
        if len(subject_ids) != self.result_count:
            raise ValueError("public execution result_count differs from subjects")
        expected_status = (
            "completed"
            if self.expectation_mismatch_count == 0
            else "completed_with_expectation_mismatch"
        )
        if self.status != expected_status:
            raise ValueError("public execution status disagrees with mismatch count")
        return self


class NaturalCohortReference(StrictFrozenModel):
    freeze_id: Identifier
    manifest_sha256: Sha256
    natural_trace_count: int = Field(ge=30, le=45)
    ordered_trace_ids: tuple[Identifier, ...]
    ordered_trace_ids_sha256: Sha256

    @model_validator(mode="after")
    def validate_natural_reference(self) -> Self:
        if len(self.ordered_trace_ids) != self.natural_trace_count:
            raise ValueError("natural trace count differs from referenced order")
        if len(self.ordered_trace_ids) != len(set(self.ordered_trace_ids)):
            raise ValueError("referenced natural trace IDs must be unique")
        if self.ordered_trace_ids_sha256 != _canonical_sequence_sha256(self.ordered_trace_ids):
            raise ValueError("referenced natural trace order hash is inconsistent")
        return self


class PublicCounterfactualSourceIdentity(StrictFrozenModel):
    bundle_id: Identifier
    source_bundle_sha256: Sha256
    source: Literal["self_constructed_phase3_public_fixture"]
    license: Literal["MIT"]
    parent_count: int = Field(ge=2)
    counterfactual_count: int = Field(ge=10, le=15)


class PublicFixtureExecutionBundleIdentity(StrictFrozenModel):
    run_id: Identifier
    manifest_sha256: Sha256
    results_sha256: Sha256
    source_bundle_sha256: Sha256
    result_count: int = Field(ge=1)
    execution_mode: Literal["trusted_local_exact_public_allowlist_v1"]


class CounterfactualParentSnapshot(FrozenTraceBase):
    trace_kind: Literal["counterfactual_parent_snapshot"] = "counterfactual_parent_snapshot"
    public_fixture_id: Identifier


class CounterfactualSelectionRule(StrictFrozenModel):
    rule_id: Literal["five_types_type_major_parent_order_v1"]
    minimum_per_type: Literal[2] = 2
    target_per_type: Literal[3] = 3
    actual_per_type: dict[CounterfactualKind, int]
    stop_rule: NonEmptyText
    frozen_before_method_predictions: Literal[True] = True
    frozen_before_human_labels: Literal[True] = True

    @model_validator(mode="after")
    def validate_counterfactual_quotas(self) -> Self:
        if set(self.actual_per_type) != set(CounterfactualKind):
            raise ValueError("counterfactual quota map must contain all five types")
        if any(
            value < self.minimum_per_type or value > self.target_per_type
            for value in self.actual_per_type.values()
        ):
            raise ValueError("counterfactual quota must be between minimum and target")
        return self


class CounterfactualCohortManifest(StrictFrozenModel):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase3_counterfactual_cohort_overlay"] = (
        "tracejudge_phase3_counterfactual_cohort_overlay"
    )
    freeze_id: Identifier
    experiment_label: Identifier
    created_at: datetime
    natural_cohort: NaturalCohortReference
    source: PublicCounterfactualSourceIdentity
    execution: PublicFixtureExecutionBundleIdentity
    selection_rule: CounterfactualSelectionRule
    parents: tuple[CounterfactualParentSnapshot, ...]
    counterfactuals: tuple[CounterfactualTrace, ...]
    ordered_counterfactual_trace_ids: tuple[Identifier, ...]
    paired_ordered_trace_ids: tuple[Identifier, ...]
    paired_ordered_trace_ids_sha256: Sha256
    paired_method_ids: tuple[MethodId, ...]
    privacy_policy_version: Identifier

    @model_validator(mode="after")
    def validate_counterfactual_overlay(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("counterfactual cohort timestamp must be timezone-aware")
        if self.source.source_bundle_sha256 != self.execution.source_bundle_sha256:
            raise ValueError("counterfactual source and execution bundle hashes differ")
        if len(self.parents) != self.source.parent_count:
            raise ValueError("counterfactual parent count differs from source identity")
        if len(self.counterfactuals) != self.source.counterfactual_count:
            raise ValueError("counterfactual count differs from source identity")

        parent_ids = [item.trace_id for item in self.parents]
        if len(parent_ids) != len(set(parent_ids)):
            raise ValueError("counterfactual parent snapshot IDs must be unique")
        parents_by_id = {item.trace_id: item for item in self.parents}
        trace_ids = [item.trace_id for item in self.counterfactuals]
        if tuple(trace_ids) != self.ordered_counterfactual_trace_ids:
            raise ValueError("ordered counterfactual IDs must match record order")
        if len(trace_ids) != len(set(trace_ids)) or set(trace_ids) & set(parent_ids):
            raise ValueError("counterfactual trace IDs must be unique from parents")

        expected_paired = self.natural_cohort.ordered_trace_ids + tuple(trace_ids)
        if self.paired_ordered_trace_ids != expected_paired:
            raise ValueError("paired order must be natural traces followed by counterfactuals")
        if self.paired_ordered_trace_ids_sha256 != _canonical_sequence_sha256(
            self.paired_ordered_trace_ids
        ):
            raise ValueError("paired trace order hash is inconsistent")
        if len(expected_paired) < 40 or len(expected_paired) > 60:
            raise ValueError("combined research cohort must contain 40 to 60 traces")
        if tuple(self.paired_method_ids) != tuple(MethodId):
            raise ValueError("counterfactual overlay must declare all five methods in order")

        actual_counts = {kind: 0 for kind in CounterfactualKind}
        for trace in self.counterfactuals:
            parent = parents_by_id.get(trace.parent_trace_id)
            if parent is None:
                raise ValueError("counterfactual parent snapshot is absent")
            mutation = trace.mutation
            if trace.problem_id != parent.problem_id:
                raise ValueError("counterfactual problem_id differs from parent snapshot")
            if trace.public_problem_sha256 != parent.public_problem_sha256:
                raise ValueError("counterfactual public problem hash differs from parent snapshot")
            if mutation.before_solution_trace_sha256 != parent.solution_trace_sha256:
                raise ValueError("counterfactual before solution hash differs from parent snapshot")
            if (
                mutation.before_structured_explanation_sha256
                != parent.structured_explanation_sha256
            ):
                raise ValueError(
                    "counterfactual before explanation hash differs from parent snapshot"
                )
            if mutation.before_code_sha256 != parent.code_sha256:
                raise ValueError("counterfactual before code hash differs from parent snapshot")
            evidence = trace.functional_evidence
            if not isinstance(evidence, PublicFixtureFunctionalEvidenceRef):
                raise ValueError("public fixture counterfactual must use public fixture evidence")
            if evidence.public_fixture_id != parent.public_fixture_id:
                raise ValueError("counterfactual evidence fixture differs from parent snapshot")
            if mutation.mutation_kind == CounterfactualKind.REASONING_SWAP:
                if evidence.execution_subject_id != parent.trace_id:
                    raise ValueError("reasoning counterfactual must reuse parent execution subject")
                if evidence != parent.functional_evidence:
                    raise ValueError("reasoning counterfactual must reuse exact parent evidence")
            else:
                if mutation.evidence_strategy != EvidenceStrategy.INDEPENDENT_PUBLIC_FIXTURE:
                    raise ValueError("changed public code must use independent fixture evidence")
                if evidence.execution_subject_id != trace.trace_id:
                    raise ValueError("changed code evidence must be executed for this trace")
            actual_counts[mutation.mutation_kind] += 1
        if actual_counts != self.selection_rule.actual_per_type:
            raise ValueError("counterfactual records differ from frozen type quotas")
        return self
