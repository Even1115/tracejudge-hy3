from __future__ import annotations

import hashlib
import json
import logging
import socket
from pathlib import Path
from unittest.mock import AsyncMock

import openai
import pytest
from openai import _base_client

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


@pytest.fixture
async def sdk_mock_provider(monkeypatch):
    """Exercise the installed SDK's HTTP decoding with no real network access."""

    def deny_network(*_args, **_kwargs):
        raise AssertionError("real network access is forbidden in this test")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    # OpenAI 3.x uses httpx2; earlier supported SDK versions use httpx.
    sdk_httpx = getattr(_base_client, "httpx2", None) or _base_client.httpx
    real_client_factory = openai.AsyncOpenAI
    providers = []

    def make_provider(response_bodies: list[bytes], **overrides):
        requests = []

        def handler(request):
            assert request.url.host == "hy3.invalid"
            assert request.method == "POST"
            requests.append(json.loads(request.content))
            assert len(requests) <= len(response_bodies), "unexpected extra SDK request"
            return sdk_httpx.Response(
                200,
                content=response_bodies[len(requests) - 1],
                headers={"content-type": "application/json"},
            )

        def client_factory(**kwargs):
            return real_client_factory(
                **kwargs,
                http_client=sdk_httpx.AsyncClient(transport=sdk_httpx.MockTransport(handler)),
            )

        monkeypatch.setattr(openai, "AsyncOpenAI", client_factory)
        settings = {
            "hy3_timeout_seconds": 120,
            "hy3_max_retries": 2,
            "hy3_max_parse_repairs": 1,
            "hy3_enable_reasoning_effort": False,
            **overrides,
        }
        provider = Hy3OpenAIProvider(_settings(**settings))
        providers.append(provider)
        return provider, requests

    yield make_provider
    for provider in providers:
        await provider.aclose()


def _sdk_completion_body(content: str) -> bytes:
    return json.dumps(
        {
            "id": "offline-completion",
            "object": "chat.completion",
            "created": 1,
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
        }
    ).encode()


@pytest.mark.parametrize(
    "invalid_body",
    [b"", b'{"RESPONSE_BODY_CANARY":'],
    ids=["empty", "truncated"],
)
async def test_hy3_sdk_http_json_failure_retries_then_succeeds(sdk_mock_provider, invalid_body):
    problem = load_problem_by_id(DATASET, "safe_mean")
    valid_solution = await MockProvider(case="correct").generate_solution(problem)
    provider, requests = sdk_mock_provider(
        [invalid_body, _sdk_completion_body(valid_solution.model_dump_json())]
    )

    generation = await provider.generate_solution_with_details(problem)

    assert generation.status == "success"
    assert generation.solution == valid_solution
    assert generation.attempt_outcomes == ("provider_error", "success")
    assert generation.attempt_count == len(requests) == 2
    assert generation.retry_count == 1
    assert generation.raw_output_attempt == 2
    assert requests[0]["messages"] == requests[1]["messages"]


@pytest.mark.parametrize(
    "invalid_body",
    [b"", b'{"RESPONSE_BODY_CANARY":'],
    ids=["empty", "truncated"],
)
async def test_hy3_sdk_http_json_exhaustion_is_bounded_and_safe(
    sdk_mock_provider, invalid_body, caplog
):
    provider, requests = sdk_mock_provider([invalid_body] * 3)
    problem = load_problem_by_id(DATASET, "safe_mean")

    generation = await provider.generate_solution_with_details(problem)

    assert generation.status == "provider_error"
    assert generation.attempt_outcomes == ("provider_error",) * 3
    assert generation.attempt_count == len(requests) == 3
    assert generation.retry_count == 2
    assert generation.raw_output is None
    assert generation.raw_output_attempt is None
    assert generation.parse_attempted is False
    assert isinstance(generation.error, ProviderResponseError)
    assert str(generation.error) == (
        "Hy3 API request failed after 3 attempt(s): Hy3 API returned invalid JSON"
    )
    assert "RESPONSE_BODY_CANARY" not in caplog.text
    assert "test-secret-not-real" not in caplog.text
    assert all(request["messages"] == requests[0]["messages"] for request in requests)


