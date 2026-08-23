"""Format conservative source locations from AST evidence."""

from __future__ import annotations

import re

from tracejudge_hy3.schemas.execution import ExecutionSummary, StaticEvidence


def function_code_span(evidence: StaticEvidence) -> str | None:
    """Return the analyzed function span as ``Lx`` or ``Lx-Ly``."""

    start = evidence.function_start_line
    end = evidence.function_end_line
    if start is None:
        return None
    if end is None or end == start:
        return f"L{start}"
    return f"L{start}-L{end}"


def best_available_code_span(
    evidence: StaticEvidence,
    execution: ExecutionSummary | None = None,
) -> str | None:
    """Prefer a syntax-error line, then fall back to the whole function span."""

    if execution and execution.setup_error:
        match = re.search(r"\bline\s+(\d+)\b", execution.setup_error, re.IGNORECASE)
        if match:
            return f"L{match.group(1)}"
    return function_code_span(evidence)
