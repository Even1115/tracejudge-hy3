"""Read-only adapter for the 2x2 evidence-ablation comparison view.

The page renders the SAME frozen candidates judged under four evidence
conditions (``ablation_a``/``b``/``c``/``d``) side by side.  This module only
READS artifacts written and hash-pinned by the ablation runner and scorer; it
never calls a provider, never executes candidate code and never modifies
source files.  It deliberately does NOT reuse the three-method comparison
identity: the ablation is a separate experiment with a separate pre-registered
plan, and the two pages stay separate.

Trust boundaries (any failure raises ``ComparisonSourceError`` and the
endpoint reports "results not ready" instead of guessing):

- the run directory and the scores file are explicit, configured paths --
  never "latest", never ``current.json``, never client-supplied;
- ``run-report.json`` pins the SHA-256 of ``predictions.jsonl``,
  ``requests.jsonl``, ``run-config.json`` and ``ablation-plan.json``; a
  mismatch means the run is still being written or was modified;
- ``run-config.json`` must bind the exact pilot configuration, the four
  conditions, the recomputed condition fingerprints, the per-item input hashes
  and the pre-registered plan hash (which must match both the documented
  ``EXPECTED_PLAN_SHA256`` and the fingerprint of the stored plan file);
- predictions are revalidated by calling the offline scorer ``score_ablation``
  itself (identity, duplicates, input hashes, reference binding AND the
  deterministic re-merge of every judge output);
- the published ``ablation-scores.json`` must hash-bind the same predictions
  file and must equal a fresh ``score_ablation`` recomputation on every
  scorer-produced key -- the page never re-implements scoring rules and never
  displays numbers that disagree with the scorer;
- human labels come only from the hash-bound, review-audited sources loaded by
  ``prepare_pilot`` -- never from a working copy.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from tracejudge_hy3.demo_app.comparison import (
    DIVERGENCE_FIELDS,
    ComparisonSourceError,
    _assessment_view,
    _contained_run_dir,
    _gold_category,
    _label_view,
    _parse_usage,
    _read_json_file,
    _verify_report_hashes,
)
from tracejudge_hy3.demo_app.current_validation import (
    DIVISION_ELEMENTS_ITEM,
    MAX_PRODUCT_ITEM,
    CurrentValidationError,
    load_repetition_tally,
)
from tracejudge_hy3.process_eval_v2.ablation import (
    COMPARISONS,
    CONDITION_DEFS,
    CONDITIONS,
    PLAN_SCHEMA,
    RUN_CONFIG_SCHEMA,
    RUN_REPORT_SCHEMA,
    SCORES_SCHEMA,
    AblationPrediction,
    condition_fingerprints,
    plan_fingerprint,
)
from tracejudge_hy3.process_eval_v2.contracts import public_process_correct
from tracejudge_hy3.process_eval_v2.materials import digest
from tracejudge_hy3.process_eval_v2.preflight import PreparedPilot, prepare_pilot, read_rows
from tracejudge_hy3.process_eval_v2.scoring import score_ablation

# Explicit, configured inputs.  Nothing here is auto-discovered: pointing the
# page at a newer run means editing these constants (and the docs) deliberately.
PILOT_CONFIG = Path("data/manifests/process_pilot_v1.json")
ABLATION_RUN_DIR = Path("artifacts/experiments/process-pilot/mbpp12-ablation-20260909-v1")
ABLATION_SCORES_FILE = Path(
    "artifacts/experiments/process-pilot/mbpp12-ablation-score-20260909-v1/ablation-scores.json"
)

ABLATION_VIEW_SCHEMA = "tracejudge-demo-ablation-comparison-v1"

#: The pre-registered plan hash documented in docs/experiments/process-pilot-ablation-v1.md.
#: The displayed run must bind exactly this plan; anything else is refused.
EXPECTED_PLAN_SHA256 = "1448c841986d6a4bdc0e0b5ba887fededf7c05b6fc7bc2272ddae239c9609774"

REQUIRED_RUN_FILES = {
    "predictions.jsonl",
    "requests.jsonl",
    "run-config.json",
    "ablation-plan.json",
}

#: Display labels.  The ablation conditions are NEVER renamed to the
#: historical Direct/Structured/Full methods; the two experiments stay apart.
CONDITION_LABELS = {
    "ablation_a": "A · 仅公开材料",
    "ablation_b": "B · +静态证据",
    "ablation_c": "C · +功能汇总",
    "ablation_d": "D · +静态+功能",
}

CONDITION_DESCRIPTIONS = {
    "ablation_a": "Judge 只看到公开题目与冻结解答；不提供静态证据，不提供官方功能汇总。",
    "ablation_b": "在 A 的基础上向 Judge 提供代码的 AST 静态分析证据；不提供官方功能汇总。",
    "ablation_c": "在 A 的基础上向 Judge 提供官方 base/plus 聚合功能结果；不提供静态证据。",
    "ablation_d": "同时向 Judge 提供静态分析证据与官方聚合功能结果。",
}

#: Editorial case notes for the two reviewed items, sourced from
#: docs/experiments/process-pilot-case-review-v1.md and the ablation run doc.
#: Numbers on the page always come from the verified artifacts; these notes
#: only add review context.  Keyed by frozen item_id: if the pinned run ever
#: changes, loading refuses rather than attaching notes to the wrong rows.
#: Five-repetition conclusions are NOT written here: they are appended from
#: the verified tally by ``load_ablation_comparison`` (never hard-coded).
CASE_NOTES: dict[str, dict[str, Any]] = {
    "item-e5b9ff02f8bd9e3f3b61": {
        "kind": "judgment_change",
        "title": "单轮重点案例 · max_product：rep1 仅 D 检出",
        "review_doc": "docs/experiments/process-pilot-case-review-v1.md",
        "points": [
            "单轮 rep1 每个条件每题一次判断：A/B/C 判过程成立，D 判过程不成立（相对冻结标签 fn→tp）。这是单次观察，不构成必要条件或因果结论；五轮重复结论见下方重复汇总。",
            "模型反例：D 的判断解释原文（见下方 D 条件卡片）自举 [-2,0,1] 应得 1 而算法得 0——这是模型输出文字，未执行验证。",
            "审核反例：案例审核人工推演 [0,2,3] 预期 6（两种公开解读下一致）——人工静态推演，未执行验证。两者分开陈列，不以人工解释替换模型原文。",
            "官方功能证据只有 base pass / plus fail 的汇总状态；不能声称上述反例就是官方 plus 失败的具体用例或原因。",
            "D 判断中的原文引用与冻结字段匹配成功，只证明位置绑定成立，不单独证明错误判断正确。",
        ],
        "historical_reference": None,
    },
    "item-caf2c4228350e46f5b68": {
        "kind": "label_dispute",
        "title": "单轮重点案例 · division_elements：四条件漏判，标签争议已裁决维持",
        "review_doc": "docs/experiments/process-pilot-case-review-v1.md",
        "points": [
            "单轮 rep1 四个条件均判过程成立，相对冻结共识标签（过程错误）计为漏判；计分依据就是这组冻结标签，页面与报告均不改动它。",
            "标签争议已于 2026-09-09 裁决：仅凭公开需求文本与示例复核（不查阅任何方法预测），“mathematical division”的主读法是精确除法，// 的地板除语义是需求中不存在的假设；维持 R03 原标签。完整推理链见案例审核文档“标签争议裁决记录”一节。",
            "裁决只解决“公开需求是否支持 // 假设”；非整除输入下的确切期望输出仍需需求澄清或官方材料，不依据官方 plus 失败反推。",
        ],
        "historical_reference": (
            "历史参考（实验设置不同，不作为本轮证据）：历史 full_system 曾在不同提示与处理下"
            "（功能结论写入判断字段、提示不统一）检出该样本为 R03_UNSUPPORTED_ASSUMPTION，"
            "见 mbpp12-run-20260909-v1。历史与本轮判断不同，尚不能区分提示差异、采样波动与"
            "标签争议的影响，不能直接归因比较。"
        ),
    },
}

FOCUS_CASES = [
    {"item_id": "item-e5b9ff02f8bd9e3f3b61", "label": "max_product · rep1 仅 D 检出"},
    {"item_id": "item-caf2c4228350e46f5b68", "label": "division_elements · 争议已裁决维持"},
]

#: Identity of the displayed experiment: the historical single round.  The
#: five-repetition tally is a separate frozen artifact and is shown in its own
#: block, never merged into the single-round table or its paired transitions.
IDENTITY_LABEL = "历史单轮消融 · 2026-09-09 · 原方案与原标签"

VIEW_NOTES = [
    "本页是统一提示下的 2×2 证据消融，与“方法对比”页的历史三方法运行是两个独立实验，不合并排名。",
    "“不提供证据”指该证据不进入 Judge 的输入；AST 静态分析与确定性规则层对全部四个条件都离线运行，用于分离规则贡献。",
    "公开过程口径为 reasoning_correct AND plan_code_aligned 的三值逻辑；官方功能汇总独立展示，不并入过程信号，不伪造逐用例执行记录。",
    "当前表格仍是 2026-09-09 原始单轮（rep1）结果；五轮冻结重复已完成并在上方单独汇总，不混入单轮表格与配对转移统计。",
    "已知过程标签 10 条（8 正确、2 错误），另 2 条未知保留在覆盖率但排除出二分类准确率；只报告计数与成对变化，不做统计显著性或泛化声明。",
    "首错步骤与结构化首错位置金标数均为 0：页面比较各条件给出的位置，但不标记定位正确，null/null 不算命中。",
    "人工过程标签是反馈后的开发集修订共识，不是独立留出测试集金标；标注者独立性未核实。",
    "引用出现在原文只证明绑定成功，不证明错误判断成立。",
    "页面只读取已落盘并哈希验证的记录；浏览与切换样本不会发起模型请求、不运行 Solver、不执行候选代码。",
    "金额未由 Provider 返回，成本保持未知。",
]


def _parse_ablation_predictions(run_path: Path) -> tuple[bytes, list[AblationPrediction]]:
    raw = (run_path / "predictions.jsonl").read_bytes()
    try:
        rows = read_rows(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ComparisonSourceError(f"predictions.jsonl 含损坏记录：{exc}") from exc
    predictions = []
    for row in rows:
        try:
            predictions.append(AblationPrediction.model_validate(row))
        except ValueError as exc:
            raise ComparisonSourceError(f"predictions.jsonl 含不符合契约的记录：{exc}") from exc
    return raw, predictions


def _check_run_identity(
    config: dict, plan: dict, pilot: PreparedPilot, config_raw_sha: str
) -> None:
    if config.get("schema") != RUN_CONFIG_SCHEMA:
        raise ComparisonSourceError("run-config.json 不是已知的消融运行身份。")
    if config.get("pilot_config_sha256") != config_raw_sha:
        raise ComparisonSourceError("运行绑定的是另一份 pilot 配置，拒绝混用。")
    if config.get("conditions") != list(CONDITIONS):
        raise ComparisonSourceError("运行的条件集合与消融页定义不一致。")
    if config.get("condition_fingerprints") != condition_fingerprints():
        raise ComparisonSourceError("条件提示词/可见字段/输出契约指纹与当前实现不一致，拒绝混用。")
    expected_hashes = {item_id: pilot.input_hash(item_id) for item_id in pilot.inputs}
    if config.get("input_hashes") != expected_hashes:
        raise ComparisonSourceError("运行的逐条输入哈希与当前绑定材料不一致，拒绝混用。")
    plan_sha = config.get("plan_sha256")
    if plan_sha != EXPECTED_PLAN_SHA256:
        raise ComparisonSourceError("运行绑定的方案不是本页登记的预注册方案，拒绝展示。")
    if plan.get("schema") != PLAN_SCHEMA:
        raise ComparisonSourceError("ablation-plan.json 不是已知的消融方案。")
    if plan_fingerprint(plan) != plan_sha:
        raise ComparisonSourceError("ablation-plan.json 的内容与运行绑定的方案哈希不一致。")
    if plan.get("items") != expected_hashes:
        raise ComparisonSourceError("预注册方案的输入哈希与当前绑定材料不一致，拒绝混用。")
    order = plan.get("execution_order")
    expected_pairs = {(condition, item_id) for condition in CONDITIONS for item_id in pilot.inputs}
    if (
        not isinstance(order, list)
        or len(order) != len(expected_pairs)
        or {(pair[0], pair[1]) for pair in order if isinstance(pair, list) and len(pair) == 2}
        != expected_pairs
    ):
        raise ComparisonSourceError("预注册方案的执行顺序不完整或含未知条目。")


def _check_scores_file(scores: dict, predictions_raw: bytes, recomputed: dict, pilot) -> None:
    """The published scores must bind the same predictions and match a fresh
    scorer recomputation on every scorer-produced key."""

    if scores.get("schema") != SCORES_SCHEMA:
        raise ComparisonSourceError("ablation-scores.json 不是已知的消融计分文件。")
    if scores.get("predictions_sha256") != digest(predictions_raw):
        raise ComparisonSourceError("计分文件绑定的是另一份预测文件，拒绝混用。")
    expected_hashes = {item_id: pilot.input_hash(item_id) for item_id in pilot.inputs}
    if scores.get("input_hashes") != expected_hashes:
        raise ComparisonSourceError("计分文件的输入哈希与当前绑定材料不一致，拒绝混用。")
    if scores.get("conditions") != list(CONDITIONS):
        raise ComparisonSourceError("计分文件的条件集合与消融页定义不一致。")
    extra = {"predictions_sha256", "verified_sources", "input_hashes"}
    if set(scores) - extra != set(recomputed):
        raise ComparisonSourceError("计分文件的字段集合与计分器输出不一致。")
    for key in recomputed:
        if scores[key] != recomputed[key]:
            raise ComparisonSourceError(
                f"计分文件的 {key} 与对预测的离线重算不一致；拒绝展示未经核验的数字。"
            )


def build_ablation_view(
    pilot: PreparedPilot,
    predictions: list[AblationPrediction],
    scores: dict,
    usage: dict[tuple[str, str], dict[str, Any]],
    *,
    experiment_meta: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the page payload from already verified inputs."""

    indexed: dict[tuple[str, str], AblationPrediction] = {
        (row.condition, row.item_id): row for row in predictions
    }

    items = []
    for item_id in sorted(pilot.inputs):
        item = pilot.inputs[item_id]
        label = pilot.labels[item_id]
        gold = label.process_correct
        conditions_view = {}
        for condition in CONDITIONS:
            row = indexed.get((condition, item_id))
            status = row.status if row is not None else "missing"
            judge = row.judge_raw if row is not None else None
            merged = row.assessment if row is not None else None
            conditions_view[condition] = {
                "status": status,
                "judge": _assessment_view(judge) if judge is not None else None,
                "merged": _assessment_view(merged) if merged is not None else None,
                "rules": (
                    [obs.model_dump(mode="json") for obs in row.rule_report.rules]
                    if row is not None and row.rule_report is not None
                    else None
                ),
                "combination_source": (
                    row.rule_report.combination_source
                    if row is not None and row.rule_report is not None
                    else None
                ),
                "rule_changed": (
                    merged != judge if judge is not None and merged is not None else None
                ),
                "outcome_judge": _gold_category(
                    status,
                    public_process_correct(judge.reasoning_correct, judge.plan_code_aligned)
                    if judge is not None
                    else None,
                    gold,
                ),
                "outcome_merged": _gold_category(
                    status,
                    public_process_correct(merged.reasoning_correct, merged.plan_code_aligned)
                    if merged is not None
                    else None,
                    gold,
                ),
                "usage": usage.get((condition, item_id), {"recorded": False}),
            }
        transitions = {}
        for treatment, baseline in COMPARISONS:
            before = conditions_view[baseline]["outcome_merged"]
            after = conditions_view[treatment]["outcome_merged"]
            transitions[f"{treatment} vs {baseline}"] = f"{before}->{after}"
        disagreement = {}
        for view_name in ("judge", "merged"):
            decided = [
                conditions_view[condition][view_name]
                for condition in CONDITIONS
                if conditions_view[condition]["status"] == "ok"
            ]
            fields = [
                field for field in DIVERGENCE_FIELDS if len({view[field] for view in decided}) > 1
            ]
            disagreement[view_name] = {"any": bool(fields), "fields": fields}
        items.append(
            {
                "item_id": item_id,
                "input_sha256": pilot.input_hash(item_id),
                "problem": item.problem.model_dump(mode="json"),
                "solution_trace": item.solution_trace.model_dump(mode="json"),
                "functional_evidence": {
                    "scope": item.functional_evidence.scope,
                    "infrastructure_status": item.functional_evidence.infrastructure_status,
                    "base_status": item.functional_evidence.base_status,
                    "plus_status": item.functional_evidence.plus_status,
                    "functional_correct": item.functional_evidence.functional_correct,
                    "candidate_code_sha256": item.functional_evidence.candidate_code_sha256,
                    "source_run_id": item.functional_evidence.source_run_id,
                },
                "conditions": conditions_view,
                "transitions": transitions,
                "disagreement": disagreement,
                "human_reference": _label_view(label),
                "case_note": CASE_NOTES.get(item_id),
            }
        )

    item_ids = set(pilot.inputs)
    missing_notes = [key for key in CASE_NOTES if key not in item_ids]
    if missing_notes:
        raise ComparisonSourceError(f"重点案例备注指向未知样本：{', '.join(sorted(missing_notes))}")

    label_counts = Counter(
        "unknown" if x.process_correct is None else "correct" if x.process_correct else "incorrect"
        for x in pilot.labels.values()
    )
    step_gold_n = sum(
        x.process_correct is False
        and x.first_faulty_step is not None
        and x.localization_status == "supported"
        for x in pilot.labels.values()
    )
    structured_gold_n = sum(x.first_faulty_location is not None for x in pilot.labels.values())

    score_views = {}
    for view_name in ("judge_raw", "rule_merged"):
        score_views[view_name] = {
            condition: {
                "coverage": scores["views"][view_name][condition]["coverage"],
                "public_process": scores["views"][view_name][condition]["dimensions"][
                    "public_process_correct"
                ],
            }
            for condition in CONDITIONS
        }
    views_identical = scores["views"]["judge_raw"] == scores["views"]["rule_merged"] and all(
        not scores["rule_effects"][condition]["changed_items"] for condition in CONDITIONS
    )

    notes = list(VIEW_NOTES)
    if views_identical:
        notes.append(
            "本轮确定性规则 0 次触发：judge_raw 与 rule_merged 两个视图完全一致，未观察到规则合并贡献。"
        )
    if experiment_meta.get("run_status") != "completed":
        notes.insert(
            0,
            f"该运行的状态为 {experiment_meta.get('run_status')}，以下仅为已完整落盘并验证的记录。",
        )

    return {
        "ok": True,
        "schema": ABLATION_VIEW_SCHEMA,
        "data_kind": "real_experiment",
        "repetition": None,
        "experiment": {
            **experiment_meta,
            "identity_label": IDENTITY_LABEL,
            "item_count": len(items),
            "conditions": [
                {
                    "id": condition,
                    "label": CONDITION_LABELS[condition],
                    "description": CONDITION_DESCRIPTIONS[condition],
                    "static_evidence": CONDITION_DEFS[condition]["static_evidence"],
                    "functional_evidence": CONDITION_DEFS[condition]["functional_evidence"],
                }
                for condition in CONDITIONS
            ],
            "label_stage": pilot.label_stage,
            "annotation_provenance": pilot.annotation_provenance,
            "label_summary": {
                "correct": label_counts.get("correct", 0),
                "incorrect": label_counts.get("incorrect", 0),
                "unknown": label_counts.get("unknown", 0),
            },
            "location_gold": {
                "first_step_supported": step_gold_n,
                "structured_location": structured_gold_n,
                "has_reference": bool(step_gold_n or structured_gold_n),
            },
        },
        "scores": {
            "views": score_views,
            "views_identical": views_identical,
            "paired_comparisons": scores["paired_comparisons"],
            "rule_effects": scores["rule_effects"],
        },
        "items": items,
        "focus_cases": [case for case in FOCUS_CASES if case["item_id"] in item_ids],
        "notes": notes,
    }


