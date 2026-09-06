from __future__ import annotations

import json
from pathlib import Path

import pytest
from mbppplus_fixtures import (
    EXPECTED_RECORD_COUNT,
    REVISION,
    expected_task_ids,
    raw_row,
    sha256_bytes,
    write_snapshot,
)
from typer.testing import CliRunner

from tracejudge_hy3.cli import app
from tracejudge_hy3.dataset.loader import load_problems
from tracejudge_hy3.dataset.mbppplus import (
    ADAPTER_NAME,
    ADAPTER_VERSION,
    DATASET_SOURCE,
    FULL_PROJECTION_KIND,
    FULL_SELECTION_ALGORITHM,
    SELECTION_ALGORITHM,
    SELECTION_MANIFEST_KIND,
    SIGNATURE_NOT_PUBLISHED_TAG,
    WITHHELD_REFERENCE_CODE,
    convert_mbppplus,
    ordered_problem_ids_sha256,
    sample_mbppplus,
    select_mbppplus_problem_ids,
    validate_mbppplus_public_problems,
)
from tracejudge_hy3.exceptions import DatasetError

SEED = 20260905
PRIVATE_CANARIES = (
    "PRIVATE_CANONICAL_CANARY",
    "PRIVATE_CONTRACT_CANARY",
    "PRIVATE_BASE_INPUT_CANARY",
    "PRIVATE_PLUS_INPUT_CANARY",
    "PRIVATE_ASSERTION_CANARY",
)


def _convert(tmp_path: Path):
    snapshot = write_snapshot(tmp_path / "raw")
    result = convert_mbppplus(
        input_path=snapshot.input_path,
        revision=REVISION,
        source_manifest_path=snapshot.source_manifest_path,
        output_dir=tmp_path / "full",
    )
    return snapshot, result


def test_convert_produces_complete_public_projection_without_private_content(tmp_path):
    _, result = _convert(tmp_path)

    assert result.record_count == EXPECTED_RECORD_COUNT
    problems = load_problems(result.dataset_path)
    validate_mbppplus_public_problems(
        problems,
        require_complete_snapshot=True,
        expected_ids=expected_task_ids(),
    )
    first = problems[0]
    assert first.problem_id == "Mbpp/2"
    assert first.source == DATASET_SOURCE
    assert first.difficulty == "unknown"
    assert first.reference_code == WITHHELD_REFERENCE_CODE
    assert first.function_signature == f"def {first.function_name}(...):"
    assert SIGNATURE_NOT_PUBLISHED_TAG in first.tags
    assert not (first.visible_test_cases or first.hidden_test_cases or first.challenge_test_cases)

    dataset_bytes = result.dataset_path.read_bytes()
    manifest_bytes = result.manifest_path.read_bytes()
    for canary in PRIVATE_CANARIES:
        assert canary.encode() not in dataset_bytes
        assert canary.encode() not in manifest_bytes

    manifest = json.loads(manifest_bytes)
    assert manifest["kind"] == FULL_PROJECTION_KIND
    assert manifest["adapter"] == {"name": ADAPTER_NAME, "version": ADAPTER_VERSION}
    assert manifest["revision"] == REVISION
    assert manifest["selection"]["algorithm"] == FULL_SELECTION_ALGORITHM
    assert manifest["selection"]["selected_problem_ids"] == expected_task_ids()
    assert set(manifest["withheld_fields"]) == {
        "assertion",
        "atol",
        "base_input",
        "canonical_solution",
        "contract",
        "plus_input",
    }

    # Re-running the identical conversion is idempotent.
    again = convert_mbppplus(
        input_path=tmp_path / "raw" / "MbppPlus.jsonl",
        revision=REVISION,
        source_manifest_path=tmp_path / "raw" / "source_manifest.json",
        output_dir=tmp_path / "full",
    )
    assert again.dataset_sha256 == result.dataset_sha256
    assert again.manifest_sha256 == result.manifest_sha256


