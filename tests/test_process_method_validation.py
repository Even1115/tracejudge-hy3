"""Method validation tests: mock transport and static inputs, no candidate execution."""

import importlib.util
import json
from pathlib import Path

import pytest
from test_process_pilot_ablation import (
    ASSESSMENT_OK,
    CONFIG_SHA,
    _completion,
    _kwargs,
    _pilot,
    _read_rows,
)
from test_process_pilot_ablation import (
    transport as transport,
)

from tracejudge_hy3.process_eval_v2.ablation import (
    build_ablation_prompt,
    condition_fingerprints,
)
from tracejudge_hy3.process_eval_v2.ablation_live import _run_ablation
from tracejudge_hy3.process_eval_v2.assumptions import (
    ASSUMPTION_PROMPT,
    AssumptionCheck,
    validate_assumptions,
)
from tracejudge_hy3.process_eval_v2.consistency import check_public_consistency
from tracejudge_hy3.process_eval_v2.method_validation import (
    compare_methods,
    offline_checks,
    prepare_validation,
)
from tracejudge_hy3.process_eval_v2.scoring import score_ablation
from tracejudge_hy3.schemas.evaluation import ProcessAssessment

ROOT = Path(__file__).resolve().parents[1]


def test_code_contradiction_may_be_alignment_error_without_reasoning_error():
    # Reconstruction from a saved successful baseline, NOT a recovered failed response.
    pilot, _ = prepare_validation(ROOT, "probes")
    item = pilot.inputs["probe-07c8d275bebcd1296b3c"]
    assessment = ProcessAssessment(
        reasoning_correct=True,
        plan_code_aligned=False,
        functional_correct=None,
        process_correct=False,
        explanation="Synthetic regression: correct plan, strict code boundary.",
    )
    check = AssumptionCheck(
        requirement_id="R1",
        requirement_quote=item.problem.requirements[0].content,
        claim={"source_field": "code", "quote": "if len(word) > 4"},
        category="explicit_contradiction",
        rationale="Static reconstruction, candidate not executed.",
    )
    validate_assumptions([check], assessment, item)
    assert assessment.reasoning_correct is True
    for reasoning, alignment in ((True, True), (None, True), (True, None)):
        with pytest.raises(ValueError, match="conflicts"):
            validate_assumptions(
                [check],
                ProcessAssessment(
                    reasoning_correct=reasoning,
                    plan_code_aligned=alignment,
                    functional_correct=None,
                    process_correct=True if alignment is reasoning is True else None,
                    explanation="No definite public error.",
                ),
                item,
            )


def test_failed_attempt_diagnostics_are_redacted_and_bound(tmp_path, transport):
    target = tmp_path / "diagnostics"
    bad = "not JSON; Authorization: Bearer diagnostic-secret-canary"
    transport([_completion(bad), _completion(ASSESSMENT_OK)])
    report = _run_ablation(
        _pilot(1), CONFIG_SHA, target, conditions=("ablation_a",), **_kwargs(max_requests=2)
    )
    raw = (target / "diagnostics.jsonl").read_text(encoding="utf-8")
    assert "diagnostic-secret-canary" not in raw
    rows = [json.loads(line) for line in raw.splitlines()]
    assert len(rows) == 1
    assert rows[0]["seq"] == 1
    assert rows[0]["item_id"] == "item-0"
    assert rows[0]["method"] == "ablation_a"
    assert rows[0]["stage"] == "schema"
    assert "not JSON" in rows[0]["response_redacted"]
    assert "diagnostics.jsonl" in report["files_sha256"]
    assert report["budget"]["consumed_requests"] == 2