def load_ablation_comparison(
    repo_root: str | Path,
    run_dir: Path = ABLATION_RUN_DIR,
    scores_file: Path = ABLATION_SCORES_FILE,
    config_path: Path = PILOT_CONFIG,
) -> dict[str, Any]:
    """Load the pinned ablation run read-only; raises ComparisonSourceError if unverifiable."""

    root = Path(repo_root).resolve()
    run_path = _contained_run_dir(root, run_dir)
    if scores_file.is_absolute() or ".." in scores_file.parts:
        raise ComparisonSourceError("scores file must be a relative path inside the repo")
    scores_path = root.joinpath(*scores_file.parts).resolve()
    if not scores_path.is_relative_to(root):
        raise ComparisonSourceError("scores file escapes the repository root")

    report = _read_json_file(run_path / "run-report.json", "运行报告 run-report.json")
    if report.get("schema") != RUN_REPORT_SCHEMA:
        raise ComparisonSourceError("run-report.json 不是已知的消融运行报告。")
    _verify_report_hashes(run_path, report, required=REQUIRED_RUN_FILES)
    config = _read_json_file(run_path / "run-config.json", "运行身份 run-config.json")
    plan = _read_json_file(run_path / "ablation-plan.json", "预注册方案 ablation-plan.json")
    try:
        pilot = prepare_pilot(root, config_path)
    except (ValueError, OSError, KeyError) as exc:
        raise ComparisonSourceError(f"绑定材料或人工参考校验失败：{exc}") from exc
    config_raw = (
        digest((root / config_path).read_bytes())
        if not config_path.is_absolute()
        else digest(config_path.read_bytes())
    )
    _check_run_identity(config, plan, pilot, config_raw)

    predictions_raw, predictions = _parse_ablation_predictions(run_path)
    # score_ablation applies the scorer's own strictness: identity, duplicates,
    # input hashes, reference binding AND deterministic re-merge verification.
    try:
        recomputed = score_ablation(pilot, predictions, run_report=report)
    except ValueError as exc:
        raise ComparisonSourceError(f"预测记录未通过计分校验：{exc}") from exc

    scores = _read_json_file(scores_path, "计分文件 ablation-scores.json")
    _check_scores_file(scores, predictions_raw, recomputed, pilot)

    usage = _parse_usage(run_path, report, methods=set(CONDITION_LABELS))

    requests_summary = report.get("requests", {})
    experiment_meta = {
        "name": run_path.name,
        "run_dir": run_dir.as_posix(),
        "scores_file": scores_file.as_posix(),
        "run_status": report.get("status"),
        "finished_utc": report.get("finished_utc"),
        "plan_sha256": config.get("plan_sha256"),
        "seed": plan.get("seed"),
        "pilot_config": config_path.as_posix(),
        "model": config.get("model"),
        "budget": report.get("budget"),
        "judgments": report.get("judgments"),
        "requests": {
            "dispatched": requests_summary.get("dispatched"),
            "request_seconds": requests_summary.get("total_request_seconds"),
            "input_tokens": requests_summary.get("input_tokens"),
            "output_tokens": requests_summary.get("output_tokens"),
            "token_usage": requests_summary.get("token_usage"),
        },
    }
    view = build_ablation_view(pilot, predictions, scores, usage, experiment_meta=experiment_meta)
    _attach_repetition(root, view)
    return view


