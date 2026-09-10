"""Offline scoring with explicit abstention, failure and missing denominators."""

from __future__ import annotations

from collections import Counter
from typing import Any

from tracejudge_hy3.process_eval_v2.contracts import METHODS, Prediction, public_process_correct
from tracejudge_hy3.process_eval_v2.preflight import PreparedPilot


def ratio(numerator: int, denominator: int) -> dict:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
    }


def _check_assessment_references(pilot: PreparedPilot, item_id: str, assessment) -> None:
    item = pilot.inputs[item_id]
    assessment.validate_location_against(item.solution_trace)
    steps = {s.step_id for s in item.solution_trace.implementation_steps}
    references = set(assessment.affected_steps)
    if assessment.first_faulty_step is not None:
        references.add(assessment.first_faulty_step)
    if references - steps:
        raise ValueError("prediction references unknown steps")
    requirements = {r.requirement_id for r in item.problem.requirements}
    if (
        assessment.violated_requirement is not None
        and assessment.violated_requirement not in requirements
    ):
        raise ValueError("prediction references unknown requirement")


def _score_methods(
    pilot: PreparedPilot,
    indexed: dict[tuple[str, str], tuple[str, Any]],
    methods,
) -> dict:
    """Score (status, assessment) rows keyed by (method, item_id)."""

    methods_out = {}
    for method in methods:
        coverage = Counter({"ok": 0, "missing": 0, "provider_error": 0, "parse_error": 0})
        for item_id in pilot.inputs:
            entry = indexed.get((method, item_id))
            coverage[entry[0] if entry else "missing"] += 1
        dimensions = {}
        for field in ("reasoning_correct", "plan_code_aligned", "public_process_correct"):
            counts = Counter(
                {
                    name: 0
                    for name in (
                        "gold_known",
                        "gold_unknown",
                        "tp",
                        "fp",
                        "tn",
                        "fn",
                        "known_missing",
                        "known_failed",
                        "known_abstained",
                        "unknown_missing",
                        "unknown_failed",
                        "unknown_abstained",
                        "unknown_decided",
                    )
                }
            )
            for item_id, gold in pilot.labels.items():
                expected = (
                    gold.process_correct
                    if field == "public_process_correct"
                    else getattr(gold, field)
                )
                prefix = "unknown" if expected is None else "known"
                counts[f"gold_{prefix}"] += 1
                entry = indexed.get((method, item_id))
                if entry is None:
                    counts[f"{prefix}_missing"] += 1
                    continue
                status, assessment = entry
                if status != "ok":
                    counts[f"{prefix}_failed"] += 1
                    continue
                observed = (
                    public_process_correct(
                        assessment.reasoning_correct, assessment.plan_code_aligned
                    )
                    if field == "public_process_correct"
                    else getattr(assessment, field)
                )
                if observed is None:
                    counts[f"{prefix}_abstained"] += 1
                elif expected is None:
                    counts["unknown_decided"] += 1
                else:
                    # The positive class is an error (False).
                    counts[
                        ("tp" if observed is False else "fn")
                        if expected is False
                        else ("fp" if observed is False else "tn")
                    ] += 1
            decided = sum(counts[k] for k in ("tp", "fp", "tn", "fn"))
            dimensions[field] = {
                **counts,
                "accuracy_all_known": ratio(counts["tp"] + counts["tn"], counts["gold_known"]),
                "decision_coverage_known": ratio(decided, counts["gold_known"]),
                "error_precision_decided": ratio(counts["tp"], counts["tp"] + counts["fp"]),
                "error_recall_decided": ratio(counts["tp"], counts["tp"] + counts["fn"]),
                "false_positive_rate_decided": ratio(counts["fp"], counts["fp"] + counts["tn"]),
            }
        localization = {}
        for metric, field in (
            ("first_layer", "first_faulty_layer"),
            ("first_step", "first_faulty_step"),
            ("structured_location_exact", "first_faulty_location"),
        ):
            eligible = {
                key: label
                for key, label in pilot.labels.items()
                if label.process_correct is False
                and label.localization_status == "supported"
                and getattr(label, field) is not None
            }
            correct = 0
            for key, label in eligible.items():
                entry = indexed.get((method, key))
                if entry and entry[0] == "ok":
                    assessment = entry[1]
                    if public_process_correct(
                        assessment.reasoning_correct, assessment.plan_code_aligned
                    ) is False and getattr(assessment, field) == getattr(label, field):
                        correct += 1
            localization[metric] = ratio(correct, len(eligible))
        methods_out[method] = {
            "coverage": dict(coverage),
            "dimensions": dimensions,
            "localization": localization,
        }
    return methods_out


