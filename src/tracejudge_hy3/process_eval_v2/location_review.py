"""Supplementary citation coverage; never changes frozen exact-match scoring."""

from collections import Counter
from pathlib import Path

from tracejudge_hy3.process_eval_v2.contracts import public_process_correct
from tracejudge_hy3.process_eval_v2.materials import digest
from tracejudge_hy3.process_eval_v2.method_validation import (
    compare_methods,
    load_method_run,
    prepare_validation,
)
from tracejudge_hy3.process_eval_v2.preflight import read_json
from tracejudge_hy3.process_eval_v2.scoring import ratio

POLICY = Path("data/manifests/process_location_coverage_v1.json")
POLICY_SHA256 = "7690ed57face0f5471c03ce722de4017c948438a09391cbc10247737bf1ed092"


def source_text(location, solution):
    if location.source_field == "implementation_steps":
        return next(
            step.content
            for step in solution.implementation_steps
            if step.step_id == location.step_id
        )
    if location.source_field == "edge_cases_considered":
        return solution.edge_cases_considered[location.entry_index]
    return getattr(solution, location.source_field) or ""


def citation_coverage(pilot, predictions, *, view):
    if view not in ("judge_raw", "assessment"):
        raise ValueError("unsupported location view")
    indexed = {}
    for row in predictions:
        if (
            row.condition != "ablation_a"
            or row.item_id not in pilot.inputs
            or row.item_id in indexed
        ):
            raise ValueError("location review needs unique ablation_a predictions")
        if row.input_sha256 != pilot.input_hash(row.item_id):
            raise ValueError("location review input hash mismatch")
        indexed[row.item_id] = row
    counts, items = Counter(), {}
    eligible = hits = 0
    for key, gold in sorted(pilot.labels.items()):
        location = gold.first_faulty_location
        if (
            gold.process_correct is not False
            or gold.localization_status != "supported"
            or location is None
        ):
            counts["no_supported_structured_gold"] += 1
            continue
        location.validate_against(pilot.inputs[key].solution_trace)
        text = source_text(location, pilot.inputs[key].solution_trace)
        # Include overlapping occurrences when deciding whether a quote is unique.
        occurrences = sum(text.startswith(location.quote, n) for n in range(len(text)))
        if occurrences != 1:
            counts["ambiguous_gold_quote"] += 1
            continue
        eligible += 1
        row = indexed.get(key)
        assessment = getattr(row, view) if row and row.status == "ok" else None
        predicted = assessment.first_faulty_location if assessment else None
        if assessment is None:
            reason = "missing" if row is None else row.status
        elif (
            public_process_correct(assessment.reasoning_correct, assessment.plan_code_aligned)
            is not False
        ):
            reason = "no_decided_process_error"
        elif predicted is None:
            reason = "no_location"
        else:
            predicted.validate_against(pilot.inputs[key].solution_trace)
            if assessment.first_faulty_layer != gold.first_faulty_layer:
                reason = "different_layer"
            elif any(
                getattr(predicted, f) != getattr(location, f)
                for f in ("source_field", "step_id", "entry_index")
            ):
                reason = "different_source_address"
            elif location.quote not in predicted.quote:
                reason = "gold_quote_not_covered"
            else:
                reason = "covered"
                hits += 1
        counts[reason] += 1
        items[key] = {
            "reason": reason,
            "gold_location": location.model_dump(mode="json"),
            "predicted_location": predicted.model_dump(mode="json") if predicted else None,
            "gold_quote_chars": len(location.quote),
            "predicted_quote_chars": len(predicted.quote) if predicted else None,
        }
    return {"coverage": ratio(hits, eligible), "counts": dict(counts), "per_item": items}


def review_locations(root: Path, baseline: Path, audit: Path, *, location_v2=False):
    pilot, sha = prepare_validation(root, "probes", location_v2=location_v2)
    policy_raw = (root / POLICY).read_bytes()
    if digest(policy_raw) != POLICY_SHA256:
        raise ValueError("location coverage policy SHA-256 mismatch")
    policy = read_json(policy_raw)
    if (
        policy["metric"] != "same_source_gold_quote_coverage_v1"
        or policy["cross_source_credit"] is not False
    ):
        raise ValueError("unsupported location coverage policy")
    original = compare_methods(pilot, sha, baseline, audit, location_v2=location_v2)
    sources = {**pilot.provenance, str(root / POLICY): digest(policy_raw)}
    supplemental = {}
    rubrics = (
        ("baseline_location_v2", "assumption_audit_location_v2")
        if location_v2
        else ("baseline", "assumption_audit_v1")
    )
    for name, path, rubric in zip(
        ("baseline", "assumption_audit"), (baseline, audit), rubrics, strict=True
    ):
        rows, _, _ = load_method_run(pilot, sha, path, rubric)
        supplemental[name] = {
            view: citation_coverage(pilot, rows, view=field)
            for view, field in (("judge_raw", "judge_raw"), ("rule_merged", "assessment"))
        }
        report_path = path / "run-report.json"
        report = read_json(report_path.read_bytes())
        sources[str(report_path)] = digest(report_path.read_bytes())
        for filename in report["files_sha256"]:
            if filename not in {
                "run-config.json",
                "ablation-plan.json",
                "predictions.jsonl",
                "requests.jsonl",
                "diagnostics.jsonl",
            }:
                raise ValueError("unexpected run artifact")
            sources[str(path / filename)] = digest((path / filename).read_bytes())
    return {
        "schema": "tracejudge-location-review-v1",
        "calls_made": 0,
        "candidate_executions": 0,
        "constructed": True,
        "policy": policy,
        "original_comparison": original,
        "supplementary": supplemental,
        "verified_sources": sources,
        "review_implementation_sha256": digest(Path(__file__).read_bytes()),
    }
