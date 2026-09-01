"""Strict public/private contracts for phase-four reproducibility artifacts."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
GitObjectId = Annotated[str, StringConstraints(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
RelativePath = Annotated[
    str,
    StringConstraints(min_length=1, max_length=4096),
]
OctalMode = Annotated[str, StringConstraints(pattern=r"^0[0-7]{3}$")]


class Phase4Contract(BaseModel):
    """Base contract with fail-closed parsing and immutable values."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Phase4GitIdentity(Phase4Contract):
    commit: GitObjectId
    branch: str = Field(min_length=1, max_length=255)
    dirty: bool


class ArtifactInventoryEntry(Phase4Contract):
    artifact_id: Identifier
    relative_path: RelativePath
    privacy_class: Literal["private_restricted", "deidentified_aggregate", "public_fixture"]
    size_bytes: int = Field(ge=0)
    mode_octal: OctalMode
    sha256: Sha256
    permission_warning: bool = False

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        parts = value.split("/")
        if value.startswith("/") or "\\" in value or any(part in {"", ".", ".."} for part in parts):
            raise ValueError("artifact path must be normalized and repository-relative")
        return value


class Phase4ArtifactInventory(Phase4Contract):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase4_private_artifact_inventory"] = (
        "tracejudge_phase4_private_artifact_inventory"
    )
    inventory_id: Identifier
    created_at: datetime
    source_git: Phase4GitIdentity
    artifact_set_sha256: Sha256
    artifact_count: int = Field(ge=1)
    permission_warning_count: int = Field(ge=0)
    artifacts: tuple[ArtifactInventoryEntry, ...]
    paths_are_repo_relative: Literal[True] = True
    artifact_content_parsed: Literal[False] = False
    includes_credentials: Literal[False] = False

    @model_validator(mode="after")
    def validate_accounting(self) -> Phase4ArtifactInventory:
        if self.artifact_count != len(self.artifacts):
            raise ValueError("artifact count differs from inventory entries")
        if self.permission_warning_count != sum(item.permission_warning for item in self.artifacts):
            raise ValueError("permission warning count differs from inventory entries")
        artifact_ids = [item.artifact_id for item in self.artifacts]
        relative_paths = [item.relative_path for item in self.artifacts]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("artifact IDs must be unique")
        if len(relative_paths) != len(set(relative_paths)):
            raise ValueError("artifact paths must be unique")
        return self


class PublicArtifactAnchor(Phase4Contract):
    artifact_id: Identifier
    sha256: Sha256
    size_bytes: int = Field(ge=0)


class Phase4PublicArtifactDigest(Phase4Contract):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase4_public_artifact_digest"] = (
        "tracejudge_phase4_public_artifact_digest"
    )
    digest_id: Identifier
    inventory_id: Identifier
    created_at: datetime
    source_git: Phase4GitIdentity
    artifact_set_sha256: Sha256
    private_inventory_sha256: Sha256
    private_artifact_count: int = Field(ge=1)
    permission_warning_count: int = Field(ge=0)
    public_anchor_count: int = Field(ge=1)
    public_anchors: tuple[PublicArtifactAnchor, ...]
    contains_absolute_paths: Literal[False] = False
    contains_private_relative_paths: Literal[False] = False
    contains_artifact_content: Literal[False] = False
    contains_annotation_records: Literal[False] = False
    contains_per_trace_predictions: Literal[False] = False
    contains_provider_raw: Literal[False] = False
    contains_hidden_evaluation_content: Literal[False] = False
    privacy_review_status: Literal["passed", "permission_hardening_required"]

    @model_validator(mode="after")
    def validate_accounting(self) -> Phase4PublicArtifactDigest:
        if self.public_anchor_count != len(self.public_anchors):
            raise ValueError("public anchor count differs from digest entries")
        artifact_ids = [item.artifact_id for item in self.public_anchors]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("public artifact anchor IDs must be unique")
        expected_status = (
            "permission_hardening_required" if self.permission_warning_count else "passed"
        )
        if self.privacy_review_status != expected_status:
            raise ValueError("privacy review status differs from permission warnings")
        return self


