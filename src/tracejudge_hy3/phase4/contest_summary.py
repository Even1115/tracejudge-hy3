"""Deterministic, aggregate-only contest summary and difficulty-proxy analysis.

The difficulty proxy is intentionally independent of model outputs and human labels.
It ranks the frozen 45-task source cohort by structural features of the benchmark's
canonical solution, splits that pre-outcome ranking into equal thirds, and only then
joins the 42 included natural traces to their frozen EvalPlus execution status.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from statistics import median
from typing import Any

RAW_SNAPSHOT_RELATIVE_PATH = Path("artifacts/datasets/raw/humanevalplus/test.jsonl")
SOURCE_COHORT_RELATIVE_PATH = Path(
    "artifacts/datasets/processed/humanevalplus-research-natural-45/problems.jsonl"
)
NATURAL_MANIFEST_RELATIVE_PATH = Path(
    "artifacts/experiments/phase3-freezes/phase3_natural_42_v1/manifest.json"
)
CHART_MANIFEST_RELATIVE_PATH = Path(
    "docs/releases/phase4/charts/phase4_public_charts_v1/manifest.json"
)
STATISTICS_REPORT_RELATIVE_PATH = Path(
    "artifacts/experiments/phase3-statistics/phase3_stats_primary_round1_v1/report.json"
)
P1_COMMITMENT_RELATIVE_PATH = Path(
    "docs/experiments/phase4_p1_formal_subset/phase4_p1_formal_subset_v1/commitment.json"
)
P1_AGREEMENT_MANIFEST_RELATIVE_PATH = Path(
    "artifacts/experiments/phase4-p1-agreement/phase4_p1_inter_rater_agreement_v1/manifest.json"
)
P1_AGREEMENT_ANALYSIS_RELATIVE_PATH = P1_AGREEMENT_MANIFEST_RELATIVE_PATH.with_name(
    "agreement.json"
)

RAW_SNAPSHOT_SHA256 = "908377f1daf28dcb36846db73a5662b2e05a9907407c2696c89ad9d3b0b04492"
SOURCE_COHORT_SHA256 = "701ed34b3a66032f0f356734607709fb3d65f753dbe01cf4b4395c4409df2dc0"
NATURAL_MANIFEST_SHA256 = "a4116a7ddb7ac910b79bd52e9530db79dd0f05c9edee8ecd947fc78c35c03692"
CHART_MANIFEST_SHA256 = "20d94ad514400ff7ebe72b8d288eb6a208b571069878091b4b6b481659f30d71"
STATISTICS_REPORT_SHA256 = "972e7c0f5eac36d59035ec65376133fbcc0dfa941281e97fb7dcc70f02360a10"
P1_COMMITMENT_SHA256 = "b5090ad78715857455852e3450fa606f4963ca726a3df91a1b6603d372c491a2"
P1_AGREEMENT_MANIFEST_SHA256 = "20d11548ed638c34bb9054d12893e28bd5c18e3028091dc5186e914182471c76"
P1_AGREEMENT_ANALYSIS_SHA256 = "fe9c66d505c0ce472deb652676ac38ea4d6849547323a1e3061ad1d9deea2135"

DIFFICULTY_PROXY_ID = "canonical_solution_structure_tertiles_v1"
DIFFICULTY_TIER_NAMES = ("easy-proxy", "medium-proxy", "hard-proxy")
DIFFICULTY_TIE_BREAK_SALT = "tracejudge_difficulty_proxy_v1"

_CONTROL_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Try,
    ast.IfExp,
    ast.Match,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)


class ContestSummaryError(ValueError):
    """Raised when frozen inputs do not match the expected release identities."""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_hash_bound(path: Path, expected_sha256: str, *, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ContestSummaryError(f"{label} must be a regular file")
    payload = path.read_bytes()
    actual = _sha256(payload)
    if actual != expected_sha256:
        raise ContestSummaryError(
            f"{label} SHA256 differs: expected {expected_sha256}, got {actual}"
        )
    return payload


def _decode_json(payload: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ContestSummaryError(f"{label} is not valid UTF-8 JSON") from None
    if not isinstance(value, dict):
        raise ContestSummaryError(f"{label} must contain one JSON object")
    return value


def _decode_jsonl(payload: bytes, *, label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise ContestSummaryError(f"{label} is not valid UTF-8") from None
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            raise ContestSummaryError(f"{label} contains a blank line at {line_number}")
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            raise ContestSummaryError(
                f"{label} contains invalid JSON at line {line_number}"
            ) from None
        if not isinstance(value, dict):
            raise ContestSummaryError(f"{label} line {line_number} is not an object")
        rows.append(value)
    return rows


def _find_function(tree: ast.Module, entry_point: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == entry_point:
            return node
    raise ContestSummaryError(f"canonical program is missing entry point {entry_point!r}")


def _max_control_depth(node: ast.AST, depth: int = 0) -> int:
    current = depth + 1 if isinstance(node, _CONTROL_NODES) else depth
    return max(
        (current, *(_max_control_depth(child, current) for child in ast.iter_child_nodes(node)))
    )


def _canonical_features(record: Mapping[str, Any]) -> dict[str, int]:
    task_id = record.get("task_id")
    prompt = record.get("prompt")
    canonical_solution = record.get("canonical_solution")
    entry_point = record.get("entry_point")
    if not all(
        isinstance(value, str) for value in (task_id, prompt, canonical_solution, entry_point)
    ):
        raise ContestSummaryError("raw benchmark row is missing a required string field")
    try:
        tree = ast.parse(prompt + canonical_solution)
    except SyntaxError:
        raise ContestSummaryError(f"canonical program for {task_id} does not parse") from None
    function = _find_function(tree, entry_point)
    canonical_sloc = sum(
        bool(line.strip()) and not line.lstrip().startswith("#")
        for line in canonical_solution.splitlines()
    )
    control_flow_count = sum(isinstance(node, _CONTROL_NODES) for node in ast.walk(function))
    ast_node_count = sum(1 for _node in ast.walk(function))
    parameter_count = (
        len(function.args.posonlyargs)
        + len(function.args.args)
        + len(function.args.kwonlyargs)
        + int(function.args.vararg is not None)
        + int(function.args.kwarg is not None)
    )
    max_control_depth = _max_control_depth(function)
    complexity_score = (
        10 * canonical_sloc
        + 20 * control_flow_count
        + 10 * max_control_depth
        + ast_node_count
        + 5 * max(0, parameter_count - 1)
    )
    return {
        "canonical_sloc": canonical_sloc,
        "control_flow_count": control_flow_count,
        "max_control_depth": max_control_depth,
        "ast_node_count": ast_node_count,
        "parameter_count": parameter_count,
        "complexity_score": complexity_score,
    }


def _wilson_95(numerator: int, denominator: int) -> tuple[float, float]:
    if denominator <= 0 or numerator < 0 or numerator > denominator:
        raise ContestSummaryError("invalid proportion")
    z = 1.959963984540054
    estimate = numerator / denominator
    scale = 1 + z**2 / denominator
    center = (estimate + z**2 / (2 * denominator)) / scale
    radius = (
        z * math.sqrt(estimate * (1 - estimate) / denominator + z**2 / (4 * denominator**2)) / scale
    )
    return center - radius, center + radius


def _tier_index(position: int, total: int) -> int:
    if total % 3:
        raise ContestSummaryError("source cohort must divide evenly into three tiers")
    return min(position // (total // 3), 2)


def build_difficulty_analysis(repo_root: str | Path) -> dict[str, Any]:
    """Build an aggregate difficulty-proxy analysis from hash-bound frozen inputs."""

    root = Path(repo_root)
    raw_rows = _decode_jsonl(
        _read_hash_bound(
            root / RAW_SNAPSHOT_RELATIVE_PATH,
            RAW_SNAPSHOT_SHA256,
            label="HumanEval+ raw snapshot",
        ),
        label="HumanEval+ raw snapshot",
    )
    source_rows = _decode_jsonl(
        _read_hash_bound(
            root / SOURCE_COHORT_RELATIVE_PATH,
            SOURCE_COHORT_SHA256,
            label="45-task source cohort",
        ),
        label="45-task source cohort",
    )
    natural_manifest = _decode_json(
        _read_hash_bound(
            root / NATURAL_MANIFEST_RELATIVE_PATH,
            NATURAL_MANIFEST_SHA256,
            label="42-trace natural manifest",
        ),
        label="42-trace natural manifest",
    )

    raw_by_id = {row.get("task_id"): row for row in raw_rows}
    if len(raw_by_id) != len(raw_rows) or not all(isinstance(key, str) for key in raw_by_id):
        raise ContestSummaryError("raw snapshot task IDs are invalid or duplicated")
    source_problem_ids = [row.get("problem_id") for row in source_rows]
    if len(source_problem_ids) != 45 or not all(
        isinstance(problem_id, str) for problem_id in source_problem_ids
    ):
        raise ContestSummaryError("source cohort must contain 45 valid problem IDs")
    if len(set(source_problem_ids)) != len(source_problem_ids):
        raise ContestSummaryError("source cohort problem IDs are duplicated")
    if any(problem_id not in raw_by_id for problem_id in source_problem_ids):
        raise ContestSummaryError("source cohort is not covered by the raw snapshot")

    ranked: list[dict[str, Any]] = []
    for problem_id in source_problem_ids:
        features = _canonical_features(raw_by_id[problem_id])
        ranked.append(
            {
                "problem_id": problem_id,
                "features": features,
                "tie_break": _sha256(f"{DIFFICULTY_TIE_BREAK_SALT}\0{problem_id}".encode()),
            }
        )
    ranked.sort(key=lambda item: (item["features"]["complexity_score"], item["tie_break"]))
    tier_by_problem_id: dict[str, str] = {}
    for position, item in enumerate(ranked):
        tier_by_problem_id[item["problem_id"]] = DIFFICULTY_TIER_NAMES[
            _tier_index(position, len(ranked))
        ]

    traces = natural_manifest.get("traces")
    if not isinstance(traces, list) or len(traces) != 42:
        raise ContestSummaryError("natural manifest must contain 42 traces")
    evidence_by_problem_id: dict[str, Mapping[str, Any]] = {}
    for trace in traces:
        if not isinstance(trace, dict):
            raise ContestSummaryError("natural trace record is invalid")
        problem_id = trace.get("problem_id")
        evidence = trace.get("functional_evidence")
        if not isinstance(problem_id, str) or not isinstance(evidence, dict):
            raise ContestSummaryError("natural trace identity or evidence is invalid")
        if problem_id not in tier_by_problem_id or problem_id in evidence_by_problem_id:
            raise ContestSummaryError("natural trace problem ID is invalid or duplicated")
        evidence_by_problem_id[problem_id] = evidence

    strata: list[dict[str, Any]] = []
    for tier_name in DIFFICULTY_TIER_NAMES:
        source_items = [
            item for item in ranked if tier_by_problem_id[item["problem_id"]] == tier_name
        ]
        included = [
            (item, evidence_by_problem_id[item["problem_id"]])
            for item in source_items
            if item["problem_id"] in evidence_by_problem_id
        ]
        pass_count = sum(
            evidence.get("passed_base") is True and evidence.get("passed_plus") is True
            for _item, evidence in included
        )
        denominator = len(included)
        lower, upper = _wilson_95(pass_count, denominator)
        scores = [item["features"]["complexity_score"] for item in source_items]
        strata.append(
            {
                "tier": tier_name,
                "source_task_count": len(source_items),
                "included_natural_trace_count": denominator,
                "source_exclusion_count": len(source_items) - denominator,
                "base_and_plus_pass_count": pass_count,
                "base_and_plus_fail_count": denominator - pass_count,
                "base_and_plus_pass_rate": pass_count / denominator,
                "wilson_95_lower": lower,
                "wilson_95_upper": upper,
                "complexity_score_min": min(scores),
                "complexity_score_median": median(scores),
                "complexity_score_max": max(scores),
            }
        )

    rates = [item["base_and_plus_pass_rate"] for item in strata]
    lowest_rate = min(rates)
    lowest_tiers = [
        stratum["tier"] for stratum in strata if stratum["base_and_plus_pass_rate"] == lowest_rate
    ]
    first_drop_index = next(
        (index for index, rate in enumerate(rates[1:], start=1) if rate < rates[index - 1]),
        None,
    )
    return {
        "schema_version": 1,
        "kind": "tracejudge_phase4_difficulty_proxy_analysis",
        "analysis_id": "phase4_difficulty_proxy_analysis_v1",
        "difficulty_proxy": {
            "proxy_id": DIFFICULTY_PROXY_ID,
            "construct": "canonical-solution implementation complexity",
            "not_claimed_constructs": [
                "official HumanEval+ difficulty",
                "human-perceived difficulty",
                "model-independent intrinsic difficulty",
            ],
            "score_formula": (
                "10*canonical_sloc + 20*control_flow_count + 10*max_control_depth "
                "+ ast_node_count + 5*max(parameter_count-1, 0)"
            ),
            "assignment": (
                "Rank all 45 source tasks before joining outcomes; split into three equal "
                "15-task tiers; break score ties with SHA256(salt\\0problem_id)."
            ),
            "uses_model_outputs": False,
            "uses_human_labels": False,
            "uses_execution_outcomes_for_assignment": False,
        },
        "source_identities": {
            "dataset_id": "evalplus/humanevalplus",
            "dataset_revision": "d32357cf319e50e9c8d8dab5ea876c72b0fd321b",
            "raw_snapshot_sha256": RAW_SNAPSHOT_SHA256,
            "source_cohort_sha256": SOURCE_COHORT_SHA256,
            "natural_manifest_sha256": NATURAL_MANIFEST_SHA256,
            "source_task_count": 45,
            "included_natural_trace_count": 42,
        },
        "metric": {
            "name": "single-candidate Base-and-Plus pass rate",
            "positive_definition": "passed_base=true and passed_plus=true",
            "interval": "Wilson 95%",
            "analysis_level": "natural traces only",
        },
        "strata": strata,
        "observed_degradation": {
            "lowest_observed_tiers": lowest_tiers,
            "lowest_observed_rate": lowest_rate,
            "first_observed_drop_tier": (
                strata[first_drop_index]["tier"] if first_drop_index is not None else None
            ),
            "monotonic_nonincreasing": all(
                left >= right for left, right in zip(rates, rates[1:], strict=False)
            ),
            "claim": (
                "The first observed drop occurs at the medium-proxy tier; the hard-proxy tier "
                "does not decline further. Only two failures occur across 42 traces, so no "
                "stable general difficulty degradation point is established."
            ),
        },
        "limitations": [
            "The benchmark does not publish an official difficulty label for these tasks.",
            "The proxy uses canonical solution structure and is a relative ranking within this 45-task sample.",
            "Only one generated candidate per task is analyzed.",
            "Three source tasks ended in Provider failure before the 42-trace natural cohort was frozen.",
            "Counterfactual traces test mutation mechanisms and are intentionally excluded from task-difficulty strata.",
        ],
    }


def _method_metrics_by_id(statistics_report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows = statistics_report.get("method_metrics")
    if not isinstance(rows, list):
        raise ContestSummaryError("statistics report method metrics are invalid")
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("method_id"), str):
            raise ContestSummaryError("statistics report contains an invalid method row")
        result[row["method_id"]] = row
    return result


def build_contest_summary(repo_root: str | Path) -> dict[str, Any]:
    """Build the public-facing aggregate summary without reading raw participant labels."""

    root = Path(repo_root)
    chart_manifest = _decode_json(
        _read_hash_bound(
            root / CHART_MANIFEST_RELATIVE_PATH,
            CHART_MANIFEST_SHA256,
            label="public chart manifest",
        ),
        label="public chart manifest",
    )
    statistics_report = _decode_json(
        _read_hash_bound(
            root / STATISTICS_REPORT_RELATIVE_PATH,
            STATISTICS_REPORT_SHA256,
            label="aggregate statistics report",
        ),
        label="aggregate statistics report",
    )
    p1_commitment = _decode_json(
        _read_hash_bound(
            root / P1_COMMITMENT_RELATIVE_PATH,
            P1_COMMITMENT_SHA256,
            label="P1 commitment",
        ),
        label="P1 commitment",
    )
    p1_agreement_manifest = _decode_json(
        _read_hash_bound(
            root / P1_AGREEMENT_MANIFEST_RELATIVE_PATH,
            P1_AGREEMENT_MANIFEST_SHA256,
            label="P1 agreement manifest",
        ),
        label="P1 agreement manifest",
    )
    p1_agreement = _decode_json(
        _read_hash_bound(
            root / P1_AGREEMENT_ANALYSIS_RELATIVE_PATH,
            P1_AGREEMENT_ANALYSIS_SHA256,
            label="P1 aggregate agreement analysis",
        ),
        label="P1 aggregate agreement analysis",
    )
    difficulty = build_difficulty_analysis(root)

    methods = chart_manifest.get("methods")
    cohort = chart_manifest.get("cohort")
    if not isinstance(methods, list) or not isinstance(cohort, dict):
        raise ContestSummaryError("public chart manifest has invalid aggregate sections")
    statistics_by_id = _method_metrics_by_id(statistics_report)
    method_rows: list[dict[str, Any]] = []
    for method in methods:
        if not isinstance(method, dict):
            raise ContestSummaryError("public method summary is invalid")
        confusion = method.get("valid_only_confusion")
        method_id = method.get("method_id")
        if not isinstance(confusion, dict) or not isinstance(method_id, str):
            raise ContestSummaryError("public confusion summary is invalid")
        false_positive = confusion.get("false_positive")
        true_negative = confusion.get("true_negative")
        if not isinstance(false_positive, int) or not isinstance(true_negative, int):
            raise ContestSummaryError("public false-positive counts are invalid")
        fpr_denominator = false_positive + true_negative
        stats = statistics_by_id.get(method_id)
        if stats is None:
            raise ContestSummaryError("method is missing from aggregate statistics")
        scopes = stats.get("scopes")
        if not isinstance(scopes, dict) or not isinstance(scopes.get("all"), dict):
            raise ContestSummaryError("method aggregate scope is invalid")
        step_metric = scopes["all"].get("first_faulty_step_accuracy_labeled_gold_steps")
        if not isinstance(step_metric, dict):
            raise ContestSummaryError("first-error-step metric is invalid")
        method_rows.append(
            {
                "method_id": method_id,
                "display_name": method.get("display_name"),
                "accuracy": method.get("accuracy_all"),
                "true_positive": confusion.get("true_positive"),
                "false_positive": false_positive,
                "true_negative": true_negative,
                "false_negative": confusion.get("false_negative"),
                "false_positive_rate": false_positive / fpr_denominator,
                "false_positive_rate_numerator": false_positive,
                "false_positive_rate_denominator": fpr_denominator,
                "first_faulty_step_accuracy": step_metric,
            }
        )

    best_detection = max(method_rows, key=lambda row: row["accuracy"]["estimate"])
    best_localization = max(
        method_rows,
        key=lambda row: row["first_faulty_step_accuracy"]["estimate"] or -1,
    )
    full = next(row for row in method_rows if row["method_id"] == "full_tracejudge")
    if p1_commitment.get("formal_data_collected") is not False:
        raise ContestSummaryError("P1 commitment no longer matches its pre-collection identity")
    if (
        p1_agreement_manifest.get("analysis_sha256") != P1_AGREEMENT_ANALYSIS_SHA256
        or p1_agreement_manifest.get("item_count") != 20
        or p1_agreement_manifest.get("contains_trace_ids") is not False
        or p1_agreement_manifest.get("contains_per_item_labels") is not False
        or p1_agreement_manifest.get("contains_rationales") is not False
        or p1_agreement.get("item_count") != 20
        or p1_agreement.get("adjudication_performed") is not False
        or p1_agreement.get("raw_labels_unchanged") is not True
    ):
        raise ContestSummaryError("P1 aggregate agreement identity or privacy flags are invalid")
    binary_fields = p1_agreement.get("binary_fields")
    if not isinstance(binary_fields, list):
        raise ContestSummaryError("P1 aggregate agreement binary fields are invalid")
    has_error_agreement = next(
        (
            item
            for item in binary_fields
            if isinstance(item, dict) and item.get("field_name") == "has_error"
        ),
        None,
    )
    if not isinstance(has_error_agreement, dict) or not isinstance(
        has_error_agreement.get("raw_agreement"), dict
    ):
        raise ContestSummaryError("P1 has_error agreement is missing")
    p1_raw = has_error_agreement["raw_agreement"]
    if (
        p1_raw.get("agreeing_count") != 20
        or p1_raw.get("denominator") != 20
        or p1_raw.get("estimate") != 1.0
        or has_error_agreement.get("cohen_kappa") != 1.0
    ):
        raise ContestSummaryError("P1 has_error agreement differs from the frozen result")
    return {
        "schema_version": 1,
        "kind": "tracejudge_phase4_contest_results_overview",
        "overview_id": "phase4_contest_results_overview_v1",
        "headline": {
            "trace_count": cohort.get("trace_count"),
            "pair_count": cohort.get("pair_count"),
            "best_detection_method": best_detection["display_name"],
            "best_detection_numerator": best_detection["accuracy"]["numerator"],
            "best_detection_denominator": best_detection["accuracy"]["denominator"],
            "best_detection_accuracy": best_detection["accuracy"]["estimate"],
            "best_localization_method": best_localization["display_name"],
            "best_localization_numerator": best_localization["first_faulty_step_accuracy"][
                "numerator"
            ],
            "best_localization_denominator": best_localization["first_faulty_step_accuracy"][
                "denominator"
            ],
            "best_localization_accuracy": best_localization["first_faulty_step_accuracy"][
                "estimate"
            ],
            "full_false_positive_rate": full["false_positive_rate"],
            "full_false_positive_numerator": full["false_positive_rate_numerator"],
            "full_false_positive_denominator": full["false_positive_rate_denominator"],
        },
        "cohort": cohort,
        "methods": method_rows,
        "human_review": {
            "primary_rater_count": 1,
            "primary_labeled_trace_count": cohort.get("trace_count"),
            "primary_cohort_trace_count": cohort.get("trace_count"),
            "primary_coverage": 1.0,
            "second_rater_planned_subset_count": p1_commitment.get("selected_total_count"),
            "second_rater_completed_count": 20,
            "second_rater_coverage": 1.0,
            "agreement_status": "computed",
            "has_error_raw_agreement_numerator": p1_raw["agreeing_count"],
            "has_error_raw_agreement_denominator": p1_raw["denominator"],
            "has_error_raw_agreement": p1_raw["estimate"],
            "has_error_cohen_kappa": has_error_agreement["cohen_kappa"],
        },
        "difficulty": difficulty,
        "source_identities": {
            "public_chart_manifest_sha256": CHART_MANIFEST_SHA256,
            "aggregate_statistics_report_sha256": STATISTICS_REPORT_SHA256,
            "difficulty_natural_manifest_sha256": NATURAL_MANIFEST_SHA256,
            "p1_commitment_sha256": P1_COMMITMENT_SHA256,
            "p1_agreement_manifest_sha256": P1_AGREEMENT_MANIFEST_SHA256,
            "p1_agreement_analysis_sha256": P1_AGREEMENT_ANALYSIS_SHA256,
        },
        "evidence_status": {
            "verification_status": chart_manifest.get("verification_status"),
            "overall_confidence": chart_manifest.get("overall_confidence"),
            "reproducibility": chart_manifest.get("reproducibility"),
            "exploratory_only": chart_manifest.get("exploratory_only"),
        },
    }


def _percent(value: float, digits: int = 1) -> str:
    return f"{value * 100:.{digits}f}%"


def render_difficulty_markdown(analysis: Mapping[str, Any]) -> str:
    rows = analysis["strata"]
    lines = [
        "# TraceJudge-Hy3 难度代理分层分析 v1",
        "",
        "## Material Passport",
        "",
        "- Origin Skill: academic-research-suite/experiment-agent",
        "- Origin Mode: validate_engineering",
        "- Verification Status: ANALYZED",
        "- Version Label: phase4_difficulty_proxy_analysis_v1",
        "",
        "## 结论",
        "",
        "HumanEval+ 在本项目所用字段中没有官方难度标签，因此这里不把 `unknown` 改写成伪官方的 easy/medium/hard。我们使用不读取模型输出、人工标签或执行结果的**参考实现结构复杂度代理**，先对冻结的 45 题来源队列等量分层，再观察其中成功生成并纳入自然研究集的 42 条轨迹。",
        "",
        "观察到的下降从 `medium-proxy` 开始：`easy-proxy` 为 14/14（100.0%），`medium-proxy` 和 `hard-proxy` 均为 13/14（92.9%）。hard 层没有继续下降，且总共只有 2 个失败，因此不能声称已经找到稳定、可推广的难度退化点。",
        "",
        "## 分层结果",
        "",
        "| 代理难度 | 来源题数 | 纳入自然轨迹 | 来源排除 | Base+Plus 通过 | 通过率（95% Wilson CI） | 代理分数范围 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            "| {tier} | {source_task_count} | {included_natural_trace_count} | "
            "{source_exclusion_count} | {base_and_plus_pass_count}/{included_natural_trace_count} | "
            "{rate} [{lower}, {upper}] | {minimum}–{maximum}（中位 {median:g}） |".format(
                tier=row["tier"],
                source_task_count=row["source_task_count"],
                included_natural_trace_count=row["included_natural_trace_count"],
                source_exclusion_count=row["source_exclusion_count"],
                base_and_plus_pass_count=row["base_and_plus_pass_count"],
                rate=_percent(row["base_and_plus_pass_rate"]),
                lower=_percent(row["wilson_95_lower"]),
                upper=_percent(row["wilson_95_upper"]),
                minimum=row["complexity_score_min"],
                maximum=row["complexity_score_max"],
                median=row["complexity_score_median"],
            )
        )
    lines.extend(
        [
            "",
            "`来源排除` 是阶段一 Provider 失败，不是根据难度、标签或结果替换题目；三个代理层各有 1 条来源题未进入 42 条自然轨迹。",
            "",
            "## 代理定义",
            "",
            "对固定 revision 的 HumanEval+ 参考实现计算：",
            "",
            "```text",
            "score = 10 × 非空代码行",
            "      + 20 × 控制流节点数",
            "      + 10 × 最大控制流嵌套深度",
            "      + AST 节点数",
            "      + 5 × max(参数数 − 1, 0)",
            "```",
            "",
            "在连接任何执行结果前，按 `score` 排序并切成 15/15/15；同分时只用固定盐与公开 `problem_id` 的 SHA256 破同分。分数仅用于本 45 题样本内排序，不是心理测量量尺。反事实 15 条用于测试变异机制，故继续按 mutation type 分析，不混入任务难度层。",
            "",
            "## 复现身份",
            "",
            f"- Dataset revision: `{analysis['source_identities']['dataset_revision']}`",
            f"- Raw snapshot SHA256: `{analysis['source_identities']['raw_snapshot_sha256']}`",
            f"- 45-task source SHA256: `{analysis['source_identities']['source_cohort_sha256']}`",
            f"- 42-trace natural manifest SHA256: `{analysis['source_identities']['natural_manifest_sha256']}`",
            "- 只读复算：`.venv/bin/python -m tracejudge_hy3.phase4.contest_summary --difficulty`",
            "",
            "## 限制",
            "",
            "- 这是参考实现结构复杂度代理，不是 HumanEval+ 官方难度，也不等于人类感知难度。",
            "- 每题只有一个模型候选，样本量不足以估计可靠的难度曲线。",
            "- 通过率描述 Solver 代码的 EvalPlus Base+Plus 结果，不是过程评估器准确率。",
            "- 当前结果只支持报告从 `medium-proxy` 开始的观察性下降，不支持一般化的难度退化结论。",
            "",
        ]
    )
    return "\n".join(lines)


def render_overview_markdown(summary: Mapping[str, Any]) -> str:
    headline = summary["headline"]
    review = summary["human_review"]
    difficulty_rows = summary["difficulty"]["strata"]
    lines = [
        "# TraceJudge-Hy3 竞赛结果总览 v1",
        "",
        "## Material Passport",
        "",
        "- Origin Skill: academic-research-suite/experiment-agent",
        "- Origin Mode: validate_engineering",
        "- Verification Status: ANALYZED",
        "- Version Label: phase4_contest_results_overview_v1",
        "",
        "## 一句话结论",
        "",
        "TraceJudge-Hy3 在 57 条冻结轨迹上对 5 种方法完成 285 个严格配对判断；最佳观察到的错误检测准确率为 56/57（98.2%），Full TraceJudge 的有效判断混淆矩阵为 TP=13、FP=1、TN=42、FN=1，对应误报率 1/43（2.33%）。预先冻结的 20 条独立复标中，`has_error` 原始一致率为 20/20、Cohen's κ=1.000；结果仍属探索性，不构成普遍优越性结论。",
        "",
        "## 四个核心数字",
        "",
        "| 冻结轨迹 | 配对判断 | 最佳检测准确率 | Full 误报率 |",
        "|---:|---:|---:|---:|",
        f"| **{headline['trace_count']}**（42 自然 + 15 反事实） | **{headline['pair_count']}**（5 方法） | **{_percent(headline['best_detection_accuracy'])}**（{headline['best_detection_numerator']}/{headline['best_detection_denominator']}，{headline['best_detection_method']}） | **{_percent(headline['full_false_positive_rate'], 2)}**（{headline['full_false_positive_numerator']}/{headline['full_false_positive_denominator']}） |",
        "",
        "最佳首错步骤定位为 Four-layer + AST 的 10/11（90.9%）；Full TraceJudge 为 9/11（81.8%）。结构化方法的最佳检测值不应被改写为“Full 方法显著优于全部基线”。",
        "",
        "## 错误检测与误报率",
        "",
        "| 方法 | 全分母准确率 | TP | FP | TN | FN | FPR=FP/(FP+TN) | 首错步骤 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary["methods"]:
        accuracy = row["accuracy"]
        step = row["first_faulty_step_accuracy"]
        step_text = (
            "N/A"
            if step["denominator"] == 0
            else f"{step['numerator']}/{step['denominator']}（{_percent(step['estimate'])}）"
        )
        lines.append(
            "| {name} | {num}/{den}（{accuracy}） | {tp} | {fp} | {tn} | {fn} | "
            "{fpr_num}/{fpr_den}（{fpr}） | {step} |".format(
                name=row["display_name"],
                num=accuracy["numerator"],
                den=accuracy["denominator"],
                accuracy=_percent(accuracy["estimate"]),
                tp=row["true_positive"],
                fp=row["false_positive"],
                tn=row["true_negative"],
                fn=row["false_negative"],
                fpr_num=row["false_positive_rate_numerator"],
                fpr_den=row["false_positive_rate_denominator"],
                fpr=_percent(row["false_positive_rate"], 2),
                step=step_text,
            )
        )
    lines.extend(
        [
            "",
            "准确率使用 57 条全分母并把 Provider 失败计为错误；TP/FP/TN/FN 与 FPR 只针对有效二元判断，因此两种口径不能互相替代。",
            "",
            "## 人工核验覆盖",
            "",
            "| 核验层 | 已完成/计划 | 覆盖率 | 当前状态 |",
            "|---|---:|---:|---|",
            f"| 单主标注者盲法标签 | {review['primary_labeled_trace_count']}/{review['primary_cohort_trace_count']} | {_percent(review['primary_coverage'])} | 已冻结，用于当前结果 |",
            f"| 第二标注者独立复标 | {review['second_rater_completed_count']}/{review['second_rater_planned_subset_count']} | {_percent(review['second_rater_coverage'])} | 已冻结；has_error raw={review['has_error_raw_agreement_numerator']}/{review['has_error_raw_agreement_denominator']}，κ={review['has_error_cohen_kappa']:.3f}；agreement=`{review['agreement_status']}` |",
            "",
            "第二标注者覆盖的是预先冻结的 20/57 子集，不是另外 20 条新轨迹。该子集的 `has_error` 20/20 一致（Wilson 95% CI 83.9%–100.0%），完整七字段记录 19/20 一致；这增强了子集上的标注可靠性证据，但不把其外推为全部 57 条或其他任务的完美一致。",
            "",
            "唯一过程细节分歧随后通过 `documented_consensus` 完成 1/1 裁决；原始 19/20 指标保持不变，不能改写为裁决后 20/20 一致率。零影响字段与固定分母变化上界见[P1 裁决后敏感性分析](phase4_p1_post_adjudication_sensitivity_v1.md)。",
            "",
            "## 难度代理结果",
            "",
            "| 代理难度 | 纳入自然轨迹 | Base+Plus 通过率 |",
            "|---|---:|---:|",
        ]
    )
    for row in difficulty_rows:
        lines.append(
            f"| {row['tier']} | {row['included_natural_trace_count']} | "
            f"{row['base_and_plus_pass_count']}/{row['included_natural_trace_count']}（{_percent(row['base_and_plus_pass_rate'])}） |"
        )
    lines.extend(
        [
            "",
            "观察到的下降从 `medium-proxy` 开始，hard 层没有继续下降，且只有 2 个失败；详见[难度代理分层分析](phase4_difficulty_proxy_analysis_v1.md)。",
            "",
            "## 反事实结果与边界",
            "",
            "- Full TraceJudge 相对 Test-only 在 15 条反事实上的检测准确率差为 +13.3 pp（14/15 vs 12/15），但只有 3 个父题 cluster，95% cluster bootstrap 区间为 [0, 20] pp。",
            "- `reasoning_swap` 中 Test-only 为 0/3，四个 Judge 方法均为 3/3，说明只看测试无法发现“代码对、解释错”的过程问题。",
            "- `equivalent_implementation` 中 Test-only 为 3/3，Judge 方法均为 2/3，说明语义等价实现仍可能被误报；这也是 FPR 必须独立展示的原因。",
            "",
            "## 证据入口",
            "",
            "- [冻结正式研究报告](phase3_research_report_public_v1.md)",
            "- [难度代理分层分析](phase4_difficulty_proxy_analysis_v1.md)",
            "- [四案例 Judge 稳定性与标识符规范化敏感性分析](phase4_judge_stability_sensitivity_v1.md)",
            "- [P1 裁决后敏感性分析](phase4_p1_post_adjudication_sensitivity_v1.md)",
            "- [P1 一致性分析协议、命令与结果身份](../../experiments/phase4_protocol.md#26-gate-dp1-第二标注者准备)",
            "- [确定性聚合图表](charts/phase4_public_charts_v1/)",
            "- [2 分钟公开 Fixture Demo 脚本](phase4_fixture_demo_v1.md)",
            "",
            "## 解释限制",
            "",
            "- 主要 57 条方法性能仍以第一位标注者标签为参照；第二位标注者只独立复标预先冻结的 20 条子集。",
            "- `has_error` 零分歧使经验分布 bootstrap 的 κ 区间退化为 1–1；应同时看 20/20 原始一致率的 Wilson 下限 83.9%，不能宣称总体可靠性必为 1。",
            "- 57 条研究集和 3 个反事实父题不足以支持一般模型能力或因果结论。",
            "- `ANALYZED / CAUTION / CANNOT_VERIFY` 边界保持不变；本总览没有重跑 Hy3，也没有覆盖任何冻结产物。",
            "",
        ]
    )
    return "\n".join(lines)


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--difficulty", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.difficulty:
        payload = build_difficulty_analysis(args.repo_root)
        output = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            if args.json
            else render_difficulty_markdown(payload)
        )
    else:
        payload = build_contest_summary(args.repo_root)
        output = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            if args.json
            else render_overview_markdown(payload)
        )
    print(output, end="" if output.endswith("\n") else "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
