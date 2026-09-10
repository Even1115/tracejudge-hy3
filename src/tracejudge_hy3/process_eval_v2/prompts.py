"""Prompts and visibility rules for the three pilot judging methods.

Method ladder (each tier is a strict superset of structure/evidence):

- ``direct_judge``: problem + frozen solution trace only; a short rubric; a
  reduced output schema (reasoning / alignment / explanation). No functional
  evidence, no static evidence, no layer/step/location output.
- ``structured_judge``: the same materials as ``direct_judge``, but the full
  structured rubric (four layers, error taxonomy, quote-bound locations) and
  the full ProcessAssessment schema with context validation.
- ``full_system``: the same structured rubric plus derived evidence -- real
  AST static analysis of the frozen code and the official aggregate base/plus
  functional outcome -- combined with the deterministic rule layer, exactly
  like the product pipeline. Aggregate functional evidence is explicitly
  labelled as aggregate; no per-test records exist or are fabricated.

No method ever sees human labels, rationales, coordinator layer tags, reference
implementations, or hidden test content: every payload is built from PilotInput
public fields (plus derived static/aggregate evidence for full_system).
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field

from tracejudge_hy3.process_eval_v2.contracts import PilotInput
from tracejudge_hy3.process_eval_v2.materials import canonical, digest
from tracejudge_hy3.prompts.evaluator import build_evaluator_json_schema
from tracejudge_hy3.schemas.execution import StaticEvidence

METHOD_DEFINITION_VERSION = "pilot-methods-v1"

#: Fields each method's user payload contains, in order. Fingerprints bind these
#: lists so a silent visibility change invalidates any previous run directory.
BASE_PAYLOAD_FIELDS = (
    "title",
    "requirement",
    "function_signature",
    "requirements",
    "solution_trace",
)
FULL_SYSTEM_EXTRA_FIELDS = ("static_evidence", "official_aggregate_functional_evidence")

DIRECT_JUDGE_SYSTEM_PROMPT = """\
你是一名代码评审员。你会收到题目需求与一份模型给出的结构化解答
（需求理解、设计摘要、边界情况、实现步骤与代码）。

请直接判断两件事：
1. reasoning_correct：解答对题目目标、输入输出含义与约束的理解是否正确，
   以及整体设计是否成立；
2. plan_code_aligned：设计摘要与实现步骤所声称的关键逻辑是否与代码实际行为一致。

约束：
- 只依据给出的材料判断。你没有、也不要假设任何测试执行结果或静态分析证据。
- 不要仅因为代码与你设想的实现方式不同就判定为错误；满足需求的等价实现是合法的。
- 无法确定时对应字段输出 null，不要猜测。
- 只输出一个符合给定 JSON Schema 的 JSON 对象，不要输出 Markdown 代码围栏，
  不要输出 JSON 之外的任何文字。
"""

STRUCTURED_JUDGE_SYSTEM_PROMPT = """\
你是一名代码生成过程评估器。你会收到题目需求、需求条款、模型给出的结构化解答
（reasoning + 代码）。本轮不提供 AST 静态分析证据，也不提供任何测试执行结果。

你需要依次判断：
1. 需求理解是否正确（reasoning 是否正确理解了题目目标、输入输出含义与约束）；
2. 解题说明内部是否成立（步骤之间是否有依据、是否前后矛盾、复杂度声明是否合理）；
3. 实现步骤与代码是否一致（reasoning 中声称的关键逻辑是否能在代码中找到对应实现）；
4. 如果存在错误，首个错误发生在哪个层级（requirement / reasoning / alignment /
   implementation / execution）；
5. 首个错误对应哪个实现步骤（如果适用，给出 step_id）；
6. 该错误违反了哪个需求条款（给出 requirement_id）；
7. 属于哪种错误类型（从给定的错误类型体系中选择一个主要类型，允许附加次要类型）；
8. 你的结论由哪些具体证据支持（引用解答中的具体字段，而不是凭空猜测）。

重要约束：
- 你没有测试执行结果：functional_correct 必须输出 null，不要推测功能正确性。
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

