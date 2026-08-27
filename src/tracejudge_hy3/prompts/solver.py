"""Prompt construction for the Hy3 Solver call.

The model is asked for an auditable, user-facing solution write-up -- not a
hidden chain-of-thought -- plus the final code, as strict JSON matching
SolutionTrace's schema.
"""

from __future__ import annotations

import json

from tracejudge_hy3.schemas.problem import ProblemSpec
from tracejudge_hy3.schemas.solution import SolutionTrace

SOLVER_SYSTEM_PROMPT = """\
你是一名严谨的 Python 工程师，正在完成一个函数级代码生成任务。

你必须只输出一个符合给定 JSON Schema 的 JSON 对象，不要输出 Markdown 代码围栏（如 ```json），
不要输出 JSON 之外的任何解释性文字。

关于 JSON 各字段的要求：
- requirement_understanding：用自然语言概括你对需求的理解（目标、输入输出约束）。
- design_summary：简要说明你选择的算法或实现思路。
- edge_cases_considered：列出你认为需要处理的边界情况。
- implementation_steps：将实现过程拆分为若干可审查的步骤，每一步说明关联的需求条款
  （从题目给出的 requirement_id 中选择）以及该步骤在代码中预期对应的行为。
- declared_time_complexity / declared_space_complexity：给出你认为方案的时间和空间复杂度。
- code：完整的 Python 函数实现，函数名和签名必须与题目给出的函数签名一致。

重要约束：
1. implementation_steps 中的内容应当是你愿意展示给用户审查的解题说明和实现计划，
   不需要输出不可见的内部思维过程，但内容必须真实反映你实际采用的方案，不能是与代码不符的编造描述。
2. 不要针对下方给出的可见测试用例进行硬编码或特判（例如直接判断输入是否等于某个样例并返回固定答案）。
3. 不要假设或引用题面未提供的测试、期望值或其他信息。
4. 代码必须是可以独立运行的完整函数定义，不要包含使用示例、print 调用或额外的解释文字。
"""


def _test_case_preview(problem: ProblemSpec) -> list[dict]:
    return [
        {"args": tc.args, "kwargs": tc.kwargs, "expected": tc.expected}
        for tc in problem.visible_test_cases
    ]


def solver_public_payload(problem: ProblemSpec) -> dict:
    """Return the exact public-data allowlist supplied to the Solver."""

    return {
        "problem_id": problem.problem_id,
        "requirement": problem.requirement,
        "function_signature": problem.function_signature,
        "requirements": [
            {"requirement_id": r.requirement_id, "content": r.content} for r in problem.requirements
        ],
        "visible_test_cases": _test_case_preview(problem),
    }


def build_solver_user_prompt(problem: ProblemSpec) -> str:
    payload = solver_public_payload(problem)
    return (
        "请为以下题目生成结构化解答：\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n\n请严格按照系统提示中的 JSON Schema 输出结果。"
    )


def build_solver_json_schema() -> dict:
    schema = SolutionTrace.model_json_schema()
    schema["additionalProperties"] = False
    return schema
