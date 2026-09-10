"""Opt-in request accounting and separately sanitized failure diagnostics."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

request_observer: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "request_observer", default=None
)

# Providers must sanitize error/response text BEFORE emitting to this sink.
failure_observer: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "failure_observer", default=None
)


def observe_request(response: Any, elapsed: float, error_type: str | None) -> None:
    observer = request_observer.get()
    if observer is None:
        return
    usage = getattr(response, "usage", None)

    def tokens(name: str) -> int | None:
        value = getattr(usage, name, None)
        return value if type(value) is int and value >= 0 else None

    observer(
        {
            "input_tokens": tokens("prompt_tokens"),
            "output_tokens": tokens("completion_tokens"),
            "request_seconds": round(elapsed, 6),
            "status": "request_error" if error_type else "response_received",
            "error_type": error_type,
        }
    )