def test_convert_rejects_wrong_revision(tmp_path):
    snapshot = write_snapshot(tmp_path / "raw")
    with pytest.raises(DatasetError, match="pinned MBPP\\+ revision"):
        convert_mbppplus(
            input_path=snapshot.input_path,
            revision="0" * 40,
            source_manifest_path=snapshot.source_manifest_path,
            output_dir=tmp_path / "out",
        )


def test_convert_rejects_tampered_raw_jsonl_hash(tmp_path):
    snapshot = write_snapshot(tmp_path / "raw")
    snapshot.input_path.write_bytes(snapshot.raw_bytes + b"\n")
    with pytest.raises(DatasetError, match="pinned source manifest"):
        convert_mbppplus(
            input_path=snapshot.input_path,
            revision=REVISION,
            source_manifest_path=snapshot.source_manifest_path,
            output_dir=tmp_path / "out",
        )


def test_convert_rejects_tampered_manifest_expected_ids(tmp_path):
    snapshot = write_snapshot(tmp_path / "raw")
    manifest = json.loads(snapshot.source_manifest_path.read_text(encoding="utf-8"))
    manifest["expected_task_ids"] = manifest["expected_task_ids"][:-1] + ["Mbpp/999"]
    snapshot.source_manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(DatasetError):
        convert_mbppplus(
            input_path=snapshot.input_path,
            revision=REVISION,
            source_manifest_path=snapshot.source_manifest_path,
            output_dir=tmp_path / "out",
        )


def test_convert_rejects_invalid_task_id_row(tmp_path):
    ids = expected_task_ids()
    rows = [raw_row(task_id, index) for index, task_id in enumerate(ids)]
    rows[3]["task_id"] = "HumanEval/3"
    snapshot = write_snapshot(tmp_path / "raw", rows=rows)
    with pytest.raises(DatasetError, match="invalid task_id"):
        convert_mbppplus(
            input_path=snapshot.input_path,
            revision=REVISION,
            source_manifest_path=snapshot.source_manifest_path,
            output_dir=tmp_path / "out",
        )


def test_convert_rejects_duplicate_task_id_row(tmp_path):
    ids = expected_task_ids()
    rows = [raw_row(task_id, index) for index, task_id in enumerate(ids)]
    rows[7]["task_id"] = rows[6]["task_id"]
    snapshot = write_snapshot(tmp_path / "raw", rows=rows)
    with pytest.raises(DatasetError, match="duplicate MBPP\\+ task_id|expected_task_ids"):
        convert_mbppplus(
            input_path=snapshot.input_path,
            revision=REVISION,
            source_manifest_path=snapshot.source_manifest_path,
            output_dir=tmp_path / "out",
        )


def test_convert_rejects_missing_withheld_field_and_unknown_field(tmp_path):
    ids = expected_task_ids()
    rows = [raw_row(task_id, index) for index, task_id in enumerate(ids)]
    del rows[0]["plus_input"]
    snapshot = write_snapshot(tmp_path / "raw", rows=rows)
    with pytest.raises(DatasetError, match="missing required fields"):
        convert_mbppplus(
            input_path=snapshot.input_path,
            revision=REVISION,
            source_manifest_path=snapshot.source_manifest_path,
            output_dir=tmp_path / "out",
        )

    rows = [raw_row(task_id, index) for index, task_id in enumerate(ids)]
    rows[0]["official_test"] = "assert True"
    snapshot = write_snapshot(tmp_path / "raw2", rows=rows)
    with pytest.raises(DatasetError, match="outside the pinned schema"):
        convert_mbppplus(
            input_path=snapshot.input_path,
            revision=REVISION,
            source_manifest_path=snapshot.source_manifest_path,
            output_dir=tmp_path / "out2",
        )


