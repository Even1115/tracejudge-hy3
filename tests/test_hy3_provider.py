from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tracejudge_hy3.config import Settings
from tracejudge_hy3.dataset.loader import load_problem_by_id
from tracejudge_hy3.exceptions import (
    ProviderAuthError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from tracejudge_hy3.logging_config import redact_secret
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
    await provider.aclose()
    assert fake_client.closed is True


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

    result = await provider.generate_solution(problem)

    assert result == valid_solution
    assert provider._call_model.await_count == 2
    repaired_messages = provider._call_model.await_args_list[1].args[0]
    assert "未通过 JSON Schema 校验" in repaired_messages[-1]["content"]


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

    result = await provider.generate_solution(problem)

    assert result == valid_solution
    assert provider._call_model.await_count == 2


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
