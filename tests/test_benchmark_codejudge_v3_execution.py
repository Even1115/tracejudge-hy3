from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

from tracejudge_hy3.benchmark.codejudge_eval import (
    CodeJudgeEvalAdapter,
    CodeJudgeEvalError,
    CodeJudgeEvalSelection,
)
from tracejudge_hy3.benchmark.codejudge_v3 import (
    CodeJudgeV3Selection,
    select_v3_experiment_a,
    select_v3_experiment_b,
    used_raw_task_ids,
)
from tracejudge_hy3.benchmark.codejudge_v3_execution import (
    CodeJudgeV3Record,
    V3ParseError,
    build_v3_call_plan,
    judge_v3_call,
    parse_v4_verdict,
    validate_resume_prefix,
)
from tracejudge_hy3.benchmark.codejudge_v3_metrics import (
    exact_mcnemar_p_value,
    exact_mcnemar_power,
    holm_adjust,
    score_v3_a,
    score_v3_b,
)
from tracejudge_hy3.benchmark.contracts import JudgeStatus, canonical_sha256
from tracejudge_hy3.benchmark.judge_only_prompt_v4 import (
    V4_CONDITION_SCHEMAS,
    JudgeOnlyVerdictV4Hard,
    judge_only_prompt_v4_bundle_sha256,
)

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "codejudge_eval"
V4_HASH = "7a5657d6df18167fc064ae32a696e7d427e4e5cb90f794a50fb6969408ea6d4a"

HARD_OUTCOMES = {
    "A": (),
    "B": (),
    "C": ("WA",),
    "D": ("RE",),
    "E": ("WA", "RE"),
    "F": ("TLE",),
    "G": ("WA", "TLE"),
    "H": ("RE", "TLE"),
    "I": ("WA", "RE", "TLE"),
}


@pytest.fixture(scope="module")
def v4_materials():
    adapter = CodeJudgeEvalAdapter()
    samples = adapter.load_samples(DATA_DIR, exclude_ambiguous_interface=True)
    v2 = CodeJudgeEvalSelection.model_validate_json(
        (DATA_DIR / "selection_v2.json").read_text(encoding="utf-8")
    )
    used = used_raw_task_ids(v2)
    selection_a = select_v3_experiment_a(
        samples,
        descriptor=adapter.descriptor,
        exclude_raw_task_ids=used,
        prompt_bundle_sha256=V4_HASH,
    )
    selection_b = select_v3_experiment_b(
        samples,
        descriptor=adapter.descriptor,
        exclude_raw_task_ids=frozenset(used | {entry.raw_task_id for entry in selection_a.entries}),
        prompt_bundle_sha256=V4_HASH,
    )
    return samples, selection_a, selection_b


def test_prompt_v4_is_pinned_and_omits_root_cause_field():
    assert judge_only_prompt_v4_bundle_sha256() == V4_HASH
    for schema in V4_CONDITION_SCHEMAS.values():
        assert "error_type" not in schema.model_json_schema()["properties"]
    assert set(
        V4_CONDITION_SCHEMAS["easy"].model_json_schema()["properties"]["verdict"]["enum"]
    ) == {
        "AC",
        "CE",
        "NOT_AC",
    }
    assert "execution_outcomes" in V4_CONDITION_SCHEMAS["hard"].model_json_schema()["properties"]


@pytest.mark.parametrize("bad", [["WA", "BOGUS"], ["BOGUS"], ["wa"]])
def test_prompt_v4_hard_rejects_any_unknown_outcome(bad):
    with pytest.raises(Exception, match="execution_outcomes"):
        JudgeOnlyVerdictV4Hard.model_validate(
            {
                "functional_correct": False,
                "verdict": "ERRORED",
                "execution_outcomes": bad,
                "explanation": "invalid",
            }
        )


def test_v4_strict_parser_rejects_v3_error_type_field():
    with pytest.raises(V3ParseError) as caught:
        parse_v4_verdict(
            json.dumps(
                {
                    "functional_correct": True,
                    "verdict": "AC",
                    "error_type": None,
                    "explanation": "extra field must fail",
                }
            ),
            "easy",
        )
    assert caught.value.diagnostic_code == "schema_validation_failed"


