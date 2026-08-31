"""Fail-closed parser for phase-three method judgments.

Unlike the general MVP parser, this parser does not extract JSON from Markdown
fences or surrounding prose.  A response is valid only when the complete text
is one JSON object that validates against :class:`MethodJudgment`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .contracts import MethodJudgment


@dataclass(frozen=True, slots=True)
class StrictJudgmentParseError(ValueError):
    diagnostic_code: str
    safe_diagnostic: str

    def __str__(self) -> str:
        return self.safe_diagnostic


def _safe_schema_diagnostic(exc: ValidationError) -> str:
    errors: list[dict[str, Any]] = []
    for item in exc.errors(include_url=False, include_input=False):
        errors.append({key: item[key] for key in ("type", "loc", "msg") if key in item})
    return json.dumps(errors, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def parse_method_judgment(raw_text: str) -> MethodJudgment:
    """Parse one entire raw response as strict JSON and validate its schema."""

    if not isinstance(raw_text, str) or not raw_text.strip():
        raise StrictJudgmentParseError("empty_response", "empty_response")
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        diagnostic = f"invalid_json_at_{exc.lineno}_{exc.colno}"
        raise StrictJudgmentParseError("invalid_json", diagnostic) from None
    if not isinstance(payload, dict):
        raise StrictJudgmentParseError("non_object_json", "top_level_json_must_be_object")
    try:
        return MethodJudgment.model_validate(payload)
    except ValidationError as exc:
        raise StrictJudgmentParseError(
            "schema_validation_failed",
            _safe_schema_diagnostic(exc),
        ) from None
