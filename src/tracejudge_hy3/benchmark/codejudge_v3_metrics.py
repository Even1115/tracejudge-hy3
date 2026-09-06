"""Pre-registered aggregate scorers for CodeJudge-Eval v3-A and v3-B."""

from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Sequence
from typing import Any

from .codejudge_eval import CodeJudgeEvalError, CodeJudgeEvalSample
from .codejudge_v3_execution import CodeJudgeV3Record, PlannedV3Call
from .contracts import JudgeStatus

A_BOOTSTRAP_SEED = 20260907
B_BOOTSTRAP_SEED = 20260908
BOOTSTRAP_RESAMPLES = 10_000
B_FAILURE_RATE_LIMIT = 0.05

_PAIRWISE_CONDITIONS = (
    ("easy", "middle"),
    ("easy", "hard"),
    ("middle", "hard"),
)
_HARD_GOLD_OUTCOMES: dict[str, tuple[str, ...] | None] = {
    "A": (),
    "B": None,
    "C": ("WA",),
    "D": ("RE",),
    "E": ("WA", "RE"),
    "F": ("TLE",),
    "G": ("WA", "TLE"),
    "H": ("RE", "TLE"),
    "I": ("WA", "RE", "TLE"),
}


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _wilson(numerator: int, denominator: int, z: float = 1.959963984540054) -> list[float] | None:
    if denominator == 0:
        return None
    p = numerator / denominator
    z2 = z * z
    scale = 1.0 + z2 / denominator
    center = (p + z2 / (2.0 * denominator)) / scale
    half = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * denominator)) / denominator) / scale
    return [max(0.0, center - half), min(1.0, center + half)]


def _proportion(numerator: int, denominator: int) -> dict[str, object]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": _ratio(numerator, denominator),
        "wilson_95": _wilson(numerator, denominator),
    }


def exact_mcnemar_p_value(b: int, c: int) -> float:
    """Two-sided exact conditional binomial McNemar p-value."""

    if b < 0 or c < 0:
        raise ValueError("McNemar discordant counts must be non-negative")
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(min(b, c) + 1)) / (2**n)
    return min(1.0, 2.0 * tail)


def exact_mcnemar_power(
    *,
    candidate_n: int,
    discordance_rate: float,
    directional_probability: float,
    alpha: float = 0.05,
) -> float:
    """Unconditional power of the two-sided exact McNemar test.

    ``directional_probability`` is P(first condition correct, second wrong |
    discordant).  The function integrates over the binomially distributed
    number of discordant candidate pairs.
    """

    if candidate_n < 1:
        raise ValueError("candidate_n must be positive")
    if not 0.0 <= discordance_rate <= 1.0:
        raise ValueError("discordance_rate must lie within [0, 1]")
    if not 0.0 <= directional_probability <= 1.0:
        raise ValueError("directional_probability must lie within [0, 1]")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must lie within (0, 1)")
    power = 0.0
    for discordant_n in range(candidate_n + 1):
        discordant_mass = (
            math.comb(candidate_n, discordant_n)
            * discordance_rate**discordant_n
            * (1.0 - discordance_rate) ** (candidate_n - discordant_n)
        )
        rejection_probability = sum(
            math.comb(discordant_n, b)
            * directional_probability**b
            * (1.0 - directional_probability) ** (discordant_n - b)
            for b in range(discordant_n + 1)
            if exact_mcnemar_p_value(b, discordant_n - b) <= alpha
        )
        power += discordant_mass * rejection_probability
    return power


def holm_adjust(p_values: Sequence[float]) -> tuple[float, ...]:
    """Holm step-down adjusted p-values in original order."""

    if any(not 0.0 <= value <= 1.0 for value in p_values):
        raise ValueError("p-values must lie within [0, 1]")
    ranked = sorted(enumerate(p_values), key=lambda item: item[1])
    adjusted = [0.0] * len(p_values)
    running = 0.0
    total = len(p_values)
    for rank, (original_index, value) in enumerate(ranked):
        running = max(running, min(1.0, (total - rank) * value))
        adjusted[original_index] = running
    return tuple(adjusted)


