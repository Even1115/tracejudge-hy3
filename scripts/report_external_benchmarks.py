#!/usr/bin/env python3
"""Write an aggregate-only MBPP+/LCB report suitable for the interim report."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def rate(value):
    if value["value"] is None:
        return "未执行"
    return f"{value['numerator']}/{value['denominator']} = {value['value']:.1%}"


def render(mbpp, lcb):
    lines = [
        "# MBPP+ 与 LiveCodeBench 追加评测结果",
        "",
        "本节可追加至双数据集阶段报告。两个数据集均为固定子集、单候选生成实验；",
        "生成覆盖与官方执行通过率分别报告，基础设施失败保留在来源 cohort 分母中。",
        "",
        "| 数据集 | 完整执行 | 生成覆盖 | 端到端功能通过 | 条件于实际执行 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, report in (("MBPP+", mbpp), ("LiveCodeBench", lcb)):
        if report is None:
            lines.append(f"| {name} | 待提供结果 | — | — | — |")
            continue
        prefix = "base_plus_pass" if name == "MBPP+" else "pass"
        lines.append(
            f"| {name} | {'完成' if report['complete'] else '未完成'} | "
            f"{rate(report['generation_coverage'])} | {rate(report[prefix + '_full_denominator'])} | "
            f"{rate(report[prefix + '_conditional_on_execution'])} |"
        )
    if lcb is not None:
        lines += [
            "",
            "## LiveCodeBench 分难度",
            "",
            "| 难度 | 来源题数 | 功能通过 |",
            "| --- | --- | --- |",
        ]
        for difficulty, result in lcb["per_difficulty"].items():
            lines.append(
                f"| {difficulty} | {result['planned_n']} | {rate(result['pass_full_denominator'])} |"
            )
    lines += [
        "",
        "## 完整聚合计数与区间",
        "",
        "以下 JSON 仅包含聚合指标，不包含候选代码、模型原始响应或隐藏测试。",
        "",
        "```json",
        json.dumps({"mbppplus": mbpp, "livecodebench": lcb}, ensure_ascii=False, indent=2),
        "```",
        "",
        "这些结果不构成完整数据集排行榜分数，也不直接证明过程判断或错误证书能力。",
        "LiveCodeBench 是 easy/medium/hard 各 20 题的平衡子集，不能当作自然题目分布下的总体通过率。",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mbpp-run", type=Path)
    parser.add_argument("--lcb-run", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not (args.mbpp_run or args.lcb_run):
        parser.error("provide at least one completed/in-progress run")
    reports = []
    hashes = {}
    for name, folder in (("mbppplus", args.mbpp_run), ("livecodebench", args.lcb_run)):
        if folder is None:
            reports.append(None)
            continue
        path = folder / "report.json"
        raw = path.read_bytes()
        report = json.loads(raw)
        expected = (
            "mbpp120-combined-report-v1"
            if name == "mbppplus"
            else "lcb60-generation-execution-report-v1"
        )
        if report.get("schema") != expected:
            raise ValueError("unsupported report schema")
        reports.append(report)
        hashes[name] = hashlib.sha256(raw).hexdigest()
    text = (
        render(*reports)
        + "\n输入报告 SHA256：\n\n```json\n"
        + json.dumps(hashes, indent=2)
        + "\n```\n"
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Explicit exclusive output avoids overwriting a previously reviewed report.
    with args.output.open("x", encoding="utf-8") as handle:
        handle.write(text)
    print(args.output.resolve())
