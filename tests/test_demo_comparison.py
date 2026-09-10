"""Tests for the read-only three-method comparison adapter and its endpoint.

Uses a synthetic PreparedPilot and a synthetic run directory in tmp_path: no
provider calls, no candidate execution, no reliance on the real (git-ignored)
pilot artifacts.  One integration test exercises the real pinned run when the
artifacts exist locally, and skips otherwise.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tracejudge_hy3.demo_app.comparison import (
    METHOD_LABELS,
    ComparisonSourceError,
    build_comparison_view,
    load_method_comparison,
    synthetic_method_comparison,
)
from tracejudge_hy3.demo_app.server import make_server
from tracejudge_hy3.process_eval_v2.contracts import (
    FunctionalEvidence,
    PilotInput,
    PilotLabel,
    Prediction,
    PublicTask,
)
from tracejudge_hy3.process_eval_v2.materials import canonical, digest, jsonl
from tracejudge_hy3.process_eval_v2.preflight import PreparedPilot
from tracejudge_hy3.schemas.evaluation import ProcessAssessment
from tracejudge_hy3.schemas.location import FaultLocation
from tracejudge_hy3.schemas.solution import SolutionTrace

REPO_ROOT = Path(__file__).resolve().parents[1]
VERDICTS = (True, False, True, None)


@pytest.fixture
def pilot():
    inputs, labels = {}, {}
    for index, verdict in enumerate(VERDICTS):
        key = f"item-{index}"
        solution = SolutionTrace(
            problem_id=key,
            requirement_understanding="Return one.",
            design_summary="Always return zero.",
            edge_cases_considered=["Empty input."],
            implementation_steps=[
                {"step_id": "S1", "content": "Return zero.", "related_requirements": ["R1"]}
            ],
            code="def f():\n    return 0\n",
        )
        inputs[key] = PilotInput(
            item_id=key,
            problem=PublicTask(
                title=f"task {index}",
                requirement="Return one.",
                function_signature="f()",
                requirements=[{"requirement_id": "R1", "content": "Return one."}],
            ),
            solution_trace=solution,
            functional_evidence=FunctionalEvidence(
                source_run_id="test",
                source_record_sha256="a" * 64,
                candidate_code_sha256=digest(solution.code.encode()),
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
                    "code_span": "L2",
                    "requirement_id": "R1",
                    "description": "Evidence.",
                }
            ]
            if verdict is False
            else [],
            rationale="SYNTHETIC_LABEL",
        )
    return PreparedPilot(inputs, labels, {}, "post_feedback_development_consensus", "unverified")


def _assessment(pilot, key, verdict, located=False):
    return ProcessAssessment(
        reasoning_correct=verdict,
        plan_code_aligned=True,
        functional_correct=False,
        process_correct=False if verdict is False else verdict,
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


def _prediction(pilot, key, method, verdict=True, status="ok", located=False):
    return Prediction(
        item_id=key,
        method=method,
        input_sha256=pilot.input_hash(key),
        status=status,
        assessment=_assessment(pilot, key, verdict, located) if status == "ok" else None,
    )


def _write_run(tmp_path: Path, pilot, predictions, *, ledger=True):
    """Write a consistent run directory with report-pinned hashes."""

    run = tmp_path / "run"
    run.mkdir()
    (run / "predictions.jsonl").write_bytes(
        jsonl([row.model_dump(mode="json") for row in predictions])
    )
    ledger_rows = []
    seq = 0
    for row in predictions:
        seq += 1
        ledger_rows.append(
            {"event": "dispatched", "item_id": row.item_id, "method": row.method, "seq": seq}
        )
        ledger_rows.append(
            {
                "event": "outcome",
                "seq": seq,
                "status": "response_received" if row.status == "ok" else "request_error",
                "error_type": None,
                "input_tokens": 100 if row.status == "ok" else None,
                "output_tokens": 50 if row.status == "ok" else None,
                "request_seconds": 1.5,
            }
        )
    if ledger:
        (run / "requests.jsonl").write_bytes(jsonl(ledger_rows))
    else:
        (run / "requests.jsonl").write_bytes(b"")
    config = {
        "schema": "tracejudge-process-pilot-run-config-v1",
        "pilot_config_sha256": "c" * 64,
        "input_hashes": {key: pilot.input_hash(key) for key in pilot.inputs},
        "methods": ["direct_judge", "structured_judge", "full_system"],
        "method_fingerprints": {m: "d" * 64 for m in METHOD_LABELS},
        "prediction_schema_sha256": "e" * 64,
        "model": {"provider": "synthetic", "model": "none"},
        "budget": {"max_requests": 100, "max_attempts_per_judgment": 2},
        "implementation_sha256": {},
    }
    (run / "run-config.json").write_bytes(canonical(config))
    if ledger:
        request_summary = {
            "dispatched": seq,
            "outcomes_recorded": seq,
            "total_request_seconds": 1.5 * seq,
            "input_tokens": 100 * sum(r.status == "ok" for r in predictions),
            "output_tokens": 50 * sum(r.status == "ok" for r in predictions),
            "token_usage": "complete",
        }
    else:
        request_summary = {"dispatched": 0, "token_usage": "missing"}
    report = {
        "schema": "tracejudge-process-pilot-run-v1",
        "status": "completed",
        "stop_reason": None,
        "finished_utc": "2026-09-09T00:00:00+00:00",
        "budget": {"max_requests": 100, "consumed_requests": seq, "money_estimate": None},
        "requests": request_summary,
        "files_sha256": {
            name: digest((run / name).read_bytes())
            for name in ("predictions.jsonl", "requests.jsonl", "run-config.json")
        },
    }
    (run / "run-report.json").write_bytes(canonical(report))
    return run, report, config


def _full_predictions(pilot, *, skip=None):
    rows = []
    for key in pilot.inputs:
        for method in METHOD_LABELS:
            if (method, key) == skip:
                continue
            verdict = pilot.labels[key].reasoning_correct
            rows.append(_prediction(pilot, key, method, verdict=verdict is not False))
    return rows


def test_view_assembles_items_methods_and_gold(pilot, tmp_path):
    run, report, config = _write_run(tmp_path, pilot, _full_predictions(pilot))
    view = build_comparison_view(
        pilot, run, run_dir_display="synthetic/run", report=report, config=config
    )

    assert view["ok"] is True and view["data_kind"] == "real_experiment"
    assert len(view["items"]) == len(pilot.inputs)
    first = view["items"][0]
    assert set(first["methods"]) == set(METHOD_LABELS)
    assert first["input_sha256"] == pilot.input_hash(first["item_id"])
    # Label summary is computed from the labels, never hardcoded.
    assert view["experiment"]["label_summary"] == {"correct": 2, "incorrect": 1, "unknown": 1}
    for item in view["items"]:
        for method in item["methods"].values():
            assert method["status"] == "ok"
            assert method["usage"]["recorded"] is True
            assert method["usage"]["requests"] == 1
            assert method["usage"]["input_tokens"] == 100
    blob = json.dumps(view, ensure_ascii=False)
    assert str(tmp_path) not in blob and str(REPO_ROOT) not in blob


def test_gold_comparison_categories(pilot, tmp_path):
    predictions = _full_predictions(pilot)
    # item-1 gold is an error; make direct_judge miss it (fn) and structured
    # catch it (tp).  item-0 gold is correct; make full_system over-report (fp).
    for row in predictions:
        if (row.method, row.item_id) == ("direct_judge", "item-1"):
            row.assessment = _assessment(pilot, "item-1", True)
        if (row.method, row.item_id) == ("structured_judge", "item-1"):
            row.assessment = _assessment(pilot, "item-1", False, located=True)
        if (row.method, row.item_id) == ("full_system", "item-0"):
            row.assessment = _assessment(pilot, "item-0", False, located=True)
    run, report, config = _write_run(tmp_path, pilot, predictions)
    view = build_comparison_view(pilot, run, run_dir_display="x", report=report, config=config)
    items = {item["item_id"]: item for item in view["items"]}

    missed = items["item-1"]["methods"]["direct_judge"]
    assert missed["gold_comparison"]["reasoning_correct"] == "fn"
    assert missed["outcome"] == "fn"
    caught = items["item-1"]["methods"]["structured_judge"]
    assert caught["gold_comparison"]["reasoning_correct"] == "tp"
    over = items["item-0"]["methods"]["full_system"]
    assert over["gold_comparison"]["reasoning_correct"] == "fp"
    unknown = items["item-3"]["methods"]["direct_judge"]
    assert unknown["gold_comparison"]["reasoning_correct"] == "gold_unknown"

    # Divergence is computed across ok methods only, on the compared fields.
    assert "reasoning_correct" in items["item-1"]["disagreement"]["fields"]
    located = caught["assessment"]["first_faulty_location"]
    assert located["quote"] == "Return zero."
    refs = caught["assessment"]["references"]
    assert any(ref["target"].get("step_id") == "S1" for ref in refs)
    assert all(ref["verified"] for ref in refs)


def test_failure_and_missing_are_not_divergence(pilot, tmp_path):
    predictions = [
        row
        for row in _full_predictions(pilot, skip=("structured_judge", "item-2"))
        if not (row.method == "full_system" and row.item_id == "item-2")
    ]
    predictions.append(_prediction(pilot, "item-2", "full_system", status="provider_error"))
    run, report, config = _write_run(tmp_path, pilot, predictions)
    view = build_comparison_view(pilot, run, run_dir_display="x", report=report, config=config)
    item = next(entry for entry in view["items"] if entry["item_id"] == "item-2")
    assert item["methods"]["structured_judge"]["status"] == "missing"
    assert item["methods"]["structured_judge"]["usage"]["recorded"] is False
    assert item["methods"]["full_system"]["status"] == "provider_error"
    assert item["disagreement"]["any"] is False
    assert item["methods"]["structured_judge"]["outcome"] == "missing"
    assert item["methods"]["full_system"]["outcome"] == "failed"


def test_report_hash_mismatch_refused(pilot, tmp_path):
    run, _report, _config = _write_run(tmp_path, pilot, _full_predictions(pilot))
    # Tamper after the report was written: the pinned hash no longer matches.
    (run / "predictions.jsonl").write_bytes(b'{"tampered": true}\n')
    with pytest.raises(ComparisonSourceError, match="哈希"):
        load_method_comparison(
            tmp_path, Path("run"), REPO_ROOT / "data/manifests/process_pilot_v1.json"
        )


def test_missing_report_is_not_ready(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    with pytest.raises(ComparisonSourceError, match="尚未就绪"):
        load_method_comparison(
            tmp_path, Path("run"), REPO_ROOT / "data/manifests/process_pilot_v1.json"
        )


def test_duplicate_prediction_refused(pilot, tmp_path):
    rows = _full_predictions(pilot)
    rows.append(_prediction(pilot, "item-0", "direct_judge"))
    run, report, config = _write_run(tmp_path, pilot, rows)
    with pytest.raises(ComparisonSourceError, match="计分|duplicate|unexpected"):
        build_comparison_view(pilot, run, run_dir_display="x", report=report, config=config)


def test_stale_input_hash_refused(pilot, tmp_path):
    rows = _full_predictions(pilot)
    stale = rows[0].model_copy(update={"input_sha256": "0" * 64})
    rows[0] = stale
    run, report, config = _write_run(tmp_path, pilot, rows)
    with pytest.raises(ComparisonSourceError):
        build_comparison_view(pilot, run, run_dir_display="x", report=report, config=config)


def test_run_identity_mismatch_refused(pilot, tmp_path):
    run, report, config = _write_run(tmp_path, pilot, _full_predictions(pilot))
    bad_config = {**config, "methods": ["direct_judge"]}
    with pytest.raises(ComparisonSourceError, match="方法集合"):
        build_comparison_view(pilot, run, run_dir_display="x", report=report, config=bad_config)


def test_unrecorded_usage_stays_unrecorded(pilot, tmp_path):
    run, report, config = _write_run(tmp_path, pilot, _full_predictions(pilot), ledger=False)
    view = build_comparison_view(pilot, run, run_dir_display="x", report=report, config=config)
    for item in view["items"]:
        for method in item["methods"].values():
            assert method["usage"] == {"recorded": False}


def test_partial_run_status_is_disclosed(pilot, tmp_path):
    run, report, config = _write_run(tmp_path, pilot, _full_predictions(pilot))
    report = {**report, "status": "partial_execution"}
    view = build_comparison_view(pilot, run, run_dir_display="x", report=report, config=config)
    assert view["experiment"]["run_status"] == "partial_execution"
    assert any("partial_execution" in note for note in view["notes"])


# ------------------------------------------------------------ real pinned run

REAL_RUN = REPO_ROOT / "artifacts/experiments/process-pilot/mbpp12-run-20260909-v1"


@pytest.mark.skipif(not REAL_RUN.is_dir(), reason="pinned pilot run artifacts are not present")
def test_real_run_loads_and_is_consistent():
    view = load_method_comparison(REPO_ROOT)
    assert view["ok"] is True and view["data_kind"] == "real_experiment"
    assert view["experiment"]["run_status"] == "completed"
    assert view["experiment"]["item_count"] == 12
    assert view["experiment"]["identity_label"] == "历史三方法试点 · 2026-09-09"
    assert (
        view["experiment"]["run_dir"]
        == "artifacts/experiments/process-pilot/mbpp12-run-20260909-v1"
    )
    assert view["experiment"]["requests"]["dispatched"] == 36
    # Counts come from the verified labels, not from hardcoded expectations.
    summary = view["experiment"]["label_summary"]
    assert summary["correct"] + summary["incorrect"] + summary["unknown"] == 12
    for item in view["items"]:
        assert len(item["methods"]) == 3
        assert all(method["status"] == "ok" for method in item["methods"].values())
        for method in item["methods"].values():
            assert method["usage"]["requests"] == 1
            assert method["usage"]["input_tokens"] is not None
    blob = json.dumps(view, ensure_ascii=False)
    assert str(REPO_ROOT) not in blob
    assert "HY3_API_KEY" not in blob


@pytest.mark.skipif(not REAL_RUN.is_dir(), reason="pinned pilot run artifacts are not present")
def test_real_run_notes_keep_historical_pilot_honest():
    view = load_method_comparison(REPO_ROOT)
    notes = " ".join(view["notes"])
    # The pilot identity and its limits are stated on the page itself.
    assert "历史三方法试点 · 2026-09-09" in notes
    assert "12 条开发试点（8 正确、2 错误、2 unknown）" in notes
    assert "标注者独立性未核实" in notes
    # Location gold is zero: displayed locations are not localization accuracy.
    assert "不能被解释为定位准确率" in notes
    # The 2026-09-10 36-item validation used different methods: no merged ranking.
    assert "不能直接合并排名" in notes


# ------------------------------------------------------------- synthetic view


def test_synthetic_view_is_marked_and_covers_states():
    view = synthetic_method_comparison()
    assert view["data_kind"] == "synthetic_ui_test"
    assert "界面测试数据" in view["experiment"]["name"]
    statuses = {method["status"] for item in view["items"] for method in item["methods"].values()}
    assert {"ok", "provider_error", "parse_error", "missing"} <= statuses
    outcomes = {method["outcome"] for item in view["items"] for method in item["methods"].values()}
    assert {"tp", "tn", "fp", "fn"} & outcomes
    assert any(item["disagreement"]["any"] for item in view["items"])
    assert any(
        method["usage"]["recorded"] is False
        for item in view["items"]
        for method in item["methods"].values()
    )
    blob = json.dumps(view, ensure_ascii=False)
    assert "PRIVATE" not in blob and "HY3_API_KEY" not in blob


@pytest.mark.skipif(not REAL_RUN.is_dir(), reason="pinned pilot run artifacts are not present")
def test_synthetic_and_real_items_share_shape():
    real = load_method_comparison(REPO_ROOT)
    synthetic = synthetic_method_comparison()
    assert set(real) == set(synthetic)
    real_item, synth_item = real["items"][0], synthetic["items"][0]
    assert set(real_item) == set(synth_item)
    for method in real_item["methods"]:
        assert set(real_item["methods"][method]) == set(synth_item["methods"][method])
    assert set(real_item["methods"]["direct_judge"]["assessment"]) == set(
        synth_item["methods"]["direct_judge"]["assessment"]
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


def test_comparison_synthetic_endpoint(server):
    status, payload = _get(server, "/api/method-comparison?fixture=synthetic")
    assert status == 200
    assert payload["data_kind"] == "synthetic_ui_test"


def test_comparison_rejects_arbitrary_query(server):
    for query in ("?path=../../.env", "?fixture=real", "?fixture=synthetic&x=1", "?run=/etc"):
        status, payload = _get(server, f"/api/method-comparison{query}")
        assert status == 400, query
        assert "unsupported" in payload["error"]


@pytest.mark.skipif(not REAL_RUN.is_dir(), reason="pinned pilot run artifacts are not present")
def test_comparison_real_endpoint(server):
    status, payload = _get(server, "/api/method-comparison")
    assert status == 200
    assert payload["data_kind"] == "real_experiment"
    assert payload["experiment"]["item_count"] == 12
    assert str(REPO_ROOT) not in json.dumps(payload)
