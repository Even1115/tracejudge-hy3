"""Tests for the development-set loader (carried pilot consensus + extension).

Synthetic fixtures only: ``load_development_inputs`` is monkeypatched so no
packet materials are needed, while the carried/extension/pilot-config/audit
files are written with genuinely computed SHA-256 bindings so the loader's
verification chain is exercised for real. An integration test loads the real
development config when its artifacts exist locally, and skips otherwise.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracejudge_hy3.process_eval_v2 import development
from tracejudge_hy3.process_eval_v2.contracts import (
    FunctionalEvidence,
    PilotInput,
)
from tracejudge_hy3.process_eval_v2.development import (
    DEVELOPMENT_CONFIG_SCHEMA,
    DEVELOPMENT_LABEL_STAGE,
    prepare_development,
)
from tracejudge_hy3.process_eval_v2.materials import digest

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_CONFIG = Path("data/manifests/process_development_v1.json")
REAL_PACKET = Path("artifacts/process-annotations/mbpp120-process-v1/manifest.json")
REAL_LABELS = Path(
    "artifacts/annotation-development/mbpp36-dev-20260909-v1/labels_extension_20260909.jsonl"
)

ERROR_ITEMS = {
    "item-e5b9ff02f8bd9e3f3b61": "P01_ALGORITHM_ERROR",  # max_product (v1 consensus)
    "item-caf2c4228350e46f5b68": "R03_UNSUPPORTED_ASSUMPTION",  # division_elements
    "item-80524df8244578aa4abf": "R03_UNSUPPORTED_ASSUMPTION",  # text_lowercase_underscore
    "item-d97b96cd8a87051bb420": "R03_UNSUPPORTED_ASSUMPTION",  # opposite_Signs
}


def _input(item_id: str) -> PilotInput:
    code = "def f():\n    return 0\n"
    return PilotInput(
        item_id=item_id,
        problem={
            "title": f"task {item_id}",
            "requirement": "Return one.",
            "function_signature": "def f():",
            "requirements": [{"requirement_id": "R1", "content": "Return one."}],
        },
        solution_trace={
            "problem_id": item_id,
            "requirement_understanding": "Return one.",
            "design_summary": "Always return zero.",
            "edge_cases_considered": ["Empty input."],
            "implementation_steps": [
                {"step_id": "S1", "content": "Return zero.", "related_requirements": ["R1"]}
            ],
            "declared_time_complexity": "O(1)",
            "declared_space_complexity": "O(1)",
            "code": code,
        },
        functional_evidence=FunctionalEvidence(
            source_run_id="test",
            source_record_sha256="a" * 64,
            candidate_code_sha256=digest(code.encode()),
            infrastructure_status="ok",
            base_status="fail",
            plus_status="fail",
        ),
    )


def _correct_row(item_id: str, annotator: str) -> dict:
    return {
        "annotation_status": "reviewed",
        "annotator": annotator,
        "error_type": None,
        "evidence": [],
        "first_faulty_layer": None,
        "first_faulty_step": None,
        "item_id": item_id,
        "localization_status": "not_applicable",
        "plan_code_aligned": True,
        "process_correct": True,
        "rationale": "Synthetic row for loader tests.",
        "reasoning_correct": True,
    }


def _error_row(item_id: str, annotator: str) -> dict:
    return {
        "annotation_status": "reviewed",
        "annotator": annotator,
        "error_type": "R03_UNSUPPORTED_ASSUMPTION",
        "evidence": [
            {
                "step_id": None,
                "code_span": "L2",
                "requirement_id": "R1",
                "description": "Synthetic evidence for the override row.",
            }
        ],
        "first_faulty_layer": "reasoning",
        "first_faulty_step": None,
        "item_id": item_id,
        "localization_status": "supported",
        "plan_code_aligned": True,
        "process_correct": False,
        "rationale": "Synthetic override: reasoning adopts an unsupported assumption.",
        "reasoning_correct": False,
    }


def _write_jsonl(path: Path, rows: list[dict]) -> bytes:
    raw = (
        "\n".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for row in rows
        )
        + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def _entry(root: Path, rel: str, raw: bytes) -> dict:
    return {"path": rel, "sha256": digest(raw)}


@pytest.fixture
def dev_setup(tmp_path, monkeypatch):
    inputs = {key: _input(key) for key in ("item-a", "item-b", "item-c")}
    monkeypatch.setattr(development, "load_development_inputs", lambda *a, **k: dict(inputs))

    reviewed_a = _write_jsonl(
        tmp_path / "audit/submitted_a.jsonl",
        [_correct_row("item-a", "annotator_a"), _correct_row("item-b", "annotator_a")],
    )
    pilot_config_raw = json.dumps(
        {
            "schema": "tracejudge-process-pilot-config-v1",
            "annotations": {"a": _entry(tmp_path, "audit/submitted_a.jsonl", reviewed_a)},
        }
    ).encode()
    (tmp_path / "manifests").mkdir()
    (tmp_path / "manifests/pilot.json").write_bytes(pilot_config_raw)
    audit_raw = json.dumps({"input_submissions": {"a": {"sha256": digest(reviewed_a)}}}).encode()
    (tmp_path / "audit/structural-audit.json").write_bytes(audit_raw)

    def write_config(carried_rows=None, extension_rows=None, annotator="annotator_dev"):
        if carried_rows is None:
            carried_rows = [
                _correct_row("item-a", "annotator_a"),
                _correct_row("item-b", "annotator_a"),
            ]
        if extension_rows is None:
            extension_rows = [
                _error_row("item-b", "annotator_dev"),
                _correct_row("item-c", "annotator_dev"),
            ]
        carried_raw = _write_jsonl(tmp_path / "dev/carried.jsonl", carried_rows)
        extension_raw = _write_jsonl(tmp_path / "dev/extension.jsonl", extension_rows)
        config = {
            "schema": DEVELOPMENT_CONFIG_SCHEMA,
            "label_stage": DEVELOPMENT_LABEL_STAGE,
            "annotation_provenance": "synthetic test fixture",
            "packet_manifest": {"path": "packet/manifest.json", "sha256": "b" * 64},
            "review_audit": _entry(tmp_path, "audit/structural-audit.json", audit_raw),
            "pilot_config": _entry(tmp_path, "manifests/pilot.json", pilot_config_raw),
            "annotations": {
                "carried": _entry(tmp_path, "dev/carried.jsonl", carried_raw),
                "extension": {
                    **_entry(tmp_path, "dev/extension.jsonl", extension_raw),
                    "annotator": annotator,
                },
            },
            "benchmark_registry": "data/manifests/benchmark_sources_v1.json",
        }
        config_path = tmp_path / "manifests/development.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        return config_path

    return tmp_path, write_config, inputs


def test_happy_path_carried_plus_extension_override(dev_setup):
    root, write_config, inputs = dev_setup
    prepared = prepare_development(root, write_config())
    assert set(prepared.inputs) == set(inputs)
    assert set(prepared.labels) == {"item-a", "item-b", "item-c"}
    assert prepared.labels["item-a"].process_correct is True
    assert prepared.labels["item-a"].annotator == "annotator_a"
    # Extension overrides the carried pilot row.
    assert prepared.labels["item-b"].process_correct is False
    assert prepared.labels["item-b"].annotator == "annotator_dev"
    assert prepared.labels["item-b"].error_type == "R03_UNSUPPORTED_ASSUMPTION"
    assert prepared.labels["item-c"].process_correct is True
    assert prepared.label_stage == DEVELOPMENT_LABEL_STAGE
    # Every input file is hash-pinned in the provenance record:
    # config, pilot config, reviewed submission, audit, carried, extension.
    assert len(prepared.provenance) == 6


def test_carried_row_differing_from_reviewed_submission_refused(dev_setup):
    root, write_config, _ = dev_setup
    tampered = _correct_row("item-b", "annotator_a")
    tampered["rationale"] = "Tampered rationale."
    rows = [_correct_row("item-a", "annotator_a"), tampered]
    with pytest.raises(ValueError, match="carried label differs"):
        prepare_development(root, write_config(carried_rows=rows))


def test_carried_coverage_mismatch_refused(dev_setup):
    root, write_config, _ = dev_setup
    with pytest.raises(ValueError, match="carried label coverage"):
        prepare_development(
            root, write_config(carried_rows=[_correct_row("item-a", "annotator_a")])
        )


def test_extension_annotator_mismatch_refused(dev_setup):
    root, write_config, _ = dev_setup
    rows = [_error_row("item-b", "annotator_a"), _correct_row("item-c", "annotator_a")]
    with pytest.raises(ValueError, match="unexpected extension annotator"):
        prepare_development(root, write_config(extension_rows=rows))


def test_extension_outside_development_split_refused(dev_setup):
    root, write_config, _ = dev_setup
    rows = [_error_row("item-b", "annotator_dev"), _correct_row("item-zz", "annotator_dev")]
    with pytest.raises(ValueError, match="outside the development split"):
        prepare_development(root, write_config(extension_rows=rows))


def test_final_coverage_gap_refused(dev_setup):
    root, write_config, _ = dev_setup
    with pytest.raises(ValueError, match="do not cover the full development split"):
        prepare_development(
            root, write_config(extension_rows=[_error_row("item-b", "annotator_dev")])
        )


def test_tampered_reviewed_submission_refused(dev_setup, tmp_path):
    root, write_config, _ = dev_setup
    config_path = write_config()
    # Rewrite the reviewed submission after the audit pinned its digest.
    _write_jsonl(
        tmp_path / "audit/submitted_a.jsonl",
        [_correct_row("item-a", "annotator_a"), _error_row("item-b", "annotator_a")],
    )
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        prepare_development(root, config_path)


def test_wrong_config_schema_refused(dev_setup):
    root, write_config, _ = dev_setup
    config_path = write_config()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["schema"] = "tracejudge-process-pilot-config-v1"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported development configuration"):
        prepare_development(root, config_path)


@pytest.mark.skipif(
    not (REPO_ROOT / REAL_CONFIG).exists()
    or not (REPO_ROOT / REAL_PACKET).exists()
    or not (REPO_ROOT / REAL_LABELS).exists(),
    reason="real development annotation artifacts not present",
)
def test_real_development_config_loads_full_split():
    prepared = prepare_development(REPO_ROOT, REAL_CONFIG)
    assert len(prepared.inputs) == 36
    assert set(prepared.labels) == set(prepared.inputs)
    incorrect = {
        key: label.error_type
        for key, label in prepared.labels.items()
        if label.process_correct is False
    }
    assert incorrect == {key: value for key, value in ERROR_ITEMS.items()}
    assert sum(1 for x in prepared.labels.values() if x.process_correct is True) == 32
    assert all(x.process_correct is not None for x in prepared.labels.values())
    # Both v1 pilot unknowns are resolved by extension rows; sample_nam's
    # extension row re-affirms its already-correct v1 consensus.
    for key in (
        "item-ab1645e8d37a7d4cb6b0",
        "item-65955ed5f85107105326",
        "item-d97b96cd8a87051bb420",
    ):
        assert prepared.labels[key].annotator == "annotator_dev"
    # Carried rows still present and attributed to the reviewed pilot annotator.
    carried = [x for x in prepared.labels.values() if x.annotator == "annotator_a"]
    assert len(carried) == 9
    # Every error label carries a supported localization.
    for key in incorrect:
        label = prepared.labels[key]
        assert label.localization_status == "supported"
        assert label.evidence
    assert prepared.label_stage == DEVELOPMENT_LABEL_STAGE
    assert "single_reviewer" in prepared.annotation_provenance
