#!/usr/bin/env python3
"""Freeze runtime, smoke real containers and preflight both cohorts; no model API."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from tracejudge_hy3.lcb.experiment import require_idle_benchmark_containers  # noqa: E402


def main():
    # No container creation or source snapshot while another evaluation is active.
    require_idle_benchmark_containers()
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/freeze_benchmark_runtime.py")], cwd=ROOT, check=True
    )
    frozen = json.loads((ROOT / "artifacts/benchmark-runtime/current.json").read_text())
    runtime = Path(frozen["runtime_root"])
    image = json.loads((ROOT / "artifacts/datasets/livecodebench/image.json").read_text())["image"]

    def step(script, *args):
        subprocess.run(
            [sys.executable, str(runtime / "scripts" / script), "--project-root", str(ROOT), *args],
            cwd=ROOT,
            check=True,
        )

    step("run_livecodebench.py", "--prepare")
    step("smoke_external_benchmarks.py", "--dataset", "mbpp")
    step("smoke_external_benchmarks.py", "--dataset", "lcb", "--image", image)
    step("run_mbppplus.py", "--preflight")
    step("run_livecodebench.py", "--preflight")
    print("[ready] Both cohorts passed real container gates. No model API calls were made.")
    print("Run these separately, waiting for each evaluation to finish:")
    for script, run_id in (("run_mbppplus.py", "mbpp120-v1"), ("run_livecodebench.py", "lcb60-v1")):
        print(
            shlex.join(
                [
                    sys.executable,
                    str(runtime / "scripts" / script),
                    "--project-root",
                    str(ROOT),
                    "--run-id",
                    run_id,
                    "--confirm-real-provider",
                ]
            )
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"[blocked] {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, ValueError, subprocess.SubprocessError):
        print(
            "[blocked] Preparation did not pass. No model API was called; inspect the preceding gate result.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
