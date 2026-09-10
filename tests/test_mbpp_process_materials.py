import json

import pytest

from tracejudge_hy3.process_eval_v2.materials import build_materials, canonical, digest, jsonl


def inputs(count=20):
    problems, generation, candidates, execution = [], [], [], []
    for i in range(count):
        task = f"Mbpp/{i}"
        code = "def f(x): return x"
        problems.append(
            {
                "problem_id": task,
                "title": "identity",
                "requirement": "return x",
                "function_signature": "def f(x):",
                "requirements": [],
                "reference_code": "PRIVATE_REFERENCE_CANARY",
                "hidden_test_cases": ["PRIVATE_TEST_CANARY"],
                "metadata": "PRIVATE_METADATA_CANARY",
            }
        )
        trace = {
            "problem_id": task,
            "code": code,
            "requirement_understanding": "return x",
            "design_summary": "identity",
            "implementation_steps": [
                {"step_id": "S1", "content": "return x", "related_requirements": []}
            ],
        }
        generation.append(
            {
                "problem_id": task,
                "run_id": "fixed",
                "status": "success",
                "solution_trace": trace,
                "raw_output": "PRIVATE_RAW_CANARY",
            }
        )
        candidates.append({"task_id": task, "candidate_id": f"c{i}", "code": code})
        execution.append(
            {
                "problem_id": task,
                "run_id": "fixed",
                "source_candidate": {
                    "task_id": task,
                    "candidate_id": f"c{i}",
                    "code_sha256": digest(code.encode()),
                },
                "solution_sha256": digest(code.encode()),
                "infrastructure_status": "ok",
                "base_status": "pass",
                "plus_status": "fail" if i % 3 == 0 else "pass",
                "passed_base": True,
                "passed_plus": i % 3 != 0,
                "hidden_failure_details": "PRIVATE_FAILURE_CANARY",
            }
        )
    return dict(
        problems_raw=jsonl(problems),
        generation_raw=jsonl(generation),
        candidates_raw=jsonl(candidates),
        execution_raw=jsonl(execution),
        run_id="fixed",
    )


def test_packets_are_blind_unlabelled_and_preserve_original_text():
    args = inputs()
    files, summary = build_materials(**args)
    assert summary["exported_count"] == 20
    assert summary["human_label_count"] == 0
    assert summary["split_counts"] == {"development": 6, "reserved_candidate": 14}
    assert b"CANARY" not in b"".join(files.values())
    original = json.loads(args["generation_raw"].splitlines()[0])["solution_trace"]
    original.pop("problem_id")
    parents = {"development": set(), "reserved_candidate": set()}
    for row in map(json.loads, files["coordinator/identity_and_outcomes.jsonl"].splitlines()):
        parents[row["split"]].add(row["parent_task_id"])
    assert parents["development"].isdisjoint(parents["reserved_candidate"])
    for name, raw in files.items():
        if name.endswith("items.jsonl"):
            assert b"passed_base" not in raw and b"stratum" not in raw and b"Mbpp/" not in raw
            for row in map(json.loads, raw.splitlines()):
                assert row["solution_trace"] == original
        if name.endswith("template.jsonl"):
            assert all(row["process_correct"] is None for row in map(json.loads, raw.splitlines()))


def test_export_is_deterministic_and_split_does_not_depend_on_outcomes():
    args = inputs()
    first, summary = build_materials(**args)
    assert (first, summary) == build_materials(**args)
    rows = [json.loads(line) for line in args["execution_raw"].splitlines()]
    for row in rows:
        row.update(passed_plus=True, plus_status="pass")
    args["execution_raw"] = jsonl(rows)
    second, _ = build_materials(**args)
    for name in first:
        if name.startswith(("development/", "reserved_candidate/")):
            assert first[name] == second[name]


@pytest.mark.parametrize("mutation", ["code", "hash", "duplicate", "run", "coverage"])
def test_inconsistent_sources_fail_closed(mutation):
    args = inputs()
    rows = [json.loads(line) for line in args["execution_raw"].splitlines()]
    if mutation == "code":
        generation = [json.loads(line) for line in args["generation_raw"].splitlines()]
        generation[0]["solution_trace"]["code"] += "\n# changed"
        args["generation_raw"] = jsonl(generation)
    elif mutation == "hash":
        rows[0]["solution_sha256"] = "wrong"
    elif mutation == "duplicate":
        rows.append(rows[0])
    elif mutation == "run":
        rows[0]["run_id"] = "other"
    else:
        rows.pop()
    args["execution_raw"] = jsonl(rows)
    with pytest.raises(ValueError):
        build_materials(**args)


def test_missing_original_steps_are_excluded_never_reconstructed():
    args = inputs()
    rows = [json.loads(line) for line in args["generation_raw"].splitlines()]
    rows[0]["solution_trace"]["implementation_steps"] = []
    args["generation_raw"] = jsonl(rows)
    files, summary = build_materials(**args)
    assert summary["excluded_count"] == 1
    assert summary["exported_count"] == 19
    assert "no_original_steps" in files["coordinator/exclusions.jsonl"].decode()


def test_timeout_is_separate_from_wrong_answer_selection():
    args = inputs(1)
    row = json.loads(args["execution_raw"].splitlines()[0])
    row.update(plus_status="timeout", passed_plus=False)
    args["execution_raw"] = canonical(row) + b"\n"
    _, summary = build_materials(**args)
    assert summary["stratum_counts"] == {"timeout": 1}
    assert summary["base_pass_plus_not_pass_count"] == 1