def test_each_context_failure_is_retained_and_resume_does_not_reset_cap(tmp_path, transport):
    from test_process_pilot_ablation import SECRET, _contradictory_assessment

    target = tmp_path / "context-failures"
    bad = json.loads(_contradictory_assessment())
    bad["explanation"] += f" {SECRET} password=private-diagnostic-canary"
    requests = transport([_completion(json.dumps(bad))] * 2)
    options = {"conditions": ("ablation_a",), **_kwargs(max_requests=2)}
    report = _run_ablation(_pilot(1), CONFIG_SHA, target, **options)
    raw = (target / "diagnostics.jsonl").read_text(encoding="utf-8")
    assert SECRET not in raw and "private-diagnostic-canary" not in raw
    rows = [json.loads(line) for line in raw.splitlines()]
    assert [row["seq"] for row in rows] == [1, 2]
    assert all(row["stage"] == "context_consistency" for row in rows)
    assert all("first_faulty_location" in row["response_redacted"] for row in rows)
    assert _read_rows(target)[0]["status"] == "parse_error"
    before = (target / "diagnostics.jsonl").read_bytes()
    options["resume"] = True
    _run_ablation(_pilot(1), CONFIG_SHA, target, **options)
    assert (target / "diagnostics.jsonl").read_bytes() == before
    assert report["budget"]["consumed_requests"] == len(requests) == 2


def test_provider_failure_is_diagnosed_before_successful_retry(tmp_path, transport):
    target = tmp_path / "provider-diagnostic"
    transport(
        [
            (500, json.dumps({"error": "synthetic provider failure"})),
            _completion(ASSESSMENT_OK),
        ]
    )
    report = _run_ablation(
        _pilot(1), CONFIG_SHA, target, conditions=("ablation_a",), **_kwargs(max_requests=2)
    )
    rows = [json.loads(line) for line in (target / "diagnostics.jsonl").read_bytes().splitlines()]
    assert len(rows) == 1
    assert rows[0]["stage"] == "provider" and rows[0]["seq"] == 1
    assert rows[0]["response_redacted"] is None
    assert report["budget"]["consumed_requests"] == 2


@pytest.mark.parametrize(
    "rubric",
    ["baseline", "assumption_audit_v1", "baseline_location_v2", "assumption_audit_location_v2"],
)
def test_three_probe_contracts_with_synthetic_responses(tmp_path, transport, rubric):
    from tracejudge_hy3.process_eval_v2.ablation import execution_order

    pilot, config_hash = prepare_validation(
        ROOT, "probes", location_v2=rubric.endswith("location_v2")
    )
    outputs = []
    for condition, key in execution_order(pilot.inputs):
        if condition != "ablation_a":
            continue
        gold = pilot.labels[key]
        data = {
            field: getattr(gold, field)
            for field in (
                "reasoning_correct",
                "plan_code_aligned",
                "process_correct",
                "first_faulty_layer",
                "first_faulty_step",
                "first_faulty_location",
                "error_type",
            )
        }
        assessment = ProcessAssessment(
            **data,
            functional_correct=None,
            explanation="Synthetic contract fixture, not model performance evidence.",
        ).model_dump(mode="json")
        if rubric in ("assumption_audit_v1", "assumption_audit_location_v2"):
            item = pilot.inputs[key]
            claim = gold.first_faulty_location
            if claim is None:
                step = next(
                    s
                    for s in item.solution_trace.implementation_steps
                    if s.step_id == gold.first_faulty_step
                )
                claim = {
                    "source_field": "implementation_steps",
                    "step_id": step.step_id,
                    "quote": step.content,
                }
            elif hasattr(claim, "model_dump"):
                claim = claim.model_dump(mode="json")
            assessment = {
                "assessment": assessment,
                "checks": [
                    {
                        "requirement_id": "R1",
                        "requirement_quote": item.problem.requirements[0].content,
                        "claim": claim,
                        "category": "explicit_contradiction",
                        "rationale": "Synthetic output plumbing, no candidate execution or semantic performance claim.",
                    }
                ],
            }
        outputs.append(_completion(json.dumps(assessment)))
    requests = transport(outputs)
    target = tmp_path / rubric
    report = _run_ablation(
        pilot,
        config_hash,
        target,
        rubric=rubric,
        conditions=("ablation_a",),
        **_kwargs(max_requests=6),
    )
    assert len(requests) == 3
    assert report["judgments"] == {"ablation_a": {"ok": 3}}
    assert all(row["status"] == "ok" for row in _read_rows(target))


