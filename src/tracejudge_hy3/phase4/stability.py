"""Independent 4-case x 5-repeat Full TraceJudge stability study.

The study consumes only the exact public ``safe_mean`` fixtures already frozen
for phase three.  It deliberately writes to a new phase-four run directory and
never reads, mutates, or merges into the frozen 57 x 5 experiment.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import shutil
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, ValidationError, model_validator

from tracejudge_hy3.baseline.runner import _git_metadata
from tracejudge_hy3.config import Settings, get_settings
from tracejudge_hy3.phase3.contracts import (
    CounterfactualKind,
    MethodId,
    MethodOutcome,
    MethodOutcomeStatus,
    MethodSpec,
)
from tracejudge_hy3.phase3.counterfactual import (
    PUBLIC_COUNTERFACTUAL_SOURCE_SHA256,
    _load_execution_evidence,
    _load_source_bundle,
    _public_problem_payload,
)
from tracejudge_hy3.phase3.execution import (
    Phase3Hy3JudgeProvider,
    _hy3_public_configuration,
)
from tracejudge_hy3.phase3.materials import _public_execution_payload
from tracejudge_hy3.phase3.privacy import (
    assert_public_payload_safe,
    canonical_sha256,
)
from tracejudge_hy3.phase3.runner import (
    Phase3JudgeProvider,
    Phase3TraceMaterial,
    PublicDynamicEvidenceInput,
    build_method_specs,
    evaluate_method,
    output_schema_sha256,
    project_method_input,
    provider_config_sha256,
)
from tracejudge_hy3.prompts.phase3 import (
    method_prompt_sha256,
    method_prompt_version,
)

from .contracts import Phase4Contract

STABILITY_STUDY_ID = "phase4_judge_stability_public4x5_v1"
STABILITY_REPETITION_COUNT = 5
STABILITY_CASE_COUNT = 4
STABILITY_EVALUATION_COUNT = STABILITY_CASE_COUNT * STABILITY_REPETITION_COUNT
STABILITY_ORDERING_POLICY = "repeat_major_fixed_case_order_v1"
STABILITY_OUTPUT_DIR = "artifacts/experiments/phase4-judge-stability"

_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TARGET_PARENT_ID = "public-parent:safe_mean:v1"
_NULL_VALUE = "<none>"
_FIELD_NAMES = ("has_error", "first_faulty_step", "error_type")


@dataclass(frozen=True, slots=True)
class _CasePlan:
    case_id: str
    case_role: Literal[
        "normal_correct",
        "reasoning_swap",
        "boundary_error",
        "equivalent_implementation",
    ]
    mutation_kind: CounterfactualKind | None
    expected_execution_status: Literal["pass", "fail"]


_CASE_PLANS = (
    _CasePlan("normal_correct", "normal_correct", None, "pass"),
    _CasePlan(
        "reasoning_swap",
        "reasoning_swap",
        CounterfactualKind.REASONING_SWAP,
        "pass",
    ),
    _CasePlan(
        "boundary_error",
        "boundary_error",
        CounterfactualKind.BOUNDARY_DELETION,
        "fail",
    ),
    _CasePlan(
        "equivalent_implementation",
        "equivalent_implementation",
        CounterfactualKind.EQUIVALENT_IMPLEMENTATION,
        "pass",
    ),
)


class Phase4StabilityError(ValueError):
    def __init__(self, message: str, *, safe_stage: str = "P4_STABILITY") -> None:
        super().__init__(message)
        self.safe_stage = safe_stage


class StabilityGitIdentity(Phase4Contract):
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    branch: str = Field(min_length=1, max_length=255)
    dirty: bool
    working_tree_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_dirty_identity(self) -> Self:
        if self.dirty and self.working_tree_sha256 is None:
            raise ValueError("dirty Git state requires a working-tree fingerprint")
        if not self.dirty and self.working_tree_sha256 is not None:
            raise ValueError("clean Git state must not carry a working-tree fingerprint")
        return self


class StabilityCaseDefinition(Phase4Contract):
    case_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    case_role: Literal[
        "normal_correct",
        "reasoning_swap",
        "boundary_error",
        "equivalent_implementation",
    ]
    trace_id: str = Field(min_length=1, max_length=200)
    execution_subject_id: str = Field(min_length=1, max_length=200)
    expected_execution_status: Literal["pass", "fail"]
    method_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class StabilityProtocol(Phase4Contract):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase4_judge_stability_protocol"] = (
        "tracejudge_phase4_judge_stability_protocol"
    )
    study_id: Literal["phase4_judge_stability_public4x5_v1"] = STABILITY_STUDY_ID
    research_question: str = Field(min_length=1, max_length=512)
    analysis_scope: Literal["exploratory_public_fixture_stability"] = (
        "exploratory_public_fixture_stability"
    )
    source_git: StabilityGitIdentity
    python_version: str = Field(min_length=1, max_length=64)
    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    provider_configuration: dict[str, Any]
    provider_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    method_id: Literal["full_tracejudge"] = "full_tracejudge"
    prompt_version: str = Field(min_length=1, max_length=256)
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    method_spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    implementation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    temperature: float = Field(ge=0.0)
    timeout_seconds: float = Field(gt=0.0)
    parse_policy: Literal["strict_json_schema_one_repair_v1"] = "strict_json_schema_one_repair_v1"
    repetition_count: Literal[5] = STABILITY_REPETITION_COUNT
    case_count: Literal[4] = STABILITY_CASE_COUNT
    scheduled_evaluation_count: Literal[20] = STABILITY_EVALUATION_COUNT
    nominal_provider_call_count: Literal[20] = STABILITY_EVALUATION_COUNT
    maximum_provider_call_count: Literal[40] = STABILITY_EVALUATION_COUNT * 2
    ordering_policy: Literal["repeat_major_fixed_case_order_v1"] = STABILITY_ORDERING_POLICY
    source_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_results_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    material_payloads_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cases: tuple[StabilityCaseDefinition, ...]
    primary_fields: tuple[Literal["has_error", "first_faulty_step", "error_type"], ...] = (
        _FIELD_NAMES
    )
    agreement_estimand: Literal["within_case_pairwise_exact_agreement"] = (
        "within_case_pairwise_exact_agreement"
    )
    missingness_policy: Literal["failures_excluded_from_field_pairs_and_reported_separately"] = (
        "failures_excluded_from_field_pairs_and_reported_separately"
    )
    main_experiment_merge_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_fixed_design(self) -> Self:
        if tuple(item.case_id for item in self.cases) != tuple(
            item.case_id for item in _CASE_PLANS
        ):
            raise ValueError("stability cases differ from the fixed public four-case order")
        if len(self.cases) != self.case_count:
            raise ValueError("stability case count differs from case definitions")
        if self.primary_fields != _FIELD_NAMES:
            raise ValueError("stability primary fields differ from the preregistered order")
        if (
            self.provider_configuration.get("provider") != self.provider
            or self.provider_configuration.get("model") != self.model
            or canonical_sha256(self.provider_configuration) != self.provider_config_sha256
        ):
            raise ValueError("provider configuration differs from its bound identity")
        return self


class StabilityTrialRecord(Phase4Contract):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase4_judge_stability_trial"] = (
        "tracejudge_phase4_judge_stability_trial"
    )
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    trial_id: str = Field(pattern=r"^stability_trial_[0-9]{3}$")
    trial_index: int = Field(ge=1, le=STABILITY_EVALUATION_COUNT)
    repetition_index: int = Field(ge=1, le=STABILITY_REPETITION_COUNT)
    case_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    trace_id: str = Field(min_length=1, max_length=200)
    outcome: MethodOutcome

    @model_validator(mode="after")
    def bind_outcome(self) -> Self:
        if self.outcome.run_id != self.run_id:
            raise ValueError("trial outcome run_id differs")
        if self.outcome.trace_id != self.trace_id:
            raise ValueError("trial outcome trace_id differs")
        if self.outcome.method_id != MethodId.FULL_TRACEJUDGE:
            raise ValueError("stability trial must use Full TraceJudge")
        return self


class StabilityFieldSummary(Phase4Contract):
    field_name: Literal["has_error", "first_faulty_step", "error_type", "joint_label"]
    agreeing_pair_count: int = Field(ge=0)
    comparable_pair_count: int = Field(ge=0)
    pairwise_agreement: float | None = Field(default=None, ge=0.0, le=1.0)
    case_count_with_two_or_more_valid: int = Field(ge=0, le=STABILITY_CASE_COUNT)
    all_five_consistent_case_count: int = Field(ge=0, le=STABILITY_CASE_COUNT)

    @model_validator(mode="after")
    def validate_ratio(self) -> Self:
        if self.agreeing_pair_count > self.comparable_pair_count:
            raise ValueError("agreement numerator exceeds denominator")
        expected = (
            self.agreeing_pair_count / self.comparable_pair_count
            if self.comparable_pair_count
            else None
        )
        if expected is None:
            if self.pairwise_agreement is not None:
                raise ValueError("zero comparable pairs must report N/A")
        elif self.pairwise_agreement is None or not math.isclose(
            self.pairwise_agreement, expected, rel_tol=0.0, abs_tol=1e-12
        ):
            raise ValueError("pairwise agreement differs from counts")
        return self


class StabilityCaseFieldSummary(Phase4Contract):
    field_name: Literal["has_error", "first_faulty_step", "error_type", "joint_label"]
    valid_value_count: int = Field(ge=0, le=STABILITY_REPETITION_COUNT)
    distribution: dict[str, int]
    mode_value: str | None = None
    mode_count: int = Field(ge=0, le=STABILITY_REPETITION_COUNT)
    modal_agreement: float | None = Field(default=None, ge=0.0, le=1.0)
    agreeing_pair_count: int = Field(ge=0)
    comparable_pair_count: int = Field(ge=0)
    pairwise_agreement: float | None = Field(default=None, ge=0.0, le=1.0)
    all_five_consistent: bool

    @model_validator(mode="after")
    def validate_distribution_and_ratios(self) -> Self:
        if (
            any(value < 1 for value in self.distribution.values())
            or sum(self.distribution.values()) != self.valid_value_count
        ):
            raise ValueError("field distribution differs from valid values")
        expected_pairs = math.comb(self.valid_value_count, 2) if self.valid_value_count >= 2 else 0
        expected_agreeing = sum(math.comb(value, 2) for value in self.distribution.values())
        if (
            self.comparable_pair_count != expected_pairs
            or self.agreeing_pair_count != expected_agreeing
        ):
            raise ValueError("field pair counts differ from the distribution")
        expected_pairwise = expected_agreeing / expected_pairs if expected_pairs else None
        if expected_pairwise is None:
            if self.pairwise_agreement is not None:
                raise ValueError("zero comparable pairs must report N/A")
        elif self.pairwise_agreement is None or not math.isclose(
            self.pairwise_agreement, expected_pairwise, rel_tol=0.0, abs_tol=1e-12
        ):
            raise ValueError("field pairwise agreement differs from counts")
        if self.distribution:
            expected_mode_value, expected_mode_count = min(
                self.distribution.items(), key=lambda item: (-item[1], item[0])
            )
            if self.mode_value != expected_mode_value or self.mode_count != expected_mode_count:
                raise ValueError("field mode differs from the distribution")
            if self.modal_agreement is None or not math.isclose(
                self.modal_agreement,
                expected_mode_count / self.valid_value_count,
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                raise ValueError("field modal agreement differs from the distribution")
        elif self.mode_value is not None or self.mode_count or self.modal_agreement is not None:
            raise ValueError("empty distribution may not carry a mode")
        expected_all_five = (
            self.valid_value_count == STABILITY_REPETITION_COUNT and len(self.distribution) == 1
        )
        if self.all_five_consistent != expected_all_five:
            raise ValueError("all-five flag differs from the distribution")
        return self


class StabilityCaseSummary(Phase4Contract):
    case_id: str
    trace_id: str
    scheduled_count: Literal[5] = STABILITY_REPETITION_COUNT
    valid_judgment_count: int = Field(ge=0, le=STABILITY_REPETITION_COUNT)
    provider_failure_count: int = Field(ge=0, le=STABILITY_REPETITION_COUNT)
    parse_failure_count: int = Field(ge=0, le=STABILITY_REPETITION_COUNT)
    other_failure_count: int = Field(ge=0, le=STABILITY_REPETITION_COUNT)
    parse_repair_trial_count: int = Field(ge=0, le=STABILITY_REPETITION_COUNT)
    fields: tuple[StabilityCaseFieldSummary, ...]

    @model_validator(mode="after")
    def validate_accounting(self) -> Self:
        if (
            self.valid_judgment_count
            + self.provider_failure_count
            + self.parse_failure_count
            + self.other_failure_count
            != self.scheduled_count
        ):
            raise ValueError("case outcome counts do not cover five repetitions")
        return self


class StabilityReport(Phase4Contract):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase4_judge_stability_report"] = (
        "tracejudge_phase4_judge_stability_report"
    )
    run_id: str
    generated_at: datetime
    verification_status: Literal["ANALYZED"] = "ANALYZED"
    analysis_scope: Literal["exploratory_public_fixture_stability"] = (
        "exploratory_public_fixture_stability"
    )
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scheduled_evaluation_count: Literal[20] = STABILITY_EVALUATION_COUNT
    valid_judgment_count: int = Field(ge=0, le=STABILITY_EVALUATION_COUNT)
    provider_failure_count: int = Field(ge=0, le=STABILITY_EVALUATION_COUNT)
    parse_failure_count: int = Field(ge=0, le=STABILITY_EVALUATION_COUNT)
    other_failure_count: int = Field(ge=0, le=STABILITY_EVALUATION_COUNT)
    observed_provider_call_count: int = Field(ge=0, le=STABILITY_EVALUATION_COUNT * 2)
    parse_repair_trial_count: int = Field(ge=0, le=STABILITY_EVALUATION_COUNT)
    all_twenty_valid: bool
    overall_fields: tuple[StabilityFieldSummary, ...]
    cases: tuple[StabilityCaseSummary, ...]
    conclusion_boundary: str = Field(min_length=1)
    main_experiment_merge_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_accounting(self) -> Self:
        if (
            self.valid_judgment_count
            + self.provider_failure_count
            + self.parse_failure_count
            + self.other_failure_count
            != self.scheduled_evaluation_count
        ):
            raise ValueError("report outcome counts do not cover twenty evaluations")
        if self.all_twenty_valid != (self.valid_judgment_count == self.scheduled_evaluation_count):
            raise ValueError("all_twenty_valid differs from result accounting")
        return self


class StabilityRunManifest(Phase4Contract):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase4_judge_stability_run"] = "tracejudge_phase4_judge_stability_run"
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    status: Literal["running", "completed", "completed_with_failures"]
    created_at: datetime
    updated_at: datetime
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scheduled_evaluation_count: Literal[20] = STABILITY_EVALUATION_COUNT
    completed_evaluation_count: int = Field(ge=0, le=STABILITY_EVALUATION_COUNT)
    observed_provider_call_count: int = Field(ge=0, le=STABILITY_EVALUATION_COUNT * 2)
    results_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    report_json_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    report_markdown_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    main_experiment_merge_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_terminal_state(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("manifest update precedes creation")
        hashes = (
            self.results_sha256,
            self.report_json_sha256,
            self.report_markdown_sha256,
        )
        if self.status == "running":
            if any(value is not None for value in hashes):
                raise ValueError("running manifest may not claim final artifacts")
        elif self.completed_evaluation_count != self.scheduled_evaluation_count or any(
            value is None for value in hashes
        ):
            raise ValueError("completed manifest requires twenty rows and final hashes")
        return self


@dataclass(frozen=True, slots=True)
class StabilityPreflight:
    run_id: str
    case_count: int
    repetition_count: int
    scheduled_evaluation_count: int
    nominal_provider_call_count: int
    maximum_provider_call_count: int
    provider: str
    model: str
    protocol_sha256: str
    source_bundle_sha256: str
    execution_results_sha256: str
    material_payloads_sha256: str
    method_spec_sha256: str
    prompt_sha256: str
    output_schema_sha256: str
    git_commit: str
    git_dirty: bool


@dataclass(frozen=True, slots=True)
class StabilityRunResult:
    run_id: str
    run_dir: Path
    manifest_path: Path
    protocol_path: Path
    results_path: Path
    report_json_path: Path
    report_markdown_path: Path
    manifest_sha256: str
    results_sha256: str
    report_json_sha256: str
    report_markdown_sha256: str
    valid_judgment_count: int
    provider_failure_count: int
    parse_failure_count: int
    observed_provider_call_count: int


@dataclass(frozen=True, slots=True)
class _PreparedStability:
    protocol: StabilityProtocol
    protocol_payload: bytes
    protocol_sha256: str
    materials: Mapping[str, Phase3TraceMaterial]
    spec: MethodSpec
    output_root: Path
    run_dir: Path
    preflight: StabilityPreflight


@dataclass(frozen=True, slots=True)
class _TrialPlan:
    trial_index: int
    repetition_index: int
    case: StabilityCaseDefinition

    @property
    def trial_id(self) -> str:
        return f"stability_trial_{self.trial_index:03d}"

    @property
    def filename(self) -> str:
        return f"trial_{self.trial_index:03d}.json"


def _json_bytes(value: Any, *, pretty: bool = True) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    kwargs: dict[str, Any] = {
        "ensure_ascii": False,
        "allow_nan": False,
        "sort_keys": True,
    }
    if pretty:
        kwargs["indent"] = 2
    else:
        kwargs["separators"] = (",", ":")
    return json.dumps(value, **kwargs).encode("utf-8") + b"\n"


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stability_implementation_sha256() -> str:
    paths = (
        Path(__file__),
        Path(__file__).parents[1] / "phase3" / "runner.py",
        Path(__file__).parents[1] / "phase3" / "materials.py",
        Path(__file__).parents[1] / "prompts" / "phase3.py",
    )
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(Path(__file__).parents[1]).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _git_identity(
    *, repo_root: str | Path, output_dir: str | Path, allow_dirty: bool
) -> StabilityGitIdentity:
    git = _git_metadata(Path(repo_root), excluded_paths=(Path(output_dir),))
    commit = git.get("commit")
    branch = git.get("branch")
    dirty = git.get("dirty")
    fingerprint = git.get("working_tree_sha256")
    if (
        git.get("available") is not True
        or not isinstance(commit, str)
        or not _GIT_COMMIT_PATTERN.fullmatch(commit)
        or not isinstance(branch, str)
        or not branch.strip()
        or not isinstance(dirty, bool)
    ):
        raise Phase4StabilityError(
            "Git execution identity is unavailable", safe_stage="P4_STABILITY_GIT"
        )
    if dirty and not allow_dirty:
        raise Phase4StabilityError(
            "formal stability evaluation requires a clean worktree",
            safe_stage="P4_STABILITY_GIT_DIRTY",
        )
    if dirty and (not isinstance(fingerprint, str) or not _SHA256_PATTERN.fullmatch(fingerprint)):
        raise Phase4StabilityError(
            "dirty working-tree fingerprint is unavailable",
            safe_stage="P4_STABILITY_GIT",
        )
    return StabilityGitIdentity(
        commit=commit,
        branch=branch,
        dirty=dirty,
        working_tree_sha256=fingerprint if dirty else None,
    )


def _validate_output_target(
    *, run_id: str, output_dir: str | Path, resume: bool
) -> tuple[Path, Path]:
    if not _RUN_ID_PATTERN.fullmatch(run_id):
        raise Phase4StabilityError(
            "run_id contains unsupported characters", safe_stage="P4_STABILITY_OUTPUT"
        )
    raw_output_root = Path(output_dir).expanduser()
    if raw_output_root.is_symlink():
        raise Phase4StabilityError(
            "stability output root is unsafe", safe_stage="P4_STABILITY_OUTPUT"
        )
    output_root = raw_output_root.resolve()
    if output_root.exists() and not output_root.is_dir():
        raise Phase4StabilityError(
            "stability output root is unsafe", safe_stage="P4_STABILITY_OUTPUT"
        )
    run_dir = output_root / run_id
    if resume:
        if run_dir.is_symlink() or not run_dir.is_dir():
            raise Phase4StabilityError(
                "resume run directory is missing or unsafe",
                safe_stage="P4_STABILITY_RESUME",
            )
    elif run_dir.exists() or run_dir.is_symlink():
        raise Phase4StabilityError(
            "stability run directory already exists", safe_stage="P4_STABILITY_OUTPUT"
        )
    return output_root, run_dir


def _build_public_materials(
    *,
    source_bundle_path: str | Path,
    execution_run_dir: str | Path,
    spec: MethodSpec,
    privacy_canaries: Sequence[str | bytes],
) -> tuple[
    tuple[StabilityCaseDefinition, ...],
    Mapping[str, Phase3TraceMaterial],
    str,
    str,
    str,
    str,
]:
    source = _load_source_bundle(
        source_bundle_path,
        expected_source_sha256=PUBLIC_COUNTERFACTUAL_SOURCE_SHA256,
        privacy_canaries=privacy_canaries,
    )
    evidence = _load_execution_evidence(
        execution_run_dir,
        prepared_source=source,
        privacy_canaries=privacy_canaries,
    )
    parents = {item.parent_trace_id: item for item in source.bundle.parents}
    parent = parents.get(_TARGET_PARENT_ID)
    if parent is None:
        raise Phase4StabilityError(
            "fixed safe_mean parent is absent from the public bundle",
            safe_stage="P4_STABILITY_CASES",
        )
    variants = {
        item.mutation_kind: item
        for item in source.bundle.counterfactuals
        if item.parent_trace_id == _TARGET_PARENT_ID
    }
    materials: dict[str, Phase3TraceMaterial] = {}
    definitions: list[StabilityCaseDefinition] = []
    for plan in _CASE_PLANS:
        if plan.mutation_kind is None:
            trace_id = parent.parent_trace_id
            solution = parent.solution_trace
            execution_subject_id = parent.parent_trace_id
        else:
            variant = variants.get(plan.mutation_kind)
            if variant is None:
                raise Phase4StabilityError(
                    "a fixed stability variant is absent from the public bundle",
                    safe_stage="P4_STABILITY_CASES",
                )
            trace_id = variant.trace_id
            solution = variant.solution_trace
            execution_subject_id = (
                parent.parent_trace_id
                if plan.mutation_kind == CounterfactualKind.REASONING_SWAP
                else variant.trace_id
            )
        functional = evidence.evidence_by_subject.get(execution_subject_id)
        execution_result = evidence.results_by_subject.get(execution_subject_id)
        execution_row_sha256 = evidence.result_sha256_by_subject.get(execution_subject_id)
        if functional is None or execution_result is None or execution_row_sha256 is None:
            raise Phase4StabilityError(
                "fixed public execution evidence is incomplete",
                safe_stage="P4_STABILITY_EVIDENCE",
            )
        if execution_result.execution_status != plan.expected_execution_status:
            raise Phase4StabilityError(
                "fixed case execution status differs from the preregistered role",
                safe_stage="P4_STABILITY_EVIDENCE",
            )
        dynamic_payload = _public_execution_payload(
            execution_result,
            execution_evidence_sha256=execution_row_sha256,
        )
        material = Phase3TraceMaterial(
            trace_id=trace_id,
            public_problem=_public_problem_payload(parent.fixture),
            solution_trace=solution,
            functional_evidence=functional,
            public_dynamic_evidence=PublicDynamicEvidenceInput(
                status="available",
                evidence_sha256=canonical_sha256(dynamic_payload),
                payload=dynamic_payload,
            ),
        )
        assert_public_payload_safe(material, canaries=privacy_canaries)
        method_input_sha256 = canonical_sha256(project_method_input(spec=spec, material=material))
        materials[plan.case_id] = material
        definitions.append(
            StabilityCaseDefinition(
                case_id=plan.case_id,
                case_role=plan.case_role,
                trace_id=trace_id,
                execution_subject_id=execution_subject_id,
                expected_execution_status=plan.expected_execution_status,
                method_input_sha256=method_input_sha256,
            )
        )
    material_payloads_sha256 = canonical_sha256(
        [materials[item.case_id].model_dump(mode="json") for item in definitions]
    )
    return (
        tuple(definitions),
        materials,
        source.source_sha256,
        evidence.identity.manifest_sha256,
        evidence.identity.results_sha256,
        material_payloads_sha256,
    )


def _prepare_stability(
    *,
    run_id: str,
    provider_configuration: Mapping[str, Any],
    source_bundle_path: str | Path,
    execution_run_dir: str | Path,
    output_dir: str | Path,
    repo_root: str | Path,
    temperature: float,
    timeout_seconds: float,
    resume: bool,
    allow_dirty: bool,
    privacy_canaries: Sequence[str | bytes],
) -> _PreparedStability:
    configuration = dict(provider_configuration)
    provider = configuration.get("provider")
    model = configuration.get("model")
    if not isinstance(provider, str) or not provider.strip():
        raise Phase4StabilityError(
            "provider configuration lacks a public provider identity",
            safe_stage="P4_STABILITY_PROVIDER",
        )
    if not isinstance(model, str) or not model.strip():
        raise Phase4StabilityError(
            "provider configuration lacks a public model identity",
            safe_stage="P4_STABILITY_PROVIDER",
        )
    assert_public_payload_safe(configuration, canaries=privacy_canaries)
    output_root, run_dir = _validate_output_target(
        run_id=run_id, output_dir=output_dir, resume=resume
    )
    source_git = _git_identity(
        repo_root=repo_root,
        output_dir=output_dir,
        allow_dirty=allow_dirty,
    )
    specs = build_method_specs(
        provider=provider,
        model=model,
        temperature=temperature,
        timeout_seconds=timeout_seconds,
    )
    spec = next(item for item in specs if item.method_id == MethodId.FULL_TRACEJUDGE)
    (
        cases,
        materials,
        source_sha256,
        evidence_manifest_sha256,
        evidence_results_sha256,
        material_payloads_sha256,
    ) = _build_public_materials(
        source_bundle_path=source_bundle_path,
        execution_run_dir=execution_run_dir,
        spec=spec,
        privacy_canaries=privacy_canaries,
    )
    prompt_version = method_prompt_version(MethodId.FULL_TRACEJUDGE)
    prompt_sha = method_prompt_sha256(MethodId.FULL_TRACEJUDGE)
    assert prompt_version is not None and prompt_sha is not None
    protocol = StabilityProtocol(
        research_question=(
            "在四个固定公开案例上，Full TraceJudge 的 has_error、"
            "first_faulty_step 与 error_type 判断在五次独立评审中是否稳定？"
        ),
        source_git=source_git,
        python_version=platform.python_version(),
        provider=provider,
        model=model,
        provider_configuration=configuration,
        provider_config_sha256=canonical_sha256(configuration),
        prompt_version=prompt_version,
        prompt_sha256=prompt_sha,
        output_schema_sha256=output_schema_sha256(),
        method_spec_sha256=canonical_sha256(spec.model_dump(mode="json")),
        implementation_sha256=stability_implementation_sha256(),
        temperature=temperature,
        timeout_seconds=timeout_seconds,
        source_bundle_sha256=source_sha256,
        execution_manifest_sha256=evidence_manifest_sha256,
        execution_results_sha256=evidence_results_sha256,
        material_payloads_sha256=material_payloads_sha256,
        cases=cases,
    )
    assert_public_payload_safe(protocol, canaries=privacy_canaries)
    protocol_payload = _json_bytes(protocol)
    protocol_sha256 = hashlib.sha256(protocol_payload).hexdigest()
    preflight = StabilityPreflight(
        run_id=run_id,
        case_count=len(cases),
        repetition_count=STABILITY_REPETITION_COUNT,
        scheduled_evaluation_count=STABILITY_EVALUATION_COUNT,
        nominal_provider_call_count=STABILITY_EVALUATION_COUNT,
        maximum_provider_call_count=STABILITY_EVALUATION_COUNT * 2,
        provider=provider,
        model=model,
        protocol_sha256=protocol_sha256,
        source_bundle_sha256=source_sha256,
        execution_results_sha256=evidence_results_sha256,
        material_payloads_sha256=material_payloads_sha256,
        method_spec_sha256=protocol.method_spec_sha256,
        prompt_sha256=prompt_sha,
        output_schema_sha256=protocol.output_schema_sha256,
        git_commit=source_git.commit,
        git_dirty=source_git.dirty,
    )
    return _PreparedStability(
        protocol=protocol,
        protocol_payload=protocol_payload,
        protocol_sha256=protocol_sha256,
        materials=materials,
        spec=spec,
        output_root=output_root,
        run_dir=run_dir,
        preflight=preflight,
    )


def preflight_judge_stability(**kwargs: Any) -> StabilityPreflight:
    """Validate the complete 4 x 5 design without writing or calling a Provider."""

    return _prepare_stability(**kwargs).preflight


def preflight_hy3_judge_stability(
    *,
    run_id: str,
    source_bundle_path: str | Path,
    execution_run_dir: str | Path,
    output_dir: str | Path = STABILITY_OUTPUT_DIR,
    repo_root: str | Path = ".",
    temperature: float = 0.0,
    timeout_seconds: float = 120.0,
    resume: bool = False,
    allow_dirty: bool = False,
    settings: Settings | None = None,
    privacy_canaries: Sequence[str | bytes] = (),
) -> StabilityPreflight:
    """Read-only Hy3 preflight using only the provider's public configuration."""

    configured = settings or get_settings()
    model = configured.hy3_model
    if not isinstance(model, str) or not model.strip():
        raise Phase4StabilityError(
            "Hy3 model is not configured", safe_stage="P4_STABILITY_PROVIDER"
        )
    configuration = _hy3_public_configuration(configured, model=model)
    return preflight_judge_stability(
        run_id=run_id,
        provider_configuration=configuration,
        source_bundle_path=source_bundle_path,
        execution_run_dir=execution_run_dir,
        output_dir=output_dir,
        repo_root=repo_root,
        temperature=temperature,
        timeout_seconds=timeout_seconds,
        resume=resume,
        allow_dirty=allow_dirty,
        privacy_canaries=privacy_canaries,
    )


