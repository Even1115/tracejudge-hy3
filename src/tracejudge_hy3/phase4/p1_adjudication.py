"""Private, versioned pending record for the single P1 label disagreement.

This module deliberately reads only the aggregate-only agreement bundle.  It
does not open either rater's per-item annotations and therefore cannot identify
or decide the disputed item.  Those actions remain assigned to an authorized
human adjudicator after the raw-agreement freeze.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, ValidationError, model_validator

from tracejudge_hy3.phase3.annotations import (
    BlindedAnnotationTask,
    _fsync_directory,
    _write_new_file,
)
from tracejudge_hy3.schemas.evaluation import ErrorType, FaultyLayer

from .contracts import Identifier, Phase4Contract, Sha256
from .p1_agreement import (
    P1_AGREEMENT_DEFAULT_MANIFEST,
    P1AgreementManifest,
    P1InterRaterAgreementAnalysis,
)
from .p1_annotations import Phase4P1AnnotationError, _decode_json, _json_bytes
from .p1_formal_packet import (
    P1_FORMAL_PACKET_DEFAULT_OUTPUT,
    P1_FORMAL_PACKET_ID,
    P1FormalPacketManifest,
)
from .p1_study import _assert_private_location

P1_ADJUDICATION_ID = "phase4_p1_single_disagreement_adjudication_v1"
P1_ADJUDICATION_BUNDLE_ID = "phase4_p1_adjudication_pending_v1"
P1_ADJUDICATION_DEFAULT_OUTPUT = "artifacts/experiments/phase4-p1-adjudication"
P1_ADJUDICATION_DEFAULT_MANIFEST = (
    f"{P1_ADJUDICATION_DEFAULT_OUTPUT}/{P1_ADJUDICATION_BUNDLE_ID}/manifest.json"
)
P1_ADJUDICATION_COMPLETED_BUNDLE_ID = "phase4_p1_adjudication_completed_v1"
P1_ADJUDICATION_COMPLETED_DEFAULT_MANIFEST = (
    f"{P1_ADJUDICATION_DEFAULT_OUTPUT}/{P1_ADJUDICATION_COMPLETED_BUNDLE_ID}/manifest.json"
)
P1_ADJUDICATION_SOURCE_PENDING_MANIFEST_SHA256 = (
    "5dc8e34b1e6842b41db294b035e374afc2df77899433362b8af80b74c0da9009"
)
P1_ADJUDICATION_SOURCE_PENDING_RECORD_SHA256 = (
    "c424d94a843d8048b989ec9980038063e367938e81ad69a9fd914dd28f6598ec"
)
P1_ADJUDICATION_SOURCE_FORMAL_PACKET_MANIFEST_SHA256 = (
    "8297183a615e53f62dff40bed33c3b2d83f3b3ed45ba06b3f8882759f6fcde2f"
)
P1_ADJUDICATION_SOURCE_FORMAL_PACKET_MANIFEST = (
    f"{P1_FORMAL_PACKET_DEFAULT_OUTPUT}/{P1_FORMAL_PACKET_ID}/manifest.json"
)
P1_ADJUDICATION_SOURCE_AGREEMENT_MANIFEST_SHA256 = (
    "20d11548ed638c34bb9054d12893e28bd5c18e3028091dc5186e914182471c76"
)
P1_ADJUDICATION_SOURCE_AGREEMENT_ANALYSIS_SHA256 = (
    "fe9c66d505c0ce472deb652676ac38ea4d6849547323a1e3061ad1d9deea2135"
)
P1_ADJUDICATION_ORIGIN_DATE = "2026-09-04"


class P1AdjudicationDisagreementSummary(Phase4Contract):
    compared_item_count: Literal[20] = 20
    full_record_disagreement_count: Literal[1] = 1
    has_error_disagreement_count: Literal[0] = 0
    process_correct_disagreement_count: Literal[0] = 0
    reasoning_correct_disagreement_count: Literal[0] = 0
    plan_code_aligned_disagreement_count: Literal[1] = 1
    first_faulty_layer_disagreement_count: Literal[1] = 1
    first_faulty_step_disagreement_count: Literal[1] = 1
    error_type_disagreement_count: Literal[1] = 1
    joint_fault_label_disagreement_count: Literal[1] = 1
    all_observed_field_disagreements_belong_to_one_item: Literal[True] = True


class P1PendingAdjudicationRecord(Phase4Contract):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase4_p1_adjudication_record"] = (
        "tracejudge_phase4_p1_adjudication_record"
    )
    adjudication_id: Literal[P1_ADJUDICATION_ID] = P1_ADJUDICATION_ID
    record_version: Literal[1] = 1
    status: Literal["pending_human_review"] = "pending_human_review"
    source_agreement_manifest_sha256: Sha256
    source_agreement_analysis_sha256: Sha256
    source_primary_manifest_sha256: Sha256
    source_primary_annotations_sha256: Sha256
    source_secondary_manifest_sha256: Sha256
    source_secondary_annotations_sha256: Sha256
    disagreement_summary: P1AdjudicationDisagreementSummary
    adjudication_scope: Literal["single_full_record_disagreement"] = (
        "single_full_record_disagreement"
    )
    required_resolution_modes: tuple[
        Literal["independent_third_adjudicator", "documented_consensus", "retain_disagreement"],
        ...,
    ] = (
        "independent_third_adjudicator",
        "documented_consensus",
        "retain_disagreement",
    )
    disagreement_annotation_item_id: None = None
    disagreement_trace_id_sha256: None = None
    primary_label_sha256: None = None
    secondary_label_sha256: None = None
    case_material_sha256: None = None
    adjudicator_id: None = None
    adjudication_mode: None = None
    adjudicator_blinded_to_method_predictions: None = None
    decision: None = None
    decision_rationale: None = None
    completed_at: None = None
    original_primary_labels_unchanged: Literal[True] = True
    original_secondary_labels_unchanged: Literal[True] = True
    raw_agreement_result_unchanged: Literal[True] = True
    adjudication_not_applied_to_primary_results: Literal[True] = True
    contains_item_identity: Literal[False] = False
    contains_original_labels: Literal[False] = False
    contains_rationales: Literal[False] = False
    raw_participant_data_accessed_by_initializer: Literal[False] = False


class P1AdjudicationWorkingTemplate(Phase4Contract):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase4_p1_adjudication_working_record"] = (
        "tracejudge_phase4_p1_adjudication_working_record"
    )
    adjudication_id: Literal[P1_ADJUDICATION_ID] = P1_ADJUDICATION_ID
    source_pending_record_sha256: Sha256
    status: Literal["pending"] = "pending"
    disagreement_annotation_item_id: Identifier | None = None
    disagreement_trace_id_sha256: Sha256 | None = None
    primary_label_sha256: Sha256 | None = None
    secondary_label_sha256: Sha256 | None = None
    case_material_sha256: Sha256 | None = None
    adjudicator_id: Identifier | None = None
    adjudication_mode: (
        Literal["independent_third_adjudicator", "documented_consensus", "retain_disagreement"]
        | None
    ) = None
    adjudicator_blinded_to_method_predictions: bool | None = None
    decision_process_correct: bool | None = None
    decision_has_error: bool | None = None
    decision_reasoning_correct: bool | None = None
    decision_plan_code_aligned: bool | None = None
    decision_first_faulty_layer: (
        Literal["requirement", "reasoning", "alignment", "implementation", "execution"] | None
    ) = None
    decision_first_faulty_step: Identifier | None = None
    decision_error_type: str | None = Field(default=None, max_length=128)
    decision_rationale: str | None = Field(default=None, max_length=2000)
    adjudication_started_at: datetime | None = None
    adjudication_completed_at: datetime | None = None
    original_primary_labels_unchanged: Literal[True] = True
    original_secondary_labels_unchanged: Literal[True] = True
    raw_agreement_result_unchanged: Literal[True] = True

    @model_validator(mode="after")
    def validate_pending_template(self) -> Self:
        populated = (
            self.disagreement_annotation_item_id,
            self.disagreement_trace_id_sha256,
            self.primary_label_sha256,
            self.secondary_label_sha256,
            self.case_material_sha256,
            self.adjudicator_id,
            self.adjudication_mode,
            self.adjudicator_blinded_to_method_predictions,
            self.decision_process_correct,
            self.decision_has_error,
            self.decision_reasoning_correct,
            self.decision_plan_code_aligned,
            self.decision_first_faulty_layer,
            self.decision_first_faulty_step,
            self.decision_error_type,
            self.decision_rationale,
            self.adjudication_started_at,
            self.adjudication_completed_at,
        )
        if any(value is not None for value in populated):
            raise ValueError("frozen pending template cannot contain an adjudication decision")
        return self


class P1AdjudicationBundleManifest(Phase4Contract):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase4_p1_adjudication_bundle"] = (
        "tracejudge_phase4_p1_adjudication_bundle"
    )
    bundle_id: Literal[P1_ADJUDICATION_BUNDLE_ID] = P1_ADJUDICATION_BUNDLE_ID
    adjudication_id: Literal[P1_ADJUDICATION_ID] = P1_ADJUDICATION_ID
    record_version: Literal[1] = 1
    status: Literal["pending_human_review"] = "pending_human_review"
    created_at: datetime
    record_path: Literal["adjudication_record.json"] = "adjudication_record.json"
    working_template_path: Literal["adjudication_working_template.json"] = (
        "adjudication_working_template.json"
    )
    instructions_path: Literal["INSTRUCTIONS.md"] = "INSTRUCTIONS.md"
    record_sha256: Sha256
    working_template_sha256: Sha256
    instructions_sha256: Sha256
    source_agreement_manifest_sha256: Sha256
    source_agreement_analysis_sha256: Sha256
    raw_participant_data_accessed: Literal[False] = False
    contains_item_identity: Literal[False] = False
    contains_original_labels: Literal[False] = False
    contains_rationales: Literal[False] = False
    original_labels_unchanged: Literal[True] = True

    @model_validator(mode="after")
    def validate_created_at(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("adjudication bundle timestamp must be timezone-aware")
        return self


class P1AdjudicationDecision(Phase4Contract):
    decision_scope: Literal["disputed_fields_only"] = "disputed_fields_only"
    different_fields: tuple[
        Literal[
            "plan_code_aligned",
            "first_faulty_layer",
            "first_faulty_step",
            "error_type",
        ],
        ...,
    ] = (
        "plan_code_aligned",
        "first_faulty_layer",
        "first_faulty_step",
        "error_type",
    )
    plan_code_aligned: bool
    first_faulty_layer: FaultyLayer
    first_faulty_step: Identifier
    error_type: ErrorType

    @model_validator(mode="after")
    def validate_disputed_fields(self) -> Self:
        if self.different_fields != (
            "plan_code_aligned",
            "first_faulty_layer",
            "first_faulty_step",
            "error_type",
        ):
            raise ValueError("completed decision must preserve the observed field order")
        return self


class P1CompletedAdjudicationRecord(Phase4Contract):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase4_p1_completed_adjudication_record"] = (
        "tracejudge_phase4_p1_completed_adjudication_record"
    )
    adjudication_id: Literal[P1_ADJUDICATION_ID] = P1_ADJUDICATION_ID
    record_version: Literal[1] = 1
    status: Literal["completed_human_consensus"] = "completed_human_consensus"
    source_pending_manifest_sha256: Sha256
    source_pending_record_sha256: Sha256
    source_agreement_manifest_sha256: Sha256
    source_agreement_analysis_sha256: Sha256
    source_primary_manifest_sha256: Sha256
    source_primary_annotations_sha256: Sha256
    source_secondary_manifest_sha256: Sha256
    source_secondary_annotations_sha256: Sha256
    source_formal_packet_manifest_sha256: Sha256
    source_participant_packet_sha256: Sha256
    annotation_item_id: Identifier
    case_material_sha256: Sha256
    code_sha256: Sha256
    structured_explanation_sha256: Sha256
    functional_evidence_sha256: Sha256
    adjudication_mode: Literal["documented_consensus"] = "documented_consensus"
    decision_authority: Literal["two_original_human_raters"] = "two_original_human_raters"
    confirming_roles: tuple[Literal["primary_rater", "secondary_rater"], ...] = (
        "primary_rater",
        "secondary_rater",
    )
    both_original_raters_confirmed: Literal[True] = True
    adjudicators_blinded_to_method_predictions: Literal[True] = True
    decision: P1AdjudicationDecision
    decision_rationale: str = Field(min_length=1, max_length=2000)
    adjudication_started_at: datetime
    adjudication_completed_at: datetime
    ai_assistance_disclosed: Literal[True] = True
    ai_role: Literal["technical_advisory_to_coordinator_not_adjudicator"] = (
        "technical_advisory_to_coordinator_not_adjudicator"
    )
    ai_advisory_timing: Literal["before_reported_human_consensus"] = (
        "before_reported_human_consensus"
    )
    ai_advisory_visibility_to_both_raters: Literal["not_reported"] = "not_reported"
    per_item_original_label_hashes_included: Literal[False] = False
    per_item_original_label_hashes_omission_reason: Literal[
        "least_privilege_no_raw_label_access_by_completer"
    ] = "least_privilege_no_raw_label_access_by_completer"
    non_disputed_fields_copied_into_decision: Literal[False] = False
    original_primary_labels_unchanged: Literal[True] = True
    original_secondary_labels_unchanged: Literal[True] = True
    raw_agreement_result_unchanged: Literal[True] = True
    adjudication_not_applied_to_primary_results: Literal[True] = True
    contains_item_identity: Literal[True] = True
    contains_original_labels: Literal[False] = False
    contains_original_rationales: Literal[False] = False
    contains_method_predictions: Literal[False] = False
    contains_adjudication_rationale: Literal[True] = True
    raw_participant_label_data_accessed_by_completer: Literal[False] = False
    non_label_case_material_accessed_by_completer: Literal[True] = True

    @model_validator(mode="after")
    def validate_completion(self) -> Self:
        if self.adjudication_started_at.tzinfo is None:
            raise ValueError("adjudication start timestamp must be timezone-aware")
        if self.adjudication_completed_at.tzinfo is None:
            raise ValueError("adjudication completion timestamp must be timezone-aware")
        if self.adjudication_completed_at < self.adjudication_started_at:
            raise ValueError("adjudication completion precedes its start")
        if self.confirming_roles != ("primary_rater", "secondary_rater"):
            raise ValueError("documented consensus requires both original rater roles")
        return self


class P1CompletedAdjudicationBundleManifest(Phase4Contract):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase4_p1_completed_adjudication_bundle"] = (
        "tracejudge_phase4_p1_completed_adjudication_bundle"
    )
    bundle_id: Literal[P1_ADJUDICATION_COMPLETED_BUNDLE_ID] = P1_ADJUDICATION_COMPLETED_BUNDLE_ID
    adjudication_id: Literal[P1_ADJUDICATION_ID] = P1_ADJUDICATION_ID
    record_version: Literal[1] = 1
    status: Literal["completed_human_consensus"] = "completed_human_consensus"
    created_at: datetime
    decision_path: Literal["adjudication_decision.json"] = "adjudication_decision.json"
    report_path: Literal["decision_report.md"] = "decision_report.md"
    decision_sha256: Sha256
    report_sha256: Sha256
    source_pending_manifest_sha256: Sha256
    source_pending_record_sha256: Sha256
    source_formal_packet_manifest_sha256: Sha256
    source_participant_packet_sha256: Sha256
    original_labels_unchanged: Literal[True] = True
    raw_agreement_result_unchanged: Literal[True] = True
    contains_original_labels: Literal[False] = False
    contains_method_predictions: Literal[False] = False
    raw_participant_label_data_accessed: Literal[False] = False
    public_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_created_at(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("completed adjudication bundle timestamp must be timezone-aware")
        return self


@dataclass(frozen=True, slots=True)
class P1AdjudicationPreflight:
    record: P1PendingAdjudicationRecord
    record_sha256: str
    working_template_sha256: str
    instructions_sha256: str
    ready_to_initialize: bool


@dataclass(frozen=True, slots=True)
class P1AdjudicationResult(P1AdjudicationPreflight):
    run_dir: Path
    manifest_path: Path
    record_path: Path
    working_template_path: Path
    instructions_path: Path
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class P1AdjudicationVerification:
    bundle_id: str
    status: str
    manifest_sha256: str
    record_sha256: str
    working_template_sha256: str
    instructions_sha256: str
    verified: bool


@dataclass(frozen=True, slots=True)
class P1CompletedAdjudicationPreflight:
    record: P1CompletedAdjudicationRecord
    decision_sha256: str
    report_sha256: str
    ready_to_complete: bool


@dataclass(frozen=True, slots=True)
class P1CompletedAdjudicationResult(P1CompletedAdjudicationPreflight):
    run_dir: Path
    manifest_path: Path
    decision_path: Path
    report_path: Path
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class P1CompletedAdjudicationVerification:
    bundle_id: str
    status: str
    annotation_item_id: str
    manifest_sha256: str
    decision_sha256: str
    report_sha256: str
    verified: bool


@dataclass(frozen=True, slots=True)
class _PreparedP1Adjudication:
    preflight: P1AdjudicationPreflight
    record_payload: bytes
    working_template_payload: bytes
    instructions_payload: bytes
    output_root: Path
    run_dir: Path


@dataclass(frozen=True, slots=True)
class _PreparedP1CompletedAdjudication:
    preflight: P1CompletedAdjudicationPreflight
    decision_payload: bytes
    report_payload: bytes
    output_root: Path
    run_dir: Path


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_private(path: Path, *, label: str) -> bytes:
    _assert_private_location(path, label=label)
    if path.is_symlink() or not path.is_file():
        raise Phase4P1AnnotationError(
            f"{label} must be a regular non-symlink file",
            safe_stage="P4D_P1_ADJUDICATION_SOURCE",
        )
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise Phase4P1AnnotationError(
            f"{label} permissions are too broad",
            safe_stage="P4D_P1_ADJUDICATION_SOURCE",
        )
    try:
        return path.read_bytes()
    except OSError:
        raise Phase4P1AnnotationError(
            f"cannot read {label}", safe_stage="P4D_P1_ADJUDICATION_SOURCE"
        ) from None


def _disagreement_count(proportion: Any) -> int:
    return int(proportion.denominator - proportion.agreeing_count)


def _summary_from_analysis(
    analysis: P1InterRaterAgreementAnalysis,
) -> P1AdjudicationDisagreementSummary:
    binary = {item.field_name: item for item in analysis.binary_fields}
    localization = {item.field_name: item for item in analysis.localization_fields}
    counts = {
        "full": _disagreement_count(analysis.full_record_exact_agreement),
        **{
            field_name: _disagreement_count(binary[field_name].raw_agreement)
            for field_name in (
                "has_error",
                "process_correct",
                "reasoning_correct",
                "plan_code_aligned",
            )
        },
        **{
            field_name: _disagreement_count(
                localization[field_name].all_items_including_no_error_null
            )
            for field_name in (
                "first_faulty_layer",
                "first_faulty_step",
                "error_type",
                "joint_fault_label",
            )
        },
    }
    expected = {
        "full": 1,
        "has_error": 0,
        "process_correct": 0,
        "reasoning_correct": 0,
        "plan_code_aligned": 1,
        "first_faulty_layer": 1,
        "first_faulty_step": 1,
        "error_type": 1,
        "joint_fault_label": 1,
    }
    if counts != expected:
        raise Phase4P1AnnotationError(
            "aggregate results no longer contain exactly one expected process-detail disagreement",
            safe_stage="P4D_P1_ADJUDICATION_SCOPE",
        )
    return P1AdjudicationDisagreementSummary()


def _load_agreement_bundle(
    *,
    agreement_manifest_path: str | Path,
    expected_manifest_sha256: str | None,
) -> tuple[P1AgreementManifest, P1InterRaterAgreementAnalysis, str]:
    manifest_path = Path(agreement_manifest_path).expanduser().resolve()
    manifest_payload = _read_private(manifest_path, label="agreement manifest")
    manifest_sha256 = _sha256(manifest_payload)
    if expected_manifest_sha256 and manifest_sha256 != expected_manifest_sha256:
        raise Phase4P1AnnotationError(
            "agreement manifest differs from the adjudication source identity",
            safe_stage="P4D_P1_ADJUDICATION_SOURCE",
        )
    try:
        manifest = P1AgreementManifest.model_validate(
            _decode_json(manifest_payload, label="agreement manifest")
        )
    except ValidationError:
        raise Phase4P1AnnotationError(
            "agreement manifest failed adjudication source validation",
            safe_stage="P4D_P1_ADJUDICATION_SOURCE",
        ) from None
    analysis_payload = _read_private(
        manifest_path.parent / manifest.analysis_path, label="aggregate agreement analysis"
    )
    report_payload = _read_private(
        manifest_path.parent / manifest.report_path, label="aggregate agreement report"
    )
    if (
        _sha256(analysis_payload) != manifest.analysis_sha256
        or _sha256(report_payload) != manifest.report_sha256
    ):
        raise Phase4P1AnnotationError(
            "agreement bundle hashes are inconsistent",
            safe_stage="P4D_P1_ADJUDICATION_SOURCE",
        )
    try:
        analysis = P1InterRaterAgreementAnalysis.model_validate(
            _decode_json(analysis_payload, label="aggregate agreement analysis")
        )
    except ValidationError:
        raise Phase4P1AnnotationError(
            "aggregate agreement analysis failed schema validation",
            safe_stage="P4D_P1_ADJUDICATION_SOURCE",
        ) from None
    if (
        manifest.analysis_sha256 != P1_ADJUDICATION_SOURCE_AGREEMENT_ANALYSIS_SHA256
        and expected_manifest_sha256
    ):
        raise Phase4P1AnnotationError(
            "aggregate agreement analysis differs from the adjudication source identity",
            safe_stage="P4D_P1_ADJUDICATION_SOURCE",
        )
    if (
        analysis.contains_trace_ids
        or analysis.contains_per_item_labels
        or analysis.contains_rationales
        or analysis.disagreement_items_emitted
        or analysis.adjudication_performed
    ):
        raise Phase4P1AnnotationError(
            "agreement source is not the aggregate-only pre-adjudication result",
            safe_stage="P4D_P1_ADJUDICATION_SOURCE",
        )
    return manifest, analysis, manifest_sha256


def _render_instructions(record_sha256: str) -> str:
    return f"""## Material Passport

