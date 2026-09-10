#!/usr/bin/env python3
"""Verify and read fixed benchmark sources with the Python standard library."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath

DEFAULT_REGISTRY = Path("data/manifests/benchmark_sources_v1.json")
ROOT = Path(__file__).resolve().parents[1]


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical_sha256(value: object) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256(raw)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def source_path(root: Path, relative: str) -> Path:
    """Only portable, contained paths can identify evidence in the registry."""
    value = PurePosixPath(relative)
    require(
        bool(relative)
        and not value.is_absolute()
        and not {"..", "."}.intersection(value.parts)
        and "\\" not in relative
        and ":" not in relative,
        f"Invalid source path: {relative}",
    )
    candidate = root.joinpath(*value.parts)
    require(candidate.resolve().is_relative_to(root.resolve()), f"Source escapes root: {relative}")
    return candidate


def verify_sources(root: Path = ROOT, registry_path: Path = DEFAULT_REGISTRY):
    """Return verified bytes so consumers do not reopen unchecked report files.

    A registry is a reviewed trust anchor, not a signature. This checks its pinned
    files and runtime inventories; it does not execute candidates or certify the
    current host. Deliberately never reads benchmark-runtime/current.json.
    """
    root = root.resolve()
    path = registry_path if registry_path.is_absolute() else root / registry_path
    raw_registry = path.read_bytes()
    registry = json.loads(raw_registry)
    require(registry.get("schema") == "tracejudge-benchmark-sources-v1", "Unsupported registry")
    require(registry.get("path_base") == "project_root", "Unsupported path base")
    blobs = {}
    for source_id, entry in registry["sources"].items():
        raw = source_path(root, entry["path"]).read_bytes()
        require(len(raw) == entry["size_bytes"], f"Size mismatch: {source_id}")
        require(sha256(raw) == entry["sha256"], f"SHA-256 mismatch: {source_id}")
        blobs[source_id] = raw

    runtime_files = 0
    for freeze_id, entry in registry.get("runtimes", {}).items():
        folder = source_path(root, entry["directory"])
        raw = (folder / "FREEZE.json").read_bytes()
        require(sha256(raw) == entry["freeze_sha256"], f"Freeze mismatch: {freeze_id}")
        freeze = json.loads(raw)
        inventory = freeze["files_sha256"]
        require(canonical_sha256(inventory) == entry["inventory_sha256"], "Freeze inventory mismatch")
        require(len(inventory) == entry["file_count"], "Freeze file count mismatch")
        identity = {"files": inventory, "environment": freeze["environment"]}
        require(canonical_sha256(identity)[:16] == freeze_id, "Freeze identity mismatch")
        for name, expected in inventory.items():
            require(sha256(source_path(folder, name).read_bytes()) == expected, f"Runtime source changed: {freeze_id}/{name}")
        runtime_files += len(inventory)

    inventory_files = 0
    for inventory_id, entry in registry.get("inventories", {}).items():
        folder = source_path(root, entry["directory"])
        inventory = entry["files_sha256"]
        require(canonical_sha256(inventory) == entry["inventory_sha256"], f"Inventory mismatch: {inventory_id}")
        for name, expected in inventory.items():
            require(sha256(source_path(folder, name).read_bytes()) == expected, f"Inventory file changed: {inventory_id}/{name}")
        if entry.get("strict_files"):
            actual = {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}
            require(actual == set(inventory), f"Unexpected inventory files: {inventory_id}")
        inventory_files += len(inventory)

    resolved = {}
    for dataset, entry in registry["datasets"].items():
        report_ids = {run["report_source"] for run in entry["runs"]}
        require(set(entry["reports"].values()) <= report_ids, f"Unbound report: {dataset}")
        for run in entry["runs"]:
            folder = source_path(root, run["run_directory"])
            for key in ("manifest_source", "report_source"):
                source_id = run[key]
                source = registry["sources"][source_id]
                require(source["run_id"] == run["run_id"], f"Run identity mismatch: {source_id}")
                require(source_path(root, source["path"]).resolve().is_relative_to(folder.resolve()), f"Artifact outside its run: {source_id}")
                value = json.loads(blobs[source_id])
                # Combined reports bind their run through their execution summary.
                observed = value.get("run_id", value.get("execution_summary", {}).get("run_id"))
                if key == "manifest_source" or observed is not None:
                    require(observed == run["run_id"], f"Artifact run identity mismatch: {source_id}")
            runtime = run["runtime"]
            if runtime["kind"] == "frozen_inventory":
                require(runtime["freeze_id"] in registry["runtimes"], f"Unbound runtime: {dataset}")
                runtime_folder = source_path(root, registry["runtimes"][runtime["freeze_id"]]["directory"])
                require(folder.resolve().is_relative_to(runtime_folder.resolve()), f"Run outside its runtime: {dataset}")
            elif runtime["kind"] == "captured_source_inventory":
                require(runtime["inventory"] in registry["inventories"], f"Unbound source snapshot: {dataset}")
        resolved[dataset] = {
            role: {"source_id": sid, **registry["sources"][sid]}
            for role, sid in entry["reports"].items()
        }
    verification = {
        "status": "PASS",
        "scope": "Fixed source bytes, run bindings and captured runtime inventories; no rescoring or current-host readiness certification.",
        "registry_sha256": sha256(raw_registry),
        "source_files_verified": len(blobs),
        "runtime_files_verified": runtime_files,
        "inventory_files_verified": inventory_files,
        "resolved_reports": resolved,
    }
    return registry, blobs, verification


def load_verified_reports(root: Path = ROOT, registry_path: Path = DEFAULT_REGISTRY):
    registry, blobs, verification = verify_sources(root, registry_path)
    reports = {
        dataset: {role: json.loads(blobs[sid]) for role, sid in entry["reports"].items()}
        for dataset, entry in registry["datasets"].items()
    }
    return reports, verification


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output", type=Path, help="Create an aggregate-only verified report bundle")
    args = parser.parse_args()
    try:
        reports, verification = load_verified_reports(args.project_root, args.registry)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump({"schema": "tracejudge-verified-reports-v1", "verification": verification, "reports": reports}, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
        print(json.dumps(verification, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"[source verification failed] {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
