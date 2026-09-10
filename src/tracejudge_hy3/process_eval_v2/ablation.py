"""2x2 evidence ablation for the process-evaluation pilot.

Four conditions share ONE core rubric, ONE output schema, the same model and
generation parameters; only evidence visibility changes:

- ``ablation_a``: public problem + frozen solution trace only.
- ``ablation_b``: A + AST static evidence.
- ``ablation_c``: A + official aggregate base/plus functional verdict.
- ``ablation_d``: A + both.

Design rules:

- ``functional_correct`` is always forced to null in stored judgments; the
  bound official verdict is recorded in a separate report field, never merged
  into the process signal. Process scoring keeps the three-valued
  ``reasoning_correct AND plan_code_aligned`` logic.
- The Judge's raw structured output and the deterministic rule layer are stored
  separately (``judge_raw`` / ``rule_report`` / merged ``assessment``), so rule
  contribution is compared by offline re-merge of the SAME judge output -- rule
  merging never costs an extra model request.
- The aggregate functional outcome stays aggregate: no per-case execution
  records are fabricated, and a failing verdict is never rewritten into a
  reasoning error by the runner.
- Conditions are judged in a fixed-seed interleaved order stored in the
  pre-registered plan; the plan is hashed into the run identity before any
  call. Prior pilot runs are historical reference only and are NOT claimed as
  pre-registered for this ablation.
- No condition sees human labels, rationales, coordinator layer tags, reference
  implementations, or hidden test content.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tracejudge_hy3.evaluator.alignment import combine_assessment
from tracejudge_hy3.evaluator.rule_based import (
    check_complexity_declaration,
    check_empty_input_claim,
    check_execution_evidence,
    check_set_usage_claim,
    check_single_pass_claim,
    evaluate_alignment_rules,
)
from tracejudge_hy3.process_eval_v2.aggregate import (
    aggregate_execution_summary,
    build_scaffold_problem,
)
from tracejudge_hy3.process_eval_v2.assumptions import (
    ASSUMPTION_PROMPT,
    RUBRICS,
    AssumptionCheck,
    AssumptionJudgment,
    validate_assumptions,
)
from tracejudge_hy3.process_eval_v2.consistency import check_public_consistency, public_assessment
from tracejudge_hy3.process_eval_v2.contracts import PilotInput
from tracejudge_hy3.process_eval_v2.live import _context_check
from tracejudge_hy3.process_eval_v2.location_format import (
    LOCATION_PROMPT,
    LOCATION_RUBRICS,
    location_schema,
    validate_location_format,
)
from tracejudge_hy3.process_eval_v2.materials import canonical, digest
from tracejudge_hy3.process_eval_v2.prompts import _base_payload, _user_prompt
from tracejudge_hy3.prompts.evaluator import build_evaluator_json_schema
from tracejudge_hy3.schemas.evaluation import ProcessAssessment
from tracejudge_hy3.schemas.execution import ExecutionSummary, StaticEvidence
from tracejudge_hy3.schemas.problem import ProblemSpec
from tracejudge_hy3.static_analysis.ast_analyzer import analyze_code

ABLATION_VERSION = "pilot-ablation-v1"
PLAN_SCHEMA = "tracejudge-process-ablation-plan-v1"
RUN_CONFIG_SCHEMA = "tracejudge-process-ablation-run-config-v1"
RUN_REPORT_SCHEMA = "tracejudge-process-ablation-run-v1"
SCORES_SCHEMA = "tracejudge-process-ablation-scores-v1"

#: Fixed shuffle seed for the interleaved execution order; recorded in the plan.
ABLATION_SEED = 20260909

CONDITIONS = ("ablation_a", "ablation_b", "ablation_c", "ablation_d")
Condition = Literal["ablation_a", "ablation_b", "ablation_c", "ablation_d"]

#: Evidence visibility per condition. Nothing else may differ.
CONDITION_DEFS: dict[str, dict[str, bool]] = {
    "ablation_a": {"static_evidence": False, "functional_evidence": False},
    "ablation_b": {"static_evidence": True, "functional_evidence": False},
    "ablation_c": {"static_evidence": False, "functional_evidence": True},
    "ablation_d": {"static_evidence": True, "functional_evidence": True},
}

#: Pre-registered paired comparisons (second element is the baseline).
COMPARISONS = (
    ("ablation_b", "ablation_a"),  # showing static evidence to the Judge
    ("ablation_c", "ablation_a"),  # showing the aggregate functional verdict
    ("ablation_d", "ablation_b"),  # adding functional verdict given static
    ("ablation_d", "ablation_c"),  # adding static evidence given functional
)

_EVIDENCE_BLOCK = {
    "ablation_a": (
        "本轮不提供 AST 静态分析证据，也不提供官方基准功能结果；只依据题目与解答材料判断。"
    ),
    "ablation_b": (
        "本轮额外提供代码的 AST 静态分析证据（输入中的 static_evidence 字段），"
        "不提供官方基准功能结果。静态证据可作为判断依据之一；"
        "其中未列出的内容不等于不存在。"
    ),
    "ablation_c": (
        "本轮不提供 AST 静态分析证据；额外提供官方基准的聚合功能结果"
        "（输入中的 official_aggregate_functional_evidence 字段）。"
        "聚合功能结果只说明该候选在官方 MBPP base 与 plus 测试集上的总体通过状态，"
        "它不是逐用例执行记录；不得据此编造或引用任何具体测试用例。"
        "功能未通过不等于过程一定有错，功能通过也不等于过程正确。"
    ),
    "ablation_d": (
        "本轮额外提供代码的 AST 静态分析证据（输入中的 static_evidence 字段），"
        "并提供官方基准的聚合功能结果（输入中的 official_aggregate_functional_evidence 字段）。"
        "静态证据可作为判断依据之一；其中未列出的内容不等于不存在。"
        "聚合功能结果只说明该候选在官方 MBPP base 与 plus 测试集上的总体通过状态，"
        "它不是逐用例执行记录；不得据此编造或引用任何具体测试用例。"
        "功能未通过不等于过程一定有错，功能通过也不等于过程正确。"
    ),
}

#: One core rubric for all four conditions. The ONLY interpolated part is the
#: evidence-availability sentence; everything else is byte-identical.
ABLATION_SYSTEM_CORE = """\
你是一名代码生成过程评估器。你会收到题目需求、需求条款与模型给出的结构化解答
（reasoning + 代码）。{evidence_block}

