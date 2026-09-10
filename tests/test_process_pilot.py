"""Offline contracts and source gates use synthetic, non-executable candidates."""

import importlib.util
import json
from pathlib import Path

import pytest

from tracejudge_hy3.process_eval_v2.contracts import (
    FunctionalEvidence,
    PilotInput,
    PilotLabel,
    Prediction,
    PublicTask,
)
from tracejudge_hy3.process_eval_v2.materials import canonical, digest, jsonl
from tracejudge_hy3.process_eval_v2.preflight import (
    PreparedPilot,
    preflight_report,
    prepare_pilot,
    read_json,
)
from tracejudge_hy3.process_eval_v2.scoring import score_predictions
from tracejudge_hy3.schemas.evaluation import ProcessAssessment
from tracejudge_hy3.schemas.location import FaultLocation
from tracejudge_hy3.schemas.solution import SolutionTrace


@pytest.fixture
def pilot():
    inputs, labels = {}, {}
    for index, verdict in enumerate((True, False, True, False, None, True)):
        key = f"item-{index}"
        solution = SolutionTrace(
            problem_id=key,
            requirement_understanding="Return one.",
            design_summary="Always return zero.",
            edge_cases_considered=["Empty input."],
            implementation_steps=[
                {"step_id": "S1", "content": "Return zero.", "related_requirements": ["R1"]}
            ],
            code="def f():\n    return 0\n",
        )
        inputs[key] = PilotInput(
            item_id=key,
            problem=PublicTask(
                title="f",
                requirement="Return one.",
                function_signature="f()",
                requirements=[{"requirement_id": "R1", "content": "Return one."}],
            ),
            solution_trace=solution,
            functional_evidence=FunctionalEvidence(
                source_run_id="test",
                source_record_sha256="a" * 64,
                candidate_code_sha256=digest(solution.code.encode()),
                infrastructure_status="ok",
                base_status="fail",
                plus_status="fail",
            ),
        )
        labels[key] = PilotLabel(
            item_id=key,
            annotation_status="reviewed",
            annotator="annotator_a",
            reasoning_correct=verdict,
            plan_code_aligned=True,
            process_correct=verdict,
            localization_status="supported"
            if verdict is False
            else "unknown"
            if verdict is None
            else "not_applicable",
            first_faulty_layer="reasoning" if verdict is False else None,
            first_faulty_step=None,
            error_type="P01_ALGORITHM_ERROR" if verdict is False else None,
            evidence=[
                {
                    "step_id": "S1",
                    "code_span": "L2",
                    "requirement_id": "R1",
                    "description": "Evidence.",
                }
            ]
            if verdict is False
            else [],
            rationale="PRIVATE_LABEL_CANARY",
        )
    return PreparedPilot(inputs, labels, {}, "post_feedback_development_consensus", "unverified")


def prediction(pilot, key, verdict, **kwargs):
    assessment = ProcessAssessment(
        reasoning_correct=verdict,
        plan_code_aligned=True,
        functional_correct=False,
        process_correct=False,
        explanation="Offline synthetic prediction.",
    )
    return Prediction(
        item_id=key,
        method="direct_judge",
        input_sha256=pilot.input_hash(key),
        status="ok",
        assessment=assessment,
        **kwargs,
    )


def test_score_denominators_and_legacy_joint_signal(pilot):
    rows = [
        prediction(pilot, "item-0", True),
        prediction(pilot, "item-1", False),
        Prediction(
            item_id="item-2",
            method="direct_judge",
            input_sha256=pilot.input_hash("item-2"),
            status="provider_error",
        ),
        prediction(pilot, "item-3", None),
        prediction(pilot, "item-4", False),
    ]
    scores = score_predictions(pilot, rows)["methods"]
    direct = scores["direct_judge"]
    assert direct["coverage"] == {"ok": 4, "missing": 1, "provider_error": 1, "parse_error": 0}
    result = direct["dimensions"]["public_process_correct"]
    assert result["accuracy_all_known"] == {"numerator": 2, "denominator": 5, "value": 0.4}
    assert (
        result["tn"] == result["tp"] == result["known_abstained"] == result["unknown_decided"] == 1
    )
    assert direct["localization"]["first_step"]["value"] is None
    assert direct["localization"]["structured_location_exact"]["denominator"] == 0
    missing = scores["full_system"]["dimensions"]["public_process_correct"]
    assert missing["accuracy_all_known"]["value"] == 0
    assert missing["false_positive_rate_decided"]["value"] is None


