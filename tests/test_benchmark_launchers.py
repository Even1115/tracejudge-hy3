from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tracejudge_hy3.benchmark.mbpp_readiness import MBPP_SMOKE_EXPECTATIONS, MBPP_SMOKE_SCHEMA


def load_script(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_freeze_is_clean_independent_and_leaves_parent_index_unchanged(tmp_path):
    module = load_script("freeze_benchmark_runtime")
    project = tmp_path / "project"
    for folder in ("src", "scripts", "data/manifests", "docs"):
        (project / folder).mkdir(parents=True, exist_ok=True)
    for name in (
        "src/module.py",
        "scripts/entry.py",
        "pyproject.toml",
        "uv.lock",
        "data/manifests/evalplus_mbppplus_64fc4195.json",
    ):
        (project / name).write_text("{}\n")
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    (project / ".gitignore").write_text("artifacts/\n")
    before = subprocess.check_output(["git", "status", "--porcelain"], cwd=project)
    first = module.freeze(project)
    assert first == module.freeze(project)
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=project) == before
    snapshot = Path(first["runtime_root"])
    assert not subprocess.check_output(["git", "status", "--porcelain"], cwd=snapshot)
    (snapshot / "src/module.py").write_text("tampered")
    with pytest.raises(ValueError, match="modified"):
        module.freeze(project)


@pytest.fixture
def mbpp_cli(tmp_path, monkeypatch):
    module = load_script("run_mbppplus")
    # The CLI deliberately chdirs to its (fake) project root during preflight;
    # restore the caller's cwd at teardown so later tests still see the repo root.
    monkeypatch.chdir(Path.cwd())
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "require_idle_benchmark_containers", lambda: None)
    monkeypatch.setattr(module, "execution_identity", lambda *_: {})
    monkeypatch.setattr(module, "source_identity", lambda _: {"python": "test", "dependencies": {}})
    monkeypatch.setattr(
        module, "get_settings", lambda: SimpleNamespace(hy3_configured=lambda: True)
    )
    bundle = tmp_path / "artifacts/datasets/mbppplus/sample120"
    bundle.mkdir(parents=True)
    (bundle / "dataset_manifest.json").write_text("{}")
    (bundle / "problems.jsonl").write_text("public fixture\n")
    folder = tmp_path / "artifacts/benchmark-readiness"
    folder.mkdir(parents=True)
    (folder / "mbpp.json").write_text(
        json.dumps(
            {
                "schema": MBPP_SMOKE_SCHEMA,
                "ready": True,
                "uses_model_api": False,
                "image": module.DEFAULT_EVALPLUS_IMAGE,
                "execution_source_sha256": {},
                "cases": {
                    name: {
                        "expected": expected,
                        "actual": {key: values[0] for key, values in expected.items()},
                        "infrastructure_error_type": None,
                        "ok": True,
                    }
                    for name, expected in MBPP_SMOKE_EXPECTATIONS.items()
                },
            }
        )
    )
    monkeypatch.setattr(
        module, "_selection_identity", lambda _: ({}, [f"Mbpp/{i}" for i in range(120)], "hash")
    )
    monkeypatch.setattr(
        module,
        "load_problems",
        lambda _: [
            SimpleNamespace(problem_id=f"Mbpp/{i}", requirement="public", function_name="f")
            for i in range(120)
        ],
    )

    class Executor:
        def __init__(self, **kwargs):
            self.limits = kwargs["limits"]

        def preflight(self, *, task_metadata, workspace):
            assert len(task_metadata) == 120
            return SimpleNamespace(ready=True, runtime={"verified_task_count": 120})

    monkeypatch.setattr(module, "MbppPlusDockerRunner", Executor)
    return module


def test_mbpp_preflight_never_instantiates_a_provider(mbpp_cli, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sys.argv", ["run_mbppplus.py", "--project-root", str(tmp_path), "--preflight"]
    )

    def forbidden():
        raise AssertionError("provider must not be constructed")

    monkeypatch.setattr(mbpp_cli, "Hy3OpenAIProvider", forbidden)
    assert mbpp_cli.main() == 0


