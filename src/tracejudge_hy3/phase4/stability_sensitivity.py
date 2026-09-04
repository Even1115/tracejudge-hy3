"""Publish a post-hoc identifier-normalization sensitivity analysis.

The source stability run remains immutable.  This module validates its hashes,
recomputes the preregistered exact-agreement estimand, and then applies one
explicit alias allowlist to show how identifier spelling affects localization
agreement.  It never calls a Provider and never publishes trial-level text.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from tracejudge_hy3.phase3.contracts import MethodOutcome, MethodOutcomeStatus
from tracejudge_hy3.phase3.privacy import assert_public_payload_safe

from .contracts import Phase4Contract
from .stability import (
    STABILITY_CASE_COUNT,
    STABILITY_EVALUATION_COUNT,
    STABILITY_REPETITION_COUNT,
    StabilityProtocol,
    StabilityReport,
    StabilityRunManifest,
    StabilityTrialRecord,
)

SENSITIVITY_ANALYSIS_ID = "phase4_judge_stability_identifier_sensitivity_v1"
SENSITIVITY_NORMALIZATION_POLICY_ID = "first_faulty_step_exact_alias_allowlist_v1"
SENSITIVITY_REPORT_FILENAME = "phase4_judge_stability_sensitivity_v1.md"
SENSITIVITY_JSON_FILENAME = "phase4_judge_stability_sensitivity_v1.json"
SENSITIVITY_CARD_RELATIVE_PATH = "charts/contest_showcase_v1/05_judge_stability_card.svg"

_NULL_VALUE = "<none>"
_FIELD_NAMES = ("has_error", "first_faulty_step", "error_type", "joint_label")
_STEP_ALIASES: Mapping[str, str] = {
    "solution_trace.requirement_understanding": "requirement_understanding",
}


class Phase4StabilitySensitivityError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        safe_stage: str = "P4_STABILITY_SENSITIVITY",
    ) -> None:
        super().__init__(message)
        self.safe_stage = safe_stage


class StepNormalizationRule(Phase4Contract):
    source_value: str = Field(min_length=1, max_length=200)
    canonical_value: str = Field(min_length=1, max_length=200)
    match_policy: Literal["exact"] = "exact"


class AgreementMeasure(Phase4Contract):
    agreeing_pair_count: int = Field(ge=0)
    comparable_pair_count: int = Field(ge=1)
    pairwise_agreement: float = Field(ge=0.0, le=1.0)
    distribution: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_measure(self) -> Self:
        if self.agreeing_pair_count > self.comparable_pair_count:
            raise ValueError("agreeing pairs exceed comparable pairs")
        if self.distribution:
            if any(count < 1 for count in self.distribution.values()):
                raise ValueError("agreement distributions require positive counts")
            value_count = sum(self.distribution.values())
            expected_comparable = math.comb(value_count, 2)
            expected_agreeing = sum(math.comb(count, 2) for count in self.distribution.values())
            if (
                self.comparable_pair_count != expected_comparable
                or self.agreeing_pair_count != expected_agreeing
            ):
                raise ValueError("agreement pair counts differ from the distribution")
        expected_rate = self.agreeing_pair_count / self.comparable_pair_count
        if not math.isclose(
            self.pairwise_agreement,
            expected_rate,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("agreement rate differs from pair counts")
        return self


class FieldSensitivity(Phase4Contract):
    field_name: Literal["has_error", "first_faulty_step", "error_type", "joint_label"]
    raw: AgreementMeasure
    normalized: AgreementMeasure
    absolute_change_percentage_points: float

    @model_validator(mode="after")
    def validate_change(self) -> Self:
        expected = (self.normalized.pairwise_agreement - self.raw.pairwise_agreement) * 100
        if not math.isclose(
            self.absolute_change_percentage_points,
            expected,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ValueError("sensitivity change differs from agreement rates")
        return self


class CaseSensitivity(Phase4Contract):
    case_id: str
    valid_judgment_count: Literal[5] = STABILITY_REPETITION_COUNT
    fields: tuple[FieldSensitivity, ...]


class StabilitySensitivityAnalysis(Phase4Contract):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase4_stability_identifier_sensitivity"] = (
        "tracejudge_phase4_stability_identifier_sensitivity"
    )
    analysis_id: Literal["phase4_judge_stability_identifier_sensitivity_v1"] = (
        SENSITIVITY_ANALYSIS_ID
    )
    analysis_scope: Literal["post_hoc_identifier_normalization_sensitivity"] = (
        "post_hoc_identifier_normalization_sensitivity"
    )
    verification_status: Literal["ANALYZED"] = "ANALYZED"
    overall_confidence: Literal["CAUTION"] = "CAUTION"
    source_run_id: str
    source_git_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_provider: str
    source_model: str
    source_protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_results_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_report_json_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_report_markdown_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalization_policy_id: Literal["first_faulty_step_exact_alias_allowlist_v1"] = (
        SENSITIVITY_NORMALIZATION_POLICY_ID
    )
    normalization_rules: tuple[StepNormalizationRule, ...]
    scheduled_evaluation_count: Literal[20] = STABILITY_EVALUATION_COUNT
    valid_judgment_count: Literal[20] = STABILITY_EVALUATION_COUNT
    provider_call_count_for_sensitivity_analysis: Literal[0] = 0
    raw_primary_result_preserved: Literal[True] = True
    normalized_result_is_post_hoc: Literal[True] = True
    source_raw_report_agreement_verified: Literal[True] = True
    fallacy_scan_coverage: Literal[11] = 11
    overall_fields: tuple[FieldSensitivity, ...]
    cases: tuple[CaseSensitivity, ...]
    conclusion_boundary: str = Field(min_length=1)
    contains_trial_level_text: Literal[False] = False
    contains_provider_raw: Literal[False] = False
    main_experiment_merge_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_fixed_shape(self) -> Self:
        if tuple(item.field_name for item in self.overall_fields) != _FIELD_NAMES:
            raise ValueError("overall sensitivity fields differ from the fixed order")
        if len(self.cases) != STABILITY_CASE_COUNT:
            raise ValueError("sensitivity report requires four source cases")
        if tuple(self.normalization_rules) != tuple(
            StepNormalizationRule(source_value=source, canonical_value=canonical)
            for source, canonical in _STEP_ALIASES.items()
        ):
            raise ValueError("normalization rules differ from the exact alias allowlist")
        return self


@dataclass(frozen=True, slots=True)
class StabilitySensitivityReleaseResult:
    analysis: StabilitySensitivityAnalysis
    json_path: Path
    markdown_path: Path
    card_path: Path
    json_sha256: str
    markdown_sha256: str
    card_sha256: str


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _payload_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(value: Any) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _read_model(path: Path, model: type[Phase4Contract], *, label: str) -> Any:
    if path.is_symlink() or not path.is_file():
        raise Phase4StabilitySensitivityError(
            f"{label} is missing or unsafe",
            safe_stage="P4_STABILITY_SENSITIVITY_SOURCE",
        )
    try:
        return model.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise Phase4StabilitySensitivityError(
            f"{label} failed strict validation",
            safe_stage="P4_STABILITY_SENSITIVITY_SOURCE",
        ) from exc


def _load_source_run(
    run_dir: str | Path,
) -> tuple[
    StabilityProtocol,
    StabilityRunManifest,
    StabilityReport,
    tuple[StabilityTrialRecord, ...],
    dict[str, str],
]:
    root = Path(run_dir).expanduser().resolve()
    if root.is_symlink() or not root.is_dir():
        raise Phase4StabilitySensitivityError(
            "source stability run is missing or unsafe",
            safe_stage="P4_STABILITY_SENSITIVITY_SOURCE",
        )
    paths = {
        "protocol": root / "protocol.json",
        "manifest": root / "manifest.json",
        "results": root / "results.jsonl",
        "report_json": root / "report.json",
        "report_markdown": root / "REPORT.md",
    }
    protocol = _read_model(paths["protocol"], StabilityProtocol, label="source protocol")
    manifest = _read_model(paths["manifest"], StabilityRunManifest, label="source manifest")
    report = _read_model(paths["report_json"], StabilityReport, label="source report")

    if paths["results"].is_symlink() or not paths["results"].is_file():
        raise Phase4StabilitySensitivityError(
            "source results are missing or unsafe",
            safe_stage="P4_STABILITY_SENSITIVITY_SOURCE",
        )
    try:
        trials = tuple(
            StabilityTrialRecord.model_validate_json(line)
            for line in paths["results"].read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    except (OSError, ValueError) as exc:
        raise Phase4StabilitySensitivityError(
            "source results failed strict validation",
            safe_stage="P4_STABILITY_SENSITIVITY_SOURCE",
        ) from exc

    hashes = {name: _file_sha256(path) for name, path in paths.items()}
    if (
        manifest.status != "completed"
        or manifest.completed_evaluation_count != STABILITY_EVALUATION_COUNT
        or manifest.results_sha256 != hashes["results"]
        or manifest.report_json_sha256 != hashes["report_json"]
        or manifest.report_markdown_sha256 != hashes["report_markdown"]
        or manifest.protocol_sha256 != hashes["protocol"]
        or report.protocol_sha256 != hashes["protocol"]
        or report.run_id != manifest.run_id
        or len(trials) != STABILITY_EVALUATION_COUNT
        or not report.all_twenty_valid
        or report.valid_judgment_count != STABILITY_EVALUATION_COUNT
    ):
        raise Phase4StabilitySensitivityError(
            "source run identity or terminal accounting differs",
            safe_stage="P4_STABILITY_SENSITIVITY_IDENTITY",
        )
    expected_trial_ids = [f"stability_trial_{index:03d}" for index in range(1, 21)]
    if [trial.trial_id for trial in trials] != expected_trial_ids:
        raise Phase4StabilitySensitivityError(
            "source trial order or identity differs",
            safe_stage="P4_STABILITY_SENSITIVITY_IDENTITY",
        )
    if any(
        trial.run_id != manifest.run_id
        or trial.outcome.status != MethodOutcomeStatus.VALID_JUDGMENT
        or trial.outcome.judgment is None
        for trial in trials
    ):
        raise Phase4StabilitySensitivityError(
            "source trials are not twenty valid judgments",
            safe_stage="P4_STABILITY_SENSITIVITY_IDENTITY",
        )
    return protocol, manifest, report, trials, hashes


def _canonical_step(value: str | None) -> str:
    if value is None:
        return _NULL_VALUE
    return _STEP_ALIASES.get(value, value)


def _field_value(outcome: MethodOutcome, field_name: str, *, normalized: bool) -> str:
    judgment = outcome.judgment
    assert judgment is not None
    if field_name == "has_error":
        return "true" if judgment.has_error else "false"
    if field_name == "first_faulty_step":
        value = (
            _canonical_step(judgment.first_faulty_step)
            if normalized
            else (judgment.first_faulty_step or _NULL_VALUE)
        )
        return value
    if field_name == "error_type":
        return judgment.error_type.value if judgment.error_type is not None else _NULL_VALUE
    if field_name == "joint_label":
        step = (
            _canonical_step(judgment.first_faulty_step)
            if normalized
            else (judgment.first_faulty_step)
        )
        return json.dumps(
            [
                judgment.has_error,
                None if step == _NULL_VALUE else step,
                judgment.error_type.value if judgment.error_type is not None else None,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    raise AssertionError("unknown sensitivity field")


def _measure(values: Sequence[str]) -> AgreementMeasure:
    distribution = dict(sorted(Counter(values).items()))
    comparable = math.comb(len(values), 2)
    agreeing = sum(math.comb(count, 2) for count in distribution.values())
    return AgreementMeasure(
        agreeing_pair_count=agreeing,
        comparable_pair_count=comparable,
        pairwise_agreement=agreeing / comparable,
        distribution=distribution,
    )


def _field_sensitivity(outcomes: Sequence[MethodOutcome], field_name: str) -> FieldSensitivity:
    raw = _measure([_field_value(outcome, field_name, normalized=False) for outcome in outcomes])
    normalized = _measure(
        [_field_value(outcome, field_name, normalized=True) for outcome in outcomes]
    )
    return FieldSensitivity(
        field_name=field_name,
        raw=raw,
        normalized=normalized,
        absolute_change_percentage_points=(
            round((normalized.pairwise_agreement - raw.pairwise_agreement) * 100, 10)
        ),
    )


def _assert_raw_report_matches(
    report: StabilityReport,
    cases: Sequence[CaseSensitivity],
    overall: Sequence[FieldSensitivity],
) -> None:
    source_case_map = {case.case_id: case for case in report.cases}
    for case in cases:
        source_fields = {field.field_name: field for field in source_case_map[case.case_id].fields}
        for field in case.fields:
            source = source_fields[field.field_name]
            if (
                source.agreeing_pair_count != field.raw.agreeing_pair_count
                or source.comparable_pair_count != field.raw.comparable_pair_count
                or source.distribution != field.raw.distribution
                or source.pairwise_agreement != field.raw.pairwise_agreement
            ):
                raise Phase4StabilitySensitivityError(
                    "recomputed raw case agreement differs from the frozen report",
                    safe_stage="P4_STABILITY_SENSITIVITY_RECOMPUTE",
                )
    source_overall = {field.field_name: field for field in report.overall_fields}
    for field in overall:
        source = source_overall[field.field_name]
        if (
            source.agreeing_pair_count != field.raw.agreeing_pair_count
            or source.comparable_pair_count != field.raw.comparable_pair_count
            or source.pairwise_agreement != field.raw.pairwise_agreement
        ):
            raise Phase4StabilitySensitivityError(
                "recomputed raw overall agreement differs from the frozen report",
                safe_stage="P4_STABILITY_SENSITIVITY_RECOMPUTE",
            )


def analyze_stability_sensitivity(
    run_dir: str | Path,
    *,
    privacy_canaries: Sequence[str | bytes] = (),
) -> StabilitySensitivityAnalysis:
    """Validate a completed run and compute raw plus post-hoc agreement."""

    protocol, manifest, report, trials, hashes = _load_source_run(run_dir)
    grouped: dict[str, list[MethodOutcome]] = defaultdict(list)
    for trial in trials:
        grouped[trial.case_id].append(trial.outcome)
    case_order = [case.case_id for case in protocol.cases]
    if set(grouped) != set(case_order) or any(
        len(grouped[case_id]) != STABILITY_REPETITION_COUNT for case_id in case_order
    ):
        raise Phase4StabilitySensitivityError(
            "source case coverage differs from the fixed four-by-five design",
            safe_stage="P4_STABILITY_SENSITIVITY_IDENTITY",
        )

    cases = tuple(
        CaseSensitivity(
            case_id=case_id,
            fields=tuple(
                _field_sensitivity(grouped[case_id], field_name) for field_name in _FIELD_NAMES
            ),
        )
        for case_id in case_order
    )
    overall_fields = []
    for field_name in _FIELD_NAMES:
        case_fields = [
            next(field for field in case.fields if field.field_name == field_name) for case in cases
        ]
        raw_agreeing = sum(field.raw.agreeing_pair_count for field in case_fields)
        raw_comparable = sum(field.raw.comparable_pair_count for field in case_fields)
        normalized_agreeing = sum(field.normalized.agreeing_pair_count for field in case_fields)
        normalized_comparable = sum(field.normalized.comparable_pair_count for field in case_fields)
        raw = AgreementMeasure(
            agreeing_pair_count=raw_agreeing,
            comparable_pair_count=raw_comparable,
            pairwise_agreement=raw_agreeing / raw_comparable,
        )
        normalized = AgreementMeasure(
            agreeing_pair_count=normalized_agreeing,
            comparable_pair_count=normalized_comparable,
            pairwise_agreement=normalized_agreeing / normalized_comparable,
        )
        overall_fields.append(
            FieldSensitivity(
                field_name=field_name,
                raw=raw,
                normalized=normalized,
                absolute_change_percentage_points=(
                    round(
                        (normalized.pairwise_agreement - raw.pairwise_agreement) * 100,
                        10,
                    )
                ),
            )
        )
    overall = tuple(overall_fields)
    _assert_raw_report_matches(report, cases, overall)

    analysis = StabilitySensitivityAnalysis(
        source_run_id=manifest.run_id,
        source_git_commit=protocol.source_git.commit,
        source_provider=protocol.provider,
        source_model=protocol.model,
        source_protocol_sha256=hashes["protocol"],
        source_manifest_sha256=hashes["manifest"],
        source_results_sha256=hashes["results"],
        source_report_json_sha256=hashes["report_json"],
        source_report_markdown_sha256=hashes["report_markdown"],
        normalization_rules=tuple(
            StepNormalizationRule(source_value=source, canonical_value=canonical)
            for source, canonical in _STEP_ALIASES.items()
        ),
        overall_fields=overall,
        cases=cases,
        conclusion_boundary=(
            "规范化结果是读取既有 20 次判断后进行的事后敏感性分析，仅说明一个已观察到的"
            "路径前缀别名如何影响字符串精确一致率。预注册的原始 90.0% 首错步骤成对一致率"
            "保持主结果；规范化后的 100.0% 不得替代主结果、并入冻结的 57×5 主实验，或外推"
            "到其他任务、模型、Prompt 与未来服务版本。"
        ),
    )
    assert_public_payload_safe(analysis, canaries=privacy_canaries)
    return analysis


def _rate(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_sensitivity_markdown(analysis: StabilitySensitivityAnalysis) -> bytes:
    fields = {item.field_name: item for item in analysis.overall_fields}
    case_rows = []
    for case in analysis.cases:
        step = next(field for field in case.fields if field.field_name == "first_faulty_step")
        joint = next(field for field in case.fields if field.field_name == "joint_label")
        case_rows.append(
            f"| `{case.case_id}` | {step.raw.agreeing_pair_count}/"
            f"{step.raw.comparable_pair_count}（{_rate(step.raw.pairwise_agreement)}） | "
            f"{step.normalized.agreeing_pair_count}/{step.normalized.comparable_pair_count}"
            f"（{_rate(step.normalized.pairwise_agreement)}） | "
            f"{_rate(joint.raw.pairwise_agreement)} → "
            f"{_rate(joint.normalized.pairwise_agreement)} |"
        )
    overall_rows = []
    labels = {
        "has_error": "`has_error`",
        "first_faulty_step": "`first_faulty_step`",
        "error_type": "`error_type`",
        "joint_label": "三字段联合标签",
    }
    for field in analysis.overall_fields:
        overall_rows.append(
            f"| {labels[field.field_name]} | {field.raw.agreeing_pair_count}/"
            f"{field.raw.comparable_pair_count}（{_rate(field.raw.pairwise_agreement)}） | "
            f"{field.normalized.agreeing_pair_count}/"
            f"{field.normalized.comparable_pair_count}"
            f"（{_rate(field.normalized.pairwise_agreement)}） | "
            f"{field.absolute_change_percentage_points:+.1f} pp |"
        )
    step = fields["first_faulty_step"]
    text = f"""# Full TraceJudge 标识符规范化敏感性报告 v1

