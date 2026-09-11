"""Read-only regression for the two published process case excerpts."""

from __future__ import annotations

import json
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tracejudge_hy3.demo_app.featured_cases import (
    BUNDLE,
    REVIEW_DOCS,
    FeaturedCaseError,
    load_featured_cases,
)
from tracejudge_hy3.demo_app.server import make_server

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def published(tmp_path):
    dest = tmp_path / BUNDLE
    dest.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / BUNDLE, dest)
    for relative in REVIEW_DOCS.values():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    return tmp_path


def test_published_bundle_without_local_artifacts(published):
    data = load_featured_cases(published)
    assert [case["id"] for case in data["cases"]] == ["tuple_str_int", "find_char_long"]
    assert {s["verification"] for s in data["sources"].values()} == {"published_excerpt_only"}
    assert str(ROOT) not in json.dumps(data)
    assert "/mnt/" not in json.dumps(data)


def test_tuple_source_claim_and_functional_scope(published):
    case = load_featured_cases(published)["cases"][0]
    assert case["functional"]["base_status"] == case["functional"]["plus_status"] == "pass"
    assert case["claim"]["text"] == case["solution"]["edge_cases_considered"][1]
    assert case["assessment"]["first_faulty_step"] is None
    assert case["assessment"]["plan_code_aligned"] is True
    assert case["assessment"]["error_type"] == "P01_ALGORITHM_ERROR"
    assert "预测后" in " ".join(case["limitations"])
    assert "未执行" in case["analysis"]


def test_probe_code_location_does_not_become_step_hit(published):
    case = load_featured_cases(published)["cases"][1]
    assert ">= 4" in case["claim"]["text"]
    assert "> 4" in case["solution"]["code"].splitlines()[1]
    assert case["functional"]["base_status"] is None
    assert case["assessment"]["first_faulty_step"] == "S2"
    for judgment in case["judgments"]:
        a = judgment["assessment"]
        assert a["first_faulty_step"] is None
        assert a["first_faulty_location"]["code_span"] == "L2"
        assert a["first_faulty_location"]["quote"] in case["solution"]["code"]
        assert a["functional_correct"] is None


@pytest.mark.parametrize("mutation", ["number", "quote", "path", "malformed"])
def test_tampered_bundle_refused(published, mutation):
    target = published / BUNDLE
    data = json.loads(target.read_bytes())
    if mutation == "number":
        data["cases"][0]["functional"]["base_status"] = "fail"
    elif mutation == "quote":
        data["cases"][1]["claim"]["text"] = "Forged quote"
    elif mutation == "path":
        data["sources"]["tuple_input"]["path"] = "../.env"
    target.write_text("{" if mutation == "malformed" else json.dumps(data), encoding="utf-8")
    with pytest.raises(FeaturedCaseError):
        load_featured_cases(published)


def test_bundle_survives_git_line_endings(published):
    target = published / BUNDLE
    target.write_bytes(target.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert load_featured_cases(published)["ok"]


def test_existing_original_drift_refuses_even_with_valid_bundle(published):
    data = json.loads((published / BUNDLE).read_bytes())
    relative = Path(data["sources"]["tuple_input"]["path"])
    target = published / relative
    target.parent.mkdir(parents=True)
    target.write_text('{"item_id":"wrong"}\n', encoding="utf-8")
    with pytest.raises(FeaturedCaseError):
        load_featured_cases(published)


def test_local_source_content_verified_when_available():
    original = (
        ROOT
        / "artifacts/process-annotations/mbpp120-process-v1/development/annotator_a/items.jsonl"
    )
    if not original.exists():
        pytest.skip("Optional local artifacts absent")
    data = load_featured_cases(ROOT)
    assert data["sources"]["tuple_input"]["verification"] == "local_content_verified"


def test_http_routes_and_refusal(published):
    httpd = make_server(port=0, repo_root=published)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    def get(route):
        try:
            with urllib.request.urlopen(base + route, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    try:
        status, body = get("/api/featured-process-cases")
        assert status == 200 and json.loads(body)["ok"]
        assert get("/api/featured-process-cases?path=../../.env")[0] == 400
        for case_id in REVIEW_DOCS:
            assert get("/docs/featured-process/" + case_id)[0] == 200
        assert get("/docs/featured-process/../../.env")[0] == 404
        assert get("/docs/featured-process/tuple_str_int?path=.env")[0] == 400
        (published / BUNDLE).unlink()
        status, body = get("/api/featured-process-cases")
        assert status == 503
        assert str(published) not in body.decode()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
