"""Aggregate-only post-adjudication sensitivity analysis for P1.

The analysis preserves the frozen raw inter-rater agreement estimands and reads
only their aggregate bundle plus the separate completed adjudication record. It
does not open either rater's per-item labels or any method predictions. The
result therefore reports exact zero-impact fields and conservative one-item
impact envelopes, not invented method-level score deltas.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, ValidationError, model_validator

from tracejudge_hy3.phase3.privacy import assert_public_payload_safe

from .contracts import Phase4Contract, Sha256
from .p1_adjudication import (
    P1_ADJUDICATION_COMPLETED_DEFAULT_MANIFEST,
    P1_ADJUDICATION_SOURCE_PENDING_MANIFEST_SHA256,
    P1CompletedAdjudicationBundleManifest,
    P1CompletedAdjudicationRecord,
    _read_private,
    _sha256,
    verify_p1_completed_adjudication,
)
from .p1_agreement import (
    P1_AGREEMENT_DEFAULT_MANIFEST,
    P1AgreementProportion,
    P1InterRaterAgreementAnalysis,
)
from .p1_annotations import _decode_json

P1_POST_ADJUDICATION_SENSITIVITY_ID = "phase4_p1_post_adjudication_sensitivity_v1"
P1_POST_ADJUDICATION_SENSITIVITY_JSON = "phase4_p1_post_adjudication_sensitivity_v1.json"
P1_POST_ADJUDICATION_SENSITIVITY_REPORT = "phase4_p1_post_adjudication_sensitivity_v1.md"
P1_POST_ADJUDICATION_SENSITIVITY_DEFAULT_OUTPUT = "docs/releases/phase4"
P1_POST_ADJUDICATION_SENSITIVITY_ORIGIN_DATE = "2026-09-04"
P1_POST_ADJUDICATION_SOURCE_AGREEMENT_MANIFEST_SHA256 = (
    "20d11548ed638c34bb9054d12893e28bd5c18e3028091dc5186e914182471c76"
)
P1_POST_ADJUDICATION_SOURCE_COMPLETED_MANIFEST_SHA256 = (
    "6e48963ee7cfe6cda2f113271286612af1640ca1abaf0eaeacedb62de2639287"
)

_BINARY_FIELD_ORDER = (
    "has_error",
    "process_correct",
    "reasoning_correct",
    "plan_code_aligned",
)
_LOCALIZATION_FIELD_ORDER = (
    "first_faulty_layer",
    "first_faulty_step",
    "error_type",
    "joint_fault_label",
)
_IMPACT_ORDER = (
    "has_error_detection",
    "process_correct",
    "reasoning_correct",
    "plan_code_aligned",
    "first_faulty_layer",
    "first_faulty_step",
    "error_type",
    "joint_fault_label",
    "full_seven_field_reference_record",
)
_FALLACY_ORDER = (
    "simpsons_paradox",
    "ecological_fallacy",
    "berksons_paradox",
    "collider_bias",
    "base_rate_neglect",
    "regression_to_the_mean",
    "survivorship_bias",
    "look_elsewhere_effect",
    "garden_of_forking_paths",
    "correlation_not_causation",
    "reverse_causality",
)


class P1PostAdjudicationSensitivityError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        safe_stage: str = "P4D_P1_POST_ADJUDICATION_SENSITIVITY",
    ) -> None:
        super().__init__(message)
        self.safe_stage = safe_stage


class P1RawBinaryAgreementSnapshot(Phase4Contract):
    field_name: Literal["has_error", "process_correct", "reasoning_correct", "plan_code_aligned"]
    raw_agreement: P1AgreementProportion
    cohen_kappa: float | None = Field(default=None, ge=-1.0, le=1.0)


class P1RawLocalizationAgreementSnapshot(Phase4Contract):
    field_name: Literal[
        "first_faulty_layer", "first_faulty_step", "error_type", "joint_fault_label"
    ]
    all_items_including_no_error_null: P1AgreementProportion
    both_error_items: P1AgreementProportion


class P1DownstreamImpactEnvelope(Phase4Contract):
    metric_name: Literal[
        "has_error_detection",
        "process_correct",
        "reasoning_correct",
        "plan_code_aligned",
        "first_faulty_layer",
        "first_faulty_step",
        "error_type",
        "joint_fault_label",
        "full_seven_field_reference_record",
    ]
    impact_status: Literal[
        "guaranteed_unchanged",
        "eligible_to_change_by_at_most_one_item",
    ]
    maximum_changed_reference_items: int = Field(ge=0, le=1)
    fixed_all_item_denominator: Literal[20] = 20
    maximum_absolute_change_percentage_points_if_fixed_denominator_20: float = Field(ge=0.0, le=5.0)
    conditional_both_error_denominator: Literal[6] | None = None
    maximum_absolute_change_percentage_points_if_fixed_denominator_6: float | None = Field(
        default=None, ge=0.0, le=16.666666666667
    )
    exact_method_score_delta_status: Literal[
        "exact_zero",
        "not_computed_without_versioned_downstream_target_and_predictions",
    ]
    rationale: str = Field(min_length=1, max_length=600)

    @model_validator(mode="after")
    def validate_envelope(self) -> Self:
        if self.impact_status == "guaranteed_unchanged":
            if (
                self.maximum_changed_reference_items != 0
                or self.maximum_absolute_change_percentage_points_if_fixed_denominator_20 != 0.0
                or self.conditional_both_error_denominator is not None
                or self.maximum_absolute_change_percentage_points_if_fixed_denominator_6 is not None
                or self.exact_method_score_delta_status != "exact_zero"
            ):
                raise ValueError("unchanged impact envelope carries a non-zero bound")
        else:
            if (
                self.maximum_changed_reference_items != 1
                or not math.isclose(
                    self.maximum_absolute_change_percentage_points_if_fixed_denominator_20,
                    5.0,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
                or self.exact_method_score_delta_status
                != "not_computed_without_versioned_downstream_target_and_predictions"
            ):
                raise ValueError("affected impact envelope differs from the one-item bound")
            conditional = self.conditional_both_error_denominator is not None
            if conditional != (
                self.maximum_absolute_change_percentage_points_if_fixed_denominator_6 is not None
            ):
                raise ValueError("conditional impact bound is incomplete")
        return self


class P1SensitivityFallacyCheck(Phase4Contract):
    fallacy: Literal[
        "simpsons_paradox",
        "ecological_fallacy",
        "berksons_paradox",
        "collider_bias",
        "base_rate_neglect",
        "regression_to_the_mean",
        "survivorship_bias",
        "look_elsewhere_effect",
        "garden_of_forking_paths",
        "correlation_not_causation",
        "reverse_causality",
    ]
    severity: Literal["NOTE", "CAUTION"]
    finding: str = Field(min_length=1, max_length=500)


class P1PostAdjudicationSensitivityAnalysis(Phase4Contract):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase4_p1_post_adjudication_sensitivity"] = (
        "tracejudge_phase4_p1_post_adjudication_sensitivity"
    )
    analysis_id: Literal[P1_POST_ADJUDICATION_SENSITIVITY_ID] = P1_POST_ADJUDICATION_SENSITIVITY_ID
    analysis_scope: Literal["post_hoc_aggregate_only_impact_envelope"] = (
        "post_hoc_aggregate_only_impact_envelope"
    )
    verification_status: Literal["ANALYZED"] = "ANALYZED"
    overall_confidence: Literal["CAUTION"] = "CAUTION"
    source_agreement_manifest_sha256: Sha256
    source_agreement_analysis_sha256: Sha256
    source_completed_adjudication_manifest_sha256: Sha256
    source_completed_adjudication_decision_sha256: Sha256
    source_pending_adjudication_manifest_sha256: Sha256
    source_adjudication_mode: Literal["documented_consensus"] = "documented_consensus"
    item_count: Literal[20] = 20
    both_error_item_count: Literal[6] = 6
    original_full_record_disagreement_count: Literal[1] = 1
    resolved_disagreement_count: Literal[1] = 1
    unresolved_disagreement_count: Literal[0] = 0
    raw_full_record_exact_agreement: P1AgreementProportion
    raw_binary_fields: tuple[P1RawBinaryAgreementSnapshot, ...]
    raw_localization_fields: tuple[P1RawLocalizationAgreementSnapshot, ...]
    impact_envelopes: tuple[P1DownstreamImpactEnvelope, ...]
    raw_inter_rater_metrics_preserved: Literal[True] = True
    post_adjudication_inter_rater_agreement_created: Literal[False] = False
    consolidated_reference_label_set_created: Literal[False] = False
    actual_method_level_deltas_computed: Literal[False] = False
    method_level_delta_limitation: Literal[
        "method_predictions_and_per_item_original_labels_not_accessed"
    ] = "method_predictions_and_per_item_original_labels_not_accessed"
    original_primary_labels_unchanged: Literal[True] = True
    original_secondary_labels_unchanged: Literal[True] = True
    raw_agreement_result_unchanged: Literal[True] = True
    main_57x5_results_unchanged: Literal[True] = True
    contains_item_identity: Literal[False] = False
    contains_adjudication_values: Literal[False] = False
    contains_adjudication_rationale: Literal[False] = False
    contains_per_item_original_labels: Literal[False] = False
    contains_method_predictions: Literal[False] = False
    raw_participant_label_data_accessed: Literal[False] = False
    provider_call_count: Literal[0] = 0
    docker_call_count: Literal[0] = 0
    network_call_count: Literal[0] = 0
    fallacy_scan_coverage: Literal[11] = 11
    fallacy_scan: tuple[P1SensitivityFallacyCheck, ...]
    conclusion_boundary: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_fixed_shape(self) -> Self:
        if tuple(item.field_name for item in self.raw_binary_fields) != _BINARY_FIELD_ORDER:
            raise ValueError("raw binary fields differ from the frozen order")
        if tuple(item.field_name for item in self.raw_localization_fields) != (
            _LOCALIZATION_FIELD_ORDER
        ):
            raise ValueError("raw localization fields differ from the frozen order")
        if tuple(item.metric_name for item in self.impact_envelopes) != _IMPACT_ORDER:
            raise ValueError("impact envelopes differ from the fixed order")
        if tuple(item.fallacy for item in self.fallacy_scan) != _FALLACY_ORDER:
            raise ValueError("fallacy scan differs from the fixed 11-item order")
        if (
            self.raw_full_record_exact_agreement.denominator != self.item_count
            or self.raw_full_record_exact_agreement.agreeing_count
            != self.item_count - self.original_full_record_disagreement_count
        ):
            raise ValueError("raw full-record agreement differs from the resolution count")
        return self


@dataclass(frozen=True, slots=True)
class P1PostAdjudicationSensitivityResult:
    analysis: P1PostAdjudicationSensitivityAnalysis
    json_path: Path
    markdown_path: Path
    json_sha256: str
    markdown_sha256: str


@dataclass(frozen=True, slots=True)
class P1PostAdjudicationSensitivityVerification:
    analysis_id: str
    json_sha256: str
    markdown_sha256: str
    verified: bool


def _payload_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(value: Any) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _load_completed_adjudication(
    *,
    manifest_path: str | Path,
    expected_manifest_sha256: str | None,
) -> tuple[P1CompletedAdjudicationBundleManifest, P1CompletedAdjudicationRecord, str]:
    verification = verify_p1_completed_adjudication(
        manifest_path=manifest_path,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    path = Path(manifest_path).expanduser().resolve()
    manifest_payload = _read_private(path, label="completed adjudication manifest")
    try:
        manifest = P1CompletedAdjudicationBundleManifest.model_validate(
            _decode_json(manifest_payload, label="completed adjudication manifest")
        )
        decision_payload = _read_private(
            path.parent / manifest.decision_path,
            label="completed adjudication decision",
        )
        record = P1CompletedAdjudicationRecord.model_validate(
            _decode_json(decision_payload, label="completed adjudication decision")
        )
    except ValidationError:
        raise P1PostAdjudicationSensitivityError(
            "completed adjudication failed sensitivity source validation",
            safe_stage="P4D_P1_POST_ADJUDICATION_SOURCE",
        ) from None
    if verification.decision_sha256 != _sha256(decision_payload) or (
        expected_manifest_sha256 is not None
        and record.source_pending_manifest_sha256 != P1_ADJUDICATION_SOURCE_PENDING_MANIFEST_SHA256
    ):
        raise P1PostAdjudicationSensitivityError(
            "completed adjudication source bindings differ from the frozen decision",
            safe_stage="P4D_P1_POST_ADJUDICATION_SOURCE",
        )
    return manifest, record, verification.manifest_sha256


def _load_aggregate_sources(
    *,
    agreement_manifest_path: str | Path,
    expected_agreement_manifest_sha256: str | None,
    completed_adjudication_manifest_path: str | Path,
    expected_completed_adjudication_manifest_sha256: str | None,
) -> tuple[
    P1InterRaterAgreementAnalysis,
    P1CompletedAdjudicationBundleManifest,
    P1CompletedAdjudicationRecord,
    str,
    str,
]:
    from .p1_adjudication import _load_agreement_bundle

    agreement_manifest, agreement, agreement_manifest_sha256 = _load_agreement_bundle(
        agreement_manifest_path=agreement_manifest_path,
        expected_manifest_sha256=expected_agreement_manifest_sha256,
    )
    completed_manifest, decision, completed_manifest_sha256 = _load_completed_adjudication(
        manifest_path=completed_adjudication_manifest_path,
        expected_manifest_sha256=expected_completed_adjudication_manifest_sha256,
    )
    if (
        decision.source_agreement_manifest_sha256 != agreement_manifest_sha256
        or decision.source_agreement_analysis_sha256 != agreement_manifest.analysis_sha256
        or decision.adjudication_mode != "documented_consensus"
        or not decision.both_original_raters_confirmed
        or not decision.adjudicators_blinded_to_method_predictions
    ):
        raise P1PostAdjudicationSensitivityError(
            "agreement and completed adjudication sources are not consistently bound",
            safe_stage="P4D_P1_POST_ADJUDICATION_SOURCE",
        )
    return (
        agreement,
        completed_manifest,
        decision,
        agreement_manifest_sha256,
        completed_manifest_sha256,
    )


def _impact_envelopes() -> tuple[P1DownstreamImpactEnvelope, ...]:
    zero_reasons = {
        "has_error_detection": (
            "双方原始 has_error 无分歧，裁决不触及该字段，因此固定 P1 子集上的检测标签不变。"
        ),
        "process_correct": (
            "process_correct 是 has_error 的 Schema 强制补集，裁决未改变 has_error。"
        ),
        "reasoning_correct": "双方原始 reasoning_correct 无分歧，裁决不触及该字段。",
    }
    affected_reasons = {
        "plan_code_aligned": (
            "该字段属于唯一分歧；若构建另行版本化的共识参考集，固定 20 条分母中最多改变 1 条。"
        ),
        "first_faulty_layer": (
            "该定位字段属于唯一分歧；全 20 条最多改变 1 条，双方判错的 6 条条件分母中也最多改变 1 条。"
        ),
        "first_faulty_step": (
            "该定位字段属于唯一分歧；全 20 条最多改变 1 条，双方判错的 6 条条件分母中也最多改变 1 条。"
        ),
        "error_type": (
            "该定位字段属于唯一分歧；全 20 条最多改变 1 条，双方判错的 6 条条件分母中也最多改变 1 条。"
        ),
        "joint_fault_label": (
            "联合标签由三个均存在分歧的定位字段组成；全 20 条和双方判错的 6 条中最多各影响 1 条。"
        ),
        "full_seven_field_reference_record": (
            "七字段完整记录的唯一分歧已解决，但这表示 1/1 分歧完成裁决，不产生新的标注者一致率。"
        ),
    }
    values: list[P1DownstreamImpactEnvelope] = []
    for metric_name in _IMPACT_ORDER:
        if metric_name in zero_reasons:
            values.append(
                P1DownstreamImpactEnvelope(
                    metric_name=metric_name,
                    impact_status="guaranteed_unchanged",
                    maximum_changed_reference_items=0,
                    maximum_absolute_change_percentage_points_if_fixed_denominator_20=0.0,
                    exact_method_score_delta_status="exact_zero",
                    rationale=zero_reasons[metric_name],
                )
            )
            continue
        conditional = metric_name in {
            "first_faulty_layer",
            "first_faulty_step",
            "error_type",
            "joint_fault_label",
        }
        values.append(
            P1DownstreamImpactEnvelope(
                metric_name=metric_name,
                impact_status="eligible_to_change_by_at_most_one_item",
                maximum_changed_reference_items=1,
                maximum_absolute_change_percentage_points_if_fixed_denominator_20=5.0,
                conditional_both_error_denominator=6 if conditional else None,
                maximum_absolute_change_percentage_points_if_fixed_denominator_6=(
                    16.666666666667 if conditional else None
                ),
                exact_method_score_delta_status=(
                    "not_computed_without_versioned_downstream_target_and_predictions"
                ),
                rationale=affected_reasons[metric_name],
            )
        )
    return tuple(values)


def _fallacy_scan() -> tuple[P1SensitivityFallacyCheck, ...]:
    findings = {
        "simpsons_paradox": (
            "未合并相互冲突的分层趋势；本报告分别保留全 20 条与双方判错 6 条分母。"
        ),
        "ecological_fallacy": "不从聚合变化上界推断任一标注者的个人能力或判断动机。",
        "berksons_paradox": "20 条是预先冻结的确定性子集，不能外推到全部任务。",
        "collider_bias": "未按裁决结果筛选条目或进行条件回归。",
        "base_rate_neglect": "同时保留 6 条双方判错与 14 条双方无错的基率背景。",
        "regression_to_the_mean": "没有按极端得分选择前后测样本。",
        "survivorship_bias": "正式 20/20 条均进入原始一致性分析，没有排除未完成条目。",
        "look_elsewhere_effect": "仅分析预先观测到分歧的固定字段，不搜索额外终点。",
        "garden_of_forking_paths": (
            "裁决后分析明确标为 post-hoc，原始 19/20 与 20/20 指标保持主读数。"
        ),
        "correlation_not_causation": "只给出描述性影响上界，不声称裁决导致模型性能变化。",
        "reverse_causality": "未使用方法预测决定裁决，也不建立时间方向因果模型。",
    }
    caution = {
        "berksons_paradox",
        "look_elsewhere_effect",
        "garden_of_forking_paths",
    }
    return tuple(
        P1SensitivityFallacyCheck(
            fallacy=name,
            severity="CAUTION" if name in caution else "NOTE",
            finding=findings[name],
        )
        for name in _FALLACY_ORDER
    )


def analyze_p1_post_adjudication_sensitivity(
    *,
    agreement_manifest_path: str | Path = P1_AGREEMENT_DEFAULT_MANIFEST,
    expected_agreement_manifest_sha256: str | None = (
        P1_POST_ADJUDICATION_SOURCE_AGREEMENT_MANIFEST_SHA256
    ),
    completed_adjudication_manifest_path: str | Path = (P1_ADJUDICATION_COMPLETED_DEFAULT_MANIFEST),
    expected_completed_adjudication_manifest_sha256: str | None = (
        P1_POST_ADJUDICATION_SOURCE_COMPLETED_MANIFEST_SHA256
    ),
) -> P1PostAdjudicationSensitivityAnalysis:
    """Compute a public-safe impact envelope from aggregate-only sources."""

    agreement, completed_manifest, decision, agreement_sha256, completed_sha256 = (
        _load_aggregate_sources(
            agreement_manifest_path=agreement_manifest_path,
            expected_agreement_manifest_sha256=(expected_agreement_manifest_sha256),
            completed_adjudication_manifest_path=completed_adjudication_manifest_path,
            expected_completed_adjudication_manifest_sha256=(
                expected_completed_adjudication_manifest_sha256
            ),
        )
    )
    binary = tuple(
        P1RawBinaryAgreementSnapshot(
            field_name=item.field_name,
            raw_agreement=item.raw_agreement,
            cohen_kappa=item.cohen_kappa,
        )
        for item in agreement.binary_fields
    )
    localization = tuple(
        P1RawLocalizationAgreementSnapshot(
            field_name=item.field_name,
            all_items_including_no_error_null=item.all_items_including_no_error_null,
            both_error_items=item.both_error_items,
        )
        for item in agreement.localization_fields
    )
    try:
        analysis = P1PostAdjudicationSensitivityAnalysis(
            source_agreement_manifest_sha256=agreement_sha256,
            source_agreement_analysis_sha256=decision.source_agreement_analysis_sha256,
            source_completed_adjudication_manifest_sha256=completed_sha256,
            source_completed_adjudication_decision_sha256=(completed_manifest.decision_sha256),
            source_pending_adjudication_manifest_sha256=(decision.source_pending_manifest_sha256),
            raw_full_record_exact_agreement=agreement.full_record_exact_agreement,
            raw_binary_fields=binary,
            raw_localization_fields=localization,
            impact_envelopes=_impact_envelopes(),
            fallacy_scan=_fallacy_scan(),
            conclusion_boundary=(
                "原始两标注者完整七字段一致率保持 19/20，has_error 一致率保持 20/20；"
                "唯一过程细节分歧随后以记录在案的人类共识解决。该解决状态是 1/1 分歧已裁决，"
                "不是新的 20/20 标注者一致率。由于本分析未读取逐条原标签或方法预测，只有"
                "has_error、process_correct、reasoning_correct 的零影响可以确定；计划—代码对齐"
                "与定位类下游分数仅报告固定分母下最多一个条目的影响上界。任何精确方法分数"
                "变化都必须在另行版本化、预先声明口径的下游分析中计算，不得覆盖 57×5 主结果。"
            ),
        )
    except ValidationError:
        raise P1PostAdjudicationSensitivityError(
            "post-adjudication sensitivity analysis failed schema validation",
            safe_stage="P4D_P1_POST_ADJUDICATION_ANALYSIS",
        ) from None
    canaries = (
        decision.annotation_item_id,
        decision.decision_rationale,
        decision.case_material_sha256,
        decision.code_sha256,
        decision.structured_explanation_sha256,
        decision.functional_evidence_sha256,
        decision.decision.first_faulty_step,
        decision.decision.error_type.value,
    )
    assert_public_payload_safe(analysis, canaries=canaries)
    return analysis


def _ratio(value: P1AgreementProportion) -> str:
    if value.estimate is None:
        return "0/0（N/A）"
    return f"{value.agreeing_count}/{value.denominator}（{value.estimate * 100:.1f}%）"


def render_p1_post_adjudication_sensitivity_markdown(
    analysis: P1PostAdjudicationSensitivityAnalysis,
) -> bytes:
    binary_rows = "\n".join(
        f"| `{item.field_name}` | {_ratio(item.raw_agreement)} | "
        f"{'N/A' if item.cohen_kappa is None else f'{item.cohen_kappa:.3f}'} |"
        for item in analysis.raw_binary_fields
    )
    location_rows = "\n".join(
        f"| `{item.field_name}` | {_ratio(item.all_items_including_no_error_null)} | "
        f"{_ratio(item.both_error_items)} |"
        for item in analysis.raw_localization_fields
    )
    impact_rows = []
    for item in analysis.impact_envelopes:
        status = "确定不变" if item.impact_status == "guaranteed_unchanged" else "最多影响 1 条"
        conditional = (
            "—"
            if item.maximum_absolute_change_percentage_points_if_fixed_denominator_6 is None
            else f"≤ {item.maximum_absolute_change_percentage_points_if_fixed_denominator_6:.1f} pp"
        )
        impact_rows.append(
            f"| `{item.metric_name}` | {status} | "
            f"≤ {item.maximum_absolute_change_percentage_points_if_fixed_denominator_20:.1f} pp | "
            f"{conditional} | {item.rationale} |"
        )
    fallacy_rows = "\n".join(
        f"| `{item.fallacy}` | {item.severity} | {item.finding} |" for item in analysis.fallacy_scan
    )
    text = f"""# P1 裁决后敏感性分析 v1

