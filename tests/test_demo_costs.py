from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tracejudge_hy3.config import Settings
from tracejudge_hy3.dataset.loader import load_problem_by_id
from tracejudge_hy3.demo_app.costs import DemoCosts, MeteredProvider, load_method_comparison
from tracejudge_hy3.demo_app.runner import run_demo
from tracejudge_hy3.exceptions import ProviderTimeoutError
from tracejudge_hy3.providers.hy3_openai import Hy3OpenAIProvider
from tracejudge_hy3.providers.mock import MockProvider
from tracejudge_hy3.providers.telemetry import observe_request, request_observer
from tracejudge_hy3.schemas.evaluation import ProcessAssessment
from tracejudge_hy3.schemas.execution import ExecutionSummary, StaticEvidence

ROOT = Path(__file__).resolve().parents[1]


def response(text, prompt=10, completion=20):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
    )


def provider():
    return Hy3OpenAIProvider(
        Settings(
            _env_file=None,
            hy3_base_url="https://example.invalid/v1",
            hy3_api_key="private-test-key",
            hy3_model="test-model",
            hy3_max_retries=1,
        )
    )


async def test_real_provider_retry_and_role_accounting_with_fake_sdk(tmp_path, monkeypatch):
    problem = load_problem_by_id(ROOT / "data/sample_problems.jsonl", "safe_mean")
    solution = await MockProvider(case="correct").generate_solution(problem)
    assessment = ProcessAssessment(functional_correct=True, explanation="public test")
    p = provider()
    create = AsyncMock(
        side_effect=[
            TimeoutError("private-test-key"),
            response(solution.model_dump_json()),
            response(assessment.model_dump_json(), 7, 9),
        ]
    )
    monkeypatch.setattr(p._client.chat.completions, "create", create)
    costs = DemoCosts(tmp_path, "hy3")
    wrapped = MeteredProvider(p, costs)
    try:
        generated = await wrapped.generate_solution(problem)
        judged = await wrapped.evaluate_process(
            problem,
            generated,
            StaticEvidence(),
            ExecutionSummary(
                problem_id=problem.problem_id, function_name="safe_mean", sandbox_backend="docker"
            ),
        )
    finally:
        await wrapped.aclose()
    assert judged == assessment
    summary = costs.summary(10, 1)
    solver, judge = summary["roles"]["solver"], summary["roles"]["judge"]
    assert (solver["calls"], solver["retries"], solver["failed_requests"]) == (2, 1, 1)
    assert solver["input_tokens"] is None  # timeout usage is unknown, not zero
    assert (solver["known_input_tokens"], solver["input_tokens_coverage"]) == (10, 1)
    assert (judge["calls"], judge["retries"], judge["input_tokens"], judge["output_tokens"]) == (
        1,
        0,
        7,
        9,
    )
    assert create.await_count == 3
    assert request_observer.get() is None
    events = [json.loads(line) for line in costs.path.read_text().splitlines()]
    assert len([e for e in events if e["event"] == "request"]) == 3
    assert "private-test-key" not in costs.path.read_text()
    assert "messages" not in json.dumps(summary)


async def test_parse_repair_retains_both_response_usages(tmp_path, monkeypatch):
    problem = load_problem_by_id(ROOT / "data/sample_problems.jsonl", "safe_mean")
    solution = await MockProvider(case="correct").generate_solution(problem)
    p = provider()
    monkeypatch.setattr(
        p._client.chat.completions,
        "create",
        AsyncMock(
            side_effect=[
                response("not JSON", 3, 4),
                response(solution.model_dump_json(), 5, 6),
            ]
        ),
    )
    costs = DemoCosts(tmp_path, "hy3")
    try:
        await MeteredProvider(p, costs).generate_solution(problem)
    finally:
        await p.aclose()
    solver = costs.summary(2, 0)["roles"]["solver"]
    assert (
        solver["calls"],
        solver["retries"],
        solver["input_tokens"],
        solver["output_tokens"],
    ) == (2, 1, 8, 10)


async def test_failed_requests_persist_and_observer_resets(tmp_path, monkeypatch):
    p = provider()
    monkeypatch.setattr(
        p._client.chat.completions, "create", AsyncMock(side_effect=TimeoutError("secret"))
    )
    costs = DemoCosts(tmp_path, "hy3")
    problem = load_problem_by_id(ROOT / "data/sample_problems.jsonl", "safe_mean")
    try:
        with pytest.raises(ProviderTimeoutError):
            await MeteredProvider(p, costs).generate_solution(problem)
    finally:
        await p.aclose()
    assert len(costs.requests) == 2
    assert costs.operations[0]["status"] == "failed"
    assert request_observer.get() is None
    assert costs.path.read_text().count('"event": "request"') == 2


async def test_concurrent_samples_do_not_share_usage(tmp_path):
    async def sample(value):
        costs = DemoCosts(tmp_path, "hy3")
        with costs.operation("solver"):
            await asyncio.sleep(0)
            observe_request(response("unused", value, value), 0.1, None)
        return costs.summary(1, 0)

    results = await asyncio.gather(sample(11), sample(29))
    assert [r["roles"]["solver"]["input_tokens"] for r in results] == [11, 29]


def test_fixture_costs_persist_without_model_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACEJUDGE_ARTIFACT_DIR", str(tmp_path))
    result = run_demo("fixture")
    assert result["ok"]
    costs = result["cost_metrics"]
    assert costs["mode"] == "fixture"
    assert costs["requests"] == []
    assert costs["roles"]["solver"]["calls"] == costs["roles"]["judge"]["input_tokens"] == 0
    assert costs["total_seconds"] >= costs["evaluation_seconds"] >= costs["replay_seconds"]
    journal = tmp_path / "demo_costs" / f"{costs['sample_id']}.jsonl"
    complete = json.loads(journal.read_text().splitlines()[-1])
    assert complete["event"] == "complete" and complete["ok"]
    assert complete["cost_metrics"]["requests"] == costs["requests"]


def test_comparison_uses_published_numbers_and_rejects_incomplete_report(tmp_path):
    data = load_method_comparison(ROOT)
    assert len(data["rows"]) == 5
    full = data["rows"][-1]
    assert (full["correct"], full["known_input_tokens"], full["duration_sum_seconds"]) == (
        55,
        169300,
        1442.9,
    )
    direct = data["rows"][1]
    assert (direct["input_coverage"], direct["total"], direct["provider_failures"]) == (56, 57, 1)
    path = tmp_path / data["source"]
    path.parent.mkdir(parents=True)
    path.write_text("no published rows", encoding="utf-8")
    with pytest.raises(ValueError):
        load_method_comparison(tmp_path)
