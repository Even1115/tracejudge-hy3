"""Pre-API gates must preserve pinned status semantics and fail closed."""

import copy
import hashlib
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tracejudge_hy3.benchmark import mbpp_readiness as gate


@pytest.fixture
def scripts(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("smoke_external_benchmarks")


def receipt():
    cases = {
        name: {
            "expected": expected,
            "actual": {key: values[0] for key, values in expected.items()},
            "ok": True,
            "infrastructure_error_type": None,
        }
        for name, expected in gate.MBPP_SMOKE_EXPECTATIONS.items()
    }
    return {
        "schema": gate.MBPP_SMOKE_SCHEMA,
        "ready": True,
        "uses_model_api": False,
        "image": "pinned-image",
        "execution_source_sha256": {"source": "sha256"},
        "cases": cases,
    }


def validate(value):
    gate.validate_mbpp_readiness(value, image="pinned-image", execution_source={"source": "sha256"})


def test_official_inner_timeout_is_fail_and_outer_timeout_is_timeout():
    assert gate.MBPP_SMOKE_EXPECTATIONS["call_timeout"]["base_status"] == ["fail"]
    assert gate.MBPP_SMOKE_EXPECTATIONS["process_timeout"] == {
        "base_status": ["timeout"],
        "plus_status": ["timeout"],
    }
    validate(receipt())


@pytest.mark.parametrize(
    "change", ["legacy", "missing", "forged_ok", "plus_pass", "infra", "image", "source"]
)
def test_receipt_gate_recomputes_all_cases_instead_of_trusting_ready(change):
    value = copy.deepcopy(receipt())
    if change == "legacy":
        value.pop("schema")
    elif change == "missing":
        value["cases"].pop("process_timeout")
    elif change == "forged_ok":
        value["cases"]["process_timeout"]["actual"]["base_status"] = "fail"
    elif change == "plus_pass":
        value["cases"]["call_timeout"]["actual"]["plus_status"] = "pass"
    elif change == "infra":
        value["cases"]["pass"]["infrastructure_error_type"] = "executor_failed"
    else:
        value["image" if change == "image" else "execution_source_sha256"] = "changed"
    with pytest.raises(ValueError):
        validate(value)


def test_real_smoke_fixture_contract_checks_both_groups(scripts, monkeypatch, tmp_path):
    problem = SimpleNamespace(
        problem_id="Mbpp/2", requirement="public", function_name="similar_elements"
    )
    monkeypatch.setattr(scripts, "load_problems", lambda _: [problem])

    class Runner:
        def preflight(self, **kwargs):
            return SimpleNamespace(ready=True, runtime={})

        def run_task(self, *, sample, **kwargs):
            # Host does not execute candidate strings, including the infinite loops.
            if sample.solution.startswith("while True"):
                statuses = ("timeout", "timeout")
            elif "while True" in sample.solution:
                statuses = ("fail", "timeout")
            elif "return ()" in sample.solution:
                statuses = ("fail", "fail")
            else:
                statuses = ("pass", "pass")
            return SimpleNamespace(
                raw_result=dict(zip(("base_status", "plus_status"), statuses, strict=True)),
                infrastructure_error_type=None,
                duration_seconds=0.1,
            )

    monkeypatch.setattr(scripts, "MbppPlusDockerRunner", Runner)
    monkeypatch.setattr(scripts, "parse_official_result", lambda raw, **_: raw)
    report = scripts.mbpp(tmp_path, tmp_path)
    assert report["ready"]
    assert set(report["cases"]) == set(gate.MBPP_SMOKE_EXPECTATIONS)
    assert report["cases"]["call_timeout"]["actual"]["base_status"] == "fail"


def test_replacing_readiness_archives_exact_previous_bytes(scripts, tmp_path):
    path = tmp_path / "mbpp.json"
    previous = b'{"ready": false}\n'
    path.write_bytes(previous)
    scripts.save_readiness(path, {"ready": True})
    digest = hashlib.sha256(previous).hexdigest()
    assert (tmp_path / "history" / f"mbpp-{digest}.json").read_bytes() == previous
    assert json.loads(path.read_text()) == {"ready": True}


def test_mbpp_only_preparation_does_not_touch_lcb_or_call_model(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    prepare = importlib.import_module("prepare_external_benchmarks")
    pointer = tmp_path / "artifacts/benchmark-runtime/current.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(json.dumps({"runtime_root": str(tmp_path / "frozen")}))
    monkeypatch.setattr(prepare, "ROOT", tmp_path)
    monkeypatch.setattr(prepare, "require_idle_benchmark_containers", lambda: None)
    commands = []
    monkeypatch.setattr(prepare.subprocess, "run", lambda command, **_: commands.append(command))
    monkeypatch.setattr("sys.argv", ["prepare", "--dataset", "mbpp"])
    assert prepare.main() == 0
    assert len(commands) == 3
    assert "--dataset" in commands[1] and "mbpp" in commands[1]
    assert "--preflight" in commands[2]
    assert all("--confirm-real-provider" not in command for command in commands)
    assert all(not any("livecodebench" in part for part in command) for command in commands)


@pytest.mark.parametrize("configured", [True, False])
def test_preflight_writes_auditable_receipt_without_api_client(monkeypatch, tmp_path, configured):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    run = importlib.import_module("run_mbppplus")
    dataset = tmp_path / "artifacts/datasets/mbppplus/sample120"
    dataset.mkdir(parents=True)
    (dataset / "dataset_manifest.json").write_text("{}")
    (dataset / "problems.jsonl").write_text("public projection only\n")
    smoke = tmp_path / "artifacts/benchmark-readiness/mbpp.json"
    smoke.parent.mkdir(parents=True)
    smoke.write_text(json.dumps(receipt()))
    problem = SimpleNamespace(problem_id="Mbpp/2", requirement="public", function_name="f")
    monkeypatch.setattr(run, "ROOT", tmp_path)
    monkeypatch.setattr(run, "_selection_identity", lambda _: ({}, list(range(120)), {}))
    monkeypatch.setattr(run, "load_problems", lambda _: [problem] * 120)
    monkeypatch.setattr(run, "validate_mbpp_readiness", lambda *_, **__: None)
    monkeypatch.setattr(run, "execution_identity", lambda *_, **__: {"source": "sha256"})
    monkeypatch.setattr(run, "require_idle_benchmark_containers", lambda: None)
    monkeypatch.setattr(
        run, "get_settings", lambda: SimpleNamespace(hy3_configured=lambda: configured)
    )
    monkeypatch.setattr(run, "source_identity", lambda _: {"python": "test", "dependencies": {}})

    class Runner:
        def __init__(self, limits):
            self.limits = limits

        def preflight(self, **kwargs):
            return SimpleNamespace(ready=True, runtime={"verified_task_count": 120})

    def forbidden(*_, **__):
        pytest.fail("preflight must not construct or invoke the model provider")

    monkeypatch.setattr(run, "MbppPlusDockerRunner", Runner)
    monkeypatch.setattr(run, "Hy3OpenAIProvider", forbidden)
    monkeypatch.setattr(run, "run_baseline_experiment", forbidden)
    monkeypatch.setattr(
        "sys.argv", ["run_mbppplus", "--project-root", str(tmp_path), "--preflight"]
    )
    assert run.main() == (0 if configured else 1)
    result = json.loads((tmp_path / "artifacts/benchmark-preflight/mbpp/receipt.json").read_text())
    assert result["ready"] is configured
    assert result["planned_n"] == 120
    assert result["uses_model_api"] is False
    assert result["remote_auth_quota_availability_checked"] is False
    assert result["formal_parallel"] == 1
    assert len(result["smoke_receipt_sha256"]) == 64
    assert not (tmp_path / "artifacts/experiments").exists()