def _attach_repetition(root: Path, view: dict[str, Any]) -> None:
    """Attach the verified five-repetition summary to the page payload.

    The single-round table, its scores and its paired transitions are never
    touched: repetition conclusions live in their own block and are appended
    to the two focus-case notes.  If the tally is missing or fails
    verification the page keeps showing the historical single round and says
    so, instead of dropping or guessing the repetition outcome.
    """

    try:
        repetition = load_repetition_tally(root)
    except CurrentValidationError:
        view["repetition"] = None
        view["notes"].append(
            "五轮冻结重复摘要暂不可用（来源缺失或未通过校验）；当前表格为原始单轮结果，"
            "页面不以任何内容替代重复结论。"
        )
        return

    view["repetition"] = repetition
    max_product = repetition["max_product_detection"]
    division_total = repetition["division_elements_total"]
    max_product_point = (
        "五轮冻结重复已完成：D 只在 "
        f"{max_product['ablation_d'][0]}/{max_product['ablation_d'][1]} 轮检出，"
        f"A 为 {max_product['ablation_a'][0]}/{max_product['ablation_a'][1]}，"
        f"B 为 {max_product['ablation_b'][0]}/{max_product['ablation_b'][1]}（一次 Provider 超时），"
        f"C 为 {max_product['ablation_c'][0]}/{max_product['ablation_c'][1]}，"
        "因此 rep1 的 D 检出没有稳定复现。"
    )
    division_point = (
        f"五轮冻结重复中，四条件五轮合计 {division_total[0]}/{division_total[1]} 检出，"
        "是稳定漏检案例。"
    )
    for item in view["items"]:
        note = item.get("case_note")
        if note is None:
            continue
        note = dict(note)
        note["points"] = list(note["points"])
        if item["item_id"] == MAX_PRODUCT_ITEM:
            note["points"].insert(1, max_product_point)
        elif item["item_id"] == DIVISION_ELEMENTS_ITEM:
            note["points"].append(division_point)
        item["case_note"] = note


