"""Tests for the 2026-09-10 latest-validation adapter and its endpoints.

No provider calls, no candidate execution.  Integration tests exercise the
real pinned artifacts when they exist locally and skip otherwise; refusal
paths run against temporary repos with tampered copies.
"""

from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tracejudge_hy3.demo_app.current_validation import (
    ABLATION_TALLY_FILE,
    DEV36_RESCORE_FILE,
    LOCATION_COMPARISON_FILE,
    LOCATION_REVIEW_FILE,
    PILOT_CONFIG,
    SUMMARY_RELATIVE_PATH,
    CurrentValidationError,
    build_current_validation_summary,
    load_current_validation_summary,
    load_repetition_tally,
)
from tracejudge_hy3.demo_app.server import make_server

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = (
    DEV36_RESCORE_FILE,
    ABLATION_TALLY_FILE,
    LOCATION_COMPARISON_FILE,
    LOCATION_REVIEW_FILE,
)
needs_artifacts = pytest.mark.skipif(
    not all((REPO_ROOT / relative).is_file() for relative in ARTIFACTS),
    reason="pinned latest-validation artifacts are not present",
)


def _write_summary_repo(root: Path, summary: dict, *, artifacts: bool = False) -> Path:
    """A minimal fake repo: the tracked summary plus, optionally, copies of
    the pinned artifacts and the pilot config at their configured paths."""

    target = root / SUMMARY_RELATIVE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if artifacts:
        for relative in (*ARTIFACTS, PILOT_CONFIG):
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO_ROOT / relative, destination)
    return root


def _tracked_summary() -> dict:
    return json.loads((REPO_ROOT / SUMMARY_RELATIVE_PATH).read_text(encoding="utf-8"))


# --------------------------------------------------------------- generation


@needs_artifacts
def test_tracked_summary_matches_regeneration():
    regenerated = build_current_validation_summary(REPO_ROOT)
    assert regenerated == _tracked_summary()
    assert regenerated["schema"] == "tracejudge-current-validation-summary-v1"
    assert regenerated["calls_made"] == 0 and regenerated["candidate_executions"] == 0


@needs_artifacts
def test_summary_records_expected_counts():
    summary = _tracked_summary()
    dev = summary["experiments"]["dev36_label_rescore"]
    assert (dev["accuracy_numerator"], dev["accuracy_denominator"]) == (33, 36)
    assert (dev["tp"], dev["fp"], dev["tn"], dev["fn"]) == (2, 0, 31, 3)
    assert (dev["error_recall_numerator"], dev["error_recall_denominator"]) == (2, 5)
    assert (dev["false_positive_numerator"], dev["false_positive_denominator"]) == (0, 31)

    rep = summary["experiments"]["ablation_repetition"]
    assert rep["rep1_accuracy"] == {
        "ablation_a": [8, 10],
        "ablation_b": [8, 10],
        "ablation_c": [8, 10],
        "ablation_d": [9, 10],
    }
    assert rep["max_product_detection"] == {
        "ablation_a": [1, 5],
        "ablation_b": [0, 4],
        "ablation_c": [0, 5],
        "ablation_d": [2, 5],
    }
    assert rep["division_elements_total"] == [0, 20]

    probes = summary["experiments"]["location_probes"]
    assert (probes["detection_numerator"], probes["detection_denominator"]) == (3, 3)
    assert (probes["first_layer_numerator"], probes["first_layer_denominator"]) == (3, 3)
    assert (probes["first_step_numerator"], probes["first_step_denominator"]) == (1, 3)
    assert (
        probes["structured_location_exact_numerator"],
        probes["structured_location_exact_denominator"],
    ) == (0, 3)
    assert (
        probes["supplementary_citation_numerator"],
        probes["supplementary_citation_denominator"],
    ) == (1, 3)
    assert probes["requests"] == {"baseline": 5, "assumption_audit": 3}

    # The three experiments keep separate denominators: nothing merges
    # 36 / 12 / 3 into one count.
    counts = {
        key: summary["experiments"][key]["item_count"]
        for key in ("dev36_label_rescore", "ablation_repetition", "location_probes")
    }
    assert counts == {"dev36_label_rescore": 36, "ablation_repetition": 12, "location_probes": 3}


