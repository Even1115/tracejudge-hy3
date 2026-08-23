"""Run the reference implementation and a candidate implementation on the same
input through a sandbox backend, and report whether their behavior differs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tracejudge_hy3.sandbox.base import SandboxBackend
from tracejudge_hy3.schemas.execution import ExecutionSummary
from tracejudge_hy3.schemas.problem import TestCase


@dataclass
class DifferentialResult:
    reference_output: Any
    reference_exception: str | None
    candidate_output: Any
    candidate_exception: str | None

    @property
    def differs(self) -> bool:
        if self.reference_exception != self.candidate_exception:
            return True
        if self.reference_exception is None and self.candidate_exception is None:
            return self.reference_output != self.candidate_output
        return False


def _outcome(summary: ExecutionSummary) -> tuple[Any, str | None]:
    if summary.runtime_status != "completed" or not summary.results:
        reason = summary.setup_error or summary.runtime_status
        return None, f"SandboxSetupError: {reason}"
    result = summary.results[0]
    return result.actual_output, result.exception_type


def run_differential(
    backend: SandboxBackend,
    reference_code: str,
    candidate_code: str,
    function_name: str,
    args: list[Any],
    kwargs: dict[str, Any] | None = None,
) -> DifferentialResult:
    kwargs = kwargs or {}
    probe = TestCase(case_id="probe", args=args, kwargs=kwargs, expected=None, category="challenge")

    ref_summary = backend.run(reference_code, function_name, [probe])
    cand_summary = backend.run(candidate_code, function_name, [probe])

    ref_output, ref_exc = _outcome(ref_summary)
    cand_output, cand_exc = _outcome(cand_summary)

    return DifferentialResult(
        reference_output=ref_output,
        reference_exception=ref_exc,
        candidate_output=cand_output,
        candidate_exception=cand_exc,
    )
