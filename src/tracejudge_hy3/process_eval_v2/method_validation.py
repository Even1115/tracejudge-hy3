"""Bound development/probe validation, independent of sealed held-out materials."""

from collections import Counter
from pathlib import Path

from tracejudge_hy3.process_eval_v2.ablation import (
    AblationPrediction,
    build_rule_report,
    condition_fingerprints,
    execution_order,
    plan_fingerprint,
)
from tracejudge_hy3.process_eval_v2.aggregate import (
    aggregate_execution_summary,
    build_scaffold_problem,
)
from tracejudge_hy3.process_eval_v2.assumptions import AssumptionJudgment
from tracejudge_hy3.process_eval_v2.contracts import (
    FunctionalEvidence,
    PilotInput,
    PilotLabel,
    PublicTask,
)
from tracejudge_hy3.process_eval_v2.development import prepare_development
from tracejudge_hy3.process_eval_v2.location_format import (
    LOCATION_RUBRICS,
    validate_location_format,
)
from tracejudge_hy3.process_eval_v2.materials import canonical, digest
from tracejudge_hy3.process_eval_v2.preflight import (
    PreparedPilot,
    by_id,
    read_json,
    read_rows,
    validate_label_context,
    verified,
)
from tracejudge_hy3.process_eval_v2.scoring import outcome_category, score_ablation
from tracejudge_hy3.schemas.solution import SolutionTrace
from tracejudge_hy3.static_analysis.ast_analyzer import analyze_code

CONFIG = Path("data/manifests/process_method_validation_v2.json")
LOCATION_CONFIG = Path("data/manifests/process_method_location_v2.json")


def prepare_validation(
    root: Path, dataset: str, *, location_v2: bool = False
) -> tuple[PreparedPilot, str]:
    if dataset not in ("development", "probes"):
        raise ValueError("only development and constructed probes are supported")
    config_path = LOCATION_CONFIG if location_v2 else CONFIG
    raw = (root / config_path).read_bytes()
    config = read_json(raw)
    if (
        config["schema"] != "tracejudge-method-validation-config-v2"
        or config["conditions"] != ["ablation_a"]
        or config["rubrics"]
        != (
            ["baseline_location_v2", "assumption_audit_location_v2"]
            if location_v2
            else ["baseline", "assumption_audit_v1"]
        )
        or config["max_attempts_per_judgment"] != 2
    ):
        raise ValueError("unsupported method validation protocol")
    provenance = {str((root / config_path).resolve()): digest(raw)}
    if location_v2:
        if config.get("parent_config_sha256") != digest((root / CONFIG).read_bytes()):
            raise ValueError("location protocol parent config mismatch")
        provenance[str((root / CONFIG).resolve())] = config["parent_config_sha256"]
    path, _ = verified(root, config["development"], provenance)
    development = prepare_development(root, path)
    if len(development.inputs) != 36:
        raise ValueError("the frozen development protocol requires 36 items")
    provenance.update(development.provenance)
    if dataset == "development":
        development.provenance = provenance
        return development, digest(raw)
    _, raw_bundle = verified(root, config["probes"], provenance)
    bundle = read_json(raw_bundle)
    if (
        bundle["schema"] != "tracejudge-process-counterfactuals-v1"
        or bundle["constructed"] is not True
    ):
        raise ValueError("not a constructed probe bundle")
    _, packet_raw = verified(root, bundle["source_packet"], provenance)
    packet = read_json(packet_raw)
    dev_config = read_json(path.read_bytes())
    if digest(packet_raw) != dev_config["packet_manifest"]["sha256"]:
        raise ValueError("probe and development packets differ")
    packet_root = (root / bundle["source_packet"]["path"]).parent
    name = "development/annotator_a/items.jsonl"
    _, items_raw = verified(
        packet_root, {"path": name, "sha256": packet["files_sha256"][name]}, provenance
    )
    materials = by_id(read_rows(items_raw))
    parents = by_id(bundle["parents"])
    for key, parent in parents.items():
        original = materials[key]
        if (
            parent["packet_item_sha256"] != digest(canonical(original))
            or parent["problem"] != original["problem"]
            or parent["solution_trace"] != original["solution_trace"]
        ):
            raise ValueError("constructed parent binding mismatch")
    inputs, labels = {}, {}
    for probe in bundle["counterfactuals"]:
        if (
            probe["parent_item_id"] not in parents
            or probe["problem"] != materials[probe["parent_item_id"]]["problem"]
        ):
            raise ValueError("probe references an unbound parent or changed requirement")
        if (
            probe["execution_status"] != "not_executed_constructed"
            or probe["functional_evidence"] != "unavailable_never_executed"
        ):
            raise ValueError("probe cannot claim execution evidence")
        # Mutation names and expected impacts stay with the coordinator, not Judge.
        key = "probe-" + digest(probe["trace_id"].encode())[:20]
        if key in inputs:
            raise ValueError("duplicate probe identity")
        solution = SolutionTrace.model_validate({**probe["solution_trace"], "problem_id": key})
        parent = development.inputs[probe["parent_item_id"]]
        item = PilotInput(
            item_id=key,
            problem=PublicTask.model_validate(parent.problem.model_dump()),
            solution_trace=solution,
            functional_evidence=FunctionalEvidence(
                scope="constructed_unexecuted",
                infrastructure_status="not_executed",
                source_run_id=bundle["bundle_id"],
                source_record_sha256=digest(canonical(probe)),
                candidate_code_sha256=digest(solution.code.encode()),
                base_status="unavailable",
                plus_status="unavailable",
            ),
        )
        label = PilotLabel.model_validate({**probe["gold_label"], "item_id": key})
        validate_label_context(label, item)
        inputs[key], labels[key] = item, label
    if len(inputs) != 3:
        raise ValueError("the frozen probe protocol requires three probes")
    return PreparedPilot(
        inputs,
        labels,
        provenance,
        "constructed_capability_probes_v1",
        "single_reviewer_ai_assisted_constructed_not_natural",
    ), digest(raw)


