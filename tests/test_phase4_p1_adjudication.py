from __future__ import annotations

import hashlib
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tracejudge_hy3.phase3.annotations import BlindedAnnotationTask, _jsonl_bytes
from tracejudge_hy3.phase4 import (
    P1AgreementManifest,
    P1CohortAgreement,
    P1FormalPacketManifest,
    P1InterRaterAgreementAnalysis,
    Phase4P1AnnotationError,
    complete_p1_consensus_adjudication,
    initialize_p1_adjudication,
    preflight_p1_adjudication,
    preflight_p1_consensus_adjudication,
    verify_p1_adjudication,
    verify_p1_completed_adjudication,
)
from tracejudge_hy3.phase4.p1_adjudication_sensitivity import (
    P1PostAdjudicationSensitivityError,
    analyze_p1_post_adjudication_sensitivity,
    publish_p1_post_adjudication_sensitivity,
    verify_p1_post_adjudication_sensitivity,
)
from tracejudge_hy3.phase4.p1_agreement import (
    _agreement_proportion,
    _binary_agreement,
    _conditional_agreement,
)
from tracejudge_hy3.phase4.p1_annotations import _json_bytes

NOW = datetime(2026, 9, 4, 9, 0, tzinfo=UTC)


def _digest(value: bytes | str) -> str:
    payload = value if isinstance(value, bytes) else value.encode()
    return hashlib.sha256(payload).hexdigest()


