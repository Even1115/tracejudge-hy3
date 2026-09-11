"""Development-set preparation: pilot consensus carried forward plus a
single-reviewer extension, under the same source-binding discipline.

The development config names the frozen annotation packet, the reviewed pilot
configuration (which in turn pins the reviewed dual-annotator submissions), a
carried label file that must reproduce the reviewed pilot rows byte-for-byte in
content, and an extension label file whose single-reviewer authorship is
declared honestly in ``annotation_provenance``. Extension rows override carried
rows for the same item; final coverage must equal the development split.
"""

from __future__ import annotations

import re
from pathlib import Path

from tracejudge_hy3.process_eval_v2.contracts import (
    FunctionalEvidence,
    PilotInput,
    PilotLabel,
    PublicTask,
)
from tracejudge_hy3.process_eval_v2.materials import canonical, digest
from tracejudge_hy3.process_eval_v2.preflight import (
    PreparedPilot,
    by_id,
    contained,
    read_json,
    read_rows,
    validate_label_context,
    verified,
)
from tracejudge_hy3.schemas.solution import SolutionTrace

DEVELOPMENT_CONFIG_SCHEMA = "tracejudge-process-development-config-v1"
DEVELOPMENT_LABEL_STAGE = "post_feedback_development_extension"
DEFAULT_DEVELOPMENT_CONFIG = Path("data/manifests/process_development_v1.json")


def load_development_inputs(
    root: Path, packet_entry: dict, registry_path: str, provenance: dict
) -> dict[str, PilotInput]:
    """Rebuild development PilotInputs from the frozen packet with full binding checks."""
    root = root.resolve()
    packet_path, raw_packet = verified(root, packet_entry, provenance)
    packet = read_json(raw_packet)
    if packet["schema"] != "mbpp-process-materials-v1":
        raise ValueError("unsupported annotation packet")
    packet_root = packet_path.parent

    def packet_file(name):
        return verified(
            packet_root, {"path": name, "sha256": packet["files_sha256"][name]}, provenance
        )[1]

    packet_file("ANNOTATION_GUIDE.md")
    a_items = by_id(read_rows(packet_file("development/annotator_a/items.jsonl")))
    b_items = by_id(read_rows(packet_file("development/annotator_b/items.jsonl")))
    if not a_items or a_items != b_items:
        raise ValueError("development annotators did not receive identical items")
    mapping = by_id(read_rows(packet_file("coordinator/identity_and_outcomes.jsonl")))
    registry_raw = contained(root, registry_path).read_bytes()
    if digest(registry_raw) != packet["source_verification"]["registry_sha256"]:
        raise ValueError("benchmark registry differs from the packet's pinned registry")
    provenance[registry_path] = digest(registry_raw)
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
            raise ValueError("development set includes reserved or cross-run material")
        if origin["parent_task_id"] in parent_ids:
            raise ValueError("development set requires one original candidate per parent")
        parent_ids.add(origin["parent_task_id"])
        if digest(canonical(material)) != origin["packet_item_sha256"]:
            raise ValueError("development item binding mismatch")
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
    return inputs


def prepare_development(
    root: Path, config_path: Path = DEFAULT_DEVELOPMENT_CONFIG
) -> PreparedPilot:
    root = root.resolve()
    path = config_path if config_path.is_absolute() else root / config_path
    raw_config = path.read_bytes()
    config = read_json(raw_config)
    if config["schema"] != DEVELOPMENT_CONFIG_SCHEMA:
        raise ValueError("unsupported development configuration")
    if config["label_stage"] != DEVELOPMENT_LABEL_STAGE:
        raise ValueError("this adapter only supports the declared extension stage")
    provenance = {str(path.resolve()): digest(raw_config)}

    inputs = load_development_inputs(
        root, config["packet_manifest"], config["benchmark_registry"], provenance
    )

    # The carried file must reproduce the reviewed pilot "a" submission exactly:
    # bind through the reviewed pilot config, which the pilot audit pins in turn.
    _, raw_pilot_config = verified(root, config["pilot_config"], provenance)
    pilot_config = read_json(raw_pilot_config)
    if pilot_config["schema"] != "tracejudge-process-pilot-config-v1":
        raise ValueError("carried labels must bind to the reviewed pilot configuration")
    _, raw_reviewed_a = verified(root, pilot_config["annotations"]["a"], provenance)
    _, raw_audit = verified(root, config["review_audit"], provenance)
    audit = read_json(raw_audit)
    if audit["input_submissions"]["a"]["sha256"] != digest(raw_reviewed_a):
        raise ValueError("carried source is not the reviewed pilot submission")
    reviewed_rows = by_id(read_rows(raw_reviewed_a))

    _, raw_carried = verified(root, config["annotations"]["carried"], provenance)
    carried_rows = by_id(read_rows(raw_carried))
    if set(carried_rows) != set(reviewed_rows):
        raise ValueError("carried label coverage differs from the reviewed pilot")
    for item_id, row in carried_rows.items():
        if row != reviewed_rows[item_id]:
            raise ValueError("carried label differs from the reviewed pilot row")
    if not set(carried_rows) <= set(inputs):
        raise ValueError("carried labels reference items outside the development split")
    carried = {key: PilotLabel.model_validate(value) for key, value in carried_rows.items()}
    for key, label in carried.items():
        validate_label_context(label, inputs[key])

    _, raw_extension = verified(root, config["annotations"]["extension"], provenance)
    extension_rows = by_id(read_rows(raw_extension))
    if not set(extension_rows) <= set(inputs):
        raise ValueError("extension labels reference items outside the development split")
    declared_annotator = config["annotations"]["extension"]["annotator"]
    extension = {key: PilotLabel.model_validate(value) for key, value in extension_rows.items()}
    for key, label in extension.items():
        if label.annotator != declared_annotator:
            raise ValueError("unexpected extension annotator identity")
        validate_label_context(label, inputs[key])

    labels = {**carried, **extension}
    if set(labels) != set(inputs):
        raise ValueError("final development labels do not cover the full development split")
    return PreparedPilot(
        inputs,
        labels,
        provenance,
        config["label_stage"],
        config["annotation_provenance"],
    )
