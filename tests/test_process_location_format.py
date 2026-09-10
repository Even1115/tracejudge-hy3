"""Versioned format and supplementary metric regressions, with no live calls."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_process_pilot_ablation import ASSESSMENT_OK, CONFIG_SHA, _completion, _kwargs, _pilot
from test_process_pilot_ablation import transport as transport

from tracejudge_hy3.process_eval_v2.ablation import condition_fingerprints, rubric_contract
from tracejudge_hy3.process_eval_v2.ablation_live import _run_ablation
from tracejudge_hy3.process_eval_v2.location_format import SPAN_PATTERN, validate_location_format
from tracejudge_hy3.process_eval_v2.location_review import citation_coverage, review_locations
from tracejudge_hy3.process_eval_v2.method_validation import (
    compare_methods,
    load_method_run,
    prepare_validation,
)
from tracejudge_hy3.schemas.evaluation import ProcessAssessment

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "artifacts/experiments/process-method-v2"


def test_historical_fingerprints_and_comparison_are_unchanged():
    for rubric, name in (("baseline", "baseline"), ("assumption_audit_v1", "assumptions")):
        identity = json.loads((BASE / f"probes3-{name}-20260910-v2/run-config.json").read_bytes())
        assert (
            condition_fingerprints(rubric)["ablation_a"]
            == identity["condition_fingerprints"]["ablation_a"]
        )
    pilot, sha = prepare_validation(ROOT, "probes")
    result = compare_methods(
        pilot, sha, BASE / "probes3-baseline-20260910-v2", BASE / "probes3-assumptions-20260910-v2"
    )
    assert result == json.loads(
        (BASE / "probes3-comparison-20260910-v2/comparison.json").read_bytes()
    )


@pytest.mark.parametrize(
    "old,new",
    [("baseline", "baseline_location_v2"), ("assumption_audit_v1", "assumption_audit_location_v2")],
)
def test_new_json_schema_describes_and_constrains_every_span(old, new):
    old_prompt, old_schema, _ = rubric_contract(old)
    prompt, schema, _ = rubric_contract(new)
    assert "定位输出规范 location_v2" in prompt and "定位输出规范 location_v2" not in old_prompt
    assert condition_fingerprints(new) != condition_fingerprints(old)

    def check(node):
        found = 0
        if isinstance(node, dict):
            if "code_span" in node.get("properties", {}):
                span = node["properties"]["code_span"]
                assert span["anyOf"] == [
                    {"type": "string", "pattern": SPAN_PATTERN},
                    {"type": "null"},
                ]
                assert span["examples"] == ["L3", "L3-L5", None]
                found += 1
            found += sum(check(value) for value in node.values())
        elif isinstance(node, list):
            found += sum(check(value) for value in node)
        return found

    assert check(schema) == 2  # top-level assessment and shared FaultLocation (also used by checks)
    assert rubric_contract(old)[1] == old_schema


@pytest.mark.parametrize("span", ["return 0", "3-5", "L2-L1", "L99", "L0"])
def test_new_top_level_span_rejects_text_reversed_or_out_of_bounds(span):
    assessment = ProcessAssessment.model_validate_json(ASSESSMENT_OK)
    assessment.code_span = span
    with pytest.raises(ValueError, match="code_span"):
        validate_location_format(assessment, _pilot(1).inputs["item-0"])


@pytest.mark.parametrize("span", [None, "L1", "L1-L2"])
def test_new_span_accepts_null_and_real_line_ranges(span):
    assessment = ProcessAssessment.model_validate_json(ASSESSMENT_OK)
    assessment.code_span = span
    validate_location_format(assessment, _pilot(1).inputs["item-0"])


@pytest.mark.parametrize("rubric", ["baseline_location_v2", "assumption_audit_location_v2"])
def test_bad_top_level_span_uses_existing_repair_budget(tmp_path, transport, rubric):
    tmp_path = tmp_path / "run"
    good = json.loads(ASSESSMENT_OK)
    bad = {**good, "code_span": "return 0"}
    if rubric.startswith("assumption"):
        bad, good = ({"assessment": value, "checks": []} for value in (bad, good))
    requests = transport([_completion(json.dumps(bad)), _completion(json.dumps(good))])
    report = _run_ablation(
        _pilot(1),
        CONFIG_SHA,
        tmp_path,
        rubric=rubric,
        conditions=("ablation_a",),
        **_kwargs(max_requests=2),
    )
    assert report["judgments"] == {"ablation_a": {"ok": 1}}
    assert report["budget"]["consumed_requests"] == len(requests) == 2
    diagnostic = json.loads((tmp_path / "diagnostics.jsonl").read_bytes())
    assert "code_span" in diagnostic["error_message_redacted"]


def test_new_methods_compare_and_reject_old_rubric_identity(tmp_path, transport):
    pilot = _pilot(1)
    for name, rubric in (
        ("base", "baseline_location_v2"),
        ("audit", "assumption_audit_location_v2"),
    ):
        value = json.loads(ASSESSMENT_OK)
        if name == "audit":
            value = {"assessment": value, "checks": []}
        transport([_completion(json.dumps(value))])
        _run_ablation(
            pilot,
            CONFIG_SHA,
            tmp_path / name,
            rubric=rubric,
            conditions=("ablation_a",),
            **_kwargs(max_requests=2),
        )
    result = compare_methods(
        pilot, CONFIG_SHA, tmp_path / "base", tmp_path / "audit", location_v2=True
    )
    assert result["paired"]["judge_raw"]["transitions"] == {"tn->tn": 1}
    with pytest.raises(ValueError, match="rubric"):
        compare_methods(pilot, CONFIG_SHA, tmp_path / "base", tmp_path / "audit")


def test_supplementary_metric_preserves_exact_scores_and_no_cross_source_credit():
    result = review_locations(
        ROOT, BASE / "probes3-baseline-20260910-v2", BASE / "probes3-assumptions-20260910-v2"
    )
    for method, numerator in (("baseline", 1), ("assumption_audit", 2)):
        for view in ("judge_raw", "rule_merged"):
            assert (
                result["original_comparison"][method]["views"][view]["ablation_a"]["localization"][
                    "structured_location_exact"
                ]["numerator"]
                == 0
            )
            metric = result["supplementary"][method][view]
            assert metric["coverage"] == {
                "numerator": numerator,
                "denominator": 3,
                "value": numerator / 3,
            }
            assert (
                metric["per_item"]["probe-c37f9befe938d0bc2a6e"]["reason"]
                == "different_source_address"
            )


def test_coverage_handles_missing_empty_gold_and_wrong_layer_without_credit():
    pilot, sha = prepare_validation(ROOT, "probes")
    assert citation_coverage(pilot, [], view="judge_raw")["coverage"]["denominator"] == 3
    assert (
        citation_coverage(replace(pilot, labels={}), [], view="judge_raw")["coverage"]["value"]
        is None
    )
    rows, _, _ = load_method_run(
        pilot, sha, BASE / "probes3-assumptions-20260910-v2", "assumption_audit_v1"
    )
    changed = [row.model_copy(deep=True) for row in rows]
    for row in changed:
        row.judge_raw.first_faulty_layer = "execution"
    metric = citation_coverage(pilot, changed, view="judge_raw")
    assert metric["coverage"]["numerator"] == 0
    assert metric["counts"]["different_layer"] == 3