## Material Passport

- Origin Skill: experiment-agent
- Origin Mode: validate
- Origin Date: 2026-09-03
- Verification Status: {analysis.verification_status}
- Version Label: {analysis.analysis_id}
- Source Run ID: `{analysis.source_run_id}`
- Source Protocol SHA256: `{analysis.source_protocol_sha256}`
- Source Results SHA256: `{analysis.source_results_sha256}`

## 分析目的

本报告不重新运行 Hy3。它在保持预注册原始字符串精确一致率不变的前提下，检查一个已观察到的字段路径别名是否改变 `first_faulty_step` 与三字段联合标签的一致率。

## 主结果与事后结果必须并列

| 口径 | 首错步骤一致对/可比对 | 成对一致率 | 研究身份 |
|---|---:|---:|---|
| 原始精确字符串匹配 | {step.raw.agreeing_pair_count}/{step.raw.comparable_pair_count} | **{_rate(step.raw.pairwise_agreement)}** | 预注册主结果，保持不变 |
| 精确别名规范化 | {step.normalized.agreeing_pair_count}/{step.normalized.comparable_pair_count} | **{_rate(step.normalized.pairwise_agreement)}** | post-hoc 敏感性分析，不替代主结果 |

## 规范化规则

只使用一条精确白名单映射：