# ------------------------------------------------------- synthetic UI fixture


def synthetic_ablation_comparison() -> dict[str, Any]:
    """Clearly marked synthetic payload for exercising page states only.

    This data is NOT an experiment result: it exists so the ablation page's
    agreement, divergence, rule-merge, unknown, failure, missing and long-text
    states can be checked without touching the real run.  It is served only via
    an explicit allowlisted query and is always labelled 界面测试数据 in the UI.
    """

    long_code = "\n".join(
        f"def stage_{index:02d}(value):  # synthetic filler line {index}\n    return value"
        for index in range(1, 41)
    )
    long_text = "这是一段用于验证长文本折叠与滚动行为的合成说明。" * 12

    def assessment(reasoning, aligned, explanation="合成解释文本，仅用于界面状态验证。", **extra):
        view = {
            "reasoning_correct": reasoning,
            "plan_code_aligned": aligned,
            "public_process_correct": public_process_correct(reasoning, aligned),
            "functional_correct": None,
            "joint_process_correct": public_process_correct(reasoning, aligned),
            "first_faulty_layer": None,
            "first_faulty_step": None,
            "first_faulty_location": None,
            "error_type": None,
            "secondary_error_types": [],
            "violated_requirement": None,
            "code_span": None,
            "affected_steps": [],
            "explanation": explanation,
            "confidence": None,
            "references": [],
        }
        view.update(extra)
        return view

    def usage(requests=1, seconds=9.5, tokens=(800, 600)):
        return {
            "recorded": True,
            "requests": requests,
            "request_seconds": seconds,
            "input_tokens": tokens[0] if tokens else None,
            "output_tokens": tokens[1] if tokens else None,
            "token_coverage": "1/1" if tokens else "0/1",
        }

    def problem(title, requirement="合成需求：返回输入列表中所有偶数的平方和。"):
        return {
            "title": title,
            "requirement": requirement,
            "function_signature": "def solve(values: list[int]) -> int:",
            "requirements": [
                {"requirement_id": "R1", "content": "仅统计偶数元素。"},
                {"requirement_id": "R2", "content": "空列表返回 0。"},
            ],
        }

    def trace(code="def solve(values):\n    return sum(v * v for v in values if v % 2 == 0)\n"):
        return {
            "problem_id": "synthetic",
            "requirement_understanding": "合成需求理解：筛选偶数并平方求和。",
            "design_summary": "合成设计摘要：生成器表达式一次遍历。",
            "edge_cases_considered": ["空列表返回 0。"],
            "implementation_steps": [
                {
                    "step_id": "S1",
                    "content": "筛选偶数元素。",
                    "related_requirements": ["R1"],
                    "expected_code_behavior": None,
                },
                {
                    "step_id": "S2",
                    "content": "平方后求和，空列表得 0。",
                    "related_requirements": ["R2"],
                    "expected_code_behavior": None,
                },
            ],
            "declared_time_complexity": "O(n)",
            "declared_space_complexity": "O(1)",
            "code": code,
        }

    def functional(correct):
        return {
            "scope": "evalplus_mbpp_base_and_plus",
            "infrastructure_status": "ok" if correct is not None else "unknown",
            "base_status": "pass" if correct else "fail" if correct is False else "unknown",
            "plus_status": "pass" if correct else "fail" if correct is False else "unknown",
            "functional_correct": correct,
            "candidate_code_sha256": "0" * 64,
            "source_run_id": "synthetic",
        }

    def human(reasoning, aligned, **extra):
        view = {
            "reasoning_correct": reasoning,
            "plan_code_aligned": aligned,
            "process_correct": public_process_correct(reasoning, aligned),
            "localization_status": "not_applicable",
            "first_faulty_layer": None,
            "first_faulty_step": None,
            "first_faulty_location": None,
            "error_type": None,
            "evidence": [],
            "rationale": "合成人工参考理由，仅用于界面验证。",
        }
        view.update(extra)
        return view

    def condition(
        status,
        judge_view=None,
        merged_view=None,
        rules=None,
        combination_source=None,
        usage_view=None,
        outcome_judge="tn",
        outcome_merged="tn",
    ):
        return {
            "status": status,
            "judge": judge_view,
            "merged": merged_view,
            "rules": rules,
            "combination_source": combination_source,
            "rule_changed": (
                merged_view != judge_view
                if judge_view is not None and merged_view is not None
                else None
            ),
            "outcome_judge": outcome_judge,
            "outcome_merged": outcome_merged,
            "usage": usage_view if usage_view is not None else usage(),
        }

    def no_rules():
        return [
            {
                "rule_id": f"rule_{index}",
                "fired": False,
                "first_faulty_layer": None,
                "error_type": None,
                "basis": None,
            }
            for index in range(1, 6)
        ]

    def item(item_id, prob, tr, func, conditions_view, disagreement, human_view, case_note=None):
        transitions = {}
        for treatment, baseline in COMPARISONS:
            before = conditions_view[baseline]["outcome_merged"]
            after = conditions_view[treatment]["outcome_merged"]
            transitions[f"{treatment} vs {baseline}"] = f"{before}->{after}"
        return {
            "item_id": item_id,
            "input_sha256": "2" * 64,
            "problem": prob,
            "solution_trace": tr,
            "functional_evidence": func,
            "conditions": conditions_view,
            "transitions": transitions,
            "disagreement": disagreement,
            "human_reference": human_view,
            "case_note": case_note,
        }

    located_judge = assessment(
        False,
        True,
        explanation=(
            "合成解释：D 条件检出了该样本。包含不可信文本探针："
            "<b>不应渲染为HTML</b><img src=x onerror=window.__tj_xss=1>。"
        ),
        first_faulty_layer="reasoning",
        first_faulty_step="S1",
        first_faulty_location={
            "source_field": "implementation_steps",
            "quote": "筛选偶数元素",
            "step_id": "S1",
            "entry_index": None,
            "code_span": None,
        },
        error_type="P01_ALGORITHM_ERROR",
        references=[
            {
                "kind": "quote",
                "label": "首错位置 · implementation_steps",
                "quote": "筛选偶数元素",
                "target": {
                    "type": "step",
                    "field": "implementation_steps",
                    "step_id": "S1",
                    "entry_index": None,
                    "code_span": None,
                },
                "verified": True,
            },
            {
                "kind": "step",
                "label": "步骤 S1",
                "quote": None,
                "target": {"type": "step", "step_id": "S1"},
                "verified": True,
            },
        ],
    )

    rule_fired = [
        {
            "rule_id": "empty_input_claim",
            "fired": True,
            "first_faulty_layer": "plan_code",
            "error_type": "A01_PLAN_CODE_MISMATCH",
            "basis": "合成规则依据：步骤声称存在空输入分支，代码中没有对应实现。",
        },
        *no_rules()[1:],
    ]

    all_tn = {
        condition_id: condition(
            "ok", assessment(True, True), assessment(True, True), no_rules(), "llm"
        )
        for condition_id in CONDITIONS
    }

    diverge = {
        "ablation_a": condition(
            "ok",
            assessment(True, True),
            assessment(True, True),
            no_rules(),
            "llm",
            outcome_judge="fn",
            outcome_merged="fn",
        ),
        "ablation_b": condition(
            "ok",
            assessment(True, True),
            assessment(True, True),
            no_rules(),
            "llm",
            outcome_judge="fn",
            outcome_merged="fn",
        ),
        "ablation_c": condition(
            "ok",
            assessment(True, True),
            assessment(True, True),
            no_rules(),
            "llm",
            outcome_judge="fn",
            outcome_merged="fn",
        ),
        "ablation_d": condition(
            "ok",
            located_judge,
            located_judge,
            no_rules(),
            "llm",
            outcome_judge="tp",
            outcome_merged="tp",
        ),
    }

    rule_change_judge = assessment(True, True, explanation="合成解释：Judge 未检出。")
    rule_change_merged = assessment(
        True,
        False,
        explanation="合成解释：确定性规则合并后判定计划与代码不一致。",
        first_faulty_layer="plan_code",
        first_faulty_step="S2",
        error_type="A01_PLAN_CODE_MISMATCH",
    )
    rule_changed_conditions = {
        condition_id: condition(
            "ok",
            rule_change_judge,
            rule_change_merged if condition_id == "ablation_b" else rule_change_judge,
            rule_fired if condition_id == "ablation_b" else no_rules(),
            "rule" if condition_id == "ablation_b" else "llm",
            outcome_judge="fp",
            outcome_merged="tn" if condition_id == "ablation_b" else "fp",
        )
        for condition_id in CONDITIONS
    }

    unknown_failed = {
        "ablation_a": condition(
            "ok",
            assessment(None, None),
            assessment(None, None),
            no_rules(),
            "llm",
            outcome_judge="abstained",
            outcome_merged="abstained",
        ),
        "ablation_b": condition(
            "provider_error",
            None,
            None,
            None,
            None,
            usage_view={"recorded": False},
            outcome_judge="failed",
            outcome_merged="failed",
        ),
        "ablation_c": condition(
            "ok",
            assessment(True, True),
            assessment(True, True),
            no_rules(),
            "llm",
            outcome_judge="gold_unknown",
            outcome_merged="gold_unknown",
        ),
        "ablation_d": condition(
            "ok",
            assessment(True, True),
            assessment(True, True),
            no_rules(),
            "llm",
            outcome_judge="gold_unknown",
            outcome_merged="gold_unknown",
        ),
    }

    missing_long = {
        "ablation_a": condition(
            "ok", assessment(True, True), assessment(True, True), no_rules(), "llm"
        ),
        "ablation_b": condition(
            "missing",
            None,
            None,
            None,
            None,
            usage_view={"recorded": False},
            outcome_judge="missing",
            outcome_merged="missing",
        ),
        "ablation_c": condition(
            "parse_error",
            None,
            None,
            None,
            None,
            usage_view=usage(requests=2, seconds=31.4, tokens=None),
            outcome_judge="failed",
            outcome_merged="failed",
        ),
        "ablation_d": condition(
            "ok", assessment(True, True), assessment(True, True), no_rules(), "llm"
        ),
    }

    synthetic_case_note = {
        "kind": "judgment_change",
        "title": "合成案例备注 · 条件分歧展示（非真实实验结论）",
        "review_doc": None,
        "points": ["这是合成备注文本，仅用于验证重点案例区域的展示状态。"],
        "historical_reference": "合成历史参考文本，仅用于验证历史参考区域的展示状态。",
    }

    items = [
        item(
            "ui-fixture-agree",
            problem("合成样本 · 四条件一致"),
            trace(),
            functional(True),
            all_tn,
            {"judge": {"any": False, "fields": []}, "merged": {"any": False, "fields": []}},
            human(True, True),
        ),
        item(
            "ui-fixture-diverge",
            problem("合成样本 · 仅 D 检出（重点案例形态）"),
            trace(),
            functional(False),
            diverge,
            {
                "judge": {
                    "any": True,
                    "fields": [
                        "reasoning_correct",
                        "public_process_correct",
                        "first_faulty_layer",
                        "first_faulty_step",
                        "error_type",
                    ],
                },
                "merged": {
                    "any": True,
                    "fields": [
                        "reasoning_correct",
                        "public_process_correct",
                        "first_faulty_layer",
                        "first_faulty_step",
                        "error_type",
                    ],
                },
            },
            human(
                False,
                True,
                localization_status="supported",
                first_faulty_layer="reasoning",
                error_type="P01_ALGORITHM_ERROR",
                evidence=[
                    {
                        "step_id": "S1",
                        "code_span": None,
                        "requirement_id": None,
                        "description": "合成证据：S1 的筛选条件有误。",
                    }
                ],
            ),
            case_note=synthetic_case_note,
        ),
        item(
            "ui-fixture-rule-merge",
            problem("合成样本 · 规则合并改变判断（B 条件）"),
            trace(),
            functional(True),
            rule_changed_conditions,
            {
                "judge": {"any": False, "fields": []},
                "merged": {"any": True, "fields": ["plan_code_aligned", "public_process_correct"]},
            },
            human(True, True),
        ),
        item(
            "ui-fixture-unknown-failed",
            problem("合成样本 · 参考未知、弃答与调用失败"),
            trace(),
            functional(None),
            unknown_failed,
            {"judge": {"any": False, "fields": []}, "merged": {"any": False, "fields": []}},
            human(None, None, localization_status="unknown"),
        ),
        item(
            "ui-fixture-missing-long",
            problem("合成样本 · 结果缺失与长文本", requirement=long_text),
            trace(code=long_code),
            functional(True),
            missing_long,
            {"judge": {"any": False, "fields": []}, "merged": {"any": False, "fields": []}},
            human(True, True),
        ),
    ]

    def scores_block(tp, fp, tn, fn, known, total):
        return {
            "coverage": {
                "ok": total,
                "missing": 0,
                "provider_error": 0,
                "parse_error": 0,
            },
            "public_process": {
                "gold_known": known,
                "gold_unknown": total - known,
                "tp": tp,
                "fp": fp,
                "tn": tn,
                "fn": fn,
                "known_abstained": 0,
                "known_failed": 0,
                "known_missing": 0,
                "unknown_decided": total - known,
                "unknown_abstained": 0,
                "unknown_failed": 0,
                "unknown_missing": 0,
                "accuracy_all_known": {
                    "numerator": tp + tn,
                    "denominator": known,
                    "value": (tp + tn) / known if known else None,
                },
                "decision_coverage_known": {"numerator": known, "denominator": known, "value": 1.0},
                "error_precision_decided": {
                    "numerator": tp,
                    "denominator": tp + fp,
                    "value": tp / (tp + fp) if (tp + fp) else None,
                },
                "error_recall_decided": {
                    "numerator": tp,
                    "denominator": tp + fn,
                    "value": tp / (tp + fn) if (tp + fn) else None,
                },
                "false_positive_rate_decided": {
                    "numerator": fp,
                    "denominator": fp + tn,
                    "value": fp / (fp + tn) if (fp + tn) else None,
                },
            },
        }

    synthetic_scores = {
        "views": {
            "judge_raw": {
                "ablation_a": scores_block(0, 0, 2, 1, 3, 5),
                "ablation_b": scores_block(0, 0, 2, 1, 3, 5),
                "ablation_c": scores_block(0, 0, 2, 1, 3, 5),
                "ablation_d": scores_block(1, 0, 2, 0, 3, 5),
            },
            "rule_merged": {
                "ablation_a": scores_block(0, 0, 2, 1, 3, 5),
                "ablation_b": scores_block(0, 0, 2, 1, 3, 5),
                "ablation_c": scores_block(0, 0, 2, 1, 3, 5),
                "ablation_d": scores_block(1, 0, 2, 0, 3, 5),
            },
        },
        "views_identical": False,
        "paired_comparisons": [
            {
                "treatment": "ablation_d",
                "baseline": "ablation_b",
                "view": "rule-merged final judgments on the public process signal",
                "transitions": {"tn->tn": 3, "fn->tp": 1, "gold_unknown->gold_unknown": 1},
                "changed_items": {"ui-fixture-diverge": "fn->tp"},
            }
        ],
        "rule_effects": {
            condition_id: {
                "rules_fired": {"empty_input_claim": 1} if condition_id == "ablation_b" else {},
                "changed_items": (
                    [
                        {
                            "item_id": "ui-fixture-rule-merge",
                            "changes": {"plan_code_aligned": {"judge": True, "merged": False}},
                        }
                    ]
                    if condition_id == "ablation_b"
                    else []
                ),
                "note": "synthetic",
            }
            for condition_id in CONDITIONS
        },
    }

    return {
        "ok": True,
        "schema": ABLATION_VIEW_SCHEMA,
        "data_kind": "synthetic_ui_test",
        "repetition": None,
        "experiment": {
            "name": "界面测试数据（合成，非实验结果）",
            "identity_label": "合成界面数据 · 非实验身份",
            "run_dir": "synthetic://ui-fixture",
            "scores_file": "synthetic://ui-fixture",
            "run_status": "synthetic",
            "finished_utc": None,
            "plan_sha256": None,
            "seed": None,
            "pilot_config": None,
            "model": None,
            "budget": None,
            "judgments": None,
            "requests": {
                "dispatched": None,
                "request_seconds": None,
                "input_tokens": None,
                "output_tokens": None,
                "token_usage": "missing",
            },
            "item_count": len(items),
            "conditions": [
                {
                    "id": condition_id,
                    "label": CONDITION_LABELS[condition_id],
                    "description": CONDITION_DESCRIPTIONS[condition_id],
                    "static_evidence": CONDITION_DEFS[condition_id]["static_evidence"],
                    "functional_evidence": CONDITION_DEFS[condition_id]["functional_evidence"],
                }
                for condition_id in CONDITIONS
            ],
            "label_stage": "synthetic_ui_test",
            "annotation_provenance": "synthetic_ui_test",
            "label_summary": {"correct": 2, "incorrect": 1, "unknown": 1},
            "location_gold": {
                "first_step_supported": 0,
                "structured_location": 0,
                "has_reference": False,
            },
        },
        "scores": synthetic_scores,
        "items": items,
        "focus_cases": [
            {"item_id": "ui-fixture-diverge", "label": "合成 · 仅 D 检出"},
        ],
        "notes": [
            "这是界面测试数据：用于验证四条件一致、条件分歧、规则合并改变判断、参考未知、失败、缺失与长文本等展示状态，不是实验结果，不得计入任何成绩。",
        ],
    }
