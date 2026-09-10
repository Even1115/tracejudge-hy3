"""Freeze the held-out method-evaluation set from the reserved candidates.

Two audits run BEFORE the freeze is written, both fully deterministic:

1. Near-duplicate audit vs the development split: identical function names or
   boilerplate-stripped requirement Jaccard >= 0.7 exclude a reserved item;
   0.4-0.7 is flagged but retained. Requirement texts are compared
   mechanically only; no reserved text is opened for reading.
2. History audit: which reserved parent tasks appear in earlier project
   materials (phase1 generation runs, docs, phase3/phase4 data) and in what
   role. Exposure is recorded honestly, not hidden.

Outputs:
- artifacts/process-heldout/mbpp84-audit-20260909-v1/heldout-audit.json
- data/manifests/process_heldout_v1.json

No labels are created; the held-out set stays unlabeled per the annotation
guide (freeze protocol and hashes BEFORE any labeling).

Run from the repository root:
    PYTHONPATH=src python scripts/freeze_process_heldout.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tracejudge_hy3.process_eval_v2.materials import digest  # noqa: E402
from tracejudge_hy3.process_eval_v2.preflight import by_id, read_rows  # noqa: E402

PACKET_DIR = ROOT / "artifacts/process-annotations/mbpp120-process-v1"
AUDIT_DIR = ROOT / "artifacts/process-heldout/mbpp84-audit-20260909-v1"
MANIFEST_PATH = ROOT / "data/manifests/process_heldout_v1.json"

PACKET_MANIFEST_SHA256 = "f1d6733de1e6c8f9de8bf1f01a18c004848973dc2f085ed1acd5d59be1b003da"

EXCLUDE_JACCARD = 0.7
FLAG_JACCARD = 0.4

STOPWORDS = set(
    "write a function to the given that returns return true false if assert "
    "check whether or not and in of is are an it find all for from with by as "
    "on at be this these those".split()
)

HISTORY_SCAN_GLOBS = (
    "artifacts/experiments/phase1-mbpp/*/responses.jsonl",
    "docs/**/*.md",
    "data/phase3/*",
    "data/phase4/*",
)
# The freeze's own protocol document would otherwise record itself as exposure.
HISTORY_SCAN_EXCLUDE = {"docs/experiments/process-heldout-v1.md"}


def function_name(signature: str) -> str | None:
    match = re.match(r"def\s+([A-Za-z_][A-Za-z0-9_]*)", signature.strip())
    return match.group(1) if match else None


def content_tokens(requirement: str) -> set[str]:
    head = requirement.split("assert")[0]
    return {
        token
        for token in re.findall(r"[a-z0-9]+", head.lower())
        if token not in STOPWORDS and len(token) > 2
    }


def main() -> None:
    manifest_raw = (PACKET_DIR / "manifest.json").read_bytes()
    if digest(manifest_raw) != PACKET_MANIFEST_SHA256:
        raise SystemExit("source packet differs from the pinned manifest")
    mapping = by_id(
        read_rows((PACKET_DIR / "coordinator/identity_and_outcomes.jsonl").read_bytes())
    )
    exclusions = read_rows((PACKET_DIR / "coordinator/exclusions.jsonl").read_bytes())
    if exclusions:
        raise SystemExit("coordinator exclusions must be empty for this freeze")

    def load_split(name):
        return by_id(read_rows((PACKET_DIR / f"{name}/annotator_a/items.jsonl").read_bytes()))

    development = load_split("development")
    reserved = load_split("reserved_candidate")
    if set(development) & set(reserved):
        raise SystemExit("development and reserved splits overlap")

    dev_names: dict[str, list[str]] = {}
    dev_tokens: dict[str, set[str]] = {}
    dev_parent: dict[str, str] = {}
    for item_id, material in development.items():
        name = function_name(material["problem"]["function_signature"])
        dev_names.setdefault(name, []).append(item_id)
        dev_tokens[item_id] = content_tokens(material["problem"]["requirement"])
        dev_parent[item_id] = mapping[item_id]["parent_task_id"]

    # --- History audit: earlier materials mentioning reserved parents ---
    reserved_parents = {mapping[i]["parent_task_id"] for i in reserved}
    history: dict[str, dict] = {parent: {"runs": [], "docs": []} for parent in reserved_parents}
    for pattern in HISTORY_SCAN_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            if not path.is_file():
                continue
            rel = path.relative_to(ROOT).as_posix()
            if rel in HISTORY_SCAN_EXCLUDE:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for parent in sorted(reserved_parents):
                # Exact task-id match: "Mbpp/72" must not match inside "Mbpp/725".
                if re.search(rf"{re.escape(parent)}(?!\d)", text):
                    bucket = "runs" if "responses.jsonl" in rel else "docs"
                    history[parent][bucket].append(rel)

    # --- Near-duplicate audit (mechanical comparison only) ---
    items = []
    excluded, flagged = [], []
    for item_id, material in sorted(reserved.items()):
        parent = mapping[item_id]["parent_task_id"]
        name = function_name(material["problem"]["function_signature"])
        tokens = content_tokens(material["problem"]["requirement"])
        best_score, best_dev = 0.0, None
        for dev_id, dev_tok in dev_tokens.items():
            union = len(tokens | dev_tok)
            score = len(tokens & dev_tok) / union if union else 0.0
            if score > best_score:
                best_score, best_dev = score, dev_id
        name_match = name in dev_names
        if name_match or best_score >= EXCLUDE_JACCARD:
            status = "excluded_near_duplicate"
            excluded.append(parent)
        elif best_score >= FLAG_JACCARD:
            status = "held_out_flagged_similarity"
            flagged.append(parent)
        else:
            status = "held_out"
        items.append(
            {
                "item_id": item_id,
                "parent_task_id": parent,
                "packet_item_sha256": mapping[item_id]["packet_item_sha256"],
                "status": status,
                "near_duplicate_audit": {
                    "function_name": name,
                    "function_name_matches_development": name_match,
                    "best_development_parent": dev_parent.get(best_dev),
                    "best_jaccard_boilerplate_stripped": round(best_score, 4),
                },
                "history_audit": history[parent],
            }
        )

    held_out = [item for item in items if item["status"].startswith("held_out")]
    audit = {
        "schema": "tracejudge-process-heldout-audit-v1",
        "created": "2026-09-09",
        "source_packet": {
            "path": "artifacts/process-annotations/mbpp120-process-v1/manifest.json",
            "sha256": PACKET_MANIFEST_SHA256,
        },
        "rules": {
            "exclude_if_function_name_matches_development": True,
            "exclude_if_jaccard_boilerplate_stripped_gte": EXCLUDE_JACCARD,
            "flag_if_jaccard_boilerplate_stripped_gte": FLAG_JACCARD,
            "stopwords": sorted(STOPWORDS),
            "note": (
                "mechanical token comparison only; reserved texts were not opened "
                "for reading during this audit"
            ),
        },
        "history_scan_globs": list(HISTORY_SCAN_GLOBS),
        "counts": {
            "reserved_candidates": len(items),
            "held_out": len(held_out),
            "excluded_near_duplicate": len(excluded),
            "flagged_similarity_retained": len(flagged),
        },
        "excluded_parents": sorted(excluded),
        "flagged_parents": sorted(flagged),
        "items": items,
    }
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    audit_path = AUDIT_DIR / "heldout-audit.json"
    audit_path.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    manifest = {
        "schema": "tracejudge-process-heldout-v1",
        "created": "2026-09-09",
        "source_packet": audit["source_packet"],
        "audit": {
            "path": audit_path.relative_to(ROOT).as_posix(),
            "sha256": digest(audit_path.read_bytes()),
        },
        "label_status": "unlabeled_no_labeling_started",
        "freeze_commitments": [
            "item set, exclusion rules and material hashes frozen before any labeling",
            "no method predictions on held-out items may influence label creation",
            "labels, once created, are bound by a new config with hash-pinned files",
            "held-out texts stay unopened until the labeling protocol starts",
            "counterfactual bundle and development labels never mix into held-out statistics",
        ],
        "items": [
            {
                "item_id": item["item_id"],
                "parent_task_id": item["parent_task_id"],
                "packet_item_sha256": item["packet_item_sha256"],
                "status": item["status"],
            }
            for item in items
        ],
    }
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {audit_path.relative_to(ROOT)}")
    print(f"wrote {MANIFEST_PATH.relative_to(ROOT)}")
    print(f"held_out={len(held_out)} excluded={len(excluded)} flagged={len(flagged)}")
    print(f"excluded: {sorted(excluded)}")
    print(f"flagged: {sorted(flagged)}")


if __name__ == "__main__":
    main()