## Material Passport

- Origin Skill: experiment-agent
- Origin Mode: validate
- Origin Date: {P1_POST_ADJUDICATION_SENSITIVITY_ORIGIN_DATE}
- Verification Status: {analysis.verification_status}
- Version Label: {analysis.analysis_id}
- Source Agreement Manifest SHA256: `{analysis.source_agreement_manifest_sha256}`
- Source Completed Adjudication Manifest SHA256: `{analysis.source_completed_adjudication_manifest_sha256}`

## 结论先行

- 原始完整七字段一致率保持 **19/20（95.0%）**；原始 `has_error` 一致率保持 **20/20（100.0%）**。
- 唯一过程细节分歧已通过两位原始标注者的 `documented_consensus` 完成裁决：**1/1 已解决，0 条未解决**。
- 这不能写成“裁决后标注者一致率为 20/20”。裁决解决分歧，不会改写已经观察到的原始两标注者一致性。
- `has_error`、`process_correct`、`reasoning_correct` 的固定 P1 标签确定不变；计划—代码对齐和定位类指标在固定 20 条分母下最多受 1 条影响。
- 本分析没有读取逐条原标签或方法预测，因此不伪造具体方法分数的变化方向。

## 原始一致性快照（保持不变）

完整七字段记录：{_ratio(analysis.raw_full_record_exact_agreement)}。

