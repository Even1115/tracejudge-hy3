from __future__ import annotations

from tracejudge_hy3.static_analysis.ast_analyzer import analyze_code

CORRECT_SAFE_MEAN = """
def safe_mean(nums):
    if not nums:
        return 0.0
    return sum(nums) / len(nums)
"""

FAULTY_SAFE_MEAN = """
def safe_mean(nums):
    return sum(nums) / len(nums)
"""

NESTED_LOOPS = """
def pairs(items):
    result = []
    for i in items:
        for j in items:
            if i != j:
                result.append((i, j))
    return result
"""

USES_SET = """
def dedup(items):
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result
"""


def test_analyze_correct_code_detects_empty_check():
    evidence = analyze_code(CORRECT_SAFE_MEAN, function_name="safe_mean")
    assert evidence.ast_parse_ok
    assert evidence.function_name == "safe_mean"
    assert evidence.function_start_line == 2
    assert evidence.function_end_line == 5
    assert evidence.parameters == ["nums"]
    assert evidence.has_empty_input_check
    assert evidence.empty_input_check_lines
    assert evidence.if_count == 1


def test_analyze_faulty_code_missing_empty_check():
    evidence = analyze_code(FAULTY_SAFE_MEAN, function_name="safe_mean")
    assert evidence.ast_parse_ok
    assert not evidence.has_empty_input_check
    assert evidence.if_count == 0


def test_dead_not_expression_is_not_an_empty_input_control_flow_check():
    code = "def f(items):\n    ignored = not items\n    return len(items)\n"
    evidence = analyze_code(code, function_name="f")
    assert evidence.has_empty_input_check is False


def test_analyze_syntax_error():
    evidence = analyze_code("def broken(:\n    pass", function_name="broken")
    assert not evidence.ast_parse_ok
    assert evidence.ast_parse_error is not None


def test_analyze_nested_loops_depth():
    evidence = analyze_code(NESTED_LOOPS, function_name="pairs")
    assert evidence.loop_count == 2
    assert evidence.for_loop_count == 2
    assert evidence.while_loop_count == 0
    assert evidence.max_loop_nesting_depth == 2


def test_analyze_counts_for_and_while_loops_separately():
    code = """
def consume(groups):
    for group in groups:
        while group:
            group.pop()
"""
    evidence = analyze_code(code, function_name="consume")
    assert evidence.loop_count == 2
    assert evidence.for_loop_count == 1
    assert evidence.while_loop_count == 1
    assert evidence.max_loop_nesting_depth == 2


def test_analyze_counts_only_parameter_controlled_loops_as_input_dependent():
    code = """
async def process(items, stream, remaining):
    for item in items:
        pass
    async for event in stream:
        pass
    while remaining > 0:
        remaining -= 1
    for _ in range(3):
        pass
    while True:
        break
"""
    evidence = analyze_code(code, function_name="process")
    assert evidence.loop_count == 5
    assert evidence.input_dependent_loop_count == 3


def test_fixed_range_loop_is_not_input_dependent():
    code = "def repeat(value):\n    for _ in range(3):\n        value += 1\n    return value\n"
    evidence = analyze_code(code, function_name="repeat")
    assert evidence.loop_count == 1
    assert evidence.input_dependent_loop_count == 0


def test_analyze_collects_comparison_operators():
    code = """
def allowed(value, items, sentinel):
    if 0 < value <= 10 and value not in items:
        return sentinel is not None
    return value == 0 or value != -1
"""
    evidence = analyze_code(code, function_name="allowed")
    assert evidence.comparison_operators == ["<", "<=", "not in", "is not", "==", "!="]


def test_analyze_detects_set_usage():
    evidence = analyze_code(USES_SET, function_name="dedup")
    assert "set" in evidence.data_structures_used


def test_analyze_notable_literals_and_returns():
    code = """
def classify(x):
    if x == 42:
        return "answer"
    return "other"
"""
    evidence = analyze_code(code, function_name="classify")
    assert 42 in evidence.notable_literals
    assert "answer" in evidence.notable_literals
    assert len(evidence.return_statement_lines) == 2


def test_analyze_suspicious_hardcoding_flagged_only_as_heuristic():
    code = """
def lookup(x):
    if x == 7:
        return 99
    return x * 2
"""
    evidence = analyze_code(code, function_name="lookup", visible_test_values=[7, 99])
    assert evidence.suspicious_hardcoding
    assert evidence.suspicious_hardcoding_reason is not None


def test_analyze_no_hardcoding_without_visible_test_overlap():
    code = """
def lookup(x):
    if x == 7:
        return 99
    return x * 2
"""
    evidence = analyze_code(code, function_name="lookup", visible_test_values=[1, 2, 3])
    assert not evidence.suspicious_hardcoding


def test_analyze_flags_multiple_visible_constants_in_branches():
    code = """
def lookup(x):
    if x == 17:
        return 100
    if x == 29:
        return 200
    return x
"""
    evidence = analyze_code(
        code,
        function_name="lookup",
        visible_test_values=[[17], 100, [29], 200],
    )
    assert evidence.suspicious_hardcoding
    assert "multiple branch constants" in evidence.suspicious_hardcoding_reason


def test_nested_function_structure_does_not_pollute_target_evidence():
    code = """
def outer(items):
    def helper(values):
        for value in values:
            if value == 1:
                return value
    return list(items)
"""
    evidence = analyze_code(code, function_name="outer")
    assert evidence.loop_count == 0
    assert evidence.if_count == 0
    assert evidence.comparison_operators == []
    assert evidence.return_statement_lines == [7]


def test_analyze_missing_function_reports_error():
    evidence = analyze_code("x = 1\n", function_name="foo")
    assert not evidence.ast_parse_ok
    assert "no top-level function" in evidence.ast_parse_error


def test_analyze_does_not_fall_back_when_named_function_is_missing():
    code = "def unrelated(value):\n    return value\n"
    evidence = analyze_code(code, function_name="target")
    assert not evidence.ast_parse_ok
    assert evidence.function_name is None
    assert evidence.parameters == []
    assert evidence.ast_parse_error == "no top-level function named 'target' found"
