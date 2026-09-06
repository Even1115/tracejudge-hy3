from __future__ import annotations

import json

import pytest
from test_lcb_docker_runner import PRIVATE_CANARY, _raw_record, _raw_result

from tracejudge_hy3.benchmark.livecodebench import (
    LiveCodeBenchBenchmarkAdapter,
    parse_source_record,
)
from tracejudge_hy3.config import Settings
from tracejudge_hy3.exceptions import ProviderResponseError
from tracejudge_hy3.lcb.experiment import load_events, run_experiment, run_lock
from tracejudge_hy3.lcb.generation import StdioHy3Provider
from tracejudge_hy3.providers.base import SolutionGeneration
from tracejudge_hy3.schemas.solution import SolutionTrace


def solution(task):
    return SolutionTrace(
        problem_id=task.identity.task_id,
        requirement_understanding="Echo input",
        design_summary="Read and print",
        code="print(int(input()))\n",
        implementation_steps=[{"step_id": "S1", "content": "Read stdin and write stdout"}],
    )


class FakeProvider:
    def __init__(self, fail=(), crash=None):
        self.calls = []
        self.fail = set(fail)
        self.crash = crash

    async def generate_task(self, task):
        tid = task.identity.task_id
        self.calls.append(tid)
        if tid == self.crash:
            raise RuntimeError("simulated interruption")
        if tid in self.fail:
            return SolutionGeneration(
                status="provider_error",
                raw_output=None,
                solution=None,
                attempt_count=1,
                attempt_outcomes=("provider_error",),
                error=ProviderResponseError("offline test"),
            )
        value = solution(task)
        return SolutionGeneration(
            status="success",
            raw_output=value.model_dump_json(),
            solution=value,
            attempt_count=1,
            attempt_outcomes=("success",),
            raw_output_attempt=1,
            parse_attempted=True,
        )


class FakeExecutor:
    def __init__(self, infra=(), wrong=()):
        self.calls = []
        self.infra = set(infra)
        self.wrong = set(wrong)

    def run_task(self, *, task, candidate, **kwargs):
        tid = task.identity.task_id
        self.calls.append(tid)
        assert PRIVATE_CANARY in kwargs["private_test_cases_raw"]
        if tid in self.infra:
            return _raw_result(
                task, candidate, infrastructure_status="error", error_type="docker_unavailable"
            )
        return _raw_result(
            task, candidate, official_test_results=[True, -2] if tid in self.wrong else [True, True]
        )


@pytest.fixture
def cohort():
    adapter = LiveCodeBenchBenchmarkAdapter()
    records = [parse_source_record(_raw_record(str(i)), source_file="fixture") for i in range(3)]
    return {
        "adapter": adapter,
        "records": {r.question_id: r for r in records},
        "tasks": [adapter.project_record(r) for r in records],
        "identity": {"per_test_timeout_seconds": 1, "source": "frozen"},
        "progress": lambda _: None,
    }


@pytest.mark.asyncio
async def test_resume_preserves_successes_and_wrong_answers_and_retries_only_failures(
    tmp_path, cohort
):
    run = tmp_path / "run1"
    report = await run_experiment(
        **cohort,
        run_dir=run,
        provider=FakeProvider(fail={"1"}),
        executor=FakeExecutor(infra={"2"}, wrong={"0"}),
    )
    assert report["generation_coverage"]["numerator"] == 2
    assert report["actual_execution_n"] == 1
    assert report["pass_full_denominator"]["denominator"] == 3
    provider, executor = FakeProvider(), FakeExecutor()
    report = await run_experiment(
        **cohort, run_dir=run, provider=provider, executor=executor, resume=True
    )
    assert provider.calls == ["1"]
    assert executor.calls == ["1", "2"]
    assert report["complete"]
    assert report["pass_full_denominator"]["numerator"] == 2  # original WA retained
    events, _, _ = load_events(run, cohort["tasks"], run.name)
    assert len(events) == 8
    assert not any(PRIVATE_CANARY in p.read_text() for p in run.rglob("*.json"))


@pytest.mark.asyncio
async def test_interruption_checkpoint_and_resume_identity_gate(tmp_path, cohort):
    run = tmp_path / "interrupted"
    with pytest.raises(RuntimeError):
        await run_experiment(
            **cohort, run_dir=run, provider=FakeProvider(crash="1"), executor=FakeExecutor()
        )
    assert (run / "report.json").exists()
    provider = FakeProvider()
    changed = {**cohort, "identity": {"source": "changed", "per_test_timeout_seconds": 1}}
    with pytest.raises(ValueError, match="resume refused"):
        await run_experiment(
            **changed, run_dir=run, provider=provider, executor=FakeExecutor(), resume=True
        )
    assert not provider.calls
    report = await run_experiment(
        **cohort, run_dir=run, provider=provider, executor=FakeExecutor(), resume=True
    )
    assert report["complete"] and provider.calls == ["1", "2"]


@pytest.mark.asyncio
async def test_generate_then_execute_without_provider_and_reject_tampering(tmp_path, cohort):
    run = tmp_path / "split"
    await run_experiment(
        **cohort, run_dir=run, provider=FakeProvider(), executor=None, phase="generate"
    )
    report = await run_experiment(
        **cohort, run_dir=run, provider=None, executor=FakeExecutor(), phase="execute", resume=True
    )
    assert report["complete"]
    first = run / "events/000000.json"
    row = json.loads(first.read_text())
    row["event"]["solution"]["code"] = "tampered"
    first.write_text(json.dumps(row))
    with pytest.raises(ValueError, match="integrity"):
        load_events(run, cohort["tasks"], run.name)


def test_exclusive_run_lock(tmp_path):
    with run_lock(tmp_path):
        with pytest.raises(ValueError, match="already active"), run_lock(tmp_path):
            pass


@pytest.mark.asyncio
async def test_stdio_prompt_and_bounded_repair_never_receive_private_tests(cohort):
    class Provider(StdioHy3Provider):
        calls = []

        async def _call_model(self, messages):
            self.calls.append(messages)
            return (
                "invalid JSON"
                if len(self.calls) == 1
                else solution(cohort["tasks"][0]).model_dump_json()
            )

    provider = Provider(
        Settings(
            hy3_base_url="https://example.invalid/v1",
            hy3_api_key="test-secret",
            hy3_model="fixture",
            hy3_max_retries=2,
            hy3_max_parse_repairs=1,
        )
    )
    try:
        result = await provider.generate_task(cohort["tasks"][0])
        assert result.status == "success" and result.attempt_count == 2
        assert result.attempt_outcomes == ("parse_error", "success")
        prompt = json.dumps(provider.calls)
        assert PRIVATE_CANARY not in prompt
        assert "stdin" in prompt and "stdout" in prompt
        assert "test-secret" not in prompt
        assert "print" in provider.calls[0][0]["content"]
        assert provider.calls[1][1] == provider.calls[0][1]  # original context survives repair
        assert provider.public_generation_config()["stdio_prompt_sha256"]
    finally:
        await provider.aclose()
