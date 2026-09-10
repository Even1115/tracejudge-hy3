"""Tests for the offline repetition tally of fixed-config ablation runs.

Synthetic PreparedPilot + synthetic run directories only: no provider calls,
no candidate execution.  Predictions are built through the REAL
``build_rule_report``/``merge_offline`` so the scorer's deterministic
re-merge verification accepts them, and each run directory is assembled with
genuinely computed hashes (plan fingerprint, pinned file SHA-256) so the
tally's verification chain is exercised for real.  An integration test
tallies the real pinned first repetition when its artifacts exist locally,
and skips otherwise.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracejudge_hy3.process_eval_v2 import repetition
from tracejudge_hy3.process_eval_v2.ablation import (
    CONDITIONS,
    RUN_CONFIG_SCHEMA,
    RUN_REPORT_SCHEMA,
    AblationPrediction,
    _functional_snapshot,
    build_plan,
    build_rule_report,
    condition_fingerprints,
    merge_offline,
    plan_fingerprint,
)
from tracejudge_hy3.process_eval_v2.aggregate import (
    aggregate_execution_summary,
    build_scaffold_problem,
)
from tracejudge_hy3.process_eval_v2.contracts import (
    FunctionalEvidence,
    PilotInput,
    PilotLabel,
    PublicTask,
    public_process_correct,
)
from tracejudge_hy3.process_eval_v2.materials import canonical, digest, jsonl
from tracejudge_hy3.process_eval_v2.preflight import PreparedPilot
from tracejudge_hy3.process_eval_v2.repetition import TALLY_SCHEMA, tally_repetitions
from tracejudge_hy3.schemas.evaluation import ProcessAssessment
from tracejudge_hy3.static_analysis.ast_analyzer import analyze_code

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_REP1 = "artifacts/experiments/process-pilot/mbpp12-ablation-20260909-v1"
REAL_PLAN_SHA256 = "1448c841986d6a4bdc0e0b5ba887fededf7c05b6fc7bc2272ddae239c9609774"
MAX_PRODUCT = "item-e5b9ff02f8bd9e3f3b61"
DIVISION = "item-caf2c4228350e46f5b68"

VERDICTS = (True, False, True, None)  # gold: correct / incorrect / correct / unknown
REL_RUNS = "artifacts/experiments/process-pilot"


@pytest.fixture
def pilot():
    inputs, labels = {}, {}
    for index, verdict in enumerate(VERDICTS):
        key = f"item-{index}"
        code = "def f():\n    return 0\n"
        inputs[key] = PilotInput(
            item_id=key,
            problem=PublicTask(
                title=f"task {index}",
                requirement="Return one.",
                function_signature="def f():",
                requirements=[{"requirement_id": "R1", "content": "Return one."}],
            ),
            solution_trace={
                "problem_id": key,
                "requirement_understanding": "Return one.",
                "design_summary": "Always return zero.",
                "edge_cases_considered": ["Empty input."],
                "implementation_steps": [
                    {"step_id": "S1", "content": "Return zero.", "related_requirements": ["R1"]}
                ],
                "code": code,
            },
            functional_evidence=FunctionalEvidence(
                source_run_id="test",
                source_record_sha256="a" * 64,
                candidate_code_sha256=digest(code.encode()),
                infrastructure_status="ok",
                base_status="fail",
                plus_status="fail",
            ),
        )
        labels[key] = PilotLabel(
            item_id=key,
            annotation_status="reviewed",
            annotator="annotator_a",
            reasoning_correct=verdict,
            plan_code_aligned=True,
            process_correct=verdict,
            localization_status=(
                "supported"
                if verdict is False
                else "unknown"
                if verdict is None
                else "not_applicable"
            ),
            first_faulty_layer="reasoning" if verdict is False else None,
            first_faulty_step=None,
            error_type="P01_ALGORITHM_ERROR" if verdict is False else None,
            evidence=[
                {
                    "step_id": "S1",
                    "code_span": None,
                    "requirement_id": "R1",
                    "description": "Synthetic evidence.",
                }
            ]
            if verdict is False
            else [],
            rationale="synthetic",
        )
    return PreparedPilot(inputs, labels, {}, "post_feedback_development_consensus", "unverified")


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "pilot-config.json"
    path.write_bytes(b'{"synthetic": "config"}\n')
    return path


@pytest.fixture
def bind_pilot(monkeypatch, pilot):
    monkeypatch.setattr(repetition, "prepare_pilot", lambda root, config_path: pilot)
    return pilot


def _judge(verdict):
    return ProcessAssessment(
        reasoning_correct=verdict,
        plan_code_aligned=True,
        functional_correct=None,
        process_correct=public_process_correct(verdict, True),
        first_faulty_layer=None,
        first_faulty_step=None,
        first_faulty_location=None,
        error_type=None,
        explanation="Synthetic judgment.",
    )


def _prediction(pilot, key, condition, verdict=True, status="ok"):
    item = pilot.inputs[key]
    if status != "ok":
        return AblationPrediction(
            item_id=key,
            condition=condition,
            input_sha256=pilot.input_hash(key),
            status=status,
            functional_evidence=_functional_snapshot(item),
        )
    judge_raw = _judge(verdict)
    problem = build_scaffold_problem(item)
    static = analyze_code(
        item.solution_trace.code, function_name=problem.function_name, visible_test_values=[]
    )
    execution = aggregate_execution_summary(item)
    rule_report = build_rule_report(problem, item, static, execution)
    merged = merge_offline(problem, item, static, execution, judge_raw, rule_report.merged_rule)
    return AblationPrediction(
        item_id=key,
        condition=condition,
        input_sha256=pilot.input_hash(key),
        status="ok",
        functional_evidence=_functional_snapshot(item),
        judge_raw=judge_raw,
        rule_report=rule_report,
        assessment=merged,
    )


def _write_rep(
    root: Path,
    name: str,
    pilot: PreparedPilot,
    config_sha256: str,
    *,
    verdicts: dict[tuple[str, str], bool | None] | None = None,
    statuses: dict[tuple[str, str], str] | None = None,
    skip: set[tuple[str, str]] | None = None,
    model: dict | None = None,
    run_status: str = "completed",
) -> str:
    """Write a synthetic repetition run directory with genuinely computed hashes."""

    verdicts = verdicts or {}
    statuses = statuses or {}
    skip = skip or set()
    target = root / REL_RUNS / name
    target.mkdir(parents=True)
    plan = build_plan(pilot)
    identity = {
        "schema": RUN_CONFIG_SCHEMA,
        "pilot_config_sha256": config_sha256,
        "plan_sha256": plan_fingerprint(plan),
        "input_hashes": {key: pilot.input_hash(key) for key in sorted(pilot.inputs)},
        "conditions": list(CONDITIONS),
        "condition_fingerprints": condition_fingerprints(),
        "prediction_schema_sha256": digest(canonical(AblationPrediction.model_json_schema())),
        "model": model if model is not None else {"model": "synthetic-judge", "temperature": 0},
        "budget": {"max_requests": 96, "max_attempts_per_judgment": 2},
        "implementation_sha256": {"synthetic": "0" * 64},
    }
    predictions = []
    for condition in CONDITIONS:
        for item_id in sorted(pilot.inputs):
            key = (condition, item_id)
            if key in skip:
                continue
            predictions.append(
                _prediction(
                    pilot,
                    item_id,
                    condition,
                    verdict=verdicts.get(key, True),
                    status=statuses.get(key, "ok"),
                )
            )
    files = {
        "ablation-plan.json": (json.dumps(plan, ensure_ascii=False, indent=2) + "\n").encode(),
        "run-config.json": (json.dumps(identity, ensure_ascii=False, indent=2) + "\n").encode(),
        "predictions.jsonl": jsonl([row.model_dump(mode="json") for row in predictions]),
        "requests.jsonl": b"",
    }
    report = {
        "schema": RUN_REPORT_SCHEMA,
        "status": run_status,
        "stop_reason": None if run_status == "completed" else "budget_exhausted",
        "resumed": False,
        "finished_utc": "2026-09-09T00:00:00+00:00",
        "run_identity": identity,
        "execution_order": plan["execution_order"],
        "budget": {"max_requests": 96, "money_estimate": None},
        "requests": {"dispatched": len(predictions), "token_usage": "missing"},
        "judgments": {},
        "pending": [],
        "files_sha256": {name: digest(raw) for name, raw in files.items()},
        "notes": [],
    }
    for filename, raw in files.items():
        (target / filename).write_bytes(raw)
    (target / "run-report.json").write_bytes(
        (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode()
    )
    return f"{REL_RUNS}/{name}"


def test_tally_detection_frequency_and_flapping(bind_pilot, pilot, config_file, tmp_path):
    config_sha = digest(config_file.read_bytes())
    # item-1 (gold: error) is detected by condition d in reps 1-2 but not 3.
    rep1 = _write_rep(
        tmp_path, "rep1", pilot, config_sha, verdicts={("ablation_d", "item-1"): False}
    )
    rep2 = _write_rep(
        tmp_path, "rep2", pilot, config_sha, verdicts={("ablation_d", "item-1"): False}
    )
    rep3 = _write_rep(tmp_path, "rep3", pilot, config_sha)

    tally = tally_repetitions(tmp_path, config_file, [rep1, rep2, rep3], expect_plan_sha256=None)

    assert tally["schema"] == TALLY_SCHEMA
    assert tally["repetition_count"] == 3
    assert tally["run_dirs"] == [rep1, rep2, rep3]

    cell = tally["items"]["item-1"]["conditions"]["ablation_d"]
    assert [row["decision_merged"] for row in cell["repetitions"]] == [
        "incorrect",
        "incorrect",
        "correct",
    ]
    assert [row["outcome_merged"] for row in cell["repetitions"]] == ["tp", "tp", "fn"]
    freq = cell["merged"]["detection_frequency"]
    assert (freq["numerator"], freq["denominator"], freq["value"]) == (2, 3, 2 / 3)

    assert tally["flapping"] == [
        {
            "item_id": "item-1",
            "condition": "ablation_d",
            "judged_incorrect_merged": 2,
            "decided_repetitions": 3,
        }
    ]
    # A stable correct cell never flaps.
    stable = tally["items"]["item-0"]["conditions"]["ablation_a"]
    assert stable["merged"]["judged_incorrect"] == 0
    assert stable["merged"]["detection_frequency"]["value"] == 0.0
    # Every repetition's own scorer output is reported alongside.
    assert len(tally["per_repetition_scores"]) == 3
    assert tally["per_repetition_scores"][0]["views"]["rule_merged"]["ablation_d"]
    assert any("不挑选" in note or "逐轮" in note for note in tally["notes"])


def test_tally_abstain_failure_missing_not_counted_as_detection(
    bind_pilot, pilot, config_file, tmp_path
):
    config_sha = digest(config_file.read_bytes())
    rep1 = _write_rep(
        tmp_path,
        "rep1",
        pilot,
        config_sha,
        verdicts={("ablation_a", "item-1"): None},  # abstention
        statuses={("ablation_b", "item-1"): "provider_error"},
        skip={("ablation_c", "item-1")},
        run_status="partial_execution",
    )
    rep2 = _write_rep(
        tmp_path, "rep2", pilot, config_sha, verdicts={("ablation_a", "item-1"): False}
    )

    tally = tally_repetitions(tmp_path, config_file, [rep1, rep2])

    abstain = tally["items"]["item-1"]["conditions"]["ablation_a"]
    assert abstain["merged"]["abstained"] == 1
    assert abstain["merged"]["judged_incorrect"] == 1
    # Denominator excludes the abstained round: 1/1, not 1/2.
    assert abstain["merged"]["detection_frequency"]["numerator"] == 1
    assert abstain["merged"]["detection_frequency"]["denominator"] == 1

    failed = tally["items"]["item-1"]["conditions"]["ablation_b"]
    assert failed["merged"]["failed"] == 1
    assert failed["merged"]["detection_frequency"]["denominator"] == 1

    missing = tally["items"]["item-1"]["conditions"]["ablation_c"]
    assert missing["merged"]["missing"] == 1
    assert missing["merged"]["detection_frequency"]["denominator"] == 1

    # The partial round is reported honestly, not hidden.
    assert tally["repetitions"][0]["status"] == "partial_execution"
    # Flapping: rep1 abstained, so no decided flapping for ablation_a.
    assert not any(
        row["item_id"] == "item-1" and row["condition"] == "ablation_a" for row in tally["flapping"]
    )


def test_tally_unknown_gold_decision_counted_without_tp_fp(
    bind_pilot, pilot, config_file, tmp_path
):
    config_sha = digest(config_file.read_bytes())
    rep1 = _write_rep(
        tmp_path, "rep1", pilot, config_sha, verdicts={("ablation_c", "item-3"): False}
    )
    rep2 = _write_rep(tmp_path, "rep2", pilot, config_sha)

    tally = tally_repetitions(tmp_path, config_file, [rep1, rep2])

    cell = tally["items"]["item-3"]["conditions"]["ablation_c"]
    assert tally["items"]["item-3"]["gold_process_correct"] is None
    assert [row["outcome_merged"] for row in cell["repetitions"]] == [
        "unknown_decided",
        "unknown_decided",
    ]
    # For unknown gold the decided direction is still reported per round.
    assert [row["decision_merged"] for row in cell["repetitions"]] == ["incorrect", "correct"]
    assert cell["merged"]["judged_incorrect"] == 1
    assert {(row["item_id"], row["condition"]) for row in tally["flapping"]} == {
        ("item-3", "ablation_c")
    }


def test_tally_refuses_mixed_identity(bind_pilot, pilot, config_file, tmp_path):
    config_sha = digest(config_file.read_bytes())
    rep1 = _write_rep(tmp_path, "rep1", pilot, config_sha)
    rep2 = _write_rep(
        tmp_path, "rep2", pilot, config_sha, model={"model": "another-model", "temperature": 0}
    )

    with pytest.raises(ValueError, match="不允许混跑"):
        tally_repetitions(tmp_path, config_file, [rep1, rep2])


def test_tally_refuses_plan_hash_mismatch(bind_pilot, pilot, config_file, tmp_path):
    config_sha = digest(config_file.read_bytes())
    rep1 = _write_rep(tmp_path, "rep1", pilot, config_sha)

    with pytest.raises(ValueError, match="预登记"):
        tally_repetitions(tmp_path, config_file, [rep1], expect_plan_sha256="0" * 64)


def test_tally_refuses_tampered_files(bind_pilot, pilot, config_file, tmp_path):
    config_sha = digest(config_file.read_bytes())
    rep1 = _write_rep(tmp_path, "rep1", pilot, config_sha)
    # Modify a pinned file after the report was written.
    with (tmp_path / rep1 / "requests.jsonl").open("ab") as handle:
        handle.write(b'{"tampered": true}\n')

    with pytest.raises(ValueError, match="SHA-256"):
        tally_repetitions(tmp_path, config_file, [rep1])


def test_tally_refuses_duplicate_run_dirs(bind_pilot, pilot, config_file, tmp_path):
    config_sha = digest(config_file.read_bytes())
    rep1 = _write_rep(tmp_path, "rep1", pilot, config_sha)

    with pytest.raises(ValueError, match="两次"):
        tally_repetitions(tmp_path, config_file, [rep1, rep1])


def test_tally_refuses_rebound_pilot_config(bind_pilot, pilot, config_file, tmp_path):
    rep1 = _write_rep(tmp_path, "rep1", pilot, digest(b'{"other": "config"}'))

    with pytest.raises(ValueError, match="另一份 pilot 配置"):
        tally_repetitions(tmp_path, config_file, [rep1])


def test_tally_refuses_out_of_root_paths(bind_pilot, config_file, tmp_path):
    with pytest.raises(ValueError):
        tally_repetitions(tmp_path, config_file, ["../outside"])


def test_cli_writes_pinned_report(bind_pilot, pilot, config_file, tmp_path):
    from scripts.tally_ablation_repeats import main

    config_sha = digest(config_file.read_bytes())
    rep1 = _write_rep(tmp_path, "rep1", pilot, config_sha)
    rep2 = _write_rep(tmp_path, "rep2", pilot, config_sha)
    output = f"{REL_RUNS}/tally-out"

    code = main(
        [
            "--project-root",
            str(tmp_path),
            "--config",
            str(config_file),
            "--runs",
            rep1,
            rep2,
            "--output",
            output,
        ]
    )
    assert code == 0
    payload = (tmp_path / output / "tally-report.json").read_bytes()
    manifest = json.loads((tmp_path / output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files_sha256"]["tally-report.json"] == digest(payload)
    assert json.loads(payload)["repetition_count"] == 2

    # An existing tally directory is never overwritten.
    assert (
        main(
            [
                "--project-root",
                str(tmp_path),
                "--config",
                str(config_file),
                "--runs",
                rep1,
                rep2,
                "--output",
                output,
            ]
        )
        == 2
    )


@pytest.mark.skipif(
    not (REPO_ROOT / REAL_REP1 / "run-report.json").exists(),
    reason="real pinned repetition 1 artifacts are not present locally",
)
def test_real_repetition_one_passes_verification():
    """The completed first round tallies against its pre-registered plan hash."""

    tally = tally_repetitions(
        REPO_ROOT,
        Path("data/manifests/process_pilot_v1.json"),
        [REAL_REP1],
        expect_plan_sha256=REAL_PLAN_SHA256,
    )

    assert tally["repetition_count"] == 1
    assert tally["plan_sha256"] == REAL_PLAN_SHA256
    assert tally["repetitions"][0]["status"] == "completed"
    # Round-1 facts already documented in the ablation report:
    # max_product detected only by D, division_elements detected by none.
    max_product = tally["items"][MAX_PRODUCT]["conditions"]
    assert max_product["ablation_d"]["merged"]["judged_incorrect"] == 1
    for condition in ("ablation_a", "ablation_b", "ablation_c"):
        assert max_product[condition]["merged"]["judged_incorrect"] == 0
    division = tally["items"][DIVISION]["conditions"]
    for condition in CONDITIONS:
        assert division[condition]["merged"]["judged_incorrect"] == 0
        assert division[condition]["repetitions"][0]["outcome_merged"] == "fn"
    assert tally["flapping"] == []