```text
solution_trace.requirement_understanding → requirement_understanding
```

不删除任意前缀、不做模糊匹配，也不合并其他标识符。该映射只影响 `reasoning_swap` 第 4 次判断；两种字符串都指向输入结构中的同一 `solution_trace.requirement_understanding` 字段。

## 总体敏感性

| 字段 | 原始精确一致 | 规范化一致 | 变化 |
|---|---:|---:|---:|
{chr(10).join(overall_rows)}

## 分案例敏感性

| 案例 | 首错步骤：原始 | 首错步骤：规范化 | 联合标签：原始 → 规范化 |
|---|---:|---:|---:|
{chr(10).join(case_rows)}

`normal_correct`、`boundary_error` 和 `equivalent_implementation` 的所有读数均未改变。`reasoning_swap` 的首错步骤和联合标签从 6/10（60.0%）变为 10/10（100.0%）。

## 解释

证据支持的描述是：Full TraceJudge 在这四个公开案例中稳定判断“是否有错”和“错误类型”；原始首错步骤精确一致率为 90.0%。唯一表面差异是同一位置的两种字符串写法，说明输出标识符应由未来版本的 Schema 或后处理层进行规范化。

这不能证明 Judge 在一般任务上的首错定位达到 100%，也不能把事后规范化读数写成预注册结果。

