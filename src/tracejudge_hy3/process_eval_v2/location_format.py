"""Versioned Judge-facing location schema; historical model schemas stay frozen."""

import re
from copy import deepcopy

LOCATION_RUBRICS = ("baseline_location_v2", "assumption_audit_location_v2")
SPAN_PATTERN = r"^L[1-9][0-9]*(?:-L[1-9][0-9]*)?$"
LOCATION_PROMPT = """
定位输出规范 location_v2：
- quote 存放 source_field 对应原文中的逐字引用，选取足以支持判断的局部片段。
  代码文字放在 source_field=code 的 quote 中，不要把代码文字填入 code_span。
- 所有 code_span（assessment 顶层、first_faulty_location、checks[*].claim）只允许
  行号如 "L3"、"L3-L5"，或 null；行号从 solution_trace.code 第一行开始计数，闭区间。
  不确定行号时使用 null；不能输出表达式、函数名、"3-5" 或 Markdown 围栏。
- 例如代码原文位于第三行，可写 quote="return total", code_span="L3"；
  这只是格式示例，不得复制到不含该原文的任务中。
- implementation_steps 引用必须使用真实 step_id；code 引用的 step_id 必须为 null。
  first_faulty_step 与 first_faulty_location.step_id 一致，不为了填步骤号改变证据来源。
  计划正确但代码失配时，可在代码位置指出失配，并在 explanation 说明相关计划步骤；
  不能据此声称那个计划步骤本身推理错误。affected_steps 也不等同于首错步骤。
- edge_cases_considered 使用从 0 开始的 entry_index；其他来源 entry_index 为 null。
  先依据材料判断，再选择定位；不猜测金标的措辞。引用可验证不等于语义结论正确。
"""


def location_schema(schema: dict) -> dict:
    """Add machine-readable constraints at every span/quote property, on a copy."""
    result = deepcopy(schema)

    def visit(node):
        if isinstance(node, dict):
            props = node.get("properties", {})
            if "code_span" in props:
                props["code_span"] = {
                    "anyOf": [{"type": "string", "pattern": SPAN_PATTERN}, {"type": "null"}],
                    "default": None,
                    "description": "Code line numbers only, 1-based inclusive; never code text. Use null if uncertain.",
                    "examples": ["L3", "L3-L5", None],
                }
            if "quote" in props and "source_field" in props:
                props["quote"]["description"] = (
                    "Verbatim local evidence from the specified source field. "
                    "Put code text here when source_field is code, not in code_span."
                )
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(result)
    return result


def validate_location_format(judgment, item) -> None:
    """Enforce the displayed schema, including top-level spans and quote bounds."""
    assessment = getattr(judgment, "assessment", judgment)
    spans = [assessment.code_span]
    locations = [assessment.first_faulty_location]
    locations += [check.claim for check in getattr(judgment, "checks", [])]
    for location in locations:
        if location is not None:
            location.validate_against(item.solution_trace)
            spans.append(location.code_span)
    line_count = len(item.solution_trace.code.splitlines())
    for span in spans:
        if span is None:
            continue
        if not re.fullmatch(SPAN_PATTERN, span):
            raise ValueError("code_span must be L3, L3-L5, or null; put code text in quote")
        bounds = [int(part[1:]) for part in span.split("-")]
        if not 1 <= bounds[0] <= bounds[-1] <= line_count:
            raise ValueError("code_span must be ordered and within solution_trace.code")
