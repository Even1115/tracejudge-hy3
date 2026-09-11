"""Static checks for the read-only "60 秒看懂项目" guided tour.

The tour is a pure front-end addition over existing views.  These tests verify
that the served page contains the tour entry, bar and four stops, that the
stops only reuse existing read-only components (no POST /api/run, no model
calls, no candidate execution), and that the tour coordinates with the
recording presenter instead of competing with it.
"""

from __future__ import annotations

import threading
import urllib.request
from pathlib import Path

import pytest

from tracejudge_hy3.demo_app.server import make_server

REPO_ROOT = Path(__file__).resolve().parents[1]

TOUR_STOP_KEYS = [
    "tuple_str_int",
    "find_char_long",
    "certificate_replay",
    "current_validation",
]

TOUR_NOTES = [
    "官方测试通过，并不证明解答中的每项承诺都成立。",
    "系统把计划与代码的差异关联到具体步骤和代码位置。",
    "有执行支持的错误，可以保存反例并核查已有重放结果。",
    "检测、精确定位与稳定性分别验证，结果同时披露收益和不足。",
]


@pytest.fixture()
def server():
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


def _get_text(base: str, path: str) -> str:
    with urllib.request.urlopen(base + path, timeout=30) as resp:
        return resp.read().decode("utf-8")


def _tour_block(app_js: str) -> str:
    """The tour implementation: from the stop table to the init section,
    excluding the descriptive header comment."""
    start = "const TOUR_STOPS"
    end = "// ----------------------------------------------------------------- init"
    assert start in app_js and end in app_js
    return app_js.split(start, 1)[1].split(end, 1)[0]


def test_index_page_exposes_tour_entry_and_bar(server):
    html = _get_text(server, "/")
    assert 'id="open-tour"' in html
    assert "60 秒看懂项目" in html
    assert 'id="tour-bar"' in html
    assert 'id="tour-progress"' in html
    assert 'id="tour-title"' in html
    assert 'id="tour-status"' in html
    for button_id in ("tour-prev", "tour-next", "tour-exit"):
        assert f'id="{button_id}"' in html
    # The bar exists in both normal and recording modes (same served page).
    assert _get_text(server, "/?recording=1").count('id="tour-bar"') == 1


def test_tour_stops_and_one_line_explanations(server):
    app_js = _get_text(server, "/static/app.js")
    tour = _tour_block(app_js)
    for key in TOUR_STOP_KEYS:
        assert f'key: "{key}"' in tour
    for note in TOUR_NOTES:
        assert note in tour
    # Every stop reuses an existing view or loader rather than duplicating data.
    assert 'loadFeaturedCase("tuple_str_int")' in tour
    assert 'loadFeaturedCase("find_char_long")' in tour
    assert 'selectCase("boundary_deletion")' in tour
    assert 'showView("demo")' in tour


def test_tour_never_starts_runs_or_model_calls(server):
    app_js = _get_text(server, "/static/app.js")
    tour = _tour_block(app_js)
    assert "/api/run" not in tour
    assert "POST" not in tour
    assert "startRun" not in tour


def test_tour_coordinates_with_presenter_and_url_params(server):
    app_js = _get_text(server, "/static/app.js")
    # The presenter bar is hidden while the tour is open (no competing bars).
    assert '$("presenter").hidden = true' in app_js
    assert "tourActive() || view !==" in app_js
    # ?tour=1 takes priority over ?case=; ?recording=1 combines with either.
    init_tail = app_js.rsplit("await Promise.all", 1)[1]
    assert 'params.get("tour") === "1"' in init_tail
    assert "startTour()" in init_tail
    assert init_tail.index('params.get("tour")') < init_tail.index('params.get("case")')


def test_tour_has_degraded_states_without_stale_content(server):
    app_js = _get_text(server, "/static/app.js")
    tour = _tour_block(app_js)
    assert "未用其他内容顶替" in tour
    assert "未伪造重放结果" in tour
    assert "未展示上一站内容顶替" in tour
    # Stations remain reachable even when a source fails.
    assert tour.count("仍可前进或退出导览") >= 2
