"""Regression for MBPP native set inputs and exactly-once deserialization."""

import hashlib
import json

import pytest

from tracejudge_hy3.evalplus_mbpp import container_entrypoint as entry


def test_override_retains_serialized_input_when_native_loader_creates_sets(tmp_path):
    raw = {
        "task_id": "Mbpp/2",
        "prompt": "public",
        "entry_point": "f",
        "base_input": [[[1, 2], [2, 3]]],
        "plus_input": [],
    }
    native = {"Mbpp/2": {**raw, "base_input": [[{1, 2}, {2, 3}]]}}
    path = tmp_path / "release.jsonl"
    path.write_text(json.dumps(raw) + "\n")
    result = entry._read_verified_release_rows(str(path), native)
    assert result == {"Mbpp/2": raw}
    assert len(entry._canonical_dataset_sha256(result)) == 64
    output = tmp_path / "override.jsonl"
    digest = entry._write_private_jsonl(output, result["Mbpp/2"])
    assert json.loads(output.read_text()) == raw
    assert digest == hashlib.md5(output.read_bytes()).hexdigest()


def test_release_rows_reject_duplicates_and_public_identity_drift(tmp_path):
    raw = {"task_id": "Mbpp/2", "prompt": "public", "entry_point": "f"}
    path = tmp_path / "release.jsonl"
    path.write_text((json.dumps(raw) + "\n") * 2)
    with pytest.raises(entry._EntrypointError):
        entry._read_verified_release_rows(str(path), {"Mbpp/2": raw})
    path.write_text(json.dumps(raw) + "\n")
    with pytest.raises(entry._EntrypointError):
        entry._read_verified_release_rows(str(path), {"Mbpp/2": {**raw, "prompt": "changed"}})


def test_private_release_fingerprint_preserves_official_nonfinite_inputs(tmp_path):
    raw = {"task_id": "Mbpp/2", "base_input": [[float("inf"), float("-inf")]]}
    assert len(entry._canonical_dataset_sha256({"Mbpp/2": raw})) == 64
    path = tmp_path / "override.jsonl"
    entry._write_private_jsonl(path, raw)
    assert json.loads(path.read_text()) == raw