- Origin Skill: experiment-agent
- Origin Mode: manage
- Origin Date: {P1_ADJUDICATION_ORIGIN_DATE}
- Verification Status: UNVERIFIED
- Version Label: {P1_ADJUDICATION_BUNDLE_ID}

## 单条分歧裁决说明

本目录只是**待人工裁决记录**，不是裁决结论。初始化过程没有读取两位标注者的逐条标签，也没有识别分歧条目。

### 不可变来源

- pending record SHA256：`{record_sha256}`
- 两份原始标签、既有 raw agreement 和本目录中的三个冻结文件均不得编辑或覆盖。
- 实际裁决必须写入新的版本化完成产物，不得把 working 文件放回本目录覆盖模板。

### 授权裁决者操作

1. 仅由有权访问原始标签的协调者，在受限离线目录中复制 `adjudication_working_template.json` 作为工作文件。
2. 对照两份冻结 annotations，只定位这一条完整记录分歧；记录盲化 `formal_item_*`、trace ID 的 SHA256、两份原始标签规范化 SHA256 和案例材料 SHA256，不把真实 trace ID 写入公开文档。
3. 复核公开题面、结构化说明、候选代码、功能证据及两份原始 rationale；不得查看五方法预测或以事后方法结果倒推裁决。
4. 选择且记录一种模式：独立第三方裁决、记录在案的共识讨论、或保留分歧。不得把任一原标注者自动当作金标准。
5. 若给出最终标签，按原 `AnnotationRecord` 条件约束填写七字段和简短依据；若保留分歧，明确写明原因，不伪造共识。
6. 记录化名裁决者 ID、开始/完成时间和是否保持对方法预测盲法，然后将完成工作文件交给独立的 freeze/verify 步骤生成新版本。

