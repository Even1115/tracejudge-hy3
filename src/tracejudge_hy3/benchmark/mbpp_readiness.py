"""Versioned pre-API gate for the pinned EvalPlus MBPP+ smoke checks.

Per-call time_limit exceptions become failed tests in the official evaluator.
Only an unfinished evaluation child receives the distinct timeout status.
Never relabel a real candidate's result using the intent of a smoke fixture.
"""

MBPP_SMOKE_SCHEMA = "mbpp-real-smoke-v2"
MBPP_SMOKE_EXPECTATIONS = {
    "pass": {"base_status": ["pass"], "plus_status": ["pass"]},
    "wrong_answer": {"base_status": ["fail"], "plus_status": ["fail"]},
    "call_timeout": {"base_status": ["fail"], "plus_status": ["fail", "timeout"]},
    "process_timeout": {"base_status": ["timeout"], "plus_status": ["timeout"]},
}


def mbpp_smoke_case_ok(name, case):
    expected = MBPP_SMOKE_EXPECTATIONS[name]
    actual = case.get("actual")
    return (
        case.get("expected") == expected
        and "infrastructure_error_type" in case
        and case["infrastructure_error_type"] is None
        and isinstance(actual, dict)
        and set(actual) == set(expected)
        and all(actual[key] in allowed for key, allowed in expected.items())
    )


def validate_mbpp_readiness(receipt, *, image, execution_source):
    if (
        receipt.get("schema") != MBPP_SMOKE_SCHEMA
        or receipt.get("ready") is not True
        or receipt.get("uses_model_api") is not False
        or receipt.get("image") != image
        or receipt.get("execution_source_sha256") != execution_source
    ):
        raise ValueError("MBPP real smoke receipt version/image/source mismatch or not ready")
    cases = receipt.get("cases")
    if not isinstance(cases, dict) or set(cases) != set(MBPP_SMOKE_EXPECTATIONS):
        raise ValueError("MBPP real smoke receipt coverage mismatch")
    for name, case in cases.items():
        if not isinstance(case, dict) or not mbpp_smoke_case_ok(name, case):
            raise ValueError("MBPP real smoke case failed; ready flag is not sufficient")