# ------------------------------------------------------------------ loading


@needs_artifacts
def test_load_verifies_against_local_artifacts():
    payload = load_current_validation_summary(REPO_ROOT)
    assert payload["ok"] is True
    assert payload["verification"] == "artifacts_verified"
    assert payload["title"] == "2026-09-10 最新补充验证"
    cards = {card["id"]: card for card in payload["experiments"]}
    assert set(cards) == {"dev36_label_rescore", "ablation_repetition", "location_probes"}
    dev_lines = " ".join(cards["dev36_label_rescore"]["lines"])
    assert "33/36" in dev_lines and "2/0/31/3" in dev_lines
    assert "2/5" in dev_lines and "0/31" in dev_lines
    rep_lines = " ".join(cards["ablation_repetition"]["lines"])
    assert "9/10" in rep_lines and "8/10" in rep_lines
    assert "2/5" in rep_lines and "0/4" in rep_lines and "0/20" in rep_lines
    probe_lines = " ".join(cards["location_probes"]["lines"])
    assert "3/3" in probe_lines and "1/3" in probe_lines and "0/3" in probe_lines
    # Every card carries limitations; global limitations disclose the held-out
    # set and the no-merging rule.
    assert all(card["limitations"] for card in cards.values())
    global_text = " ".join(payload["global_limitations"])
    assert "81 条保留集" in global_text and "不合并" in global_text
    assert payload["overview_entry"]["href"] == "/docs/contest-results-overview"
    assert payload["overview_entry"]["repo_path"] == "docs/contest_results_overview.md"
    blob = json.dumps(payload, ensure_ascii=False)
    assert str(REPO_ROOT) not in blob and "/mnt/" not in blob
    assert "HY3_API_KEY" not in blob and "Authorization" not in blob


def test_fresh_clone_serves_tracked_summary_only(tmp_path):
    _write_summary_repo(tmp_path, _tracked_summary())
    payload = load_current_validation_summary(tmp_path)
    assert payload["ok"] is True
    assert payload["verification"] == "tracked_summary_only"
    cards = {card["id"]: card for card in payload["experiments"]}
    assert "33/36" in " ".join(cards["dev36_label_rescore"]["lines"])


def test_missing_summary_refused(tmp_path):
    with pytest.raises(CurrentValidationError):
        load_current_validation_summary(tmp_path)


def test_schema_error_refused(tmp_path):
    summary = {**_tracked_summary(), "schema": "something-else"}
    _write_summary_repo(tmp_path, summary)
    with pytest.raises(CurrentValidationError, match="不是已知"):
        load_current_validation_summary(tmp_path)


def test_internally_inconsistent_counts_refused(tmp_path):
    summary = _tracked_summary()
    summary["experiments"]["dev36_label_rescore"]["accuracy_numerator"] = 34
    _write_summary_repo(tmp_path, summary)
    with pytest.raises(CurrentValidationError, match="不一致"):
        load_current_validation_summary(tmp_path)


def test_merged_denominators_refused(tmp_path):
    # A summary that quietly mixes the 36/12/3 experiments into one
    # denominator must not pass validation.
    summary = _tracked_summary()
    summary["experiments"]["location_probes"]["item_count"] = 51
    _write_summary_repo(tmp_path, summary)
    with pytest.raises(CurrentValidationError, match="身份"):
        load_current_validation_summary(tmp_path)


@needs_artifacts
def test_count_drift_against_artifacts_refused(tmp_path):
    # Internally consistent but disagrees with the artifact re-extraction.
    summary = _tracked_summary()
    summary["experiments"]["location_probes"]["first_step_numerator"] = 2
    _write_summary_repo(tmp_path, summary, artifacts=True)
    with pytest.raises(CurrentValidationError, match="重新抽取"):
        load_current_validation_summary(tmp_path)