### 当前状态

- `pending_human_review`
- 已知聚合范围：20 条中恰有 1 条完整七字段记录不一致；`has_error`、`process_correct`、`reasoning_correct` 无分歧，过程对齐和首错定位字段存在分歧。
- 此状态不得用于替换主标签、重算主实验成绩或宣称分歧已经解决。
"""


def prepare_p1_adjudication(
    *,
    agreement_manifest_path: str | Path = P1_AGREEMENT_DEFAULT_MANIFEST,
    expected_agreement_manifest_sha256: str | None = (
        P1_ADJUDICATION_SOURCE_AGREEMENT_MANIFEST_SHA256
    ),
    output_dir: str | Path = P1_ADJUDICATION_DEFAULT_OUTPUT,
) -> _PreparedP1Adjudication:
    manifest, analysis, manifest_sha256 = _load_agreement_bundle(
        agreement_manifest_path=agreement_manifest_path,
        expected_manifest_sha256=expected_agreement_manifest_sha256,
    )
    summary = _summary_from_analysis(analysis)
    try:
        record = P1PendingAdjudicationRecord(
            source_agreement_manifest_sha256=(
                expected_agreement_manifest_sha256 or manifest_sha256
            ),
            source_agreement_analysis_sha256=manifest.analysis_sha256,
            source_primary_manifest_sha256=analysis.source_primary_manifest_sha256,
            source_primary_annotations_sha256=analysis.source_primary_annotations_sha256,
            source_secondary_manifest_sha256=analysis.source_secondary_manifest_sha256,
            source_secondary_annotations_sha256=analysis.source_secondary_annotations_sha256,
            disagreement_summary=summary,
        )
    except ValidationError:
        raise Phase4P1AnnotationError(
            "pending adjudication record failed validation",
            safe_stage="P4D_P1_ADJUDICATION_RECORD",
        ) from None
    record_payload = _json_bytes(record)
    record_sha256 = _sha256(record_payload)
    working_template = P1AdjudicationWorkingTemplate(source_pending_record_sha256=record_sha256)
    working_template_payload = _json_bytes(working_template)
    instructions_payload = (_render_instructions(record_sha256).rstrip() + "\n").encode("utf-8")
    output_root = Path(output_dir).expanduser().resolve()
    run_dir = output_root / P1_ADJUDICATION_BUNDLE_ID
    return _PreparedP1Adjudication(
        preflight=P1AdjudicationPreflight(
            record=record,
            record_sha256=record_sha256,
            working_template_sha256=_sha256(working_template_payload),
            instructions_sha256=_sha256(instructions_payload),
            ready_to_initialize=True,
        ),
        record_payload=record_payload,
        working_template_payload=working_template_payload,
        instructions_payload=instructions_payload,
        output_root=output_root,
        run_dir=run_dir,
    )


def preflight_p1_adjudication(**kwargs: Any) -> P1AdjudicationPreflight:
    return prepare_p1_adjudication(**kwargs).preflight


def initialize_p1_adjudication(**kwargs: Any) -> P1AdjudicationResult:
    prepared = prepare_p1_adjudication(**kwargs)
    if prepared.run_dir.exists() or prepared.run_dir.is_symlink():
        raise Phase4P1AnnotationError(
            "adjudication directory already exists",
            safe_stage="P4D_P1_ADJUDICATION_OUTPUT",
        )
    prepared.output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    prepared.output_root.chmod(0o700)
    temporary_dir: Path | None = None
    try:
        temporary_dir = Path(
            tempfile.mkdtemp(prefix=f".{P1_ADJUDICATION_BUNDLE_ID}.", dir=prepared.output_root)
        )
        temporary_dir.chmod(0o700)
        _write_new_file(temporary_dir / "adjudication_record.json", prepared.record_payload)
        _write_new_file(
            temporary_dir / "adjudication_working_template.json",
            prepared.working_template_payload,
        )
        _write_new_file(temporary_dir / "INSTRUCTIONS.md", prepared.instructions_payload)
        manifest = P1AdjudicationBundleManifest(
            created_at=datetime.now(UTC),
            record_sha256=prepared.preflight.record_sha256,
            working_template_sha256=prepared.preflight.working_template_sha256,
            instructions_sha256=prepared.preflight.instructions_sha256,
            source_agreement_manifest_sha256=(
                prepared.preflight.record.source_agreement_manifest_sha256
            ),
            source_agreement_analysis_sha256=(
                prepared.preflight.record.source_agreement_analysis_sha256
            ),
        )
        manifest_payload = _json_bytes(manifest)
        _write_new_file(temporary_dir / "manifest.json", manifest_payload)
        os.replace(temporary_dir, prepared.run_dir)
        temporary_dir = None
        _fsync_directory(prepared.output_root)
    except OSError:
        raise Phase4P1AnnotationError(
            "cannot atomically initialize adjudication bundle",
            safe_stage="P4D_P1_ADJUDICATION_OUTPUT",
        ) from None
    finally:
        if temporary_dir is not None:
            shutil.rmtree(temporary_dir, ignore_errors=True)
    manifest_path = prepared.run_dir / "manifest.json"
    return P1AdjudicationResult(
        **asdict(prepared.preflight),
        run_dir=prepared.run_dir,
        manifest_path=manifest_path,
        record_path=prepared.run_dir / "adjudication_record.json",
        working_template_path=prepared.run_dir / "adjudication_working_template.json",
        instructions_path=prepared.run_dir / "INSTRUCTIONS.md",
        manifest_sha256=_sha256(manifest_path.read_bytes()),
    )


def verify_p1_adjudication(
    *,
    manifest_path: str | Path = P1_ADJUDICATION_DEFAULT_MANIFEST,
    expected_manifest_sha256: str | None = None,
) -> P1AdjudicationVerification:
    path = Path(manifest_path).expanduser().resolve()
    payload = _read_private(path, label="adjudication manifest")
    manifest_sha256 = _sha256(payload)
    if expected_manifest_sha256 and manifest_sha256 != expected_manifest_sha256:
        raise Phase4P1AnnotationError(
            "adjudication manifest differs from the expected identity",
            safe_stage="P4D_P1_ADJUDICATION_VERIFY",
        )
    try:
        manifest = P1AdjudicationBundleManifest.model_validate(
            _decode_json(payload, label="adjudication manifest")
        )
    except ValidationError:
        raise Phase4P1AnnotationError(
            "adjudication manifest failed schema validation",
            safe_stage="P4D_P1_ADJUDICATION_VERIFY",
        ) from None
    record_payload = _read_private(path.parent / manifest.record_path, label="pending record")
    template_payload = _read_private(
        path.parent / manifest.working_template_path, label="working template"
    )
    instructions_payload = _read_private(
        path.parent / manifest.instructions_path, label="adjudication instructions"
    )
    if (
        _sha256(record_payload) != manifest.record_sha256
        or _sha256(template_payload) != manifest.working_template_sha256
        or _sha256(instructions_payload) != manifest.instructions_sha256
    ):
        raise Phase4P1AnnotationError(
            "adjudication bundle hashes are inconsistent",
            safe_stage="P4D_P1_ADJUDICATION_VERIFY",
        )
    try:
        record = P1PendingAdjudicationRecord.model_validate(
            _decode_json(record_payload, label="pending record")
        )
        template = P1AdjudicationWorkingTemplate.model_validate(
            _decode_json(template_payload, label="working template")
        )
    except ValidationError:
        raise Phase4P1AnnotationError(
            "adjudication bundle payload failed schema validation",
            safe_stage="P4D_P1_ADJUDICATION_VERIFY",
        ) from None
    if (
        template.source_pending_record_sha256 != manifest.record_sha256
        or record.source_agreement_manifest_sha256 != manifest.source_agreement_manifest_sha256
        or record.source_agreement_analysis_sha256 != manifest.source_agreement_analysis_sha256
    ):
        raise Phase4P1AnnotationError(
            "adjudication bundle source bindings are inconsistent",
            safe_stage="P4D_P1_ADJUDICATION_VERIFY",
        )
    return P1AdjudicationVerification(
        bundle_id=manifest.bundle_id,
        status=manifest.status,
        manifest_sha256=manifest_sha256,
        record_sha256=manifest.record_sha256,
        working_template_sha256=manifest.working_template_sha256,
        instructions_sha256=manifest.instructions_sha256,
        verified=True,
    )


def _load_pending_adjudication_source(
    *,
    manifest_path: str | Path,
    expected_manifest_sha256: str | None,
) -> tuple[P1AdjudicationBundleManifest, P1PendingAdjudicationRecord, str]:
    verification = verify_p1_adjudication(
        manifest_path=manifest_path,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    path = Path(manifest_path).expanduser().resolve()
    manifest_payload = _read_private(path, label="source pending adjudication manifest")
    try:
        manifest = P1AdjudicationBundleManifest.model_validate(
            _decode_json(manifest_payload, label="source pending adjudication manifest")
        )
        record_payload = _read_private(
            path.parent / manifest.record_path,
            label="source pending adjudication record",
        )
        record = P1PendingAdjudicationRecord.model_validate(
            _decode_json(record_payload, label="source pending adjudication record")
        )
    except ValidationError:
        raise Phase4P1AnnotationError(
            "source pending adjudication bundle failed completion validation",
            safe_stage="P4D_P1_ADJUDICATION_COMPLETE_SOURCE",
        ) from None
    if verification.record_sha256 != _sha256(record_payload):
        raise Phase4P1AnnotationError(
            "source pending adjudication record binding is inconsistent",
            safe_stage="P4D_P1_ADJUDICATION_COMPLETE_SOURCE",
        )
    if (
        expected_manifest_sha256 is not None
        and verification.record_sha256 != P1_ADJUDICATION_SOURCE_PENDING_RECORD_SHA256
    ):
        raise Phase4P1AnnotationError(
            "source pending adjudication record differs from the completion identity",
            safe_stage="P4D_P1_ADJUDICATION_COMPLETE_SOURCE",
        )
    return manifest, record, verification.manifest_sha256


def _load_non_label_case_material(
    *,
    formal_packet_manifest_path: str | Path,
    expected_manifest_sha256: str | None,
    annotation_item_id: str,
) -> tuple[P1FormalPacketManifest, BlindedAnnotationTask, str, str]:
    manifest_path = Path(formal_packet_manifest_path).expanduser().resolve()
    manifest_payload = _read_private(manifest_path, label="formal packet manifest")
    manifest_sha256 = _sha256(manifest_payload)
    if expected_manifest_sha256 and manifest_sha256 != expected_manifest_sha256:
        raise Phase4P1AnnotationError(
            "formal packet manifest differs from the adjudication case source identity",
            safe_stage="P4D_P1_ADJUDICATION_COMPLETE_CASE",
        )
    try:
        manifest = P1FormalPacketManifest.model_validate(
            _decode_json(manifest_payload, label="formal packet manifest")
        )
    except ValidationError:
        raise Phase4P1AnnotationError(
            "formal packet manifest failed adjudication case validation",
            safe_stage="P4D_P1_ADJUDICATION_COMPLETE_CASE",
        ) from None
    packet_payload = _read_private(
        manifest_path.parent / manifest.participant_packet_path,
        label="non-label participant case packet",
    )
    packet_sha256 = _sha256(packet_payload)
    if packet_sha256 != manifest.participant_packet_sha256:
        raise Phase4P1AnnotationError(
            "formal packet case material hash is inconsistent",
            safe_stage="P4D_P1_ADJUDICATION_COMPLETE_CASE",
        )
    try:
        text = packet_payload.decode("utf-8")
    except UnicodeDecodeError:
        raise Phase4P1AnnotationError(
            "formal packet case material is not UTF-8 JSONL",
            safe_stage="P4D_P1_ADJUDICATION_COMPLETE_CASE",
        ) from None
    lines = text.splitlines()
    if len(lines) != manifest.item_count or any(not line.strip() for line in lines):
        raise Phase4P1AnnotationError(
            "formal packet case material has an unexpected row count",
            safe_stage="P4D_P1_ADJUDICATION_COMPLETE_CASE",
        )
    cases: list[BlindedAnnotationTask] = []
    for line in lines:
        try:
            cases.append(
                BlindedAnnotationTask.model_validate(
                    _decode_json(line.encode("utf-8"), label="formal packet case")
                )
            )
        except ValidationError:
            raise Phase4P1AnnotationError(
                "formal packet case material failed schema validation",
                safe_stage="P4D_P1_ADJUDICATION_COMPLETE_CASE",
            ) from None
    if tuple(case.annotation_item_id for case in cases) != manifest.ordered_annotation_item_ids:
        raise Phase4P1AnnotationError(
            "formal packet case order differs from its manifest",
            safe_stage="P4D_P1_ADJUDICATION_COMPLETE_CASE",
        )
    selected = [case for case in cases if case.annotation_item_id == annotation_item_id]
    if len(selected) != 1:
        raise Phase4P1AnnotationError(
            "adjudication item is not unique in the formal packet",
            safe_stage="P4D_P1_ADJUDICATION_COMPLETE_CASE",
        )
    case = selected[0]
    return manifest, case, manifest_sha256, _sha256(_json_bytes(case))


def _render_completed_report(
    record: P1CompletedAdjudicationRecord,
    *,
    decision_sha256: str,
) -> str:
    decision = record.decision
    return f"""## Material Passport