def _trial_plan(protocol: StabilityProtocol) -> tuple[_TrialPlan, ...]:
    plans: list[_TrialPlan] = []
    index = 0
    for repetition_index in range(1, protocol.repetition_count + 1):
        for case in protocol.cases:
            index += 1
            plans.append(
                _TrialPlan(
                    trial_index=index,
                    repetition_index=repetition_index,
                    case=case,
                )
            )
    return tuple(plans)


def _manifest(
    *,
    run_id: str,
    status: Literal["running", "completed", "completed_with_failures"],
    created_at: datetime,
    protocol_sha256: str,
    trials: Sequence[StabilityTrialRecord],
    results_sha256: str | None = None,
    report_json_sha256: str | None = None,
    report_markdown_sha256: str | None = None,
) -> StabilityRunManifest:
    return StabilityRunManifest(
        run_id=run_id,
        status=status,
        created_at=created_at,
        updated_at=datetime.now(UTC),
        protocol_sha256=protocol_sha256,
        completed_evaluation_count=len(trials),
        observed_provider_call_count=sum(item.outcome.attempt_count for item in trials),
        results_sha256=results_sha256,
        report_json_sha256=report_json_sha256,
        report_markdown_sha256=report_markdown_sha256,
    )


def _initialize_run(prepared: _PreparedStability) -> datetime:
    prepared.output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    prepared.output_root.chmod(0o700)
    temporary: Path | None = None
    created_at = datetime.now(UTC)
    try:
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{prepared.run_dir.name}.", dir=prepared.output_root)
        )
        temporary.chmod(0o700)
        (temporary / "trials").mkdir(mode=0o700)
        _atomic_write(temporary / "protocol.json", prepared.protocol_payload)
        initial_manifest = _manifest(
            run_id=prepared.run_dir.name,
            status="running",
            created_at=created_at,
            protocol_sha256=prepared.protocol_sha256,
            trials=(),
        )
        _atomic_write(temporary / "manifest.json", _json_bytes(initial_manifest))
        os.replace(temporary, prepared.run_dir)
        temporary = None
    except OSError as exc:
        raise Phase4StabilityError(
            "cannot initialize stability run directory",
            safe_stage="P4_STABILITY_OUTPUT",
        ) from exc
    finally:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
    return created_at


