"""Offline preparation: exact source bindings, no providers or sandbox imports."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from tracejudge_hy3.process_eval_v2.contracts import (
    METHODS,
    FunctionalEvidence,
    PilotInput,
    PilotLabel,
    PublicTask,
)
from tracejudge_hy3.process_eval_v2.materials import canonical, digest
from tracejudge_hy3.schemas.solution import SolutionTrace

DEFAULT_CONFIG = Path("data/manifests/process_pilot_v1.json")


def read_json(raw: bytes):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError(f"non-finite JSON value: {value}")

    return json.loads(
        raw.decode("utf-8-sig"), object_pairs_hook=unique, parse_constant=reject_constant
    )


def read_rows(raw: bytes) -> list[dict]:
    return [read_json(line) for line in raw.splitlines() if line.strip()]


def by_id(rows: list[dict], field: str = "item_id") -> dict[str, dict]:
    result = {}
    for row in rows:
        key = row[field]
        if not isinstance(key, str) or not key or key in result:
            raise ValueError(f"missing or duplicate {field}")
        result[key] = row
    return result


def contained(root: Path, relative: str) -> Path:
    part = PurePosixPath(relative)
    if (
        not relative
        or part.is_absolute()
        or ".." in part.parts
        or ":" in relative
        or "\\" in relative
    ):
        raise ValueError("invalid relative evidence path")
    path = root.joinpath(*part.parts).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("evidence path escapes its root")
    return path


def verified(root: Path, entry: dict, provenance: dict) -> tuple[Path, bytes]:
    path = contained(root, entry["path"])
    raw = path.read_bytes()
    if digest(raw) != entry["sha256"]:
        raise ValueError(f"source SHA-256 mismatch: {entry['path']}")
    provenance[str(path)] = digest(raw)
    return path, raw


def validate_label_context(label: PilotLabel, item: PilotInput) -> None:
    steps = {step.step_id for step in item.solution_trace.implementation_steps}
    requirements = {req.requirement_id for req in item.problem.requirements}
    if label.first_faulty_step is not None and label.first_faulty_step not in steps:
        raise ValueError("gold label references an unknown step")
    if label.first_faulty_location is not None:
        label.first_faulty_location.validate_against(item.solution_trace)
    for evidence in label.evidence:
        if evidence.step_id is not None and evidence.step_id not in steps:
            raise ValueError("gold evidence references an unknown step")
        if evidence.requirement_id is not None and evidence.requirement_id not in requirements:
            raise ValueError("gold evidence references an unknown requirement")
        if not evidence.description.strip():
            raise ValueError("gold evidence description is blank")
        if evidence.code_span is not None:
            match = re.fullmatch(r"L([1-9]\d*)(?:-L([1-9]\d*))?", evidence.code_span)
            if not match or not 1 <= int(match[1]) <= int(match[2] or match[1]) <= len(
                item.solution_trace.code.splitlines()
            ):
                raise ValueError("gold evidence line span is invalid")


@dataclass
class PreparedPilot:
    inputs: dict[str, PilotInput]
    labels: dict[str, PilotLabel]
    provenance: dict
    label_stage: str
    annotation_provenance: str

    def input_hash(self, item_id: str) -> str:
        return digest(canonical(self.inputs[item_id].model_dump(mode="json")))


def prepare_pilot(root: Path, config_path: Path = DEFAULT_CONFIG) -> PreparedPilot:
    root = root.resolve()
    path = config_path if config_path.is_absolute() else root / config_path
    raw_config = path.read_bytes()
    config = read_json(raw_config)
    if config["schema"] != "tracejudge-process-pilot-config-v1":
        raise ValueError("unsupported pilot configuration")
    if config["label_stage"] != "post_feedback_development_consensus":
        raise ValueError("this adapter only supports revised development consensus")
    provenance = {str(path.resolve()): digest(raw_config)}
    packet_path, raw_packet = verified(root, config["packet_manifest"], provenance)
    packet = read_json(raw_packet)
    if packet["schema"] != "mbpp-process-materials-v1":
        raise ValueError("unsupported annotation packet")
    packet_root = packet_path.parent
    _, audit_raw = verified(root, config["review_audit"], provenance)
    audit = read_json(audit_raw)
    if audit["packet_manifest_sha256"] != digest(raw_packet) or audit["structural_errors"]:
        raise ValueError("review audit is not a clean review of this packet")

    def packet_file(name):
        return verified(
            packet_root, {"path": name, "sha256": packet["files_sha256"][name]}, provenance
        )[1]

    packet_file("ANNOTATION_GUIDE.md")
    a_items = by_id(read_rows(packet_file("pilot/annotator_a/items.jsonl")))
    b_items = by_id(read_rows(packet_file("pilot/annotator_b/items.jsonl")))
    if not a_items or a_items != b_items:
        raise ValueError("pilot annotators did not receive identical items")
    mapping = by_id(read_rows(packet_file("coordinator/identity_and_outcomes.jsonl")))
    registry_raw = contained(root, config["benchmark_registry"]).read_bytes()
    if digest(registry_raw) != packet["source_verification"]["registry_sha256"]:
        raise ValueError("benchmark registry differs from the packet's pinned registry")
    provenance[config["benchmark_registry"]] = digest(registry_raw)
    registry = read_json(registry_raw)
    entry = registry["sources"]["mbpp.execution.results"]
    if entry["sha256"] != packet["source_hashes"]["mbpp.execution.results"]:
        raise ValueError("execution source differs from the packet binding")
    _, execution_raw = verified(root, entry, provenance)
    executions = by_id(read_rows(execution_raw), "problem_id")
    inputs = {}
    parent_ids = set()
    for item_id, material in a_items.items():
        origin = mapping[item_id]
        if origin["split"] != "development" or origin["run_id"] != packet["source_run_id"]:
            raise ValueError("pilot includes reserved or cross-run material")
        if origin["parent_task_id"] in parent_ids:
            raise ValueError("this pilot requires one original candidate per parent")
        parent_ids.add(origin["parent_task_id"])
        if digest(canonical(material)) != origin["packet_item_sha256"]:
            raise ValueError("pilot item binding mismatch")
        result = executions[origin["parent_task_id"]]
        code_hash = digest(material["solution_trace"]["code"].encode("utf-8"))
        if (
            digest(canonical(result)) != origin["execution_record_sha256"]
            or code_hash != origin["code_sha256"]
            or code_hash != result["solution_sha256"]
            or code_hash != result["source_candidate"]["code_sha256"]
            or result["run_id"] != packet["source_run_id"]
            or result["source_candidate"]["task_id"] != origin["parent_task_id"]
        ):
            raise ValueError("functional evidence does not match frozen candidate/run")
        ok = result["infrastructure_status"] == "ok"
        if ok and (
            result["passed_base"] is not (result["base_status"] == "pass")
            or result["passed_plus"] is not (result["plus_status"] == "pass")
        ):
            raise ValueError("inconsistent official status booleans")
        task = PublicTask.model_validate(material["problem"])
        # Fix the future Judge view, preserving the previously seen packet bytes.
        task = task.model_copy(update={"title": re.sub(r"^Mbpp/\d+:\s*", "", task.title)})
        solution = SolutionTrace.model_validate(
            {"problem_id": item_id, **material["solution_trace"]}
        )
        req_ids = {req.requirement_id for req in task.requirements}
        if solution.problem_id != item_id or any(
            set(step.related_requirements) - req_ids for step in solution.implementation_steps
        ):
            raise ValueError("solution public-context identity mismatch")
        inputs[item_id] = PilotInput(
            item_id=item_id,
            problem=task,
            solution_trace=solution,
            functional_evidence=FunctionalEvidence(
                source_run_id=result["run_id"],
                source_record_sha256=digest(canonical(result)),
                candidate_code_sha256=code_hash,
                infrastructure_status="ok" if ok else "error",
                base_status=result["base_status"] if ok else "unavailable",
                plus_status=result["plus_status"] if ok else "unavailable",
            ),
        )
    labels_by_person = {}
    for person in ("a", "b"):
        _, raw = verified(root, config["annotations"][person], provenance)
        if digest(raw) != audit["input_submissions"][person]["sha256"]:
            raise ValueError("labels differ from the reviewed submission")
        values = by_id(read_rows(raw))
        if set(values) != set(inputs):
            raise ValueError("label coverage differs from pilot")
        labels = {key: PilotLabel.model_validate(value) for key, value in values.items()}
        for key, label in labels.items():
            if label.annotator != f"annotator_{person}":
                raise ValueError("unexpected annotator identity")
            validate_label_context(label, inputs[key])
        labels_by_person[person] = labels
    for key in inputs:
        decisions = [
            labels_by_person[p][key].model_dump(exclude={"evidence", "rationale", "annotator"})
            for p in ("a", "b")
        ]
        if decisions[0] != decisions[1]:
            raise ValueError("unresolved inter-annotator decision disagreement")
    return PreparedPilot(
        inputs,
        labels_by_person["a"],
        provenance,
        config["label_stage"],
        config["annotation_provenance"],
    )


def preflight_report(
    pilot: PreparedPilot, *, max_attempts: int = 2, max_requests: int = 72
) -> dict:
    base = len(pilot.inputs) * len(METHODS)
    if max_attempts < 1 or max_requests < base:
        raise ValueError("request plan cannot cover the planned judgments")
    labels = list(pilot.labels.values())
    return {
        "schema": "tracejudge-process-preflight-v1",
        "status": "ready_for_offline_scoring",
        "ready_for_live_run": True,
        "live_run": (
            "implemented as scripts/run_process_pilot.py --phase run --execute with "
            "enforced request budgets, append-only checkpoints and resume identity checks; "
            "this preflight phase itself stays offline"
        ),
        "label_stage": pilot.label_stage,
        "annotation_provenance": pilot.annotation_provenance,
        "item_count": len(labels),
        "methods": list(METHODS),
        "labels": dict(
            Counter(
                "unknown"
                if x.process_correct is None
                else "correct"
                if x.process_correct
                else "incorrect"
                for x in labels
            )
        ),
        "step_location_gold_n": sum(
            x.process_correct is False
            and x.first_faulty_step is not None
            and x.localization_status == "supported"
            for x in labels
        ),
        "structured_location_gold_n": sum(x.first_faulty_location is not None for x in labels),
        "functional_evidence": dict(
            Counter(
                "unknown"
                if x.functional_evidence.functional_correct is None
                else "pass"
                if x.functional_evidence.functional_correct
                else "fail"
                for x in pilot.inputs.values()
            )
        ),
        "budget_plan": {
            "base_judgments": base,
            "max_attempts_per_judgment": max_attempts,
            "unconstrained_max_requests": base * max_attempts,
            "request_cap": max_requests,
            "covers_all_planned_retries": max_requests >= base * max_attempts,
            "money_estimate": None,
            "note": "planning defaults; --phase run --execute enforces these caps with conservative accounting",
        },
        "calls_made": {"solver": 0, "judge": 0, "candidate_executions": 0},
        "verified_sources": pilot.provenance,
        "input_hashes": {key: pilot.input_hash(key) for key in pilot.inputs},
        "limitations": [
            "development feedback consensus, not independent held-out gold",
            "aggregate official evidence is not a per-test ExecutionSummary",
            "unknown labels and failed/missing predictions remain visible",
            "title IDs removed from new Judge view; earlier exposure cannot be undone",
            "no locations inferred from prose rationales or null step IDs",
        ],
    }