| 二元字段 | 原始一致率 | Cohen's κ |
|---|---:|---:|
{binary_rows}

| 定位字段 | 全 20 条（含双方无错 null） | 双方判错的 6 条 |
|---|---:|---:|
{location_rows}

## 裁决后的下游影响包络

以下百分点评估只适用于固定完整分母：1/20 = 5.0 pp，1/6 = 16.7 pp。它们是最大绝对变化上界，不是实际方法性能变化。

| 指标 | 标签影响 | 固定 20 条上界 | 固定 6 条条件分母上界 | 依据 |
|---|---|---:|---:|---|
{chr(10).join(impact_rows)}

## 为什么不报告精确方法分数变化

本产物没有打开五种方法的逐条预测，也没有打开两位标注者的逐条原标签。完成态裁决记录只提供最终四字段决定，不提供两份原始逐条值。因此可以严格确认零影响字段、分歧解决状态和一条样本的变化上界，但不能在没有明确下游目标、分母和版本号的情况下声称某方法准确率具体上升或下降。

如以后需要精确裁决后方法分数，应新建独立分析 ID，明确“以共识参考集替代哪一版标签、哪些方法结果、采用何种无效判断分母”，并与当前 57×5 主分析并列报告，不得覆盖。

## 统计谬误扫描

