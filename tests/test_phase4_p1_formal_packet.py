from __future__ import annotations

import hashlib
import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import tracejudge_hy3.phase4.p1_formal_packet as packet_module
from tracejudge_hy3.phase4 import (
    P1_PROTOCOL_SHA256,
    P1PracticeAdmissionRecord,
    Phase4P1AnnotationError,
    prepare_p1_formal_packet,
    prepare_p1_practice_admission,
    verify_p1_formal_packet,
    write_p1_formal_packet,
)

REPO_ROOT = Path(__file__).parents[1]
PHASE3_GUIDE = REPO_ROOT / "docs/experiments/phase3_annotation_guide_v1.md"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _jsonl(rows: list[dict[str, object]]) -> bytes:
    return b"".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode() + b"\n"
        for row in rows
    )


def _practice_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(1, 6):
        has_error = index <= 3
        rows.append(
            {
                "annotation_protocol_sha256": P1_PROTOCOL_SHA256,
                "blinded_to_method_predictions": True,
                "blinded_to_primary_labels": True,
                "calibration_round": 1,
                "error_type": "C01_BOUNDARY_ERROR" if has_error else None,
                "first_faulty_layer": "implementation" if has_error else None,
                "first_faulty_step": "S1" if has_error else None,
                "has_error": has_error,
                "plan_code_aligned": True,
                "practice_item_id": f"practice_item_{index:03d}",
                "process_correct": not has_error,
                "rater_id": "p1_rater_02",
                "rationale": "The public trace and evidence support this calibration label.",
                "reasoning_correct": not has_error,
                "status": "completed",
            }
        )
    return rows


def _patch_admission_references(monkeypatch: pytest.MonkeyPatch) -> None:
    references = tuple(
        SimpleNamespace(
            practice_item_id=row["practice_item_id"],
            reference_annotation=SimpleNamespace(
                has_error=row["has_error"],
                process_correct=row["process_correct"],
                first_faulty_layer=row["first_faulty_layer"],
            ),
        )
        for row in _practice_rows()
    )
    monkeypatch.setattr(
        packet_module,
        "_load_protocol_and_source",
        lambda **_kwargs: (SimpleNamespace(), SimpleNamespace()),
    )
    monkeypatch.setattr(
        packet_module,
        "_load_private_references",
        lambda *_args, **_kwargs: (b"synthetic-reference\n", references),
    )


def test_practice_admission_scores_calibration_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_admission_references(monkeypatch)
    labels = tmp_path / "labels.jsonl"
    labels.write_bytes(_jsonl(_practice_rows()))
    result = prepare_p1_practice_admission(
        completed_labels_path=labels,
        returned_archive_sha256=_digest("archive"),
        public_evidence_only_rationales_confirmed=True,
        coordinator_written_authorization_confirmed=True,
    )
    assert result.record.has_error_exact_agreement_count == 5
    assert result.record.process_correct_exact_agreement_count == 5
    assert result.record.error_item_first_faulty_layer_exact_agreement_count == 3
    assert result.record.decision == "admitted_to_formal_20"
    assert result.record.excluded_from_research_endpoints is True

    failing = _practice_rows()
    for row in failing[:2]:
        row.update(
            has_error=False,
            process_correct=True,
            reasoning_correct=True,
            first_faulty_layer=None,
            first_faulty_step=None,
            error_type=None,
        )
    labels.write_bytes(_jsonl(failing))
    with pytest.raises(Phase4P1AnnotationError, match="thresholds"):
        prepare_p1_practice_admission(
            completed_labels_path=labels,
            returned_archive_sha256=_digest("archive"),
            public_evidence_only_rationales_confirmed=True,
            coordinator_written_authorization_confirmed=True,
        )


