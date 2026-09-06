#!/usr/bin/env python3
"""Sandboxed differential verification of three CodeJudge-Eval mismatch cases.

For each full-v1 functional mismatch where static analysis suspects gold-label
noise, the dataset candidate code is executed inside the project's Docker
isolation posture (no network, read-only mount, dropped capabilities, memory/
cpu/pids limits) and compared against a locally-computed reference:

- easy/4123  two-gram:  exhaustive |{A,B,C}|^2..10; any max-count gram accepted
- medium/83  division:  random + targeted arrays; printed d re-validated
                        arithmetically against the problem condition
- hard/171   password:  exhaustive {A,a,1,!}^1..8 (covers the full
                        upper/lower/digit/special decision space) + random
                        full-charset strings; strict ASCII reference

easy/135 is settled analytically (early-exit bound, see
docs/codejudge_eval_error_analysis.md) and needs no execution.

Usage: python3 scripts/differential_verify_codejudge.py
"""

from __future__ import annotations

import itertools
import json
import random
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tracejudge_hy3.benchmark.codejudge_eval import ZERO_SHOT_FILES  # noqa: E402
from tracejudge_hy3.benchmark.contracts import BenchmarkDifficulty  # noqa: E402

DATA_DIR = REPO_ROOT / "data" / "codejudge_eval"
IMAGE = "python:3.11-slim"
PER_CASE_TIMEOUT_SECONDS = 2
HOST_CONTAINER_TIMEOUT_SECONDS = 900

DRIVER = r"""
import builtins, contextlib, io, json, signal, sys

candidate = open("/sandbox/candidate.py", encoding="utf-8").read()
cases = json.load(open("/sandbox/cases.json", encoding="utf-8"))

def check(mode, out, case):
    if mode == "exact":
        return out == case["expected"]
    if mode == "any_of":
        return out in case["expected"]
    if mode == "division_d":
        try:
            d = int(out)
        except (TypeError, ValueError):
            return False
        arr = case["arr"]
        required = -(-len(arr) // 2)
        if d == 0:
            return not case["valid_exists"]
        return (
            -1000 <= d <= 1000
            and sum(1 for a in arr if a / d > 0) >= required
        )
    raise ValueError(mode)

summary = {"total": len(cases), "ok": 0, "mismatch": 0, "timeout": 0, "error": 0,
           "examples": []}
for index, case in enumerate(cases):
    lines = iter(case["stdin"].rstrip("\n").split("\n"))
    def fake_input(prompt="", _lines=lines):
        return next(_lines)
    buf = io.StringIO()
    def handler(signum, frame):
        raise TimeoutError
    signal.signal(signal.SIGALRM, handler)
    signal.alarm(PER_CASE_TIMEOUT)
    real_input = builtins.input
    builtins.input = fake_input
    try:
        with contextlib.redirect_stdout(buf):
            exec(compile(candidate, "candidate.py", "exec"), {"__name__": "__main__"})
        out = buf.getvalue().strip()
        ok = check(case["mode"], out, case)
        key = "ok" if ok else "mismatch"
    except TimeoutError:
        ok, key, out = False, "timeout", None
    except Exception as exc:
        ok, key, out = False, "error", f"{type(exc).__name__}: {exc}"
    finally:
        builtins.input = real_input
        signal.alarm(0)
    summary[key] += 1
    if not ok and len(summary["examples"]) < 10:
        summary["examples"].append({"i": index, "kind": key, "out": out,
                                    "stdin": case["stdin"][:200]})
print(json.dumps(summary))
""".replace("PER_CASE_TIMEOUT", str(PER_CASE_TIMEOUT_SECONDS))


def _extract_candidate(difficulty: BenchmarkDifficulty, task_id: int, data_id: int) -> str:
    payload = json.loads((DATA_DIR / ZERO_SHOT_FILES[difficulty]).read_text(encoding="utf-8"))
    record = next(r for r in payload if r["task_id"] == task_id and r["data_id"] == data_id)
    return record["code"]


