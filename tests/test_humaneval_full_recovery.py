"""Recovery must never cherry-pick, lose original failures, or trust edited raw."""

import copy
import hashlib
import importlib
from pathlib import Path

import pytest


@pytest.fixture
def recovery(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("recover_humanevalplus_full")


def original_and_replacements(recovery):
    old = {
        task: {
            "problem_id": task,
            "infrastructure_status": "error" if task in recovery.SUPPLEMENTS else "ok",
            "passed_plus": False,
            "run_id": "original",
        }
        for task in recovery.IDS
    }
    replacements = {
        task: {
            "problem_id": task,
            "infrastructure_status": "ok",
            "passed_plus": False,
            "run_id": "recovery",
        }
        for task in recovery.SUPPLEMENTS
    }
    return old, replacements


def test_exact_164_coverage_preserves_every_original_result_even_failures(recovery):
    old, replacements = original_and_replacements(recovery)
    before = copy.deepcopy(old)
    merged = recovery.merge_records(old, replacements)
    assert [r["problem_id"] for r in merged] == recovery.IDS
    assert old == before
    for row in merged:
        task = row["problem_id"]
        assert row is (old[task] if task not in recovery.SUPPLEMENTS else replacements[task])
    # Failed judgments are valid executions, not retriable infrastructure errors.
    assert all(row["passed_plus"] is False for row in merged)


@pytest.mark.parametrize(
    "corruption", ["missing", "extra", "replace_valid", "failed_replacement", "wrong_id"]
)
def test_invalid_merge_fails_closed(recovery, corruption):
    old, replacements = original_and_replacements(recovery)
    if corruption == "missing":
        replacements.pop("HumanEval/104")
    elif corruption == "extra":
        replacements["HumanEval/10"] = replacements["HumanEval/15"]
    elif corruption == "replace_valid":
        old["HumanEval/15"]["infrastructure_status"] = "ok"
    elif corruption == "failed_replacement":
        replacements["HumanEval/15"]["infrastructure_status"] = "error"
    else:
        replacements["HumanEval/15"]["problem_id"] = "HumanEval/11"
    with pytest.raises(ValueError):
        recovery.merge_records(old, replacements)


def imported_example():
    solution = "def candidate():\n    return 1\n"
    code_hash = hashlib.sha256(solution.encode()).hexdigest()
    raw = {
        "date": "2026-09-06",
        "hash": "a" * 32,
        "eval": {
            "HumanEval/103": [
                {
                    "task_id": "HumanEval/103",
                    "solution": solution,
                    "base_status": "pass",
                    "plus_status": "pass",
                    "base_fail_tests": [],
                    "plus_fail_tests": [],
                }
            ]
        },
    }
    item = {
        "problem_id": "HumanEval/103",
        "code_sha256": code_hash,
        "infrastructure_error_type": None,
        "official_result": {
            "passed_base": True,
            "passed_plus": True,
            "base_status": "pass",
            "plus_status": "pass",
            "error_type": None,
        },
        "container_observations": [
            {
                "problem_id": "HumanEval/103",
                "diagnostic_kill_requested": False,
                "cleanup_status": "removed",
                "before_cleanup": {
                    "available": True,
                    "Running": False,
                    "ExitCode": 0,
                    "OOMKilled": False,
                },
                "control": {"status": "ok"},
            }
        ],
    }
    return item, raw, code_hash


def test_import_requires_raw_and_historical_safe_agreement(recovery):
    item, raw, code_hash = imported_example()
    assert recovery.validate_imported(item, raw, code_hash)["passed_plus"] is True
    raw["eval"]["HumanEval/103"][0]["plus_status"] = "fail"
    with pytest.raises(ValueError, match="raw/status mismatch"):
        recovery.validate_imported(item, raw, code_hash)


def test_changed_candidate_and_killed_execution_are_rejected(recovery):
    item, raw, code_hash = imported_example()
    with pytest.raises(ValueError):
        recovery.validate_imported(item, raw, "0" * 64)
    item["container_observations"][0]["diagnostic_kill_requested"] = True
    with pytest.raises(ValueError):
        recovery.validate_imported(item, raw, code_hash)


def test_source_hashes_detect_drift_and_reject_links(recovery, tmp_path):
    source = tmp_path / "source.json"
    source.write_text("original")
    hashes = recovery.hash_files([source])
    source.write_text("changed")
    assert recovery.hash_files([source]) != hashes
    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises(ValueError):
        recovery.hash_files([link])
