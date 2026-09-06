"""Allowed-metrics scoring for the CodeJudge-Eval judge-only experiment.

Label semantics (scoring layer v2):

- CodeJudge-Eval gold WA/RE/TLE are *execution outcomes*; the judge's
  ``error_type`` field is a *root-cause* taxonomy entry (R/P/A/C/E codes).
  The two spaces are never compared: execution-outcome agreement is derived
  from the judge's coarse ``verdict`` (WA→E03, RE→E01, TLE→E02) on
  gold-mapped samples only, while root-cause error types are reported as a
  descriptive distribution with no agreement rate.
- The judge cannot emit the gold-only label ``NOT_AC_UNSPECIFIED`` (easy
  file) nor the per-combination MIXED labels of the hard file, so no
  "native seven-class" claim is made.  Coarse verdict macro-F1 is reported
  twice, both with explicit ``zero_division=0`` semantics: on the full gold
  label space (diagnostic only — the judge structurally cannot predict
  ``NOT_AC_UNSPECIFIED``) and on the compatible subset of gold labels the
  judge can actually emit.  A class with gold support but zero predictions
  contributes F1 = 0 to the average instead of being skipped, and
  ``n_classes_averaged`` always equals ``len(classes)``.

Process correctness, first-step localization, and certificate replay metrics
remain absent: this dataset has no reasoning traces.
"""

from __future__ import annotations

import math
from typing import Any

from tracejudge_hy3.phase3.statistics import wilson_interval
from tracejudge_hy3.schemas.evaluation import ErrorType

from .codejudge_eval import CodeJudgeEvalError, CodeJudgeEvalSample
from .contracts import BenchmarkDifficulty, BenchmarkJudgeRecord, JudgeStatus

ALLOWED_METRICS: tuple[str, ...] = (
    "correctness-accuracy-full-denominator",
    "valid-only-functional-precision-recall-f1",
    "valid-only-sensitivity-specificity",
    "valid-only-balanced-accuracy",
    "valid-only-mcc",
    "majority-class-baseline-accuracy",
    "correct-code-false-positive-rate",
    "coarse-verdict-macro-f1-compatible-subset",
    "coarse-verdict-macro-f1-full-gold-space-diagnostic",
    "execution-outcome-agreement-on-mapped",
    "per-difficulty-breakdown",
    "per-source-error-type-breakdown",
    "provider-parse-failure-counts",
)

FORBIDDEN_METRICS: tuple[str, ...] = (
    "process-correctness",
    "first-step-localization",
    "certificate-replay-success",
)

# Coarse execution-outcome labels the judge's verdict space can express.
JUDGE_OUTPUT_LABELS: tuple[str, ...] = ("AC", "CE", "MIXED", "RE", "TLE", "WA")

# Execution-outcome derivation from the judge's coarse verdict.  This is the
# *only* bridge to the gold execution-outcome labels; the root-cause
# ``error_type`` field is never compared against gold.
JUDGE_VERDICT_EXECUTION_OUTCOME: dict[str, ErrorType] = {
    "WA": ErrorType.E03_WRONG_OUTPUT,
    "RE": ErrorType.E01_RUNTIME_EXCEPTION,
    "TLE": ErrorType.E02_TIMEOUT_OR_RESOURCE_ERROR,
}

_MAPPED_GOLD_OUTCOMES = frozenset(JUDGE_VERDICT_EXECUTION_OUTCOME.values())


def _proportion(successes: int, total: int) -> dict[str, Any]:
    lower, upper = wilson_interval(successes, total)
    return {
        "numerator": successes,
        "denominator": total,
        "estimate": successes / total if total else None,
        "wilson_95_lower": lower,
        "wilson_95_upper": upper,
    }


def _prf(tp: int, fp: int, fn: int) -> dict[str, Any]:
    """Binary precision/recall/F1; ``None`` when undefined (never macro-averaged)."""

    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall
        else None
    )
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}


def _mcc(tp: int, fp: int, fn: int, tn: int) -> float | None:
    """Matthews correlation coefficient; ``None`` when any factor is empty."""

    denominator = math.sqrt((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn))
    if denominator == 0:
        return None
    return (tp * tn - fp * fn) / denominator


