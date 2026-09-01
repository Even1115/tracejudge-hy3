"""Deterministic hashing and fail-closed checks for public phase-three payloads."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel


class PublicPayloadError(ValueError):
    """A payload intended for publication crossed a phase-three privacy boundary."""


_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "apikey",
        "accesstoken",
        "authorization",
        "canonicalsolution",
        "cookie",
        "env",
        "evalplusrawresults",
        "evalplusraw",
        "failureinput",
        "failureinputs",
        "hiddeninput",
        "hiddeninputs",
        "officialfailureinput",
        "officialfailureinputs",
        "officialtestinput",
        "officialtestinputs",
        "plusfailtests",
        "basefailtests",
        "proxycredential",
        "proxycredentials",
        "rawoutput",
        "referencecode",
        "requestheaders",
        "samples",
        "secret",
        "token",
    }
)


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _json_ready(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize one JSON-compatible value deterministically for identity hashing."""

    return json.dumps(
        _json_ready(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    """Return the SHA256 of :func:`canonical_json_bytes`."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def jsonl_record_sha256(record: bytes) -> str:
    """Hash one exact UTF-8 JSONL record, including its required final LF."""

    if record == b"\n" or not record.endswith(b"\n") or record.count(b"\n") != 1:
        raise ValueError("JSONL record must be exactly one non-empty line ending in LF")
    try:
        record.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("JSONL record must be valid UTF-8") from exc
    return hashlib.sha256(record).hexdigest()


def _contains_canary(value: str | bytes, canaries: Sequence[str | bytes]) -> bool:
    if isinstance(value, bytes):
        return any(
            (canary.encode("utf-8") if isinstance(canary, str) else canary) in value
            for canary in canaries
        )
    return any(
        (canary.decode("utf-8", errors="ignore") if isinstance(canary, bytes) else canary) in value
        for canary in canaries
    )


def _walk_public_payload(
    value: Any,
    *,
    path: str,
    canaries: Sequence[str | bytes],
) -> None:
    value = _json_ready(value)
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            item_path = f"{path}.{key_text}"
            if _normalized_key(key_text) in _FORBIDDEN_PUBLIC_KEYS:
                raise PublicPayloadError(f"public payload contains forbidden key at {item_path}")
            if _contains_canary(key_text, canaries):
                raise PublicPayloadError(f"public payload contains a canary in a key at {path}")
            _walk_public_payload(item, path=item_path, canaries=canaries)
        return
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        for index, item in enumerate(value):
            _walk_public_payload(item, path=f"{path}[{index}]", canaries=canaries)
        return
    if isinstance(value, str | bytes) and _contains_canary(value, canaries):
        raise PublicPayloadError(f"public payload contains a canary at {path}")


def assert_public_payload_safe(
    value: Any,
    *,
    canaries: Sequence[str | bytes] = (),
) -> None:
    """Reject sensitive field names and caller-provided canaries without echoing values."""

    _walk_public_payload(value, path="$", canaries=canaries)