class Phase4ReplaySafety(Phase4Contract):
    provider_call_count: Literal[0] = 0
    docker_call_count: Literal[0] = 0
    network_call_count: Literal[0] = 0
    executed_public_case_count: Literal[1] = 1
    exact_public_allowlist: Literal[True] = True
    contains_candidate_source: Literal[False] = False
    contains_counterexample_inputs: Literal[False] = False
    contains_provider_raw: Literal[False] = False
    contains_hidden_evaluation_content: Literal[False] = False
    contains_per_trace_predictions: Literal[False] = False


class Phase4ReplayRuntime(Phase4Contract):
    python_version: str = Field(min_length=1, max_length=64)
    sandbox_backend: Literal["trusted-local"] = "trusted-local"
    replay_implementation_sha256: Sha256
    direct_dependencies_sha256: Sha256


class Phase4PublicReplayReceipt(Phase4Contract):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase4_public_replay_receipt"] = (
        "tracejudge_phase4_public_replay_receipt"
    )
    receipt_id: Identifier
    replay_started_at: datetime
    replay_completed_at: datetime
    source_git: Phase4GitIdentity
    certificate_id: Identifier
    trace_id: Identifier
    problem_id: Identifier
    certificate_sha256: Sha256
    certificate_manifest_sha256: Sha256
    cohort_manifest_sha256: Sha256
    natural_manifest_sha256: Sha256
    public_source_sha256: Sha256
    execution_evidence_sha256: Sha256
    reproduced_failure: Literal[True] = True
    evidence_hash_verified: Literal[True] = True
    replay_command: str = Field(min_length=1, max_length=4096)
    runtime: Phase4ReplayRuntime
    safety: Phase4ReplaySafety

    @model_validator(mode="after")
    def validate_timing(self) -> Phase4PublicReplayReceipt:
        if self.replay_completed_at < self.replay_started_at:
            raise ValueError("replay completion precedes replay start")
        return self


ChartMethodId = Literal[
    "test_only",
    "direct_llm_judge",
    "four_layer_structured_judge",
    "four_layer_ast",
    "full_tracejudge",
]