def test_convert_accepts_official_empty_plus_input_quirk(tmp_path):
    """Mbpp/793 in the pinned v0.2.0 corpus ships plus_input={} (zero plus tests)."""

    ids = expected_task_ids()
    rows = [raw_row(task_id, index) for index, task_id in enumerate(ids)]
    quirky_index = ids.index("Mbpp/793")
    rows[quirky_index]["plus_input"] = {}
    snapshot = write_snapshot(tmp_path / "raw", rows=rows)
    result = convert_mbppplus(
        input_path=snapshot.input_path,
        revision=REVISION,
        source_manifest_path=snapshot.source_manifest_path,
        output_dir=tmp_path / "full",
    )
    assert result.record_count == EXPECTED_RECORD_COUNT


def test_convert_rejects_non_empty_dict_plus_input(tmp_path):
    ids = expected_task_ids()
    rows = [raw_row(task_id, index) for index, task_id in enumerate(ids)]
    rows[0]["plus_input"] = {"unexpected": "shape"}
    snapshot = write_snapshot(tmp_path / "raw", rows=rows)
    with pytest.raises(DatasetError, match="invalid withheld field 'plus_input'"):
        convert_mbppplus(
            input_path=snapshot.input_path,
            revision=REVISION,
            source_manifest_path=snapshot.source_manifest_path,
            output_dir=tmp_path / "out",
        )


def test_convert_rejects_prompt_that_hides_entry_point(tmp_path):
    ids = expected_task_ids()
    rows = [raw_row(task_id, index) for index, task_id in enumerate(ids)]
    rows[0]["prompt"] = '"""\nWrite a function with an unnamed entry point.\n"""\n'
    snapshot = write_snapshot(tmp_path / "raw", rows=rows)
    with pytest.raises(DatasetError, match="must mention its entry_point"):
        convert_mbppplus(
            input_path=snapshot.input_path,
            revision=REVISION,
            source_manifest_path=snapshot.source_manifest_path,
            output_dir=tmp_path / "out",
        )


def test_selection_is_deterministic_ordered_and_exclusion_aware():
    ids = expected_task_ids()
    first = select_mbppplus_problem_ids(available_ids=ids, count=120, seed=SEED)
    second = select_mbppplus_problem_ids(available_ids=ids, count=120, seed=SEED)
    assert first == second
    assert len(first) == 120
    assert len(set(first)) == 120
    numbers = [int(item.split("/")[1]) for item in first]
    assert numbers == sorted(numbers)

    other_seed = select_mbppplus_problem_ids(available_ids=ids, count=120, seed=SEED + 1)
    assert other_seed != first

    excluded = first[:20]
    reduced = select_mbppplus_problem_ids(
        available_ids=ids,
        count=120,
        seed=SEED,
        exclude_ids=excluded,
    )
    assert not (set(reduced) & set(excluded))

    with pytest.raises(DatasetError, match="exceeds the available"):
        select_mbppplus_problem_ids(available_ids=ids, count=379, seed=SEED)
    with pytest.raises(DatasetError, match="unknown problem IDs"):
        select_mbppplus_problem_ids(available_ids=ids, count=1, seed=SEED, exclude_ids=["Mbpp/1"])
    with pytest.raises(DatasetError, match="duplicates"):
        select_mbppplus_problem_ids(available_ids=[ids[0], ids[0]], count=1, seed=SEED)


def test_sample_publishes_selection_bundle_bound_to_parent(tmp_path):
    _, conversion = _convert(tmp_path)
    result = sample_mbppplus(
        dataset_path=conversion.dataset_path,
        source_manifest_path=conversion.manifest_path,
        count=120,
        seed=SEED,
        output_dir=tmp_path / "sample120",
    )

    assert len(result.selected_problem_ids) == 120
    expected_ids = select_mbppplus_problem_ids(
        available_ids=expected_task_ids(), count=120, seed=SEED
    )
    assert result.selected_problem_ids == expected_ids

    manifest = json.loads(result.manifest_path.read_bytes())
    assert manifest["kind"] == SELECTION_MANIFEST_KIND
    assert manifest["selection"]["algorithm"] == SELECTION_ALGORITHM
    assert manifest["selection"]["seed"] == SEED
    assert manifest["selection"]["selected_problem_ids_sha256"] == ordered_problem_ids_sha256(
        list(expected_ids)
    )
    assert manifest["parent_manifest_sha256"] == conversion.manifest_sha256
    problems = load_problems(result.dataset_path)
    validate_mbppplus_public_problems(problems)
    assert [problem.problem_id for problem in problems] == list(expected_ids)
    for canary in PRIVATE_CANARIES:
        assert canary.encode() not in result.dataset_path.read_bytes()