FULL_SYSTEM_SYSTEM_PROMPT = """\
你是一名代码生成过程评估器（完整系统模式）。你会收到题目需求、需求条款、模型给出的
结构化解答（reasoning + 代码）、代码的 AST 静态分析证据，以及官方基准的聚合功能结果。

关于功能证据：
- 聚合功能结果只说明该候选在官方 MBPP base 与 plus 测试集上的总体通过状态，
  它不是逐用例执行记录；不得据此编造或引用任何具体测试用例。
- functional_correct 由系统根据聚合证据填写，你的输出中该字段会被覆盖，请输出 null。
- 功能未通过不等于过程一定有错，功能通过也不等于 reasoning 或计划—代码对齐正确；
  你仍需基于过程材料独立判断。

你需要依次判断：
1. 需求理解是否正确（reasoning 是否正确理解了题目目标、输入输出含义与约束）；
2. 解题说明内部是否成立（步骤之间是否有依据、是否前后矛盾、复杂度声明是否合理）；
3. 实现步骤与代码是否一致（reasoning 中声称的关键逻辑是否能在代码或静态证据中找到对应实现）；
4. 聚合功能结果与静态证据各自说明了什么（不得当作逐用例执行记录）；
5. 如果存在错误，首个错误发生在哪个层级（requirement / reasoning / alignment / implementation / execution）；
6. 首个错误对应哪个实现步骤（如果适用，给出 step_id）；
7. 该错误违反了哪个需求条款（给出 requirement_id）；
8. 属于哪种错误类型（从给定的错误类型体系中选择一个主要类型，允许附加次要类型）；
9. 你的结论由哪些具体证据支持（引用 static_evidence 字段或解答中的具体文本，而不是凭空猜测）。

重要约束：
- first_faulty_location 可引用 requirement_understanding、design_summary、edge_cases_considered、
  implementation_steps、复杂度声明或 code。quote 必须逐字来自该字段；不能编造或改写引用。
  implementation_steps 必须指定真实 step_id；edge_cases_considered 必须指定从 0 开始的 entry_index。
  其他字段的 step_id 为 null；设计摘要有错不意味着 S1 有错。无法定位时整个 location 为 null。
  first_faulty_step 必须与 location.step_id 一致。找到引用只证明出处，不自动证明主张错误。
- 不要仅因为代码与你设想的实现方式不同就判定为错误；只要代码满足需求条款，
  应视为合法的等价实现。
- 你的判断应尽量引用 static_evidence 中的具体字段作为依据，而不是仅凭 reasoning 文本自由发挥。
- 只输出一个符合给定 JSON Schema 的 JSON 对象，不要输出 Markdown 代码围栏，
  不要输出 JSON 之外的任何文字。
"""


class DirectJudgment(BaseModel):
    """Reduced direct-judge output; mapped onto ProcessAssessment afterwards."""

    model_config = ConfigDict(extra="forbid")

    reasoning_correct: bool | None
    plan_code_aligned: bool | None
    explanation: str = Field(min_length=1)


def _direct_schema() -> dict:
    schema = DirectJudgment.model_json_schema()
    schema["additionalProperties"] = False
    return schema


def _base_payload(item: PilotInput) -> dict:
    return {
        "title": item.problem.title,
        "requirement": item.problem.requirement,
        "function_signature": item.problem.function_signature,
        "requirements": [
            {"requirement_id": r.requirement_id, "content": r.content}
            for r in item.problem.requirements
        ],
        "solution_trace": json.loads(item.solution_trace.model_dump_json()),
    }


def _user_prompt(payload: dict) -> str:
    return (
        "请基于以下材料完成评估：\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n\n请严格按照系统提示中的 JSON Schema 输出结果。"
    )


def build_direct_judge_prompt(item: PilotInput) -> tuple[str, str]:
    system = (
        f"{DIRECT_JUDGE_SYSTEM_PROMPT}\n\nJSON Schema:\n"
        f"{json.dumps(_direct_schema(), ensure_ascii=False, indent=2)}"
    )
    return system, _user_prompt(_base_payload(item))


def build_structured_judge_prompt(item: PilotInput) -> tuple[str, str]:
    system = (
        f"{STRUCTURED_JUDGE_SYSTEM_PROMPT}\n\nJSON Schema:\n"
        f"{json.dumps(build_evaluator_json_schema(), ensure_ascii=False, indent=2)}"
    )
    return system, _user_prompt(_base_payload(item))


def build_full_system_prompt(item: PilotInput, static_evidence: StaticEvidence) -> tuple[str, str]:
    evidence = item.functional_evidence
    payload = {
        **_base_payload(item),
        "static_evidence": json.loads(static_evidence.model_dump_json()),
        "official_aggregate_functional_evidence": {
            "scope": evidence.scope,
            "infrastructure_status": evidence.infrastructure_status,
            "base_status": evidence.base_status,
            "plus_status": evidence.plus_status,
            "note": "官方聚合结果（base/plus 总体通过状态），不是逐用例执行记录。",
        },
    }
    system = (
        f"{FULL_SYSTEM_SYSTEM_PROMPT}\n\nJSON Schema:\n"
        f"{json.dumps(build_evaluator_json_schema(), ensure_ascii=False, indent=2)}"
    )
    return system, _user_prompt(payload)


_METHOD_FINGERPRINT_PARTS = {
    "direct_judge": {
        "system": DIRECT_JUDGE_SYSTEM_PROMPT,
        "payload_fields": list(BASE_PAYLOAD_FIELDS),
        "output_schema": _direct_schema(),
    },
    "structured_judge": {
        "system": STRUCTURED_JUDGE_SYSTEM_PROMPT,
        "payload_fields": list(BASE_PAYLOAD_FIELDS),
        "output_schema": build_evaluator_json_schema(),
    },
    "full_system": {
        "system": FULL_SYSTEM_SYSTEM_PROMPT,
        "payload_fields": [*BASE_PAYLOAD_FIELDS, *FULL_SYSTEM_EXTRA_FIELDS],
        "output_schema": build_evaluator_json_schema(),
    },
}


def method_fingerprints() -> dict[str, str]:
    """Bind each method's prompt, visibility and output schema for resume checks."""

    return {
        method: digest(canonical({"version": METHOD_DEFINITION_VERSION, **parts}))
        for method, parts in _METHOD_FINGERPRINT_PARTS.items()
    }
