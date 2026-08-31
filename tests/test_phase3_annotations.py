from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import tracejudge_hy3.cli as cli_module
from tracejudge_hy3.cli import app
from tracejudge_hy3.phase3.annotations import (
    ANNOTATION_GUIDE_SHA256,
    ANNOTATION_PROTOCOL_SHA256,
    AnnotationPacketPreflightResult,
    Phase3AnnotationError,
    export_annotation_packet,
    preflight_annotation_packet,
)
from tracejudge_hy3.phase3.contracts import (
    Phase2FunctionalEvidenceRef,
    PublicFixtureExecutionCaseResult,
    PublicFixtureExecutionResult,
    PublicFixtureFunctionalEvidenceRef,
)
from tracejudge_hy3.phase3.labels import (
    AnnotationLabelCheckResult,
    check_annotation_labels,
    freeze_annotation_labels,
    preflight_annotation_labels_freeze,
)
from tracejudge_hy3.phase3.materials import LoadedPhase3Materials, _public_execution_payload
from tracejudge_hy3.phase3.privacy import canonical_sha256
from tracejudge_hy3.phase3.runner import (
    Phase3TraceMaterial,
    PublicDynamicEvidenceInput,
    functional_evidence_payload,
)
from tracejudge_hy3.schemas.solution import ImplementationStep, SolutionTrace

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = REPO_ROOT / "data" / "phase3" / "annotation_protocol_v1.json"
GUIDE = REPO_ROOT / "docs" / "experiments" / "phase3_annotation_guide_v1.md"
COHORT_SHA = "3290221625d687e6d7412a0544247dc81a34857b114a545458b93cc04e35d255"
H0 = "0" * 64
H1 = "1" * 64
H2 = "2" * 64
H3 = "3" * 64
H4 = "4" * 64


def _solution() -> SolutionTrace:
    return SolutionTrace(
        problem_id="safe_mean",
        requirement_understanding="Return zero for empty input and the mean otherwise.",
        design_summary="Guard empty input, then divide the sum by the length.",
        edge_cases_considered=["empty input"],
        implementation_steps=[
            ImplementationStep(
                step_id="S1",
                content="Return zero for empty input.",
                related_requirements=["R1"],
            )
        ],
        declared_time_complexity="O(n)",
        declared_space_complexity="O(1)",
        code=(
            "def safe_mean(nums: list[float]) -> float:\n"
            "    if not nums:\n"
            "        return 0.0\n"
            "    return sum(nums) / len(nums)\n"
        ),
    )


