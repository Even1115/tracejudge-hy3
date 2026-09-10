"""Offline live-run tests: mock transport only, never a real API or network."""

import json
import os
import socket
from pathlib import Path

import openai
import pytest
from openai import _base_client

from tracejudge_hy3.config import Settings
from tracejudge_hy3.exceptions import ProviderAuthError
from tracejudge_hy3.process_eval_v2.contracts import (
    FunctionalEvidence,
    PilotInput,
    PilotLabel,
    Prediction,
    PublicTask,
)
from tracejudge_hy3.process_eval_v2.live import RequestLedger, _run
from tracejudge_hy3.process_eval_v2.materials import digest
from tracejudge_hy3.process_eval_v2.preflight import PreparedPilot
from tracejudge_hy3.process_eval_v2.scoring import score_predictions

SECRET = "test-secret-not-real"
CONFIG_SHA = "c" * 64


def _pilot(item_count=2):
    inputs, labels = {}, {}
    for index, verdict in enumerate((True, False, True, False, None, True)[:item_count]):
        key = f"item-{index}"
        solution_code = "def f():\n    return 0\n"
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
                    {"step_id": "S1", "content": "Return zero.", "related_requirements": ["R1"]}
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


DIRECT_OK = json.dumps(
    {"reasoning_correct": True, "plan_code_aligned": True, "explanation": "direct ok"}
)
ASSESSMENT_OK = json.dumps(
    {
        "reasoning_correct": True,
        "plan_code_aligned": True,
        "functional_correct": None,
        "process_correct": True,
        "explanation": "structured ok",
    }
)
HTTP_500 = (500, b'{"error":{"message":"synthetic server failure","type":"server_error"}}')
HTTP_401 = (401, b'{"error":{"message":"synthetic auth failure","type":"auth_error"}}')


def _adaptive(messages) -> bytes:
    """Return a schema-valid body for whichever method the system prompt belongs to."""

    system = messages[0]["content"]
    if "代码评审员" in system:
        return _completion(DIRECT_OK)
    return _completion(ASSESSMENT_OK)


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


def _run_kwargs(pilot, tmp_path, **overrides):
    kwargs = {
        "resume": False,
        "max_requests": 72,
        "max_attempts_per_judgment": 2,
        "settings": _settings(),
    }
    kwargs.update(overrides)
    return kwargs