@pytest.mark.parametrize("repair_budget", [0, 1])
async def test_hy3_sdk_model_json_error_keeps_parse_repair_budget(sdk_mock_provider, repair_budget):
    invalid_content = "synthetic non-JSON model content"
    provider, requests = sdk_mock_provider(
        [_sdk_completion_body(invalid_content)] * (repair_budget + 1),
        hy3_max_parse_repairs=repair_budget,
    )
    problem = load_problem_by_id(DATASET, "safe_mean")

    generation = await provider.generate_solution_with_details(problem)

    assert generation.status == "parse_error"
    assert isinstance(generation.error, ProviderParseError)
    assert generation.attempt_outcomes == ("parse_error",) * (repair_budget + 1)
    assert generation.attempt_count == len(requests) == repair_budget + 1
    assert generation.raw_output == invalid_content
    assert generation.parse_attempted is True
    assert len(requests[0]["messages"]) == 2
    if repair_budget:
        assert len(requests[1]["messages"]) == 4


async def test_hy3_sdk_http_json_error_does_not_consume_parse_repair(sdk_mock_provider):
    problem = load_problem_by_id(DATASET, "safe_mean")
    valid_solution = await MockProvider(case="correct").generate_solution(problem)
    provider, requests = sdk_mock_provider(
        [
            b"",
            _sdk_completion_body("synthetic non-JSON model content"),
            _sdk_completion_body(valid_solution.model_dump_json()),
        ]
    )

    generation = await provider.generate_solution_with_details(problem)

    assert generation.status == "success"
    assert generation.attempt_outcomes == ("provider_error", "parse_error", "success")
    assert generation.attempt_count == len(requests) == 3
    assert [len(request["messages"]) for request in requests] == [2, 2, 4]


async def test_hy3_sdk_http_json_error_does_not_reset_parse_repair(sdk_mock_provider):
    invalid_content = "synthetic non-JSON model content"
    provider, requests = sdk_mock_provider(
        [_sdk_completion_body(invalid_content), b"", _sdk_completion_body(invalid_content)],
        hy3_max_retries=3,
    )
    problem = load_problem_by_id(DATASET, "safe_mean")

    generation = await provider.generate_solution_with_details(problem)

    assert generation.status == "parse_error"
    assert generation.attempt_outcomes == ("parse_error", "provider_error", "parse_error")
    assert generation.attempt_count == len(requests) == 3
    assert generation.raw_output_attempt == 3
    assert isinstance(generation.error, ProviderParseError)
    assert [len(request["messages"]) for request in requests] == [2, 4, 4]
    assert requests[1]["messages"] == requests[2]["messages"]


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


async def test_hy3_provider_parse_repair_budget_zero_is_terminal_on_first_parse_error(
    monkeypatch,
):
    fake_client = _FakeClient()
    monkeypatch.setattr(
        "tracejudge_hy3.providers.hy3_openai.openai.AsyncOpenAI",
        lambda **kwargs: fake_client,
    )
    provider = Hy3OpenAIProvider(_settings(hy3_max_retries=2, hy3_max_parse_repairs=0))
    provider._call_model = AsyncMock(return_value="not JSON")
    problem = load_problem_by_id(DATASET, "safe_mean")

    generation = await provider.generate_solution_with_details(problem)

    assert generation.status == "parse_error"
    assert generation.attempt_outcomes == ("parse_error",)
    assert generation.attempt_count == 1
    assert provider._call_model.await_count == 1


async def test_hy3_provider_only_consumes_one_parse_repair_despite_extra_retries(
    monkeypatch,
):
    fake_client = _FakeClient()
    monkeypatch.setattr(
        "tracejudge_hy3.providers.hy3_openai.openai.AsyncOpenAI",
        lambda **kwargs: fake_client,
    )
    provider = Hy3OpenAIProvider(_settings(hy3_max_retries=2, hy3_max_parse_repairs=1))
    provider._call_model = AsyncMock(
        side_effect=["not JSON", "still not JSON", "should not be called"]
    )
    problem = load_problem_by_id(DATASET, "safe_mean")

    generation = await provider.generate_solution_with_details(problem)

    assert generation.status == "parse_error"
    assert generation.attempt_outcomes == ("parse_error", "parse_error")
    assert generation.attempt_count == 2
    assert provider._call_model.await_count == 2