def test_predictions_reject_duplicate_stale_and_unknown_inputs(pilot):
    row = prediction(pilot, "item-0", True)
    for rows, message in (
        ([row, row], "duplicate"),
        ([row.model_copy(update={"input_sha256": "b" * 64})], "SHA-256"),
        ([row.model_copy(update={"item_id": "not-pilot"})], "unexpected"),
    ):
        with pytest.raises(ValueError, match=message):
            score_predictions(pilot, rows)


def test_location_binding_and_legacy_compatibility(pilot):
    solution = pilot.inputs["item-0"].solution_trace
    FaultLocation(source_field="design_summary", quote="return zero").validate_against(solution)
    FaultLocation(
        source_field="implementation_steps", step_id="S1", quote="Return zero."
    ).validate_against(solution)
    FaultLocation(
        source_field="edge_cases_considered", entry_index=0, quote="Empty"
    ).validate_against(solution)
    FaultLocation(source_field="code", code_span="L2", quote="return 0").validate_against(solution)
    for value in (
        {"source_field": "design_summary", "quote": "invented"},
        {"source_field": "implementation_steps", "step_id": "S2", "quote": "Return zero."},
        {"source_field": "edge_cases_considered", "entry_index": 1, "quote": "Empty"},
        {"source_field": "code", "code_span": "L1", "quote": "return 0"},
        {"source_field": "code", "code_span": "L2-L1", "quote": "return 0"},
        {"source_field": "design_summary", "step_id": "S1", "quote": "zero"},
    ):
        with pytest.raises(ValueError):
            FaultLocation.model_validate(value).validate_against(solution)
    legacy = ProcessAssessment(functional_correct=None, explanation="Legacy.")
    assert legacy.first_faulty_location is None
    with pytest.raises(ValueError, match="conflicts"):
        ProcessAssessment(
            functional_correct=False,
            explanation="Invalid.",
            first_faulty_step="S1",
            first_faulty_layer="reasoning",
            error_type="P01_ALGORITHM_ERROR",
            first_faulty_location=FaultLocation(source_field="design_summary", quote="zero"),
        )


def test_no_null_location_success_and_exact_bound_location(pilot):
    key = "item-1"
    location = FaultLocation(source_field="design_summary", quote="Always return zero.")
    pilot.labels[key] = pilot.labels[key].model_copy(update={"first_faulty_location": location})
    row = prediction(pilot, key, False)
    row.assessment.first_faulty_layer = "reasoning"
    row.assessment.error_type = "P01_ALGORITHM_ERROR"
    row.assessment.first_faulty_location = location
    scores = score_predictions(pilot, [row])["methods"]["direct_judge"]["localization"]
    assert scores["structured_location_exact"]["value"] == 1
    assert scores["first_layer"]["value"] == 0.5
    row.assessment.first_faulty_location = location.model_copy(update={"quote": "invented"})
    with pytest.raises(ValueError, match="absent"):
        score_predictions(pilot, [row])


