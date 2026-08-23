"""Pure metric functions over EvaluationRecord lists.

Every metric that needs human ground truth returns status="not_computable"
(never a fabricated number, and never the system's own judgement passed off
as ground truth) when no annotated records are available. data/demo_annotations.jsonl
provides a handful of ground-truth labels for the built-in mock fixtures purely
as an engineering fixture to exercise these functions in tests -- it is not a
real human-annotation study.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from tracejudge_hy3.schemas.evaluation import ErrorType, FaultyLayer, Verdict


class EvaluationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_id: str
    functional_correct: bool
    system_process_correct: bool | None = None
    system_has_error: bool
    system_verdict: Verdict | None = None
    system_error_type: ErrorType | None = None
    system_first_faulty_step: str | None = None
    system_first_faulty_layer: FaultyLayer | None = None
    executable_evidence: bool = False

    human_process_correct: bool | None = None
    human_has_error: bool | None = None
    human_error_type: ErrorType | None = None
    human_first_faulty_step: str | None = None
    human_first_faulty_layer: FaultyLayer | None = None


@dataclass
class MetricResult:
    name: str
    status: Literal["ok", "not_computable"]
    value: float | None
    n: int
    note: str | None = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "value": self.value,
            "n": self.n,
            "note": self.note,
        }


def _ok(name: str, value: float, n: int) -> MetricResult:
    return MetricResult(name=name, status="ok", value=value, n=n)


def _not_computable(name: str, note: str) -> MetricResult:
    return MetricResult(name=name, status="not_computable", value=None, n=0, note=note)


def functional_accuracy(records: list[EvaluationRecord]) -> MetricResult:
    if not records:
        return _not_computable("functional_accuracy", "no records")
    correct = sum(1 for r in records if r.functional_correct)
    return _ok("functional_accuracy", correct / len(records), len(records))


def process_correctness_rate(records: list[EvaluationRecord]) -> MetricResult:
    labeled = [r for r in records if r.human_process_correct is not None]
    if not labeled:
        return _not_computable(
            "process_correctness_rate", "no human-annotated human_process_correct labels"
        )
    correct = sum(1 for r in labeled if r.human_process_correct)
    return _ok("process_correctness_rate", correct / len(labeled), len(labeled))


def _error_detection_confusion(records: list[EvaluationRecord]) -> list[EvaluationRecord]:
    return [r for r in records if r.human_has_error is not None]


def error_detection_precision(records: list[EvaluationRecord]) -> MetricResult:
    labeled = _error_detection_confusion(records)
    if not labeled:
        return _not_computable(
            "error_detection_precision", "no human-annotated human_has_error labels"
        )
    predicted_positive = [r for r in labeled if r.system_has_error]
    if not predicted_positive:
        return _not_computable(
            "error_detection_precision", "system flagged zero errors among labeled records"
        )
    tp = sum(1 for r in predicted_positive if r.human_has_error)
    return _ok("error_detection_precision", tp / len(predicted_positive), len(predicted_positive))


def error_detection_recall(records: list[EvaluationRecord]) -> MetricResult:
    labeled = _error_detection_confusion(records)
    actual_positive = [r for r in labeled if r.human_has_error]
    if not actual_positive:
        return _not_computable(
            "error_detection_recall", "no human-annotated positive (error) records"
        )
    tp = sum(1 for r in actual_positive if r.system_has_error)
    return _ok("error_detection_recall", tp / len(actual_positive), len(actual_positive))


def error_detection_f1(records: list[EvaluationRecord]) -> MetricResult:
    precision = error_detection_precision(records)
    recall = error_detection_recall(records)
    if precision.status != "ok" or recall.status != "ok":
        return _not_computable("error_detection_f1", "precision or recall not computable")
    if precision.value == 0 and recall.value == 0:
        return _ok("error_detection_f1", 0.0, min(precision.n, recall.n))
    f1 = 2 * precision.value * recall.value / (precision.value + recall.value)  # type: ignore[operator]
    return _ok("error_detection_f1", f1, min(precision.n, recall.n))


def exact_localization_accuracy(records: list[EvaluationRecord]) -> MetricResult:
    labeled = [r for r in records if r.human_first_faulty_step is not None]
    if not labeled:
        return _not_computable(
            "exact_localization_accuracy", "no human-annotated human_first_faulty_step labels"
        )
    matches = sum(1 for r in labeled if r.system_first_faulty_step == r.human_first_faulty_step)
    return _ok("exact_localization_accuracy", matches / len(labeled), len(labeled))


def layer_localization_accuracy(records: list[EvaluationRecord]) -> MetricResult:
    labeled = [r for r in records if r.human_first_faulty_layer is not None]
    if not labeled:
        return _not_computable(
            "layer_localization_accuracy", "no human-annotated human_first_faulty_layer labels"
        )
    matches = sum(1 for r in labeled if r.system_first_faulty_layer == r.human_first_faulty_layer)
    return _ok("layer_localization_accuracy", matches / len(labeled), len(labeled))


def false_positive_rate(records: list[EvaluationRecord]) -> MetricResult:
    negatives = [r for r in records if r.human_has_error is False]
    if not negatives:
        return _not_computable(
            "false_positive_rate", "no human-annotated negative (no-error) records"
        )
    false_positives = sum(1 for r in negatives if r.system_has_error)
    return _ok("false_positive_rate", false_positives / len(negatives), len(negatives))


def correct_result_wrong_process_recall(records: list[EvaluationRecord]) -> MetricResult:
    target = [r for r in records if r.functional_correct and r.human_process_correct is False]
    if not target:
        return _not_computable(
            "correct_result_wrong_process_recall",
            "no human-annotated 'functional correct but process incorrect' records",
        )
    detected = sum(1 for r in target if r.system_process_correct is False)
    return _ok("correct_result_wrong_process_recall", detected / len(target), len(target))


def executable_evidence_rate(records: list[EvaluationRecord]) -> MetricResult:
    confirmed = [r for r in records if r.system_verdict == "confirmed_bug"]
    if not confirmed:
        return _not_computable("executable_evidence_rate", "no confirmed_bug records")
    with_evidence = sum(1 for r in confirmed if r.executable_evidence)
    return _ok("executable_evidence_rate", with_evidence / len(confirmed), len(confirmed))


ALL_METRIC_FUNCTIONS = (
    functional_accuracy,
    process_correctness_rate,
    error_detection_precision,
    error_detection_recall,
    error_detection_f1,
    exact_localization_accuracy,
    layer_localization_accuracy,
    false_positive_rate,
    correct_result_wrong_process_recall,
    executable_evidence_rate,
)


def compute_all_metrics(records: list[EvaluationRecord]) -> dict[str, dict]:
    return {fn.__name__: fn(records).as_dict() for fn in ALL_METRIC_FUNCTIONS}
