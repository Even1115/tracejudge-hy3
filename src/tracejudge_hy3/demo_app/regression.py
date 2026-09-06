"""Versioned public regression card for the contest demo.

This is a display/regression baseline, not a new model run.  It is rebuilt from
the hash-bound public counterfactual source, the published frozen report, and
the public replay receipt.  No private per-trace predictions are opened.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from tracejudge_hy3 import __version__

from .showcase import load_public_showcase

REPORT_PATH = Path("docs/releases/phase4/phase3_research_report_public_v1.md")
REPLAY_RECEIPT_PATH = Path("docs/releases/phase4/phase4_public_replay_receipt_v1.json")
CHART_MANIFEST_PATH = Path("docs/releases/phase4/charts/phase4_public_charts_v1/manifest.json")
DEFAULT_OUTPUT_DIR = Path("docs/releases/phase4/regression")

_MUTATION_ROWS = (
    "reasoning_swap",
    "code_defect",
    "boundary_deletion",
    "shortcut",
    "equivalent_implementation",
)


class RegressionCardError(ValueError):
    """Raised when a public regression source is missing or inconsistent."""


def _mutation_breakdown(report: str) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    for kind in _MUTATION_ROWS:
        match = re.search(
            rf"\|\s*{re.escape(kind)}\s*\|(?:\s*\d+/\d+\s*\|){{4}}\s*(\d+)/(\d+)\s*\|",
            report,
        )
        if match is None:
            raise RegressionCardError(f"published report is missing the {kind} Full result")
        result[kind] = (int(match.group(1)), int(match.group(2)))
    return result


def build_regression_card(
    repo_root: str | Path,
    *,
    version_label: str | None = None,
) -> dict[str, Any]:
    """Build the current public-fixture baseline card from published sources."""

    root = Path(repo_root)
    showcase = load_public_showcase(root)  # also verifies both public bundle hashes
    report_path = root / REPORT_PATH
    receipt_path = root / REPLAY_RECEIPT_PATH
    chart_manifest_path = root / CHART_MANIFEST_PATH
    if report_path.is_symlink() or not report_path.is_file():
        raise RegressionCardError("published Phase-3 report is missing")
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise RegressionCardError("public replay receipt is missing")
    if chart_manifest_path.is_symlink() or not chart_manifest_path.is_file():
        raise RegressionCardError("public chart manifest is missing")
    report = report_path.read_text(encoding="utf-8")
    rows = _mutation_breakdown(report)
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        chart_manifest = json.loads(chart_manifest_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegressionCardError("public replay receipt is invalid") from exc
    source_sha = showcase["source"]["counterfactual_sha256"]
    if (
        not isinstance(receipt, dict)
        or receipt.get("public_source_sha256") != source_sha
        or receipt.get("trace_id") != "counterfactual:safe_mean:boundary_deletion:v1"
    ):
        raise RegressionCardError("public replay receipt is bound to another source")
    methods = chart_manifest.get("methods") if isinstance(chart_manifest, dict) else None
    full_method = next(
        (
            item
            for item in methods or []
            if isinstance(item, dict) and item.get("method_id") == "full_tracejudge"
        ),
        None,
    )
    if not isinstance(full_method, dict) or not isinstance(
        full_method.get("provider_error_count"), int
    ):
        raise RegressionCardError("public chart manifest lacks Full TraceJudge accounting")

    faulty_kinds = _MUTATION_ROWS[:-1]
    detected = sum(rows[kind][0] for kind in faulty_kinds)
    faulty_total = sum(rows[kind][1] for kind in faulty_kinds)
    equivalent_correct, equivalent_total = rows["equivalent_implementation"]
    false_positives = equivalent_total - equivalent_correct
    replay_passed = bool(
        receipt.get("reproduced_failure") and receipt.get("evidence_hash_verified")
    )
    label = version_label or f"v{__version__}"
    return {
        "schema_version": 1,
        "card_id": f"tracejudge_public_regression_{label.replace('.', '_')}",
        "evaluator_version": label,
        "fixture_count": sum(denominator for _numerator, denominator in rows.values()),
        "fixture_scope": "3 个公开父题 × 5 种单因素反事实；仅作版本回归基线",
        "metrics": {
            "error_detection": {
                "numerator": detected,
                "denominator": faulty_total,
                "scope": "Full TraceJudge 在 12 条公开有错反事实上的冻结结果",
            },
            "false_positive_count": {
                "count": false_positives,
                "denominator": equivalent_total,
                "scope": "3 条 equivalent_implementation 负例",
            },
            "exact_first_step_localization": {
                "status": "not_computable",
                "reason": "公开聚合未暴露逐条方法预测，不能从汇总表重建该子集的精确定位率",
            },
            "counterexample_replay_pass": {
                "numerator": 1 if replay_passed else 0,
                "denominator": 1,
                "scope": "公开 boundary_deletion confirmed_bug receipt",
            },
            "provider_failure_count": {
                "count": full_method["provider_error_count"],
                "scope": "Full TraceJudge 冻结 57 条运行；公开报告记录 judgment availability=57/57",
            },
            "abstention_count": {
                "status": "not_recorded",
                "reason": "冻结方法输出 schema 未单列 abstention；不以缺失数据伪造 0",
            },
        },
        "source": {
            "public_counterfactual_sha256": source_sha,
            "report": REPORT_PATH.as_posix(),
            "replay_receipt": REPLAY_RECEIPT_PATH.as_posix(),
            "chart_manifest": CHART_MANIFEST_PATH.as_posix(),
            "replay_receipt_id": receipt.get("receipt_id"),
        },
        "verification_status": "ANALYZED",
        "confidence": "CAUTION",
        "boundary": (
            "这是已冻结公开结果的版本化回归基线，不是代码修改后的自动重跑结果；"
            "未来版本必须使用预先冻结的公开 fixture 输出新卡片，不得在正式 57 条 cohort 上边调边测。"
        ),
    }


def write_regression_card(
    repo_root: str | Path,
    *,
    version_label: str | None = None,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    force: bool = False,
) -> Path:
    root = Path(repo_root)
    card = build_regression_card(root, version_label=version_label)
    safe_label = re.sub(r"[^A-Za-z0-9._-]", "_", card["evaluator_version"])
    target_dir = root / output_dir
    target = target_dir / f"tracejudge_regression_{safe_label}.json"
    payload = (json.dumps(card, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    target_dir.mkdir(parents=True, exist_ok=True)
    if target.exists() and not force:
        if target.read_bytes() == payload:
            return target
        raise RegressionCardError(f"refusing to overwrite different card: {target}")
    temporary = target.with_suffix(".json.tmp")
    temporary.write_bytes(payload)
    temporary.replace(target)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version-label", default=None)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    path = write_regression_card(
        ".",
        version_label=args.version_label,
        output_dir=args.output_dir,
        force=args.force,
    )
    print(path.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
