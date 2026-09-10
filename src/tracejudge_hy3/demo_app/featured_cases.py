"""Two published case excerpts, independent of fixture regression and formal scoring."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

BUNDLE = Path("docs/releases/featured_process_cases_v1.json")
BUNDLE_SHA256 = "6ed7c50fe4756f38099a8ecd08780b695ec6fe75b812dc0b2cb821efc10e1008"
REVIEW_DOCS = {
    "tuple_str_int": Path("docs/experiments/process-method-validation-v2-repair.md"),
    "find_char_long": Path("docs/experiments/process-location-format-v2.md"),
}


class FeaturedCaseError(ValueError):
    """A published excerpt or available original no longer matches its identity."""


def _digest(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _contained(root: Path, relative: Path) -> Path:
    target = root / relative
    if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
        raise FeaturedCaseError("Source outside repository")
    return target


def load_featured_cases(repo_root: str | Path) -> dict:
    """Verify the publishable bundle and each available original; never infer new results.

    Canonical JSON hashes survive Git line-ending conversion. Byte hashes are retained
    as source provenance. Missing originals are explicitly marked as excerpt-only.
    """
    root = Path(repo_root)
    try:
        data = json.loads(_contained(root, BUNDLE).read_bytes())
        if _digest(data) != BUNDLE_SHA256:
            raise FeaturedCaseError("Published case checksum mismatch")
        sources = data["sources"]
        for source in sources.values():
            path = _contained(root, Path(source["path"]))
            if not path.exists():
                source["verification"] = "published_excerpt_only"
                continue
            raw = path.read_bytes()
            content = (
                [json.loads(line) for line in raw.decode("utf-8-sig").splitlines() if line.strip()]
                if path.suffix == ".jsonl"
                else json.loads(raw)
            )
            if _digest(content) != source["content_sha256"]:
                raise FeaturedCaseError("Available source checksum mismatch")
            source["verification"] = "local_content_verified"
        for case in data["cases"]:
            case["review_href"] = "/docs/featured-process/" + case["id"]
        return {"ok": True, **data, "bundle_sha256": BUNDLE_SHA256}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise FeaturedCaseError("Featured case evidence unavailable") from exc


def read_review_doc(repo_root: Path, case_id: str) -> bytes:
    """Only the two explicit review documents can be served."""
    if case_id not in REVIEW_DOCS:
        raise FeaturedCaseError("Unknown case")
    try:
        return _contained(repo_root, REVIEW_DOCS[case_id]).read_bytes()
    except OSError as exc:
        raise FeaturedCaseError("Review unavailable") from exc
