#!/usr/bin/env python3
"""Snapshot benchmark runtime bytes into an independent clean local Git checkout.

Does not stage or commit the user's working tree. No credentials, raw data,
candidate outputs or model responses are copied. Experiments use the snapshot's
ignored artifacts directory and read downloaded data from --project-root.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from tracejudge_hy3.benchmark.contracts import canonical_sha256  # noqa: E402
from tracejudge_hy3.lcb.experiment import source_identity, write_json  # noqa: E402


def freeze(project: Path):
    paths = {p.relative_to(project) for p in (project / "src").rglob("*.py")}
    paths.update(p.relative_to(project) for p in (project / "scripts").glob("*.py"))
    paths.update(Path(n) for n in ("pyproject.toml", "uv.lock"))
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z", "--", "data", "docs"], cwd=project
    ).split(b"\0")
    paths.update(Path(p.decode()) for p in tracked if p)
    paths.add(Path("data/manifests/evalplus_mbppplus_64fc4195.json"))
    paths.update(p.relative_to(project) for p in (project / "docker").rglob("Dockerfile"))
    if any((project / p).is_symlink() or not (project / p).is_file() for p in paths):
        raise ValueError("snapshot inputs must be regular files")
    inventory = {
        p.as_posix(): hashlib.sha256((project / p).read_bytes()).hexdigest() for p in sorted(paths)
    }
    environment = source_identity(project)
    freeze_id = canonical_sha256({"files": inventory, "environment": environment})[:16]
    destination = project / "artifacts/benchmark-runtime" / freeze_id
    if destination.exists():
        for name, digest in inventory.items():
            if hashlib.sha256((destination / name).read_bytes()).hexdigest() != digest:
                raise ValueError("existing runtime snapshot was modified")
    else:
        destination.mkdir(parents=True, mode=0o700)
        for relative in sorted(paths):
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(project / relative, target)
            if hashlib.sha256(target.read_bytes()).hexdigest() != inventory[relative.as_posix()]:
                raise ValueError("source changed while freezing; create a new snapshot")
        # This file is created by the snapshot workflow, not copied from the
        # user's index, so all later outputs and bytecode remain untracked.
        (destination / ".gitignore").write_text("artifacts/\n__pycache__/\n*.pyc\n.env\n.venv/\n")
        write_json(
            destination / "FREEZE.json",
            {
                "schema": "benchmark-runtime-freeze-v1",
                "source_root": str(project),
                "files_sha256": inventory,
                "environment": environment,
            },
        )
        commands = [
            ["git", "init", "-q"],
            ["git", "add", "--", "."],
            [
                "git",
                "-c",
                "user.name=TraceJudge Runtime Snapshot",
                "-c",
                "user.email=runtime@localhost",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-qm",
                "Freeze MBPP+ and LCB experiment runtime",
            ],
        ]
        for command in commands:
            subprocess.run(command, cwd=destination, check=True, capture_output=True)
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=destination, text=True
    ).strip()
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=destination).strip():
        raise ValueError("runtime snapshot must be clean")
    result = {
        "runtime_root": str(destination),
        "commit": commit,
        "freeze_id": freeze_id,
        "source_root": str(project),
        "inventory_sha256": canonical_sha256(inventory),
    }
    write_json(project / "artifacts/benchmark-runtime/current.json", result)
    return result


if __name__ == "__main__":
    print(json.dumps(freeze(ROOT), ensure_ascii=False, indent=2))