def test_v4_selections_are_reproducible_and_old_files_remain_distinct(v4_materials):
    _, derived_a, derived_b = v4_materials
    frozen_a = CodeJudgeV3Selection.model_validate_json(
        (DATA_DIR / "selection_v3a_v4.json").read_text(encoding="utf-8")
    )
    frozen_b = CodeJudgeV3Selection.model_validate_json(
        (DATA_DIR / "selection_v3b_v4.json").read_text(encoding="utf-8")
    )
    old_a = CodeJudgeV3Selection.model_validate_json(
        (DATA_DIR / "selection_v3a.json").read_text(encoding="utf-8")
    )
    old_b = CodeJudgeV3Selection.model_validate_json(
        (DATA_DIR / "selection_v3b.json").read_text(encoding="utf-8")
    )
    assert frozen_a == derived_a
    assert frozen_b == derived_b
    assert frozen_a.prompt_bundle_sha256 == V4_HASH
    assert frozen_b.prompt_bundle_sha256 == V4_HASH
    assert old_a.prompt_bundle_sha256 != V4_HASH
    assert old_b.prompt_bundle_sha256 != V4_HASH
    assert [entry.model_dump() for entry in old_a.entries] == [
        entry.model_dump() for entry in frozen_a.entries
    ]
    assert [entry.model_dump() for entry in old_b.entries] == [
        entry.model_dump() for entry in frozen_b.entries
    ]


def test_v3_call_plans_bind_all_conditions_in_frozen_order(v4_materials):
    samples, selection_a, selection_b = v4_materials
    plan_a = build_v3_call_plan(selection_a, samples, experiment="a")
    plan_b = build_v3_call_plan(selection_b, samples, experiment="b")
    assert len(plan_a) == 120
    assert {call.condition for call, _ in plan_a} == {"hard"}
    assert len(plan_b) == 150
    for entry_index, entry in enumerate(selection_b.entries):
        calls = [call for call, _ in plan_b[entry_index * 3 : entry_index * 3 + 3]]
        assert tuple(call.condition for call in calls) == entry.call_order
        assert tuple(call.call_position for call in calls) == (1, 2, 3)


class _Responses:
    def __init__(self, *responses: str):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        return self.responses.pop(0)


def _perfect_response(condition, sample):
    letter = sample.gold.answer_letter
    if condition == "easy":
        payload = {
            "functional_correct": sample.gold.functional_correct,
            "verdict": {"A": "AC", "B": "CE", "C": "NOT_AC"}[letter],
            "explanation": "mock condition-aware judgment",
        }
    elif condition == "middle":
        payload = {
            "functional_correct": sample.gold.functional_correct,
            "verdict": {
                "A": "AC",
                "B": "CE",
                "C": "WA",
                "D": "RE",
                "E": "TLE",
                "F": "MIXED",
            }[letter],
            "explanation": "mock condition-aware judgment",
        }
    else:
        payload = {
            "functional_correct": sample.gold.functional_correct,
            "verdict": "AC" if letter == "A" else ("CE" if letter == "B" else "ERRORED"),
            "execution_outcomes": list(HARD_OUTCOMES[letter]),
            "explanation": "mock condition-aware judgment",
        }
    return json.dumps(payload)