你需要依次判断：
1. 需求理解是否正确（reasoning 是否正确理解了题目目标、输入输出含义与约束）；
2. 解题说明内部是否成立（步骤之间是否有依据、是否前后矛盾、复杂度声明是否合理）；
3. 实现步骤与代码是否一致（reasoning 中声称的关键逻辑是否能在代码中找到对应实现）；
4. 如果存在错误，首个错误发生在哪个层级（requirement / reasoning / alignment /
   implementation / execution）；
5. 首个错误对应哪个实现步骤（如果适用，给出 step_id）；
6. 该错误违反了哪个需求条款（给出 requirement_id）；
7. 属于哪种错误类型（从给定的错误类型体系中选择一个主要类型，允许附加次要类型）；
8. 你的结论由哪些具体证据支持（引用所提供材料中的具体字段，而不是凭空猜测）。

重要约束：
- functional_correct 必须输出 null，不要推测功能正确性；功能结论由独立渠道记录。
- first_faulty_location 可引用 requirement_understanding、design_summary、edge_cases_considered、
  implementation_steps、复杂度声明或 code。quote 必须逐字来自该字段；不能编造或改写引用。
  implementation_steps 必须指定真实 step_id；edge_cases_considered 必须指定从 0 开始的 entry_index。
  其他字段的 step_id 为 null；设计摘要有错不意味着 S1 有错。无法定位时整个 location 为 null。
  first_faulty_step 必须与 location.step_id 一致。找到引用只证明出处，不自动证明主张错误。
- 不要仅因为代码与你设想的实现方式不同就判定为错误；只要代码满足需求条款，
  应视为合法的等价实现。
- 无法确定时对应字段输出 null，不要猜测。
- 只输出一个符合给定 JSON Schema 的 JSON 对象，不要输出 Markdown 代码围栏，
  不要输出 JSON 之外的任何文字。