def macro_f1_zero_division(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    """Macro-F1 over the union of gold/predicted labels, ``zero_division=0``.

    A class with gold support but zero predictions gets precision = F1 = 0
    and still counts in the average; ``n_classes_averaged`` always equals
    ``len(classes)`` so the reported class count and the actual average can
    never disagree.
    """

    classes = sorted({gold for gold, _ in pairs} | {pred for _, pred in pairs})
    per_class: dict[str, Any] = {}
    f1_values: list[float] = []
    for cls in classes:
        tp = sum(1 for gold, pred in pairs if gold == cls and pred == cls)
        fp = sum(1 for gold, pred in pairs if gold != cls and pred == cls)
        fn = sum(1 for gold, pred in pairs if gold == cls and pred != cls)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_class[cls] = {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "support": tp + fn,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }
        f1_values.append(f1)
    return {
        "zero_division": 0,
        "classes": classes,
        "n_classes_averaged": len(classes),
        "per_class": per_class,
        "macro_f1": sum(f1_values) / len(f1_values) if f1_values else None,
    }


def score_judge_only(
    records: tuple[BenchmarkJudgeRecord, ...] | list[BenchmarkJudgeRecord],
    samples: tuple[CodeJudgeEvalSample, ...] | list[CodeJudgeEvalSample],
) -> dict[str, Any]:
    """Score judge-only records against bound gold; failures stay in the denominator.

    Coverage is validated first: the record set must cover every selected
    sample exactly once — no missing, extra, or duplicate keys — so a partial
    or duplicated run can never be silently scored as if it were complete.
    """

    sample_keys = [(sample.task.identity, sample.candidate.code_sha256) for sample in samples]
    record_keys = [(record.task, record.candidate_sha256) for record in records]
    if len(record_keys) != len(set(record_keys)):
        raise CodeJudgeEvalError(
            "coverage violation: duplicate judge records for the same (task, candidate) key"
        )
    if len(record_keys) != len(sample_keys) or set(record_keys) != set(sample_keys):
        missing = set(sample_keys) - set(record_keys)
        raise CodeJudgeEvalError(
            f"coverage violation: {len(record_keys)} judge records vs "
            f"{len(sample_keys)} selected samples ({len(missing)} samples unjudged)"
        )

    sample_index = {
        (sample.task.identity, sample.candidate.code_sha256): sample for sample in samples
    }
    joined: list[tuple[BenchmarkJudgeRecord, CodeJudgeEvalSample]] = []
    for record in records:
        key = (record.task, record.candidate_sha256)
        sample = sample_index.get(key)
        if sample is None:
            raise CodeJudgeEvalError(
                "judge record does not bind any selected sample "
                f"(task {record.task.task_id}, candidate {record.candidate_sha256[:12]}…)"
            )
        joined.append((record, sample))

    status_counts: dict[str, int] = {status.value: 0 for status in JudgeStatus}
    for record, _ in joined:
        status_counts[record.status.value] += 1

    total = len(joined)
    correct_full = sum(
        1
        for record, sample in joined
        if record.status is JudgeStatus.VALID_JUDGMENT
        and record.functional_correct == sample.gold.functional_correct
    )
    valid = [
        (record, sample) for record, sample in joined if record.status is JudgeStatus.VALID_JUDGMENT
    ]

    # Binary functional correctness: positive class = "incorrect code".
    tp = fp = fn = tn = 0
    verdict_pairs: list[tuple[str, str]] = []
    for record, sample in valid:
        gold_correct = sample.gold.functional_correct
        judged_correct = bool(record.functional_correct)
        if gold_correct and judged_correct:
            tn += 1
        elif gold_correct and not judged_correct:
            fp += 1
        elif not gold_correct and not judged_correct:
            tp += 1
        else:
            fn += 1
        verdict_pairs.append((sample.gold.source_error_type, str(record.source_error_type)))

    gold_correct_valid = tn + fp
    sensitivity = _proportion(tp, tp + fn)
    specificity = _proportion(tn, gold_correct_valid)
    balanced = (
        (sensitivity["estimate"] + specificity["estimate"]) / 2
        if sensitivity["estimate"] is not None and specificity["estimate"] is not None
        else None
    )

    n_gold_correct = sum(1 for _, sample in joined if sample.gold.functional_correct)
    majority_correct = max(n_gold_correct, total - n_gold_correct)

    per_difficulty: dict[str, Any] = {}
    for difficulty in BenchmarkDifficulty:
        subset = [
            (record, sample) for record, sample in joined if sample.task.difficulty is difficulty
        ]
        if not subset:
            continue
        n = len(subset)
        n_correct = sum(
            1
            for record, sample in subset
            if record.status is JudgeStatus.VALID_JUDGMENT
            and record.functional_correct == sample.gold.functional_correct
        )
        n_gold_correct_d = sum(1 for _, sample in subset if sample.gold.functional_correct)
        n_valid_gold_correct = sum(
            1
            for record, sample in subset
            if record.status is JudgeStatus.VALID_JUDGMENT and sample.gold.functional_correct
        )
        n_fp = sum(
            1
            for record, sample in subset
            if record.status is JudgeStatus.VALID_JUDGMENT
            and sample.gold.functional_correct
            and record.functional_correct is False
        )
        per_difficulty[difficulty.value] = {
            "n": n,
            "accuracy_full_denominator": _proportion(n_correct, n),
            "valid_judgments": sum(
                1 for record, _ in subset if record.status is JudgeStatus.VALID_JUDGMENT
            ),
            "correct_code_fpr": _proportion(n_fp, n_valid_gold_correct),
            "gold_correct_n": n_gold_correct_d,
        }

    per_error_type: dict[str, Any] = {}
    error_types = sorted({sample.gold.source_error_type for _, sample in joined})
    for error_type in error_types:
        subset = [
            (record, sample)
            for record, sample in joined
            if sample.gold.source_error_type == error_type
        ]
        n = len(subset)
        n_correct = sum(
            1
            for record, sample in subset
            if record.status is JudgeStatus.VALID_JUDGMENT
            and record.functional_correct == sample.gold.functional_correct
        )
        per_error_type[error_type] = {
            "n": n,
            "functional_judgment_accuracy_full_denominator": _proportion(n_correct, n),
        }

    # Coarse verdict macro-F1: two explicitly-scoped views, zero_division=0.
    judge_labels = set(JUDGE_OUTPUT_LABELS)
    compatible_pairs = [(gold, pred) for gold, pred in verdict_pairs if gold in judge_labels]
    excluded_gold: dict[str, int] = {}
    for gold, _ in verdict_pairs:
        if gold not in judge_labels:
            excluded_gold[gold] = excluded_gold.get(gold, 0) + 1
    full_space = macro_f1_zero_division(verdict_pairs)
    full_space["diagnostic_only"] = True
    full_space["caveat"] = (
        "full gold label space includes labels the judge cannot emit "
        "(NOT_AC_UNSPECIFIED; per-combination MIXED folds of the hard file); "
        "this is a mechanical diagnostic, not a native-label capability metric"
    )
    compatible = macro_f1_zero_division(compatible_pairs)
    compatible["included_n"] = len(compatible_pairs)
    compatible["excluded_n"] = len(verdict_pairs) - len(compatible_pairs)
    compatible["excluded_gold_labels"] = dict(sorted(excluded_gold.items()))
    compatible["exclusion_reason"] = (
        "gold labels outside the judge output space are excluded; no label is "
        "ever force-mapped to a judge-emittable class"
    )

    # Execution-outcome agreement: derived from the judge verdict only.
    mapped_pairs = [
        (record, sample)
        for record, sample in valid
        if not sample.gold.functional_correct
        and sample.gold.normalized_error_type in _MAPPED_GOLD_OUTCOMES
    ]
    outcome_agree = sum(
        1
        for record, sample in mapped_pairs
        if JUDGE_VERDICT_EXECUTION_OUTCOME.get(str(record.source_error_type))
        == sample.gold.normalized_error_type
    )

    # Root-cause error types: descriptive distribution only, never an
    # agreement rate against execution-outcome gold.
    root_cause_distribution: dict[str, int] = {}
    root_cause_null = 0
    for record, _sample in valid:
        if record.functional_correct is not False:
            continue
        if record.normalized_error_type is None:
            root_cause_null += 1
        else:
            key = record.normalized_error_type.value
            root_cause_distribution[key] = root_cause_distribution.get(key, 0) + 1

    return {
        "n_samples": total,
        "status_counts": status_counts,
        "judgment_availability": _proportion(len(valid), total),
        "correctness_accuracy_full_denominator": _proportion(correct_full, total),
        "majority_class_baseline_accuracy": _proportion(majority_correct, total),
        "functional_correctness": {
            "n_valid": len(valid),
            "positive_class": "incorrect-code",
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "tn": tn,
            "precision_recall_f1": _prf(tp, fp, fn),
            "sensitivity": sensitivity,
            "specificity": specificity,
            "balanced_accuracy": {
                "estimate": balanced,
                "note": "mean of sensitivity and specificity; see component Wilson intervals",
            },
            "mcc": {"estimate": _mcc(tp, fp, fn, tn)},
            "correct_code_fpr": _proportion(fp, gold_correct_valid),
        },
        "coarse_verdict": {
            "label_semantics": (
                "coarse execution-outcome classes; the judge output space is "
                "AC/CE/WA/RE/TLE/MIXED and cannot express gold-only labels"
            ),
            "judge_output_labels": list(JUDGE_OUTPUT_LABELS),
            "macro_f1_full_gold_space": full_space,
            "macro_f1_compatible_subset": compatible,
        },
        "execution_outcome": {
            "semantics": (
                "execution-outcome agreement derived from the judge verdict "
                "(WA→E03, RE→E01, TLE→E02); computed on gold-mapped samples only"
            ),
            "judge_verdict_crosswalk": {
                verdict: outcome.value
                for verdict, outcome in sorted(JUDGE_VERDICT_EXECUTION_OUTCOME.items())
            },
            "agreement_on_mapped": _proportion(outcome_agree, len(mapped_pairs)),
        },
        "root_cause_error_type": {
            "semantics": (
                "root-cause taxonomy (R/P/A/C/E codes); descriptive only and never "
                "scored against execution-outcome gold labels"
            ),
            "distribution_on_valid_incorrect_judgments": dict(
                sorted(root_cause_distribution.items())
            ),
            "null_count": root_cause_null,
            "no_agreement_metric_computed": True,
        },
        "per_difficulty": per_difficulty,
        "per_source_error_type": per_error_type,
        "crosswalk": {
            "gold_unmapped_incorrect": sum(
                1
                for _, sample in joined
                if not sample.gold.functional_correct and sample.gold.crosswalk_unmapped
            ),
            "gold_mapped_incorrect": sum(
                1
                for _, sample in joined
                if not sample.gold.functional_correct
                and sample.gold.normalized_error_type is not None
            ),
        },
        "forbidden_metrics_not_computed": list(FORBIDDEN_METRICS),
    }
