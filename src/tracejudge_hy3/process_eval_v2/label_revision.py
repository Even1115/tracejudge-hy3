"""Explicit post-prediction label revisions; original run identities stay intact."""

from pathlib import Path

from tracejudge_hy3.process_eval_v2.contracts import PilotLabel
from tracejudge_hy3.process_eval_v2.materials import canonical, digest
from tracejudge_hy3.process_eval_v2.method_validation import compare_methods, prepare_validation
from tracejudge_hy3.process_eval_v2.preflight import (
    PreparedPilot,
    by_id,
    contained,
    read_json,
    read_rows,
    validate_label_context,
    verified,
)

REVISION_CONFIG = Path("data/manifests/process_development_labels_v2.json")


def prepare_revision(root: Path, parent: PreparedPilot, config_hash: str) -> PreparedPilot:
    path = root / REVISION_CONFIG
    raw = path.read_bytes()
    config = read_json(raw)
    if (
        config["schema"] != "tracejudge-development-label-revision-v1"
        or config["parent_validation_config_sha256"] != config_hash
        or config["parent_label_stage"] != parent.label_stage
        or config["label_stage"] != "post_prediction_development_revision_v2"
        or not config["annotation_provenance"]
    ):
        raise ValueError("label revision parent/stage mismatch")
    provenance = {**parent.provenance, str(path.resolve()): digest(raw)}
    _, patch = verified(root, config["revisions"], provenance)
    verified(root, config["review_notes"], provenance)
    revisions = by_id(read_rows(patch))
    if not revisions or set(revisions) != set(config["changed_item_ids"]):
        raise ValueError("revision coverage differs from declared changes")
    if not set(revisions) <= set(parent.labels):
        raise ValueError("revision includes non-development items")
    labels = dict(parent.labels)
    for key, revision in revisions.items():
        old = parent.labels[key].model_dump(mode="json")
        if revision["previous_label_sha256"] != digest(canonical(old)) or revision[
            "input_sha256"
        ] != parent.input_hash(key):
            raise ValueError("revision old label or input SHA-256 mismatch")
        label = PilotLabel.model_validate(revision["label"])
        if label.item_id != key or label.model_dump(mode="json") == old:
            raise ValueError("revision must change the declared item's label")
        validate_label_context(label, parent.inputs[key])
        labels[key] = label
    return PreparedPilot(
        parent.inputs, labels, provenance, config["label_stage"], config["annotation_provenance"]
    )


def rescore_revision(root: Path) -> dict:
    parent, config_hash = prepare_validation(root, "development")
    revised = prepare_revision(root, parent, config_hash)
    config = read_json((root / REVISION_CONFIG).read_bytes())
    runs = config["prediction_runs"]
    baseline = contained(root, runs["baseline"])
    audit = contained(root, runs["assumption_audit"])
    for name, path in (("baseline", baseline), ("assumption_audit", audit)):
        if (
            digest((path / "run-report.json").read_bytes())
            != config["prediction_report_sha256"][name]
        ):
            raise ValueError("reviewed prediction run report SHA-256 mismatch")
    original = compare_methods(parent, config_hash, baseline, audit)
    new = compare_methods(parent, config_hash, baseline, audit, scoring_pilot=revised)
    bindings = dict(revised.provenance)
    for path in (baseline, audit):
        for name in (
            "predictions.jsonl",
            "run-config.json",
            "run-report.json",
            "ablation-plan.json",
            "requests.jsonl",
        ):
            bindings[str(path / name)] = digest((path / name).read_bytes())
    return {
        "schema": "tracejudge-development-label-rescore-v1",
        "calls_made": 0,
        "candidate_executions": 0,
        "changed_item_ids": config["changed_item_ids"],
        "label_changes": {
            key: {
                "before": parent.labels[key].model_dump(mode="json"),
                "after": revised.labels[key].model_dump(mode="json"),
            }
            for key in config["changed_item_ids"]
        },
        "original": original,
        "revised": new,
        "verified_sources": bindings,
        "interpretation": (
            "Same saved predictions, two disclosed development label versions. "
            "Post-prediction AI-assisted review, not blind independent gold or method improvement."
        ),
    }