## 验证与完整性

- 20/20 个源判断通过严格 Schema 校验，trial ID 唯一且顺序完整；
- source protocol、results、JSON report 与 Markdown report 的 SHA256 均与 manifest 一致；
- 重新计算的原始四字段一致率与冻结 `report.json` 完全一致；
- 本分析新增 Provider、Docker、网络调用均为 0；
- 未公开 trial 级 evidence summary、Provider raw、隐藏测试或人工标签。

## 统计谬误扫描

- Coverage：11/11；Overall Confidence：{analysis.overall_confidence}。
- Base-rate neglect：四个案例是目的性选择的 2 个有错、2 个无错，不能视为真实错误率。
- Garden of forking paths：规范化规则在看到结果后定义，因此明确标为 post-hoc，并保留原始指标。
- Look-elsewhere effect：只报告预注册三字段与联合标签，没有按结果筛选额外性能终点。
- Simpson、生态谬误、Berkson、collider、均值回归、生存者偏差、相关因果化和反向因果在本描述性重复评审设计中未发现适用证据。

## 结论边界

{analysis.conclusion_boundary}
"""
    return text.encode("utf-8")


def render_stability_card_svg(analysis: StabilitySensitivityAnalysis) -> bytes:
    fields = {item.field_name: item for item in analysis.overall_fields}
    step = fields["first_faulty_step"]
    raw_width = round(576 * step.raw.pairwise_agreement)
    normalized_width = round(576 * step.normalized.pairwise_agreement)
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1120" height="620" viewBox="0 0 1120 620" role="img" aria-labelledby="title desc">
  <title id="title">Full TraceJudge 四案例运行内稳定性结果卡片</title>
  <desc id="desc">四个公开案例各重复五次，共二十次有效判断。错误存在性与错误类型原始精确成对一致率均为百分之百，首错步骤原始精确一致率为百分之九十；事后精确别名规范化后为百分之百，但不替代原始结果。</desc>
  <rect width="1120" height="620" rx="24" fill="#071018"/>
  <circle cx="1030" cy="10" r="250" fill="#0e3141" opacity="0.58"/>
  <circle cx="90" cy="610" r="210" fill="#13233a" opacity="0.45"/>
  <text x="72" y="70" fill="#41d9ff" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="18" font-weight="700" letter-spacing="2">TRACEJUDGE-HY3 · RUN-INTERNAL STABILITY</text>
  <text x="72" y="122" fill="#f5f8fb" font-family="-apple-system, BlinkMacSystemFont, PingFang SC, Microsoft YaHei, sans-serif" font-size="34" font-weight="760">稳定发现错误，定位写法需要规范化</text>
  <text x="72" y="160" fill="#91a3b7" font-family="-apple-system, BlinkMacSystemFont, PingFang SC, Microsoft YaHei, sans-serif" font-size="18">4 个公开案例 × 每例 5 次 · Full TraceJudge · temperature = 0</text>

  <rect x="72" y="202" width="214" height="112" rx="14" fill="#0b1723" stroke="#213244"/>
  <text x="94" y="235" fill="#91a3b7" font-family="-apple-system, BlinkMacSystemFont, PingFang SC, Microsoft YaHei, sans-serif" font-size="15">有效判断</text>
  <text x="94" y="283" fill="#f5f8fb" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="38" font-weight="800">20 / 20</text>

  <rect x="304" y="202" width="214" height="112" rx="14" fill="#0b1723" stroke="#213244"/>
  <text x="326" y="235" fill="#91a3b7" font-family="-apple-system, BlinkMacSystemFont, PingFang SC, Microsoft YaHei, sans-serif" font-size="15">Provider / 解析失败</text>
  <text x="326" y="283" fill="#7ce7b2" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="38" font-weight="800">0 / 0</text>

  <rect x="536" y="202" width="236" height="112" rx="14" fill="#0b1723" stroke="#213244"/>
  <text x="558" y="235" fill="#91a3b7" font-family="-apple-system, BlinkMacSystemFont, PingFang SC, Microsoft YaHei, sans-serif" font-size="15">has_error 原始一致率</text>
  <text x="558" y="283" fill="#41d9ff" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="38" font-weight="800">100.0%</text>

  <rect x="790" y="202" width="258" height="112" rx="14" fill="#0b1723" stroke="#213244"/>
  <text x="812" y="235" fill="#91a3b7" font-family="-apple-system, BlinkMacSystemFont, PingFang SC, Microsoft YaHei, sans-serif" font-size="15">error_type 原始一致率</text>
  <text x="812" y="283" fill="#41d9ff" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="38" font-weight="800">100.0%</text>

  <text x="72" y="365" fill="#c5d1dd" font-family="-apple-system, BlinkMacSystemFont, PingFang SC, Microsoft YaHei, sans-serif" font-size="20" font-weight="650">首错步骤 · 原始精确字符串</text>
  <rect x="400" y="340" width="576" height="34" rx="8" fill="#142334"/>
  <rect x="400" y="340" width="{raw_width}" height="34" rx="8" fill="#ffbd59"/>
  <text x="998" y="366" text-anchor="end" fill="#ffcf7d" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="22" font-weight="800">{_rate(step.raw.pairwise_agreement)}</text>

  <text x="72" y="429" fill="#c5d1dd" font-family="-apple-system, BlinkMacSystemFont, PingFang SC, Microsoft YaHei, sans-serif" font-size="20" font-weight="650">首错步骤 · 精确别名规范化</text>
  <rect x="400" y="404" width="576" height="34" rx="8" fill="#142334"/>
  <rect x="400" y="404" width="{normalized_width}" height="34" rx="8" fill="#41d9ff"/>
  <text x="998" y="430" text-anchor="end" fill="#8ce9ff" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="22" font-weight="800">{_rate(step.normalized.pairwise_agreement)}</text>
  <rect x="72" y="463" width="976" height="50" rx="10" fill="#0b1723" stroke="#30445a" stroke-dasharray="7 6"/>
  <text x="94" y="494" fill="#aebdca" font-family="-apple-system, BlinkMacSystemFont, PingFang SC, Microsoft YaHei, sans-serif" font-size="16">post-hoc：仅合并同一字段的两种路径写法；规范化 100% 不替代预注册原始 90%</text>

  <line x1="72" y1="548" x2="1048" y2="548" stroke="#213244"/>
  <text x="72" y="582" fill="#718397" font-family="-apple-system, BlinkMacSystemFont, PingFang SC, Microsoft YaHei, sans-serif" font-size="15">exploratory only · n = 4 purposefully selected public cases · 20 judgments · not merged into frozen 57×5 study</text>
</svg>
"""
    return svg.encode("utf-8")


