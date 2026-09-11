#!/usr/bin/env python3
"""Verify fixed sources, then export original MBPP traces for human annotation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from benchmark_sources import DEFAULT_REGISTRY, ROOT, sha256, source_path, verify_sources

from tracejudge_hy3.process_eval_v2.materials import build_materials, canonical


def export(root: Path, registry_path: Path, output: Path) -> dict:
    registry, blobs, verification = verify_sources(root, registry_path)
    primary = [
        run for run in registry["datasets"]["mbppplus"]["runs"] if run.get("role") == "primary"
    ]
    if len(primary) != 1:
        raise ValueError("exactly one fixed MBPP primary run is required")
    run = primary[0]
    source_ids = ("mbpp.generation.responses", "mbpp.candidates", "mbpp.execution.results")
    run_directory = source_path(root, run["run_directory"]).resolve()
    for source_id in source_ids:
        entry = registry["sources"][source_id]
        if entry["run_id"] != run["run_id"] or not source_path(
            root, entry["path"]
        ).resolve().is_relative_to(run_directory):
            raise ValueError("MBPP source is not bound to the selected primary run")
    selection_id = "mbpp.selection.manifest"
    selection = json.loads(blobs[selection_id])
    selection_path = source_path(root, registry["sources"][selection_id]["path"])
    public = source_path(selection_path.parent, selection["public_projection"]["path"])
    problems_raw = public.read_bytes()
    if sha256(problems_raw) != selection["public_projection"]["sha256"]:
        raise ValueError("public projection SHA-256 mismatch")
    problem_ids = [json.loads(line)["problem_id"] for line in problems_raw.splitlines() if line]
    if (
        len(problem_ids) != selection["public_projection"]["record_count"]
        or problem_ids != selection["selection"]["selected_problem_ids"]
    ):
        raise ValueError("public projection selection mismatch")
    files, summary = build_materials(
        problems_raw=problems_raw,
        generation_raw=blobs[source_ids[0]],
        candidates_raw=blobs[source_ids[1]],
        execution_raw=blobs[source_ids[2]],
        run_id=run["run_id"],
    )
    summary["source_verification"] = verification
    summary["source_hashes"] = {key: sha256(blobs[key]) for key in (*source_ids, selection_id)}
    summary["public_projection_sha256"] = sha256(problems_raw)
    summary["exporter_sources_sha256"] = {
        str(path.relative_to(ROOT)): sha256(path.read_bytes())
        for path in (
            Path(__file__).resolve(),
            ROOT / "src/tracejudge_hy3/process_eval_v2/materials.py",
        )
    }
    guide = root / "docs/experiments/mbpp-process-annotation-v1.md"
    files["ANNOTATION_GUIDE.md"] = guide.read_bytes()
    summary["files_sha256"] = {name: sha256(raw) for name, raw in files.items()}
    # Never replace a packet containing human work; pin all initial file bytes.
    output = output.resolve()
    if not output.is_relative_to((root / "artifacts").resolve()):
        raise ValueError("annotation output must stay under project artifacts")
    if output.is_relative_to(run_directory) or run_directory.is_relative_to(output):
        raise ValueError("annotation output must not overlap frozen run artifacts")
    output.mkdir(parents=True, exist_ok=False)
    for name, raw in files.items():
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as handle:
            handle.write(raw)
    (output / "manifest.json").write_bytes(canonical(summary) + b"\n")
    return {
        key: summary[key]
        for key in (
            "source_run_id",
            "exported_count",
            "excluded_count",
            "pilot_count",
            "split_counts",
            "stratum_counts",
            "base_pass_plus_not_pass_count",
            "human_label_count",
        )
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.project_root.resolve()
    output = args.output if args.output.is_absolute() else root / args.output
    print(json.dumps(export(root, args.registry, output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
