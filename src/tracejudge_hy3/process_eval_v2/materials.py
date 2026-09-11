"""Build blind annotation packets from verified public inputs and frozen traces.

No candidate execution, model calls, reference solutions or hidden-test loading.
Outcome strata are selection metadata, never human process labels.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter

from tracejudge_hy3.schemas.solution import SolutionTrace


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def jsonl(rows: list[dict]) -> bytes:
    return b"".join(canonical(row) + b"\n" for row in rows)


def _indexed(raw: bytes, key: str) -> dict[str, dict]:
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    result = {}
    for row in rows:
        identity = row[key]
        if not isinstance(identity, str) or identity in result:
            raise ValueError(f"invalid or duplicate {key}")
        result[identity] = row
    return result


def _rank(seed: int, purpose: str, identity: str) -> str:
    return digest(f"{seed}\0{purpose}\0{identity}".encode())


def _stratum(execution: dict) -> str:
    if execution["infrastructure_status"] != "ok":
        return "infrastructure_error"
    if "timeout" in (execution["base_status"], execution["plus_status"]):
        return "timeout"
    if execution["passed_base"] and execution["passed_plus"]:
        return "tests_passed_process_unlabelled"
    if execution["passed_base"]:
        return "base_pass_plus_fail"
    return "base_fail"


def build_materials(
    *,
    problems_raw: bytes,
    generation_raw: bytes,
    candidates_raw: bytes,
    execution_raw: bytes,
    run_id: str,
    seed: int = 20260908,
) -> tuple[dict[str, bytes], dict]:
    problems = _indexed(problems_raw, "problem_id")
    generation = _indexed(generation_raw, "problem_id")
    candidates = _indexed(candidates_raw, "task_id")
    execution = _indexed(execution_raw, "problem_id")
    if not (set(problems) == set(generation) == set(candidates) == set(execution)):
        raise ValueError("public tasks, traces, candidates and executions must have identical IDs")
    # Partition by parent BEFORE inspecting outcomes. This is a reserved candidate
    # pool, not a certified unseen test set; near-duplicate/history audit is pending.
    parents = sorted(problems, key=lambda task: _rank(seed, "parent-split", task))
    development = set(parents[: max(1, len(parents) * 30 // 100)])
    items, mapping, excluded = {}, [], []
    for task_id in sorted(problems):
        response, candidate, result = generation[task_id], candidates[task_id], execution[task_id]
        if response["run_id"] != run_id or result["run_id"] != run_id:
            raise ValueError("mixed run identities")
        code_hash = digest(candidate["code"].encode("utf-8"))
        source = result["source_candidate"]
        if (
            source["task_id"] != task_id
            or source["candidate_id"] != candidate["candidate_id"]
            or source["code_sha256"] != code_hash
            or result["solution_sha256"] != code_hash
        ):
            raise ValueError("candidate/execution code binding mismatch")
        original = response.get("solution_trace")
        if response["status"] != "success" or original is None:
            excluded.append({"parent_task_id": task_id, "reason": "original_trace_unavailable"})
            continue
        solution = SolutionTrace.model_validate(original)
        if solution.problem_id != task_id or solution.code != candidate["code"]:
            raise ValueError("original trace differs from the executed candidate")
        if not solution.implementation_steps:
            excluded.append({"parent_task_id": task_id, "reason": "no_original_steps"})
            continue
        problem = problems[task_id]
        requirements = [
            {"requirement_id": row["requirement_id"], "content": row["content"]}
            for row in problem["requirements"]
        ]
        requirement_ids = {row["requirement_id"] for row in requirements}
        if any(
            set(step.related_requirements) - requirement_ids
            for step in solution.implementation_steps
        ):
            raise ValueError("original trace has unknown requirement IDs")
        item_id = "item-" + _rank(seed, "opaque-item", task_id)[:20]
        # Preserve original explanation/code values; remove the identifying field
        # only. Never include raw_output or arbitrary source/metadata dictionaries.
        trace = {key: value for key, value in original.items() if key != "problem_id"}
        items[item_id] = {
            "item_id": item_id,
            "problem": {
                "title": problem["title"],
                "requirement": problem["requirement"],
                "function_signature": problem["function_signature"],
                "requirements": requirements,
            },
            "solution_trace": trace,
        }
        mapping.append(
            {
                "item_id": item_id,
                "parent_task_id": task_id,
                "run_id": run_id,
                "split": "development" if task_id in development else "reserved_candidate",
                "stratum": _stratum(result),
                "code_sha256": code_hash,
                "original_solution_trace_sha256": digest(canonical(original)),
                "generation_record_sha256": digest(canonical(response)),
                "execution_record_sha256": digest(canonical(result)),
                "packet_item_sha256": digest(canonical(items[item_id])),
                "base_status": result["base_status"],
                "plus_status": result["plus_status"],
                "passed_base": result["passed_base"],
                "passed_plus": result["passed_plus"],
                "infrastructure_status": result["infrastructure_status"],
            }
        )
    if len(items) != len(mapping):
        raise ValueError("opaque identity collision")
    # A small developer pilot may use outcome strata; this selection never moves
    # a parent out of the reserved candidate partition.
    pool = [row for row in mapping if row["split"] == "development"]
    pilot = []
    for stratum, count in (
        ("base_pass_plus_fail", 6),
        ("tests_passed_process_unlabelled", 5),
        ("timeout", 1),
    ):
        selected = sorted(
            (row for row in pool if row["stratum"] == stratum),
            key=lambda row: _rank(seed, "pilot", row["item_id"]),
        )
        pilot.extend(row["item_id"] for row in selected[:count])
    remainder = sorted(
        (row for row in pool if row["item_id"] not in pilot),
        key=lambda row: _rank(seed, "pilot", row["item_id"]),
    )
    pilot.extend(row["item_id"] for row in remainder[: max(0, 12 - len(pilot))])
    files = {
        "coordinator/identity_and_outcomes.jsonl": jsonl(mapping),
        "coordinator/exclusions.jsonl": jsonl(excluded),
    }
    for split in ("development", "reserved_candidate", "pilot"):
        selected_ids = [
            row["item_id"]
            for row in mapping
            if (row["item_id"] in pilot if split == "pilot" else row["split"] == split)
        ]
        for annotator in ("annotator_a", "annotator_b"):
            ordered = sorted(selected_ids, key=lambda item: _rank(seed, annotator, item))
            prefix = f"{split}/{annotator}"
            files[f"{prefix}/items.jsonl"] = jsonl([items[item] for item in ordered])
            files[f"{prefix}/labels.template.jsonl"] = jsonl(
                [
                    {
                        "item_id": item,
                        "annotation_status": "unreviewed",
                        "reasoning_correct": None,
                        "plan_code_aligned": None,
                        "process_correct": None,
                        "localization_status": "unreviewed",
                        "first_faulty_layer": None,
                        "first_faulty_step": None,
                        "error_type": None,
                        "evidence": [],
                        "rationale": "",
                        "annotator": "",
                    }
                    for item in ordered
                ]
            )
    summary = {
        "schema": "mbpp-process-materials-v1",
        "source_run_id": run_id,
        "seed": seed,
        "source_parent_count": len(parents),
        "exported_count": len(items),
        "excluded_count": len(excluded),
        "pilot_count": len(pilot),
        "split_counts": dict(Counter(row["split"] for row in mapping)),
        "stratum_counts": dict(Counter(row["stratum"] for row in mapping)),
        "base_pass_plus_not_pass_count": sum(
            row["passed_base"] and not row["passed_plus"] for row in mapping
        ),
        "human_label_count": 0,
        "solver_calls": 0,
        "judge_calls": 0,
        "candidate_executions": 0,
        "limitations": [
            "functional outcomes are not process gold labels",
            "reserved candidates require history and near-duplicate audit before held-out use",
            "pilot is a subset of development, not extra independent samples",
            "opaque IDs hide explicit source identity, not recognisable benchmark content",
            "packets omit official tests, reference code and provider raw output",
        ],
    }
    return files, summary
