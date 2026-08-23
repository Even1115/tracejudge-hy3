from __future__ import annotations

import pytest

from tracejudge_hy3.exceptions import ParsingError
from tracejudge_hy3.parsing.structured_output import extract_json_text, parse_structured_output
from tracejudge_hy3.schemas.solution import SolutionTrace

VALID_PAYLOAD = """
{
  "problem_id": "safe_mean",
  "requirement_understanding": "u",
  "design_summary": "d",
  "edge_cases_considered": [],
  "implementation_steps": [],
  "declared_time_complexity": "O(n)",
  "declared_space_complexity": "O(1)",
  "code": "def safe_mean(nums):\\n    return sum(nums) / len(nums)\\n"
}
"""


def test_extract_json_text_from_bare_json():
    assert extract_json_text(VALID_PAYLOAD).strip().startswith("{")


def test_extract_json_text_from_markdown_fence():
    wrapped = f"这是我的回答：\n```json\n{VALID_PAYLOAD}\n```\n谢谢。"
    extracted = extract_json_text(wrapped)
    assert extracted.strip().startswith("{")
    assert extracted.strip().endswith("}")


def test_extract_json_text_with_leading_and_trailing_text_no_fence():
    wrapped = f"Sure, here you go:\n{VALID_PAYLOAD}\nHope that helps!"
    extracted = extract_json_text(wrapped)
    assert extracted.strip().startswith("{")


def test_extract_json_text_empty_raises():
    with pytest.raises(ParsingError):
        extract_json_text("   ")


def test_extract_json_text_no_json_raises():
    with pytest.raises(ParsingError):
        extract_json_text("there is no json here at all")


def test_parse_structured_output_valid():
    solution = parse_structured_output(VALID_PAYLOAD, SolutionTrace)
    assert solution.problem_id == "safe_mean"
    assert "return sum(nums)" in solution.code


def test_parse_structured_output_with_fence_and_chatter():
    wrapped = f"当然，这是结果：\n```json\n{VALID_PAYLOAD}\n```"
    solution = parse_structured_output(wrapped, SolutionTrace)
    assert solution.problem_id == "safe_mean"


def test_parse_structured_output_invalid_schema_raises_parsing_error():
    bad = '{"problem_id": "x"}'
    with pytest.raises(ParsingError):
        parse_structured_output(bad, SolutionTrace)


def test_parse_structured_output_invalid_json_raises_parsing_error():
    with pytest.raises(ParsingError):
        parse_structured_output("{not valid json", SolutionTrace)
