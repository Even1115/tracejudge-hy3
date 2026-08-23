"""Convert a PipelineResult into JSON-serializable dicts, and persist them to disk."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tracejudge_hy3.pipeline.runner import PipelineResult
from tracejudge_hy3.reporting.metrics import EvaluationRecord


def pipeline_result_to_dict(result: PipelineResult) -> dict[str, Any]:
    return {
        "problem": result.problem.model_dump(mode="json"),
        "solution": result.solution.model_dump(mode="json"),
        "static_evidence": result.static_evidence.model_dump(mode="json"),
        "execution_result": result.execution_result.model_dump(mode="json"),
        "llm_assessment": result.llm_assessment.model_dump(mode="json")
        if result.llm_assessment
        else None,
        "process_assessment": result.process_assessment.model_dump(mode="json"),
        "counterexample": result.counterexample.model_dump(mode="json")
        if result.counterexample
        else None,
        "error_certificate": result.error_certificate.model_dump(mode="json")
        if result.error_certificate
        else None,
    }


def save_result_json(result: PipelineResult, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = pipeline_result_to_dict(result)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def timestamped_artifact_path(artifact_dir: str | Path, prefix: str) -> Path:
    artifact_dir = Path(artifact_dir)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return artifact_dir / f"{prefix}_{timestamp}.json"


def evaluation_record_from_pipeline_result(
    result: PipelineResult,
    human_annotation: dict[str, Any] | None = None,
) -> EvaluationRecord:
    cert = result.error_certificate
    assessment = result.process_assessment
    human_annotation = human_annotation or {}

    return EvaluationRecord(
        response_id=result.problem.problem_id,
        functional_correct=assessment.functional_correct,
        system_process_correct=assessment.process_correct,
        system_has_error=bool(cert and cert.verdict != "cleared"),
        system_verdict=cert.verdict if cert else None,
        system_error_type=assessment.error_type,
        system_first_faulty_step=assessment.first_faulty_step,
        system_first_faulty_layer=assessment.first_faulty_layer,
        executable_evidence=bool(
            cert and cert.verdict == "confirmed_bug" and cert.counterexample is not None
        ),
        human_process_correct=human_annotation.get("process_correct"),
        human_has_error=human_annotation.get("has_error"),
        human_error_type=human_annotation.get("error_type"),
        human_first_faulty_step=human_annotation.get("first_faulty_step"),
        human_first_faulty_layer=human_annotation.get("first_faulty_layer"),
    )
