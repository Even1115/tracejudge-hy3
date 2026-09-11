"""Tests for the read-only 2x2 ablation comparison adapter and its endpoint.

Synthetic PreparedPilot + in-memory predictions only: no provider calls, no
candidate execution, no reliance on the real (git-ignored) run artifacts.
Predictions are built through the REAL ``build_rule_report``/``merge_offline``
so the scorer's deterministic re-merge verification accepts them.  Integration
tests exercise the real pinned run when the artifacts exist locally, and skip
otherwise.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tracejudge_hy3.demo_app import ablation_view
from tracejudge_hy3.demo_app.ablation_view import (
    ABLATION_RUN_DIR,
    ABLATION_SCORES_FILE,
    CASE_NOTES,
    EXPECTED_PLAN_SHA256,
    _check_run_identity,
    _check_scores_file,
    build_ablation_view,
    load_ablation_comparison,
    synthetic_ablation_comparison,
)
from tracejudge_hy3.demo_app.comparison import ComparisonSourceError
from tracejudge_hy3.demo_app.current_validation import CurrentValidationError
from tracejudge_hy3.demo_app.server import make_server
from tracejudge_hy3.process_eval_v2.ablation import (
    CONDITIONS,
    RUN_CONFIG_SCHEMA,
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
)
from tracejudge_hy3.process_eval_v2.materials import digest, jsonl
from tracejudge_hy3.process_eval_v2.preflight import PreparedPilot
from tracejudge_hy3.process_eval_v2.scoring import score_ablation
from tracejudge_hy3.schemas.evaluation import ProcessAssessment
from tracejudge_hy3.schemas.location import FaultLocation
from tracejudge_hy3.schemas.solution import SolutionTrace
from tracejudge_hy3.static_analysis.ast_analyzer import analyze_code

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_RUN = REPO_ROOT / ABLATION_RUN_DIR
REAL_SCORES = REPO_ROOT / ABLATION_SCORES_FILE
CANARY = "SYNTHETIC_LABEL_CANARY"
VERDICTS = (True, False, True, None)


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
            rationale=CANARY,
        )
    return PreparedPilot(inputs, labels, {}, "post_feedback_development_consensus", "unverified")


def _judge(verdict, located=False):
    from tracejudge_hy3.process_eval_v2.contracts import public_process_correct

    return ProcessAssessment(
        reasoning_correct=verdict,
        plan_code_aligned=True,
        functional_correct=None,
        process_correct=public_process_correct(verdict, True),
        first_faulty_layer="reasoning" if located else None,
        first_faulty_step="S1" if located else None,
        first_faulty_location=(
            FaultLocation(source_field="implementation_steps", quote="Return zero.", step_id="S1")
            if located
            else None
        ),
        error_type="P01_ALGORITHM_ERROR" if located else None,
        explanation="Synthetic judgment.",
    )


def _prediction(pilot, key, condition, verdict=True, status="ok", located=False):
    item = pilot.inputs[key]
    if status != "ok":
        return AblationPrediction(
            item_id=key,
            condition=condition,
            input_sha256=pilot.input_hash(key),
            status=status,
            functional_evidence=_functional_snapshot(item),
        )
    judge_raw = _judge(verdict, located)
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


def _full_predictions(pilot, *, overrides=None, skip=()):
    rows = []
    overrides = overrides or {}
    for key in pilot.inputs:
        for condition in CONDITIONS:
            if (condition, key) in skip:
                continue
            # Default: the judge calls the process correct regardless of gold.
            verdict, located = overrides.get((condition, key), (True, False))
            rows.append(_prediction(pilot, key, condition, verdict=verdict, located=located))
    return rows


def _meta(**overrides):
    meta = {
        "name": "synthetic-ablation-run",
        "run_dir": "synthetic://run",
        "scores_file": "synthetic://scores",
        "run_status": "completed",
        "finished_utc": None,
        "plan_sha256": "f" * 64,
        "seed": 20260909,
        "pilot_config": "synthetic",
        "model": None,
        "budget": None,
        "judgments": None,
        "requests": {
            "dispatched": None,
            "request_seconds": None,
            "input_tokens": None,
            "output_tokens": None,
            "token_usage": "missing",
        },
    }
    meta.update(overrides)
    return meta


def _view(pilot, predictions, monkeypatch, **meta_overrides):
    # The editorial case notes are bound to the real frozen item ids; the
    # synthetic pilot has none, so detach them (the refusal itself is tested
    # separately in test_case_notes_refuse_unknown_items).
    monkeypatch.setattr(ablation_view, "CASE_NOTES", {})
    scores = score_ablation(pilot, predictions)
    return build_ablation_view(
        pilot, predictions, scores, {}, experiment_meta=_meta(**meta_overrides)
    )


def test_view_assembles_conditions_scores_and_outcomes(pilot, monkeypatch):
    # ablation_d catches the known error in item-1 (fn -> tp); nothing else changes.
    predictions = _full_predictions(pilot, overrides={("ablation_d", "item-1"): (False, True)})
    view = _view(pilot, predictions, monkeypatch)

    assert view["ok"] is True and view["data_kind"] == "real_experiment"
    assert len(view["items"]) == len(pilot.inputs)
    assert view["experiment"]["label_summary"] == {"correct": 2, "incorrect": 1, "unknown": 1}
    assert len(view["experiment"]["conditions"]) == 4
    # Conditions are never renamed to the historical three-method labels.
    labels = [c["label"] for c in view["experiment"]["conditions"]]
    assert not any(word in " ".join(labels) for word in ("Direct", "Structured", "Full"))

    items = {item["item_id"]: item for item in view["items"]}
    caught = items["item-1"]["conditions"]["ablation_d"]
    assert caught["outcome_judge"] == "tp" and caught["outcome_merged"] == "tp"
    for condition in ("ablation_a", "ablation_b", "ablation_c"):
        assert items["item-1"]["conditions"][condition]["outcome_merged"] == "fn"
    assert items["item-1"]["disagreement"]["merged"]["any"] is True
    assert "public_process_correct" in items["item-1"]["disagreement"]["merged"]["fields"]
    assert items["item-3"]["conditions"]["ablation_a"]["outcome_merged"] == "gold_unknown"
    # Scores come from the verified scorer payload, in both views.
    for score_view in ("judge_raw", "rule_merged"):
        dim = view["scores"]["views"][score_view]["ablation_d"]["public_process"]
        assert (dim["tp"], dim["fp"], dim["tn"], dim["fn"]) == (1, 0, 2, 0)
        assert dim["accuracy_all_known"] == {"numerator": 3, "denominator": 3, "value": 1.0}
    changed = [
        (pc["treatment"], pc["baseline"])
        for pc in view["scores"]["paired_comparisons"]
        if pc["changed_items"]
    ]
    assert changed == [("ablation_d", "ablation_b"), ("ablation_d", "ablation_c")]
    blob = json.dumps(view, ensure_ascii=False)
    # Human rationales are intentionally shown (collapsed) in the 人工参考
    # section; what must never leak are repo paths and credentials.
    assert str(REPO_ROOT) not in blob and "HY3_API_KEY" not in blob


def test_rule_merge_changes_are_exposed_per_view(pilot, monkeypatch):
    # Rewrite item-0's trace so the empty-input rule fires while the judge says aligned.
    key = "item-0"
    item = pilot.inputs[key]
    trace = SolutionTrace(
        problem_id=key,
        requirement_understanding=item.solution_trace.requirement_understanding,
        design_summary=item.solution_trace.design_summary,
        edge_cases_considered=item.solution_trace.edge_cases_considered,
        implementation_steps=[
            {
                "step_id": "S1",
                "content": "如果输入为空列表，直接返回 0 的显式分支。",
                "related_requirements": ["R1"],
            }
        ],
        code=item.solution_trace.code,
    )
    pilot.inputs[key] = item.model_copy(update={"solution_trace": trace})

    predictions = _full_predictions(pilot)
    view = _view(pilot, predictions, monkeypatch)
    entry = next(e for e in view["items"] if e["item_id"] == key)
    for condition in CONDITIONS:
        cond = entry["conditions"][condition]
        assert cond["rule_changed"] is True
        assert cond["combination_source"] == "rule"
        fired = [rule for rule in cond["rules"] if rule["fired"]]
        assert [rule["rule_id"] for rule in fired] == ["empty_input_claim"]
        assert cond["judge"]["plan_code_aligned"] is True
        assert cond["merged"]["plan_code_aligned"] is False
    assert view["scores"]["views_identical"] is False
    assert entry["disagreement"]["judge"]["any"] is False
    assert entry["disagreement"]["merged"]["any"] is False  # all four merge the same way


def test_failure_missing_and_abstain_are_not_divergence(pilot, monkeypatch):
    predictions = [
        row
        for row in _full_predictions(pilot, skip={("ablation_b", "item-2")})
        if not (row.condition == "ablation_c" and row.item_id == "item-2")
    ]
    predictions.append(_prediction(pilot, "item-2", "ablation_c", status="provider_error"))
    # item-3's ablation_a abstains (three-valued nulls) instead of deciding.
    predictions = [
        row
        for row in predictions
        if not (row.condition == "ablation_a" and row.item_id == "item-3")
    ]
    predictions.append(_prediction(pilot, "item-3", "ablation_a", verdict=None))
    view = _view(pilot, predictions, monkeypatch)
    items = {item["item_id"]: item for item in view["items"]}

    entry = items["item-2"]["conditions"]
    assert entry["ablation_b"]["status"] == "missing"
    assert entry["ablation_b"]["outcome_merged"] == "missing"
    assert entry["ablation_b"]["usage"] == {"recorded": False}
    assert entry["ablation_c"]["status"] == "provider_error"
    assert entry["ablation_c"]["outcome_merged"] == "failed"
    assert items["item-2"]["disagreement"]["merged"]["any"] is False
    abstained = items["item-3"]["conditions"]["ablation_a"]
    assert abstained["judge"]["public_process_correct"] is None
    assert abstained["outcome_judge"] == "abstained"
    # An abstention is a completed judgment with null dimensions: it genuinely
    # differs from the other ok conditions and therefore shows as divergence;
    # only failed/missing conditions are excluded from the comparison.
    assert "reasoning_correct" in items["item-3"]["disagreement"]["judge"]["fields"]


def test_scores_file_must_match_recompute(pilot):
    predictions = _full_predictions(pilot)
    raw = jsonl([row.model_dump(mode="json") for row in predictions])
    recomputed = score_ablation(pilot, predictions)
    good = {
        **recomputed,
        "predictions_sha256": digest(raw),
        "input_hashes": {key: pilot.input_hash(key) for key in pilot.inputs},
        "verified_sources": [],
    }
    _check_scores_file(good, raw, recomputed, pilot)  # consistent: accepted

    tampered = json.loads(json.dumps(good))
    tampered["views"]["rule_merged"]["ablation_d"]["dimensions"]["public_process_correct"]["tp"] = (
        99
    )
    with pytest.raises(ComparisonSourceError, match="离线重算"):
        _check_scores_file(tampered, raw, recomputed, pilot)

    wrong_binding = {**good, "predictions_sha256": "0" * 64}
    with pytest.raises(ComparisonSourceError, match="另一份预测文件"):
        _check_scores_file(wrong_binding, raw, recomputed, pilot)


def test_run_identity_checks(pilot):
    plan = build_plan(pilot)
    config = {
        "schema": RUN_CONFIG_SCHEMA,
        "pilot_config_sha256": "c" * 64,
        "conditions": list(CONDITIONS),
        "condition_fingerprints": condition_fingerprints(),
        "input_hashes": {key: pilot.input_hash(key) for key in pilot.inputs},
        "plan_sha256": plan_fingerprint(plan),
    }
    # A synthetic plan hash is not the registered plan: refused explicitly.
    with pytest.raises(ComparisonSourceError, match="预注册方案"):
        _check_run_identity(config, plan, pilot, "c" * 64)

    wrong_conditions = {**config, "conditions": ["ablation_a"]}
    with pytest.raises(ComparisonSourceError, match="条件集合"):
        _check_run_identity(wrong_conditions, plan, pilot, "c" * 64)

    wrong_fingerprints = {**config, "condition_fingerprints": {"ablation_a": "0" * 64}}
    with pytest.raises(ComparisonSourceError, match="指纹"):
        _check_run_identity(wrong_fingerprints, plan, pilot, "c" * 64)

    wrong_config_binding = {**config, "pilot_config_sha256": "0" * 64}
    with pytest.raises(ComparisonSourceError, match="pilot 配置"):
        _check_run_identity(wrong_config_binding, plan, pilot, "c" * 64)


def test_case_notes_refuse_unknown_items(pilot, monkeypatch):
    bogus = {"item-does-not-exist": next(iter(CASE_NOTES.values()))}
    monkeypatch.setattr(ablation_view, "CASE_NOTES", bogus)
    predictions = _full_predictions(pilot)
    scores = score_ablation(pilot, predictions)
    with pytest.raises(ComparisonSourceError, match="重点案例备注"):
        build_ablation_view(pilot, predictions, scores, {}, experiment_meta=_meta())


# ------------------------------------------------------------ real pinned run

needs_real = pytest.mark.skipif(
    not (REAL_RUN.is_dir() and REAL_SCORES.is_file()),
    reason="pinned ablation run/scores artifacts are not present",
)


@needs_real
def test_real_run_loads_and_matches_verified_counts():
    view = load_ablation_comparison(REPO_ROOT)
    assert view["ok"] is True and view["data_kind"] == "real_experiment"
    exp = view["experiment"]
    assert exp["run_status"] == "completed"
    assert exp["identity_label"] == "历史单轮消融 · 2026-09-09 · 原方案与原标签"
    assert exp["plan_sha256"] == EXPECTED_PLAN_SHA256
    assert exp["seed"] == 20260909
    assert exp["item_count"] == 12
    assert exp["requests"]["dispatched"] == 48
    assert exp["budget"]["money_estimate"] is None  # cost stays unknown
    summary = exp["label_summary"]
    assert summary == {"correct": 8, "incorrect": 2, "unknown": 2}
    assert exp["location_gold"]["has_reference"] is False

    # Verified against the scorer recomputation inside the adapter itself;
    # these assertions pin the documented outcome of the pinned run.
    # The single-round scores stay the historical rep1 numbers -- the
    # five-repetition tally never rewrites them.
    assert view["scores"]["views_identical"] is True
    expected = {
        "ablation_a": (0, 0, 8, 2),
        "ablation_b": (0, 0, 8, 2),
        "ablation_c": (0, 0, 8, 2),
        "ablation_d": (1, 0, 8, 1),
    }
    for condition, counts in expected.items():
        dim = view["scores"]["views"]["rule_merged"][condition]["public_process"]
        assert (dim["tp"], dim["fp"], dim["tn"], dim["fn"]) == counts
        assert dim["gold_known"] == 10 and dim["gold_unknown"] == 2
        assert view["scores"]["views"]["rule_merged"][condition]["coverage"]["ok"] == 12
    for effect in view["scores"]["rule_effects"].values():
        assert effect["changed_items"] == []

    items = {item["item_id"]: item for item in view["items"]}
    focus_ids = {case["item_id"] for case in view["focus_cases"]}
    assert focus_ids == set(CASE_NOTES)
    max_product = items["item-e5b9ff02f8bd9e3f3b61"]
    assert [max_product["conditions"][c]["outcome_merged"] for c in CONDITIONS] == [
        "fn",
        "fn",
        "fn",
        "tp",
    ]
    assert max_product["case_note"]["kind"] == "judgment_change"
    assert max_product["case_note"]["title"] == "单轮重点案例 · max_product：rep1 仅 D 检出"
    # The model's own words stay visible; nothing rewrites them.
    assert "[-2,0,1]" in max_product["conditions"]["ablation_d"]["judge"]["explanation"]
    division = items["item-caf2c4228350e46f5b68"]
    assert [division["conditions"][c]["outcome_merged"] for c in CONDITIONS] == ["fn"] * 4
    assert division["case_note"]["kind"] == "label_dispute"
    assert division["case_note"]["historical_reference"]

    for item in view["items"]:
        assert len(item["conditions"]) == 4
        assert all(c["status"] == "ok" for c in item["conditions"].values())
        assert all(c["usage"]["recorded"] for c in item["conditions"].values())
    blob = json.dumps(view, ensure_ascii=False)
    assert str(REPO_ROOT) not in blob
    assert "/mnt/" not in blob and "verified_sources" not in blob
    assert "HY3_API_KEY" not in blob and "Authorization" not in blob


@needs_real
def test_real_run_repetition_block_matches_bound_tally():
    view = load_ablation_comparison(REPO_ROOT)
    repetition = view["repetition"]
    assert repetition is not None and repetition["available"] is True
    assert repetition["max_product_detection"] == {
        "ablation_a": [1, 5],
        "ablation_b": [0, 4],
        "ablation_c": [0, 5],
        "ablation_d": [2, 5],
    }
    assert repetition["division_elements_total"] == [0, 20]
    assert repetition["rep1_accuracy"]["ablation_d"] == [9, 10]
    assert repetition["rep1_accuracy"]["ablation_a"] == [8, 10]

    items = {item["item_id"]: item for item in view["items"]}
    max_points = " ".join(items["item-e5b9ff02f8bd9e3f3b61"]["case_note"]["points"])
    assert "2/5" in max_points and "1/5" in max_points and "0/4" in max_points
    assert "0/5" in max_points and "没有稳定复现" in max_points
    division_points = " ".join(items["item-caf2c4228350e46f5b68"]["case_note"]["points"])
    assert "0/20" in division_points and "稳定漏检" in division_points

    # Stale "repetition not yet performed" wording is gone everywhere, and the
    # page states the single-round identity instead.
    blob = json.dumps(view, ensure_ascii=False)
    assert "尚需重复实验验证" not in blob
    assert any("原始单轮" in note for note in view["notes"])
    # Repetition numbers never leak into the single-round score table or the
    # paired transitions.
    for pc in view["scores"]["paired_comparisons"]:
        assert "2/5" not in json.dumps(pc) and "0/20" not in json.dumps(pc)


def test_repetition_failure_keeps_single_round_honest(monkeypatch):
    def _raise(_root):
        raise CurrentValidationError("tally missing")

    monkeypatch.setattr(ablation_view, "load_repetition_tally", _raise)
    view = {
        "repetition": "unset",
        "notes": [],
        "items": [
            {
                "item_id": "item-e5b9ff02f8bd9e3f3b61",
                "case_note": {
                    "kind": "judgment_change",
                    "title": "t",
                    "points": ["p"],
                    "historical_reference": None,
                },
            }
        ],
    }
    ablation_view._attach_repetition(REPO_ROOT, view)
    assert view["repetition"] is None
    assert any("暂不可用" in note for note in view["notes"])
    # Case notes keep only their single-round content: no repetition claims.
    assert view["items"][0]["case_note"]["points"] == ["p"]


# ------------------------------------------------------------- synthetic view


def test_synthetic_view_is_marked_and_covers_states():
    view = synthetic_ablation_comparison()
    assert view["data_kind"] == "synthetic_ui_test"
    assert "界面测试数据" in view["experiment"]["name"]
    assert set(view["experiment"]["conditions"][0]) == {
        "id",
        "label",
        "description",
        "static_evidence",
        "functional_evidence",
    }
    statuses = {c["status"] for item in view["items"] for c in item["conditions"].values()}
    assert {"ok", "provider_error", "parse_error", "missing"} <= statuses
    outcomes = {c["outcome_merged"] for item in view["items"] for c in item["conditions"].values()}
    assert {"tp", "tn", "fp", "fn", "gold_unknown", "abstained", "failed", "missing"} <= outcomes
    assert any(item["disagreement"]["merged"]["any"] for item in view["items"])
    assert any(c["rule_changed"] for item in view["items"] for c in item["conditions"].values())
    assert any(
        c["usage"]["recorded"] is False
        for item in view["items"]
        for c in item["conditions"].values()
    )
    assert view["focus_cases"]
    # The XSS probe stays inert text in the payload; the page renders textContent only.
    probe = view["items"][1]["conditions"]["ablation_d"]["judge"]["explanation"]
    assert "onerror" in probe
    blob = json.dumps(view, ensure_ascii=False)
    assert str(REPO_ROOT) not in blob and "HY3_API_KEY" not in blob


@needs_real
def test_synthetic_and_real_items_share_shape():
    real = load_ablation_comparison(REPO_ROOT)
    synthetic = synthetic_ablation_comparison()
    assert set(real) == set(synthetic)
    assert set(real["experiment"]) == set(synthetic["experiment"])
    real_item, synth_item = real["items"][0], synthetic["items"][0]
    assert set(real_item) == set(synth_item)
    for condition in CONDITIONS:
        assert set(real_item["conditions"][condition]) == set(synth_item["conditions"][condition])
    assert set(real["scores"]) == set(synthetic["scores"])
    assert set(real["scores"]["views"]["judge_raw"]["ablation_a"]) == set(
        synthetic["scores"]["views"]["judge_raw"]["ablation_a"]
    )


# ------------------------------------------------------------------ HTTP layer


@pytest.fixture()
def server():
    import threading

    httpd = make_server(port=0, repo_root=REPO_ROOT)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(base, path):
    request = urllib.request.Request(base + path, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_ablation_synthetic_endpoint(server):
    status, payload = _get(server, "/api/ablation-comparison?fixture=synthetic")
    assert status == 200
    assert payload["data_kind"] == "synthetic_ui_test"


def test_ablation_rejects_arbitrary_query(server):
    for query in ("?path=../../.env", "?fixture=real", "?fixture=synthetic&x=1", "?run=/etc"):
        status, payload = _get(server, f"/api/ablation-comparison{query}")
        assert status == 400, query
        assert "unsupported" in payload["error"]


@needs_real
def test_ablation_real_endpoint(server):
    status, payload = _get(server, "/api/ablation-comparison")
    assert status == 200
    assert payload["data_kind"] == "real_experiment"
    assert payload["experiment"]["item_count"] == 12
    assert payload["experiment"]["plan_sha256"] == EXPECTED_PLAN_SHA256
    blob = json.dumps(payload)
    assert str(REPO_ROOT) not in blob and "/mnt/" not in blob
