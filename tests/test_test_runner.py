from __future__ import annotations

from tracejudge_hy3.sandbox.test_runner import build_execution_summary
from tracejudge_hy3.sandbox.trusted_local import TrustedLocalSandbox
from tracejudge_hy3.schemas.problem import TestCase as CaseSpec

SAFE_MEAN_FAULTY = """
def safe_mean(nums):
    return sum(nums) / len(nums)
"""

SAFE_MEAN_CORRECT = """
def safe_mean(nums):
    if not nums:
        return 0.0
    return sum(nums) / len(nums)
"""

SLOW_CODE = """
def slow(x):
    while True:
        pass
"""

SYNTAX_ERROR_CODE = "def broken(:\n    pass"


def _backend(timeout: float = 2.0) -> TrustedLocalSandbox:
    return TrustedLocalSandbox(per_test_timeout_seconds=timeout)


def test_visible_pass_hidden_fail():
    backend = _backend()
    visible = CaseSpec(case_id="v1", args=[[1, 2, 3]], expected=2.0, category="visible")
    hidden = CaseSpec(case_id="h1", args=[[]], expected=0.0, category="hidden")

    summary = backend.run(SAFE_MEAN_FAULTY, "safe_mean", [visible, hidden])

    assert summary.runtime_status == "completed"
    v_result = next(r for r in summary.results if r.case_id == "v1")
    h_result = next(r for r in summary.results if r.case_id == "h1")
    assert v_result.passed is True
    assert h_result.passed is False
    assert h_result.exception_type == "ZeroDivisionError"


def test_correct_code_passes_all():
    backend = _backend()
    visible = CaseSpec(case_id="v1", args=[[1, 2, 3]], expected=2.0, category="visible")
    hidden = CaseSpec(case_id="h1", args=[[]], expected=0.0, category="hidden")
    summary = backend.run(SAFE_MEAN_CORRECT, "safe_mean", [visible, hidden])
    assert summary.all_passed()


def test_runtime_exception_captured_independently():
    backend = _backend()
    ok = CaseSpec(case_id="ok", args=[[1, 2]], expected=1.5, category="visible")
    bad = CaseSpec(case_id="bad", args=[[]], expected=0.0, category="hidden")
    summary = backend.run(SAFE_MEAN_FAULTY, "safe_mean", [ok, bad])
    ok_result = next(r for r in summary.results if r.case_id == "ok")
    bad_result = next(r for r in summary.results if r.case_id == "bad")
    assert ok_result.passed
    assert not bad_result.passed
    assert bad_result.exception_type == "ZeroDivisionError"
    assert bad_result.exception_message


def test_expected_exception_counts_as_pass():
    backend = _backend()
    code = "def reject(value):\n    raise ValueError('invalid value')\n"
    tc = CaseSpec(
        case_id="raises",
        args=[1],
        expected={"raises": "ValueError"},
        category="challenge",
    )
    summary = backend.run(code, "reject", [tc])
    result = summary.results[0]
    assert result.passed is True
    assert result.exception_type == "ValueError"


def test_expected_exception_rejects_wrong_exception_type():
    backend = _backend()
    code = "def reject(value):\n    raise TypeError('invalid value')\n"
    tc = CaseSpec(
        case_id="raises",
        args=[1],
        expected={"raises": "ValueError"},
        category="challenge",
    )
    summary = backend.run(code, "reject", [tc])
    result = summary.results[0]
    assert result.passed is False
    assert result.exception_type == "TypeError"


def test_timeout_result_structure():
    backend = _backend(timeout=1.0)
    tc = CaseSpec(case_id="t1", args=[1], expected=None, category="visible")
    summary = backend.run(SLOW_CODE, "slow", [tc])
    assert summary.runtime_status == "completed"
    result = summary.results[0]
    assert result.timed_out is True
    assert result.passed is False
    assert result.exception_type == "TimeoutError"


def test_candidate_output_without_newline_does_not_corrupt_runner_protocol():
    backend = _backend()
    code = "def echo(value):\n    print('debug', end='')\n    return value\n"
    tc = CaseSpec(case_id="printed", args=[7], expected=7, category="visible")

    summary = backend.run(code, "echo", [tc])

    assert summary.runtime_status == "completed"
    assert summary.results[0].passed is True
    assert summary.results[0].stdout == "debug"
    assert summary.results[0].exit_code == 0


def test_candidate_stderr_is_captured_without_corrupting_results():
    backend = _backend()
    code = "def echo(value):\n    import sys\n    print('warning', file=sys.stderr)\n    return value\n"
    tc = CaseSpec(case_id="stderr", args=[7], expected=7, category="visible")

    summary = backend.run(code, "echo", [tc])

    assert summary.results[0].passed is True
    assert summary.results[0].stderr == "warning\n"


def test_each_case_gets_fresh_candidate_module_state():
    backend = _backend()
    code = """
counter = 0

def next_count():
    global counter
    counter += 1
    return counter
"""
    cases = [
        CaseSpec(case_id="first", expected=1, category="visible"),
        CaseSpec(case_id="second", expected=1, category="hidden"),
    ]

    summary = backend.run(code, "next_count", cases)

    assert summary.runtime_status == "completed"
    assert summary.all_passed()


def test_timed_out_case_is_structured_and_does_not_block_later_cases():
    backend = _backend(timeout=0.25)
    code = """
def maybe_slow(value):
    if value == "hang":
        while True:
            pass
    return value
"""
    cases = [
        CaseSpec(case_id="hang", args=["hang"], expected=None, category="visible"),
        CaseSpec(case_id="later", args=["ok"], expected="ok", category="hidden"),
    ]

    summary = backend.run(code, "maybe_slow", cases)

    assert summary.runtime_status == "completed"
    timed_out, later = summary.results
    assert timed_out.passed is False
    assert timed_out.timed_out is True
    assert timed_out.exception_type == "TimeoutError"
    assert later.passed is True


def test_syntax_error_reported_as_backend_status():
    backend = _backend()
    tc = CaseSpec(case_id="t1", args=[1], expected=1, category="visible")
    summary = backend.run(SYNTAX_ERROR_CODE, "broken", [tc])
    assert summary.runtime_status == "syntax_error"
    assert summary.setup_error is not None
    assert summary.results == []


def test_missing_function_reported_as_import_error():
    backend = _backend()
    tc = CaseSpec(case_id="t1", args=[1], expected=1, category="visible")
    summary = backend.run("def other():\n    return 1\n", "safe_mean", [tc])
    assert summary.runtime_status == "import_error"


def test_empty_test_case_list_returns_completed_with_no_results():
    backend = _backend()
    summary = backend.run(SAFE_MEAN_CORRECT, "safe_mean", [])
    assert summary.runtime_status == "completed"
    assert summary.results == []


def test_malformed_backend_case_is_reported_without_crashing():
    tc = CaseSpec(case_id="bad", args=[1], expected=1, category="visible")
    summary = build_execution_summary(
        "identity",
        "fake",
        {
            "runtime_status": "completed",
            "setup_error": None,
            "cases": [{"case_id": "bad", "output": 1}],
        },
        [tc],
    )
    assert summary.results[0].passed is False
    assert summary.results[0].exception_type == "MalformedResult"


def test_malformed_top_level_backend_payload_is_reported_without_crashing():
    tc = CaseSpec(case_id="bad", args=[1], expected=1, category="visible")

    summary = build_execution_summary("identity", "fake", ["not", "an", "object"], [tc])

    assert summary.runtime_status == "backend_error"
    assert summary.setup_error == "sandbox runner returned a non-object payload"