"""

#: Deterministic rules observed for every condition (offline, never a request).
RULE_CHECKS = (
    "empty_input_claim",
    "set_usage_claim",
    "single_pass_claim",
    "complexity_declaration",
    "execution_evidence",
)


class FunctionalEvidenceSnapshot(BaseModel):
    """Public aggregate verdict, stored beside (never inside) the process signal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: str
    infrastructure_status: str
    base_status: str
    plus_status: str
    functional_correct: bool | None


class RuleObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    fired: bool
    first_faulty_layer: str | None = None
    error_type: str | None = None
    basis: str | None = None  # the rule's own explanation when it fired


class RuleReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[RuleObservation]
    merged_rule: ProcessAssessment | None
    combination_source: Literal["rule", "llm"]


class AblationPrediction(BaseModel):
    """One condition's record: raw judge output, rule report, merged judgment."""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    condition: Condition
    input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["ok", "provider_error", "parse_error"]
    functional_evidence: FunctionalEvidenceSnapshot
    judge_raw: ProcessAssessment | None = None
    rule_report: RuleReport | None = None
    assessment: ProcessAssessment | None = None  # rule-merged final judgment
    assumption_review: list[AssumptionCheck] | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        complete = (
            self.judge_raw is not None
            and self.rule_report is not None
            and self.assessment is not None
        )
        if (self.status == "ok") != complete:
            raise ValueError("only successful ablation rows carry judge/rule/merged results")
        return self


def _functional_snapshot(item: PilotInput) -> FunctionalEvidenceSnapshot:
    evidence = item.functional_evidence
    return FunctionalEvidenceSnapshot(
        scope=evidence.scope,
        infrastructure_status=evidence.infrastructure_status,
        base_status=evidence.base_status,
        plus_status=evidence.plus_status,
        functional_correct=evidence.functional_correct,
    )


def condition_payload_fields(condition: Condition) -> list[str]:
    fields = ["title", "requirement", "function_signature", "requirements", "solution_trace"]
    if CONDITION_DEFS[condition]["static_evidence"]:
        fields.append("static_evidence")
    if CONDITION_DEFS[condition]["functional_evidence"]:
        fields.append("official_aggregate_functional_evidence")
    return fields


def rubric_contract(rubric: str):
    if rubric not in RUBRICS:
        raise ValueError("unknown evaluation rubric")
    audit = rubric in ("assumption_audit_v1", "assumption_audit_location_v2")
    extra = ASSUMPTION_PROMPT if audit else ""
    model = AssumptionJudgment if audit else ProcessAssessment
    schema = model.model_json_schema() if audit else build_evaluator_json_schema()
    if rubric in LOCATION_RUBRICS:
        extra += LOCATION_PROMPT
        schema = location_schema(schema)
    return extra, schema, model


def build_ablation_prompt(
    condition: Condition,
    item: PilotInput,
    static_evidence: StaticEvidence | None,
    *,
    rubric: str = "baseline",
) -> tuple[str, str]:
    """Same rubric and schema for every condition; only evidence visibility varies."""

    payload = _base_payload(item)
    if CONDITION_DEFS[condition]["static_evidence"]:
        if static_evidence is None:  # pragma: no cover - defensive
            raise ValueError("condition requires static evidence")
        payload["static_evidence"] = static_evidence.model_dump(mode="json")
    if CONDITION_DEFS[condition]["functional_evidence"]:
        evidence = item.functional_evidence
        payload["official_aggregate_functional_evidence"] = {
            "scope": evidence.scope,
            "infrastructure_status": evidence.infrastructure_status,
            "base_status": evidence.base_status,
            "plus_status": evidence.plus_status,
            "note": "官方聚合结果（base/plus 总体通过状态），不是逐用例执行记录。",
        }
    extra, schema, _ = rubric_contract(rubric)
    system = (
        f"{ABLATION_SYSTEM_CORE.format(evidence_block=_EVIDENCE_BLOCK[condition])}\n\n"
        f"{extra}JSON Schema:\n{json.dumps(schema, ensure_ascii=False, indent=2)}"
    )
    return system, _user_prompt(payload)


def condition_fingerprints(rubric: str = "baseline") -> dict[str, str]:
    """Bind each condition's prompt, visibility and output schema for resume checks."""

    extra, schema, _ = rubric_contract(rubric)
    return {
        condition: digest(
            canonical(
                {
                    "version": ABLATION_VERSION,
                    "system": ABLATION_SYSTEM_CORE.format(evidence_block=_EVIDENCE_BLOCK[condition])
                    + extra,
                    "payload_fields": condition_payload_fields(condition),
                    "output_schema": schema,
                }
            )
        )
        for condition in CONDITIONS
    }