async def test_hy3_provider_can_repair_after_provider_error_then_parse_error(
    monkeypatch,
):
    fake_client = _FakeClient()
    monkeypatch.setattr(
        "tracejudge_hy3.providers.hy3_openai.openai.AsyncOpenAI",
        lambda **kwargs: fake_client,
    )
    provider = Hy3OpenAIProvider(_settings(hy3_max_retries=2, hy3_max_parse_repairs=1))
    problem = load_problem_by_id(DATASET, "safe_mean")
    valid_solution = await MockProvider(case="correct").generate_solution(problem)
    provider._call_model = AsyncMock(
        side_effect=[
            ProviderResponseError("temporary API failure"),
            "not JSON",
            valid_solution.model_dump_json(),
        ]
    )

    generation = await provider.generate_solution_with_details(problem)

    assert generation.status == "success"
    assert generation.attempt_outcomes == ("provider_error", "parse_error", "success")
    assert generation.attempt_count == 3
    assert provider._call_model.await_count == 3
    repair_messages = provider._call_model.await_args_list[2].args[0]
    assert "未通过 JSON Schema" in repair_messages[-1]["content"]


async def test_hy3_provider_keeps_repair_budget_across_provider_errors(monkeypatch):
    fake_client = _FakeClient()
    monkeypatch.setattr(
        "tracejudge_hy3.providers.hy3_openai.openai.AsyncOpenAI",
        lambda **kwargs: fake_client,
    )
    provider = Hy3OpenAIProvider(_settings(hy3_max_retries=3, hy3_max_parse_repairs=1))
    problem = load_problem_by_id(DATASET, "safe_mean")
    valid_solution = await MockProvider(case="correct").generate_solution(problem)
    provider._call_model = AsyncMock(
        side_effect=[
            "not JSON",
            ProviderResponseError("first retry failed"),
            ProviderResponseError("second retry failed"),
            valid_solution.model_dump_json(),
        ]
    )

    generation = await provider.generate_solution_with_details(problem)

    assert generation.status == "success"
    assert generation.attempt_outcomes == (
        "parse_error",
        "provider_error",
        "provider_error",
        "success",
    )
    assert generation.attempt_count == 4
    assert provider._call_model.await_count == 4
    # Only the first parse error triggered one repair prompt; later provider
    # errors did not reset the budget or add further repair turns.
    message_lengths = [len(call.args[0]) for call in provider._call_model.await_args_list]
    assert message_lengths[0] == 2  # system + user
    assert all(length == 4 for length in message_lengths[1:])  # one repair turn appended


async def test_hy3_public_config_includes_parse_repair_limit(monkeypatch):
    fake_client = _FakeClient()
    monkeypatch.setattr(
        "tracejudge_hy3.providers.hy3_openai.openai.AsyncOpenAI",
        lambda **kwargs: fake_client,
    )
    provider = Hy3OpenAIProvider(_settings(hy3_max_parse_repairs=1))

    config = provider.public_generation_config()

    assert config["max_parse_repairs"] == 1
    assert config["max_retries"] == 1
    await provider.aclose()


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
        (
            {
                "process_correct": False,
                "first_faulty_layer": "reasoning",
                "error_type": "P01_ALGORITHM_ERROR",
                "first_faulty_location": {
                    "source_field": "design_summary",
                    "quote": "ABSENT_QUOTE_CANARY",
                },
            },
            "location quote is absent",
        ),
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
    invalid_assessment = valid_assessment.model_dump(mode="json") | invalid_update
    provider._call_model = AsyncMock(
        side_effect=[
            json.dumps(invalid_assessment),
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