def offline_checks(pilot: PreparedPilot) -> dict:
    fired = {}
    for key, item in pilot.inputs.items():
        problem = build_scaffold_problem(item)
        static = analyze_code(
            item.solution_trace.code, function_name=problem.function_name, visible_test_values=[]
        )
        if not static.ast_parse_ok:
            raise ValueError("frozen candidate could not be parsed statically")
        report = build_rule_report(problem, item, static, aggregate_execution_summary(item))
        fired[key] = [row.rule_id for row in report.rules if row.fired]
    return {
        "status": "offline_contracts_verified",
        "item_count": len(pilot.inputs),
        "label_stage": pilot.label_stage,
        "annotation_provenance": pilot.annotation_provenance,
        "labels": dict(
            Counter(
                "unknown"
                if x.process_correct is None
                else "correct"
                if x.process_correct
                else "incorrect"
                for x in pilot.labels.values()
            )
        ),
        "supported_layer_gold": sum(
            x.localization_status == "supported" and x.first_faulty_layer is not None
            for x in pilot.labels.values()
        ),
        "supported_step_gold": sum(
            x.localization_status == "supported" and x.first_faulty_step is not None
            for x in pilot.labels.values()
        ),
        "structured_location_gold": sum(
            x.first_faulty_location is not None for x in pilot.labels.values()
        ),
        "static_rule_triggers": fired,
        "judge_quality": "not_measured_no_model_calls",
        "calls_made": {"solver": 0, "judge": 0, "candidate_executions": 0},
        "verified_sources": pilot.provenance,
    }


