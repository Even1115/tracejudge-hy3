"""Offline label revision checks; no model or candidate execution."""

import json
from pathlib import Path

import pytest

from tracejudge_hy3.process_eval_v2.label_revision import (
    REVISION_CONFIG,
    prepare_revision,
    rescore_revision,
)
from tracejudge_hy3.process_eval_v2.materials import canonical, digest
from tracejudge_hy3.process_eval_v2.method_validation import prepare_validation

ROOT = Path(__file__).resolve().parents[1]
KEY = "item-89cda539ad51dde89041"


def test_revision_changes_only_bound_label_and_retains_original_inputs():
    parent, sha = prepare_validation(ROOT, "development")
    revised = prepare_revision(ROOT, parent, sha)
    assert revised.inputs is parent.inputs
    assert [key for key in parent.labels if parent.labels[key] != revised.labels[key]] == [KEY]
    assert parent.labels[KEY].process_correct is True
    assert revised.labels[KEY].process_correct is False
    assert revised.labels[KEY].plan_code_aligned is True
    assert revised.labels[KEY].first_faulty_location.entry_index == 1
    assert revised.labels[KEY].first_faulty_step is None
    assert sum(label.process_correct is False for label in revised.labels.values()) == 5
    assert "post_prediction" in revised.annotation_provenance


def test_revision_rejects_changed_parent_even_if_patch_hash_is_updated(tmp_path):
    parent, sha = prepare_validation(ROOT, "development")
    config = json.loads((ROOT / REVISION_CONFIG).read_bytes())
    for entry in (config["revisions"], config["review_notes"]):
        target = tmp_path / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / entry["path"]).read_bytes())
    path = tmp_path / config["revisions"]["path"]
    patch = json.loads(path.read_bytes())
    patch["previous_label_sha256"] = "0" * 64
    path.write_bytes(canonical(patch) + b"\n")
    config["revisions"]["sha256"] = digest(path.read_bytes())
    (tmp_path / REVISION_CONFIG).parent.mkdir(parents=True)
    (tmp_path / REVISION_CONFIG).write_bytes(canonical(config))
    with pytest.raises(ValueError, match="old label or input"):
        prepare_revision(tmp_path, parent, sha)


def test_both_saved_methods_are_rescored_in_both_views_offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("model construction and candidate execution forbidden")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("tracejudge_hy3.providers.hy3_openai.Hy3OpenAIProvider.__init__", forbidden)
    result = rescore_revision(ROOT)
    assert result["calls_made"] == result["candidate_executions"] == 0
    assert result["original"]["sources"] == result["revised"]["sources"]
    for view in ("judge_raw", "rule_merged"):
        assert result["original"]["paired"][view]["per_item"][KEY] == {
            "baseline": "fp",
            "assumption_audit": "fp",
        }
        assert result["revised"]["paired"][view]["per_item"][KEY] == {
            "baseline": "tp",
            "assumption_audit": "tp",
        }
    saved = (
        ROOT
        / "artifacts/experiments/process-method-v2/dev36-comparison-20260910-v1/comparison.json"
    )
    assert result["original"] == json.loads(saved.read_bytes())
