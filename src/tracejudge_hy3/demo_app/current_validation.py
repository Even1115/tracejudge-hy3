"""Read-only adapter for the 2026-09-10 latest supplementary validation summary.

The recording demo's scene 8 shows TWO clearly separated things side by side:
the historical frozen phase-4 study (57 traces, see ``overview.py``) and this
latest supplementary validation.  This module is the only source for the
latter.  It never calls a provider, never executes candidate code and never
modifies source files.

Data flow (publishable even though ``artifacts/`` is Git-ignored):

1. ``build_current_validation_summary`` extracts aggregate counts ONLY from
   four pinned, hash-bound local reports (36-item development label rescore,
   5-repetition ablation tally, 3-probe location comparison, 3-probe location
   review).  ``scripts/build_current_validation_summary.py`` writes the result
   to the Git-tracked ``docs/releases/current_validation_summary_v1.json``.
2. ``load_current_validation_summary`` serves that tracked summary.  When the
   local artifacts are present it re-verifies every recorded SHA-256 AND
   re-extracts every displayed number from the artifacts themselves; any
   drift refuses the summary entirely.  On a fresh clone (artifacts absent)
   the tracked summary is served as-is and marked ``tracked_summary_only``.

Trust boundaries (any failure raises ``CurrentValidationError`` and the
endpoint reports "最新补充验证暂不可用" instead of guessing):

- input paths are explicit, configured constants -- never "latest", never
  ``current.json``, never client-supplied;
- the tally must bind the same pre-registered plan (its own
  ``expect_plan_sha256``) and the same pilot configuration hash as the frozen
  single-round ablation;
- the summary's schema, per-experiment key counts and internally derivable
  ratios must be consistent;
- nothing absolute (paths, keys, raw model responses, annotator identities)
  is ever serialized to the browser.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from tracejudge_hy3.process_eval_v2.materials import digest
from tracejudge_hy3.process_eval_v2.preflight import read_json

SUMMARY_RELATIVE_PATH = Path("docs/releases/current_validation_summary_v1.json")
SUMMARY_SCHEMA = "tracejudge-current-validation-summary-v1"
GENERATED_ON = "2026-09-10"

#: The contest results overview document, served read-only by the demo server
#: so the page entry never 404s and never depends on Markdown rendering.
CONTEST_OVERVIEW_DOC = Path("docs/contest_results_overview.md")
CONTEST_OVERVIEW_ROUTE = "/docs/contest-results-overview"

PILOT_CONFIG = Path("data/manifests/process_pilot_v1.json")

# Explicit, configured artifact inputs.  Pointing the page at newer reports
# means editing these constants (and regenerating the summary) deliberately.
DEV36_RESCORE_FILE = Path(
    "artifacts/experiments/process-method-v2/dev36-label-rescore-20260910-v2/label-rescore.json"
)
ABLATION_TALLY_FILE = Path(
    "artifacts/experiments/process-pilot/mbpp12-ablation-repetition-tally-20260909-v1/tally-report.json"
)
LOCATION_COMPARISON_FILE = Path(
    "artifacts/experiments/process-method-v2/probes3-location-comparison-20260910-v1/comparison.json"
)
LOCATION_REVIEW_FILE = Path(
    "artifacts/experiments/process-method-v2/probes3-location-review-20260910-v2/location-review.json"
)

SOURCE_KEYS = {
    "dev36_label_rescore": DEV36_RESCORE_FILE,
    "ablation_repetition_tally": ABLATION_TALLY_FILE,
    "location_comparison": LOCATION_COMPARISON_FILE,
    "location_review": LOCATION_REVIEW_FILE,
}

DEV36_METHODS = ("baseline", "assumption_audit")
CONDITIONS = ("ablation_a", "ablation_b", "ablation_c", "ablation_d")

#: Frozen item ids of the two known-error pilot samples, matching
#: ``ablation_view.CASE_NOTES``.  If the pinned tally ever changes, extraction
#: refuses rather than attaching numbers to the wrong rows.
MAX_PRODUCT_ITEM = "item-e5b9ff02f8bd9e3f3b61"
DIVISION_ELEMENTS_ITEM = "item-caf2c4228350e46f5b68"

DEV36_LIMITATIONS = [
    "开发标签为预测后单人 AI 辅助复核（tuple_str_int 亦为预测后复核），非独立盲审金标，也不是独立留出集。",
    "与原口径 32/36 的差异完全来自标签修订，不是模型能力提升；两方法仍漏检 3 条错误。",
]
REPETITION_LIMITATIONS = [
    "12 条开发试点、原方案与原标签；样本量小，只报告计数与飘动，不做统计显著性或泛化声明。",
    "rep1 的 D 检出未稳定复现，不得声称 D 稳定优于其他条件。",
]
LOCATION_LIMITATIONS = [
    "3 条全部是构造错误样本，不能据此计算或宣称总体误报率。",
    "补充同源引用覆盖是事后机械指标，不能代替精确定位（0/3）。",
]
GLOBAL_LIMITATIONS = [
    "各实验样本、方法与标签阶段不同：不合并、不相加、不建立总排行榜。",
    "36 条为开发集标签，不是独立留出集；81 条保留集仍封存，没有保留集成绩。",
    "历史 57 条五方法研究、历史 12 条三方法试点、单轮 2×2 消融与以上补充验证各自独立统计。",
]


class CurrentValidationError(ValueError):
    """A summary source is missing, inconsistent or failed verification."""


def _contained_file(root: Path, relative: Path, description: str) -> Path:
    part = PurePosixPath(relative.as_posix())
    if relative.is_absolute() or ".." in part.parts:
        raise CurrentValidationError(f"{description} 必须是仓库内的相对路径。")
    path = root.joinpath(*part.parts)
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise CurrentValidationError(f"{description} 超出仓库根目录。")
    if path.is_symlink() or not resolved.is_file():
        raise CurrentValidationError(f"最新补充验证的来源缺失：{description}。")
    return resolved


def _read_json(path: Path, description: str) -> dict:
    try:
        value = read_json(path.read_bytes())
    except (ValueError, UnicodeDecodeError) as exc:
        raise CurrentValidationError(f"{description} 无法解析：{exc}") from exc
    if not isinstance(value, dict):
        raise CurrentValidationError(f"{description} 不是 JSON 对象。")
    return value


def _ratio(value: Any, label: str) -> tuple[int, int]:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("numerator"), int)
        or not isinstance(value.get("denominator"), int)
        or isinstance(value.get("numerator"), bool)
        or isinstance(value.get("denominator"), bool)
    ):
        raise CurrentValidationError(f"{label} 缺少分子/分母。")
    num, den = value["numerator"], value["denominator"]
    if den <= 0 or num < 0 or num > den:
        raise CurrentValidationError(f"{label} 的计数不一致（{num}/{den}）。")
    return num, den


# ------------------------------------------------------------ extraction


def extract_dev36_metrics(rescore: dict) -> dict[str, Any]:
    """Aggregate counts from the 36-item development label rescore (v2 labels)."""

    if rescore.get("schema") != "tracejudge-development-label-rescore-v1":
        raise CurrentValidationError("label-rescore.json 不是已知的开发标签重计分文件。")
    if rescore.get("calls_made") != 0 or rescore.get("candidate_executions") != 0:
        raise CurrentValidationError("重计分报告声称发生了模型调用或候选执行，拒绝展示。")
    revised = rescore.get("revised")
    if not isinstance(revised, dict):
        raise CurrentValidationError("重计分文件缺少 revised 段。")

    per_method: dict[str, Any] = {}
    for method in DEV36_METHODS:
        block = revised.get(method)
        if not isinstance(block, dict):
            raise CurrentValidationError(f"重计分文件缺少 {method} 方法段。")
        if block.get("item_count") != 36:
            raise CurrentValidationError("重计分样本数不是 36，拒绝展示。")
        if block.get("label_stage") != "post_prediction_development_revision_v2":
            raise CurrentValidationError("重计分标签阶段不是修订标签 v2，拒绝混用。")
        try:
            condition = block["views"]["rule_merged"]["ablation_a"]
            dim = condition["dimensions"]["public_process_correct"]
            coverage = condition["coverage"]
        except KeyError as exc:
            raise CurrentValidationError(f"重计分文件缺少计分字段：{exc}") from exc
        for key in ("tp", "fp", "tn", "fn"):
            if not isinstance(dim.get(key), int) or isinstance(dim.get(key), bool):
                raise CurrentValidationError("重计分的混淆矩阵计数无效。")
        accuracy = _ratio(dim.get("accuracy_all_known"), "36 条准确率")
        if accuracy != (dim["tp"] + dim["tn"], 36):
            raise CurrentValidationError("重计分准确率与混淆矩阵不一致。")
        if coverage.get("ok") != 36 or coverage.get("missing") != 0:
            raise CurrentValidationError("重计分覆盖率不是 36/36。")
        per_method[method] = {
            "tp": dim["tp"],
            "fp": dim["fp"],
            "tn": dim["tn"],
            "fn": dim["fn"],
            "accuracy": accuracy,
        }
    if per_method[DEV36_METHODS[0]] != per_method[DEV36_METHODS[1]]:
        raise CurrentValidationError("两方法的 36 条重计分不一致；页面只展示两方法持平的结论。")
    merged = per_method[DEV36_METHODS[0]]
    tp, fp, tn, fn = merged["tp"], merged["fp"], merged["tn"], merged["fn"]
    return {
        "scope": "36 条开发样本 · baseline 与 assumption audit 两方法 · 修订开发标签 v2 · 同一批预测离线重计分（0 次模型调用、0 次候选执行）",
        "item_count": 36,
        "methods": list(DEV36_METHODS),
        "accuracy_numerator": merged["accuracy"][0],
        "accuracy_denominator": merged["accuracy"][1],
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "error_recall_numerator": tp,
        "error_recall_denominator": tp + fn,
        "false_positive_numerator": fp,
        "false_positive_denominator": fp + tn,
        "coverage_ok": 36,
        "label_stage": "post_prediction_development_revision_v2",
        "limitations": list(DEV36_LIMITATIONS),
    }


def _detection_frequency(item: dict, condition: str, label: str) -> tuple[int, int]:
    try:
        freq = item["conditions"][condition]["merged"]["detection_frequency"]
    except KeyError as exc:
        raise CurrentValidationError(f"{label} 缺少 {condition} 的检出频率。") from exc
    return _ratio(freq, label)


def extract_repetition_metrics(tally: dict) -> dict[str, Any]:
    """Aggregate counts from the frozen 5-repetition ablation tally."""

    if tally.get("schema") != "tracejudge-process-ablation-repetition-tally-v1":
        raise CurrentValidationError("tally-report.json 不是已知的消融重复汇总文件。")
    if tally.get("repetition_count") != 5:
        raise CurrentValidationError("重复轮次不是 5，拒绝展示。")
    plan_sha = tally.get("plan_sha256")
    if not plan_sha or plan_sha != tally.get("expect_plan_sha256"):
        raise CurrentValidationError("重复汇总绑定的方案与预登记值不一致，拒绝展示。")
    if not isinstance(tally.get("run_dirs"), list) or len(tally["run_dirs"]) != 5:
        raise CurrentValidationError("重复汇总的运行目录清单不是 5 轮。")

    items = tally.get("items")
    if not isinstance(items, dict):
        raise CurrentValidationError("重复汇总缺少逐样本段。")
    max_product = items.get(MAX_PRODUCT_ITEM)
    division = items.get(DIVISION_ELEMENTS_ITEM)
    if not isinstance(max_product, dict) or not isinstance(division, dict):
        raise CurrentValidationError("重复汇总缺少两个已知错误样本，拒绝把数字绑到错误样本上。")
    if max_product.get("gold_process_correct") is not False:
        raise CurrentValidationError("max_product 的金标不是已知错误，拒绝展示。")
    if division.get("gold_process_correct") is not False:
        raise CurrentValidationError("division_elements 的金标不是已知错误，拒绝展示。")

    max_product_detection = {
        condition: list(_detection_frequency(max_product, condition, "max_product 检出频率"))
        for condition in CONDITIONS
    }
    division_detection = {
        condition: list(_detection_frequency(division, condition, "division_elements 检出频率"))
        for condition in CONDITIONS
    }
    division_total = [
        sum(freq[0] for freq in division_detection.values()),
        sum(freq[1] for freq in division_detection.values()),
    ]

    rep_rows = tally.get("per_repetition_scores")
    if not isinstance(rep_rows, list) or not rep_rows:
        raise CurrentValidationError("重复汇总缺少逐轮计分。")
    rep1 = next(
        (
            row
            for row in rep_rows
            if isinstance(row, dict)
            and str(row.get("run_dir", "")).endswith("mbpp12-ablation-20260909-v1")
        ),
        None,
    )
    if rep1 is None:
        raise CurrentValidationError("重复汇总找不到 rep1（原单轮）计分，无法核对单轮身份。")
    rep1_accuracy: dict[str, list[int]] = {}
    for condition in CONDITIONS:
        try:
            dim = rep1["views"]["rule_merged"][condition]["dimensions"]["public_process_correct"]
        except KeyError as exc:
            raise CurrentValidationError(f"rep1 缺少 {condition} 的计分。") from exc
        rep1_accuracy[condition] = list(_ratio(dim.get("accuracy_all_known"), "rep1 准确率"))

    return {
        "scope": "12 条开发试点 · 四证据条件 · 五轮冻结重复（rep1 + rep2–5）· 原方案与原标签",
        "item_count": 12,
        "repetition_count": 5,
        "rep1_accuracy": rep1_accuracy,
        "max_product_detection": max_product_detection,
        "division_elements_detection": division_detection,
        "division_elements_total": division_total,
        "provider_timeout_note": "B 条件 max_product 一次 Provider 超时，分母为 4",
        "limitations": list(REPETITION_LIMITATIONS),
    }


def extract_location_metrics(comparison: dict, review: dict) -> dict[str, Any]:
    """Aggregate counts from the 3 constructed location probes (v2 format)."""

    if comparison.get("schema") != "tracejudge-method-validation-comparison-v2":
        raise CurrentValidationError("comparison.json 不是已知的方法对比计分文件。")
    if comparison.get("constructed") is not True:
        raise CurrentValidationError("定位探针必须标记为构造样本，拒绝展示。")
    if review.get("schema") != "tracejudge-location-review-v1":
        raise CurrentValidationError("location-review.json 不是已知的定位复核文件。")
    if review.get("calls_made") != 0 or review.get("candidate_executions") != 0:
        raise CurrentValidationError("定位复核声称发生了模型调用或候选执行，拒绝展示。")

    per_method: dict[str, Any] = {}
    for method in DEV36_METHODS:
        block = comparison.get(method)
        if not isinstance(block, dict):
            raise CurrentValidationError(f"定位对比缺少 {method} 方法段。")
        if block.get("item_count") != 3:
            raise CurrentValidationError("定位探针数不是 3，拒绝展示。")
        if block.get("label_stage") != "constructed_capability_probes_v1":
            raise CurrentValidationError("定位探针标签阶段不符，拒绝混用。")
        try:
            condition = block["views"]["rule_merged"]["ablation_a"]
            dim = condition["dimensions"]["public_process_correct"]
            localization = condition["localization"]
            consumed = block["run"]["budget"]["consumed_requests"]
        except KeyError as exc:
            raise CurrentValidationError(f"定位对比缺少字段：{exc}") from exc
        if not isinstance(consumed, int) or isinstance(consumed, bool) or consumed <= 0:
            raise CurrentValidationError("定位探针的请求消耗记录无效。")
        detection = (dim.get("tp"), dim.get("tp") + dim.get("fn"))
        if detection[0] is None or not isinstance(detection[0], int):
            raise CurrentValidationError("定位探针检出计数无效。")
        per_method[method] = {
            "detection": detection,
            "first_layer": _ratio(localization.get("first_layer"), "首错层级"),
            "first_step": _ratio(localization.get("first_step"), "首错步骤"),
            "structured_location_exact": _ratio(
                localization.get("structured_location_exact"), "结构化位置精确匹配"
            ),
            "consumed_requests": consumed,
        }
        try:
            coverage = review["supplementary"][method]["rule_merged"]["coverage"]
        except KeyError as exc:
            raise CurrentValidationError(f"定位复核缺少 {method} 的补充覆盖。") from exc
        per_method[method]["supplementary_citation_coverage"] = _ratio(coverage, "补充同源引用覆盖")
    base, audit = per_method["baseline"], per_method["assumption_audit"]
    shared = {key: base[key] for key in base if key != "consumed_requests"}
    if shared != {key: audit[key] for key in audit if key != "consumed_requests"}:
        raise CurrentValidationError("两方法的定位探针指标不一致；页面只展示两方法持平的结论。")
    return {
        "scope": "3 条构造错误探针 · 新定位规范 v2 · baseline 与 assumption audit 两方法",
        "item_count": 3,
        "constructed": True,
        "detection_numerator": shared["detection"][0],
        "detection_denominator": shared["detection"][1],
        "first_layer_numerator": shared["first_layer"][0],
        "first_layer_denominator": shared["first_layer"][1],
        "first_step_numerator": shared["first_step"][0],
        "first_step_denominator": shared["first_step"][1],
        "structured_location_exact_numerator": shared["structured_location_exact"][0],
        "structured_location_exact_denominator": shared["structured_location_exact"][1],
        "supplementary_citation_numerator": shared["supplementary_citation_coverage"][0],
        "supplementary_citation_denominator": shared["supplementary_citation_coverage"][1],
        "requests": {
            "baseline": base["consumed_requests"],
            "assumption_audit": audit["consumed_requests"],
        },
        "limitations": list(LOCATION_LIMITATIONS),
    }


def build_current_validation_summary(repo_root: str | Path) -> dict[str, Any]:
    """Extract the publishable summary from the four pinned local reports.

    Deterministic: same artifacts produce byte-identical output, so
    ``scripts/build_current_validation_summary.py --check`` can detect drift.
    """

    root = Path(repo_root).resolve()
    sources: dict[str, dict[str, str]] = {}
    documents: dict[str, dict] = {}
    for key, relative in SOURCE_KEYS.items():
        path = _contained_file(root, relative, f"来源 {relative.as_posix()}")
        raw = path.read_bytes()
        sources[key] = {"path": relative.as_posix(), "sha256": digest(raw)}
        documents[key] = _read_json(path, f"来源 {relative.as_posix()}")

    pilot_config_path = _contained_file(root, PILOT_CONFIG, "pilot 配置")
    pilot_config_sha = digest(pilot_config_path.read_bytes())
    tally = documents["ablation_repetition_tally"]
    if tally.get("pilot_config_sha256") != pilot_config_sha:
        raise CurrentValidationError("重复汇总绑定的 pilot 配置与当前绑定材料不一致，拒绝混用。")

    return {
        "schema": SUMMARY_SCHEMA,
        "title": "2026-09-10 最新补充验证",
        "generated_on": GENERATED_ON,
        "calls_made": 0,
        "candidate_executions": 0,
        "sources": sources,
        "experiments": {
            "dev36_label_rescore": extract_dev36_metrics(documents["dev36_label_rescore"]),
            "ablation_repetition": extract_repetition_metrics(
                documents["ablation_repetition_tally"]
            ),
            "location_probes": extract_location_metrics(
                documents["location_comparison"], documents["location_review"]
            ),
        },
        "global_limitations": list(GLOBAL_LIMITATIONS),
        "overview_doc": CONTEST_OVERVIEW_DOC.as_posix(),
    }


# ------------------------------------------------------------ validation


_NUMERIC_FIELDS = {
    "dev36_label_rescore": (
        "item_count",
        "accuracy_numerator",
        "accuracy_denominator",
        "tp",
        "fp",
        "tn",
        "fn",
        "error_recall_numerator",
        "error_recall_denominator",
        "false_positive_numerator",
        "false_positive_denominator",
        "coverage_ok",
    ),
    "ablation_repetition": ("item_count", "repetition_count"),
    "location_probes": (
        "item_count",
        "detection_numerator",
        "detection_denominator",
        "first_layer_numerator",
        "first_layer_denominator",
        "first_step_numerator",
        "first_step_denominator",
        "structured_location_exact_numerator",
        "structured_location_exact_denominator",
        "supplementary_citation_numerator",
        "supplementary_citation_denominator",
    ),
}


def _require_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CurrentValidationError(f"汇总文件的 {label} 无效。")
    return value


def _require_condition_ratios(block: dict, field: str, label: str) -> None:
    ratios = block.get(field)
    if not isinstance(ratios, dict) or set(ratios) != set(CONDITIONS):
        raise CurrentValidationError(f"汇总文件的{label}缺少四个消融条件。")
    for condition, pair in ratios.items():
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or any(not isinstance(x, int) or isinstance(x, bool) for x in pair)
        ):
            raise CurrentValidationError(f"汇总文件的{label}（{condition}）无效。")
        num, den = pair
        if den <= 0 or num < 0 or num > den:
            raise CurrentValidationError(f"汇总文件的{label}（{condition}）计数不一致。")


def validate_summary(summary: dict) -> None:
    """Schema, structure and internal-consistency checks that do not need the
    local artifacts.  Drift past these checks is caught by artifact
    re-extraction whenever the artifacts are present."""

    if summary.get("schema") != SUMMARY_SCHEMA:
        raise CurrentValidationError("汇总文件不是已知的最新补充验证摘要。")
    if summary.get("generated_on") != GENERATED_ON:
        raise CurrentValidationError("汇总文件的生成日期与登记身份不符。")
    if summary.get("calls_made") != 0 or summary.get("candidate_executions") != 0:
        raise CurrentValidationError("汇总文件声称发生了模型调用或候选执行，拒绝展示。")

    sources = summary.get("sources")
    if not isinstance(sources, dict) or set(sources) != set(SOURCE_KEYS):
        raise CurrentValidationError("汇总文件的来源清单不完整。")
    for key, entry in sources.items():
        expected_path = SOURCE_KEYS[key].as_posix()
        if not isinstance(entry, dict) or entry.get("path") != expected_path:
            raise CurrentValidationError(f"汇总文件的来源 {key} 路径被改动，拒绝展示。")
        sha = entry.get("sha256")
        if not isinstance(sha, str) or len(sha) != 64:
            raise CurrentValidationError(f"汇总文件的来源 {key} 缺少有效 SHA-256。")
        try:
            int(sha, 16)
        except ValueError:
            raise CurrentValidationError(f"汇总文件的来源 {key} 缺少有效 SHA-256。") from None

    experiments = summary.get("experiments")
    if not isinstance(experiments, dict) or set(experiments) != set(_NUMERIC_FIELDS):
        raise CurrentValidationError("汇总文件的实验清单不完整。")
    for key, fields in _NUMERIC_FIELDS.items():
        block = experiments[key]
        if not isinstance(block, dict):
            raise CurrentValidationError(f"汇总文件的实验 {key} 无效。")
        for field in fields:
            _require_int(block.get(field), f"{key}.{field}")
        if not isinstance(block.get("limitations"), list) or not block["limitations"]:
            raise CurrentValidationError(f"汇总文件的实验 {key} 缺少限制说明。")

    dev = experiments["dev36_label_rescore"]
    if dev["item_count"] != 36 or dev["accuracy_denominator"] != 36:
        raise CurrentValidationError("汇总文件的 36 条实验分母被改动。")
    if dev["accuracy_numerator"] != dev["tp"] + dev["tn"]:
        raise CurrentValidationError("汇总文件的 36 条准确率与混淆矩阵不一致。")
    if dev["tp"] + dev["fp"] + dev["tn"] + dev["fn"] != 36:
        raise CurrentValidationError("汇总文件的 36 条混淆矩阵合计不为 36。")
    if (dev["error_recall_numerator"], dev["error_recall_denominator"]) != (
        dev["tp"],
        dev["tp"] + dev["fn"],
    ):
        raise CurrentValidationError("汇总文件的错误召回与混淆矩阵不一致。")
    if (dev["false_positive_numerator"], dev["false_positive_denominator"]) != (
        dev["fp"],
        dev["fp"] + dev["tn"],
    ):
        raise CurrentValidationError("汇总文件的误报率与混淆矩阵不一致。")

    rep = experiments["ablation_repetition"]
    if rep["item_count"] != 12 or rep["repetition_count"] != 5:
        raise CurrentValidationError("汇总文件的重复实验身份被改动。")
    _require_condition_ratios(rep, "rep1_accuracy", "rep1 准确率")
    _require_condition_ratios(rep, "max_product_detection", "max_product 检出频率")
    _require_condition_ratios(rep, "division_elements_detection", "division_elements 检出频率")
    division_total = rep.get("division_elements_total")
    per_condition = rep["division_elements_detection"]
    if division_total != [
        sum(pair[0] for pair in per_condition.values()),
        sum(pair[1] for pair in per_condition.values()),
    ]:
        raise CurrentValidationError("汇总文件的 division_elements 合计与分条件计数不一致。")

    probes = experiments["location_probes"]
    if probes["item_count"] != 3 or probes.get("constructed") is not True:
        raise CurrentValidationError("汇总文件的定位探针身份被改动。")
    for pair_fields in (
        ("detection_numerator", "detection_denominator"),
        ("first_layer_numerator", "first_layer_denominator"),
        ("first_step_numerator", "first_step_denominator"),
        ("structured_location_exact_numerator", "structured_location_exact_denominator"),
        ("supplementary_citation_numerator", "supplementary_citation_denominator"),
    ):
        num, den = (probes[pair_fields[0]], probes[pair_fields[1]])
        if den != 3 or num > 3:
            raise CurrentValidationError("汇总文件的定位探针计数超出 3 条样本。")
    requests = probes.get("requests")
    if not isinstance(requests, dict) or set(requests) != set(DEV36_METHODS):
        raise CurrentValidationError("汇总文件的定位探针请求计数无效。")
    for method, count in requests.items():
        _require_int(count, f"location_probes.requests.{method}")

    if summary.get("overview_doc") != CONTEST_OVERVIEW_DOC.as_posix():
        raise CurrentValidationError("汇总文件的成果总览入口被改动。")
    limitations = summary.get("global_limitations")
    if not isinstance(limitations, list) or not limitations:
        raise CurrentValidationError("汇总文件缺少全局限制说明。")


def _experiment_metrics(summary: dict) -> dict[str, dict[str, Any]]:
    """The numeric identity of each experiment, for artifact comparison."""

    experiments = summary["experiments"]
    dev = experiments["dev36_label_rescore"]
    probes = experiments["location_probes"]
    return {
        "dev36_label_rescore": {
            key: dev[key]
            for key in _NUMERIC_FIELDS["dev36_label_rescore"]
            if key not in ("item_count", "coverage_ok")
        }
        | {"label_stage": dev.get("label_stage")},
        "ablation_repetition": {
            "rep1_accuracy": experiments["ablation_repetition"]["rep1_accuracy"],
            "max_product_detection": experiments["ablation_repetition"]["max_product_detection"],
            "division_elements_detection": experiments["ablation_repetition"][
                "division_elements_detection"
            ],
            "division_elements_total": experiments["ablation_repetition"][
                "division_elements_total"
            ],
        },
        "location_probes": {
            key: probes[key] for key in _NUMERIC_FIELDS["location_probes"] if key != "item_count"
        }
        | {"requests": probes["requests"]},
    }


def verify_against_artifacts(root: Path, summary: dict) -> None:
    """Re-extract every displayed number from the pinned artifacts and require
    exact agreement with the tracked summary (hashes AND counts)."""

    documents: dict[str, dict] = {}
    for key, relative in SOURCE_KEYS.items():
        path = _contained_file(root, relative, f"来源 {relative.as_posix()}")
        if digest(path.read_bytes()) != summary["sources"][key]["sha256"]:
            raise CurrentValidationError(
                f"来源 {relative.as_posix()} 与汇总文件记录的 SHA-256 不一致；"
                "实验材料可能被修改，拒绝展示最新摘要。"
            )
        documents[key] = _read_json(path, f"来源 {relative.as_posix()}")

    pilot_config_path = _contained_file(root, PILOT_CONFIG, "pilot 配置")
    if documents["ablation_repetition_tally"].get("pilot_config_sha256") != digest(
        pilot_config_path.read_bytes()
    ):
        raise CurrentValidationError("重复汇总绑定的 pilot 配置与当前绑定材料不一致，拒绝混用。")

    fresh = {
        "dev36_label_rescore": extract_dev36_metrics(documents["dev36_label_rescore"]),
        "ablation_repetition": extract_repetition_metrics(documents["ablation_repetition_tally"]),
        "location_probes": extract_location_metrics(
            documents["location_comparison"], documents["location_review"]
        ),
    }
    expected = _experiment_metrics(summary)
    actual = {
        "dev36_label_rescore": {
            key: fresh["dev36_label_rescore"][key] for key in expected["dev36_label_rescore"]
        },
        "ablation_repetition": {
            key: fresh["ablation_repetition"][key] for key in expected["ablation_repetition"]
        },
        "location_probes": {
            key: fresh["location_probes"][key] for key in expected["location_probes"]
        },
    }
    if actual != expected:
        raise CurrentValidationError(
            "汇总文件的关键计数与绑定来源的重新抽取不一致；拒绝展示未经核验的数字。"
        )


def load_current_validation_summary(repo_root: str | Path) -> dict[str, Any]:
    """Load the tracked summary read-only; verify against local artifacts when
    they are present.  Raises ``CurrentValidationError`` on any failure."""

    root = Path(repo_root).resolve()
    path = _contained_file(root, SUMMARY_RELATIVE_PATH, "最新补充验证汇总")
    summary = _read_json(path, "最新补充验证汇总")
    validate_summary(summary)

    artifact_presence = [
        (root / relative).is_file() and not (root / relative).is_symlink()
        for relative in SOURCE_KEYS.values()
    ]
    if all(artifact_presence):
        verify_against_artifacts(root, summary)
        verification = "artifacts_verified"
    elif any(artifact_presence):
        raise CurrentValidationError(
            "本地只存在部分绑定来源，无法完成一致性核验；拒绝展示最新摘要。"
        )
    else:
        verification = "tracked_summary_only"

    return _display_payload(summary, verification)


def _display_payload(summary: dict, verification: str) -> dict[str, Any]:
    """Browser-ready payload: verified numbers rendered into short display
    lines, repo-relative paths only, no absolute paths or raw responses."""

    experiments = summary["experiments"]
    dev = experiments["dev36_label_rescore"]
    rep = experiments["ablation_repetition"]
    probes = experiments["location_probes"]

    rep1 = rep["rep1_accuracy"]
    max_product = rep["max_product_detection"]
    division_total = rep["division_elements_total"]

    cards = [
        {
            "id": "dev36_label_rescore",
            "title": "开发集重计分 · 36 条 · 两方法 · 修订标签 v2",
            "scope": dev["scope"],
            "lines": [
                f"两方法准确率均 {dev['accuracy_numerator']}/{dev['accuracy_denominator']}"
                f"，TP/FP/TN/FN = {dev['tp']}/{dev['fp']}/{dev['tn']}/{dev['fn']}",
                f"错误召回 {dev['error_recall_numerator']}/{dev['error_recall_denominator']}"
                f" · 误报 {dev['false_positive_numerator']}/{dev['false_positive_denominator']}"
                f" · 有效覆盖 {dev['coverage_ok']}/36",
            ],
            "limitations": dev["limitations"],
        },
        {
            "id": "ablation_repetition",
            "title": "五轮冻结消融重复 · 12 条 · 原方案与原标签",
            "scope": rep["scope"],
            "lines": [
                f"单轮 rep1：D {rep1['ablation_d'][0]}/{rep1['ablation_d'][1]}，"
                f"A/B/C 均 {rep1['ablation_a'][0]}/{rep1['ablation_a'][1]}；五轮后 D 优势未稳定复现",
                f"max_product 检出：A {max_product['ablation_a'][0]}/{max_product['ablation_a'][1]}"
                f" · B {max_product['ablation_b'][0]}/{max_product['ablation_b'][1]}（1 次超时）"
                f" · C {max_product['ablation_c'][0]}/{max_product['ablation_c'][1]}"
                f" · D {max_product['ablation_d'][0]}/{max_product['ablation_d'][1]}；"
                f"division_elements 四条件合计 {division_total[0]}/{division_total[1]}",
            ],
            "limitations": rep["limitations"],
        },
        {
            "id": "location_probes",
            "title": "定位探针 · 3 条构造错误样本 · 两方法",
            "scope": probes["scope"],
            "lines": [
                f"两方法均：检出 {probes['detection_numerator']}/{probes['detection_denominator']}"
                f" · 首错层级 {probes['first_layer_numerator']}/{probes['first_layer_denominator']}"
                f" · 首错步骤 {probes['first_step_numerator']}/{probes['first_step_denominator']}"
                f" · 精确位置 {probes['structured_location_exact_numerator']}/{probes['structured_location_exact_denominator']}",
                f"补充同源引用覆盖 {probes['supplementary_citation_numerator']}/{probes['supplementary_citation_denominator']}"
                f" · baseline {probes['requests']['baseline']} 次请求"
                f" / assumption audit {probes['requests']['assumption_audit']} 次请求",
            ],
            "limitations": probes["limitations"],
        },
    ]

    return {
        "ok": True,
        "schema": SUMMARY_SCHEMA,
        "title": summary["title"],
        "generated_on": summary["generated_on"],
        "verification": verification,
        "calls_made": 0,
        "candidate_executions": 0,
        "experiments": cards,
        "global_limitations": list(summary["global_limitations"]),
        "overview_entry": {
            "href": CONTEST_OVERVIEW_ROUTE,
            "repo_path": summary["overview_doc"],
        },
    }


# --------------------------------------- repetition block for the ablation page


def load_repetition_tally(repo_root: str | Path) -> dict[str, Any]:
    """Verified repetition metrics for the ablation page's own summary block.

    Reads the SAME pinned tally as the latest-validation summary and applies
    the same identity checks (plan hash self-consistency, pilot config hash,
    frozen item ids).  Raises ``CurrentValidationError`` on any failure; the
    ablation page then shows the single-round table without repetition claims.
    """

    root = Path(repo_root).resolve()
    path = _contained_file(root, ABLATION_TALLY_FILE, "五轮消融重复汇总")
    raw = path.read_bytes()
    tally = _read_json(path, "五轮消融重复汇总")
    metrics = extract_repetition_metrics(tally)
    pilot_config_path = _contained_file(root, PILOT_CONFIG, "pilot 配置")
    if tally.get("pilot_config_sha256") != digest(pilot_config_path.read_bytes()):
        raise CurrentValidationError("重复汇总绑定的 pilot 配置与当前绑定材料不一致，拒绝混用。")

    rep1 = metrics["rep1_accuracy"]
    max_product = metrics["max_product_detection"]
    division_total = metrics["division_elements_total"]
    return {
        "available": True,
        "title": "五轮冻结重复 · 2026-09-09 预登记 / 2026-09-10 完成",
        "source_sha256": digest(raw),
        "rep1_accuracy": rep1,
        "max_product_detection": max_product,
        "division_elements_detection": metrics["division_elements_detection"],
        "division_elements_total": division_total,
        "lines": [
            f"单轮 rep1：D {rep1['ablation_d'][0]}/{rep1['ablation_d'][1]}，"
            f"A/B/C 均 {rep1['ablation_a'][0]}/{rep1['ablation_a'][1]}。",
            f"max_product 五轮检出：A {max_product['ablation_a'][0]}/{max_product['ablation_a'][1]}"
            f" · B {max_product['ablation_b'][0]}/{max_product['ablation_b'][1]}"
            f"（一次 Provider 超时，分母为 4）"
            f" · C {max_product['ablation_c'][0]}/{max_product['ablation_c'][1]}"
            f" · D {max_product['ablation_d'][0]}/{max_product['ablation_d'][1]}。",
            f"division_elements 四条件五轮合计 {division_total[0]}/{division_total[1]} 检出，"
            "是稳定漏检案例。",
            "重复结果未观察到稳定的 D 优势，不能从 rep1 推断证据条件的因果贡献。",
        ],
        "review_doc": "docs/experiments/process-pilot-repetition-v1.md",
    }
