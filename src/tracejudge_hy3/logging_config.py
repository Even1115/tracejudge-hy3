"""Logging setup. Never logs API keys or full sensitive headers."""

from __future__ import annotations

import logging

_CONFIGURED = False


def get_logger(name: str) -> logging.Logger:
    global _CONFIGURED
    if not _CONFIGURED:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        _CONFIGURED = True
    return logging.getLogger(name)


def redact_secret(value: str | None) -> str:
    """Return a placeholder without exposing even a prefix/suffix of a secret."""

    if not value:
        return "<unset>"
    return "<configured>"