def score_predictions(pilot: PreparedPilot, predictions: list[Prediction]) -> dict:
    indexed = {}
    for prediction in predictions:
        key = (prediction.method, prediction.item_id)
        if prediction.item_id not in pilot.inputs or key in indexed:
            raise ValueError("duplicate or unexpected prediction identity")
        if prediction.input_sha256 != pilot.input_hash(prediction.item_id):
            raise ValueError("prediction input SHA-256 mismatch")
        if prediction.assessment is not None:
            _check_assessment_references(pilot, prediction.item_id, prediction.assessment)
        indexed[key] = (prediction.status, prediction.assessment)
    return {
        "schema": "tracejudge-process-scores-v1",
        "item_count": len(pilot.inputs),
        "label_stage": pilot.label_stage,
        "annotation_provenance": pilot.annotation_provenance,
        "positive_class": "error (False)",
        "methods": _score_methods(pilot, indexed, METHODS),
        "notes": [
            "accuracy_all_known includes missing, failed and abstained predictions in its denominator",
            "decided-only rates must be read alongside decision coverage",
            "unknown gold is excluded from binary accuracy but retained in coverage",
            "legacy joint process_correct is not the public-process scoring signal",
            "structured location exact match is textual agreement, not semantic proof",
            "zero eligible location labels yields null, never a null/null success",
        ],
    }


def outcome_category(status: str, assessment, expected: bool | None) -> str:
    """One item's outcome bucket; the positive class is an error (False)."""

    if status == "missing":
        return "missing"
    if status != "ok":
        return "failed"
    observed = public_process_correct(assessment.reasoning_correct, assessment.plan_code_aligned)
    if observed is None:
        return "abstained"
    if expected is None:
        return "unknown_decided"
    if expected is False:
        return "tp" if observed is False else "fn"
    return "fp" if observed is False else "tn"


