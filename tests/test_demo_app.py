"""Tests for the local recording demo app.

Covers: the fixture-mode real pipeline run behind the page, honest Hy3-mode
failure without credentials, the public overview data sources (structured vs
published Markdown consistency), and the HTTP surface's safety boundaries.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tracejudge_hy3.demo_app.overview import (
    DISCLAIMER,
    load_public_overview,
    parse_overview_markdown,
)
from tracejudge_hy3.demo_app.preflight import demo_preflight
from tracejudge_hy3.demo_app.regression import build_regression_card
from tracejudge_hy3.demo_app.runner import run_demo
from tracejudge_hy3.demo_app.server import make_server
from tracejudge_hy3.demo_app.showcase import load_public_showcase

REPO_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_CAPTIONS = [
    "输入待解决任务",
    "生成结构化解答",
    "对齐需求、推理、代码与执行证据",
    "执行测试并检查边界条件",
    "定位首个错误步骤",
    "生成最小反例与可重放错误证书",
]


@pytest.fixture(autouse=True)
def _clear_hy3_env(monkeypatch):
    """Ensure Hy3 credentials from the developer's .env never leak into tests:
    real environment variables take precedence over the .env file."""

    monkeypatch.setenv("HY3_BASE_URL", "")
    monkeypatch.setenv("HY3_API_KEY", "")
    monkeypatch.setenv("HY3_MODEL", "")


# ---------------------------------------------------------------- run modes


def test_fixture_mode_runs_real_pipeline_and_finds_the_bug():
    payload = run_demo("fixture")

    assert payload["ok"] is True
    assert payload["mode"] == "fixture"
    assert payload["mode_note"] == "公开 Fixture；未调用真实 Hy3"
    assert payload["problem"]["problem_id"] == "safe_mean"
    assert payload["provider"]["name"] == "mock"
    assert payload["provider"]["sandbox"] == "trusted-local"

    # Real execution: visible tests pass, the empty-input cases fail.
    categories = payload["execution"]["categories"]
    assert categories["visible"] == {"total": 2, "passed": 2}
    assert categories["hidden"]["passed"] < categories["hidden"]["total"]
    failing = [r for r in payload["execution"]["results"] if not r["passed"]]
    assert any(r["exception_type"] == "ZeroDivisionError" for r in failing)

    assessment = payload["assessment"]
    assert assessment["functional_correct"] is False
    assert assessment["process_correct"] is False
    assert assessment["first_faulty_step"] == "S1"
    assert assessment["violated_requirement"] == "R1"
    assert assessment["error_type"] == "A01_PLAN_CODE_MISMATCH"
    assert assessment["code_span"]

    assert payload["counterexample"]["args"] == [[]]
    assert payload["certificate"]["verdict"] == "confirmed_bug"

    replay = payload["replay"]
    assert replay["applicable"] is True
    assert replay["reproduced"] is True

    artifact = payload["artifact_relpath"]
    assert artifact.startswith("artifacts/")
    assert not Path(artifact).is_absolute()
    assert (REPO_ROOT / artifact).is_file()


def test_fixture_payload_never_contains_reference_code_or_absolute_paths():
    payload = run_demo("fixture")
    blob = json.dumps(payload, ensure_ascii=False)
    assert "reference_code" not in blob
    assert str(REPO_ROOT) not in blob
    # The allowlisted payload does not expose hidden/challenge test internals
    # beyond the public fixture's own executed inputs.
    assert payload["problem"]["source"] == "self_constructed_mvp_fixture"


def test_unknown_mode_is_rejected_without_running():
    payload = run_demo("hy3; rm -rf /")
    assert payload["ok"] is False
    assert payload["error_type"] == "DemoModeError"


def test_hy3_mode_without_config_fails_honestly():
    payload = run_demo("hy3")
    assert payload["ok"] is False
    assert payload["mode"] == "hy3"
    assert "未配置" in payload["error"]
    assert "Mock" not in payload["error"]


def test_hy3_error_payload_contains_no_secret_values(monkeypatch):
    monkeypatch.setenv("HY3_BASE_URL", "https://hy3-internal.example.invalid/v1")
    monkeypatch.setenv("HY3_API_KEY", "sk-test-secret-0000000000")
    monkeypatch.setenv("HY3_MODEL", "")  # still incomplete: must fail before any network call
    payload = run_demo("hy3")
    blob = json.dumps(payload, ensure_ascii=False)
    assert payload["ok"] is False
    assert "sk-test-secret-0000000000" not in blob
    assert "hy3-internal.example.invalid" not in blob


# ------------------------------------------------------------------ overview


def test_overview_markdown_parses_published_numbers():
    text = (REPO_ROOT / "docs/releases/phase4/phase4_contest_results_overview_v1.md").read_text(
        encoding="utf-8"
    )
    parsed = parse_overview_markdown(text)
    assert parsed["trace_count"] == 57
    assert parsed["pair_count"] == 285
    assert parsed["best_detection"]["numerator"] == 56
    assert parsed["best_detection"]["denominator"] == 57
    assert parsed["best_detection"]["method"] == "Four-layer Structured"
    assert parsed["full_false_positive_rate"]["numerator"] == 1
    assert parsed["full_false_positive_rate"]["denominator"] == 43
    review = parsed["human_review"]
    assert (review["primary_labeled"], review["primary_total"]) == (57, 57)
    assert (review["second_completed"], review["second_planned"]) == (0, 20)
    assert review["agreement_status"] == "not_computed"
    assert parsed["difficulty"] == [
        {"tier": "easy-proxy", "included": 14, "passed": 14},
        {"tier": "medium-proxy", "included": 14, "passed": 13},
        {"tier": "hard-proxy", "included": 14, "passed": 13},
    ]


def test_load_public_overview_is_consistent_across_sources():
    overview = load_public_overview(REPO_ROOT)
    assert overview["trace_count"] == 57
    assert overview["pair_count"] == 285
    assert overview["source"] in ("structured_artifact", "published_markdown")
    # When the frozen structured artifacts exist locally, load_public_overview
    # itself refuses to return numbers that disagree with the Markdown.
    assert overview["disclaimer"] == DISCLAIMER
    assert "探索性" in overview["disclaimer"]


def test_public_showcase_has_three_hash_bound_cases():
    showcase = load_public_showcase(REPO_ROOT)
    assert showcase["source"]["contains_private_material"] is False
    assert [item["case_id"] for item in showcase["cases"]] == [
        "reasoning_swap",
        "boundary_deletion",
        "equivalent_implementation",
    ]
    by_id = {item["case_id"]: item for item in showcase["cases"]}
    assert by_id["reasoning_swap"]["functional_correct"] is True
    assert by_id["reasoning_swap"]["process_correct"] is False
    assert by_id["boundary_deletion"]["execution"]["selected_case"]["actual"] == (
        "ZeroDivisionError"
    )
    assert by_id["equivalent_implementation"]["process_correct"] is True
    assert by_id["equivalent_implementation"]["certificate"]["verdict"] == ("unverified_suspicion")


def test_versioned_regression_card_uses_public_frozen_breakdown():
    card = build_regression_card(REPO_ROOT)
    assert card["fixture_count"] == 15
    assert card["metrics"]["error_detection"] == {
        "numerator": 12,
        "denominator": 12,
        "scope": "Full TraceJudge 在 12 条公开有错反事实上的冻结结果",
    }
    assert card["metrics"]["false_positive_count"]["count"] == 1
    assert card["metrics"]["counterexample_replay_pass"]["numerator"] == 1
    assert card["metrics"]["exact_first_step_localization"]["status"] == "not_computable"


def test_demo_preflight_required_checks_pass():
    result = demo_preflight(REPO_ROOT)
    assert result["ready"] is True
    assert all(item["ok"] for item in result["checks"] if item["required"])


# --------------------------------------------------------------- HTTP surface


@pytest.fixture()
def server():
    httpd = make_server(port=0, repo_root=REPO_ROOT)
    import threading

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(base: str, path: str) -> tuple[int, dict | bytes, dict]:
    request = urllib.request.Request(base + path, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            body = resp.read()
            headers = dict(resp.headers.items())
            if "application/json" in headers.get("Content-Type", ""):
                return resp.status, json.loads(body), headers
            return resp.status, body, headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers.items())


def _post(base: str, path: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        base + path,
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_server_binds_localhost_only():
    httpd = make_server(port=0, repo_root=REPO_ROOT)
    try:
        assert httpd.server_address[0] == "127.0.0.1"
    finally:
        httpd.server_close()


def test_index_page_served_with_security_headers(server):
    status, body, headers = _get(server, "/?recording=1")
    assert status == 200
    html = body.decode("utf-8")
    assert "TRACEJUDGE-HY3" in html
    assert "典型案例" in html
    assert "VERSIONED REGRESSION CARD" in html
    for caption in EXPECTED_CAPTIONS:
        assert caption in html
    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert "default-src 'self'" in headers.get("Content-Security-Policy", "")


def test_unknown_and_traversal_paths_rejected(server):
    assert _get(server, "/nonexistent")[0] == 404
    assert _get(server, "/static/../server.py")[0] == 404
    assert _get(server, "/../.env")[0] == 404
    assert _get(server, "/.env")[0] == 404
    assert _get(server, "/api/export/../../.env")[0] == 404


def test_status_endpoint_reports_modes_without_secrets(server):
    status, payload, _headers = _get(server, "/api/status")
    assert status == 200
    assert payload["modes"]["fixture"]["available"] is True
    assert payload["modes"]["hy3"]["available"] is False
    assert payload["modes"]["hy3"]["configured"] is False
    assert payload["problem"]["problem_id"] == "safe_mean"
    assert str(REPO_ROOT) not in json.dumps(payload)


def test_overview_endpoint(server):
    status, payload, _headers = _get(server, "/api/overview")
    assert status == 200
    assert payload["trace_count"] == 57


def test_showcase_regression_and_svg_endpoints(server):
    status, showcase, _headers = _get(server, "/api/showcase")
    assert status == 200
    assert len(showcase["cases"]) == 3

    status, card, _headers = _get(server, "/api/regression")
    assert status == 200
    assert card["metrics"]["error_detection"]["numerator"] == 12

    status, svg, headers = _get(server, "/showcase/reasoning-swap.svg")
    assert status == 200
    assert b"reasoning_swap" in svg
    assert "image/svg+xml" in headers["Content-Type"]


def test_run_endpoint_rejects_bad_mode(server):
    status, payload = _post(server, "/api/run", {"mode": "hy3 --allow-unsafe-local-exec"})
    assert status == 400
    assert "mode" in payload["error"]


def test_full_fixture_run_over_http(server):
    status, payload = _post(server, "/api/run", {"mode": "fixture"})
    assert status == 202
    run_id = payload["run_id"]

    result = None
    for _ in range(150):
        _status, state, _headers = _get(server, f"/api/run/{run_id}")
        if state["status"] == "done":
            result = state["result"]
            break
        time.sleep(0.2)
    assert result is not None, "fixture run did not complete in time"
    assert result["ok"] is True
    assert result["certificate"]["verdict"] == "confirmed_bug"
    assert result["replay"]["reproduced"] is True

    status, exported, headers = _get(server, f"/api/export/{run_id}/result.json")
    assert status == 200
    assert exported["kind"] == "tracejudge_demo_result_export"
    assert exported["run_id"] == run_id
    assert "attachment" in headers["Content-Disposition"]

    status, certificate, _headers = _get(server, f"/api/export/{run_id}/certificate.json")
    assert status == 200
    assert certificate["certificate"]["verdict"] == "confirmed_bug"
    assert certificate["replay"]["reproduced"] is True

    status, report, headers = _get(server, f"/api/export/{run_id}/report.html")
    assert status == 200
    assert b"TraceJudge-Hy3" in report
    assert b"confirmed_bug" in report
    assert "attachment" in headers["Content-Disposition"]