def execution_order(item_ids, seed: int = ABLATION_SEED) -> list[tuple[str, str]]:
    """Fixed-seed interleaving of all (condition, item) judgments."""

    pairs = [(condition, item_id) for condition in CONDITIONS for item_id in sorted(item_ids)]
    random.Random(seed).shuffle(pairs)
    return pairs


def build_plan(
    pilot,
    *,
    seed: int = ABLATION_SEED,
    max_requests: int = 96,
    max_attempts_per_judgment: int = 2,
    rubric: str = "baseline",
    conditions: tuple[str, ...] = CONDITIONS,
) -> dict:
    """The pre-registered ablation plan, hashed into the run identity."""

    if (
        not conditions
        or len(set(conditions)) != len(conditions)
        or set(conditions) - set(CONDITIONS)
    ):
        raise ValueError("invalid condition selection")
    if max_requests < 1 or max_attempts_per_judgment < 1:
        raise ValueError("request budgets must be positive")
    fingerprints = condition_fingerprints(rubric)
    order = [pair for pair in execution_order(pilot.inputs.keys(), seed) if pair[0] in conditions]
    judgments = len(order)
    plan = {
        "schema": PLAN_SCHEMA,
        "version": ABLATION_VERSION,
        "created_utc": datetime.now(UTC).isoformat(),
        "purpose": (
            "locate the source of the full-system gain by varying evidence visibility "
            "under one rubric; not an attempt to prove the full system is best"
        ),
        "seed": seed,
        "conditions": {
            condition: {
                "evidence": CONDITION_DEFS[condition],
                "payload_fields": condition_payload_fields(condition),
                "fingerprint": fingerprints[condition],
            }
            for condition in conditions
        },
        "items": {key: pilot.input_hash(key) for key in sorted(pilot.inputs)},
        "execution_order": [[condition, item_id] for condition, item_id in order],
        "budget": {
            "planned_judgments": judgments,
            "max_attempts_per_judgment": max_attempts_per_judgment,
            "max_requests": max_requests,
            "covers_all_planned_retries": max_requests >= judgments * max_attempts_per_judgment,
        },
        "stop_rules": [
            "stop when the cumulative request budget is exhausted; spent attempts stay spent",
            "stop after the first authentication error",
            "a judgment is final after max_attempts_per_judgment consumed attempts",
            "resume never resets consumed budget and refuses changed plans or inputs",
        ],
        "metrics": [
            "coverage (ok/missing/provider_error/parse_error) per condition",
            "accuracy_all_known, error recall/precision, false-positive rate on the "
            "three-valued public process signal, for judge_raw and rule-merged views",
            "paired per-sample transitions for the four pre-registered comparisons",
            "rule-merge deltas: which samples the deterministic rules changed",
        ],
        "comparisons": [list(pair) for pair in COMPARISONS if set(pair) <= set(conditions)],
        "label_scope": [
            "12 post-feedback development consensus labels: 8 correct, 2 incorrect, 2 unknown",
            "not an independent held-out set; annotator independence unverified",
            "first-step and structured-location gold counts are zero; those metrics stay null",
            "all 12 alignment gold labels are true: no mismatch-detection claim is possible",
            "unknown labels stay in coverage but leave binary accuracy denominators",
        ],
    }
    if (
        rubric != "baseline"
        or conditions != CONDITIONS
        or pilot.label_stage != "post_feedback_development_consensus"
    ):
        plan.update(
            version="process-method-validation-v2",
            rubric=rubric,
            label_stage=pilot.label_stage,
            annotation_provenance=pilot.annotation_provenance,
            verified_sources=pilot.provenance,
        )
        plan["label_scope"] = [
            f"items={len(pilot.labels)}; known={sum(x.process_correct is not None for x in pilot.labels.values())}; "
            f"errors={sum(x.process_correct is False for x in pilot.labels.values())}",
            "development and constructed probes are separate populations, not held-out results",
            "unknown labels remain visible; locations score only against non-null supported gold",
        ]
        plan["purpose"] = (
            "compare a fixed baseline with general assumption auditing; no task-specific rules"
        )
    return plan