def test_mbpp_restores_project_cwd_after_executor_preflight(mbpp_cli, tmp_path, monkeypatch):
    displaced = tmp_path / "displaced"
    displaced.mkdir()

    class Executor:
        def __init__(self, **kwargs):
            self.limits = kwargs["limits"]

        def preflight(self, **kwargs):
            os.chdir(displaced)
            return SimpleNamespace(ready=True, runtime={"verified_task_count": 120})

    monkeypatch.setattr(mbpp_cli, "MbppPlusDockerRunner", Executor)
    monkeypatch.setattr(
        "sys.argv", ["run_mbppplus.py", "--project-root", str(tmp_path), "--preflight"]
    )

    assert mbpp_cli.main() == 0
    assert Path.cwd() == tmp_path.resolve()


def test_mbpp_incomplete_generation_preserves_denominator_and_defers_export(
    mbpp_cli, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_mbppplus.py",
            "--project-root",
            str(tmp_path),
            "--run-id",
            "partial",
            "--confirm-real-provider",
        ],
    )
    monkeypatch.setattr(mbpp_cli, "Hy3OpenAIProvider", lambda: object())

    async def generate(**kwargs):
        summary = {"success_count": 95, "provider_error_count": 25, "parse_error_count": 0}
        path = kwargs["output_dir"] / kwargs["run_id"]
        path.mkdir(parents=True)
        (path / "summary.json").write_text(json.dumps(summary))
        return SimpleNamespace(summary=summary)

    monkeypatch.setattr(mbpp_cli, "run_baseline_experiment", generate)

    def forbidden(**kwargs):
        raise AssertionError("incomplete generation must not be exported")

    monkeypatch.setattr(mbpp_cli, "export_mbpp_candidates", forbidden)
    assert mbpp_cli.main() == 2
    report = json.loads(
        (tmp_path / "artifacts/experiments/mbppplus/partial/report.json").read_text()
    )
    assert report["generation_coverage"]["numerator"] == 95
    assert report["generation_coverage"]["denominator"] == 120
    assert report["provider_failure_n"] == 25 and not report["complete"]


def test_container_busy_guard_is_read_only_and_fails_closed(monkeypatch):
    from tracejudge_hy3.lcb.experiment import require_idle_benchmark_containers

    commands = []

    def busy(command, **kwargs):
        commands.append(command)
        assert kwargs["timeout"] <= 15
        return SimpleNamespace(stdout="tracejudge-evalplus-task-other-window\n")

    monkeypatch.setattr(subprocess, "run", busy)
    with pytest.raises(RuntimeError, match="Another benchmark"):
        require_idle_benchmark_containers()
    assert commands == [["docker", "ps", "--format", "{{.Names}}"]]

    def unavailable(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 15)

    monkeypatch.setattr(subprocess, "run", unavailable)
    with pytest.raises(RuntimeError, match="unavailable or busy"):
        require_idle_benchmark_containers()


def test_prepare_stops_on_smoke_failure_without_any_paid_api(tmp_path, monkeypatch):
    module = load_script("prepare_external_benchmarks")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "require_idle_benchmark_containers", lambda: None)
    monkeypatch.setattr("sys.argv", ["prepare_external_benchmarks.py"])
    pointer = tmp_path / "artifacts/benchmark-runtime/current.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(json.dumps({"runtime_root": str(tmp_path / "runtime")}))
    image = tmp_path / "artifacts/datasets/livecodebench/image.json"
    image.parent.mkdir(parents=True)
    image.write_text(json.dumps({"image": "sha256:" + "a" * 64}))
    commands = []

    def invoke(command, **kwargs):
        commands.append(command)
        assert "--confirm-real-provider" not in command
        if command[1].endswith("smoke_external_benchmarks.py"):
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(subprocess, "run", invoke)
    with pytest.raises(subprocess.CalledProcessError):
        module.main()
    assert len(commands) == 2
    assert commands[-1][-2:] == ["--dataset", "mbpp"]


def test_aggregate_report_preserves_full_and_conditional_denominators():
    from tracejudge_hy3.lcb.experiment import fraction

    module = load_script("report_external_benchmarks")
    report = {
        "complete": False,
        "generation_coverage": fraction(95, 120),
        "base_plus_pass_full_denominator": fraction(50, 120),
        "base_plus_pass_conditional_on_execution": fraction(50, 90),
    }
    rendered = module.render(report, None)
    assert "95/120" in rendered and "50/120" in rendered and "50/90" in rendered
    assert "待提供结果" in rendered
