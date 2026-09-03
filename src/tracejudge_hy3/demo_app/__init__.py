"""Local recording demo web app for TraceJudge-Hy3.

A small localhost-only (127.0.0.1) web page that runs the project's real
evaluation pipeline on demand for contest screen recordings:

- "fixture" mode runs the public self-constructed ``safe_mean`` fixture through
  the deterministic Mock provider and the trusted-local sandbox -- the same
  pipeline as ``tracejudge demo --mock --case faulty``.
- "hy3" mode runs the real Hy3 provider with the Docker sandbox -- equivalent
  to ``tracejudge run --dataset data/sample_problems.jsonl --problem-id
  safe_mean --provider hy3 --sandbox docker``.

The server never exposes credentials, absolute paths, or private research
materials to the browser; see ``docs/demo/real_recording_guide.md``.
"""

from tracejudge_hy3.demo_app.overview import OverviewSourceError, load_public_overview
from tracejudge_hy3.demo_app.runner import DEMO_MODES, DEMO_PROBLEM_ID, run_demo

__all__ = [
    "DEMO_MODES",
    "DEMO_PROBLEM_ID",
    "OverviewSourceError",
    "load_public_overview",
    "run_demo",
]