def plan_fingerprint(plan: dict) -> str:
    """Hash the plan content, excluding the creation timestamp."""

    return digest(canonical({key: value for key, value in plan.items() if key != "created_utc"}))


def build_rule_report(
    problem: ProblemSpec,
    item: PilotInput,
    static_evidence: StaticEvidence,
    execution: ExecutionSummary,
) -> RuleReport:
    """Observe each deterministic rule separately, then the merged rule output.

    Runs on every condition (even ones whose Judge never saw static evidence) so
    the rule contribution can be separated from evidence visibility offline.
    """

    checks = {
        "empty_input_claim": lambda: check_empty_input_claim(item.solution_trace, static_evidence),
        "set_usage_claim": lambda: check_set_usage_claim(item.solution_trace, static_evidence),
        "single_pass_claim": lambda: check_single_pass_claim(item.solution_trace, static_evidence),
        "complexity_declaration": lambda: check_complexity_declaration(
            item.solution_trace, static_evidence
        ),
        "execution_evidence": lambda: check_execution_evidence(problem, execution, static_evidence),
    }
    observations = []
    for rule_id in RULE_CHECKS:
        result = checks[rule_id]()
        observations.append(
            RuleObservation(
                rule_id=rule_id,
                fired=result is not None,
                first_faulty_layer=result.first_faulty_layer if result else None,
                error_type=str(result.error_type) if result and result.error_type else None,
                basis=result.explanation if result else None,
            )
        )
    merged = evaluate_alignment_rules(problem, item.solution_trace, static_evidence, execution)
    return RuleReport(
        rules=observations,
        merged_rule=merged,
        combination_source="rule" if merged is not None else "llm",
    )


def merge_offline(
    problem: ProblemSpec,
    item: PilotInput,
    static_evidence: StaticEvidence,
    execution: ExecutionSummary,
    judge_raw: ProcessAssessment,
    merged_rule: ProcessAssessment | None,
) -> ProcessAssessment:
    """Rule-merge one stored judge output; deterministic, never a model request.

    functional_correct is forced to null afterwards: the aggregate execution
    view carries no per-case results, and the official verdict lives only in the
    prediction's separate functional_evidence field.
    """

    combined = combine_assessment(
        problem,
        item.solution_trace,
        static_evidence,
        execution,
        judge_raw,
        rule_assessment=merged_rule,
    )
    return public_assessment(combined)


async def judge_ablation(
    condition, item: PilotInput, provider, pilot, *, rubric="baseline"
) -> AblationPrediction:
    """One ablation judgment; every model retry stays inside the provider call."""

    problem = build_scaffold_problem(item)
    static_evidence = analyze_code(
        item.solution_trace.code,
        function_name=problem.function_name,
        visible_test_values=[],
    )
    execution = aggregate_execution_summary(item)
    system, user = build_ablation_prompt(condition, item, static_evidence, rubric=rubric)

    def check(judgment):
        assessment = judgment.assessment if isinstance(judgment, AssumptionJudgment) else judgment
        _context_check(item)(assessment)
        check_public_consistency(assessment)
        if rubric in LOCATION_RUBRICS:
            validate_location_format(judgment, item)
        if isinstance(judgment, AssumptionJudgment):
            validate_assumptions(judgment.checks, assessment, item)

    result = await provider.judge_model(
        system,
        user,
        rubric_contract(rubric)[2],
        extra_check=check,
    )
    judged = result.assessment if isinstance(result, AssumptionJudgment) else result
    # The model is instructed to leave functional_correct null; enforce it and
    # keep the public three-valued process signal free of functional evidence.
    judge_raw = public_assessment(judged)
    rule_report = build_rule_report(problem, item, static_evidence, execution)
    merged = merge_offline(
        problem, item, static_evidence, execution, judge_raw, rule_report.merged_rule
    )
    return AblationPrediction(
        item_id=item.item_id,
        condition=condition,
        input_sha256=pilot.input_hash(item.item_id),
        status="ok",
        functional_evidence=_functional_snapshot(item),
        judge_raw=judge_raw,
        rule_report=rule_report,
        assessment=merged,
        assumption_review=result.checks if isinstance(result, AssumptionJudgment) else None,
    )
