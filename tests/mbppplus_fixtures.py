"""Shared minimal synthetic MBPP+ fixtures for offline tests.

The task ID universe comes from the committed pinned source manifest (public
IDs only).  Every prompt, candidate, and withheld field is synthetic; nothing
here downloads the official corpus or executes candidate code.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PINNED_SOURCE_MANIFEST_PATH = REPO_ROOT / "data" / "manifests" / "evalplus_mbppplus_64fc4195.json"
REVISION = "64fc4195b858a17cdfdb3324f0baf37939144e14"
EXPECTED_RECORD_COUNT = 378
TASK_ID_PATTERN = r"^Mbpp/(?:0|[1-9][0-9]*)$"

PUBLIC_WITHHELD_FIELDS = (
    "assertion",
    "atol",
    "base_input",
    "canonical_solution",
    "contract",
    "plus_input",
)
PUBLISHED_FIELDS = sorted({"task_id", "prompt", "entry_point", *PUBLIC_WITHHELD_FIELDS})


def sha256_text(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def expected_task_ids() -> list[str]:
    manifest = json.loads(PINNED_SOURCE_MANIFEST_PATH.read_text(encoding="utf-8"))
    ids = manifest["expected_task_ids"]
    assert len(ids) == EXPECTED_RECORD_COUNT
    return list(ids)


def raw_row(task_id: str, index: int, *, private_variant: str = "v1") -> dict[str, Any]:
    """One synthetic raw row with canary-laden withheld fields."""

    entry_point = f"solve_task_{index}"
    return {
        "task_id": task_id,
        "prompt": (
            '"""\n'
            f"Write a function {entry_point} that solves synthetic task {index}.\n"
            f"PUBLIC_PROMPT_CANARY_{index}\n"
            f"assert {entry_point}({index}) == {index}\n"
            '"""\n'
        ),
        "entry_point": entry_point,
        "canonical_solution": (
            f"PRIVATE_CANONICAL_CANARY_{private_variant}_{index} = {index}\n"
            f"def {entry_point}(value):\n"
            "    return value\n"
        ),
        "contract": f"# PRIVATE_CONTRACT_CANARY_{private_variant}_{index}\n",
        "base_input": [[f"PRIVATE_BASE_INPUT_CANARY_{private_variant}_{index}"]],
        "plus_input": [[f"PRIVATE_PLUS_INPUT_CANARY_{private_variant}_{index}"]],
        "atol": 0,
        "assertion": f"# PRIVATE_ASSERTION_CANARY_{private_variant}_{index}\n",
    }


@dataclass(frozen=True)
class Snapshot:
    root: Path
    input_path: Path
    source_manifest_path: Path
    raw_bytes: bytes


def write_snapshot(
    root: Path,
    *,
    rows: list[dict[str, Any]] | None = None,
    expected_ids: list[str] | None = None,
    private_variant: str = "v1",
    release_asset_sha256: str | None = None,
) -> Snapshot:
    """Write a synthetic raw JSONL plus a schema-valid pinned source manifest."""

    root.mkdir(parents=True, exist_ok=True)
    ids = expected_ids if expected_ids is not None else expected_task_ids()
    if rows is None:
        rows = [
            raw_row(task_id, index, private_variant=private_variant)
            for index, task_id in enumerate(ids)
        ]
    raw_bytes = b"".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n" for row in rows
    )
    input_path = root / "MbppPlus.jsonl"
    input_path.write_bytes(raw_bytes)
    source_manifest = {
        "schema_version": 1,
        "dataset_id": "evalplus/mbppplus",
        "dataset_card_url": "https://github.com/evalplus/mbppplus_release",
        "revision": REVISION,
        "release_tag": "v0.2.0",
        "license": "apache-2.0",
        "record_count": EXPECTED_RECORD_COUNT,
        "task_id_pattern": TASK_ID_PATTERN,
        "expected_task_ids": ids,
        "official_dataset_md5": "e" * 32,
        "published_fields": PUBLISHED_FIELDS,
        "phase1_public_fields": ["task_id", "prompt", "entry_point"],
        "phase1_withheld_fields": sorted(PUBLIC_WITHHELD_FIELDS),
        "release_asset": {
            "name": "MbppPlus.jsonl.gz",
            "size": 336032,
            "sha256": release_asset_sha256 or ("a" * 64),
        },
        "raw_files": [
            {
                "path": input_path.name,
                "size": len(raw_bytes),
                "sha256": sha256_bytes(raw_bytes),
            }
        ],
    }
    source_manifest_path = root / "source_manifest.json"
    source_manifest_path.write_text(
        json.dumps(source_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return Snapshot(
        root=root,
        input_path=input_path,
        source_manifest_path=source_manifest_path,
        raw_bytes=raw_bytes,
    )


def official_raw_document(
    task_id: str,
    solution: str,
    *,
    base_status: str = "pass",
    plus_status: str = "pass",
    base_fail_tests: list[Any] | None = None,
    plus_fail_tests: list[Any] | None = None,
) -> dict[str, Any]:
    """One pinned-schema single-task official EvalPlus raw document."""

    return {
        "date": "2026-09-05T00:00:00",
        "hash": "f" * 32,
        "eval": {
            task_id: [
                {
                    "task_id": task_id,
                    "solution": solution,
                    "base_status": base_status,
                    "plus_status": plus_status,
                    "base_fail_tests": base_fail_tests or [],
                    "plus_fail_tests": plus_fail_tests or [],
                }
            ]
        },
    }