def test_formal_packet_gate_stops_before_admission_or_material_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        packet_module,
        "preflight_p1_delivery_record",
        lambda **_kwargs: SimpleNamespace(
            data_collection_allowed=False, record_sha256=_digest("delivery")
        ),
    )

    def _unexpected_read(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("hard gate must stop all formal material reads")

    monkeypatch.setattr(packet_module, "_load_admission", _unexpected_read)
    monkeypatch.setattr(packet_module, "load_phase3_materials", _unexpected_read)
    with pytest.raises(Phase4P1AnnotationError, match="does not authorize"):
        prepare_p1_formal_packet(
            cohort_manifest_path="unused",
            natural_manifest_path="unused",
            phase1_run_dir="unused",
            phase2_run_dir="unused",
            dataset_manifest_path="unused",
            source_bundle_path="unused",
            execution_run_dir="unused",
        )


def _patch_formal_runtime(monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    monkeypatch.setattr(
        packet_module,
        "preflight_p1_delivery_record",
        lambda **_kwargs: SimpleNamespace(
            data_collection_allowed=True, record_sha256=_digest("delivery")
        ),
    )
    admission = P1PracticeAdmissionRecord(
        admitted_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
        completed_labels_sha256=_digest("labels"),
        returned_archive_sha256=_digest("archive"),
        coordinator_reference_sha256=_digest("reference"),
        has_error_exact_agreement_count=5,
        process_correct_exact_agreement_count=5,
        error_item_first_faulty_layer_exact_agreement_count=3,
    )
    admission_payload = packet_module._json_bytes(admission)
    monkeypatch.setattr(
        packet_module, "_load_admission", lambda _path: (admission_payload, admission)
    )
    monkeypatch.setattr(
        packet_module,
        "verify_p1_formal_subset",
        lambda **_kwargs: SimpleNamespace(public_commitment_sha256=_digest("commitment")),
    )

    trace_ids = tuple(f"trace_fixture_{index:03d}" for index in range(1, 21))
    subset_records = []
    traces_by_id = {}
    materials = {}
    for index, trace_id in enumerate(trace_ids, start=1):
        problem_id = f"problem_fixture_{index:03d}"
        code_sha256 = _digest(f"code:{trace_id}")
        explanation_sha256 = _digest(f"explanation:{trace_id}")
        evidence_sha256 = _digest(f"evidence:{trace_id}")
        subset_records.append(
            SimpleNamespace(
                trace_id=trace_id,
                problem_id=problem_id,
                code_sha256=code_sha256,
                structured_explanation_sha256=explanation_sha256,
                functional_evidence_sha256=evidence_sha256,
            )
        )
        functional = SimpleNamespace(functional_evidence_sha256=evidence_sha256)
        traces_by_id[trace_id] = SimpleNamespace(
            trace_id=trace_id,
            problem_id=problem_id,
            code_sha256=code_sha256,
            structured_explanation_sha256=explanation_sha256,
            functional_evidence=functional,
        )
        materials[trace_id] = SimpleNamespace(
            public_problem={"problem_id": problem_id, "prompt": "Return one."},
            solution_trace=SimpleNamespace(code="def solve():\n    return 1\n"),
            functional_evidence=functional,
            public_dynamic_evidence=SimpleNamespace(
                payload={"availability": "not_available", "attempted_public_case_count": 0}
            ),
            model_dump=lambda mode, value=trace_id: {"material_key": value},
        )
    subset = SimpleNamespace(records=tuple(subset_records))
    monkeypatch.setattr(
        packet_module,
        "_load_private_subset",
        lambda _path: (b"synthetic-subset\n", subset),
    )
    monkeypatch.setattr(
        packet_module,
        "load_phase3_materials",
        lambda **_kwargs: SimpleNamespace(
            cohort=SimpleNamespace(traces_by_id=traces_by_id), materials=materials
        ),
    )
    monkeypatch.setattr(
        packet_module,
        "_structured_solution_trace",
        lambda _solution: {"design_summary": "Return the required value."},
    )
    monkeypatch.setattr(
        packet_module,
        "functional_evidence_payload",
        lambda evidence: {
            "functional_evidence_sha256": evidence.functional_evidence_sha256,
            "execution_status": "not_available",
        },
    )
    return trace_ids


def _formal_arguments(tmp_path: Path) -> dict[str, object]:
    return {
        "protocol_path": "synthetic-protocol",
        "phase3_guide_path": PHASE3_GUIDE,
        "cohort_manifest_path": "synthetic-cohort",
        "natural_manifest_path": "synthetic-natural",
        "phase1_run_dir": "synthetic-phase1",
        "phase2_run_dir": "synthetic-phase2",
        "dataset_manifest_path": "synthetic-dataset",
        "source_bundle_path": "synthetic-source",
        "execution_run_dir": "synthetic-execution",
        "output_dir": tmp_path / "private-output",
    }


def test_formal_packet_is_deterministic_separated_and_byte_verifiable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    trace_ids = _patch_formal_runtime(monkeypatch)
    arguments = _formal_arguments(tmp_path)
    first = prepare_p1_formal_packet(**arguments)
    second = prepare_p1_formal_packet(**arguments)
    assert first.preflight == second.preflight
    result = write_p1_formal_packet(**arguments)
    assert result.manifest.item_count == 20
    assert stat.S_IMODE(result.bundle_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(result.participant_packet_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(result.coordinator_identity_map_path.stat().st_mode) == 0o600

    packet_text = result.participant_packet_path.read_text(encoding="utf-8")
    identity_text = result.coordinator_identity_map_path.read_text(encoding="utf-8")
    assert len(packet_text.splitlines()) == 20
    assert len(result.participant_labels_template_path.read_text().splitlines()) == 20
    assert all(trace_id not in packet_text for trace_id in trace_ids)
    assert all(trace_id in identity_text for trace_id in trace_ids)
    for forbidden in (
        "mutation_kind",
        "method_predictions",
        "primary_rater",
        "other_rater_labels",
    ):
        assert forbidden not in packet_text

    verification = verify_p1_formal_packet(
        manifest_path=result.manifest_path,
        expected_manifest_sha256=result.manifest_sha256,
        **arguments,
    )
    assert verification.verified is True
    assert verification.item_count == 20

    result.participant_labels_template_path.write_bytes(
        result.participant_labels_template_path.read_bytes() + b" "
    )
    with pytest.raises(Phase4P1AnnotationError, match="deterministic regeneration"):
        verify_p1_formal_packet(manifest_path=result.manifest_path, **arguments)