def _read_predictions(target: Path):
    path = target / "predictions.jsonl"
    if not path.exists():
        return []
    return [
        Prediction.model_validate(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _dispatched(target: Path):
    path = target / "requests.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line)["event"] == "dispatched"
    ]


def test_completed_run_views_budget_and_scorer_compat(pilot, tmp_path, transport, monkeypatch):
    async def forbidden_solver(*_args, **_kwargs):
        raise AssertionError("Solver must not run during the pilot")

    monkeypatch.setattr(
        "tracejudge_hy3.providers.hy3_openai.Hy3OpenAIProvider.generate_solution",
        forbidden_solver,
    )
    monkeypatch.setattr(
        "tracejudge_hy3.providers.hy3_openai.Hy3OpenAIProvider.generate_solution_with_details",
        forbidden_solver,
    )
    requests = transport([_adaptive] * 6)

    report = _run(pilot, CONFIG_SHA, tmp_path / "run", **_run_kwargs(pilot, tmp_path))

    assert report["status"] == "completed"
    assert report["budget"]["consumed_requests"] == 6
    assert report["budget"]["remaining_requests"] == 66
    assert report["requests"]["token_usage"] == "complete"
    assert report["requests"]["input_tokens"] == 6 * 11
    assert report["budget"]["money_estimate"] is None
    assert all(report["judgments"][m] == {"ok": 2} for m in report["judgments"])

    rows = _read_predictions(tmp_path / "run")
    assert len(rows) == 6
    assert all(row.status == "ok" for row in rows)
    by_method = {}
    for row in rows:
        by_method.setdefault(row.method, []).append(row)
    # direct_judge never learns functional outcomes; full_system gets the aggregate.
    assert all(row.assessment.functional_correct is None for row in by_method["direct_judge"])
    assert all(row.assessment.functional_correct is None for row in by_method["structured_judge"])
    assert all(row.assessment.functional_correct is False for row in by_method["full_system"])
    assert all(row.assessment.process_correct is True for row in by_method["direct_judge"])
    assert all(row.assessment.process_correct is False for row in by_method["full_system"])

    # The existing offline scorer consumes the file unchanged.
    scores = score_predictions(pilot, rows)
    assert scores["methods"]["direct_judge"]["coverage"]["ok"] == 2
    assert scores["methods"]["structured_judge"]["coverage"] == {
        "ok": 2,
        "missing": 0,
        "provider_error": 0,
        "parse_error": 0,
    }

    # Visibility: only full_system sees the aggregate functional evidence.
    for request in requests:
        text = json.dumps(request, ensure_ascii=False)
        assert "PRIVATE_LABEL_CANARY" not in text
        assert SECRET not in text
        system = request["messages"][0]["content"]
        if "完整系统模式" in system:
            assert "plus_status" in text
        else:
            assert "plus_status" not in text and "base_status" not in text
    assert sum("代码评审员" in r["messages"][0]["content"] for r in requests) == 2
    assert sum("完整系统模式" in r["messages"][0]["content"] for r in requests) == 2


def test_parse_repair_and_failures_stay_within_caps(pilot, tmp_path, transport):
    pilot = _pilot(1)
    requests = transport(
        [
            _completion("not JSON at all"),  # direct attempt 1 -> repair
            _adaptive,  # direct attempt 2 (repair turn)
            HTTP_500,  # structured attempt 1
            HTTP_500,  # structured attempt 2 (cap reached afterwards)
            _completion("still not JSON"),  # full attempt 1 -> repair
            _completion("still broken"),  # full attempt 2
        ]
    )

    report = _run(pilot, CONFIG_SHA, tmp_path / "run", **_run_kwargs(pilot, tmp_path))

    assert report["status"] == "completed"
    assert report["budget"]["consumed_requests"] == 6 == len(requests)
    rows = {row.method: row for row in _read_predictions(tmp_path / "run")}
    assert rows["direct_judge"].status == "ok"
    assert rows["structured_judge"].status == "provider_error"
    assert rows["full_system"].status == "parse_error"
    judgments = report["judgments"]
    assert judgments["direct_judge"]["ok"] == 1
    assert judgments["structured_judge"]["provider_error"] == 1
    assert judgments["full_system"]["parse_error"] == 1
    # The repair turn appends context; each judgment stopped at exactly 2 requests.
    assert len(requests[1]["messages"]) == 4
    assert len(requests[5]["messages"]) == 4
    ledger = _dispatched(tmp_path / "run")
    assert len(ledger) == 6


def test_auth_error_stops_the_run_after_one_request(pilot, tmp_path, transport):
    requests = transport([HTTP_401])

    report = _run(pilot, CONFIG_SHA, tmp_path / "run", **_run_kwargs(pilot, tmp_path))

    assert report["status"] == "partial_execution"
    assert report["stop_reason"] == "auth_error"
    assert report["budget"]["consumed_requests"] == 1 == len(requests)
    rows = _read_predictions(tmp_path / "run")
    assert len(rows) == 1 and rows[0].status == "provider_error"
    assert rows[0].method == "direct_judge"
    assert sum(v.get("pending", 0) for v in report["judgments"].values()) == 5


def test_resume_retries_failed_judgments_within_remaining_attempts(pilot, tmp_path, transport):
    pilot = _pilot(1)
    transport([HTTP_401])
    kwargs = _run_kwargs(pilot, tmp_path)
    first = _run(pilot, CONFIG_SHA, tmp_path / "run", **kwargs)
    assert first["status"] == "partial_execution"
    assert [row.status for row in _read_predictions(tmp_path / "run")] == ["provider_error"]

    # Credentials fixed, same run directory: the failed judgment is retried once
    # (its single remaining attempt), then replaced atomically -- never duplicated.
    requests2 = transport([_adaptive] * 3)
    resumed = _run(pilot, CONFIG_SHA, tmp_path / "run", **(kwargs | {"resume": True}))
    assert resumed["status"] == "completed"
    assert len(requests2) == 3
    rows = _read_predictions(tmp_path / "run")
    assert [row.status for row in rows] == ["ok"] * 3
    assert len({(row.method, row.item_id) for row in rows}) == 3
    scores = score_predictions(pilot, rows)
    assert scores["methods"]["full_system"]["coverage"]["ok"] == 1
    ledger = _dispatched(tmp_path / "run")
    assert len(ledger) == 4  # the failed auth attempt stays on the books


def test_global_cap_blocks_mid_judgment_and_resume_keeps_budget(pilot, tmp_path, transport):
    pilot = _pilot(1)
    requests = transport([_completion("not JSON at all")])
    kwargs = _run_kwargs(pilot, tmp_path, max_requests=1)

    report = _run(pilot, CONFIG_SHA, tmp_path / "run", **kwargs)

    assert report["status"] == "budget_exhausted"
    assert report["budget"]["consumed_requests"] == 1 == len(requests)
    assert _read_predictions(tmp_path / "run") == []
    pending = report["pending"]
    assert pending[0]["method"] == "direct_judge" and pending[0]["attempts_consumed"] == 1

    # A restart never resets the consumed budget: no further request is sent.
    resumed = _run(pilot, CONFIG_SHA, tmp_path / "run", **(kwargs | {"resume": True}))
    assert resumed["status"] == "budget_exhausted"
    assert resumed["resumed"] is True
    assert len(requests) == 1
    assert _read_predictions(tmp_path / "run") == []


def test_resume_skips_recorded_rows(pilot, tmp_path, transport):
    shared_requests = []
    transport([_adaptive] * 6, requests=shared_requests)
    kwargs = _run_kwargs(pilot, tmp_path)
    first = _run(pilot, CONFIG_SHA, tmp_path / "run", **kwargs)
    assert first["status"] == "completed" and len(shared_requests) == 6

    resumed = _run(pilot, CONFIG_SHA, tmp_path / "run", **(kwargs | {"resume": True}))
    assert resumed["status"] == "completed"
    assert resumed["resumed"] is True
    assert len(shared_requests) == 6  # nothing re-requested
    assert resumed["budget"]["consumed_requests"] == 6
    assert len(_read_predictions(tmp_path / "run")) == 6


def test_resume_after_mid_flight_interrupt_retries_conservatively(
    pilot, tmp_path, transport, monkeypatch
):
    pilot = _pilot(1)
    original_observe = RequestLedger.observe
    calls = {"n": 0}

    def flaky_observe(self, payload):
        calls["n"] += 1
        if calls["n"] == 2:
            # Simulates a crash after the request was dispatched but before its
            # outcome was persisted; the dispatched row is already on disk.
            raise KeyboardInterrupt
        return original_observe(self, payload)

    monkeypatch.setattr(RequestLedger, "observe", flaky_observe)
    requests = transport([_adaptive] * 4)
    kwargs = _run_kwargs(pilot, tmp_path)

    first = _run(pilot, CONFIG_SHA, tmp_path / "run", **kwargs)
    assert first["status"] == "interrupted"
    assert first["stop_reason"] == "interrupted"
    assert len(requests) == 2
    rows = _read_predictions(tmp_path / "run")
    assert [row.method for row in rows] == ["direct_judge"]

    monkeypatch.setattr(RequestLedger, "observe", original_observe)
    resumed = _run(pilot, CONFIG_SHA, tmp_path / "run", **(kwargs | {"resume": True}))
    assert resumed["status"] == "completed"
    # structured_judge consumed one interrupted attempt, so only one retry is left;
    # full_system then runs normally. No silent duplicate beyond the cap.
    assert len(requests) == 4
    ledger = _dispatched(tmp_path / "run")
    per_judgment = {}
    for row in ledger:
        key = (row["method"], row["item_id"])
        per_judgment[key] = per_judgment.get(key, 0) + 1
    assert per_judgment[("structured_judge", "item-0")] == 2
    assert all(count <= 2 for count in per_judgment.values())
    methods = {row.method: row.status for row in _read_predictions(tmp_path / "run")}
    assert methods == {"direct_judge": "ok", "structured_judge": "ok", "full_system": "ok"}


def test_resume_rejects_changed_configuration(pilot, tmp_path, transport):
    transport([_adaptive] * 6)
    kwargs = _run_kwargs(pilot, tmp_path)
    assert _run(pilot, CONFIG_SHA, tmp_path / "run", **kwargs)["status"] == "completed"

    changed_model = kwargs | {
        "resume": True,
        "settings": _settings(hy3_model="different-model"),
    }
    with pytest.raises(ValueError, match="refusing to mix"):
        _run(pilot, CONFIG_SHA, tmp_path / "run", **changed_model)

    changed_budget = kwargs | {"resume": True, "max_requests": 10}
    with pytest.raises(ValueError, match="refusing to mix"):
        _run(pilot, CONFIG_SHA, tmp_path / "run", **changed_budget)

    changed_inputs = kwargs | {"resume": True}
    with pytest.raises(ValueError, match="refusing to mix"):
        _run(_pilot(1), CONFIG_SHA, tmp_path / "run", **changed_inputs)


def test_corrupt_checkpoints_and_output_protection(pilot, tmp_path, transport):
    transport([_adaptive] * 6)
    kwargs = _run_kwargs(pilot, tmp_path)
    assert _run(pilot, CONFIG_SHA, tmp_path / "run", **kwargs)["status"] == "completed"

    # Refuse to mix into an existing directory without --resume.
    with pytest.raises(ValueError, match="already exists"):
        _run(pilot, CONFIG_SHA, tmp_path / "run", **kwargs)
    # Refuse to resume a missing directory.
    with pytest.raises(ValueError, match="does not exist"):
        _run(pilot, CONFIG_SHA, tmp_path / "missing", **(kwargs | {"resume": True}))

    # Truncated trailing line in predictions.jsonl -> refuse, never silently rerun.
    predictions = tmp_path / "run" / "predictions.jsonl"
    predictions.write_bytes(predictions.read_bytes() + b'{"item_id": "it')
    with pytest.raises(ValueError, match="corrupt"):
        _run(pilot, CONFIG_SHA, tmp_path / "run", **(kwargs | {"resume": True}))
    predictions.write_bytes(predictions.read_bytes()[: -len(b'{"item_id": "it')])

    ledger = tmp_path / "run" / "requests.jsonl"
    ledger.write_bytes(ledger.read_bytes() + b"not json\n")
    with pytest.raises(ValueError, match="corrupt"):
        _run(pilot, CONFIG_SHA, tmp_path / "run", **(kwargs | {"resume": True}))
    ledger.write_bytes(ledger.read_bytes()[: -len(b"not json\n")])

    # A tampered prediction no longer binding the frozen input is rejected.
    rows = predictions.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(rows[0])
    tampered["input_sha256"] = "b" * 64
    rows[0] = json.dumps(tampered)
    predictions.write_text("\n".join(rows) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="binds a different input"):
        _run(pilot, CONFIG_SHA, tmp_path / "run", **(kwargs | {"resume": True}))


def test_run_lock_blocks_concurrent_and_adopts_stale(pilot, tmp_path, transport):
    transport([_adaptive] * 6)
    kwargs = _run_kwargs(pilot, tmp_path)
    assert _run(pilot, CONFIG_SHA, tmp_path / "run", **kwargs)["status"] == "completed"

    lock = tmp_path / "run" / "run.lock"
    lock.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    with pytest.raises(ValueError, match="held by pid"):
        _run(pilot, CONFIG_SHA, tmp_path / "run", **(kwargs | {"resume": True}))

    # A lock whose process is gone is stale and adopted; the completed run stays intact.
    lock.write_text(json.dumps({"pid": 2**22 + 12345}), encoding="utf-8")
    resumed = _run(pilot, CONFIG_SHA, tmp_path / "run", **(kwargs | {"resume": True}))
    assert resumed["status"] == "completed"
    assert not lock.exists()


def test_run_artifacts_never_contain_secrets(pilot, tmp_path, transport):
    transport([HTTP_500] + [_adaptive] * 6)
    report = _run(pilot, CONFIG_SHA, tmp_path / "run", **_run_kwargs(pilot, tmp_path))
    assert report["status"] == "completed"

    for path in (tmp_path / "run").iterdir():
        content = path.read_bytes()
        assert SECRET.encode() not in content
        assert b"Authorization" not in content
    assert SECRET not in json.dumps(report, ensure_ascii=False)


def test_run_requires_usable_credentials(pilot, tmp_path):
    settings = _settings(hy3_api_key=None)
    with pytest.raises(ProviderAuthError, match="requires HY3"):
        _run(
            pilot,
            CONFIG_SHA,
            tmp_path / "run",
            resume=False,
            max_requests=72,
            max_attempts_per_judgment=2,
            settings=settings,
        )


def test_default_settings_read_project_root_env_file(tmp_path, monkeypatch):
    from tracejudge_hy3.process_eval_v2.live import _default_settings

    for name in ("HY3_BASE_URL", "HY3_API_KEY", "HY3_MODEL"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    (tmp_path / ".env").write_text(
        "HY3_BASE_URL=https://hy3.invalid/v1\nHY3_API_KEY=from-root-env\nHY3_MODEL=m\n",
        encoding="utf-8",
    )
    loaded = _default_settings(tmp_path)
    assert loaded.hy3_api_key == "from-root-env"
    assert loaded.hy3_configured()
    # Real environment variables still win over the project .env file.
    monkeypatch.setenv("HY3_API_KEY", "from-environ")
    assert _default_settings(tmp_path).hy3_api_key == "from-environ"
    # Without a project .env, fall back to the ambient environment only.
    monkeypatch.delenv("HY3_API_KEY")
    (tmp_path / ".env").unlink()
    assert _default_settings(tmp_path).hy3_api_key is None


def test_cli_argument_guards(capsys):
    import importlib.util

    script = Path(__file__).resolve().parents[1] / "scripts/run_process_pilot.py"
    spec = importlib.util.spec_from_file_location("pilot_cli_live_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with pytest.raises(SystemExit):  # --phase run without --execute
        module.main(["--phase", "run", "--output", "x"])
    with pytest.raises(SystemExit):  # --phase run without --output
        module.main(["--phase", "run", "--execute"])
    with pytest.raises(SystemExit):  # --execute outside the run phase
        module.main(["--execute"])
    with pytest.raises(SystemExit):  # --resume outside the run phase
        module.main(["--resume"])
    with pytest.raises(SystemExit):  # --predictions only with --phase score
        module.main(["--phase", "run", "--execute", "--output", "x", "--predictions", "p"])
    capsys.readouterr()
