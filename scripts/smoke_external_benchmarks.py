#!/usr/bin/env python3
"""Real Docker smoke checks; no model calls. Persist only aggregate results."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import pickle
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tracejudge_hy3.benchmark.livecodebench import (  # noqa: E402
    LCB_PIN,
    LiveCodeBenchBenchmarkAdapter,
    candidate_from_code,
    parse_source_record,
)
from tracejudge_hy3.dataset.loader import load_problems  # noqa: E402
from tracejudge_hy3.evalplus_mbpp.docker_runner import (  # noqa: E402
    DEFAULT_EVALPLUS_IMAGE,
    MbppPlusDockerRunner,
)
from tracejudge_hy3.evalplus_mbpp.parser import parse_official_result  # noqa: E402
from tracejudge_hy3.evalplus_mbpp.schemas import MbppPlusSample, MbppPlusTaskMetadata  # noqa: E402
from tracejudge_hy3.lcb.docker_runner import LCBDockerRunner  # noqa: E402
from tracejudge_hy3.lcb.experiment import (  # noqa: E402
    execution_identity,
    now,
    require_idle_benchmark_containers,
    write_json,
)


def mbpp(project, work):
    problem = next(
        p
        for p in load_problems(project / "artifacts/datasets/mbppplus/full/problems.jsonl")
        if p.problem_id == "Mbpp/2"
    )
    meta = MbppPlusTaskMetadata(
        problem_id=problem.problem_id,
        prompt_sha256=hashlib.sha256(problem.requirement.encode()).hexdigest(),
        entry_point=problem.function_name,
    )
    runner = MbppPlusDockerRunner()
    preflight = runner.preflight(task_metadata=[meta], workspace=work)
    if not preflight.ready:
        return {
            "ready": False,
            "image": DEFAULT_EVALPLUS_IMAGE,
            "preflight_error": preflight.infrastructure_error_type,
            "cases": {},
        }
    cases = {}
    implementations = {
        "pass": ("def similar_elements(a, b):\n    return tuple(set(a) & set(b))\n", "pass"),
        "wrong_answer": ("def similar_elements(a, b):\n    return ()\n", "fail"),
        "timeout": ("def similar_elements(a, b):\n    while True:\n        pass\n", "timeout"),
    }
    for name, (code, expected) in implementations.items():
        folder = work / name
        folder.mkdir()
        sample = MbppPlusSample(task_id=problem.problem_id, solution=code)
        result = runner.run_task(sample=sample, task_metadata=meta, workspace=folder)
        if result.infrastructure_error_type:
            actual = result.infrastructure_error_type
        else:
            safe = parse_official_result(
                result.raw_result,
                expected_problem_id=problem.problem_id,
                expected_solution_sha256=hashlib.sha256(code.encode()).hexdigest(),
            )
            actual = safe["base_status"]
            if expected == "pass" and not safe["passed_plus"]:
                actual = "plus_failed"
        cases[name] = {"expected": expected, "actual": actual, "ok": actual == expected}
        print(f"[mbpp smoke] {name}: {actual}", flush=True)
    return {
        "ready": all(c["ok"] for c in cases.values()),
        "image": DEFAULT_EVALPLUS_IMAGE,
        "runtime": preflight.runtime,
        "cases": cases,
        "uses_model_api": False,
    }


def lcb(project, work, image):
    checker = project / "artifacts/external/livecodebench-checker"
    if any(
        hashlib.sha256((checker / f.name).read_bytes()).hexdigest() != f.sha256
        for f in LCB_PIN.checker_files
    ):
        raise ValueError("checker files differ from pin")
    commit = subprocess.check_output(
        ["git", "-C", str(checker), "rev-parse", "HEAD"], text=True
    ).strip()
    if commit != LCB_PIN.checker_commit:
        raise ValueError("checker commit differs")
    details = json.loads(subprocess.check_output(["docker", "image", "inspect", image], text=True))[
        0
    ]
    if details["Architecture"] != "amd64" or details["Os"] != "linux":
        raise ValueError("image is not linux/amd64")
    runner = LCBDockerRunner(image=image)
    runner.verify_image_identity()
    # Self-authored fixture serialization only; never unpickle real data here.
    public = [
        {"input": "1\n", "output": "1\n", "testtype": "stdin"},
        {"input": "2\n", "output": "2\n", "testtype": "stdin"},
    ]
    private = [{"input": "3\n", "output": "3\n", "testtype": "stdin"}]
    opaque = base64.b64encode(zlib.compress(pickle.dumps(json.dumps(private)))).decode()
    raw = {
        "question_title": "Echo integer smoke fixture",
        "question_content": "Read an integer and print it.",
        "platform": "atcoder",
        "question_id": "tracejudge-smoke-echo",
        "contest_id": "smoke",
        "contest_date": "2024-08-15T00:00:00",
        "starter_code": "",
        "difficulty": "easy",
        "public_test_cases": json.dumps(public),
        "private_test_cases": opaque,
        "metadata": "{}",
    }
    record = parse_source_record(raw, source_file="self-authored-smoke.jsonl")
    adapter = LiveCodeBenchBenchmarkAdapter()
    task = adapter.project_record(record)
    cases = {}
    for name, (code, expected) in {
        "pass": ("print(int(input()))\n", "passed"),
        "wrong_answer": ("print(-999999)\n", "failed"),
        "timeout": ("while True:\n    pass\n", "timeout"),
        "runtime_error": ("raise RuntimeError('smoke')\n", "runtime_error"),
        "compile_error": ("def broken(:\n", "compile_error"),
    }.items():
        candidate = candidate_from_code(task=task, candidate_id="smoke-" + name, code=code)
        result = runner.run_task(
            task=task,
            candidate=candidate,
            public_test_cases_raw=record.public_test_cases_raw,
            private_test_cases_raw=record.private_test_cases_raw,
            public_test_count=2,
            per_test_timeout_seconds=1,
            workspace=work,
        )
        normalized = adapter.normalize_execution_result(
            task=task, candidate=candidate, result=result
        )
        actual = normalized.status.value
        cases[name] = {
            "expected": expected,
            "actual": actual,
            "ok": actual == expected,
            "failure_kind": normalized.failure_kind,
        }
        print(f"[lcb smoke] {name}: {actual}", flush=True)
    ready = all(c["ok"] for c in cases.values())
    if ready:
        write_json(
            project / "artifacts/datasets/livecodebench/image.json",
            {"image": image, "platform": "linux/amd64", "checker_commit": commit},
        )
    return {
        "ready": ready,
        "image": image,
        "checker_commit": commit,
        "cases": cases,
        "uses_model_api": False,
        "fixture": "self-authored echo, official checker in real Docker",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", choices=("mbpp", "lcb"), required=True)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--image", help="LCB local image ID or pinned repository reference")
    args = parser.parse_args()
    try:
        require_idle_benchmark_containers()
    except RuntimeError as exc:
        print(f"[blocked] {exc}")
        return 1
    project = args.project_root.resolve()
    output = project / "artifacts/benchmark-readiness"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{args.dataset}-smoke-", dir=output) as folder:
        if args.dataset == "mbpp":
            report = mbpp(project, Path(folder))
        else:
            if not args.image:
                parser.error("--image is required for LCB")
            report = lcb(project, Path(folder), args.image)
    report["completed_at"] = now()
    report["execution_source_sha256"] = execution_identity(ROOT, args.dataset)
    write_json(output / f"{args.dataset}.json", report)
    print(f"[ready={report['ready']}] {output / (args.dataset + '.json')}")
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
