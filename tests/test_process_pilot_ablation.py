"""Offline ablation tests: mock transport only, never a real API or network."""

import json
import socket
from pathlib import Path

import openai
import pytest
from openai import _base_client

from tracejudge_hy3.config import Settings
from tracejudge_hy3.process_eval_v2.ablation import (
    _EVIDENCE_BLOCK,
    CONDITION_DEFS,
    CONDITIONS,
    AblationPrediction,
    build_ablation_prompt,
    build_plan,
    condition_payload_fields,
    execution_order,
    plan_fingerprint,
)
from tracejudge_hy3.process_eval_v2.ablation_live import _run_ablation
from tracejudge_hy3.process_eval_v2.contracts import (
    FunctionalEvidence,
    PilotInput,
    PilotLabel,
    Prediction,
    PublicTask,
)
from tracejudge_hy3.process_eval_v2.materials import digest
from tracejudge_hy3.process_eval_v2.preflight import PreparedPilot
from tracejudge_hy3.process_eval_v2.scoring import score_ablation, score_predictions

SECRET = "test-secret-not-real"
CONFIG_SHA = "c" * 64


def _pilot(item_count=2, rule_trigger=False):
    inputs, labels = {}, {}
    verdicts = (True, False, True, False, None, True)[:item_count]
    for index, verdict in enumerate(verdicts):
        key = f"item-{index}"
        solution_code = "def f():\n    return 0\n"
        step_content = "Return zero."
        if rule_trigger and index == 0:
            # Claims an explicit empty-input branch the code does not contain.
            step_content = "如果输入为空列表，直接返回 0 的显式分支。"
        inputs[key] = PilotInput(
            item_id=key,
            problem=PublicTask(
                title="f",
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
                    {"step_id": "S1", "content": step_content, "related_requirements": ["R1"]}
                ],
                "code": solution_code,
            },
            functional_evidence=FunctionalEvidence(
                source_run_id="test",
                source_record_sha256="a" * 64,
                candidate_code_sha256=digest(solution_code.encode()),
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
            localization_status="not_applicable" if verdict else "unknown",
            first_faulty_layer=None,
            first_faulty_step=None,
            error_type=None,
            evidence=[],
            rationale="PRIVATE_LABEL_CANARY",
        )
    return PreparedPilot(inputs, labels, {}, "post_feedback_development_consensus", "unverified")


@pytest.fixture
def pilot():
    return _pilot(2)