def _loaded_fixture() -> LoadedPhase3Materials:
    solution = _solution()
    public_problem = {
        "problem_id": "safe_mean",
        "requirement": "Return zero for empty input and the arithmetic mean otherwise.",
        "function_signature": "def safe_mean(nums: list[float]) -> float:",
        "requirements": [{"requirement_id": "R1", "content": "Handle empty input."}],
        "visible_test_cases": [{"args": [[]], "kwargs": {}, "expected": 0.0}],
    }
    natural_evidence = Phase2FunctionalEvidenceRef(
        phase2_run_id="phase2_fixture",
        problem_id="safe_mean",
        result_line_number=1,
        result_record_sha256=H1,
        functional_evidence_sha256=H1,
        code_sha256=H2,
        base_status="pass",
        plus_status="pass",
        passed_base=True,
        passed_plus=True,
    )
    public_evidence = PublicFixtureFunctionalEvidenceRef(
        phase3_execution_run_id="public_evidence_fixture",
        execution_subject_id="counterfactual:safe_mean:boundary_deletion:v1",
        problem_id="safe_mean",
        result_line_number=1,
        result_record_sha256=H3,
        functional_evidence_sha256=H3,
        code_sha256=H4,
        public_fixture_id="safe_mean_fixture",
        public_fixture_sha256=H0,
        replay_spec_sha256=H1,
        execution_status="fail",
    )
    natural_dynamic_payload = {
        "policy_version": "phase3_public_dynamic_evidence_v1",
        "availability": "not_available",
        "reason": "no_frozen_public_oracle_for_natural_trace",
        "attempted_public_case_count": 0,
        "functional_evidence_sha256": H1,
    }
    public_dynamic_payload = {
        "policy_version": "phase3_public_dynamic_evidence_v1",
        "availability": "available",
        "source": "frozen_public_fixture_execution",
        "problem_id": "safe_mean",
        "execution_status": "fail",
        "case_results": [
            {
                "case_id": "empty",
                "category": "challenge",
                "passed": False,
                "actual_output": None,
                "expected_output": 0.0,
                "exception_type": "ZeroDivisionError",
                "timed_out": False,
                "related_requirements": ["R1"],
            }
        ],
    }
    materials = {
        "natural:safe_mean": Phase3TraceMaterial(
            trace_id="natural:safe_mean",
            public_problem=public_problem,
            solution_trace=solution,
            functional_evidence=natural_evidence,
            public_dynamic_evidence=PublicDynamicEvidenceInput(
                status="available",
                evidence_sha256=canonical_sha256(natural_dynamic_payload),
                payload=natural_dynamic_payload,
            ),
        ),
        "counterfactual:safe_mean:boundary_deletion:v1": Phase3TraceMaterial(
            trace_id="counterfactual:safe_mean:boundary_deletion:v1",
            public_problem=public_problem,
            solution_trace=solution,
            functional_evidence=public_evidence,
            public_dynamic_evidence=PublicDynamicEvidenceInput(
                status="available",
                evidence_sha256=canonical_sha256(public_dynamic_payload),
                payload=public_dynamic_payload,
            ),
        ),
    }
    traces = {
        "natural:safe_mean": SimpleNamespace(
            problem_id="safe_mean",
            code_sha256=H2,
            structured_explanation_sha256=H0,
            functional_evidence=natural_evidence,
        ),
        "counterfactual:safe_mean:boundary_deletion:v1": SimpleNamespace(
            problem_id="safe_mean",
            code_sha256=H4,
            structured_explanation_sha256=H0,
            functional_evidence=public_evidence,
        ),
    }
    cohort = SimpleNamespace(
        overlay_manifest_sha256=COHORT_SHA,
        ordered_trace_ids=tuple(traces),
        traces_by_id=traces,
        natural_trace_count=1,
        counterfactual_trace_count=1,
    )
    return LoadedPhase3Materials(
        cohort=cohort,
        materials=materials,
        material_payloads_sha256=H0,
        natural_dynamic_unavailable_count=1,
        public_dynamic_evidence_count=1,
    )


def _kwargs(tmp_path: Path) -> dict[str, object]:
    return {
        "packet_id": "phase3_annotations_primary_v1",
        "rater_id": "primary_rater",
        "annotation_round": 1,
        "blinded_to_other_raters": True,
        "cohort_manifest_path": "overlay.json",
        "natural_manifest_path": "natural.json",
        "phase1_run_dir": "phase1",
        "phase2_run_dir": "phase2",
        "dataset_manifest_path": "dataset.json",
        "source_bundle_path": "counterfactuals.json",
        "execution_run_dir": "public-evidence",
        "protocol_path": PROTOCOL,
        "guide_path": GUIDE,
        "output_dir": tmp_path / "annotations",
    }


def _completed_labels(template_path: Path, output_path: Path) -> None:
    rows = [json.loads(line) for line in template_path.read_text(encoding="utf-8").splitlines()]
    rows[0].update(
        {
            "status": "completed",
            "process_correct": True,
            "has_error": False,
            "reasoning_correct": True,
            "plan_code_aligned": True,
            "rationale": "No supported process error is present in the visible material.",
        }
    )
    rows[1].update(
        {
            "status": "completed",
            "process_correct": False,
            "has_error": True,
            "reasoning_correct": True,
            "plan_code_aligned": False,
            "first_faulty_layer": "alignment",
            "first_faulty_step": "S1",
            "error_type": "A01_PLAN_CODE_MISMATCH",
            "rationale": "The implementation omits the boundary behavior stated by S1.",
        }
    )
    output_path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    output_path.chmod(0o600)


