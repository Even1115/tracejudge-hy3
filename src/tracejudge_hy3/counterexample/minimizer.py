"""Best-effort delta-debugging shrink for a list-shaped counterexample argument.

This is intentionally narrow: it only shrinks the first list-valued positional
argument with more than one element, by repeatedly trying to remove one
element at a time while the reference/candidate outputs keep differing. It is
not a general ddmin implementation over arbitrary structures.
"""

from __future__ import annotations

from typing import Any

from tracejudge_hy3.counterexample.differential import run_differential
from tracejudge_hy3.sandbox.base import SandboxBackend


def _find_list_arg_index(args: list[Any]) -> int | None:
    for i, value in enumerate(args):
        if isinstance(value, list) and len(value) > 1:
            return i
    return None


def minimize_counterexample_args(
    backend: SandboxBackend,
    reference_code: str,
    candidate_code: str,
    function_name: str,
    args: list[Any],
    kwargs: dict[str, Any],
    max_iterations: int = 50,
) -> tuple[list[Any], bool]:
    """Return (possibly-shrunk args, whether shrinking actually happened)."""

    list_index = _find_list_arg_index(args)
    if list_index is None:
        return args, False

    current_args = list(args)
    current_list = list(current_args[list_index])
    shrunk = False
    iterations = 0

    changed = True
    while changed and len(current_list) > 0 and iterations < max_iterations:
        changed = False
        for i in range(len(current_list)):
            iterations += 1
            trial_list = current_list[:i] + current_list[i + 1 :]
            trial_args = list(current_args)
            trial_args[list_index] = trial_list
            result = run_differential(
                backend, reference_code, candidate_code, function_name, trial_args, kwargs
            )
            if result.differs:
                current_list = trial_list
                current_args[list_index] = trial_list
                shrunk = True
                changed = True
                break
            if iterations >= max_iterations:
                break

    current_args[list_index] = current_list
    return current_args, shrunk
