"""Phase-one baseline generation and artifact persistence.

This package deliberately stops after Solver generation.  It does not import
or invoke any sandbox, test runner, evaluator, or counterexample component.
"""

from tracejudge_hy3.baseline.runner import (
    BaselineExperimentError,
    BaselineRunResult,
    new_baseline_run_id,
    run_baseline_experiment,
)

__all__ = [
    "BaselineExperimentError",
    "BaselineRunResult",
    "new_baseline_run_id",
    "run_baseline_experiment",
]