def _percentile(sorted_values: list[float], probability: float) -> float:
    if not sorted_values:
        raise ValueError("percentile requires at least one value")
    position = probability * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def _paired_bootstrap_ci(
    differences: Sequence[int], *, seed: int, resamples: int = BOOTSTRAP_RESAMPLES
) -> list[float] | None:
    if not differences:
        return None
    rng = random.Random(seed)
    n = len(differences)
    estimates = [sum(differences[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples)]
    estimates.sort()
    return [_percentile(estimates, 0.025), _percentile(estimates, 0.975)]


def _balanced_bootstrap_ci(
    positive_correct: Sequence[int],
    negative_correct: Sequence[int],
    *,
    seed: int = A_BOOTSTRAP_SEED,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> list[float]:
    if not positive_correct or not negative_correct:
        raise CodeJudgeEvalError("balanced bootstrap requires both gold classes")
    rng = random.Random(seed)
    n_pos = len(positive_correct)
    n_neg = len(negative_correct)
    estimates: list[float] = []
    for _ in range(resamples):
        sensitivity = sum(positive_correct[rng.randrange(n_pos)] for _ in range(n_pos)) / n_pos
        specificity = sum(negative_correct[rng.randrange(n_neg)] for _ in range(n_neg)) / n_neg
        estimates.append((sensitivity + specificity) / 2.0)
    estimates.sort()
    return [_percentile(estimates, 0.025), _percentile(estimates, 0.975)]


def _validate_coverage(
    records: Sequence[CodeJudgeV3Record],
    plan: tuple[tuple[PlannedV3Call, CodeJudgeEvalSample], ...],
) -> list[tuple[CodeJudgeV3Record, PlannedV3Call, CodeJudgeEvalSample]]:
    expected = {call.key: (call, sample) for call, sample in plan}
    record_keys = [record.key for record in records]
    if len(record_keys) != len(set(record_keys)):
        raise CodeJudgeEvalError("coverage violation: duplicate v3 condition records")
    if len(record_keys) != len(expected) or set(record_keys) != set(expected):
        missing = set(expected) - set(record_keys)
        extra = set(record_keys) - set(expected)
        raise CodeJudgeEvalError(
            "coverage violation: "
            f"{len(records)} records vs {len(expected)} planned calls; "
            f"missing={len(missing)}, extra={len(extra)}"
        )
    joined: list[tuple[CodeJudgeV3Record, PlannedV3Call, CodeJudgeEvalSample]] = []
    for call, sample in plan:
        record = next(record for record in records if record.key == call.key)
        if (
            record.planned_call_index != call.planned_call_index
            or record.task != call.task
            or record.candidate_id != call.candidate_id
            or record.candidate_sha256 != call.candidate_sha256
            or record.call_position != call.call_position
        ):
            raise CodeJudgeEvalError("coverage violation: v3 record binding mismatch")
        joined.append((record, call, sample))
    return joined


def _status_counts(records: Sequence[CodeJudgeV3Record]) -> dict[str, int]:
    counts = Counter(record.status.value for record in records)
    return {status.value: counts.get(status.value, 0) for status in JudgeStatus}


def score_v3_a(
    records: Sequence[CodeJudgeV3Record],
    plan: tuple[tuple[PlannedV3Call, CodeJudgeEvalSample], ...],
) -> dict[str, Any]:
    """Score balanced functional correctness; failures penalize their gold stratum."""

    joined = _validate_coverage(records, plan)
    if len(joined) != 120 or any(call.condition != "hard" for _, call, _ in joined):
        raise CodeJudgeEvalError("v3-A scorer requires the complete 120-call hard plan")

    gold_incorrect = [
        (record, sample) for record, _, sample in joined if not sample.gold.functional_correct
    ]
    gold_correct = [
        (record, sample) for record, _, sample in joined if sample.gold.functional_correct
    ]
    if len(gold_incorrect) != 60 or len(gold_correct) != 60:
        raise CodeJudgeEvalError("v3-A gold balance must be exactly 60 incorrect / 60 correct")

    def correct(record: CodeJudgeV3Record, sample: CodeJudgeEvalSample) -> bool:
        return (
            record.status is JudgeStatus.VALID_JUDGMENT
            and record.functional_correct == sample.gold.functional_correct
        )

    tp = sum(
        record.status is JudgeStatus.VALID_JUDGMENT and record.functional_correct is False
        for record, _ in gold_incorrect
    )
    tn = sum(
        record.status is JudgeStatus.VALID_JUDGMENT and record.functional_correct is True
        for record, _ in gold_correct
    )
    fn = sum(
        record.status is JudgeStatus.VALID_JUDGMENT and record.functional_correct is True
        for record, _ in gold_incorrect
    )
    fp = sum(
        record.status is JudgeStatus.VALID_JUDGMENT and record.functional_correct is False
        for record, _ in gold_correct
    )
    failures_incorrect = len(gold_incorrect) - tp - fn
    failures_correct = len(gold_correct) - tn - fp
    sensitivity = tp / len(gold_incorrect)
    specificity = tn / len(gold_correct)
    balanced = (sensitivity + specificity) / 2.0
    all_correct = sum(correct(record, sample) for record, _, sample in joined)

    valid_n = tp + tn + fp + fn
    precision = _ratio(tp, tp + fp)
    valid_recall = _ratio(tp, tp + fn)
    f1 = (
        2.0 * precision * valid_recall / (precision + valid_recall)
        if precision is not None and valid_recall is not None and precision + valid_recall > 0
        else None
    )
    mcc_denominator = math.sqrt((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn))
    mcc = (tp * tn - fp * fn) / mcc_denominator if mcc_denominator else None

    positive_correct = [int(correct(record, sample)) for record, sample in gold_incorrect]
    negative_correct = [int(correct(record, sample)) for record, sample in gold_correct]
    per_stratum: dict[str, dict[str, object]] = {}
    strata = sorted({sample.gold.source_error_type for _, _, sample in joined})
    for stratum in strata:
        subset = [
            (record, sample)
            for record, _, sample in joined
            if sample.gold.source_error_type == stratum
        ]
        per_stratum[stratum] = _proportion(
            sum(correct(record, sample) for record, sample in subset), len(subset)
        )

    return {
        "schema": "codejudge-eval-v3a-report-v1",
        "experiment_id": "codejudge-eval-v3a-functional",
        "analysis_population": {
            "planned_n": len(joined),
            "gold_incorrect_n": len(gold_incorrect),
            "gold_correct_n": len(gold_correct),
            "status_counts": _status_counts(records),
            "valid_n": valid_n,
        },
        "primary": {
            "balanced_accuracy_full_denominator": balanced,
            "stratified_candidate_bootstrap_95": _balanced_bootstrap_ci(
                positive_correct, negative_correct
            ),
            "bootstrap_seed": A_BOOTSTRAP_SEED,
            "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
        },
        "binary_metrics": {
            "accuracy_full_denominator": _proportion(all_correct, len(joined)),
            "sensitivity_full_gold_incorrect_denominator": _proportion(tp, len(gold_incorrect)),
            "specificity_full_gold_correct_denominator": _proportion(tn, len(gold_correct)),
            "fpr_full_gold_correct_denominator": _proportion(fp, len(gold_correct)),
            "fnr_full_gold_incorrect_denominator": _proportion(fn, len(gold_incorrect)),
            "precision_valid_predictions": precision,
            "recall_valid_predictions": valid_recall,
            "f1_valid_predictions": f1,
            "mcc_valid_predictions": mcc,
            "valid_confusion": {"tp": tp, "tn": tn, "fp": fp, "fn": fn},
            "failures_by_gold_class": {
                "gold_incorrect": failures_incorrect,
                "gold_correct": failures_correct,
            },
        },
        "majority_baseline": {
            "accuracy": 0.5,
            "balanced_accuracy": 0.5,
            "note": "the frozen gold classes are exactly balanced 60/60",
        },
        "per_gold_source_error_type": per_stratum,
        "failure_policy": (
            "provider/parse failures remain in each gold-class denominator and count as "
            "incorrect for sensitivity, specificity, balanced accuracy and overall accuracy; "
            "precision, recall, F1 and MCC are explicitly valid-prediction diagnostics"
        ),
    }


def score_v3_b(
    records: Sequence[CodeJudgeV3Record],
    plan: tuple[tuple[PlannedV3Call, CodeJudgeEvalSample], ...],
) -> dict[str, Any]:
    """Score the 50-candidate, three-condition paired granularity experiment."""

    joined = _validate_coverage(records, plan)
    if len(joined) != 150:
        raise CodeJudgeEvalError("v3-B scorer requires the complete 150-call plan")
    by_key: dict[tuple[int, int], dict[str, tuple[CodeJudgeV3Record, CodeJudgeEvalSample]]] = {}
    for record, _, sample in joined:
        by_key.setdefault((record.raw_task_id, record.data_id), {})[record.condition] = (
            record,
            sample,
        )
    if len(by_key) != 50 or any(set(row) != {"easy", "middle", "hard"} for row in by_key.values()):
        raise CodeJudgeEvalError("v3-B coverage must be exactly 50 complete condition triplets")

    per_condition: dict[str, Any] = {}
    failure_rates: dict[str, float] = {}
    for condition in ("easy", "middle", "hard"):
        rows = [row[condition] for row in by_key.values()]
        failures = sum(record.status is not JudgeStatus.VALID_JUDGMENT for record, _ in rows)
        failure_rates[condition] = failures / len(rows)
        functional_correct = sum(
            record.status is JudgeStatus.VALID_JUDGMENT
            and record.functional_correct == sample.gold.functional_correct
            for record, sample in rows
        )
        label_correct = sum(
            record.status is JudgeStatus.VALID_JUDGMENT
            and record.derived_label == sample.gold.answer_letter
            for record, sample in rows
        )
        per_condition[condition] = {
            "planned_n": len(rows),
            "status_counts": _status_counts([record for record, _ in rows]),
            "failure_rate": failures / len(rows),
            "functional_accuracy_full_denominator": _proportion(functional_correct, len(rows)),
            "native_label_exact_match_full_denominator": _proportion(label_correct, len(rows)),
        }

    pair_rows: list[dict[str, Any]] = []
    raw_for_holm: list[float] = []
    for pair_index, (first, second) in enumerate(_PAIRWISE_CONDITIONS):
        paired: list[tuple[bool, bool, CodeJudgeV3Record, CodeJudgeV3Record]] = []
        for row in by_key.values():
            first_record, first_sample = row[first]
            second_record, second_sample = row[second]
            if (
                first_record.status is JudgeStatus.VALID_JUDGMENT
                and second_record.status is JudgeStatus.VALID_JUDGMENT
            ):
                paired.append(
                    (
                        first_record.functional_correct == first_sample.gold.functional_correct,
                        second_record.functional_correct == second_sample.gold.functional_correct,
                        first_record,
                        second_record,
                    )
                )
        b = sum(first_ok and not second_ok for first_ok, second_ok, _, _ in paired)
        c = sum(not first_ok and second_ok for first_ok, second_ok, _, _ in paired)
        raw_p = exact_mcnemar_p_value(b, c)
        available = (
            failure_rates[first] <= B_FAILURE_RATE_LIMIT
            and failure_rates[second] <= B_FAILURE_RATE_LIMIT
        )
        raw_for_holm.append(raw_p if available else 1.0)
        differences = [int(first_ok) - int(second_ok) for first_ok, second_ok, _, _ in paired]
        label_differences = [
            int(
                first_record.derived_label
                == by_key[(first_record.raw_task_id, first_record.data_id)][first][
                    1
                ].gold.answer_letter
            )
            - int(
                second_record.derived_label
                == by_key[(second_record.raw_task_id, second_record.data_id)][second][
                    1
                ].gold.answer_letter
            )
            for _, _, first_record, second_record in paired
        ]
        outcome_comparable = [
            (first_record, second_record)
            for _, _, first_record, second_record in paired
            if first_record.normalized_outcomes is not None
            and second_record.normalized_outcomes is not None
        ]
        pair_rows.append(
            {
                "first": first,
                "second": second,
                "available": available,
                "planned_pairs": 50,
                "complete_pairs": len(paired),
                "incomplete_pairs": 50 - len(paired),
                "b_first_correct_second_incorrect": b,
                "c_first_incorrect_second_correct": c,
                "functional_accuracy_difference_first_minus_second": _ratio(
                    sum(differences), len(differences)
                ),
                "candidate_bootstrap_difference_95": _paired_bootstrap_ci(
                    differences, seed=B_BOOTSTRAP_SEED + pair_index
                ),
                "native_label_exact_match_difference_first_minus_second": _ratio(
                    sum(label_differences), len(label_differences)
                ),
                "native_label_candidate_bootstrap_difference_95": _paired_bootstrap_ci(
                    label_differences, seed=B_BOOTSTRAP_SEED + 100 + pair_index
                ),
                "exact_two_sided_mcnemar_p_value": raw_p if available else None,
                "normalized_functional_output_agreement": _proportion(
                    sum(
                        first_record.functional_correct == second_record.functional_correct
                        for _, _, first_record, second_record in paired
                    ),
                    len(paired),
                ),
                "normalized_ce_agreement": _proportion(
                    sum(
                        first_record.normalized_ce == second_record.normalized_ce
                        for _, _, first_record, second_record in paired
                    ),
                    len(paired),
                ),
                "normalized_outcome_agreement_when_both_expressible": _proportion(
                    sum(
                        first_record.normalized_outcomes == second_record.normalized_outcomes
                        for first_record, second_record in outcome_comparable
                    ),
                    len(outcome_comparable),
                ),
                "unavailable_reason": (
                    None
                    if available
                    else "at least one condition has provider/parse failure rate > 5%"
                ),
            }
        )

    adjusted = holm_adjust(raw_for_holm)
    for row, adjusted_p in zip(pair_rows, adjusted, strict=True):
        row["holm_adjusted_p_value"] = adjusted_p if row["available"] else None

    hard_rows = [row["hard"] for row in by_key.values()]
    hard_expressible = [
        (record, _HARD_GOLD_OUTCOMES[sample.gold.answer_letter])
        for record, sample in hard_rows
        if _HARD_GOLD_OUTCOMES[sample.gold.answer_letter] is not None
    ]
    hard_outcome_matches = sum(
        record.status is JudgeStatus.VALID_JUDGMENT and record.normalized_outcomes == expected
        for record, expected in hard_expressible
    )

    return {
        "schema": "codejudge-eval-v3b-report-v1",
        "experiment_id": "codejudge-eval-v3b-granularity",
        "analysis_population": {
            "candidate_n": len(by_key),
            "planned_call_n": len(joined),
            "complete_triplet_n": sum(
                all(record.status is JudgeStatus.VALID_JUDGMENT for record, _ in row.values())
                for row in by_key.values()
            ),
        },
        "per_condition": per_condition,
        "primary_pairwise_functional_correctness": pair_rows,
        "multiple_comparison_policy": {
            "method": "Holm step-down across the fixed family of three pairwise comparisons",
            "family_size": 3,
            "unavailable_comparison_placeholder_p": 1.0,
        },
        "hard_execution_outcome_exact_match_non_ce": _proportion(
            hard_outcome_matches, len(hard_expressible)
        ),
        "failure_policy": (
            "no imputation; pairwise tests use complete pairs. A comparison is unavailable "
            "when either condition failure rate exceeds 5%; failures remain incorrect in "
            "per-condition full-denominator accuracy and exact-match metrics"
        ),
        "paired_difference_interval": {
            "method": "candidate-level nonparametric percentile bootstrap",
            "resamples": BOOTSTRAP_RESAMPLES,
            "base_seed": B_BOOTSTRAP_SEED,
            "native_label_seed_offset": 100,
        },
    }


__all__ = [
    "A_BOOTSTRAP_SEED",
    "BOOTSTRAP_RESAMPLES",
    "B_BOOTSTRAP_SEED",
    "B_FAILURE_RATE_LIMIT",
    "exact_mcnemar_p_value",
    "exact_mcnemar_power",
    "holm_adjust",
    "score_v3_a",
    "score_v3_b",
]
