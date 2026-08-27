from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock

from tracejudge_hy3.config import Settings
from tracejudge_hy3.prompts.solver import build_solver_user_prompt
from tracejudge_hy3.providers.hy3_openai import Hy3OpenAIProvider
from tracejudge_hy3.schemas.problem import ProblemSpec, RequirementItem
from tracejudge_hy3.schemas.problem import TestCase as ProblemTestCase
from tracejudge_hy3.schemas.solution import ImplementationStep, SolutionTrace


def _canary_problem() -> ProblemSpec:
    return ProblemSpec(
        problem_id="public_problem_id",
        title="TITLE_PRIVATE_CANARY_9f21",
        requirement="PUBLIC_DESCRIPTION_返回输入的绝对值",
        function_signature="def public_abs(value: int) -> int:",
        requirements=[
            RequirementItem(
                requirement_id="R_PUBLIC",
                content="PUBLIC_CLAUSE_负数转为正数",
                verification_hint="VERIFICATION_HINT_CANARY_9f21",
            )
        ],
        visible_test_cases=[
            ProblemTestCase(
                case_id="visible-case",
                args=["VISIBLE_INPUT_CANARY_可见"],
                expected="VISIBLE_EXPECTED_CANARY_可见",
                category="visible",
                related_requirements=["R_PUBLIC"],
            )
        ],
        hidden_test_cases=[
            ProblemTestCase(
                case_id="HIDDEN_CASE_ID_CANARY_9f21",
                args=["HIDDEN_INPUT_CANARY_9f21"],
                expected="HIDDEN_EXPECTED_CANARY_9f21",
                category="hidden",
                related_requirements=["R_PUBLIC"],
            )
        ],
        challenge_test_cases=[
            ProblemTestCase(
                case_id="CHALLENGE_CASE_ID_CANARY_9f21",
                kwargs={"value": "CHALLENGE_INPUT_CANARY_9f21"},
                expected="CHALLENGE_EXPECTED_CANARY_9f21",
                category="challenge",
                related_requirements=["R_PUBLIC"],
            )
        ],
        reference_code="REFERENCE_CODE_CANARY_9f21",
        difficulty="easy",
        source="SOURCE_CANARY_9f21",
        tags=["TAG_CANARY_9f21"],
    )


def _valid_solution(problem: ProblemSpec) -> SolutionTrace:
    return SolutionTrace(
        problem_id=problem.problem_id,
        requirement_understanding="对公开需求的可审查概括",
        design_summary="使用简单分支",
        edge_cases_considered=["零"],
        implementation_steps=[
            ImplementationStep(
                step_id="S1",
                content="返回数值的绝对值",
                related_requirements=["R_PUBLIC"],
                expected_code_behavior="输出非负整数",
            )
        ],
        declared_time_complexity="O(1)",
        declared_space_complexity="O(1)",
        code="def public_abs(value: int) -> int:\n    return abs(value)\n",
    )


def _settings(**overrides) -> Settings:
    values = {
        "hy3_base_url": "https://hy3.invalid/v1",
        "hy3_api_key": "unused-test-key",
        "hy3_model": "test-model",
        "hy3_max_retries": 1,
    }
    values.update(overrides)
    return Settings(
        _env_file=None,
        **values,
    )


def _extract_user_payload(prompt: str) -> dict:
    prefix = "请为以下题目生成结构化解答：\n\n"
    suffix = "\n\n请严格按照系统提示中的 JSON Schema 输出结果。"
    assert prompt.startswith(prefix)
    assert prompt.endswith(suffix)
    return json.loads(prompt.removeprefix(prefix).removesuffix(suffix))


def test_solver_user_prompt_has_an_exact_public_data_allowlist():
    problem = _canary_problem()

    payload = _extract_user_payload(build_solver_user_prompt(problem))

    assert payload == {
        "problem_id": "public_problem_id",
        "requirement": "PUBLIC_DESCRIPTION_返回输入的绝对值",
        "function_signature": "def public_abs(value: int) -> int:",
        "requirements": [
            {
                "requirement_id": "R_PUBLIC",
                "content": "PUBLIC_CLAUSE_负数转为正数",
            }
        ],
        "visible_test_cases": [
            {
                "args": ["VISIBLE_INPUT_CANARY_可见"],
                "kwargs": {},
                "expected": "VISIBLE_EXPECTED_CANARY_可见",
            }
        ],
    }