def _settings(**overrides):
    values = {
        "hy3_base_url": "https://hy3.invalid/v1",
        "hy3_api_key": SECRET,
        "hy3_model": "test-model",
        "hy3_enable_reasoning_effort": False,
        "hy3_timeout_seconds": 120,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _completion(content: str) -> bytes:
    return json.dumps(
        {
            "id": "offline-completion",
            "object": "chat.completion",
            "created": 1,
            "model": "test-model",
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
        }
    ).encode()


ASSESSMENT_OK = json.dumps(
    {
        "reasoning_correct": True,
        "plan_code_aligned": True,
        "functional_correct": None,
        "process_correct": True,
        "explanation": "ablation ok",
    }
)
HTTP_500 = (500, b'{"error":{"message":"synthetic server failure","type":"server_error"}}')


def _contradictory_assessment():
    # Reconstructed from the documented rep2/rep4 failure shape; raw failed
    # responses were not archived by the old runner. Not a captured response.
    return json.dumps(
        {
            "reasoning_correct": True,
            "plan_code_aligned": True,
            "functional_correct": None,
            "process_correct": False,
            "first_faulty_layer": "reasoning",
            "error_type": "P01_ALGORITHM_ERROR",
            "first_faulty_location": {
                "source_field": "design_summary",
                "quote": "Always return zero.",
            },
            "explanation": "Reconstructed contradictory response.",
        }
    )


def test_contradictory_response_repaired_inside_request_budget(tmp_path, transport):
    requests = transport(
        [_completion(_contradictory_assessment()), _completion(ASSESSMENT_OK)]
        + [_completion(ASSESSMENT_OK)] * 3
    )
    report = _run_ablation(_pilot(1), CONFIG_SHA, tmp_path / "repair", **_kwargs())
    assert report["status"] == "completed"
    assert report["budget"]["consumed_requests"] == len(requests) == 5
    assert "process" in requests[1]["messages"][-1]["content"]
    assert all(row["status"] == "ok" for row in _read_rows(tmp_path / "repair"))


def test_contradictory_response_exhaustion_is_parse_error_not_crash(tmp_path, transport):
    transport([_completion(_contradictory_assessment())] * 8)
    target = tmp_path / "invalid"
    report = _run_ablation(_pilot(1), CONFIG_SHA, target, **_kwargs(max_requests=8))
    assert report["status"] == "completed"
    assert report["budget"]["consumed_requests"] == 8
    assert [r["status"] for r in _read_rows(target)] == ["parse_error"] * 4


@pytest.fixture
def transport(monkeypatch):
    """Route the real SDK through a MockTransport; forbid any real socket."""

    def deny_network(*_args, **_kwargs):
        raise AssertionError("real network access is forbidden in this test")

    monkeypatch.setattr(socket, "create_connection", deny_network)
    sdk_httpx = getattr(_base_client, "httpx2", None) or _base_client.httpx
    real_factory = openai.AsyncOpenAI

    def make(bodies, requests=None):
        requests = requests if requests is not None else []

        def handler(request):
            assert request.url.host == "hy3.invalid"
            requests.append(json.loads(request.content))
            assert len(requests) <= len(bodies), "request sent beyond the scripted budget"
            spec = bodies[len(requests) - 1]
            if isinstance(spec, Exception):
                raise spec
            status, content = spec if isinstance(spec, tuple) else (200, spec)
            if callable(content):
                content = content(requests[-1]["messages"])
            return sdk_httpx.Response(
                status, content=content, headers={"content-type": "application/json"}
            )

        def factory(**kwargs):
            return real_factory(
                **kwargs,
                http_client=sdk_httpx.AsyncClient(transport=sdk_httpx.MockTransport(handler)),
            )

        monkeypatch.setattr("tracejudge_hy3.providers.hy3_openai.openai.AsyncOpenAI", factory)
        return requests

    return make


def _kwargs(**overrides):
    kwargs = {
        "execute": True,
        "resume": False,
        "max_requests": 96,
        "max_attempts_per_judgment": 2,
        "settings": _settings(),
    }
    kwargs.update(overrides)
    return kwargs


def _read_rows(target: Path, name="predictions.jsonl"):
    path = target / name
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def test_condition_visibility_and_prompt_parity(pilot):
    from tracejudge_hy3.process_eval_v2.aggregate import build_scaffold_problem
    from tracejudge_hy3.static_analysis.ast_analyzer import analyze_code

    item = next(iter(pilot.inputs.values()))
    problem = build_scaffold_problem(item)
    static = analyze_code(
        item.solution_trace.code, function_name=problem.function_name, visible_test_values=[]
    )
    cores = set()
    schemas = set()
    for condition in CONDITIONS:
        system, user = build_ablation_prompt(condition, item, static)
        # The evidence-availability block is the only system-prompt difference.
        cores.add(system.replace(_EVIDENCE_BLOCK[condition], "<EVIDENCE>"))
        schemas.add(system.split("JSON Schema:\n", 1)[1])
        assert SECRET not in system + user
        assert "PRIVATE_LABEL_CANARY" not in system + user
        assert ("static_evidence" in user) == CONDITION_DEFS[condition]["static_evidence"]
        assert ("official_aggregate_functional_evidence" in user) == CONDITION_DEFS[condition][
            "functional_evidence"
        ]
        assert "base_status" in user or not CONDITION_DEFS[condition]["functional_evidence"]
    assert len(cores) == 1  # identical core rubric for all four conditions
    assert len(schemas) == 1  # identical output contract
    assert condition_payload_fields("ablation_a") == [
        "title",
        "requirement",
        "function_signature",
        "requirements",
        "solution_trace",
    ]


def test_interleaved_order_fixed_seeded_and_complete():
    item_ids = [f"item-{index}" for index in range(12)]
    order = execution_order(item_ids)
    assert order == execution_order(item_ids)  # deterministic under the fixed seed
    assert len(order) == 48
    assert set(order) == {(condition, item) for condition in CONDITIONS for item in item_ids}
    # Interleaved: not one-condition-at-a-time like the original pilot runner.
    assert len({condition for condition, _ in order[:4]}) > 1
    plan = build_plan(_pilot(2))
    assert plan["execution_order"] == [list(pair) for pair in execution_order(_pilot(2).inputs)]
    assert plan["budget"]["planned_judgments"] == 8
    assert plan_fingerprint(plan) == plan_fingerprint(build_plan(_pilot(2)))


def test_plan_registered_offline_without_credentials(pilot, tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("offline plan registration must not construct a provider")

    monkeypatch.setattr(
        "tracejudge_hy3.process_eval_v2.ablation_live.PilotJudgeProvider", forbidden
    )
    target = tmp_path / "ablation"
    result = _run_ablation(
        pilot,
        CONFIG_SHA,
        target,
        execute=False,
        resume=False,
        max_requests=96,
        max_attempts_per_judgment=2,
        settings=None,
    )
    assert result["status"] == "plan_registered_offline"
    assert result["calls_made"] == {"solver": 0, "judge": 0, "candidate_executions": 0}
    assert (target / "ablation-plan.json").exists()
    assert not (target / "predictions.jsonl").exists()
    # Idempotent while the plan is unchanged; refuses a changed plan.
    again = _run_ablation(
        pilot,
        CONFIG_SHA,
        target,
        execute=False,
        resume=False,
        max_requests=96,
        max_attempts_per_judgment=2,
        settings=None,
    )
    assert again["plan_sha256"] == result["plan_sha256"]
    with pytest.raises(ValueError, match="plan changed"):
        _run_ablation(
            pilot,
            CONFIG_SHA,
            target,
            execute=False,
            resume=False,
            max_requests=95,
            max_attempts_per_judgment=2,
            settings=None,
        )


def test_completed_run_traceability_and_scoring(pilot, tmp_path, transport, monkeypatch):
    async def forbidden_solver(*_args, **_kwargs):
        raise AssertionError("Solver must not run during the ablation")

    monkeypatch.setattr(
        "tracejudge_hy3.providers.hy3_openai.Hy3OpenAIProvider.generate_solution",
        forbidden_solver,
    )
    requests = transport([_completion(ASSESSMENT_OK)] * 8)

    report = _run_ablation(pilot, CONFIG_SHA, tmp_path / "run", **_kwargs())

    assert report["status"] == "completed"
    assert report["budget"]["consumed_requests"] == 8
    assert report["budget"]["remaining_requests"] == 88
    assert all(report["judgments"][c] == {"ok": 2} for c in CONDITIONS)
    assert len(requests) == 8
    # The saved interleaved order matches what was actually dispatched.
    dispatched = [
        (row["method"], row["item_id"])
        for row in _read_rows(tmp_path / "run", "requests.jsonl")
        if row["event"] == "dispatched"
    ]
    assert dispatched == [tuple(pair) for pair in report["execution_order"]]

    predictions = [AblationPrediction.model_validate(row) for row in _read_rows(tmp_path / "run")]
    assert len(predictions) == 8
    for prediction in predictions:
        assert prediction.judge_raw is not None and prediction.assessment is not None
        assert prediction.judge_raw.functional_correct is None
        assert prediction.assessment.functional_correct is None
        assert prediction.functional_evidence.functional_correct is False
        assert all(not rule.fired for rule in prediction.rule_report.rules)

    scores = score_ablation(pilot, predictions)
    assert scores["schema"] == "tracejudge-process-ablation-scores-v1"
    for view in ("judge_raw", "rule_merged"):
        view_scores = scores["views"][view]
        assert set(view_scores) == set(CONDITIONS)
        direct = view_scores["ablation_a"]["dimensions"]["public_process_correct"]
        # Judge says correct for both; gold is (correct, incorrect): one tn, one fn.
        assert direct["accuracy_all_known"] == {"numerator": 1, "denominator": 2, "value": 0.5}
    assert scores["paired_comparisons"][0]["treatment"] == "ablation_b"
    assert all(not scores["rule_effects"][condition]["changed_items"] for condition in CONDITIONS)
    # The historical three-method scorer still works unchanged.
    legacy = score_predictions(
        pilot,
        [
            Prediction(
                item_id="item-0",
                method="direct_judge",
                input_sha256=pilot.input_hash("item-0"),
                status="ok",
                assessment=predictions[0].judge_raw,
            )
        ],
    )
    assert legacy["methods"]["direct_judge"]["coverage"]["ok"] == 1


def test_rule_contribution_is_separated_from_judge(tmp_path, transport):
    pilot = _pilot(2, rule_trigger=True)
    transport([_completion(ASSESSMENT_OK)] * 8)

    report = _run_ablation(pilot, CONFIG_SHA, tmp_path / "run", **_kwargs())
    assert report["status"] == "completed"

    predictions = [AblationPrediction.model_validate(row) for row in _read_rows(tmp_path / "run")]
    triggered = [row for row in predictions if row.item_id == "item-0"]
    assert len(triggered) == 4
    for prediction in triggered:
        # The judge said aligned; the deterministic empty-input rule disagrees.
        assert prediction.judge_raw.plan_code_aligned is True
        fired = [r.rule_id for r in prediction.rule_report.rules if r.fired]
        assert fired == ["empty_input_claim"]
        assert prediction.rule_report.combination_source == "rule"
        assert prediction.assessment.plan_code_aligned is False
        assert prediction.assessment.first_faulty_layer == "alignment"
        # Rules never touch the functional field in the aggregate setting.
        assert prediction.assessment.functional_correct is None

    scores = score_ablation(pilot, predictions)
    for condition in CONDITIONS:
        effects = scores["rule_effects"][condition]
        assert effects["rules_fired"] == {"empty_input_claim": 1}
        assert [change["item_id"] for change in effects["changed_items"]] == ["item-0"]
        change = effects["changed_items"][0]["changes"]
        assert change["plan_code_aligned"] == {"judge": True, "merged": False}
    # Judge view scores item-0 as correct; merged view catches it as a false
    # positive against the true gold label (visible in both views separately).
    judge_view = scores["views"]["judge_raw"]["ablation_a"]["dimensions"]["plan_code_aligned"]
    merged_view = scores["views"]["rule_merged"]["ablation_a"]["dimensions"]["plan_code_aligned"]
    assert judge_view["tn"] == 2 and merged_view["fp"] == 1


def test_budget_caps_cover_repairs(tmp_path, transport):
    pilot = _pilot(1)  # 1 item x 4 conditions = 4 judgments
    # First judgment needs a format-repair turn; the rest succeed at once.
    transport([_completion("not json"), *([_completion(ASSESSMENT_OK)] * 4)])
    report = _run_ablation(pilot, CONFIG_SHA, tmp_path / "run", **_kwargs(max_requests=96))
    assert report["status"] == "completed"
    rows = _read_rows(tmp_path / "run")
    assert len(rows) == 4 and all(row["status"] == "ok" for row in rows)
    assert report["budget"]["consumed_requests"] == 5  # repair turns consume budget

    # A tight global cap stops mid-run with conservative accounting.
    pilot = _pilot(2)
    transport([_completion(ASSESSMENT_OK)] * 3)
    report = _run_ablation(pilot, CONFIG_SHA, tmp_path / "tight", **_kwargs(max_requests=3))
    assert report["status"] == "budget_exhausted"
    assert report["budget"]["consumed_requests"] == 3
    assert report["budget"]["remaining_requests"] == 0


def test_resume_skips_successes_and_keeps_budget(pilot, tmp_path, transport, monkeypatch):
    from tracejudge_hy3.process_eval_v2.live import RequestLedger

    # Interrupt after the second request's outcome write.
    calls = {"count": 0}
    original_observe = RequestLedger.observe

    def interrupting_observe(self, payload):
        original_observe(self, payload)
        calls["count"] += 1
        if calls["count"] == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(RequestLedger, "observe", interrupting_observe)
    transport([_completion(ASSESSMENT_OK)] * 16)
    report = _run_ablation(pilot, CONFIG_SHA, tmp_path / "run", **_kwargs())
    assert report["status"] == "interrupted"
    assert report["budget"]["consumed_requests"] == 2

    monkeypatch.setattr(RequestLedger, "observe", original_observe)
    report = _run_ablation(pilot, CONFIG_SHA, tmp_path / "run", **_kwargs(resume=True))
    assert report["status"] == "completed"
    # The interrupted judgment's spent attempt is never reclaimed: 2 + 7.
    assert report["budget"]["consumed_requests"] == 9
    rows = _read_rows(tmp_path / "run")
    assert len(rows) == 8  # no duplicates, no reruns of recorded successes
    assert {(row["condition"], row["item_id"]) for row in rows} == {
        (condition, item) for condition in CONDITIONS for item in pilot.inputs
    }


def test_resume_rejects_changed_plan_or_inputs(pilot, tmp_path, transport):
    transport([_completion(ASSESSMENT_OK)] * 2)
    report = _run_ablation(pilot, CONFIG_SHA, tmp_path / "run", **_kwargs(max_requests=2))
    assert report["status"] == "budget_exhausted"

    # A changed budget cap changes the pre-registered plan: refused.
    with pytest.raises(ValueError, match="plan changed"):
        _run_ablation(pilot, CONFIG_SHA, tmp_path / "run", **_kwargs(max_requests=3, resume=True))
    # Changed inputs change the plan fingerprint: refused.
    changed = _pilot(2)
    key = next(iter(changed.inputs))
    changed.inputs[key] = changed.inputs[key].model_copy(
        update={"problem": changed.inputs[key].problem.model_copy(update={"title": "g"})}
    )
    with pytest.raises(ValueError, match="plan changed"):
        _run_ablation(changed, CONFIG_SHA, tmp_path / "run", **_kwargs(resume=True))


def test_empty_predictions_score_as_missing_without_fabrication(pilot):
    scores = score_ablation(pilot, [])
    for condition in CONDITIONS:
        for view in ("judge_raw", "rule_merged"):
            coverage = scores["views"][view][condition]["coverage"]
            assert coverage == {"ok": 0, "missing": 2, "provider_error": 0, "parse_error": 0}
            accuracy = scores["views"][view][condition]["dimensions"]["public_process_correct"][
                "accuracy_all_known"
            ]
            assert accuracy == {"numerator": 0, "denominator": 2, "value": 0.0}
    assert all(not scores["rule_effects"][condition]["changed_items"] for condition in CONDITIONS)
    for paired in scores["paired_comparisons"]:
        assert paired["transitions"] == {"missing->missing": 2}
        assert paired["changed_items"] == {}


def test_score_refuses_tampered_merged_assessment(pilot, tmp_path, transport):
    transport([_completion(ASSESSMENT_OK)] * 8)
    _run_ablation(pilot, CONFIG_SHA, tmp_path / "run", **_kwargs())
    rows = _read_rows(tmp_path / "run")
    tampered = rows[0]
    tampered["assessment"]["reasoning_correct"] = False  # not what re-merge yields
    predictions = [AblationPrediction.model_validate(row) for row in rows]
    with pytest.raises(ValueError, match="offline re-merge"):
        score_ablation(pilot, predictions)


def test_run_artifacts_never_contain_secrets(pilot, tmp_path, transport):
    transport([_completion(ASSESSMENT_OK)] * 8)
    report = _run_ablation(pilot, CONFIG_SHA, tmp_path / "run", **_kwargs())
    assert report["status"] == "completed"
    for path in (tmp_path / "run").iterdir():
        content = path.read_bytes()
        assert SECRET.encode() not in content, path.name
        assert b"Authorization" not in content, path.name


def test_cli_ablation_guards(capsys):
    import importlib.util

    script = Path(__file__).resolve().parents[1] / "scripts/run_process_pilot.py"
    spec = importlib.util.spec_from_file_location("pilot_cli_ablation_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with pytest.raises(SystemExit):  # ablate without --output
        module.main(["--phase", "ablate"])
    with pytest.raises(SystemExit):  # --execute outside run/ablate
        module.main(["--execute"])
    with pytest.raises(SystemExit):  # --resume requires --execute
        module.main(["--phase", "ablate", "--output", "x", "--resume"])
    with pytest.raises(SystemExit):  # ablation-score requires --predictions
        module.main(["--phase", "ablation-score"])
    with pytest.raises(SystemExit):  # --predictions not allowed for ablate
        module.main(["--phase", "ablate", "--output", "x", "--predictions", "p"])
    capsys.readouterr()
