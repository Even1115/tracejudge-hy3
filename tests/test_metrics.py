from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracejudge_hy3.dataset.loader import load_problem_by_id
from tracejudge_hy3.pipeline.runner import run_pipeline
from tracejudge_hy3.providers.mock import MockProvider
from tracejudge_hy3.reporting.metrics import (
    EvaluationRecord,
    compute_all_metrics,
    correct_result_wrong_process_recall,
    error_detection_f1,
    error_detection_precision,
    error_detection_recall,
    executable_evidence_rate,
    false_positive_rate,
    functional_accuracy,
    process_correctness_rate,
)
from tracejudge_hy3.reporting.serializer import evaluation_record_from_pipeline_result
from tracejudge_hy3.sandbox.trusted_local import TrustedLocalSandbox

REPO_ROOT = Path(__file__).resolve().parents[1]
DATASET = REPO_ROOT / "data" / "sample_problems.jsonl"
ANNOTATIONS = REPO_ROOT / "data" / "demo_annotations.jsonl"


def _load_annotations() -> dict[str, dict]:
    annotations = {}
    with ANNOTATIONS.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            annotations[payload["response_id"]] = {
                "process_correct": payload["human_process_correct"],
                "has_error": payload["human_has_error"],
                "error_type": payload["human_error_type"],
                "first_faulty_step": payload["human_first_faulty_step"],
                "first_faulty_layer": payload["human_first_faulty_layer"],
            }
    return annotations


async def _build_records() -> list[EvaluationRecord]:
    annotations = _load_annotations()
    backend = TrustedLocalSandbox(per_test_timeout_seconds=2.0)
    records = []

    safe_mean = load_problem_by_id(DATASET, "safe_mean")
    faulty_result = await run_pipeline(safe_mean, MockProvider(case="faulty"), backend)
    records.append(
        evaluation_record_from_pipeline_result(faulty_result, annotations["safe_mean_faulty_demo"])
    )

    correct_result = await run_pipeline(safe_mean, MockProvider(case="correct"), backend)
    records.append(
        evaluation_record_from_pipeline_result(
            correct_result, annotations["safe_mean_correct_demo"]
        )
    )

    dedupe = load_problem_by_id(DATASET, "deduplicate_preserve_order")
    dedupe_result = await run_pipeline(dedupe, MockProvider(), backend)
    records.append(
        evaluation_record_from_pipeline_result(
            dedupe_result, annotations["deduplicate_preserve_order_demo"]
        )
    )

    clamp = load_problem_by_id(DATASET, "clamp")
    clamp_result = await run_pipeline(clamp, MockProvider(), backend)
    records.append(evaluation_record_from_pipeline_result(clamp_result, annotations["clamp_demo"]))

    return records


@pytest.fixture
async def records() -> list[EvaluationRecord]:
    return await _build_records()


def test_functional_accuracy_no_ground_truth_needed(records):
    result = functional_accuracy(records)
    assert result.status == "ok"
    # The faulty safe_mean fixture deliberately fails hidden/challenge tests;
    # the other three fixture responses are functionally correct.
    assert result.value == pytest.approx(3 / 4)


def test_process_correctness_rate_uses_human_labels(records):
    result = process_correctness_rate(records)
    assert result.status == "ok"
    assert result.value == pytest.approx(3 / 4)


def test_error_detection_precision_recall_f1_perfect(records):
    precision = error_detection_precision(records)
    recall = error_detection_recall(records)
    f1 = error_detection_f1(records)
    assert precision.status == "ok" and precision.value == pytest.approx(1.0)
    assert recall.status == "ok" and recall.value == pytest.approx(1.0)
    assert f1.status == "ok" and f1.value == pytest.approx(1.0)


def test_false_positive_rate_zero_on_correct_samples(records):
    result = false_positive_rate(records)
    assert result.status == "ok"
    assert result.value == pytest.approx(0.0)


def test_correct_result_wrong_process_recall_not_computable_here(records):
    result = correct_result_wrong_process_recall(records)
    assert result.status == "not_computable"


def test_executable_evidence_rate(records):
    result = executable_evidence_rate(records)
    assert result.status == "ok"
    assert result.value == pytest.approx(1.0)


def test_metrics_return_not_computable_without_ground_truth():
    bare_records = [
        EvaluationRecord(
            response_id="r1",
            functional_correct=True,
            system_has_error=False,
        )
    ]
    result = process_correctness_rate(bare_records)
    assert result.status == "not_computable"
    result = error_detection_precision(bare_records)
    assert result.status == "not_computable"


def test_compute_all_metrics_returns_all_names(records):
    all_metrics = compute_all_metrics(records)
    assert "functional_accuracy" in all_metrics
    assert "executable_evidence_rate" in all_metrics
    assert len(all_metrics) == 10
