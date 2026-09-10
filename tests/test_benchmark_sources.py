"""Fixed report selection must survive relocation and reject evidence drift."""

from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/benchmark_sources.py"
SPEC = importlib.util.spec_from_file_location("benchmark_sources", SCRIPT)
sources = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sources)


class FixedSourcesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        self.run = self.root / "artifacts/fixed-run"
        self.run.mkdir(parents=True)
        self.entries = {}
        for name, value in (("manifest", {"run_id": "fixed-run"}), ("report", {"run_id": "fixed-run", "passed": 3})):
            raw = (json.dumps(value) + "\n").encode()
            (self.run / f"{name}.json").write_bytes(raw)
            self.entries[name] = {"path": f"artifacts/fixed-run/{name}.json", "sha256": sources.sha256(raw), "size_bytes": len(raw), "run_id": "fixed-run"}
        self.registry = {
            "schema": "tracejudge-benchmark-sources-v1",
            "path_base": "project_root",
            "sources": self.entries,
            "datasets": {"example": {"runs": [{"run_id": "fixed-run", "run_directory": "artifacts/fixed-run", "manifest_source": "manifest", "report_source": "report", "runtime": {"kind": "recorded_in_run_manifest"}}], "reports": {"primary": "report"}}},
        }
        self.write_registry()

    def write_registry(self):
        path = self.root / sources.DEFAULT_REGISTRY
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.registry), encoding="utf-8")

    def test_relocated_project_ignores_even_an_invalid_current_pointer(self):
        pointer = self.root / "artifacts/benchmark-runtime/current.json"
        pointer.parent.mkdir(parents=True)
        pointer.write_text("this is not JSON and must never be read", encoding="utf-8")
        destination = Path(self.temporary.name) / "relocated"
        shutil.copytree(self.root, destination)
        reports, result = sources.load_verified_reports(destination)
        self.assertEqual(reports["example"]["primary"]["passed"], 3)
        self.assertEqual(result["status"], "PASS")

    def test_same_size_report_tampering_is_rejected(self):
        path = self.run / "report.json"
        path.write_bytes(path.read_bytes().replace(b'"passed": 3', b'"passed": 9'))
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch: report"):
            sources.load_verified_reports(self.root)

    def test_missing_manifest_prevents_reading_a_valid_report(self):
        (self.run / "manifest.json").unlink()
        with self.assertRaises(FileNotFoundError):
            sources.load_verified_reports(self.root)

    def test_newline_conversion_is_not_treated_as_exact_evidence(self):
        path = self.run / "report.json"
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        with self.assertRaisesRegex(ValueError, "Size mismatch: report"):
            sources.load_verified_reports(self.root)

    def test_report_from_another_run_fails_even_if_its_hash_is_pinned(self):
        raw = b'{"run_id": "other-run", "passed": 3}\n'
        (self.run / "report.json").write_bytes(raw)
        self.entries["report"].update(sha256=sources.sha256(raw), size_bytes=len(raw))
        self.write_registry()
        with self.assertRaisesRegex(ValueError, "Artifact run identity mismatch: report"):
            sources.load_verified_reports(self.root)

    def test_source_cannot_escape_project(self):
        self.entries["report"]["path"] = "../report.json"
        self.write_registry()
        with self.assertRaisesRegex(ValueError, "Invalid source path"):
            sources.load_verified_reports(self.root)

    def test_appended_event_requires_a_new_reviewed_inventory(self):
        folder = self.run / "events"
        folder.mkdir()
        raw = b'{"event": "first"}\n'
        (folder / "000000.json").write_bytes(raw)
        inventory = {"000000.json": sources.sha256(raw)}
        self.registry["inventories"] = {"events": {"directory": "artifacts/fixed-run/events", "files_sha256": inventory, "inventory_sha256": sources.canonical_sha256(inventory), "strict_files": True}}
        self.write_registry()
        sources.load_verified_reports(self.root)
        (folder / "000001.json").write_bytes(b'{"event": "unexpected"}\n')
        with self.assertRaisesRegex(ValueError, "Unexpected inventory files: events"):
            sources.load_verified_reports(self.root)


if __name__ == "__main__":
    unittest.main()