@needs_artifacts
def test_hash_mismatch_refused(tmp_path):
    _write_summary_repo(tmp_path, _tracked_summary(), artifacts=True)
    target = tmp_path / LOCATION_REVIEW_FILE
    target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises(CurrentValidationError, match="SHA-256"):
        load_current_validation_summary(tmp_path)


@needs_artifacts
def test_partial_artifacts_refused(tmp_path):
    _write_summary_repo(tmp_path, _tracked_summary())
    destination = tmp_path / LOCATION_REVIEW_FILE
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO_ROOT / LOCATION_REVIEW_FILE, destination)
    with pytest.raises(CurrentValidationError, match="部分"):
        load_current_validation_summary(tmp_path)


# ------------------------------------------------------- repetition loader


@needs_artifacts
def test_repetition_tally_matches_bound_source():
    tally = load_repetition_tally(REPO_ROOT)
    assert tally["available"] is True
    assert tally["max_product_detection"] == {
        "ablation_a": [1, 5],
        "ablation_b": [0, 4],
        "ablation_c": [0, 5],
        "ablation_d": [2, 5],
    }
    assert tally["division_elements_total"] == [0, 20]
    assert tally["rep1_accuracy"]["ablation_d"] == [9, 10]
    assert tally["rep1_accuracy"]["ablation_a"] == [8, 10]
    lines = " ".join(tally["lines"])
    assert "2/5" in lines and "0/20" in lines and "未观察到稳定的 D 优势" in lines


def test_repetition_tally_reports_source_counts_without_substitution(tmp_path):
    source = json.loads((REPO_ROOT / ABLATION_TALLY_FILE).read_text(encoding="utf-8"))
    source["items"]["item-e5b9ff02f8bd9e3f3b61"]["conditions"]["ablation_d"]["merged"][
        "detection_frequency"
    ] = {"numerator": 5, "denominator": 5, "value": 1.0}
    target = tmp_path / ABLATION_TALLY_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(source, ensure_ascii=False) + "\n", encoding="utf-8")
    config_target = tmp_path / PILOT_CONFIG
    config_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO_ROOT / PILOT_CONFIG, config_target)
    tally = load_repetition_tally(tmp_path)
    # The loader reports the tampered file's own numbers; it never substitutes
    # remembered values.  (The summary endpoint would refuse this drift via
    # the SHA-256 pin -- covered by test_hash_mismatch_refused.)
    assert tally["max_product_detection"]["ablation_d"] == [5, 5]


# ------------------------------------------------------------------ HTTP


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
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@needs_artifacts
def test_current_validation_endpoint(server):
    status, body = _get(server, "/api/current-validation-summary")
    assert status == 200
    payload = json.loads(body)
    assert payload["ok"] is True
    assert payload["verification"] == "artifacts_verified"
    blob = body.decode("utf-8")
    assert str(REPO_ROOT) not in blob and "/mnt/" not in blob


def test_current_validation_endpoint_unavailable(tmp_path):
    import threading

    httpd = make_server(port=0, repo_root=tmp_path)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        status, body = _get(f"http://{host}:{port}", "/api/current-validation-summary")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
    assert status == 503
    payload = json.loads(body)
    assert payload["ok"] is False
    assert payload["error"] == "最新补充验证暂不可用；历史冻结结果仍可查看"


def test_current_validation_rejects_query(server):
    status, _body = _get(server, "/api/current-validation-summary?path=../../.env")
    assert status == 400


def test_contest_overview_route(server):
    status, body = _get(server, "/docs/contest-results-overview")
    assert status == 200
    text = body.decode("utf-8")
    assert "成果总览" in text and "2026-09-10" in text


def test_contest_overview_route_rejects_query_and_siblings(server):
    status, _body = _get(server, "/docs/contest-results-overview?path=x")
    assert status == 400
    status, _body = _get(server, "/docs/../.env")
    assert status == 404
    status, _body = _get(server, "/docs/contest_results_overview.md")
    assert status == 404