def _read_json_model(path: Path, model: type[Phase4Contract], *, label: str) -> Any:
    if path.is_symlink() or not path.is_file():
        raise Phase4StabilityError(
            f"{label} is missing or unsafe", safe_stage="P4_STABILITY_RESUME"
        )
    try:
        return model.model_validate_json(path.read_bytes())
    except (OSError, ValidationError):
        raise Phase4StabilityError(
            f"{label} failed strict validation", safe_stage="P4_STABILITY_RESUME"
        ) from None


def _load_existing_trials(
    prepared: _PreparedStability,
) -> tuple[datetime, tuple[StabilityTrialRecord, ...]]:
    protocol_path = prepared.run_dir / "protocol.json"
    manifest_path = prepared.run_dir / "manifest.json"
    protocol = _read_json_model(protocol_path, StabilityProtocol, label="stability protocol")
    manifest = _read_json_model(manifest_path, StabilityRunManifest, label="stability manifest")
    if (
        protocol != prepared.protocol
        or _file_sha256(protocol_path) != prepared.protocol_sha256
        or manifest.protocol_sha256 != prepared.protocol_sha256
        or manifest.run_id != prepared.run_dir.name
    ):
        raise Phase4StabilityError(
            "resume identity differs from the current stability protocol",
            safe_stage="P4_STABILITY_RESUME_IDENTITY",
        )
    if manifest.status != "running":
        raise Phase4StabilityError(
            "only an incomplete stability run may be resumed",
            safe_stage="P4_STABILITY_RESUME",
        )
    trials_dir = prepared.run_dir / "trials"
    if trials_dir.is_symlink() or not trials_dir.is_dir():
        raise Phase4StabilityError(
            "stability trial directory is missing or unsafe",
            safe_stage="P4_STABILITY_RESUME",
        )
    files = sorted(item for item in trials_dir.iterdir() if item.name != ".DS_Store")
    expected = _trial_plan(protocol)
    if len(files) > len(expected):
        raise Phase4StabilityError(
            "stability trial directory contains extra files",
            safe_stage="P4_STABILITY_RESUME",
        )
    trials: list[StabilityTrialRecord] = []
    for plan, path in zip(expected, files, strict=False):
        if path.name != plan.filename:
            raise Phase4StabilityError(
                "stability trial files are not a strict prefix",
                safe_stage="P4_STABILITY_RESUME",
            )
        trial = _read_json_model(path, StabilityTrialRecord, label="stability trial")
        if (
            trial.trial_id != plan.trial_id
            or trial.trial_index != plan.trial_index
            or trial.repetition_index != plan.repetition_index
            or trial.case_id != plan.case.case_id
            or trial.trace_id != plan.case.trace_id
        ):
            raise Phase4StabilityError(
                "stability trial differs from the fixed schedule",
                safe_stage="P4_STABILITY_RESUME_IDENTITY",
            )
        trials.append(trial)
    if manifest.completed_evaluation_count != len(
        trials
    ) or manifest.observed_provider_call_count != sum(
        item.outcome.attempt_count for item in trials
    ):
        raise Phase4StabilityError(
            "stability manifest checkpoint differs from trial files",
            safe_stage="P4_STABILITY_RESUME_IDENTITY",
        )
    return manifest.created_at, tuple(trials)


