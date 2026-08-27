"""Parse a model's raw text response into a validated Pydantic object.

Prompts instruct the model to emit bare JSON, but real models sometimes wrap
it in Markdown fences or add a stray sentence before/after. This module gives
limited, best-effort tolerance for that -- it does not attempt to recover from
arbitrarily malformed output.
"""

from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from tracejudge_hy3.exceptions import ParsingError

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

T = TypeVar("T", bound=BaseModel)


def extract_json_text(raw_text: str) -> str:
    text = raw_text.strip()
    if not text:
        raise ParsingError("empty model response")

    fence_match = _FENCE_RE.search(text)
    if fence_match:
        candidate = fence_match.group(1).strip()
        if candidate:
            return candidate

    start = text.find("{")
    if start == -1:
        raise ParsingError("no JSON object found in model response")

    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(text)):
        char = text[idx]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]

    raise ParsingError("unterminated JSON object in model response")


def parse_structured_output(raw_text: str, model: type[T]) -> T:
    json_text = extract_json_text(raw_text)
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise ParsingError(f"invalid JSON: {exc}") from exc
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        # Pydantic's default string includes ``input_value`` and can echo
        # credentials or full request headers produced by a model. Keep only
        # the diagnostic fields required for repair.
        safe_errors = [
            {key: item[key] for key in ("type", "loc", "msg") if key in item}
            for item in exc.errors(include_url=False, include_input=False)
        ]
        details = json.dumps(safe_errors, ensure_ascii=False, separators=(",", ":"))
        raise ParsingError(f"schema validation failed: {details}") from exc