def _atomic_publish(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise Phase4StabilitySensitivityError(
            "release output path is a symlink",
            safe_stage="P4_STABILITY_SENSITIVITY_OUTPUT",
        )
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise Phase4StabilitySensitivityError(
                "release output already exists with different content",
                safe_stage="P4_STABILITY_SENSITIVITY_OUTPUT",
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


def publish_stability_sensitivity_release(
    *,
    run_dir: str | Path,
    output_dir: str | Path = "docs/releases/phase4",
    privacy_canaries: Sequence[str | bytes] = (),
) -> StabilitySensitivityReleaseResult:
    """Write deterministic aggregate JSON, Markdown, and an SVG result card."""

    analysis = analyze_stability_sensitivity(
        run_dir,
        privacy_canaries=privacy_canaries,
    )
    output_root = Path(output_dir).expanduser().resolve()
    if output_root.is_symlink() or (output_root.exists() and not output_root.is_dir()):
        raise Phase4StabilitySensitivityError(
            "release output root is unsafe",
            safe_stage="P4_STABILITY_SENSITIVITY_OUTPUT",
        )
    json_path = output_root / SENSITIVITY_JSON_FILENAME
    markdown_path = output_root / SENSITIVITY_REPORT_FILENAME
    card_path = output_root / SENSITIVITY_CARD_RELATIVE_PATH
    json_payload = _json_bytes(analysis)
    markdown_payload = render_sensitivity_markdown(analysis)
    card_payload = render_stability_card_svg(analysis)
    assert_public_payload_safe(json.loads(json_payload), canaries=privacy_canaries)
    for path, payload in (
        (json_path, json_payload),
        (markdown_path, markdown_payload),
        (card_path, card_payload),
    ):
        _atomic_publish(path, payload)
    return StabilitySensitivityReleaseResult(
        analysis=analysis,
        json_path=json_path,
        markdown_path=markdown_path,
        card_path=card_path,
        json_sha256=_payload_sha256(json_payload),
        markdown_sha256=_payload_sha256(markdown_payload),
        card_sha256=_payload_sha256(card_payload),
    )


__all__ = [
    "SENSITIVITY_ANALYSIS_ID",
    "SENSITIVITY_CARD_RELATIVE_PATH",
    "SENSITIVITY_JSON_FILENAME",
    "SENSITIVITY_NORMALIZATION_POLICY_ID",
    "SENSITIVITY_REPORT_FILENAME",
    "AgreementMeasure",
    "CaseSensitivity",
    "FieldSensitivity",
    "Phase4StabilitySensitivityError",
    "StabilitySensitivityAnalysis",
    "StabilitySensitivityReleaseResult",
    "StepNormalizationRule",
    "analyze_stability_sensitivity",
    "publish_stability_sensitivity_release",
    "render_sensitivity_markdown",
    "render_stability_card_svg",
]