@pytest.mark.asyncio
async def test_v4_runner_repairs_once_and_preserves_hard_outcomes(v4_materials):
    samples, selection_a, _ = v4_materials
    call, sample = build_v3_call_plan(selection_a, samples, experiment="a")[0]
    provider = _Responses(
        "not-json",
        json.dumps(
            {
                "functional_correct": False,
                "verdict": "ERRORED",
                "execution_outcomes": ["TLE", "WA"],
                "explanation": "fails and times out",
                "confidence": 0.7,
            }
        ),
    )
    raw_events = []
    record = await judge_v3_call(
        provider,
        call,
        sample,
        experiment_id=selection_a.experiment_id,
        raw_sink=lambda planned, stage, raw: raw_events.append((planned.key, stage, raw)),
    )
    assert record.status is JudgeStatus.VALID_JUDGMENT
    assert record.execution_outcomes == ("WA", "TLE")
    assert record.normalized_outcomes == ("WA", "TLE")
    assert record.derived_label == "G"
    assert record.parse_repairs == 1
    assert [event[1] for event in raw_events] == ["initial", "repair"]
    assert len(provider.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("experiment", ["a", "b"])
async def test_full_mock_execution_plan_reaches_scorer(v4_materials, experiment):
    samples, selection_a, selection_b = v4_materials
    selection = selection_a if experiment == "a" else selection_b
    plan = build_v3_call_plan(selection, samples, experiment=experiment)
    provider = _Responses(*(_perfect_response(call.condition, sample) for call, sample in plan))
    records = []
    for call, sample in plan:
        records.append(
            await judge_v3_call(
                provider,
                call,
                sample,
                experiment_id=selection.experiment_id,
            )
        )
    report = (
        score_v3_a(tuple(records), plan) if experiment == "a" else score_v3_b(tuple(records), plan)
    )
    if experiment == "a":
        assert report["primary"]["balanced_accuracy_full_denominator"] == 1.0
    else:
        assert report["analysis_population"]["complete_triplet_n"] == 50
    assert len(provider.calls) == len(plan)


def _perfect_record(call, sample, experiment_id):
    letter = sample.gold.answer_letter
    if call.condition == "easy":
        native = {"A": "AC", "B": "CE", "C": "NOT_AC"}[letter]
        native_outcomes = None
        normalized_outcomes = () if letter == "A" else None
    elif call.condition == "middle":
        native = {"A": "AC", "B": "CE", "C": "WA", "D": "RE", "E": "TLE", "F": "MIXED"}[letter]
        native_outcomes = None
        normalized_outcomes = {
            "A": (),
            "B": None,
            "C": ("WA",),
            "D": ("RE",),
            "E": ("TLE",),
            "F": None,
        }[letter]
    else:
        native = "AC" if letter == "A" else ("CE" if letter == "B" else "ERRORED")
        native_outcomes = HARD_OUTCOMES[letter]
        normalized_outcomes = None if letter == "B" else HARD_OUTCOMES[letter]
    return CodeJudgeV3Record(
        experiment_id=experiment_id,
        **call.model_dump(mode="python", exclude={"source_record_sha256"}),
        status=JudgeStatus.VALID_JUDGMENT,
        native_verdict=native,
        functional_correct=sample.gold.functional_correct,
        execution_outcomes=native_outcomes,
        normalized_ce=letter == "B",
        normalized_outcomes=normalized_outcomes,
        derived_label=letter,
        confidence=1.0,
        source_judgment_sha256=canonical_sha256({"test": call.key}),
    )


def _failed_record(record):
    return CodeJudgeV3Record(
        experiment_id=record.experiment_id,
        planned_call_index=record.planned_call_index,
        raw_task_id=record.raw_task_id,
        data_id=record.data_id,
        condition=record.condition,
        call_position=record.call_position,
        task=record.task,
        candidate_id=record.candidate_id,
        candidate_sha256=record.candidate_sha256,
        status=JudgeStatus.PARSE_ERROR,
        diagnostic_code="test_parse_failure",
        source_judgment_sha256=canonical_sha256({"failed": record.key}),
    )


def test_v3a_scorer_full_coverage_and_balanced_metrics(v4_materials):
    samples, selection_a, _ = v4_materials
    plan = build_v3_call_plan(selection_a, samples, experiment="a")
    records = tuple(
        _perfect_record(call, sample, selection_a.experiment_id) for call, sample in plan
    )
    report = score_v3_a(records, plan)
    assert report["primary"]["balanced_accuracy_full_denominator"] == 1.0
    assert report["binary_metrics"]["accuracy_full_denominator"]["value"] == 1.0
    assert report["majority_baseline"]["balanced_accuracy"] == 0.5
    with pytest.raises(CodeJudgeEvalError, match="coverage violation"):
        score_v3_a(records[:-1], plan)
    with pytest.raises(CodeJudgeEvalError, match="duplicate"):
        score_v3_a((*records, records[-1]), plan)


def test_v3b_scorer_pairing_holm_and_failure_gate(v4_materials):
    samples, _, selection_b = v4_materials
    plan = build_v3_call_plan(selection_b, samples, experiment="b")
    records = tuple(
        _perfect_record(call, sample, selection_b.experiment_id) for call, sample in plan
    )
    report = score_v3_b(records, plan)
    assert all(
        row["functional_accuracy_full_denominator"]["value"] == 1.0
        for row in report["per_condition"].values()
    )
    assert all(
        pair["exact_two_sided_mcnemar_p_value"] == 1.0
        for pair in report["primary_pairwise_functional_correctness"]
    )
    assert report["hard_execution_outcome_exact_match_non_ce"]["value"] == 1.0

    failed = list(records)
    easy_indices = [index for index, record in enumerate(failed) if record.condition == "easy"][:3]
    for index in easy_indices:
        failed[index] = _failed_record(failed[index])
    gated = score_v3_b(tuple(failed), plan)
    involving_easy = [
        pair
        for pair in gated["primary_pairwise_functional_correctness"]
        if "easy" in {pair["first"], pair["second"]}
    ]
    assert all(pair["available"] is False for pair in involving_easy)
    assert all(pair["holm_adjusted_p_value"] is None for pair in involving_easy)


def test_resume_requires_an_exact_plan_prefix(v4_materials):
    samples, selection_a, _ = v4_materials
    plan = build_v3_call_plan(selection_a, samples, experiment="a")
    records = tuple(
        _perfect_record(call, sample, selection_a.experiment_id) for call, sample in plan[:3]
    )
    validate_resume_prefix(records, plan, experiment_id=selection_a.experiment_id)
    with pytest.raises(CodeJudgeEvalError, match="prefix"):
        validate_resume_prefix(
            (records[1], records[0]), plan, experiment_id=selection_a.experiment_id
        )


def test_exact_mcnemar_and_holm_contracts():
    assert exact_mcnemar_p_value(9, 1) == pytest.approx(0.021484375)
    assert exact_mcnemar_p_value(0, 0) == 1.0
    assert exact_mcnemar_power(
        candidate_n=50,
        discordance_rate=0.2,
        directional_probability=2 / 3,
    ) == pytest.approx(0.105682, abs=1e-6)
    assert holm_adjust((0.01, 0.04, 1.0)) == pytest.approx((0.03, 0.08, 1.0))


def _load_run_script_module():
    path = ROOT / "scripts" / "run_codejudge_eval_v3.py"
    spec = importlib.util.spec_from_file_location("test_run_codejudge_eval_v3", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _ArtifactProvider:
    def __init__(self, responses, *, interrupt_at=None):
        self.responses = list(responses)
        self.interrupt_at = interrupt_at
        self.calls = 0

    def public_configuration(self):
        return {
            "provider": "hy3",
            "model": "mock-v3",
            "temperature": 0.0,
            "timeout_seconds": 120.0,
            "max_retries": 2,
            "reasoning_effort": "high",
            "endpoint_sha256": "f" * 64,
        }

    async def complete(self, *, system_prompt, user_prompt):
        self.calls += 1
        if self.interrupt_at == self.calls:
            raise KeyboardInterrupt
        return self.responses.pop(0)

    def audit_counters(self):
        return {"calls": self.calls, "attempts": self.calls, "retries": 0}

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_formal_artifacts_resume_from_exact_persisted_prefix(tmp_path, monkeypatch, capsys):
    runner = _load_run_script_module()
    _, selection, plan = runner._load_plan("a")
    all_responses = [_perfect_response(call.condition, sample) for call, sample in plan]
    first_provider = _ArtifactProvider(all_responses, interrupt_at=4)
    monkeypatch.setattr(runner, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(runner, "Hy3JudgeOnlyProvider", lambda: first_provider)

    with pytest.raises(KeyboardInterrupt):
        await runner._run_formal(experiment="a", run_id="v3a-resume-test", resume=False)
    run_dir = tmp_path / "v3a-resume-test"
    assert len(runner._read_records(run_dir / "records.jsonl")) == 3
    assert not (run_dir / "report.json").exists()

    resume_provider = _ArtifactProvider(all_responses[3:])
    monkeypatch.setattr(runner, "Hy3JudgeOnlyProvider", lambda: resume_provider)
    await runner._run_formal(experiment="a", run_id="v3a-resume-test", resume=True)

    assert len(runner._read_records(run_dir / "records.jsonl")) == 120
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    receipt = json.loads((run_dir / "completion_receipt.json").read_text(encoding="utf-8"))
    assert report["primary"]["balanced_accuracy_full_denominator"] == 1.0
    assert receipt["audit"]["records"] == 120
    assert receipt["audit"]["invocations"] == 2
    assert resume_provider.calls == 117
    assert os.stat(run_dir).st_mode & 0o777 == 0o700
    for name in ("manifest.json", "records.jsonl", "provider_raw.jsonl", "report.json"):
        assert os.stat(run_dir / name).st_mode & 0o777 == 0o600
    output = capsys.readouterr().out
    assert "[recovery]" in output
    assert "--run-id v3a-resume-test --resume --confirm-real-provider" in output