def score_ablation(
    pilot: PreparedPilot,
    predictions: list,
    *,
    run_report: dict | None = None,
    conditions: tuple[str, ...] | None = None,
) -> dict:
    """Score ablation rows: judge-raw and rule-merged views plus paired changes.

    The stored merged assessment is recomputed offline from ``judge_raw`` and
    the deterministic rule layer; any mismatch refuses the file rather than
    silently scoring unverifiable numbers.
    """

    from tracejudge_hy3.process_eval_v2.ablation import (
        COMPARISONS,
        CONDITIONS,
        SCORES_SCHEMA,
        _functional_snapshot,
        build_rule_report,
        merge_offline,
    )
    from tracejudge_hy3.process_eval_v2.aggregate import (
        aggregate_execution_summary,
        build_scaffold_problem,
    )
    from tracejudge_hy3.process_eval_v2.assumptions import validate_assumptions
    from tracejudge_hy3.static_analysis.ast_analyzer import analyze_code

    selected = CONDITIONS if conditions is None else conditions
    if not selected or len(set(selected)) != len(selected) or set(selected) - set(CONDITIONS):
        raise ValueError("invalid scoring conditions")

    indexed: dict[tuple[str, str], Any] = {}
    for prediction in predictions:
        key = (prediction.condition, prediction.item_id)
        if prediction.item_id not in pilot.inputs or key in indexed:
            raise ValueError("duplicate or unexpected prediction identity")
        if prediction.condition not in selected:
            raise ValueError(f"unknown ablation condition: {prediction.condition!r}")
        if prediction.input_sha256 != pilot.input_hash(prediction.item_id):
            raise ValueError("prediction input SHA-256 mismatch")
        if prediction.functional_evidence != _functional_snapshot(pilot.inputs[prediction.item_id]):
            raise ValueError("functional snapshot differs from frozen input")
        if prediction.assumption_review is not None:
            if prediction.status != "ok":
                raise ValueError("failed prediction cannot carry assumption review")
            validate_assumptions(
                prediction.assumption_review, prediction.judge_raw, pilot.inputs[prediction.item_id]
            )
        for assessment in (prediction.judge_raw, prediction.assessment):
            if assessment is not None:
                _check_assessment_references(pilot, prediction.item_id, assessment)
        if prediction.status == "ok":
            # Offline re-merge of the SAME judge output; verifies traceability.
            item = pilot.inputs[prediction.item_id]
            problem = build_scaffold_problem(item)
            static = analyze_code(
                item.solution_trace.code,
                function_name=problem.function_name,
                visible_test_values=[],
            )
            execution = aggregate_execution_summary(item)
            report = build_rule_report(problem, item, static, execution)
            if report != prediction.rule_report:
                raise ValueError(
                    f"rule report for {prediction.condition}/{prediction.item_id} does not "
                    "match a deterministic recomputation; refusing to score"
                )
            recomputed = merge_offline(
                problem, item, static, execution, prediction.judge_raw, report.merged_rule
            )
            if recomputed != prediction.assessment:
                raise ValueError(
                    f"merged assessment for {prediction.condition}/{prediction.item_id} does "
                    "not match an offline re-merge of its judge output; refusing to score"
                )
        indexed[key] = prediction

    judge_indexed = {key: (row.status, row.judge_raw) for key, row in indexed.items()}
    merged_indexed = {key: (row.status, row.assessment) for key, row in indexed.items()}
    judge_scores = _score_methods(pilot, judge_indexed, selected)
    merged_scores = _score_methods(pilot, merged_indexed, selected)

    per_sample = {}
    for item_id in sorted(pilot.inputs):
        gold = pilot.labels[item_id].process_correct
        rows = {}
        for condition in selected:
            prediction = indexed.get((condition, item_id))
            if prediction is None:
                rows[condition] = {"status": "missing", "judge": "missing", "merged": "missing"}
                continue
            entry = {
                "status": prediction.status,
                "judge": outcome_category(prediction.status, prediction.judge_raw, gold),
                "merged": outcome_category(prediction.status, prediction.assessment, gold),
                "functional_verdict": prediction.functional_evidence.functional_correct,
            }
            if prediction.status == "ok":
                fired = [r.rule_id for r in prediction.rule_report.rules if r.fired]
                entry["rules_fired"] = fired
                entry["rule_changed_judgment"] = (
                    prediction.rule_report.merged_rule is not None
                    and prediction.assessment != prediction.judge_raw
                )
                entry["judge_explanation"] = prediction.judge_raw.explanation
            rows[condition] = entry
        per_sample[item_id] = {"gold_process_correct": gold, "conditions": rows}

    rule_effects = {}
    for condition in selected:
        fired_counts: Counter[str] = Counter()
        changed = []
        for item_id in sorted(pilot.inputs):
            prediction = indexed.get((condition, item_id))
            if prediction is None or prediction.status != "ok":
                continue
            for observation in prediction.rule_report.rules:
                if observation.fired:
                    fired_counts[observation.rule_id] += 1
            if prediction.assessment != prediction.judge_raw:
                changes = {}
                for field in (
                    "reasoning_correct",
                    "plan_code_aligned",
                    "process_correct",
                    "first_faulty_layer",
                    "first_faulty_step",
                    "error_type",
                ):
                    before = getattr(prediction.judge_raw, field)
                    after = getattr(prediction.assessment, field)
                    if before != after:
                        changes[field] = {"judge": before, "merged": after}
                changed.append({"item_id": item_id, "changes": changes})
        rule_effects[condition] = {
            "rules_fired": dict(fired_counts),
            "changed_items": changed,
            "note": (
                "no rule merge changed any judgment in this condition"
                if not changed
                else "listed judgments differ between judge_raw and the merged assessment"
            ),
        }

    paired = []
    for treatment, baseline in COMPARISONS:
        if treatment not in selected or baseline not in selected:
            continue
        transitions: Counter[str] = Counter()
        items = {}
        for item_id in sorted(pilot.inputs):
            gold = pilot.labels[item_id].process_correct
            before_row = indexed.get((baseline, item_id))
            after_row = indexed.get((treatment, item_id))
            before = outcome_category(
                before_row.status if before_row else "missing",
                before_row.assessment if before_row else None,
                gold,
            )
            after = outcome_category(
                after_row.status if after_row else "missing",
                after_row.assessment if after_row else None,
                gold,
            )
            transition = f"{before}->{after}"
            transitions[transition] += 1
            if before != after:
                items[item_id] = transition
        paired.append(
            {
                "treatment": treatment,
                "baseline": baseline,
                "view": "rule-merged final judgments on the public process signal",
                "transitions": dict(transitions),
                "changed_items": items,
            }
        )

    functional_verdicts = {
        item_id: pilot.inputs[item_id].functional_evidence.functional_correct
        for item_id in sorted(pilot.inputs)
    }

    result = {
        "schema": SCORES_SCHEMA,
        "item_count": len(pilot.inputs),
        "label_stage": pilot.label_stage,
        "annotation_provenance": pilot.annotation_provenance,
        "positive_class": "error (False)",
        "conditions": list(selected),
        "views": {
            "judge_raw": judge_scores,
            "rule_merged": merged_scores,
        },
        "rule_effects": rule_effects,
        "paired_comparisons": paired,
        "per_sample": per_sample,
        "functional_verdicts": functional_verdicts,
        "notes": [
            "accuracy_all_known includes missing, failed and abstained predictions in its denominator",
            "decided-only rates must be read alongside decision coverage",
            "unknown gold is excluded from binary accuracy but retained in coverage",
            "the process signal is reasoning_correct AND plan_code_aligned; functional "
            "verdicts live in functional_verdicts, never inside the process signal",
            "12 labels / 2 known errors: report counts and paired changes only; no "
            "statistical-significance or generalization claim",
            "all 12 alignment gold labels are true: no mismatch-detection claim is possible",
            "zero eligible location labels yields null, never a null/null success",
            "an empty prediction file yields all-missing coverage, never fabricated results",
        ],
    }
    if run_report is not None:
        result["run"] = {
            "status": run_report.get("status"),
            "budget": run_report.get("budget"),
            "requests": run_report.get("requests"),
        }
    if pilot.label_stage != "post_feedback_development_consensus":
        result["notes"] = [
            note for note in result["notes"] if not note.startswith(("12 labels", "all 12"))
        ]
        result["notes"].append(
            "development and constructed probes are reported separately; no held-out or generalization claim"
        )
    return result
