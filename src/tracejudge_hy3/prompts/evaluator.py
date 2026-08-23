"""Prompt construction for the Hy3 Evaluator (process-judge) call."""

from __future__ import annotations

import json

from tracejudge_hy3.schemas.evaluation import ProcessAssessment
from tracejudge_hy3.schemas.execution import ExecutionSummary, StaticEvidence
from tracejudge_hy3.schemas.problem import ProblemSpec
from tracejudge_hy3.schemas.solution import SolutionTrace

EVALUATOR_SYSTEM_PROMPT = """\
你是一名代码生成过程评估器。你会收到题目需求、需求条款、模型给出的结构化解答
（reasoning + 代码）、代码的 AST 静态分析证据，以及测试执行结果。

你需要依次判断：
1. 需求理解是否正确（reasoning 是否正确理解了题目目标、输入输出含义与约束）；
2. 解题说明内部是否成立（步骤之间是否有依据、是否前后矛盾、复杂度声明是否合理）；
3. 实现步骤与代码是否一致（reasoning 中声称的关键逻辑是否能在代码或静态证据中找到对应实现）；
4. 执行行为是否满足需求（可见/隐藏/挑战测试的通过情况说明了什么）；
5. 如果存在错误，首个错误发生在哪个层级（requirement / reasoning / alignment / implementation / execution）；
6. 首个错误对应哪个实现步骤（如果适用，给出 step_id）；
7. 该错误违反了哪个需求条款（给出 requirement_id）；
8. 属于哪种错误类型（从给定的错误类型体系中选择一个主要类型，允许附加次要类型）；
9. 你的结论由哪些具体证据支持（引用静态证据字段或测试用例结果，而不是凭空猜测）。

重要约束：
- 不要仅因为代码与你设想的实现方式不同就判定为错误；只要代码满足需求条款且执行结果正确，
  应视为合法的等价实现。
- 你的判断应尽量引用 static_evidence 或 execution_result 中的具体字段作为依据，
  而不是仅凭 reasoning 文本自由发挥。
- 只输出一个符合给定 JSON Schema 的 JSON 对象，不要输出 Markdown 代码围栏，
  不要输出 JSON 之外的任何文字。
"""


def build_evaluator_user_prompt(
    problem: ProblemSpec,
    solution: SolutionTrace,
    static_evidence: StaticEvidence,
    execution_result: ExecutionSummary,
) -> str:
    payload = {
        "requirement": problem.requirement,
        "requirements": [
            {"requirement_id": r.requirement_id, "content": r.content} for r in problem.requirements
        ],
        "solution_trace": json.loads(solution.model_dump_json()),
        "static_evidence": json.loads(static_evidence.model_dump_json()),
        "execution_result": json.loads(execution_result.model_dump_json()),
    }
    return (
        "请基于以下材料完成四层过程评估：\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n\n请严格按照系统提示中的 JSON Schema 输出结果。"
    )


def build_evaluator_json_schema() -> dict:
    schema = ProcessAssessment.model_json_schema()
    schema["additionalProperties"] = False
    return schema
