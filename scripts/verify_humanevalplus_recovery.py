#!/usr/bin/env python3
"""Offline verification of a derived recovery. No Docker or model API calls."""

import argparse
from collections import Counter
from pathlib import Path

import recover_humanevalplus_full as recovery

from tracejudge_hy3.evalplus import runner as phase2
from tracejudge_hy3.evalplus.parser import build_summary, parse_official_result


def verify(directory):
    directory = directory.resolve()
    read, require = recovery.read, recovery.require
    manifest = read(directory / "manifest.json")
    require(
        manifest["schema"] == "humanevalplus-derived-recovery-v1"
        and manifest["status"] == "completed",
        "not a completed recovery",
    )
    require(manifest["expected_problem_ids"] == recovery.IDS, "manifest cohort differs")
    require(
        manifest["source_unchanged"] is True and manifest["model_api_calls"] == 0,
        "unexpected execution contract",
    )
    require(
        recovery.hash_files(Path(p) for p in manifest["input_hashes"]) == manifest["input_hashes"],
        "original input hash drift",
    )
    for name, digest in manifest["outputs"].items():
        require(Path(name).name == name, "unsafe output path")
        require(recovery.diagnostic.sha(directory / name) == digest, "derived output hash drift")
    for name, digest in manifest["source_snapshot_hashes"].items():
        require(
            not Path(name).is_absolute() and ".." not in Path(name).parts, "unsafe snapshot path"
        )
        require(
            recovery.diagnostic.sha(directory / "source_snapshot" / name) == digest,
            "source snapshot drift",
        )
    rows = phase2._read_jsonl(directory / "results.jsonl", label="derived results")
    provenance = phase2._read_jsonl(directory / "provenance.jsonl", label="derived provenance")
    for items in (rows, provenance):
        require([r["problem_id"] for r in items] == recovery.IDS, "ordered coverage violation")
    source = Path(manifest["original_run"])
    original = read(source / "manifest.json")
    old_rows = phase2._read_jsonl(source / "results.jsonl", label="original results")
    require([r["problem_id"] for r in old_rows] == recovery.IDS, "original coverage violation")
    old = {r["problem_id"]: r for r in old_rows}
    bundle = read(directory / "evalplus_raw_results.json")
    raw_documents = bundle["raw_results"]
    require(len(raw_documents) == 164, "raw count differs")
    raw = {}
    for document in raw_documents:
        require(len(document["eval"]) == 1, "invalid raw task scope")
        task = next(iter(document["eval"]))
        require(task not in raw, "duplicate raw task")
        raw[task] = document
    require(set(raw) == set(recovery.IDS), "raw coverage violation")
    diagnosis_dir = Path(manifest["diagnostic_run"])
    diag = read(diagnosis_dir / "report.json")
    diag_items = {r["problem_id"]: r for r in diag["results"]}
    receipt = read(directory / "task15_receipt.json")
    recovery.validate_observation(receipt["evidence"])
    require(
        receipt["raw_sha256"]
        == recovery.diagnostic.sha(directory / "HumanEval_15/official_evalplus_raw_result.json"),
        "receipt/raw mismatch",
    )
    require(
        recovery.runtime_pins(manifest["preflight"]) == recovery.runtime_pins(diag["preflight"]),
        "runtime pin mismatch",
    )
    for row, trace in zip(rows, provenance, strict=True):
        task = row["problem_id"]
        code_hash = original["input"]["code_sha256"][task]
        parsed = parse_official_result(
            raw[task], expected_problem_id=task, expected_solution_sha256=code_hash
        )
        require(all(row[key] == value for key, value in parsed.items()), "safe/raw disagreement")
        require(
            trace["raw_document_sha256"] == recovery.document_hash(raw[task]),
            "raw provenance hash differs",
        )
        require(
            trace["safe_record_sha256"] == recovery.document_hash(row),
            "safe provenance hash differs",
        )
        require(
            trace["candidate_sha256"] == code_hash and trace["record_run_id"] == row["run_id"],
            "candidate/run provenance differs",
        )
        if task not in recovery.SUPPLEMENTS:
            require(
                trace["source_kind"] == "original_valid" and row == old[task],
                "original valid judgment was altered",
            )
        else:
            require(
                old[task]["infrastructure_status"] == "error", "replaced an original valid result"
            )
            if task in recovery.IMPORTED:
                require(trace["source_kind"] == "diagnostic_import", "wrong diagnostic origin")
                imported_path = (
                    diagnosis_dir / task.replace("/", "_") / "official_evalplus_raw_result.json"
                )
                historical_raw = read(imported_path)
                recovery.validate_imported(diag_items[task], historical_raw, code_hash)
                require(raw[task] == historical_raw, "imported raw altered")
            else:
                require(trace["source_kind"] == "cache_recovery_execution", "wrong recovery origin")
                require(
                    raw[task] == read(directory / "HumanEval_15/official_evalplus_raw_result.json"),
                    "task15 raw altered",
                )
    counts = dict(Counter(r["source_kind"] for r in provenance))
    require(
        counts == {"original_valid": 161, "diagnostic_import": 2, "cache_recovery_execution": 1},
        "provenance count differs",
    )
    summary = read(directory / "summary.json")
    recalculated = build_summary(rows, expected_problem_ids=recovery.IDS)
    for key, value in recalculated.items():
        if key not in {"metrics_scope", "limitations"}:
            require(summary[key] == value, "summary differs from offline recomputation")
    require(
        summary["evaluation_complete"] is True and summary["infrastructure_error_count"] == 0,
        "incomplete evaluation",
    )
    return {
        "schema": "humanevalplus-recovery-verification-v1",
        "verified_at": recovery.diagnostic.now(),
        "manifest_sha256": recovery.diagnostic.sha(directory / "manifest.json"),
        "verifier_sha256": recovery.diagnostic.sha(Path(__file__)),
        "source_hashes_unchanged": True,
        "original_161_records_unchanged": True,
        "raw_safe_summary_agreement": True,
        "unique_task_count": 164,
        "provenance_counts": counts,
        "base_pass_count": summary["base_pass_count"],
        "base_plus_pass_count": summary["base_plus_pass_count"],
        "docker_calls": 0,
        "model_api_calls": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = verify(args.run_dir)
        if args.output:
            recovery.require(not args.output.exists(), "verification output already exists")
            phase2._atomic_write_json(args.output, result)
        print(
            "[verified] 164 unique tasks; 161 originals unchanged; all input/output hashes and raw/safe/summary agree"
        )
    except Exception as exc:
        print(f"[blocked] verification failed ({type(exc).__name__}); no raw exception echoed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
