from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tracejudge_hy3.config import Settings
from tracejudge_hy3.dataset.loader import load_problem_by_id
from tracejudge_hy3.exceptions import (
    ProviderAuthError,
    ProviderParseError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from tracejudge_hy3.logging_config import redact_secret
from tracejudge_hy3.providers.base import SolutionGeneration
from tracejudge_hy3.providers.hy3_openai import Hy3OpenAIProvider
from tracejudge_hy3.providers.mock import MockProvider
from tracejudge_hy3.schemas.evaluation import ProcessAssessment
from tracejudge_hy3.schemas.execution import ExecutionSummary, StaticEvidence

DATASET = Path(__file__).resolve().parents[1] / "data" / "sample_problems.jsonl"


def _settings(**overrides) -> Settings:
    values = {
        "hy3_base_url": "https://hy3.invalid/v1",
        "hy3_api_key": "test-secret-not-real",
        "hy3_model": "test-model",
        "hy3_max_retries": 1,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class _FakeClient:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("updates", "error_type", "message"),
    [
        (
            {"attempt_count": 2, "attempt_outcomes": ("provider_error",)},
            ValueError,
            "attempt_count must equal",
        ),
        (
            {"attempt_outcomes": ["provider_error"]},
            TypeError,
            "must be a tuple",
        ),
        (
            {"attempt_outcomes": ("not_an_outcome",)},
            ValueError,
            "unsupported outcome",
        ),
        (
            {"status": "success", "attempt_outcomes": ("provider_error",)},
            ValueError,
            "final attempt outcome",
        ),
        (
            {
                "attempt_count": 2,
                "attempt_outcomes": ("success", "provider_error"),
            },
            ValueError,
            "cannot continue after success",
        ),
        (
            {
                "status": "parse_error",
                "attempt_outcomes": ("parse_error",),
                "parse_attempted": True,
            },
            ValueError,
            "must preserve raw output",
        ),
    ],
)
def test_solution_generation_rejects_ambiguous_attempt_history(
    updates,
    error_type,
    message,
):
    values = {
        "status": "provider_error",
        "raw_output": None,
        "solution": None,
        "attempt_count": 1,
        "attempt_outcomes": ("provider_error",),
        **updates,
    }

    with pytest.raises(error_type, match=message):
        SolutionGeneration(**values)


def test_hy3_provider_requires_complete_configuration():
    settings = Settings(
        _env_file=None,
        hy3_base_url=None,
        hy3_api_key=None,
        hy3_model=None,
    )
    with pytest.raises(ProviderAuthError, match="requires HY3_BASE_URL"):
        Hy3OpenAIProvider(settings)


def test_secret_redaction_never_exposes_key_fragments():
    secret = "test-secret-not-real"
    redacted = redact_secret(secret)

    assert redacted == "<configured>"
    assert all(fragment not in redacted for fragment in ("test", "real", secret))


async def test_mock_fixture_read_failure_is_provider_error_without_parse_attempt(
    tmp_path,
    monkeypatch,
):
    missing_fixture = tmp_path / "missing-fixture.json"
    monkeypatch.setattr(
        "tracejudge_hy3.providers.mock._fixture_path",
        lambda _name: missing_fixture,
    )
    problem = load_problem_by_id(DATASET, "safe_mean")

    generation = await MockProvider().generate_solution_with_details(problem)

    assert generation.status == "provider_error"
    assert generation.attempt_outcomes == ("provider_error",)
    assert generation.raw_output is None
    assert generation.raw_output_attempt is None
    assert generation.parse_attempted is False


async def test_hy3_provider_disables_sdk_retries_and_closes(monkeypatch):
    captured: dict = {}
    fake_client = _FakeClient()

    def fake_factory(**kwargs):
        captured.update(kwargs)
        return fake_client

    monkeypatch.setattr(
        "tracejudge_hy3.providers.hy3_openai.openai.AsyncOpenAI",
        fake_factory,
    )
    provider = Hy3OpenAIProvider(_settings())
    assert captured["max_retries"] == 0
    assert captured["api_key"] == "test-secret-not-real"
    public_config = provider.public_generation_config()
    assert public_config["endpoint_sha256"] == hashlib.sha256(b"https://hy3.invalid/v1").hexdigest()
    assert "https://hy3.invalid/v1" not in str(public_config)
    assert "test-secret-not-real" not in str(public_config)
    await provider.aclose()
    assert fake_client.closed is True


async def test_hy3_endpoint_fingerprint_strips_userinfo_query_and_fragment(monkeypatch):
    fake_client = _FakeClient()
    monkeypatch.setattr(
        "tracejudge_hy3.providers.hy3_openai.openai.AsyncOpenAI",
        lambda **kwargs: fake_client,
    )
    userinfo_canary = "URL_PASSWORD_CANARY"
    query_canary = "URL_QUERY_TOKEN_CANARY"
    provider = Hy3OpenAIProvider(
        _settings(
            hy3_base_url=(
                f"https://user:{userinfo_canary}@HY3.invalid/v1/"
                f"?api_key={query_canary}#private-fragment"
            )
        )
    )

    config = provider.public_generation_config()

    expected = hashlib.sha256(b"https://hy3.invalid/v1").hexdigest()
    assert config["endpoint_sha256"] == expected
    assert userinfo_canary not in str(config)
    assert query_canary not in str(config)
    await provider.aclose()


async def test_hy3_public_config_and_initial_log_redact_key_even_inside_model_knobs(
    monkeypatch,
    caplog,
):
    configured_key = "CONFIG_KEY_INSIDE_MODEL_CANARY"
    fake_client = _FakeClient()
    monkeypatch.setattr(
        "tracejudge_hy3.providers.hy3_openai.openai.AsyncOpenAI",
        lambda **kwargs: fake_client,
    )
    settings = _settings(
        hy3_api_key=configured_key,
        hy3_model=f"model-{configured_key}",
        hy3_reasoning_effort=f"effort-{configured_key}",
    )

    with caplog.at_level(logging.INFO):
        provider = Hy3OpenAIProvider(settings)

    assert configured_key not in str(provider.public_generation_config())
    assert configured_key not in caplog.text
    await provider.aclose()


async def test_hy3_provider_repairs_invalid_json_once(monkeypatch):
    fake_client = _FakeClient()
    monkeypatch.setattr(
        "tracejudge_hy3.providers.hy3_openai.openai.AsyncOpenAI",
        lambda **kwargs: fake_client,
    )
    provider = Hy3OpenAIProvider(_settings())
    problem = load_problem_by_id(DATASET, "safe_mean")
    valid_solution = await MockProvider(case="correct").generate_solution(problem)
    provider._call_model = AsyncMock(side_effect=["not JSON", valid_solution.model_dump_json()])

    generation = await provider.generate_solution_with_details(problem)

    assert generation.solution == valid_solution
    assert generation.status == "success"
    assert generation.attempt_outcomes == ("parse_error", "success")
    assert generation.attempt_count == len(generation.attempt_outcomes) == 2
    assert generation.retry_count == 1
    assert provider._call_model.await_count == 2
    repaired_messages = provider._call_model.await_args_list[1].args[0]
    assert "未通过 JSON Schema 校验" in repaired_messages[-1]["content"]


async def test_hy3_provider_first_response_success_has_one_success_outcome(monkeypatch):
    fake_client = _FakeClient()
    monkeypatch.setattr(
        "tracejudge_hy3.providers.hy3_openai.openai.AsyncOpenAI",
        lambda **kwargs: fake_client,
    )
    provider = Hy3OpenAIProvider(_settings(hy3_max_retries=2))
    problem = load_problem_by_id(DATASET, "safe_mean")
    valid_solution = await MockProvider(case="correct").generate_solution(problem)
    provider._call_model = AsyncMock(return_value=valid_solution.model_dump_json())

    generation = await provider.generate_solution_with_details(problem)

    assert generation.status == "success"
    assert generation.solution == valid_solution
    assert generation.attempt_outcomes == ("success",)
    assert generation.attempt_count == len(generation.attempt_outcomes) == 1
    assert generation.retry_count == 0
    assert provider._call_model.await_count == 1


async def test_hy3_provider_retries_wrapped_api_failure(monkeypatch):
    fake_client = _FakeClient()
    monkeypatch.setattr(
        "tracejudge_hy3.providers.hy3_openai.openai.AsyncOpenAI",
        lambda **kwargs: fake_client,
    )
    provider = Hy3OpenAIProvider(_settings())
    problem = load_problem_by_id(DATASET, "safe_mean")
    valid_solution = await MockProvider(case="correct").generate_solution(problem)
    provider._call_model = AsyncMock(
        side_effect=[
            ProviderResponseError("temporary API failure"),
            valid_solution.model_dump_json(),
        ]
    )

    generation = await provider.generate_solution_with_details(problem)

    assert generation.solution == valid_solution
    assert generation.status == "success"
    assert generation.attempt_outcomes == ("provider_error", "success")
    assert provider._call_model.await_count == 2
    first_messages = provider._call_model.await_args_list[0].args[0]
    retry_messages = provider._call_model.await_args_list[1].args[0]
    assert retry_messages == first_messages
    assert all("未通过 JSON Schema" not in message["content"] for message in retry_messages)


async def test_hy3_provider_timeout_exhaustion_uses_custom_error(monkeypatch):
    fake_client = _FakeClient()
    monkeypatch.setattr(
        "tracejudge_hy3.providers.hy3_openai.openai.AsyncOpenAI",
        lambda **kwargs: fake_client,
    )
    provider = Hy3OpenAIProvider(_settings(hy3_max_retries=1))
    provider._call_model = AsyncMock(
        side_effect=[ProviderTimeoutError("slow"), ProviderTimeoutError("still slow")]
    )
    problem = load_problem_by_id(DATASET, "safe_mean")

    with pytest.raises(ProviderTimeoutError, match="2 attempt"):
        await provider.generate_solution(problem)
    assert provider._call_model.await_count == 2


async def test_hy3_provider_parse_exhaustion_records_terminal_attempt_without_phantom_repair(
    monkeypatch,
):
    fake_client = _FakeClient()
    monkeypatch.setattr(
        "tracejudge_hy3.providers.hy3_openai.openai.AsyncOpenAI",
        lambda **kwargs: fake_client,
    )
    provider = Hy3OpenAIProvider(_settings(hy3_max_retries=1))
    provider._call_model = AsyncMock(side_effect=["not JSON", "still not JSON"])
    problem = load_problem_by_id(DATASET, "safe_mean")

    generation = await provider.generate_solution_with_details(problem)

    assert generation.status == "parse_error"
    assert generation.attempt_outcomes == ("parse_error", "parse_error")
    assert generation.attempt_count == len(generation.attempt_outcomes) == 2
    assert isinstance(generation.error, ProviderParseError)
    assert provider._call_model.await_count == 2
    sent_messages = provider._call_model.await_args_list
    assert "未通过 JSON Schema" not in sent_messages[0].args[0][-1]["content"]
    assert "未通过 JSON Schema" in sent_messages[1].args[0][-1]["content"]


async def test_hy3_provider_final_parse_error_does_not_send_unscheduled_repair(monkeypatch):
    fake_client = _FakeClient()
    monkeypatch.setattr(
        "tracejudge_hy3.providers.hy3_openai.openai.AsyncOpenAI",
        lambda **kwargs: fake_client,
    )
    provider = Hy3OpenAIProvider(_settings(hy3_max_retries=0))
    provider._call_model = AsyncMock(return_value="not JSON")
    problem = load_problem_by_id(DATASET, "safe_mean")

    generation = await provider.generate_solution_with_details(problem)

    assert generation.status == "parse_error"
    assert generation.attempt_outcomes == ("parse_error",)
    assert generation.attempt_count == 1
    assert generation.retry_count == 0
    assert provider._call_model.await_count == 1


async def test_hy3_mixed_parse_then_timeout_preserves_raw_attempt_metadata(monkeypatch):
    fake_client = _FakeClient()
    monkeypatch.setattr(
        "tracejudge_hy3.providers.hy3_openai.openai.AsyncOpenAI",
        lambda **kwargs: fake_client,
    )
    provider = Hy3OpenAIProvider(_settings(hy3_max_retries=1))
    provider._call_model = AsyncMock(
        side_effect=["not JSON", ProviderTimeoutError("second attempt timed out")]
    )
    problem = load_problem_by_id(DATASET, "safe_mean")

    generation = await provider.generate_solution_with_details(problem)

    assert generation.status == "provider_error"
    assert generation.raw_output == "not JSON"
    assert generation.raw_output_attempt == 1
    assert generation.parse_attempted is True
    assert generation.attempt_count == 2
    assert generation.retry_count == 1
    assert generation.attempt_outcomes == ("parse_error", "provider_error")
    assert isinstance(generation.error, ProviderTimeoutError)
    repair_messages = provider._call_model.await_args_list[1].args[0]
    assert "未通过 JSON Schema" in repair_messages[-1]["content"]


async def test_hy3_auth_failure_records_one_provider_error_and_never_retries(monkeypatch):
    fake_client = _FakeClient()
    monkeypatch.setattr(
        "tracejudge_hy3.providers.hy3_openai.openai.AsyncOpenAI",
        lambda **kwargs: fake_client,
    )
    provider = Hy3OpenAIProvider(_settings(hy3_max_retries=2))
    provider._call_model = AsyncMock(side_effect=ProviderAuthError("credentials rejected"))
    problem = load_problem_by_id(DATASET, "safe_mean")

    generation = await provider.generate_solution_with_details(problem)

    assert generation.status == "provider_error"
    assert generation.attempt_outcomes == ("provider_error",)
    assert generation.attempt_count == 1
    assert generation.retry_count == 0
    assert generation.raw_output is None
    assert generation.raw_output_attempt is None
    assert generation.parse_attempted is False
    assert isinstance(generation.error, ProviderAuthError)
    assert provider._call_model.await_count == 1


@pytest.mark.parametrize(
    ("invalid_update", "unknown_id"),
    [
        ({"first_faulty_step": "S999"}, "S999"),
        ({"affected_steps": ["S1", "S999"]}, "S999"),
        ({"violated_requirement": "R999"}, "R999"),
    ],
)
async def test_hy3_evaluator_repairs_unknown_context_references(
    monkeypatch,
    invalid_update,
    unknown_id,
):
    fake_client = _FakeClient()
    monkeypatch.setattr(
        "tracejudge_hy3.providers.hy3_openai.openai.AsyncOpenAI",
        lambda **kwargs: fake_client,
    )
    provider = Hy3OpenAIProvider(_settings())
    problem = load_problem_by_id(DATASET, "safe_mean")
    solution = await MockProvider(case="correct").generate_solution(problem)
    static_evidence = StaticEvidence(function_name=problem.function_name)
    execution_result = ExecutionSummary(
        problem_id=problem.problem_id,
        function_name=problem.function_name,
        sandbox_backend="test",
    )
    valid_assessment = ProcessAssessment(
        functional_correct=True,
        process_correct=True,
        explanation="all checks passed",
    )
    invalid_assessment = valid_assessment.model_copy(update=invalid_update)
    provider._call_model = AsyncMock(
        side_effect=[
            invalid_assessment.model_dump_json(),
            valid_assessment.model_dump_json(),
        ]
    )

    result = await provider.evaluate_process(
        problem,
        solution,
        static_evidence,
        execution_result,
    )

    assert result == valid_assessment
    assert provider._call_model.await_count == 2
    repair_messages = provider._call_model.await_args_list[1].args[0]
    assert unknown_id in repair_messages[-1]["content"]