@pytest.fixture
def packet(tmp_path, pilot):
    def write(name, raw):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return {"path": name, "sha256": digest(raw)}

    materials, origins, results = [], [], []
    for i, (key, item) in enumerate(pilot.inputs.items()):
        material = {
            "item_id": key,
            "problem": item.problem.model_dump(),
            "solution_trace": item.solution_trace.model_dump(exclude={"problem_id"}),
        }
        material["problem"]["title"] = f"Mbpp/{i}: f"
        result = {
            "problem_id": f"Mbpp/{i}",
            "run_id": "test",
            "solution_sha256": item.functional_evidence.candidate_code_sha256,
            "source_candidate": {
                "task_id": f"Mbpp/{i}",
                "code_sha256": item.functional_evidence.candidate_code_sha256,
            },
            "infrastructure_status": "ok",
            "base_status": "fail",
            "plus_status": "fail",
            "passed_base": False,
            "passed_plus": False,
        }
        origins.append(
            {
                "item_id": key,
                "parent_task_id": f"Mbpp/{i}",
                "run_id": "test",
                "split": "development",
                "packet_item_sha256": digest(canonical(material)),
                "execution_record_sha256": digest(canonical(result)),
                "code_sha256": item.functional_evidence.candidate_code_sha256,
            }
        )
        materials.append(material)
        results.append(result)
    execution = write("execution.jsonl", jsonl(results))
    registry = write("registry.json", canonical({"sources": {"mbpp.execution.results": execution}}))
    files = {
        "ANNOTATION_GUIDE.md": b"guide",
        "pilot/annotator_a/items.jsonl": jsonl(materials),
        "pilot/annotator_b/items.jsonl": jsonl(materials),
        "coordinator/identity_and_outcomes.jsonl": jsonl(origins),
    }
    hashes = {name: write("packet/" + name, raw)["sha256"] for name, raw in files.items()}
    manifest = write(
        "packet/manifest.json",
        canonical(
            {
                "schema": "mbpp-process-materials-v1",
                "files_sha256": hashes,
                "source_verification": {"registry_sha256": registry["sha256"]},
                "source_hashes": {"mbpp.execution.results": execution["sha256"]},
                "source_run_id": "test",
            }
        ),
    )
    annotations = {
        person: write(
            f"{person}.jsonl",
            jsonl(
                [
                    label.model_dump(mode="json") | {"annotator": f"annotator_{person}"}
                    for label in pilot.labels.values()
                ]
            ),
        )
        for person in ("a", "b")
    }
    audit = write(
        "audit.json",
        canonical(
            {
                "packet_manifest_sha256": manifest["sha256"],
                "structural_errors": [],
                "input_submissions": annotations,
            }
        ),
    )
    write(
        "config.json",
        canonical(
            {
                "schema": "tracejudge-process-pilot-config-v1",
                "label_stage": pilot.label_stage,
                "annotation_provenance": pilot.annotation_provenance,
                "packet_manifest": manifest,
                "review_audit": audit,
                "annotations": annotations,
                "benchmark_registry": "registry.json",
            }
        ),
    )
    return tmp_path


def test_preflight_sources_and_no_private_label_in_judge_view(packet):
    prepared = prepare_pilot(packet, Path("config.json"))
    report = preflight_report(prepared)
    assert report["item_count"] == 6
    assert report["calls_made"] == {"solver": 0, "judge": 0, "candidate_executions": 0}
    view = json.dumps([x.model_dump() for x in prepared.inputs.values()])
    assert "PRIVATE_LABEL_CANARY" not in view
    assert "Mbpp/" not in view
    assert "test_cases" not in view
    with pytest.raises(ValueError, match="request plan"):
        preflight_report(prepared, max_requests=1)
    (packet / "a.jsonl").write_bytes(b"{}\n")
    with pytest.raises(ValueError, match="SHA-256"):
        prepare_pilot(packet, Path("config.json"))


@pytest.mark.parametrize(
    "name", ["registry.json", "execution.jsonl", "packet/pilot/annotator_a/items.jsonl"]
)
def test_tampered_source_rejected(packet, name):
    path = packet / name
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="SHA-256|registry"):
        prepare_pilot(packet, Path("config.json"))


def test_json_duplicate_keys_rejected():
    with pytest.raises(ValueError, match="duplicate JSON key"):
        read_json(b'{"status":true,"status":false}')


def test_cli_offline_and_output_protection(packet, monkeypatch, capsys):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("network must not run")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    script = Path(__file__).resolve().parents[1] / "scripts/run_process_pilot.py"
    spec = importlib.util.spec_from_file_location("pilot_cli_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args = ["--project-root", str(packet), "--config", "config.json"]
    assert module.main(args) == 0
    assert json.loads(capsys.readouterr().out)["ready_for_live_run"] is True
    with pytest.raises(SystemExit):
        module.main(args + ["--execute"])
    assert module.main(args + ["--output", "packet"]) == 2
    capsys.readouterr()
    (packet / "empty.jsonl").write_bytes(b"")
    assert module.main(args + ["--phase", "score", "--predictions", "empty.jsonl"]) == 0
    scores = json.loads(capsys.readouterr().out)
    assert scores["methods"]["direct_judge"]["coverage"]["missing"] == 6
    destination = "artifacts/experiments/process-pilot/test"
    assert module.main(args + ["--output", destination]) == 0
    before = (packet / destination / "manifest.json").read_bytes()
    assert module.main(args + ["--output", destination]) == 2
    assert (packet / destination / "manifest.json").read_bytes() == before
    view = (packet / destination / "judgment-inputs.jsonl").read_text(encoding="utf-8")
    assert "PRIVATE_LABEL_CANARY" not in view