def _run_in_docker(candidate: str, cases: list[dict]) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "candidate.py").write_text(candidate, encoding="utf-8")
        (work / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
        (work / "driver.py").write_text(DRIVER, encoding="utf-8")
        cmd = [
            "docker",
            "run",
            "--rm",
            "--name",
            f"tracejudge-cjeval-{uuid.uuid4().hex}",
            "--network",
            "none",
            "--read-only",
            "--memory",
            "256m",
            "--cpus",
            "1",
            "--pids-limit",
            "128",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/tmp",
            "-v",
            f"{work}:/sandbox:ro",
            "-w",
            "/sandbox",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            IMAGE,
            "python",
            "driver.py",
        ]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=HOST_CONTAINER_TIMEOUT_SECONDS
        )
    if proc.returncode != 0:
        raise RuntimeError(f"container failed: {proc.stderr[-500:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _two_gram_cases() -> list[dict]:
    cases = []
    for length in range(2, 11):
        for tup in itertools.product("ABC", repeat=length):
            s = "".join(tup)
            counts: dict[str, int] = {}
            for i in range(length - 1):
                counts[s[i : i + 2]] = counts.get(s[i : i + 2], 0) + 1
            best = max(counts.values())
            acceptable = sorted(g for g, v in counts.items() if v == best)
            cases.append({"mode": "any_of", "stdin": f"{length}\n{s}", "expected": acceptable})
    return cases


def _division_cases() -> list[dict]:
    rng = random.Random(20240904)
    cases = []

    def add(arr: list[int]) -> None:
        required = -(-len(arr) // 2)
        valid_exists = any(
            d != 0 and sum(1 for a in arr if a / d > 0) >= required for d in range(-1000, 1001)
        )
        cases.append(
            {
                "mode": "division_d",
                "stdin": f"{len(arr)}\n{' '.join(map(str, arr))}",
                "arr": arr,
                "valid_exists": valid_exists,
            }
        )

    targeted = [
        [0],
        [5],
        [-5],
        [0, 0],
        [1, -1],
        [3, 3, -3],
        [0, 1, -1],
        [1000, -1000, 1000],
        [0, 0, 0, 0, 1],
        [7, -2, -2, -2, 0],
    ]
    for arr in targeted:
        add(arr)
    for _ in range(5000):
        n = rng.randint(1, 12)
        arr = [rng.choice([0, 1, -1, 2, -2, 3, -3, 1000, -1000]) for _ in range(n)]
        add(arr)
    for _ in range(200):
        n = rng.randint(90, 100)
        arr = [rng.randint(-1000, 1000) for _ in range(n)]
        add(arr)
    return cases


def _password_cases() -> list[dict]:
    def reference(p: str) -> str:
        ok = (
            len(p) >= 5
            and any("A" <= c <= "Z" for c in p)
            and any("a" <= c <= "z" for c in p)
            and any("0" <= c <= "9" for c in p)
        )
        return "Correct" if ok else "Too weak"

    cases = []
    for length in range(1, 9):
        for tup in itertools.product("Aa1!", repeat=length):
            s = "".join(tup)
            cases.append({"mode": "exact", "stdin": s, "expected": reference(s)})
    rng = random.Random(20240904)
    charset = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!?.,_"
    for _ in range(2000):
        s = "".join(rng.choice(charset) for _ in range(rng.randint(1, 100)))
        cases.append({"mode": "exact", "stdin": s, "expected": reference(s)})
    return cases


def _resolve_image_digest(image: str) -> str | None:
    """Resolve the immutable digest for the sandbox image (None if unavailable).

    The tag (``python:3.11-slim``) can drift; future verification runs record
    the resolved digest so the execution environment is pinned.
    """

    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", image, "--format", "{{json .RepoDigests}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        digests = json.loads(proc.stdout) if proc.returncode == 0 else []
    except (OSError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return None
    return digests[0] if digests else None


def main() -> None:
    plans = [
        (
            "easy/4123 (two-gram, gold=NotAC judge=AC)",
            BenchmarkDifficulty.EASY,
            4123,
            1954,
            _two_gram_cases,
        ),
        (
            "medium/83 (division, gold=WA judge=AC)",
            BenchmarkDifficulty.MEDIUM,
            83,
            350,
            _division_cases,
        ),
        (
            "hard/171 (password, gold=WA judge=AC)",
            BenchmarkDifficulty.HARD,
            171,
            754,
            _password_cases,
        ),
    ]
    verdicts = {"sandbox_image": IMAGE, "sandbox_image_digest": _resolve_image_digest(IMAGE)}
    for label, difficulty, task_id, data_id, case_fn in plans:
        candidate = _extract_candidate(difficulty, task_id, data_id)
        cases = case_fn()
        summary = _run_in_docker(candidate, cases)
        verdicts[label] = summary
        status = (
            "MATCHES reference (gold label suspect)"
            if (summary["mismatch"] == 0 and summary["timeout"] == 0 and summary["error"] == 0)
            else "DEVIATES from reference (gold label supported)"
        )
        print(f"== {label}: {status}")
        print(f"   {json.dumps({k: v for k, v in summary.items() if k != 'examples'})}")
        for ex in summary["examples"][:5]:
            print("   example:", json.dumps(ex, ensure_ascii=False))
    out = DATA_DIR / "differential_verification.json"
    out.write_text(json.dumps(verdicts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"written: {out}")


if __name__ == "__main__":
    main()