def _write_private(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    path.write_bytes(payload)
    path.chmod(0o600)


def _aggregate_agreement_fixture(tmp_path: Path) -> tuple[Path, Path]:
    has_error = (True, True, True, True, True, True, *([False] * 14))
    process_correct = tuple(not value for value in has_error)
    reasoning = (False, True, False, True, False, True, *([True] * 14))
    plan_primary = (False, False, True, True, True, True, *([True] * 14))
    plan_secondary = (True, False, True, True, True, True, *([True] * 14))
    seed = 17
    iterations = 100

    binary_fields = (
        _binary_agreement("has_error", has_error, has_error, seed=seed, iterations=iterations),
        _binary_agreement(
            "process_correct",
            process_correct,
            process_correct,
            seed=seed,
            iterations=iterations,
        ),
        _binary_agreement(
            "reasoning_correct", reasoning, reasoning, seed=seed, iterations=iterations
        ),
        _binary_agreement(
            "plan_code_aligned",
            plan_primary,
            plan_secondary,
            seed=seed,
            iterations=iterations,
        ),
    )
    layer_primary = (
        "reasoning",
        "implementation",
        "implementation",
        "execution",
        "requirement",
        "alignment",
        *([None] * 14),
    )
    layer_secondary = (
        "alignment",
        *layer_primary[1:],
    )
    step_primary = ("S1", "S2", "S3", "S4", "S1", "S2", *([None] * 14))
    step_secondary = ("S2", *step_primary[1:])
    error_type_primary = (
        "P01_ALGORITHM_ERROR",
        "C01_BOUNDARY_ERROR",
        "C02_RUNTIME_ERROR",
        "E01_EXECUTION_ERROR",
        "R01_REQUIREMENT_MISREAD",
        "A01_PLAN_CODE_MISMATCH",
        *([None] * 14),
    )
    error_type_secondary = ("A01_PLAN_CODE_MISMATCH", *error_type_primary[1:])
    joint_primary = tuple(zip(layer_primary, step_primary, error_type_primary, strict=True))
    joint_secondary = tuple(zip(layer_secondary, step_secondary, error_type_secondary, strict=True))
    localization_fields = (
        _conditional_agreement(
            "first_faulty_layer",
            layer_primary,
            layer_secondary,
            has_error,
            has_error,
        ),
        _conditional_agreement(
            "first_faulty_step", step_primary, step_secondary, has_error, has_error
        ),
        _conditional_agreement(
            "error_type",
            error_type_primary,
            error_type_secondary,
            has_error,
            has_error,
        ),
        _conditional_agreement(
            "joint_fault_label",
            joint_primary,
            joint_secondary,
            has_error,
            has_error,
        ),
    )

    cohort_has_error = []
    for cohort, indices in (
        ("all", tuple(range(20))),
        ("natural", tuple(range(15))),
        ("counterfactual", tuple(range(15, 20))),
    ):
        values = tuple(has_error[index] for index in indices)
        cohort_has_error.append(
            P1CohortAgreement(
                cohort=cohort,
                has_error=_binary_agreement(
                    "has_error", values, values, seed=seed, iterations=iterations
                ),
            )
        )

    analysis = P1InterRaterAgreementAnalysis(
        source_primary_manifest_sha256=_digest("primary manifest"),
        source_primary_annotations_sha256=_digest("primary annotations"),
        source_secondary_manifest_sha256=_digest("secondary manifest"),
        source_secondary_annotations_sha256=_digest("secondary annotations"),
        primary_annotation_protocol_sha256=_digest("primary protocol"),
        secondary_annotation_protocol_sha256=_digest("secondary protocol"),
        shared_annotation_guide_sha256=_digest("shared guide"),
        primary_rater_id="synthetic_primary",
        secondary_rater_id="synthetic_secondary",
        binary_fields=binary_fields,
        localization_fields=localization_fields,
        full_record_exact_agreement=_agreement_proportion(19, 20),
        cohort_has_error=tuple(cohort_has_error),
    )
    analysis_payload = _json_bytes(analysis)
    report_payload = b"aggregate-only synthetic agreement report\n"
    manifest = P1AgreementManifest(
        created_at=NOW,
        analysis_sha256=_digest(analysis_payload),
        report_sha256=_digest(report_payload),
        source_primary_manifest_sha256=analysis.source_primary_manifest_sha256,
        source_primary_annotations_sha256=analysis.source_primary_annotations_sha256,
        source_secondary_manifest_sha256=analysis.source_secondary_manifest_sha256,
        source_secondary_annotations_sha256=analysis.source_secondary_annotations_sha256,
    )
    source_dir = tmp_path / "aggregate"
    manifest_path = source_dir / "manifest.json"
    _write_private(source_dir / "agreement.json", analysis_payload)
    _write_private(source_dir / "report.md", report_payload)
    _write_private(manifest_path, _json_bytes(manifest))
    return manifest_path, tmp_path / "adjudication"


def _formal_packet_fixture(tmp_path: Path) -> Path:
    cases = tuple(
        BlindedAnnotationTask(
            annotation_item_id=f"formal_item_{index:03d}",
            problem_id=f"synthetic_problem_{index:03d}",
            code_sha256=_digest(f"code {index}"),
            structured_explanation_sha256=_digest(f"explanation {index}"),
            functional_evidence_sha256=_digest(f"evidence {index}"),
            public_problem={"requirement": f"requirement {index}"},
            structured_solution_trace={"steps": ["S1", "S2", "S3"]},
            candidate_code=f"def candidate_{index}():\n    return {index}\n",
            functional_evidence={"status": "passed"},
            public_dynamic_evidence={"kind": "synthetic"},
        )
        for index in range(1, 21)
    )
    packet_payload = _jsonl_bytes(cases)
    manifest = P1FormalPacketManifest(
        phase3_annotation_guide_sha256=_digest("guide"),
        delivery_record_sha256=_digest("delivery"),
        practice_admission_sha256=_digest("admission"),
        formal_subset_private_manifest_sha256=_digest("private subset"),
        formal_subset_public_commitment_sha256=_digest("public subset"),
        selected_materials_sha256=_digest("materials"),
        ordered_annotation_item_ids=tuple(case.annotation_item_id for case in cases),
        participant_packet_sha256=_digest(packet_payload),
        participant_labels_template_sha256=_digest("labels template"),
        coordinator_identity_map_sha256=_digest("identity map"),
    )
    source_dir = tmp_path / "formal_packet"
    _write_private(source_dir / "participant/packet.jsonl", packet_payload)
    manifest_path = source_dir / "manifest.json"
    _write_private(manifest_path, _json_bytes(manifest))
    return manifest_path


def _consensus_arguments(
    *, pending_manifest: Path, formal_packet_manifest: Path, output: Path
) -> dict[str, object]:
    return {
        "annotation_item_id": "formal_item_013",
        "plan_code_aligned": False,
        "first_faulty_layer": "alignment",
        "first_faulty_step": "S3",
        "error_type": "A01_PLAN_CODE_MISMATCH",
        "decision_rationale": (
            "S3 promises continued interval expansion, but the code can exit before "
            "bracketing a root."
        ),
        "adjudication_started_at": datetime.fromisoformat("2026-09-04T11:00:00+08:00"),
        "adjudication_completed_at": datetime.fromisoformat("2026-09-04T14:00:00+08:00"),
        "both_original_raters_confirmed": True,
        "adjudicators_blinded_to_method_predictions": True,
        "pending_manifest_path": pending_manifest,
        "expected_pending_manifest_sha256": None,
        "formal_packet_manifest_path": formal_packet_manifest,
        "expected_formal_packet_manifest_sha256": None,
        "output_dir": output,
    }


def test_pending_adjudication_is_private_versioned_and_non_overwriting(
    tmp_path: Path,
) -> None:
    agreement_manifest, output = _aggregate_agreement_fixture(tmp_path)
    arguments = {
        "agreement_manifest_path": agreement_manifest,
        "expected_agreement_manifest_sha256": None,
        "output_dir": output,
    }
    preflight = preflight_p1_adjudication(**arguments)
    assert preflight.ready_to_initialize is True
    assert preflight.record.status == "pending_human_review"
    assert preflight.record.record_version == 1
    assert preflight.record.disagreement_summary.full_record_disagreement_count == 1
    assert preflight.record.disagreement_summary.has_error_disagreement_count == 0
    assert preflight.record.disagreement_annotation_item_id is None
    assert preflight.record.decision is None

    result = initialize_p1_adjudication(**arguments)
    assert stat.S_IMODE(result.run_dir.stat().st_mode) == 0o700
    for path in (
        result.manifest_path,
        result.record_path,
        result.working_template_path,
        result.instructions_path,
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    record = result.record_path.read_text(encoding="utf-8")
    assert "synthetic_primary" not in record
    assert "synthetic_secondary" not in record
    assert "pending_human_review" in record
    assert 'disagreement_annotation_item_id": null' in record

    verification = verify_p1_adjudication(
        manifest_path=result.manifest_path,
        expected_manifest_sha256=result.manifest_sha256,
    )
    assert verification.verified is True
    assert verification.status == "pending_human_review"

    with pytest.raises(Phase4P1AnnotationError, match="already exists"):
        initialize_p1_adjudication(**arguments)


def test_pending_adjudication_rejects_tampering(tmp_path: Path) -> None:
    agreement_manifest, output = _aggregate_agreement_fixture(tmp_path)
    result = initialize_p1_adjudication(
        agreement_manifest_path=agreement_manifest,
        expected_agreement_manifest_sha256=None,
        output_dir=output,
    )
    result.working_template_path.write_bytes(result.working_template_path.read_bytes() + b" ")
    result.working_template_path.chmod(0o600)
    with pytest.raises(Phase4P1AnnotationError, match="hashes are inconsistent"):
        verify_p1_adjudication(manifest_path=result.manifest_path)


def test_completed_consensus_is_separate_private_and_non_overwriting(tmp_path: Path) -> None:
    agreement_manifest, output = _aggregate_agreement_fixture(tmp_path)
    pending = initialize_p1_adjudication(
        agreement_manifest_path=agreement_manifest,
        expected_agreement_manifest_sha256=None,
        output_dir=output,
    )
    packet_manifest = _formal_packet_fixture(tmp_path)
    protected_payloads = {
        path: path.read_bytes()
        for path in (
            pending.manifest_path,
            pending.record_path,
            pending.working_template_path,
            pending.instructions_path,
            packet_manifest,
            packet_manifest.parent / "participant/packet.jsonl",
        )
    }
    arguments = _consensus_arguments(
        pending_manifest=pending.manifest_path,
        formal_packet_manifest=packet_manifest,
        output=output,
    )

    preflight = preflight_p1_consensus_adjudication(**arguments)
    assert preflight.ready_to_complete is True
    assert preflight.record.status == "completed_human_consensus"
    assert preflight.record.annotation_item_id == "formal_item_013"
    assert preflight.record.decision.plan_code_aligned is False
    assert preflight.record.decision.first_faulty_layer == "alignment"
    assert preflight.record.decision.first_faulty_step == "S3"
    assert preflight.record.decision.error_type.value == "A01_PLAN_CODE_MISMATCH"
    assert preflight.record.raw_participant_label_data_accessed_by_completer is False

    result = complete_p1_consensus_adjudication(**arguments)
    assert result.run_dir != pending.run_dir
    assert stat.S_IMODE(result.run_dir.stat().st_mode) == 0o700
    for path in (result.manifest_path, result.decision_path, result.report_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert all(path.read_bytes() == payload for path, payload in protected_payloads.items())

    verification = verify_p1_completed_adjudication(
        manifest_path=result.manifest_path,
        expected_manifest_sha256=result.manifest_sha256,
    )
    assert verification.verified is True
    assert verification.status == "completed_human_consensus"
    assert verification.annotation_item_id == "formal_item_013"

    with pytest.raises(Phase4P1AnnotationError, match="already exists"):
        complete_p1_consensus_adjudication(**arguments)


def test_completed_consensus_rejects_missing_confirmation_and_tampering(
    tmp_path: Path,
) -> None:
    agreement_manifest, output = _aggregate_agreement_fixture(tmp_path)
    pending = initialize_p1_adjudication(
        agreement_manifest_path=agreement_manifest,
        expected_agreement_manifest_sha256=None,
        output_dir=output,
    )
    packet_manifest = _formal_packet_fixture(tmp_path)
    arguments = _consensus_arguments(
        pending_manifest=pending.manifest_path,
        formal_packet_manifest=packet_manifest,
        output=output,
    )
    missing_confirmation = {**arguments, "both_original_raters_confirmed": False}
    with pytest.raises(Phase4P1AnnotationError, match="requires confirmation"):
        preflight_p1_consensus_adjudication(**missing_confirmation)

    result = complete_p1_consensus_adjudication(**arguments)
    result.report_path.write_bytes(result.report_path.read_bytes() + b" ")
    result.report_path.chmod(0o600)
    with pytest.raises(Phase4P1AnnotationError, match="hashes are inconsistent"):
        verify_p1_completed_adjudication(manifest_path=result.manifest_path)


def test_post_adjudication_sensitivity_preserves_raw_metrics_and_bounds_impact(
    tmp_path: Path,
) -> None:
    agreement_manifest, output = _aggregate_agreement_fixture(tmp_path)
    pending = initialize_p1_adjudication(
        agreement_manifest_path=agreement_manifest,
        expected_agreement_manifest_sha256=None,
        output_dir=output,
    )
    packet_manifest = _formal_packet_fixture(tmp_path)
    completed = complete_p1_consensus_adjudication(
        **_consensus_arguments(
            pending_manifest=pending.manifest_path,
            formal_packet_manifest=packet_manifest,
            output=output,
        )
    )
    analysis = analyze_p1_post_adjudication_sensitivity(
        agreement_manifest_path=agreement_manifest,
        expected_agreement_manifest_sha256=None,
        completed_adjudication_manifest_path=completed.manifest_path,
        expected_completed_adjudication_manifest_sha256=None,
    )

    binary = {item.field_name: item for item in analysis.raw_binary_fields}
    impact = {item.metric_name: item for item in analysis.impact_envelopes}
    assert analysis.raw_full_record_exact_agreement.agreeing_count == 19
    assert binary["has_error"].raw_agreement.agreeing_count == 20
    assert binary["plan_code_aligned"].raw_agreement.agreeing_count == 19
    assert analysis.resolved_disagreement_count == 1
    assert analysis.unresolved_disagreement_count == 0
    assert analysis.post_adjudication_inter_rater_agreement_created is False
    assert impact["has_error_detection"].maximum_changed_reference_items == 0
    assert impact["plan_code_aligned"].maximum_changed_reference_items == 1
    assert (
        impact["first_faulty_step"].maximum_absolute_change_percentage_points_if_fixed_denominator_6
        == 16.666666666667
    )
    assert analysis.actual_method_level_deltas_computed is False
    assert analysis.raw_participant_label_data_accessed is False


def test_post_adjudication_sensitivity_release_is_public_safe_and_reproducible(
    tmp_path: Path,
) -> None:
    agreement_manifest, private_output = _aggregate_agreement_fixture(tmp_path)
    pending = initialize_p1_adjudication(
        agreement_manifest_path=agreement_manifest,
        expected_agreement_manifest_sha256=None,
        output_dir=private_output,
    )
    packet_manifest = _formal_packet_fixture(tmp_path)
    completed = complete_p1_consensus_adjudication(
        **_consensus_arguments(
            pending_manifest=pending.manifest_path,
            formal_packet_manifest=packet_manifest,
            output=private_output,
        )
    )
    public_output = tmp_path / "public"
    arguments = {
        "agreement_manifest_path": agreement_manifest,
        "expected_agreement_manifest_sha256": None,
        "completed_adjudication_manifest_path": completed.manifest_path,
        "expected_completed_adjudication_manifest_sha256": None,
        "output_dir": public_output,
    }
    first = publish_p1_post_adjudication_sensitivity(**arguments)
    second = publish_p1_post_adjudication_sensitivity(**arguments)

    assert first.json_sha256 == second.json_sha256
    assert first.markdown_sha256 == second.markdown_sha256
    assert stat.S_IMODE(first.json_path.stat().st_mode) == 0o644
    assert stat.S_IMODE(first.markdown_path.stat().st_mode) == 0o644
    combined = first.json_path.read_text() + first.markdown_path.read_text()
    assert "formal_item_013" not in combined
    assert "A01_PLAN_CODE_MISMATCH" not in combined
    assert "S3 promises continued" not in combined
    assert "不是新的 20/20 标注者一致率" in combined
    assert "1/1 已解决" in combined

    verification = verify_p1_post_adjudication_sensitivity(
        **arguments,
        expected_json_sha256=first.json_sha256,
        expected_markdown_sha256=first.markdown_sha256,
    )
    assert verification.verified is True

    first.markdown_path.write_bytes(first.markdown_path.read_bytes() + b" ")
    with pytest.raises(
        P1PostAdjudicationSensitivityError,
        match="differs from deterministic regeneration",
    ):
        verify_p1_post_adjudication_sensitivity(**arguments)