class Phase4PublicChartProportion(Phase4Contract):
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=1)
    estimate: float = Field(ge=0.0, le=1.0)
    interval_kind: Literal["wilson_95", "descriptive_only"]
    interval_lower: float | None = Field(default=None, ge=0.0, le=1.0)
    interval_upper: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_proportion(self) -> Phase4PublicChartProportion:
        if self.numerator > self.denominator or not math.isclose(
            self.estimate,
            self.numerator / self.denominator,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("chart proportion differs from its counts")
        if self.interval_kind == "wilson_95":
            if self.interval_lower is None or self.interval_upper is None:
                raise ValueError("Wilson interval bounds are required")
            if self.interval_lower > self.interval_upper or not (
                self.interval_lower - 1e-12 <= self.estimate <= self.interval_upper + 1e-12
            ):
                raise ValueError("Wilson interval does not contain the estimate")
        elif self.interval_lower is not None or self.interval_upper is not None:
            raise ValueError("descriptive-only proportions must not carry interval bounds")
        return self


class Phase4PublicConfusionSummary(Phase4Contract):
    valid_prediction_count: int = Field(ge=1)
    true_positive: int = Field(ge=0)
    true_negative: int = Field(ge=0)
    false_positive: int = Field(ge=0)
    false_negative: int = Field(ge=0)
    precision: Phase4PublicChartProportion
    recall: Phase4PublicChartProportion
    f1: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_confusion(self) -> Phase4PublicConfusionSummary:
        if (
            self.true_positive + self.true_negative + self.false_positive + self.false_negative
            != self.valid_prediction_count
        ):
            raise ValueError("confusion counts differ from the valid prediction count")
        if (
            self.precision.numerator != self.true_positive
            or self.precision.denominator != self.true_positive + self.false_positive
            or self.recall.numerator != self.true_positive
            or self.recall.denominator != self.true_positive + self.false_negative
        ):
            raise ValueError("precision or recall counts differ from the confusion matrix")
        expected_f1 = 0.0
        if self.precision.estimate + self.recall.estimate:
            expected_f1 = (
                2.0
                * self.precision.estimate
                * self.recall.estimate
                / (self.precision.estimate + self.recall.estimate)
            )
        if not math.isclose(self.f1, expected_f1, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("F1 differs from precision and recall")
        return self


class Phase4PublicMethodChartSummary(Phase4Contract):
    method_id: ChartMethodId
    display_name: str = Field(min_length=1, max_length=64)
    judgment_availability: Phase4PublicChartProportion
    provider_error_count: int = Field(ge=0)
    accuracy_all: Phase4PublicChartProportion
    accuracy_natural: Phase4PublicChartProportion
    accuracy_counterfactual: Phase4PublicChartProportion
    valid_only_confusion: Phase4PublicConfusionSummary

    @model_validator(mode="after")
    def validate_method_accounting(self) -> Phase4PublicMethodChartSummary:
        if (
            self.judgment_availability.numerator + self.provider_error_count
            > self.judgment_availability.denominator
        ):
            raise ValueError("method availability and Provider failures exceed the denominator")
        if (
            self.accuracy_all.denominator != self.judgment_availability.denominator
            or self.accuracy_natural.denominator + self.accuracy_counterfactual.denominator
            != self.accuracy_all.denominator
            or self.accuracy_natural.numerator + self.accuracy_counterfactual.numerator
            != self.accuracy_all.numerator
        ):
            raise ValueError("method source-stratum accuracy does not reconcile")
        if self.valid_only_confusion.valid_prediction_count != self.judgment_availability.numerator:
            raise ValueError("valid-only confusion count differs from judgment availability")
        if (
            self.valid_only_confusion.true_positive + self.valid_only_confusion.true_negative
            != self.accuracy_all.numerator
        ):
            raise ValueError("full-denominator accuracy differs from valid-only correct counts")
        return self


class Phase4PublicChartCohort(Phase4Contract):
    natural_trace_count: int = Field(ge=1)
    counterfactual_trace_count: int = Field(ge=1)
    counterfactual_parent_cluster_count: int = Field(ge=1)
    trace_count: int = Field(ge=1)
    method_count: int = Field(ge=1)
    pair_count: int = Field(ge=1)
    valid_judgment_count: int = Field(ge=0)
    provider_error_count: int = Field(ge=0)
    other_invalid_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_accounting(self) -> Phase4PublicChartCohort:
        if self.natural_trace_count + self.counterfactual_trace_count != self.trace_count:
            raise ValueError("trace source counts differ from the total")
        if self.trace_count * self.method_count != self.pair_count:
            raise ValueError("trace and method counts differ from the pair count")
        if (
            self.valid_judgment_count + self.provider_error_count + self.other_invalid_count
            != self.pair_count
        ):
            raise ValueError("method outcome counts differ from the pair count")
        return self


class Phase4PublicNaturalComparison(Phase4Contract):
    comparison: Literal[
        "full_tracejudge_vs_test_only",
        "full_tracejudge_vs_direct_llm_judge",
    ]
    denominator: int = Field(ge=1)
    full_correct: int = Field(ge=0)
    baseline_correct: int = Field(ge=0)
    difference_full_minus_baseline: float = Field(ge=-1.0, le=1.0)
    n01_baseline_incorrect_full_correct: int = Field(ge=0)
    n10_baseline_correct_full_incorrect: int = Field(ge=0)
    exact_two_sided_mcnemar_p_value: float = Field(ge=0.0, le=1.0)
    holm_adjusted_p_value: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_comparison(self) -> Phase4PublicNaturalComparison:
        if self.full_correct > self.denominator or self.baseline_correct > self.denominator:
            raise ValueError("natural comparison count exceeds its denominator")
        if (
            self.n01_baseline_incorrect_full_correct + self.n10_baseline_correct_full_incorrect
            > self.denominator
            or self.full_correct - self.baseline_correct
            != self.n01_baseline_incorrect_full_correct - self.n10_baseline_correct_full_incorrect
        ):
            raise ValueError("natural discordance counts do not reconcile")
        if self.holm_adjusted_p_value < self.exact_two_sided_mcnemar_p_value:
            raise ValueError("Holm-adjusted p-value is smaller than its raw p-value")
        expected = (self.full_correct - self.baseline_correct) / self.denominator
        if not math.isclose(
            self.difference_full_minus_baseline,
            expected,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("natural comparison difference differs from its counts")
        return self


class Phase4PublicCounterfactualComparison(Phase4Contract):
    comparison: Literal[
        "full_tracejudge_vs_test_only",
        "full_tracejudge_vs_direct_llm_judge",
    ]
    denominator: int = Field(ge=1)
    full_correct: int = Field(ge=0)
    baseline_correct: int = Field(ge=0)
    difference_full_minus_baseline: float = Field(ge=-1.0, le=1.0)
    cluster_bootstrap_95_lower: float = Field(ge=-1.0, le=1.0)
    cluster_bootstrap_95_upper: float = Field(ge=-1.0, le=1.0)
    parent_cluster_count: int = Field(ge=1)
    bootstrap_iteration_count: int = Field(ge=1)
    bootstrap_seed: int
    percentile_rule: Literal["type7_linear_interpolation"]

    @model_validator(mode="after")
    def validate_comparison(self) -> Phase4PublicCounterfactualComparison:
        if self.full_correct > self.denominator or self.baseline_correct > self.denominator:
            raise ValueError("counterfactual comparison count exceeds its denominator")
        if self.cluster_bootstrap_95_lower > self.cluster_bootstrap_95_upper:
            raise ValueError("counterfactual interval bounds are reversed")
        expected = (self.full_correct - self.baseline_correct) / self.denominator
        if not math.isclose(
            self.difference_full_minus_baseline,
            expected,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("counterfactual comparison difference differs from its counts")
        if not (
            self.cluster_bootstrap_95_lower
            <= self.difference_full_minus_baseline
            <= self.cluster_bootstrap_95_upper
        ):
            raise ValueError("cluster interval does not contain the comparison estimate")
        return self


class Phase4PublicChartArtifact(Phase4Contract):
    filename: Annotated[
        str,
        StringConstraints(pattern=r"^[0-9]{2}_[a-z0-9_]+\.svg$", max_length=128),
    ]
    title: str = Field(min_length=1, max_length=128)
    sha256: Sha256
    size_bytes: int = Field(ge=1)


class Phase4PublicChartsManifest(Phase4Contract):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase4_public_charts"] = "tracejudge_phase4_public_charts"
    chart_bundle_id: Identifier
    source_statistics_id: Identifier
    source_statistics_manifest_sha256: Sha256
    source_statistics_report_sha256: Sha256
    source_git: Phase4GitIdentity
    render_implementation_sha256: Sha256
    cohort: Phase4PublicChartCohort
    methods: tuple[Phase4PublicMethodChartSummary, ...]
    natural_comparisons: tuple[Phase4PublicNaturalComparison, ...]
    counterfactual_comparisons: tuple[Phase4PublicCounterfactualComparison, ...]
    figures: tuple[Phase4PublicChartArtifact, ...]
    verification_status: Literal["ANALYZED"] = "ANALYZED"
    overall_confidence: Literal["CAUTION"] = "CAUTION"
    reproducibility: Literal["CANNOT_VERIFY"] = "CANNOT_VERIFY"
    exploratory_only: Literal[True] = True
    contains_trace_ids: Literal[False] = False
    contains_annotation_records: Literal[False] = False
    contains_per_trace_predictions: Literal[False] = False
    contains_provider_raw: Literal[False] = False
    contains_hidden_evaluation_content: Literal[False] = False

    @model_validator(mode="after")
    def validate_release_shape(self) -> Phase4PublicChartsManifest:
        expected_methods = (
            "test_only",
            "direct_llm_judge",
            "four_layer_structured_judge",
            "four_layer_ast",
            "full_tracejudge",
        )
        if tuple(item.method_id for item in self.methods) != expected_methods:
            raise ValueError("public chart methods are incomplete or out of order")
        expected_comparisons = (
            "full_tracejudge_vs_test_only",
            "full_tracejudge_vs_direct_llm_judge",
        )
        if tuple(item.comparison for item in self.natural_comparisons) != expected_comparisons:
            raise ValueError("natural comparisons are incomplete or out of order")
        if (
            tuple(item.comparison for item in self.counterfactual_comparisons)
            != expected_comparisons
        ):
            raise ValueError("counterfactual comparisons are incomplete or out of order")
        expected_figures = (
            "01_cohort_and_execution.svg",
            "02_error_detection_by_source.svg",
            "03_preregistered_paired_comparisons.svg",
        )
        if tuple(item.filename for item in self.figures) != expected_figures:
            raise ValueError("public chart artifacts are incomplete or out of order")
        return self