def test_sample_rejects_tampered_full_projection(tmp_path):
    _, conversion = _convert(tmp_path)
    conversion.dataset_path.write_bytes(conversion.dataset_path.read_bytes() + b"\n")
    with pytest.raises(DatasetError, match="does not match its dataset manifest"):
        sample_mbppplus(
            dataset_path=conversion.dataset_path,
            source_manifest_path=conversion.manifest_path,
            count=10,
            seed=SEED,
            output_dir=tmp_path / "sample",
        )


def test_sample_exclude_manifest_yields_disjoint_cohort(tmp_path):
    _, conversion = _convert(tmp_path)
    first = sample_mbppplus(
        dataset_path=conversion.dataset_path,
        source_manifest_path=conversion.manifest_path,
        count=20,
        seed=SEED,
        output_dir=tmp_path / "cohort_a",
    )
    second = sample_mbppplus(
        dataset_path=conversion.dataset_path,
        source_manifest_path=conversion.manifest_path,
        count=20,
        seed=SEED,
        output_dir=tmp_path / "cohort_b",
        exclude_manifests=[first.manifest_path],
    )
    assert not (set(first.selected_problem_ids) & set(second.selected_problem_ids))
    manifest = json.loads(second.manifest_path.read_bytes())
    assert manifest["selection"]["excluded_manifests_count"] == 1
    assert manifest["selection"]["excluded_problem_ids"] == sorted(
        first.selected_problem_ids, key=lambda item: int(item.split("/")[1])
    )


def test_cli_convert_and_sample_mbppplus(tmp_path):
    snapshot = write_snapshot(tmp_path / "raw")
    runner = CliRunner()
    convert_result = runner.invoke(
        app,
        [
            "dataset",
            "convert-mbppplus",
            "--input",
            str(snapshot.input_path),
            "--revision",
            REVISION,
            "--manifest",
            str(snapshot.source_manifest_path),
            "--output-dir",
            str(tmp_path / "full"),
        ],
    )
    assert convert_result.exit_code == 0, convert_result.output
    assert "复制 canonical_solution/测试输入" in convert_result.output

    sample_result = runner.invoke(
        app,
        [
            "dataset",
            "sample-mbppplus",
            "--dataset",
            str(tmp_path / "full" / "problems.jsonl"),
            "--manifest",
            str(tmp_path / "full" / "dataset_manifest.json"),
            "--count",
            "5",
            "--seed",
            str(SEED),
            "--output-dir",
            str(tmp_path / "sample5"),
        ],
    )
    assert sample_result.exit_code == 0, sample_result.output
    assert "题目数" in sample_result.output
    assert (tmp_path / "sample5" / "dataset_manifest.json").exists()


def test_cli_convert_mbppplus_reports_failure_without_traceback(tmp_path):
    snapshot = write_snapshot(tmp_path / "raw")
    result = CliRunner().invoke(
        app,
        [
            "dataset",
            "convert-mbppplus",
            "--input",
            str(snapshot.input_path),
            "--revision",
            "0" * 40,
            "--manifest",
            str(snapshot.source_manifest_path),
            "--output-dir",
            str(tmp_path / "out"),
        ],
    )
    assert result.exit_code == 1
    assert "MBPP+ 转换失败" in result.output
    assert sha256_bytes(snapshot.raw_bytes)  # fixture sanity: raw snapshot untouched