def _field_value(outcome: MethodOutcome, field_name: str) -> str:
    assert outcome.judgment is not None
    if field_name == "has_error":
        return "true" if outcome.judgment.has_error else "false"
    if field_name == "first_faulty_step":
        return outcome.judgment.first_faulty_step or _NULL_VALUE
    if field_name == "error_type":
        error_type = outcome.judgment.error_type
        return error_type.value if error_type is not None else _NULL_VALUE
    if field_name == "joint_label":
        return json.dumps(
            [
                outcome.judgment.has_error,
                outcome.judgment.first_faulty_step,
                (
                    outcome.judgment.error_type.value
                    if outcome.judgment.error_type is not None
                    else None
                ),
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    raise AssertionError("unknown stability field")


def _case_field_summary(
    outcomes: Sequence[MethodOutcome], field_name: str
) -> StabilityCaseFieldSummary:
    values = [_field_value(item, field_name) for item in outcomes]
    distribution = dict(sorted(Counter(values).items()))
    count = len(values)
    comparable = math.comb(count, 2) if count >= 2 else 0
    agreeing = sum(math.comb(value, 2) for value in distribution.values())
    if distribution:
        mode_value, mode_count = min(distribution.items(), key=lambda item: (-item[1], item[0]))
        modal_agreement = mode_count / count
    else:
        mode_value = None
        mode_count = 0
        modal_agreement = None
    return StabilityCaseFieldSummary(
        field_name=field_name,
        valid_value_count=count,
        distribution=distribution,
        mode_value=mode_value,
        mode_count=mode_count,
        modal_agreement=modal_agreement,
        agreeing_pair_count=agreeing,
        comparable_pair_count=comparable,
        pairwise_agreement=agreeing / comparable if comparable else None,
        all_five_consistent=count == STABILITY_REPETITION_COUNT and len(distribution) == 1,
    )


def _build_report(
    *,
    run_id: str,
    protocol_sha256: str,
    trials: Sequence[StabilityTrialRecord],
) -> StabilityReport:
    case_summaries: list[StabilityCaseSummary] = []
    for case_plan in _CASE_PLANS:
        case_trials = [item for item in trials if item.case_id == case_plan.case_id]
        valid = [
            item.outcome
            for item in case_trials
            if item.outcome.status == MethodOutcomeStatus.VALID_JUDGMENT
        ]
        statuses = Counter(item.outcome.status for item in case_trials)
        fields = tuple(
            _case_field_summary(valid, field_name) for field_name in (*_FIELD_NAMES, "joint_label")
        )
        case_summaries.append(
            StabilityCaseSummary(
                case_id=case_plan.case_id,
                trace_id=case_trials[0].trace_id,
                valid_judgment_count=len(valid),
                provider_failure_count=statuses[MethodOutcomeStatus.PROVIDER_ERROR],
                parse_failure_count=statuses[MethodOutcomeStatus.PARSE_ERROR],
                other_failure_count=len(case_trials)
                - len(valid)
                - statuses[MethodOutcomeStatus.PROVIDER_ERROR]
                - statuses[MethodOutcomeStatus.PARSE_ERROR],
                parse_repair_trial_count=sum(
                    item.outcome.parse_repair_count > 0 for item in case_trials
                ),
                fields=fields,
            )
        )
    overall_fields: list[StabilityFieldSummary] = []
    for field_name in (*_FIELD_NAMES, "joint_label"):
        summaries = [
            next(item for item in case.fields if item.field_name == field_name)
            for case in case_summaries
        ]
        numerator = sum(item.agreeing_pair_count for item in summaries)
        denominator = sum(item.comparable_pair_count for item in summaries)
        overall_fields.append(
            StabilityFieldSummary(
                field_name=field_name,
                agreeing_pair_count=numerator,
                comparable_pair_count=denominator,
                pairwise_agreement=numerator / denominator if denominator else None,
                case_count_with_two_or_more_valid=sum(
                    item.valid_value_count >= 2 for item in summaries
                ),
                all_five_consistent_case_count=sum(item.all_five_consistent for item in summaries),
            )
        )
    statuses = Counter(item.outcome.status for item in trials)
    valid_count = statuses[MethodOutcomeStatus.VALID_JUDGMENT]
    return StabilityReport(
        run_id=run_id,
        generated_at=datetime.now(UTC),
        protocol_sha256=protocol_sha256,
        valid_judgment_count=valid_count,
        provider_failure_count=statuses[MethodOutcomeStatus.PROVIDER_ERROR],
        parse_failure_count=statuses[MethodOutcomeStatus.PARSE_ERROR],
        other_failure_count=len(trials)
        - valid_count
        - statuses[MethodOutcomeStatus.PROVIDER_ERROR]
        - statuses[MethodOutcomeStatus.PARSE_ERROR],
        observed_provider_call_count=sum(item.outcome.attempt_count for item in trials),
        parse_repair_trial_count=sum(item.outcome.parse_repair_count > 0 for item in trials),
        all_twenty_valid=valid_count == STABILITY_EVALUATION_COUNT,
        overall_fields=tuple(overall_fields),
        cases=tuple(case_summaries),
        conclusion_boundary=(
            "该结果只描述同一模型、同一 Prompt、四个公开 safe_mean 案例各五次评审的"
            "运行内稳定性。样本量小且案例经目的性选择，不代表 57 条主实验、其他任务、"
            "其他模型或未来服务版本的总体稳定性；不得并入冻结的 57×5 主实验。"
        ),
    )


def _format_rate(value: float | None) -> str:
    return "N/A" if value is None else f"{value * 100:.1f}%"


def _render_report_markdown(report: StabilityReport, protocol: StabilityProtocol) -> bytes:
    overall_rows = []
    labels = {
        "has_error": "has_error",
        "first_faulty_step": "first_faulty_step",
        "error_type": "error_type",
        "joint_label": "三字段联合标签",
    }
    for item in report.overall_fields:
        overall_rows.append(
            f"| {labels[item.field_name]} | {item.agreeing_pair_count}/{item.comparable_pair_count} "
            f"| {_format_rate(item.pairwise_agreement)} | "
            f"{item.all_five_consistent_case_count}/{STABILITY_CASE_COUNT} |"
        )
    case_rows = []
    for case in report.cases:
        field_map = {item.field_name: item for item in case.fields}
        case_rows.append(
            f"| {case.case_id} | {case.valid_judgment_count}/5 | "
            f"{_format_rate(field_map['has_error'].pairwise_agreement)} | "
            f"{_format_rate(field_map['first_faulty_step'].pairwise_agreement)} | "
            f"{_format_rate(field_map['error_type'].pairwise_agreement)} | "
            f"{_format_rate(field_map['joint_label'].pairwise_agreement)} |"
        )
    text = f"""# Full TraceJudge 小规模稳定性实验

## Material Passport

- Origin Skill: experiment-agent
- Origin Mode: run
- Origin Date: {report.generated_at.date().isoformat()}
- Verification Status: {report.verification_status}
- Version Label: {protocol.study_id}
- Run ID: `{report.run_id}`
- Protocol SHA256: `{report.protocol_sha256}`

## 研究问题与固定设计

{protocol.research_question}

- 方法：`full_tracejudge`
- 案例：4 个公开 `safe_mean` 固定案例
- 重复：每案例 5 次，共 20 个独立评审单元
- 顺序：每轮依次运行 normal、reasoning swap、boundary、equivalent，共 5 轮
- 温度：{protocol.temperature}
- JSON 策略：严格 Schema，最多一次修复；名义 20 次、最多 40 次底层 Provider 请求
- 独立性：新 run ID、新目录、新报告；`main_experiment_merge_allowed=false`

## 运行核算

- 有效判断：{report.valid_judgment_count}/20
- Provider 失败：{report.provider_failure_count}
- 最终解析失败：{report.parse_failure_count}
- 其他失败：{report.other_failure_count}
- 实际底层 Provider 请求：{report.observed_provider_call_count}
- 使用过 JSON 修复的评审单元：{report.parse_repair_trial_count}

## 总体字段一致性

这里的“一致率”是在每个案例内部枚举有效重复之间的所有成对比较，再合并分子和分母；失败记录不进入字段比较，而在上节单独报告。

| 字段 | 一致对/可比对 | 成对一致率 | 四案例中完整 5 次全一致 |
|---|---:|---:|---:|
{chr(10).join(overall_rows)}

## 分案例结果

| 案例 | 有效判断 | has_error | 首错步骤 | 错误类型 | 联合标签 |
|---|---:|---:|---:|---:|---:|
{chr(10).join(case_rows)}

`<none>` 是一个显式类别：无错误判断的首错步骤和错误类型为空，不等于缺失运行。完整分布保存在 `report.json`。

## 结论边界

{report.conclusion_boundary}
"""
    return text.encode("utf-8")


async def run_judge_stability(
    *,
    run_id: str,
    provider: Phase3JudgeProvider,
    source_bundle_path: str | Path,
    execution_run_dir: str | Path,
    output_dir: str | Path = STABILITY_OUTPUT_DIR,
    repo_root: str | Path = ".",
    temperature: float = 0.0,
    timeout_seconds: float = 120.0,
    resume: bool = False,
    allow_dirty: bool = False,
    privacy_canaries: Sequence[str | bytes] = (),
) -> StabilityRunResult:
    """Run or resume the fixed study; every completed evaluation is checkpointed."""

    try:
        configuration = dict(provider.public_configuration())
    except Exception:
        raise Phase4StabilityError(
            "judge provider public configuration is unavailable",
            safe_stage="P4_STABILITY_PROVIDER",
        ) from None
    prepared = _prepare_stability(
        run_id=run_id,
        provider_configuration=configuration,
        source_bundle_path=source_bundle_path,
        execution_run_dir=execution_run_dir,
        output_dir=output_dir,
        repo_root=repo_root,
        temperature=temperature,
        timeout_seconds=timeout_seconds,
        resume=resume,
        allow_dirty=allow_dirty,
        privacy_canaries=privacy_canaries,
    )
    if provider.name != prepared.protocol.provider or provider.model != prepared.protocol.model:
        raise Phase4StabilityError(
            "runtime provider identity differs from the protocol",
            safe_stage="P4_STABILITY_PROVIDER_IDENTITY",
        )
    if provider_config_sha256(provider) != prepared.protocol.provider_config_sha256:
        raise Phase4StabilityError(
            "runtime provider configuration differs from the protocol",
            safe_stage="P4_STABILITY_PROVIDER_IDENTITY",
        )
    if resume:
        created_at, existing = _load_existing_trials(prepared)
        trials = list(existing)
    else:
        created_at = _initialize_run(prepared)
        trials = []
    schedule = _trial_plan(prepared.protocol)
    for plan in schedule[len(trials) :]:
        material = prepared.materials[plan.case.case_id]
        evaluation = await evaluate_method(
            run_id=run_id,
            spec=prepared.spec,
            material=material,
            provider=provider,
        )
        if evaluation.outcome.method_input_sha256 != plan.case.method_input_sha256:
            raise Phase4StabilityError(
                "runtime method input differs from the frozen protocol",
                safe_stage="P4_STABILITY_INPUT_IDENTITY",
            )
        trial = StabilityTrialRecord(
            run_id=run_id,
            trial_id=plan.trial_id,
            trial_index=plan.trial_index,
            repetition_index=plan.repetition_index,
            case_id=plan.case.case_id,
            trace_id=plan.case.trace_id,
            outcome=evaluation.outcome,
        )
        assert_public_payload_safe(trial, canaries=privacy_canaries)
        _atomic_write(prepared.run_dir / "trials" / plan.filename, _json_bytes(trial))
        trials.append(trial)
        running_manifest = _manifest(
            run_id=run_id,
            status="running",
            created_at=created_at,
            protocol_sha256=prepared.protocol_sha256,
            trials=trials,
        )
        _atomic_write(prepared.run_dir / "manifest.json", _json_bytes(running_manifest))

    results_payload = b"".join(_json_bytes(item, pretty=False) for item in trials)
    results_path = prepared.run_dir / "results.jsonl"
    _atomic_write(results_path, results_payload)
    report = _build_report(
        run_id=run_id,
        protocol_sha256=prepared.protocol_sha256,
        trials=trials,
    )
    assert_public_payload_safe(report, canaries=privacy_canaries)
    report_json_path = prepared.run_dir / "report.json"
    report_markdown_path = prepared.run_dir / "REPORT.md"
    _atomic_write(report_json_path, _json_bytes(report))
    _atomic_write(
        report_markdown_path,
        _render_report_markdown(report, prepared.protocol),
    )
    results_sha256 = _file_sha256(results_path)
    report_json_sha256 = _file_sha256(report_json_path)
    report_markdown_sha256 = _file_sha256(report_markdown_path)
    terminal_status: Literal["completed", "completed_with_failures"] = (
        "completed" if report.all_twenty_valid else "completed_with_failures"
    )
    final_manifest = _manifest(
        run_id=run_id,
        status=terminal_status,
        created_at=created_at,
        protocol_sha256=prepared.protocol_sha256,
        trials=trials,
        results_sha256=results_sha256,
        report_json_sha256=report_json_sha256,
        report_markdown_sha256=report_markdown_sha256,
    )
    manifest_path = prepared.run_dir / "manifest.json"
    _atomic_write(manifest_path, _json_bytes(final_manifest))
    return StabilityRunResult(
        run_id=run_id,
        run_dir=prepared.run_dir,
        manifest_path=manifest_path,
        protocol_path=prepared.run_dir / "protocol.json",
        results_path=results_path,
        report_json_path=report_json_path,
        report_markdown_path=report_markdown_path,
        manifest_sha256=_file_sha256(manifest_path),
        results_sha256=results_sha256,
        report_json_sha256=report_json_sha256,
        report_markdown_sha256=report_markdown_sha256,
        valid_judgment_count=report.valid_judgment_count,
        provider_failure_count=report.provider_failure_count,
        parse_failure_count=report.parse_failure_count,
        observed_provider_call_count=report.observed_provider_call_count,
    )


async def execute_hy3_judge_stability(
    *,
    confirm_real_provider: bool,
    run_id: str,
    source_bundle_path: str | Path,
    execution_run_dir: str | Path,
    output_dir: str | Path = STABILITY_OUTPUT_DIR,
    repo_root: str | Path = ".",
    temperature: float = 0.0,
    timeout_seconds: float = 120.0,
    resume: bool = False,
    allow_dirty: bool = False,
    settings: Settings | None = None,
    privacy_canaries: Sequence[str | bytes] = (),
) -> StabilityRunResult:
    """Execute the real Hy3 study only after an explicit cost-bearing confirmation."""

    if not confirm_real_provider:
        raise Phase4StabilityError(
            "real Provider execution requires explicit confirmation",
            safe_stage="P4_STABILITY_REAL_PROVIDER_CONFIRMATION",
        )
    configured = settings or get_settings()
    model = configured.hy3_model
    if not isinstance(model, str) or not model.strip():
        raise Phase4StabilityError(
            "Hy3 model is not configured", safe_stage="P4_STABILITY_PROVIDER"
        )
    judge = Phase3Hy3JudgeProvider(configured, model=model)
    try:
        return await run_judge_stability(
            run_id=run_id,
            provider=judge,
            source_bundle_path=source_bundle_path,
            execution_run_dir=execution_run_dir,
            output_dir=output_dir,
            repo_root=repo_root,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
            resume=resume,
            allow_dirty=allow_dirty,
            privacy_canaries=privacy_canaries,
        )
    finally:
        await judge.aclose()


__all__ = [
    "STABILITY_CASE_COUNT",
    "STABILITY_EVALUATION_COUNT",
    "STABILITY_ORDERING_POLICY",
    "STABILITY_OUTPUT_DIR",
    "STABILITY_REPETITION_COUNT",
    "STABILITY_STUDY_ID",
    "Phase4StabilityError",
    "StabilityCaseDefinition",
    "StabilityCaseFieldSummary",
    "StabilityCaseSummary",
    "StabilityFieldSummary",
    "StabilityGitIdentity",
    "StabilityPreflight",
    "StabilityProtocol",
    "StabilityReport",
    "StabilityRunManifest",
    "StabilityRunResult",
    "StabilityTrialRecord",
    "execute_hy3_judge_stability",
    "preflight_hy3_judge_stability",
    "preflight_judge_stability",
    "run_judge_stability",
    "stability_implementation_sha256",
]