- Origin Skill: experiment-agent
- Origin Mode: manage
- Origin Date: {P1_ADJUDICATION_ORIGIN_DATE}
- Verification Status: VERIFIED_BY_BUNDLE_CHECK
- Version Label: {P1_ADJUDICATION_COMPLETED_BUNDLE_ID}
- Privacy: PRIVATE_RESTRICTED

## P1 单条分歧裁决记录

- 状态：`{record.status}`
- 裁决模式：`{record.adjudication_mode}`
- 决策权：两位原始人类标注者记录在案的共识
- 盲化条目：`{record.annotation_item_id}`
- 裁决范围：仅限原始记录中存在分歧的四个字段
- 开始时间：`{record.adjudication_started_at.isoformat()}`
- 完成时间：`{record.adjudication_completed_at.isoformat()}`
- 双方确认：是
- 对方法预测保持盲法：是

### 最终决定

- `plan_code_aligned`: `{str(decision.plan_code_aligned).lower()}`
- `first_faulty_layer`: `{decision.first_faulty_layer}`
- `first_faulty_step`: `{decision.first_faulty_step}`
- `error_type`: `{decision.error_type.value}`

### 裁决理由

{record.decision_rationale}

### 不可变性与披露

- 两份原始标签与原始一致性统计均未覆盖或修改；本记录是独立追加的新版本。
- 完成器没有读取两份逐条原标签，原标签整文件仅由 pending 记录中的既有 SHA256 绑定；因此不伪造逐条原标签哈希。
- 完成器只读取正式包中的非标签案例材料以绑定案例身份；本包不含原始标签、原始 rationale 或方法预测。
- Codex 在报告共识前曾向协调者提供技术性初审建议，但不是裁决者；该建议是否在双方独立复核前提供给两位标注者，未报告。
- 本决定尚未应用于第一标注者主标签或主实验结果。
- decision SHA256：`{decision_sha256}`
"""


def prepare_p1_consensus_adjudication(
    *,
    annotation_item_id: str,
    plan_code_aligned: bool,
    first_faulty_layer: FaultyLayer,
    first_faulty_step: str,
    error_type: ErrorType | str,
    decision_rationale: str,
    adjudication_started_at: datetime,
    adjudication_completed_at: datetime,
    both_original_raters_confirmed: bool,
    adjudicators_blinded_to_method_predictions: bool,
    pending_manifest_path: str | Path = P1_ADJUDICATION_DEFAULT_MANIFEST,
    expected_pending_manifest_sha256: str | None = (P1_ADJUDICATION_SOURCE_PENDING_MANIFEST_SHA256),
    formal_packet_manifest_path: str | Path = P1_ADJUDICATION_SOURCE_FORMAL_PACKET_MANIFEST,
    expected_formal_packet_manifest_sha256: str | None = (
        P1_ADJUDICATION_SOURCE_FORMAL_PACKET_MANIFEST_SHA256
    ),
    output_dir: str | Path = P1_ADJUDICATION_DEFAULT_OUTPUT,
) -> _PreparedP1CompletedAdjudication:
    if not both_original_raters_confirmed:
        raise Phase4P1AnnotationError(
            "documented consensus requires confirmation from both original raters",
            safe_stage="P4D_P1_ADJUDICATION_COMPLETE_DECISION",
        )
    if not adjudicators_blinded_to_method_predictions:
        raise Phase4P1AnnotationError(
            "completed consensus requires preserved method-prediction blinding",
            safe_stage="P4D_P1_ADJUDICATION_COMPLETE_DECISION",
        )
    pending_manifest, pending_record, pending_manifest_sha256 = _load_pending_adjudication_source(
        manifest_path=pending_manifest_path,
        expected_manifest_sha256=expected_pending_manifest_sha256,
    )
    packet_manifest, case, packet_manifest_sha256, case_material_sha256 = (
        _load_non_label_case_material(
            formal_packet_manifest_path=formal_packet_manifest_path,
            expected_manifest_sha256=expected_formal_packet_manifest_sha256,
            annotation_item_id=annotation_item_id,
        )
    )
    try:
        record = P1CompletedAdjudicationRecord(
            source_pending_manifest_sha256=pending_manifest_sha256,
            source_pending_record_sha256=pending_manifest.record_sha256,
            source_agreement_manifest_sha256=(pending_record.source_agreement_manifest_sha256),
            source_agreement_analysis_sha256=(pending_record.source_agreement_analysis_sha256),
            source_primary_manifest_sha256=pending_record.source_primary_manifest_sha256,
            source_primary_annotations_sha256=(pending_record.source_primary_annotations_sha256),
            source_secondary_manifest_sha256=(pending_record.source_secondary_manifest_sha256),
            source_secondary_annotations_sha256=(
                pending_record.source_secondary_annotations_sha256
            ),
            source_formal_packet_manifest_sha256=packet_manifest_sha256,
            source_participant_packet_sha256=packet_manifest.participant_packet_sha256,
            annotation_item_id=annotation_item_id,
            case_material_sha256=case_material_sha256,
            code_sha256=case.code_sha256,
            structured_explanation_sha256=case.structured_explanation_sha256,
            functional_evidence_sha256=case.functional_evidence_sha256,
            both_original_raters_confirmed=True,
            adjudicators_blinded_to_method_predictions=True,
            decision=P1AdjudicationDecision(
                plan_code_aligned=plan_code_aligned,
                first_faulty_layer=first_faulty_layer,
                first_faulty_step=first_faulty_step,
                error_type=error_type,
            ),
            decision_rationale=decision_rationale,
            adjudication_started_at=adjudication_started_at,
            adjudication_completed_at=adjudication_completed_at,
        )
    except ValidationError:
        raise Phase4P1AnnotationError(
            "completed consensus adjudication failed schema validation",
            safe_stage="P4D_P1_ADJUDICATION_COMPLETE_DECISION",
        ) from None
    decision_payload = _json_bytes(record)
    decision_sha256 = _sha256(decision_payload)
    report_payload = (
        _render_completed_report(record, decision_sha256=decision_sha256).rstrip() + "\n"
    ).encode("utf-8")
    output_root = Path(output_dir).expanduser().resolve()
    run_dir = output_root / P1_ADJUDICATION_COMPLETED_BUNDLE_ID
    return _PreparedP1CompletedAdjudication(
        preflight=P1CompletedAdjudicationPreflight(
            record=record,
            decision_sha256=decision_sha256,
            report_sha256=_sha256(report_payload),
            ready_to_complete=True,
        ),
        decision_payload=decision_payload,
        report_payload=report_payload,
        output_root=output_root,
        run_dir=run_dir,
    )


def preflight_p1_consensus_adjudication(**kwargs: Any) -> P1CompletedAdjudicationPreflight:
    return prepare_p1_consensus_adjudication(**kwargs).preflight


def complete_p1_consensus_adjudication(**kwargs: Any) -> P1CompletedAdjudicationResult:
    prepared = prepare_p1_consensus_adjudication(**kwargs)
    if prepared.run_dir.exists() or prepared.run_dir.is_symlink():
        raise Phase4P1AnnotationError(
            "completed adjudication directory already exists",
            safe_stage="P4D_P1_ADJUDICATION_COMPLETE_OUTPUT",
        )
    prepared.output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    prepared.output_root.chmod(0o700)
    temporary_dir: Path | None = None
    try:
        temporary_dir = Path(
            tempfile.mkdtemp(
                prefix=f".{P1_ADJUDICATION_COMPLETED_BUNDLE_ID}.",
                dir=prepared.output_root,
            )
        )
        temporary_dir.chmod(0o700)
        _write_new_file(temporary_dir / "adjudication_decision.json", prepared.decision_payload)
        _write_new_file(temporary_dir / "decision_report.md", prepared.report_payload)
        record = prepared.preflight.record
        manifest = P1CompletedAdjudicationBundleManifest(
            created_at=datetime.now(UTC),
            decision_sha256=prepared.preflight.decision_sha256,
            report_sha256=prepared.preflight.report_sha256,
            source_pending_manifest_sha256=record.source_pending_manifest_sha256,
            source_pending_record_sha256=record.source_pending_record_sha256,
            source_formal_packet_manifest_sha256=(record.source_formal_packet_manifest_sha256),
            source_participant_packet_sha256=record.source_participant_packet_sha256,
        )
        manifest_payload = _json_bytes(manifest)
        _write_new_file(temporary_dir / "manifest.json", manifest_payload)
        os.replace(temporary_dir, prepared.run_dir)
        temporary_dir = None
        _fsync_directory(prepared.output_root)
    except OSError:
        raise Phase4P1AnnotationError(
            "cannot atomically write completed adjudication bundle",
            safe_stage="P4D_P1_ADJUDICATION_COMPLETE_OUTPUT",
        ) from None
    finally:
        if temporary_dir is not None:
            shutil.rmtree(temporary_dir, ignore_errors=True)
    manifest_path = prepared.run_dir / "manifest.json"
    return P1CompletedAdjudicationResult(
        **asdict(prepared.preflight),
        run_dir=prepared.run_dir,
        manifest_path=manifest_path,
        decision_path=prepared.run_dir / "adjudication_decision.json",
        report_path=prepared.run_dir / "decision_report.md",
        manifest_sha256=_sha256(manifest_path.read_bytes()),
    )


def verify_p1_completed_adjudication(
    *,
    manifest_path: str | Path = P1_ADJUDICATION_COMPLETED_DEFAULT_MANIFEST,
    expected_manifest_sha256: str | None = None,
) -> P1CompletedAdjudicationVerification:
    path = Path(manifest_path).expanduser().resolve()
    manifest_payload = _read_private(path, label="completed adjudication manifest")
    manifest_sha256 = _sha256(manifest_payload)
    if expected_manifest_sha256 and manifest_sha256 != expected_manifest_sha256:
        raise Phase4P1AnnotationError(
            "completed adjudication manifest differs from the expected identity",
            safe_stage="P4D_P1_ADJUDICATION_COMPLETE_VERIFY",
        )
    try:
        manifest = P1CompletedAdjudicationBundleManifest.model_validate(
            _decode_json(manifest_payload, label="completed adjudication manifest")
        )
        decision_payload = _read_private(
            path.parent / manifest.decision_path,
            label="completed adjudication decision",
        )
        report_payload = _read_private(
            path.parent / manifest.report_path,
            label="completed adjudication report",
        )
        record = P1CompletedAdjudicationRecord.model_validate(
            _decode_json(decision_payload, label="completed adjudication decision")
        )
    except ValidationError:
        raise Phase4P1AnnotationError(
            "completed adjudication bundle failed schema validation",
            safe_stage="P4D_P1_ADJUDICATION_COMPLETE_VERIFY",
        ) from None
    if (
        _sha256(decision_payload) != manifest.decision_sha256
        or _sha256(report_payload) != manifest.report_sha256
    ):
        raise Phase4P1AnnotationError(
            "completed adjudication bundle hashes are inconsistent",
            safe_stage="P4D_P1_ADJUDICATION_COMPLETE_VERIFY",
        )
    expected_report = (
        _render_completed_report(record, decision_sha256=manifest.decision_sha256).rstrip() + "\n"
    ).encode("utf-8")
    if report_payload != expected_report:
        raise Phase4P1AnnotationError(
            "completed adjudication report differs from its decision record",
            safe_stage="P4D_P1_ADJUDICATION_COMPLETE_VERIFY",
        )
    if (
        record.source_pending_manifest_sha256 != manifest.source_pending_manifest_sha256
        or record.source_pending_record_sha256 != manifest.source_pending_record_sha256
        or record.source_formal_packet_manifest_sha256
        != manifest.source_formal_packet_manifest_sha256
        or record.source_participant_packet_sha256 != manifest.source_participant_packet_sha256
    ):
        raise Phase4P1AnnotationError(
            "completed adjudication source bindings are inconsistent",
            safe_stage="P4D_P1_ADJUDICATION_COMPLETE_VERIFY",
        )
    return P1CompletedAdjudicationVerification(
        bundle_id=manifest.bundle_id,
        status=manifest.status,
        annotation_item_id=record.annotation_item_id,
        manifest_sha256=manifest_sha256,
        decision_sha256=manifest.decision_sha256,
        report_sha256=manifest.report_sha256,
        verified=True,
    )


__all__ = [
    "P1_ADJUDICATION_BUNDLE_ID",
    "P1_ADJUDICATION_COMPLETED_BUNDLE_ID",
    "P1_ADJUDICATION_COMPLETED_DEFAULT_MANIFEST",
    "P1_ADJUDICATION_DEFAULT_MANIFEST",
    "P1_ADJUDICATION_DEFAULT_OUTPUT",
    "P1_ADJUDICATION_ID",
    "P1_ADJUDICATION_SOURCE_AGREEMENT_ANALYSIS_SHA256",
    "P1_ADJUDICATION_SOURCE_AGREEMENT_MANIFEST_SHA256",
    "P1AdjudicationBundleManifest",
    "P1AdjudicationDecision",
    "P1AdjudicationDisagreementSummary",
    "P1AdjudicationPreflight",
    "P1AdjudicationResult",
    "P1AdjudicationVerification",
    "P1AdjudicationWorkingTemplate",
    "P1CompletedAdjudicationBundleManifest",
    "P1CompletedAdjudicationPreflight",
    "P1CompletedAdjudicationRecord",
    "P1CompletedAdjudicationResult",
    "P1CompletedAdjudicationVerification",
    "P1PendingAdjudicationRecord",
    "complete_p1_consensus_adjudication",
    "initialize_p1_adjudication",
    "preflight_p1_consensus_adjudication",
    "preflight_p1_adjudication",
    "prepare_p1_consensus_adjudication",
    "prepare_p1_adjudication",
    "verify_p1_completed_adjudication",
    "verify_p1_adjudication",
]
