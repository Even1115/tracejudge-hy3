"""Build the process-eval counterfactual capability bundle (3 mutations).

These samples exist because the 36-item development set contains no natural
plan-code mismatch and no step-level first fault. Each counterfactual mutates
exactly one aspect of a real development parent; the mutated code was NEVER
executed, functional evidence is honestly recorded as unavailable, and the
bundle is excluded from real-sample statistics by construction.

Fail-fast: every gold label is validated against the mutated trace with the
same PilotLabel + validate_label_context checks used for human annotations,
and each embedded parent is hash-checked against the coordinator mapping
before anything is written.

Run from the repository root:
    PYTHONPATH=src python scripts/build_process_counterfactuals.py
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tracejudge_hy3.process_eval_v2.contracts import PilotLabel  # noqa: E402
from tracejudge_hy3.process_eval_v2.materials import canonical, digest  # noqa: E402
from tracejudge_hy3.process_eval_v2.preflight import (  # noqa: E402
    by_id,
    read_rows,
)
from tracejudge_hy3.schemas.solution import SolutionTrace  # noqa: E402

PACKET_DIR = ROOT / "artifacts/process-annotations/mbpp120-process-v1"
OUT_PATH = ROOT / "data/process-eval/mbpp_process_counterfactuals_v1.json"
# Pinned in data/manifests/process_pilot_v1.json; a changed packet must fail here.
PACKET_MANIFEST_SHA256 = "f1d6733de1e6c8f9de8bf1f01a18c004848973dc2f085ed1acd5d59be1b003da"

# (parent item_id, expected parent Mbpp task) — pinned so a packet mix-up fails.
PARENTS = {
    "item-6d62dd49ffb4f26e4f5c": "Mbpp/470",  # add_pairwise
    "item-0dee1b9b51cee638d8d4": "Mbpp/426",  # filter_oddnumbers
    "item-ef37ddfc257260ca6517": "Mbpp/7",  # find_char_long
}

CONSTRUCTOR = "annotator_dev"


def _gold(
    item_id: str,
    *,
    reasoning: bool,
    aligned: bool,
    layer: str,
    step: str,
    error_type: str,
    location: dict,
    evidence_description: str,
    code_span: str,
    rationale: str,
) -> dict:
    return {
        "annotation_status": "reviewed",
        "annotator": CONSTRUCTOR,
        "error_type": error_type,
        "evidence": [
            {
                "step_id": step,
                "code_span": code_span,
                "requirement_id": "R1",
                "description": evidence_description,
            }
        ],
        "first_faulty_layer": layer,
        "first_faulty_location": location,
        "first_faulty_step": step,
        "item_id": item_id,
        "localization_status": "supported",
        "plan_code_aligned": aligned,
        "process_correct": False,
        "rationale": rationale,
        "reasoning_correct": reasoning,
    }


def _mutate_code(trace: dict, old: str, new: str) -> dict:
    mutated = copy.deepcopy(trace)
    if mutated["code"].count(old) != 1:
        raise SystemExit(f"mutation anchor not unique in code: {old!r}")
    mutated["code"] = mutated["code"].replace(old, new)
    return mutated


def _mutate_step(trace: dict, step_id: str, old: str, new: str) -> dict:
    mutated = copy.deepcopy(trace)
    steps = [s for s in mutated["implementation_steps"] if s["step_id"] == step_id]
    if len(steps) != 1:
        raise SystemExit(f"step not unique: {step_id}")
    step = steps[0]
    if old not in step["content"] or old not in step.get("expected_code_behavior", ""):
        raise SystemExit(f"step anchor missing: {old!r}")
    step["content"] = step["content"].replace(old, new)
    step["expected_code_behavior"] = step["expected_code_behavior"].replace(old, new)
    return mutated


def build_counterfactuals(items: dict[str, dict]) -> list[dict]:
    # CF-1: code deviates from a correct plan (pairing offset by two, plan says one).
    parent = items["item-6d62dd49ffb4f26e4f5c"]
    cf1_trace = _mutate_code(parent["solution_trace"], "zip(tup, tup[1:])", "zip(tup, tup[2:])")
    cf1 = {
        "trace_id": "counterfactual:item-6d62dd49ffb4f26e4f5c:pairing_offset:v1",
        "parent_item_id": "item-6d62dd49ffb4f26e4f5c",
        "mutation_kind": "code_pairing_offset",
        "sole_change": (
            "代码 L2 的 zip(tup, tup[1:]) 改为 zip(tup, tup[2:])；计划 S2/S3 不变"
            "（仍描述偏移一位的相邻对）。"
        ),
        "expected_impact": (
            "公开断言应失败（(1,5,7,8,10) 得到 (8,13,17) 而非 (6,12,15,18)），"
            "但变异代码从未执行，功能证据如实记为不可用；计划本身仍正确，"
            "失配只存在于计划与代码之间。"
        ),
        "gold_label": _gold(
            "counterfactual:item-6d62dd49ffb4f26e4f5c:pairing_offset:v1",
            reasoning=True,
            aligned=False,
            layer="alignment",
            step="S2",
            error_type="A01_PLAN_CODE_MISMATCH",
            location={
                "source_field": "implementation_steps",
                "step_id": "S2",
                "quote": "偏移一位",
            },
            evidence_description=(
                "计划 S2 要求 zip(tup, tup[1:]) 的相邻配对（'偏移一位'），代码 L2 实为 "
                "zip(tup, tup[2:])（跨位配对），代码偏离了计划。"
            ),
            code_span="L2",
            rationale=(
                "构造样本：计划（S2/S3）与需求一致且未变；代码配对偏移被改为 2，"
                "与计划的偏移 1 直接矛盾。金标签：reasoning 正确、plan-code 不一致，"
                "首错层 alignment，首错步骤 S2，A01。"
            ),
        ),
    }

    # CF-2: step inverts the filter condition; code follows the faulty step.
    parent = items["item-0dee1b9b51cee638d8d4"]
    cf2_trace = _mutate_step(parent["solution_trace"], "S2", "x % 2 != 0", "x % 2 == 0")
    cf2_trace = _mutate_code(cf2_trace, "x % 2 != 0", "x % 2 == 0")
    cf2 = {
        "trace_id": "counterfactual:item-0dee1b9b51cee638d8d4:condition_inversion:v1",
        "parent_item_id": "item-0dee1b9b51cee638d8d4",
        "mutation_kind": "step_condition_inversion",
        "sole_change": (
            "步骤 S2 的筛选条件由 x % 2 != 0 改为 x % 2 == 0（仍声称'筛选奇数元素'），"
            "代码 L2 同步改为实现该条件；需求理解不变。"
        ),
        "expected_impact": (
            "公开断言应失败（返回偶数 [2,4,6,8,10] 而非奇数），"
            "但变异代码从未执行，功能证据如实记为不可用；代码与（错误）计划一致，"
            "错误只存在于计划与需求之间，且落在具体步骤 S2 上。"
        ),
        "gold_label": _gold(
            "counterfactual:item-0dee1b9b51cee638d8d4:condition_inversion:v1",
            reasoning=False,
            aligned=True,
            layer="reasoning",
            step="S2",
            error_type="R01_REQUIREMENT_MISREAD",
            location={
                "source_field": "implementation_steps",
                "step_id": "S2",
                "quote": "x % 2 == 0",
            },
            evidence_description=(
                "需求要求 filter odd numbers；步骤 S2 的条件 x % 2 == 0 实际保留偶数，"
                "与需求直接矛盾；代码 L2 忠实实现了该错误步骤。"
            ),
            code_span="L2",
            rationale=(
                "构造样本：需求理解正确（'仅包含奇数'），但步骤 S2 把筛选条件写反，"
                "代码与错误步骤一致。金标签：reasoning 错误、plan-code 一致，"
                "首错层 reasoning，首错步骤 S2，R01。"
            ),
        ),
    }

    # CF-3: code tightens the length boundary the plan states as >= 4.
    parent = items["item-ef37ddfc257260ca6517"]
    cf3_trace = _mutate_code(parent["solution_trace"], "len(word) >= 4", "len(word) > 4")
    cf3 = {
        "trace_id": "counterfactual:item-ef37ddfc257260ca6517:boundary_tightening:v1",
        "parent_item_id": "item-ef37ddfc257260ca6517",
        "mutation_kind": "code_boundary_tightening",
        "sole_change": (
            "代码 L2 的 len(word) >= 4 改为 len(word) > 4；计划 S2 不变（仍要求 length >= 4）。"
        ),
        "expected_impact": (
            "公开断言应失败（'back' 恰为 4 字符被排除），"
            "但变异代码从未执行，功能证据如实记为不可用；计划仍正确，"
            "失配只存在于计划与代码之间。"
        ),
        "gold_label": _gold(
            "counterfactual:item-ef37ddfc257260ca6517:boundary_tightening:v1",
            reasoning=True,
            aligned=False,
            layer="alignment",
            step="S2",
            error_type="A01_PLAN_CODE_MISMATCH",
            location={
                "source_field": "implementation_steps",
                "step_id": "S2",
                "quote": "length >= 4",
            },
            evidence_description=(
                "计划 S2 要求保留 length >= 4 的词，代码 L2 实为 len(word) > 4，"
                "把恰好 4 字符的词错误排除，代码偏离了计划。"
            ),
            code_span="L2",
            rationale=(
                "构造样本：计划（S2 的 >= 4）与需求一致且未变；代码边界被收紧，"
                "与计划直接矛盾。金标签：reasoning 正确、plan-code 不一致，"
                "首错层 alignment，首错步骤 S2，A01。"
            ),
        ),
    }

    for cf, trace in ((cf1, cf1_trace), (cf2, cf2_trace), (cf3, cf3_trace)):
        parent = items[cf["parent_item_id"]]
        cf["problem"] = parent["problem"]
        cf["solution_trace"] = trace
        cf["execution_status"] = "not_executed_constructed"
        cf["functional_evidence"] = "unavailable_never_executed"
    return [cf1, cf2, cf3]


def main() -> None:
    items_raw = by_id(read_rows((PACKET_DIR / "development/annotator_a/items.jsonl").read_bytes()))
    mapping = by_id(
        read_rows((PACKET_DIR / "coordinator/identity_and_outcomes.jsonl").read_bytes())
    )
    packet_manifest_raw = (PACKET_DIR / "manifest.json").read_bytes()
    if digest(packet_manifest_raw) != PACKET_MANIFEST_SHA256:
        raise SystemExit("source packet differs from the pinned manifest")

    parents = []
    for item_id, task_prefix in PARENTS.items():
        material = items_raw[item_id]
        origin = mapping[item_id]
        if digest(canonical(material)) != origin["packet_item_sha256"]:
            raise SystemExit(f"parent binding mismatch: {item_id}")
        if not material["problem"]["title"].startswith(task_prefix):
            raise SystemExit(f"parent identity mismatch: {item_id}")
        parents.append(
            {
                "item_id": item_id,
                "parent_task_id": origin["parent_task_id"],
                "packet_item_sha256": origin["packet_item_sha256"],
                "problem": material["problem"],
                "solution_trace": material["solution_trace"],
            }
        )

    counterfactuals = build_counterfactuals(items_raw)
    for cf in counterfactuals:
        label = PilotLabel.model_validate(cf["gold_label"])
        # Validate the gold label against the MUTATED public context.
        solution = SolutionTrace.model_validate(
            {"problem_id": cf["trace_id"], **cf["solution_trace"]}
        )
        requirements = {req["requirement_id"] for req in cf["problem"]["requirements"]}
        steps = {s.step_id for s in solution.implementation_steps}
        if label.first_faulty_step not in steps:
            raise SystemExit(f"gold step unknown: {cf['trace_id']}")
        label.first_faulty_location.validate_against(solution)
        for evidence in label.evidence:
            if evidence.requirement_id not in requirements:
                raise SystemExit(f"gold requirement unknown: {cf['trace_id']}")
            if evidence.step_id is not None and evidence.step_id not in steps:
                raise SystemExit(f"gold evidence step unknown: {cf['trace_id']}")
            if evidence.code_span is not None:
                if not 1 <= int(evidence.code_span[1:]) <= len(solution.code.splitlines()):
                    raise SystemExit(f"gold code span invalid: {cf['trace_id']}")

    bundle = {
        "schema": "tracejudge-process-counterfactuals-v1",
        "bundle_id": "mbpp-process-counterfactuals-3-20260909-v1",
        "created": "2026-09-09",
        "constructed": True,
        "execution_policy": (
            "counterfactual code was never executed; functional evidence is "
            "honestly recorded as unavailable and must not be estimated or backfilled"
        ),
        "statistics_policy": (
            "constructed capability probes only; excluded from every real-sample "
            "statistic (detection rates, paired comparisons, leaderboards)"
        ),
        "motivation": (
            "the 36-item development set contains no natural plan-code mismatch "
            "and no step-level first fault; see "
            "artifacts/annotation-development/mbpp36-dev-20260909-v1/review_notes.md"
        ),
        "source_packet": {
            "path": "artifacts/process-annotations/mbpp120-process-v1/manifest.json",
            "sha256": digest(packet_manifest_raw),
        },
        "parents": parents,
        "counterfactuals": counterfactuals,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {OUT_PATH.relative_to(ROOT)} sha256={digest(OUT_PATH.read_bytes())}")
    print(f"parents={len(parents)} counterfactuals={len(counterfactuals)}")


if __name__ == "__main__":
    main()
