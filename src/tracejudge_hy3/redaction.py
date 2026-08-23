"""Credential-aware text redaction shared by providers and artifact writers."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

_SENSITIVE_MARKERS = (
    "apikey",
    "authorization",
    "bearer",
    "cookie",
    "credential",
    "header",
    "password",
    "secret",
    "token",
)
_CREDENTIAL_LABEL = (
    r"(?:(?:x[_ -]?)?api[_ -]?key|authorization(?:[_ -]?header)?|access[_ -]?token|"
    r"client[_ -]?secret|cookie|credential|password)"
)


def normalize_unicode_scalars(value: str) -> str:
    """Replace lone UTF-16 surrogates, which cannot be encoded as UTF-8."""

    return re.sub(r"[\ud800-\udfff]", "\ufffd", value)


def is_sensitive_key(key: str) -> bool:
    """Whether a mapping key conventionally contains credential material."""

    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return any(marker in normalized for marker in _SENSITIVE_MARKERS)


def _redact_patterns(value: str) -> str:
    # Quoted assignment/JSON values, including short passwords.
    redacted = re.sub(
        rf"(?i)([\"']?{_CREDENTIAL_LABEL}[\"']?\s*[:=]\s*)([\"'])[^\"'\r\n]*\2",
        r'\1"<redacted>"',
        value,
    )
    # Unquoted token-like assignments, but do not consume function calls such
    # as ``api_key = os.getenv(...)`` that contain no credential value.
    redacted = re.sub(
        rf"(?i)([\"']?{_CREDENTIAL_LABEL}[\"']?\s*[:=]\s*)"
        r"[A-Za-z0-9._~+/=-]{4,}(?![A-Za-z0-9._~+/=-]|\s*\()",
        r"\1<redacted>",
        redacted,
    )
    redacted = re.sub(
        r"(?i)\b((?:authorization\s+)?(?:bearer|basic)\s+)"
        r"[A-Za-z0-9._~+/=-]{4,}",
        r"\1<redacted>",
        redacted,
    )
    redacted = re.sub(
        r"(?i)\b((?:x[_ -]?)?api[_ -]?key\s+)[A-Za-z0-9._~+/=-]{4,}",
        r"\1<redacted>",
        redacted,
    )
    redacted = re.sub(
        r"(?i)([?&](?:api[_-]?key|access[_-]?token)=)[^&#\s]+",
        r"\1<redacted>",
        redacted,
    )
    redacted = re.sub(r"(?i)\bsk-[A-Za-z0-9_-]{8,}\b", "<redacted>", redacted)
    return re.sub(r"://[^/@\s]+:[^/@\s]+@", "://<redacted>@", redacted)


def _safe_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_patterns(normalize_unicode_scalars(value))
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = normalize_unicode_scalars(str(raw_key))
            result[key] = "<redacted>" if is_sensitive_key(key) else _safe_json_value(item)
        return result
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [_safe_json_value(item) for item in value]
    return value


def _json_object_spans(value: str):
    """Yield balanced JSON-object spans; malformed candidates are skipped later."""

    for start, initial in enumerate(value):
        if initial != "{":
            continue
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(value)):
            character = value[index]
            if in_string:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = False
                continue
            if character == '"':
                in_string = True
            elif character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    yield start, index + 1
                    break


def _redact_json_object(value: str) -> str:
    """Structurally scrub the first valid JSON object without always reformatting it."""

    for start, end in _json_object_spans(value):
        candidate = value[start:end]
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        safe_payload = _safe_json_value(payload)
        if safe_payload == payload:
            return value
        replacement = json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":"))
        return value[:start] + replacement + value[end:]
    return value


def redact_sensitive_text(value: str, *, known_secrets: Sequence[str] = ()) -> str:
    """Redact known secrets plus common credential-bearing text/JSON forms."""

    redacted = normalize_unicode_scalars(value)
    for secret in sorted((item for item in known_secrets if item), key=len, reverse=True):
        redacted = redacted.replace(secret, "<redacted>")
    redacted = _redact_json_object(redacted)
    return _redact_patterns(redacted)


def redact_error_text(value: str, *, known_secrets: Sequence[str] = ()) -> str:
    """Additionally remove complete request-header blocks from error text."""

    redacted = re.sub(
        r"(?i)([\"']?(?:request[_ -]?)?headers?[\"']?\s*[:=]\s*)"
        r"(?:\{[^\r\n]*\}|[^\r\n]+)",
        r"\1<redacted>",
        value,
    )
    return redact_sensitive_text(redacted, known_secrets=known_secrets)