- Coverage：{analysis.fallacy_scan_coverage}/11
- Overall Confidence：{analysis.overall_confidence}

| 谬误 | 严重度 | 检查结果 |
|---|---|---|
{fallacy_rows}

## 隐私与复现

- 输入仅为 aggregate-only 一致性包和独立完成态裁决包；未读取两份逐条原标签。
- 输出不含盲化条目 ID、最终裁决值、裁决理由、案例哈希或方法预测。
- 新增 Provider、Docker、网络调用均为 0。
- JSON 与 Markdown 均可从固定源哈希确定性重建。

## 结论边界

{analysis.conclusion_boundary}
"""
    return text.encode("utf-8")


def _privacy_canaries(decision: P1CompletedAdjudicationRecord) -> tuple[str, ...]:
    return (
        decision.annotation_item_id,
        decision.decision_rationale,
        decision.case_material_sha256,
        decision.code_sha256,
        decision.structured_explanation_sha256,
        decision.functional_evidence_sha256,
        decision.decision.first_faulty_step,
        decision.decision.error_type.value,
    )


def _atomic_publish(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise P1PostAdjudicationSensitivityError(
            "sensitivity output path is a symlink",
            safe_stage="P4D_P1_POST_ADJUDICATION_OUTPUT",
        )
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise P1PostAdjudicationSensitivityError(
                "sensitivity output already exists with different content",
                safe_stage="P4D_P1_POST_ADJUDICATION_OUTPUT",
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o644)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def publish_p1_post_adjudication_sensitivity(
    *,
    output_dir: str | Path = P1_POST_ADJUDICATION_SENSITIVITY_DEFAULT_OUTPUT,
    **kwargs: Any,
) -> P1PostAdjudicationSensitivityResult:
    analysis = analyze_p1_post_adjudication_sensitivity(**kwargs)
    output_root = Path(output_dir).expanduser().resolve()
    if output_root.is_symlink() or (output_root.exists() and not output_root.is_dir()):
        raise P1PostAdjudicationSensitivityError(
            "sensitivity output root is unsafe",
            safe_stage="P4D_P1_POST_ADJUDICATION_OUTPUT",
        )
    json_path = output_root / P1_POST_ADJUDICATION_SENSITIVITY_JSON
    markdown_path = output_root / P1_POST_ADJUDICATION_SENSITIVITY_REPORT
    json_payload = _json_bytes(analysis)
    markdown_payload = render_p1_post_adjudication_sensitivity_markdown(analysis)

    _, _, decision, _, _ = _load_aggregate_sources(
        agreement_manifest_path=kwargs.get(
            "agreement_manifest_path", P1_AGREEMENT_DEFAULT_MANIFEST
        ),
        expected_agreement_manifest_sha256=kwargs.get(
            "expected_agreement_manifest_sha256",
            P1_POST_ADJUDICATION_SOURCE_AGREEMENT_MANIFEST_SHA256,
        ),
        completed_adjudication_manifest_path=kwargs.get(
            "completed_adjudication_manifest_path",
            P1_ADJUDICATION_COMPLETED_DEFAULT_MANIFEST,
        ),
        expected_completed_adjudication_manifest_sha256=kwargs.get(
            "expected_completed_adjudication_manifest_sha256",
            P1_POST_ADJUDICATION_SOURCE_COMPLETED_MANIFEST_SHA256,
        ),
    )
    canaries = _privacy_canaries(decision)
    assert_public_payload_safe(json.loads(json_payload), canaries=canaries)
    assert_public_payload_safe(markdown_payload.decode("utf-8"), canaries=canaries)
    _atomic_publish(json_path, json_payload)
    _atomic_publish(markdown_path, markdown_payload)
    return P1PostAdjudicationSensitivityResult(
        analysis=analysis,
        json_path=json_path,
        markdown_path=markdown_path,
        json_sha256=_payload_sha256(json_payload),
        markdown_sha256=_payload_sha256(markdown_payload),
    )


def verify_p1_post_adjudication_sensitivity(
    *,
    output_dir: str | Path = P1_POST_ADJUDICATION_SENSITIVITY_DEFAULT_OUTPUT,
    expected_json_sha256: str | None = None,
    expected_markdown_sha256: str | None = None,
    **kwargs: Any,
) -> P1PostAdjudicationSensitivityVerification:
    analysis = analyze_p1_post_adjudication_sensitivity(**kwargs)
    output_root = Path(output_dir).expanduser().resolve()
    json_path = output_root / P1_POST_ADJUDICATION_SENSITIVITY_JSON
    markdown_path = output_root / P1_POST_ADJUDICATION_SENSITIVITY_REPORT
    if (
        json_path.is_symlink()
        or markdown_path.is_symlink()
        or not json_path.is_file()
        or not markdown_path.is_file()
    ):
        raise P1PostAdjudicationSensitivityError(
            "sensitivity release files are missing or unsafe",
            safe_stage="P4D_P1_POST_ADJUDICATION_VERIFY",
        )
    json_payload = json_path.read_bytes()
    markdown_payload = markdown_path.read_bytes()
    expected_json = _json_bytes(analysis)
    expected_markdown = render_p1_post_adjudication_sensitivity_markdown(analysis)
    if json_payload != expected_json or markdown_payload != expected_markdown:
        raise P1PostAdjudicationSensitivityError(
            "sensitivity release differs from deterministic regeneration",
            safe_stage="P4D_P1_POST_ADJUDICATION_VERIFY",
        )
    json_sha256 = _payload_sha256(json_payload)
    markdown_sha256 = _payload_sha256(markdown_payload)
    if (
        expected_json_sha256 is not None
        and json_sha256 != expected_json_sha256
        or expected_markdown_sha256 is not None
        and markdown_sha256 != expected_markdown_sha256
    ):
        raise P1PostAdjudicationSensitivityError(
            "sensitivity release differs from the expected identity",
            safe_stage="P4D_P1_POST_ADJUDICATION_VERIFY",
        )
    return P1PostAdjudicationSensitivityVerification(
        analysis_id=analysis.analysis_id,
        json_sha256=json_sha256,
        markdown_sha256=markdown_sha256,
        verified=True,
    )


__all__ = [
    "P1_POST_ADJUDICATION_SENSITIVITY_DEFAULT_OUTPUT",
    "P1_POST_ADJUDICATION_SENSITIVITY_ID",
    "P1_POST_ADJUDICATION_SENSITIVITY_JSON",
    "P1_POST_ADJUDICATION_SENSITIVITY_REPORT",
    "P1DownstreamImpactEnvelope",
    "P1PostAdjudicationSensitivityAnalysis",
    "P1PostAdjudicationSensitivityError",
    "P1PostAdjudicationSensitivityResult",
    "P1PostAdjudicationSensitivityVerification",
    "P1RawBinaryAgreementSnapshot",
    "P1RawLocalizationAgreementSnapshot",
    "P1SensitivityFallacyCheck",
    "analyze_p1_post_adjudication_sensitivity",
    "publish_p1_post_adjudication_sensitivity",
    "render_p1_post_adjudication_sensitivity_markdown",
    "verify_p1_post_adjudication_sensitivity",
]