def test_frozen_protocol_and_guide_hashes_match_bytes():
    assert canonical_sha256(json.loads(PROTOCOL.read_text(encoding="utf-8"))) != H0
    assert __import__("hashlib").sha256(PROTOCOL.read_bytes()).hexdigest() == (
        ANNOTATION_PROTOCOL_SHA256
    )
    assert __import__("hashlib").sha256(GUIDE.read_bytes()).hexdigest() == (ANNOTATION_GUIDE_SHA256)


def test_annotation_packet_preflight_is_deterministic_and_write_free(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.setattr(
        "tracejudge_hy3.phase3.annotations.load_phase3_materials",
        lambda **_kwargs: _loaded_fixture(),
    )
    kwargs = _kwargs(tmp_path)
    first = preflight_annotation_packet(**kwargs)
    second = preflight_annotation_packet(**kwargs)

    assert first == second
    assert first.item_count == 2
    assert first.natural_item_count == 1
    assert first.counterfactual_item_count == 1
    assert not Path(kwargs["output_dir"]).exists()


def test_annotation_packet_export_separates_blind_packet_and_identity_map(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.setattr(
        "tracejudge_hy3.phase3.annotations.load_phase3_materials",
        lambda **_kwargs: _loaded_fixture(),
    )
    result = export_annotation_packet(**_kwargs(tmp_path))

    packet_text = result.packet_path.read_text(encoding="utf-8")
    identity_text = result.identity_map_path.read_text(encoding="utf-8")
    assert "boundary_deletion" not in packet_text
    assert "counterfactual:" not in packet_text
    assert "execution_subject_id" not in packet_text
    assert "expected_execution_status" not in packet_text
    assert "expectation_met" not in packet_text
    assert "method_predictions" not in packet_text
    assert "counterfactual:safe_mean:boundary_deletion:v1" in identity_text
    packet_rows = [json.loads(line) for line in packet_text.splitlines()]
    assert all("structured_solution_trace" in item for item in packet_rows)
    assert all("candidate_code" in item for item in packet_rows)
    assert all("solution_trace" not in item for item in packet_rows)
    assert stat.S_IMODE(result.run_dir.stat().st_mode) == 0o700
    for path in (
        result.manifest_path,
        result.packet_path,
        result.identity_map_path,
        result.labels_template_path,
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    templates = [
        json.loads(line)
        for line in result.labels_template_path.read_text(encoding="utf-8").splitlines()
    ]
    assert all(item["status"] == "pending" for item in templates)
    assert all(item["has_error"] is None for item in templates)


def test_public_evidence_projection_removes_counterfactual_construction_metadata():
    functional = (
        _loaded_fixture()
        .materials["counterfactual:safe_mean:boundary_deletion:v1"]
        .functional_evidence
    )
    projected_functional = functional_evidence_payload(functional)
    assert "execution_subject_id" not in projected_functional

    result = PublicFixtureExecutionResult(
        run_id="public_evidence_fixture",
        execution_subject_id="counterfactual:safe_mean:boundary_deletion:v1",
        problem_id="safe_mean",
        public_fixture_id="safe_mean_fixture",
        public_fixture_sha256=H0,
        code_sha256=H4,
        replay_spec_sha256=H1,
        execution_status="fail",
        expected_execution_status="fail",
        expectation_met=True,
        case_count=1,
        pass_count=0,
        fail_count=1,
        timeout_count=0,
        case_results=(
            PublicFixtureExecutionCaseResult(
                case_id="empty",
                category="challenge",
                passed=False,
                actual_output=None,
                expected_output=0.0,
                exception_type="ZeroDivisionError",
                related_requirements=("R1",),
            ),
        ),
    )
    projected_dynamic = _public_execution_payload(
        result,
        execution_evidence_sha256=H3,
    )
    assert "execution_subject_id" not in projected_dynamic
    assert "expected_execution_status" not in projected_dynamic
    assert "expectation_met" not in projected_dynamic


def test_tampered_annotation_protocol_is_rejected_before_material_loading(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    tampered = tmp_path / "protocol.json"
    payload = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    payload["annotation_order_seed"] += 1
    tampered.write_text(json.dumps(payload), encoding="utf-8")
    called = False

    def fake_loader(**_kwargs):
        nonlocal called
        called = True
        return _loaded_fixture()

    monkeypatch.setattr(
        "tracejudge_hy3.phase3.annotations.load_phase3_materials",
        fake_loader,
    )
    kwargs = _kwargs(tmp_path)
    kwargs["protocol_path"] = tampered
    with pytest.raises(Phase3AnnotationError, match="differs from the frozen"):
        preflight_annotation_packet(**kwargs)
    assert called is False


def test_cli_annotation_packet_preflight_reports_zero_write(
    monkeypatch: pytest.MonkeyPatch,
):
    expected = AnnotationPacketPreflightResult(
        packet_id="phase3_annotations_primary_v1",
        protocol_id="phase3_annotation_protocol_57_v1",
        annotation_protocol_sha256=H0,
        annotation_guide_sha256=H1,
        item_count=57,
        natural_item_count=42,
        counterfactual_item_count=15,
        material_payloads_sha256=H2,
        packet_sha256=H3,
        identity_map_sha256=H4,
        labels_template_sha256=H0,
        rater_id="primary_rater",
        annotation_round=1,
    )
    calls: list[dict[str, object]] = []

    def fake_preflight(**kwargs):
        calls.append(kwargs)
        return expected

    monkeypatch.setattr(cli_module, "preflight_annotation_packet", fake_preflight)
    result = CliRunner().invoke(
        app,
        [
            "phase3",
            "annotation-packet-preflight",
            "--packet-id",
            "phase3_annotations_primary_v1",
            "--rater-id",
            "primary_rater",
            "--cohort-manifest",
            "overlay.json",
            "--natural-manifest",
            "natural.json",
            "--phase1-run",
            "phase1",
            "--phase2-run",
            "phase2",
            "--dataset-manifest",
            "dataset.json",
            "--execution-run",
            "public-evidence",
        ],
    )
    assert result.exit_code == 0
    assert len(calls) == 1
    assert "57" in result.stdout
    assert "否 / 否 / 否 / 否" in result.stdout


def test_annotation_label_check_preserves_identity_blinding_until_complete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.setattr(
        "tracejudge_hy3.phase3.annotations.load_phase3_materials",
        lambda **_kwargs: _loaded_fixture(),
    )
    packet = export_annotation_packet(**_kwargs(tmp_path))

    def identity_must_not_be_opened(*_args, **_kwargs):
        raise AssertionError("progress check opened the coordinator identity map")

    monkeypatch.setattr(
        "tracejudge_hy3.phase3.labels._load_identities",
        identity_must_not_be_opened,
    )
    pending = check_annotation_labels(
        packet_run_dir=packet.run_dir,
        expected_packet_manifest_sha256=packet.manifest_sha256,
        completed_labels_path=packet.labels_template_path,
        protocol_path=PROTOCOL,
        guide_path=GUIDE,
    )
    assert pending.completed_count == 0
    assert pending.pending_count == 2
    assert pending.invalid_count == 0
    assert pending.ready_to_freeze is False

    completed_path = tmp_path / "completed.jsonl"
    _completed_labels(packet.labels_template_path, completed_path)
    completed = check_annotation_labels(
        packet_run_dir=packet.run_dir,
        expected_packet_manifest_sha256=packet.manifest_sha256,
        completed_labels_path=completed_path,
        protocol_path=PROTOCOL,
        guide_path=GUIDE,
    )
    assert completed.completed_count == 2
    assert completed.pending_count == 0
    assert completed.ready_to_freeze is True


def test_annotation_label_check_reports_invalid_and_order_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.setattr(
        "tracejudge_hy3.phase3.annotations.load_phase3_materials",
        lambda **_kwargs: _loaded_fixture(),
    )
    packet = export_annotation_packet(**_kwargs(tmp_path))
    completed_path = tmp_path / "completed.jsonl"
    _completed_labels(packet.labels_template_path, completed_path)
    rows = [json.loads(line) for line in completed_path.read_text(encoding="utf-8").splitlines()]
    rows[0]["process_correct"] = False
    rows.reverse()
    completed_path.write_text(
        "".join(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    completed_path.chmod(0o600)

    result = check_annotation_labels(
        packet_run_dir=packet.run_dir,
        expected_packet_manifest_sha256=packet.manifest_sha256,
        completed_labels_path=completed_path,
        protocol_path=PROTOCOL,
        guide_path=GUIDE,
    )
    assert result.invalid_count == 1
    assert result.order_mismatch_count == 1
    assert result.invalid_line_numbers == (2,)
    assert result.ready_to_freeze is False


def test_annotation_label_check_reports_missing_rows_and_rejects_broad_permissions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.setattr(
        "tracejudge_hy3.phase3.annotations.load_phase3_materials",
        lambda **_kwargs: _loaded_fixture(),
    )
    packet = export_annotation_packet(**_kwargs(tmp_path))
    labels_path = tmp_path / "working.jsonl"
    first_line = packet.labels_template_path.read_text(encoding="utf-8").splitlines()[0]
    labels_path.write_text(first_line + "\n", encoding="utf-8")
    labels_path.chmod(0o600)

    result = check_annotation_labels(
        packet_run_dir=packet.run_dir,
        expected_packet_manifest_sha256=packet.manifest_sha256,
        completed_labels_path=labels_path,
        protocol_path=PROTOCOL,
        guide_path=GUIDE,
    )
    assert result.pending_count == 1
    assert result.missing_item_count == 1
    assert result.ready_to_freeze is False

    labels_path.chmod(0o644)
    with pytest.raises(Phase3AnnotationError) as exc_info:
        check_annotation_labels(
            packet_run_dir=packet.run_dir,
            expected_packet_manifest_sha256=packet.manifest_sha256,
            completed_labels_path=labels_path,
            protocol_path=PROTOCOL,
            guide_path=GUIDE,
        )
    assert exc_info.value.safe_stage == "P3E_LABELS_PERMISSIONS"


def test_annotation_label_freeze_rejects_tampered_packet_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    loaded = _loaded_fixture()
    monkeypatch.setattr(
        "tracejudge_hy3.phase3.annotations.load_phase3_materials",
        lambda **_kwargs: loaded,
    )
    packet = export_annotation_packet(**_kwargs(tmp_path))
    completed_path = tmp_path / "completed.jsonl"
    _completed_labels(packet.labels_template_path, completed_path)
    packet.identity_map_path.write_bytes(packet.identity_map_path.read_bytes() + b" ")
    packet.identity_map_path.chmod(0o600)

    with pytest.raises(Phase3AnnotationError) as exc_info:
        preflight_annotation_labels_freeze(
            annotation_set_id="phase3_labels_primary_v1",
            packet_run_dir=packet.run_dir,
            expected_packet_manifest_sha256=packet.manifest_sha256,
            completed_labels_path=completed_path,
            cohort_manifest_path="overlay.json",
            natural_manifest_path="natural.json",
            protocol_path=PROTOCOL,
            guide_path=GUIDE,
            output_dir=tmp_path / "frozen-labels",
        )
    assert exc_info.value.safe_stage == "P3E_IDENTITY_JOIN"


def test_annotation_label_check_rejects_unexpected_packet_manifest_hash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.setattr(
        "tracejudge_hy3.phase3.annotations.load_phase3_materials",
        lambda **_kwargs: _loaded_fixture(),
    )
    packet = export_annotation_packet(**_kwargs(tmp_path))
    with pytest.raises(Phase3AnnotationError) as exc_info:
        check_annotation_labels(
            packet_run_dir=packet.run_dir,
            expected_packet_manifest_sha256=H0,
            completed_labels_path=packet.labels_template_path,
            protocol_path=PROTOCOL,
            guide_path=GUIDE,
        )
    assert exc_info.value.safe_stage == "P3E_PACKET_IDENTITY"


def test_annotation_label_freeze_binds_identity_and_writes_private_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    loaded = _loaded_fixture()
    monkeypatch.setattr(
        "tracejudge_hy3.phase3.annotations.load_phase3_materials",
        lambda **_kwargs: loaded,
    )
    packet = export_annotation_packet(**_kwargs(tmp_path))
    completed_path = tmp_path / "completed.jsonl"
    _completed_labels(packet.labels_template_path, completed_path)
    monkeypatch.setattr(
        "tracejudge_hy3.phase3.labels.load_paired_cohort",
        lambda **_kwargs: loaded.cohort,
    )
    kwargs = {
        "annotation_set_id": "phase3_labels_primary_v1",
        "packet_run_dir": packet.run_dir,
        "expected_packet_manifest_sha256": packet.manifest_sha256,
        "completed_labels_path": completed_path,
        "cohort_manifest_path": "overlay.json",
        "natural_manifest_path": "natural.json",
        "protocol_path": PROTOCOL,
        "guide_path": GUIDE,
        "output_dir": tmp_path / "frozen-labels",
    }
    preflight = preflight_annotation_labels_freeze(**kwargs)
    assert preflight.record_count == 2
    assert preflight.natural_trace_count == 1
    assert preflight.counterfactual_trace_count == 1
    assert not Path(kwargs["output_dir"]).exists()

    frozen = freeze_annotation_labels(**kwargs)
    assert stat.S_IMODE(frozen.run_dir.stat().st_mode) == 0o700
    for path in (
        frozen.manifest_path,
        frozen.completed_labels_path,
        frozen.annotation_records_path,
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    opaque_text = frozen.completed_labels_path.read_text(encoding="utf-8")
    records_text = frozen.annotation_records_path.read_text(encoding="utf-8")
    assert "natural:safe_mean" not in opaque_text
    assert "counterfactual:" not in opaque_text
    assert "annotation_item_id" not in records_text
    assert "natural:safe_mean" in records_text
    assert "counterfactual:safe_mean:boundary_deletion:v1" in records_text
    manifest = json.loads(frozen.manifest_path.read_text(encoding="utf-8"))
    assert manifest["source_packet_manifest_sha256"] == packet.manifest_sha256
    assert manifest["agreement_kind"] == "not_computed"
    assert manifest["contains_method_predictions"] is False

    with pytest.raises(Phase3AnnotationError, match="already exists"):
        freeze_annotation_labels(**kwargs)


def test_annotation_label_freeze_rejects_pending_before_identity_join(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.setattr(
        "tracejudge_hy3.phase3.annotations.load_phase3_materials",
        lambda **_kwargs: _loaded_fixture(),
    )
    packet = export_annotation_packet(**_kwargs(tmp_path))

    def identity_must_not_be_opened(*_args, **_kwargs):
        raise AssertionError("incomplete labels reached the identity join")

    monkeypatch.setattr(
        "tracejudge_hy3.phase3.labels._load_identities",
        identity_must_not_be_opened,
    )
    with pytest.raises(Phase3AnnotationError) as exc_info:
        preflight_annotation_labels_freeze(
            annotation_set_id="phase3_labels_primary_v1",
            packet_run_dir=packet.run_dir,
            expected_packet_manifest_sha256=packet.manifest_sha256,
            completed_labels_path=packet.labels_template_path,
            cohort_manifest_path="overlay.json",
            natural_manifest_path="natural.json",
            protocol_path=PROTOCOL,
            guide_path=GUIDE,
            output_dir=tmp_path / "frozen-labels",
        )
    assert exc_info.value.safe_stage == "P3E_LABELS_NOT_READY"


def test_cli_annotation_labels_check_reports_pending_without_identity_join(
    monkeypatch: pytest.MonkeyPatch,
):
    expected = AnnotationLabelCheckResult(
        packet_id="phase3_annotation_primary_round1_v1",
        expected_item_count=57,
        completed_count=0,
        pending_count=57,
        invalid_count=0,
        missing_item_count=0,
        extra_line_count=0,
        order_mismatch_count=0,
        invalid_line_numbers=(),
        working_labels_sha256=H0,
        packet_manifest_sha256=H1,
        ready_to_freeze=False,
    )
    monkeypatch.setattr(cli_module, "check_annotation_labels", lambda **_kwargs: expected)
    result = CliRunner().invoke(
        app,
        [
            "phase3",
            "annotation-labels-check",
            "--packet-run",
            "packet",
            "--packet-manifest-sha256",
            H1,
            "--labels",
            "working.jsonl",
        ],
    )
    assert result.exit_code == 0
    assert "0 / 57" in result.stdout
    assert "否 / 否" in result.stdout
