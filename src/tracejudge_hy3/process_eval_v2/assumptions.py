"""General requirement/assumption audit; no task identities or gold labels."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tracejudge_hy3.schemas.evaluation import ProcessAssessment
from tracejudge_hy3.schemas.location import FaultLocation

RUBRICS = (
    "baseline",
    "assumption_audit_v1",
    "baseline_location_v2",
    "assumption_audit_location_v2",
)
ASSUMPTION_PROMPT = """
在给出最终评估前，核对公开需求与解答中的关键假设：
1. 找出解答新增、收紧或改变的输入范围、量词、边界定义、输出语义和算法前提。
2. 将关键主张与对应公开需求逐字引用配对；举出能区分两种解释的情形，
   但必须说明它是否属于公开允许的输入范围。未执行的推演不得称为实测。
3. 分类为 explicit_contradiction（与明确条款或允许输入下的推导直接矛盾）、
   unsupported_restriction（收紧需求且有可论证的语义/行为差异）、
   reasonable_interpretation（合理、已说明且不违反公开约束的解释）、
   semantic_ambiguity（公开材料不足以排除多种解释）。
4. 不能把“题面未逐字说明”本身当成错误；实现细节、等价算法或已明确的合理解释不应误报。
   单个示例不能证明某种解释唯一；不得依据基准总体通过/失败反推隐藏需求。
5. 对 semantic_ambiguity 保留不确定性：若它决定 reasoning 是否成立且没有其他独立错误，
   reasoning_correct 应为 null。明确矛盾或有证据的限制错误才支持 false。
6. 每个检查项给出 requirement_id、requirement_quote、claim（引用解答的字段与原文）、
   category 和 rationale（依据、区分情形及限制）。claim 是待审核主张位置，未必是错误位置。
7. 没有可疑假设可以返回空 checks；仍需完成算法和计划—代码对齐检查。
   最终 assessment 与 checks 必须相互一致，引用匹配只证明出处，不证明分类正确。
"""


class AssumptionCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirement_id: str
    requirement_quote: str = Field(min_length=1)
    claim: FaultLocation
    category: Literal[
        "explicit_contradiction",
        "unsupported_restriction",
        "reasonable_interpretation",
        "semantic_ambiguity",
    ]
    rationale: str = Field(min_length=1)


class AssumptionJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assessment: ProcessAssessment
    checks: list[AssumptionCheck] = Field(max_length=12)


def validate_assumptions(
    checks: list[AssumptionCheck], assessment: ProcessAssessment, item
) -> None:
    requirements = {r.requirement_id: r.content for r in item.problem.requirements}
    for check in checks:
        if not check.rationale.strip() or not check.requirement_quote.strip():
            raise ValueError("assumption evidence must not be blank")
        if (
            check.requirement_id not in requirements
            or check.requirement_quote not in requirements[check.requirement_id]
        ):
            raise ValueError("assumption requirement quote does not bind to public requirements")
        check.claim.validate_against(item.solution_trace)
        if (
            check.category in ("explicit_contradiction", "unsupported_restriction")
            and assessment.reasoning_correct is not False
            and assessment.plan_code_aligned is not False
        ):
            raise ValueError(
                "assumption error classification conflicts with public process dimensions"
            )
    # A code contradiction may invalidate alignment while the plan stays sound.
    # Quote source alone cannot determine which dimension is wrong; never
    # rewrite a verdict or discard localization to satisfy this check.
    # Ambiguity can coexist with an independent algorithm error; do not enforce
    # a verdict from categories alone. Semantic correctness needs human review.
