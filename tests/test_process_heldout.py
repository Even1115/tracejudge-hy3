"""Tests for the held-out freeze manifest and its audit.

Pure offline integrity checks against the frozen packet: no labeling, no
provider calls, no candidate execution. The deterministic freeze script is
also re-run into a temp location to confirm reproducibility of the audit
rules... no — the script writes fixed repo paths, so these tests validate
the committed artifacts directly and recompute the audits from the packet
to confirm the recorded results are reproducible.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tracejudge_hy3.process_eval_v2.materials import canonical, digest
from tracejudge_hy3.process_eval_v2.preflight import by_id, read_rows

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKET_DIR = REPO_ROOT / "artifacts/process-annotations/mbpp120-process-v1"
MANIFEST_PATH = REPO_ROOT / "data/manifests/process_heldout_v1.json"
AUDIT_PATH = REPO_ROOT / "artifacts/process-heldout/mbpp84-audit-20260909-v1/heldout-audit.json"

pytestmark = pytest.mark.skipif(
    not MANIFEST_PATH.exists() or not AUDIT_PATH.exists() or not PACKET_DIR.exists(),
    reason="held-out freeze artifacts or source packet not present",
)

STOPWORDS = set(
    "write a function to the given that returns return true false if assert "
    "check whether or not and in of is are an it find all for from with by as "
    "on at be this these those".split()
)


def _content_tokens(requirement: str) -> set[str]:
    head = requirement.split("assert")[0]
    return {
        token
        for token in re.findall(r"[a-z0-9]+", head.lower())
        if token not in STOPWORDS and len(token) > 2
    }


def _function_name(signature: str) -> str | None:
    match = re.match(r"def\s+([A-Za-z_][A-Za-z0-9_]*)", signature.strip())
    return match.group(1) if match else None


@pytest.fixture(scope="module")
def manifest():
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def audit():
    return json.loads(AUDIT_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def packet():
    mapping = by_id(
        read_rows((PACKET_DIR / "coordinator/identity_and_outcomes.jsonl").read_bytes())
    )
    dev = by_id(read_rows((PACKET_DIR / "development/annotator_a/items.jsonl").read_bytes()))
    res = by_id(read_rows((PACKET_DIR / "reserved_candidate/annotator_a/items.jsonl").read_bytes()))
    return mapping, dev, res


def test_manifest_schema_and_binding(manifest):
    assert manifest["schema"] == "tracejudge-process-heldout-v1"
    assert manifest["label_status"] == "unlabeled_no_labeling_started"
    assert manifest["source_packet"]["sha256"] == digest(
        (PACKET_DIR / "manifest.json").read_bytes()
    )
    assert manifest["audit"]["sha256"] == digest(AUDIT_PATH.read_bytes())
    assert manifest["freeze_commitments"]


def test_manifest_covers_all_reserved_exactly(manifest, packet):
    mapping, _, reserved = packet
    reserved_ids = set(reserved)
    assert {item["item_id"] for item in manifest["items"]} == reserved_ids
    assert len(manifest["items"]) == 84
    for item in manifest["items"]:
        origin = mapping[item["item_id"]]
        assert item["parent_task_id"] == origin["parent_task_id"]
        assert item["packet_item_sha256"] == origin["packet_item_sha256"]
        assert origin["split"] == "reserved_candidate"


def test_no_overlap_with_development_and_no_labels(manifest, packet):
    _, development, _ = packet
    assert not ({item["item_id"] for item in manifest["items"]} & set(development))
    # Freeze predates labeling: no label fields anywhere in the manifest items.
    for item in manifest["items"]:
        assert set(item) == {
            "item_id",
            "parent_task_id",
            "packet_item_sha256",
            "status",
        }


def test_audit_rules_reproduce_recorded_statuses(audit, packet):
    mapping, development, reserved = packet
    dev_names = {}
    dev_tokens = {}
    for item_id, material in development.items():
        name = _function_name(material["problem"]["function_signature"])
        dev_names.setdefault(name, []).append(item_id)
        dev_tokens[item_id] = _content_tokens(material["problem"]["requirement"])
    assert audit["counts"]["reserved_candidates"] == 84
    assert audit["counts"]["held_out"] == 81
    assert audit["counts"]["excluded_near_duplicate"] == 3
    for record in audit["items"]:
        material = reserved[record["item_id"]]
        name = _function_name(material["problem"]["function_signature"])
        tokens = _content_tokens(material["problem"]["requirement"])
        best = max(
            len(tokens & dt) / len(tokens | dt) if tokens | dt else 0.0
            for dt in dev_tokens.values()
        )
        expected = (
            "excluded_near_duplicate"
            if (name in dev_names or best >= 0.7)
            else ("held_out_flagged_similarity" if best >= 0.4 else "held_out")
        )
        assert record["status"] == expected
        assert record["near_duplicate_audit"]["best_jaccard_boilerplate_stripped"] == (
            pytest.approx(best, abs=1e-4)
        )
    # The exclusions really are duplicates of named development tasks.
    excluded = set(audit["excluded_parents"])
    assert excluded == {"Mbpp/104", "Mbpp/267", "Mbpp/563"}


def test_history_audit_records_phase1_exposure_honestly(audit):
    for record in audit["items"]:
        history = record["history_audit"]
        # All reserved parents appeared in the four phase1 generation runs.
        assert len(history["runs"]) == 4
        assert all("phase1-mbpp" in path for path in history["runs"])
    docs_hits = {
        record["parent_task_id"] for record in audit["items"] if record["history_audit"]["docs"]
    }
    assert docs_hits == {"Mbpp/267", "Mbpp/432", "Mbpp/456", "Mbpp/765"}


def test_audit_item_hashes_match_coordinator(audit, packet):
    mapping, _, _ = packet
    for record in audit["items"]:
        assert record["packet_item_sha256"] == mapping[record["item_id"]]["packet_item_sha256"]
        # Recompute against the actual packet bytes as well.
    reserved_rows = read_rows(
        (PACKET_DIR / "reserved_candidate/annotator_a/items.jsonl").read_bytes()
    )
    digests = {row["item_id"]: digest(canonical(row)) for row in reserved_rows}
    for record in audit["items"]:
        assert record["packet_item_sha256"] == digests[record["item_id"]]