async def test_all_initial_and_repair_messages_exclude_private_dataset_canaries(monkeypatch):
    problem = _canary_problem()
    monkeypatch.setattr(
        "tracejudge_hy3.providers.hy3_openai.openai.AsyncOpenAI",
        lambda **kwargs: object(),
    )
    provider = Hy3OpenAIProvider(_settings())
    provider._call_model = AsyncMock(
        side_effect=["not valid JSON", _valid_solution(problem).model_dump_json()]
    )

    result = await provider.generate_solution_with_details(problem)

    assert result.status == "success"
    assert result.attempt_count == 2
    assert result.attempt_outcomes == ("parse_error", "success")
    assert result.retry_count == 1
    assert provider._call_model.await_count == 2

    all_outbound_messages = [
        message for call in provider._call_model.await_args_list for message in call.args[0]
    ]
    serialized = json.dumps(all_outbound_messages, ensure_ascii=False)
    private_canaries = {
        "TITLE_PRIVATE_CANARY_9f21",
        "VERIFICATION_HINT_CANARY_9f21",
        "HIDDEN_CASE_ID_CANARY_9f21",
        "HIDDEN_INPUT_CANARY_9f21",
        "HIDDEN_EXPECTED_CANARY_9f21",
        "CHALLENGE_CASE_ID_CANARY_9f21",
        "CHALLENGE_INPUT_CANARY_9f21",
        "CHALLENGE_EXPECTED_CANARY_9f21",
        "REFERENCE_CODE_CANARY_9f21",
        "SOURCE_CANARY_9f21",
        "TAG_CANARY_9f21",
    }
    assert not [canary for canary in private_canaries if canary in serialized]
    for forbidden_field_name in (
        "reference_code",
        "hidden_test_cases",
        "challenge_test_cases",
        "verification_hint",
    ):
        assert forbidden_field_name not in serialized

    initial_messages = provider._call_model.await_args_list[0].args[0]
    initial_serialized = json.dumps(initial_messages, ensure_ascii=False)
    for public_value in (
        "public_problem_id",
        "PUBLIC_DESCRIPTION_返回输入的绝对值",
        "def public_abs(value: int) -> int:",
        "R_PUBLIC",
        "PUBLIC_CLAUSE_负数转为正数",
        "VISIBLE_INPUT_CANARY_可见",
        "VISIBLE_EXPECTED_CANARY_可见",
    ):
        assert public_value in initial_serialized

    repair_messages = provider._call_model.await_args_list[1].args[0]
    assert repair_messages[-2] == {"role": "assistant", "content": "not valid JSON"}
    assert "公开上下文校验" in repair_messages[-1]["content"]


async def test_hy3_redacts_configured_key_from_raw_parsed_errors_and_logs(
    monkeypatch,
    caplog,
):
    configured_key = "NAKED_CONFIGURED_KEY_CANARY_7f31"
    problem = _canary_problem()
    monkeypatch.setattr(
        "tracejudge_hy3.providers.hy3_openai.openai.AsyncOpenAI",
        lambda **kwargs: object(),
    )
    provider = Hy3OpenAIProvider(_settings(hy3_api_key=configured_key, hy3_max_retries=0))
    leaked_solution = _valid_solution(problem).model_copy(
        update={"requirement_understanding": f"summary {configured_key}"}
    )
    provider._call_model = AsyncMock(return_value=leaked_solution.model_dump_json())

    success = await provider.generate_solution_with_details(problem)

    assert success.status == "success"
    assert success.attempt_outcomes == ("success",)
    assert configured_key not in (success.raw_output or "")
    assert success.solution is not None
    assert configured_key not in success.solution.model_dump_json()

    provider._call_model = AsyncMock(
        return_value=json.dumps({"api_key": configured_key, "unexpected": True})
    )
    with caplog.at_level(logging.WARNING):
        failure = await provider.generate_solution_with_details(problem)

    assert failure.status == "parse_error"
    assert failure.attempt_outcomes == ("parse_error",)
    assert configured_key not in (failure.raw_output or "")
    assert failure.error is not None
    assert configured_key not in str(failure.error)
    assert configured_key not in caplog.text


async def test_hy3_repair_and_parse_error_exclude_unconfigured_header_values(monkeypatch):
    header_canary = "OTHER_AUTH_HEADER_CANARY_83cd"
    sensitive_raw = json.dumps({"Authorization": f"Basic {header_canary}", "unexpected": True})
    problem = _canary_problem()
    monkeypatch.setattr(
        "tracejudge_hy3.providers.hy3_openai.openai.AsyncOpenAI",
        lambda **kwargs: object(),
    )
    repairing_provider = Hy3OpenAIProvider(_settings(hy3_max_retries=1))
    repairing_provider._call_model = AsyncMock(
        side_effect=[sensitive_raw, _valid_solution(problem).model_dump_json()]
    )

    repaired = await repairing_provider.generate_solution_with_details(problem)

    assert repaired.status == "success"
    assert repaired.attempt_outcomes == ("parse_error", "success")
    repair_messages = repairing_provider._call_model.await_args_list[1].args[0]
    assert header_canary not in json.dumps(repair_messages, ensure_ascii=False)

    failing_provider = Hy3OpenAIProvider(_settings(hy3_max_retries=0))
    failing_provider._call_model = AsyncMock(return_value=sensitive_raw)
    failed = await failing_provider.generate_solution_with_details(problem)

    assert failed.status == "parse_error"
    assert failed.attempt_outcomes == ("parse_error",)
    assert header_canary not in (failed.raw_output or "")
    assert failed.error is not None
    assert header_canary not in str(failed.error)
    assert "input_value" not in str(failed.error)