def test_historical_five_round_tally_is_unchanged():
    from tracejudge_hy3.process_eval_v2.preflight import DEFAULT_CONFIG
    from tracejudge_hy3.process_eval_v2.repetition import tally_repetitions

    path = (
        ROOT
        / "artifacts/experiments/process-pilot/mbpp12-ablation-repetition-tally-20260909-v1/tally-report.json"
    )
    if not path.exists():
        pytest.skip("historical repetition artifacts unavailable")
    saved = json.loads(path.read_text(encoding="utf-8"))
    result = tally_repetitions(
        ROOT, DEFAULT_CONFIG, saved["run_dirs"], expect_plan_sha256=saved["expect_plan_sha256"]
    )
    assert result == saved


def test_budgeted_contradiction_repair_does_not_change_evidence(tmp_path, transport):
    from test_process_pilot_ablation import _contradictory_assessment

    valid_error = json.loads(_contradictory_assessment())
    valid_error["reasoning_correct"] = False
    requests = transport(
        [_completion(_contradictory_assessment()), _completion(json.dumps(valid_error))]
    )
    target = tmp_path / "error"
    result = _run_ablation(
        _pilot(1), CONFIG_SHA, target, conditions=("ablation_a",), **_kwargs(max_requests=2)
    )
    saved = _read_rows(target)[0]
    assert saved["status"] == "ok"
    assert saved["judge_raw"]["first_faulty_location"] == valid_error["first_faulty_location"] | {
        "step_id": None,
        "entry_index": None,
        "code_span": None,
    }
    assert result["budget"]["consumed_requests"] == len(requests) == 2
    resumed = _run_ablation(
        _pilot(1),
        CONFIG_SHA,
        target,
        conditions=("ablation_a",),
        **_kwargs(max_requests=2, resume=True),
    )
    assert resumed["budget"]["consumed_requests"] == 2
    assert len(requests) == 2


def test_projection_rejects_false_error_metadata_without_location():
    assessment = ProcessAssessment(
        reasoning_correct=True,
        plan_code_aligned=True,
        functional_correct=None,
        process_correct=True,
        error_type="R03_UNSUPPORTED_ASSUMPTION",
        explanation="Invalid.",
    )
    with pytest.raises(ValueError, match="classification"):
        check_public_consistency(assessment)


def test_assumption_prompt_is_general_and_public_only():
    item = _pilot(1).inputs["item-0"]
    baseline, public = build_ablation_prompt("ablation_a", item, None)
    audit, audited_public = build_ablation_prompt(
        "ablation_a", item, None, rubric="assumption_audit_v1"
    )
    assert public == audited_public
    assert ASSUMPTION_PROMPT in audit and ASSUMPTION_PROMPT not in baseline
    for forbidden in (
        "PRIVATE_LABEL_CANARY",
        "base_status",
        "plus_status",
        "mutation_kind",
        "gold_label",
    ):
        assert forbidden not in public
    for forbidden in (
        "max_product",
        "division_elements",
        "opposite_Signs",
        "text_lowercase_underscore",
        "item-",
    ):
        assert forbidden not in ASSUMPTION_PROMPT
    assert (
        condition_fingerprints()["ablation_a"]
        != condition_fingerprints("assumption_audit_v1")["ablation_a"]
    )


@pytest.mark.parametrize(
    "category,verdict",
    [
        ("reasonable_interpretation", True),
        ("semantic_ambiguity", None),
        ("explicit_contradiction", False),
        ("unsupported_restriction", False),
    ],
)
def test_assumption_categories_and_quote_binding(category, verdict):
    item = _pilot(1).inputs["item-0"]
    check = AssumptionCheck(
        requirement_id="R1",
        requirement_quote="Return one.",
        claim={"source_field": "design_summary", "quote": "Always return zero."},
        category=category,
        rationale="Synthetic contract test, not a semantic gold judgment.",
    )
    assessment = ProcessAssessment(
        reasoning_correct=verdict,
        plan_code_aligned=True,
        functional_correct=None,
        process_correct=verdict,
        explanation="Synthetic.",
    )
    validate_assumptions([check], assessment, item)
    with pytest.raises(ValueError, match="quote"):
        validate_assumptions(
            [check.model_copy(update={"requirement_quote": "INVENTED"})], assessment, item
        )
    if verdict is False:
        with pytest.raises(ValueError, match="conflicts"):
            validate_assumptions(
                [check], assessment.model_copy(update={"reasoning_correct": True}), item
            )