def load_method_run(pilot: PreparedPilot, config_hash: str, target: Path, rubric: str):
    report = read_json((target / "run-report.json").read_bytes())
    if "diagnostics.jsonl" in report["files_sha256"] and (
        digest((target / "diagnostics.jsonl").read_bytes())
        != report["files_sha256"]["diagnostics.jsonl"]
    ):
        raise ValueError("diagnostic artifact SHA-256 mismatch")
    for name in ("run-config.json", "ablation-plan.json", "predictions.jsonl", "requests.jsonl"):
        if digest((target / name).read_bytes()) != report["files_sha256"][name]:
            raise ValueError("run artifact SHA-256 mismatch")
    identity = read_json((target / "run-config.json").read_bytes())
    plan = read_json((target / "ablation-plan.json").read_bytes())
    expected_inputs = {key: pilot.input_hash(key) for key in pilot.inputs}
    if (
        identity != report["run_identity"]
        or identity["pilot_config_sha256"] != config_hash
        or identity["input_hashes"] != expected_inputs
        or plan["items"] != expected_inputs
    ):
        raise ValueError("run/source identity mismatch")
    if (
        plan.get("rubric") != rubric
        or plan.get("label_stage") != pilot.label_stage
        or plan.get("verified_sources") != pilot.provenance
        or identity["conditions"] != ["ablation_a"]
        or plan_fingerprint(plan) != identity["plan_sha256"]
    ):
        raise ValueError("run rubric or dataset binding mismatch")
    if identity["condition_fingerprints"] != {
        key: value["fingerprint"] for key, value in plan["conditions"].items()
    }:
        raise ValueError("condition identity mismatch")
    if identity["condition_fingerprints"] != {
        "ablation_a": condition_fingerprints(rubric)["ablation_a"]
    }:
        raise ValueError("condition prompt changed")
    order = [list(pair) for pair in execution_order(pilot.inputs) if pair[0] == "ablation_a"]
    if plan["execution_order"] != order or report["execution_order"] != order:
        raise ValueError("execution order differs from the fixed validation protocol")
    expected_budget = {"max_attempts_per_judgment": 2, "max_requests": len(pilot.inputs) * 2}
    if identity["budget"] != expected_budget or any(
        plan["budget"][key] != value for key, value in expected_budget.items()
    ):
        raise ValueError("budget differs from the validation protocol")
    rows = [
        AblationPrediction.model_validate(row)
        for row in read_rows((target / "predictions.jsonl").read_bytes())
    ]
    for row in rows:
        if row.status == "ok" and (
            (row.assumption_review is not None)
            != (rubric in ("assumption_audit_v1", "assumption_audit_location_v2"))
        ):
            raise ValueError("assumption review is inconsistent with the run rubric")
        if row.status == "ok" and rubric in LOCATION_RUBRICS:
            judgment = (
                AssumptionJudgment(assessment=row.judge_raw, checks=row.assumption_review)
                if row.assumption_review is not None
                else row.judge_raw
            )
            validate_location_format(judgment, pilot.inputs[row.item_id])
    scores = score_ablation(pilot, rows, run_report=report, conditions=("ablation_a",))
    return rows, scores, identity


def compare_methods(
    pilot: PreparedPilot,
    config_hash: str,
    baseline: Path,
    audit: Path,
    *,
    scoring_pilot: PreparedPilot | None = None,
    location_v2: bool = False,
) -> dict:
    left_rubric, right_rubric = (
        ("baseline_location_v2", "assumption_audit_location_v2")
        if location_v2
        else ("baseline", "assumption_audit_v1")
    )
    left, left_scores, left_id = load_method_run(pilot, config_hash, baseline, left_rubric)
    right, right_scores, right_id = load_method_run(pilot, config_hash, audit, right_rubric)
    for key in ("model", "budget", "implementation_sha256", "prediction_schema_sha256"):
        if left_id[key] != right_id[key]:
            raise ValueError(f"baseline and audit differ in {key}")
    if scoring_pilot is not None:
        if {key: pilot.input_hash(key) for key in pilot.inputs} != {
            key: scoring_pilot.input_hash(key) for key in scoring_pilot.inputs
        }:
            raise ValueError("label-only rescoring cannot change inputs")
        # Verify historical run identities against ORIGINAL labels first;
        # only the scoring stage receives the explicitly revised labels.
        pilot = scoring_pilot
        left_scores, right_scores = [
            score_ablation(
                pilot,
                rows,
                run_report=read_json((path / "run-report.json").read_bytes()),
                conditions=("ablation_a",),
            )
            for rows, path in ((left, baseline), (right, audit))
        ]
    sides = [{row.item_id: row for row in rows} for rows in (left, right)]
    comparisons = {}
    for view, field in (("judge_raw", "judge_raw"), ("rule_merged", "assessment")):
        items, transitions = {}, Counter()
        for key, gold in pilot.labels.items():
            outcomes = []
            for side in sides:
                row = side.get(key)
                outcomes.append(
                    outcome_category(
                        row.status if row else "missing",
                        getattr(row, field) if row else None,
                        gold.process_correct,
                    )
                )
            transition = "->".join(outcomes)
            transitions[transition] += 1
            items[key] = {"baseline": outcomes[0], "assumption_audit": outcomes[1]}
        comparisons[view] = {"transitions": dict(transitions), "per_item": items}
    return {
        "schema": "tracejudge-method-validation-comparison-v2",
        "label_stage": pilot.label_stage,
        "constructed": pilot.label_stage == "constructed_capability_probes_v1",
        "baseline": left_scores,
        "assumption_audit": right_scores,
        "paired": comparisons,
        "sources": {
            str(path): digest((path / "predictions.jsonl").read_bytes())
            for path in (baseline, audit)
        },
        "interpretation": "descriptive development/probe comparison; assumption semantics require human review; no generalization claim",
    }
