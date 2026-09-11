"""Export exactly two reviewed cases for the demo; no model or candidate execution."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path("docs/releases/featured_process_cases_v1.json")
TUPLE_ID = "item-89cda539ad51dde89041"
PROBE_ID = "probe-07c8d275bebcd1296b3c"


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def build(root: Path) -> dict:
    sources = {}

    def read(key: str, relative: str):
        raw = (root / relative).read_bytes()
        value = (
            [json.loads(line) for line in raw.decode("utf-8-sig").splitlines() if line.strip()]
            if relative.endswith(".jsonl")
            else json.loads(raw)
        )
        sources[key] = {
            "path": relative,
            "sha256": digest(raw),
            "content_sha256": digest(canonical(value)),
        }
        return value

    def one(rows: list, key: str, value: str) -> dict:
        matches = [row for row in rows if row.get(key) == value]
        if len(matches) != 1:
            raise ValueError(f"Expected one {key}={value}")
        return matches[0]

    packet_root = "artifacts/process-annotations/mbpp120-process-v1/"
    manifest = json.loads((root / packet_root / "manifest.json").read_bytes())
    packet = one(
        read("tuple_input", packet_root + "development/annotator_a/items.jsonl"),
        "item_id",
        TUPLE_ID,
    )
    outcome = one(
        read("tuple_outcome", packet_root + "coordinator/identity_and_outcomes.jsonl"),
        "item_id",
        TUPLE_ID,
    )
    for key in ("tuple_input", "tuple_outcome"):
        relative = sources[key]["path"].removeprefix(packet_root)
        if sources[key]["sha256"] != manifest["files_sha256"][relative]:
            raise ValueError("Packet manifest mismatch")
    revision_path = "artifacts/annotation-development/mbpp36-dev-20260910-v2/revisions.jsonl"
    revision = one(read("tuple_revision", revision_path), "item_id", TUPLE_ID)
    config = json.loads((root / "data/manifests/process_development_labels_v2.json").read_bytes())
    if sources["tuple_revision"]["sha256"] != config["revisions"]["sha256"]:
        raise ValueError("Label revision hash mismatch")
    probe_bundle = read("probe_input", "data/process-eval/mbpp_process_counterfactuals_v1.json")
    probe = one(probe_bundle["counterfactuals"], "mutation_kind", "code_boundary_tightening")
    judgments = []
    for key, label, run in (
        ("probe_baseline", "基线", "probes3-baseline-location-20260910-v1"),
        ("probe_audit", "需求—假设核对", "probes3-assumptions-location-20260910-v1"),
    ):
        directory = "artifacts/experiments/process-method-v2/" + run
        report = json.loads((root / directory / "run-report.json").read_bytes())
        pred = one(read(key, directory + "/predictions.jsonl"), "item_id", PROBE_ID)
        if sources[key]["sha256"] != report["files_sha256"]["predictions.jsonl"]:
            raise ValueError("Prediction hash mismatch")
        assessment = pred["judge_raw"]
        if assessment["first_faulty_location"]["quote"] not in probe["solution_trace"]["code"]:
            raise ValueError("Prediction quote not bound to code")
        judgments.append({"method": label, "run_id": run, "source": key, "assessment": assessment})

    label_fields = (
        "reasoning_correct",
        "plan_code_aligned",
        "process_correct",
        "error_type",
        "first_faulty_layer",
        "first_faulty_step",
        "first_faulty_location",
    )
    tuple_trace = packet["solution_trace"]
    tuple_label = revision["label"]
    if tuple_label["first_faulty_location"]["quote"] != tuple_trace["edge_cases_considered"][1]:
        raise ValueError("Tuple claim is not verbatim")
    if digest(tuple_trace["code"].encode()) != outcome["code_sha256"]:
        raise ValueError("Functional outcome does not bind this code")
    if (outcome["base_status"], outcome["plus_status"]) != ("pass", "pass"):
        raise ValueError("Unexpected official outcome")
    return {
        "schema": "tracejudge-featured-process-cases-v1",
        "date": "2026-09-10",
        "sources": sources,
        "cases": [
            {
                "id": "tuple_str_int",
                "item_id": TUPLE_ID,
                "title": "官方测试通过，边界承诺仍有缺陷",
                "kind": "MBPP 开发样本 · 预测后标签复核 v2",
                "conclusion": "算法没有处理尾随逗号，无法兑现自己对单元素元组的承诺。代码与 S1/S2 一致，审核归为推理层算法错误。",
                "problem": packet["problem"],
                "solution": tuple_trace,
                "assessment": {key: tuple_label[key] for key in label_fields},
                "functional": {
                    key: outcome[key]
                    for key in ("base_status", "plus_status", "run_id", "code_sha256")
                },
                "claim": {
                    "label": "边界说明原文 · edge_cases_considered[1]",
                    "text": tuple_trace["edge_cases_considered"][1],
                    "highlight": "(5,)",
                },
                "code_highlight": "int(x.strip())",
                "code_line": 5,
                "analysis": '静态推演（未执行）："(5,)" → 内部字符串 "5," → 按逗号分割为 "5" 和空字符串 → 空字符串不能转换为整数。历史官方测试通过只覆盖既有用例，不证明这个额外承诺。',
                "limitations": [
                    "仅静态推演；未运行这个边界输入，没有新增功能测试或可重放错误证书。",
                    "标签经过预测后单人 AI 辅助复核，已接触两方法预测，供负责人审核；不是独立盲审或保留集金标。",
                    "首错位置是边界说明条目，首错步骤为 null；S2 与 L5 是相关实现依据，不是步骤首错命中。",
                ],
                "source_ids": ["tuple_input", "tuple_outcome", "tuple_revision"],
                "review_doc": "docs/experiments/process-method-validation-v2-repair.md",
                "judgments": [],
            },
            {
                "id": "find_char_long",
                "item_id": PROBE_ID,
                "title": "计划正确，代码收紧了长度边界",
                "kind": "构造探针 · code_boundary_tightening",
                "conclusion": "计划要求保留长度至少为 4 的词，代码只保留长度大于 4 的词。审核归为对齐层计划—代码失配，A01。",
                "problem": probe["problem"],
                "solution": probe["solution_trace"],
                "assessment": {key: probe["gold_label"][key] for key in label_fields},
                "functional": {"base_status": None, "plus_status": None, "run_id": None},
                "claim": {
                    "label": "计划原文 · S2",
                    "text": probe["solution_trace"]["implementation_steps"][1]["content"],
                    "highlight": ">= 4",
                },
                "code_highlight": "> 4",
                "code_line": 2,
                "analysis": '静态对照（未执行）：计划包含恰好 4 个字符的词；代码条件会排除它们，例如公开题面中的 "move" 和 "back"。唯一变异是代码从 >= 4 改为 > 4，计划保持不变。',
                "limitations": [
                    "这是构造的能力探针，不是自然错误样本；变异代码从未执行，官方功能证据不可用。",
                    "冻结金标的 S2 表示发生失配的相关计划步骤，不表示计划自身推理错误。",
                    "新定位规范下两方法均引用代码 L2，首错步骤为 null。代码定位与步骤定位分开报告，不能据此声称精确位置命中。",
                ],
                "source_ids": ["probe_input", "probe_baseline", "probe_audit"],
                "review_doc": "docs/experiments/process-location-format-v2.md",
                "judgments": judgments,
            },
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    result = build(ROOT)
    target = ROOT / OUTPUT
    if args.check:
        if canonical(json.loads(target.read_bytes())) != canonical(result):
            raise SystemExit("Featured case bundle differs from the bound sources")
    else:
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{OUTPUT}: canonical SHA-256 {digest(canonical(result))}; model calls=0; executions=0")


if __name__ == "__main__":
    main()