def test_new_rubric_run_and_compare_use_same_budget_and_isolated_reviews(tmp_path, transport):
    pilot = _pilot(1)
    base = tmp_path / "baseline"
    audit = tmp_path / "audit"
    transport([_completion(ASSESSMENT_OK)])
    _run_ablation(pilot, CONFIG_SHA, base, conditions=("ablation_a",), **_kwargs(max_requests=2))
    wrapper = {"assessment": json.loads(ASSESSMENT_OK), "checks": []}
    requests = transport([_completion(json.dumps(wrapper))])
    _run_ablation(
        pilot,
        CONFIG_SHA,
        audit,
        rubric="assumption_audit_v1",
        conditions=("ablation_a",),
        **_kwargs(max_requests=2),
    )
    assert len(requests) == 1
    result = compare_methods(pilot, CONFIG_SHA, base, audit)
    assert result["paired"]["judge_raw"]["transitions"] == {"tn->tn": 1}
    assert _read_rows(audit)[0]["assumption_review"] == []
    with pytest.raises(ValueError, match="rubric"):
        compare_methods(pilot, CONFIG_SHA, audit, base)
    path = audit / "predictions.jsonl"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="SHA-256"):
        compare_methods(pilot, CONFIG_SHA, base, audit)


def test_assumption_evidence_failure_repaired_under_same_cap(tmp_path, transport):
    row = {
        "assessment": json.loads(ASSESSMENT_OK),
        "checks": [
            {
                "requirement_id": "R1",
                "requirement_quote": "INVENTED",
                "claim": {"source_field": "design_summary", "quote": "Always return zero."},
                "category": "reasonable_interpretation",
                "rationale": "Synthetic.",
            }
        ],
    }
    bad = json.dumps(row)
    row["checks"][0]["requirement_quote"] = "Return one."
    requests = transport([_completion(bad), _completion(json.dumps(row))])
    report = _run_ablation(
        _pilot(1),
        CONFIG_SHA,
        tmp_path / "quote",
        rubric="assumption_audit_v1",
        conditions=("ablation_a",),
        **_kwargs(max_requests=2),
    )
    assert report["budget"]["consumed_requests"] == len(requests) == 2


def test_actual_development_and_probes_offline_without_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("candidate execution forbidden")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    dev, _ = prepare_validation(ROOT, "development")
    probes, _ = prepare_validation(ROOT, "probes")
    dev_report, probe_report = offline_checks(dev), offline_checks(probes)
    assert dev_report["labels"] == {"correct": 32, "incorrect": 4}
    assert dev_report["structured_location_gold"] == 2
    assert probe_report["item_count"] == 3
    assert probe_report["supported_step_gold"] == 3
    assert not set(dev.inputs) & set(probes.inputs)
    for item in probes.inputs.values():
        assert item.functional_evidence.functional_correct is None
        assert item.functional_evidence.infrastructure_status == "not_executed"
        assert item.item_id.startswith("probe-")
        _, view = build_ablation_prompt("ablation_a", item, None, rubric="assumption_audit_v1")
        for secret in (
            "gold_label",
            "mutation_kind",
            "counterfactual:",
            "expected_impact",
            "functional_evidence",
        ):
            assert secret not in view
    missing = score_ablation(probes, [], conditions=("ablation_a",))
    assert missing["views"]["judge_raw"]["ablation_a"]["coverage"]["missing"] == 3


def test_cli_plan_is_offline_and_rejects_changed_binding(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location(
        "validation_cli", ROOT / "scripts/run_process_method_validation.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def forbidden(*args, **kwargs):
        raise AssertionError("provider construction forbidden")

    monkeypatch.setattr(
        "tracejudge_hy3.process_eval_v2.ablation_live.PilotJudgeProvider", forbidden
    )
    monkeypatch.setattr(
        "tracejudge_hy3.process_eval_v2.method_validation.prepare_validation",
        lambda *a: (_pilot(1), CONFIG_SHA),
    )
    args = [
        "--project-root",
        str(tmp_path),
        "--dataset",
        "development",
        "--rubric",
        "assumption_audit_v1",
        "--output",
        "artifacts/experiments/process-method-v2/test",
    ]
    assert module.main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["planned_judgments"] == 1
    assert report["calls_made"]["judge"] == 0
    args[args.index("assumption_audit_v1")] = "baseline"
    assert module.main(args) == 2
