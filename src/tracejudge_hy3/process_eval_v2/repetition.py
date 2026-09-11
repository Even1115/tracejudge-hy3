"""Offline tally of repeated fixed-configuration ablation runs.

The repetition experiment (pre-registered in
docs/experiments/process-pilot-ablation-v1.md, "重复验证方案") runs the SAME
pre-registered plan n times and reports the per-sample detection frequency
across ALL repetitions -- no round is ever dropped or cherry-picked.

This module is purely offline: it never constructs a provider and never
executes candidate code.  Every repetition is verified before it is pooled:

- each run directory is an explicit, repository-relative path (never
  "latest"); ``run-report.json`` must pin the SHA-256 of the predictions,
  requests ledger, run config and plan files, and every pinned hash must
  match the bytes on disk;
- the run identity must bind the current pilot configuration, the four
  conditions, the recomputed condition fingerprints and the per-item input
  hashes; the stored plan's fingerprint must equal the run's plan hash;
- predictions are validated by the offline scorer ``score_ablation`` itself
  (identity, duplicates, input hashes, reference binding AND the
  deterministic re-merge of every judge output);
- across repetitions the identity fields (pilot config, plan hash, input
  hashes, conditions, condition fingerprints, prediction schema, model and
  implementation fingerprints) must be IDENTICAL -- a repetition run under a
  different configuration is refused, never silently pooled.

Detection frequencies count only completed, non-abstained judgments;
abstentions, failures and missing judgments are counted separately and are
never treated as either detection or non-detection.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tracejudge_hy3.process_eval_v2.ablation import (
    CONDITIONS,
    PLAN_SCHEMA,
    RUN_CONFIG_SCHEMA,
    RUN_REPORT_SCHEMA,
    AblationPrediction,
    condition_fingerprints,
    plan_fingerprint,
)
from tracejudge_hy3.process_eval_v2.contracts import public_process_correct
from tracejudge_hy3.process_eval_v2.materials import digest
from tracejudge_hy3.process_eval_v2.preflight import (
    PreparedPilot,
    contained,
    prepare_pilot,
    read_json,
    read_rows,
)
from tracejudge_hy3.process_eval_v2.scoring import score_ablation

TALLY_SCHEMA = "tracejudge-process-ablation-repetition-tally-v1"

REPORT_FILE = "run-report.json"
PREDICTIONS_FILE = "predictions.jsonl"
REQUESTS_FILE = "requests.jsonl"
CONFIG_FILE = "run-config.json"
PLAN_FILE = "ablation-plan.json"
RUN_FILES = (PREDICTIONS_FILE, REQUESTS_FILE, CONFIG_FILE, PLAN_FILE)

#: Fields that must be identical across every repetition of a fixed-config
#: experiment.  ``budget``/``run_identity`` runtime consumption is excluded;
#: configuration is not.
IDENTITY_FIELDS = (
    "pilot_config_sha256",
    "plan_sha256",
    "input_hashes",
    "conditions",
    "condition_fingerprints",
    "prediction_schema_sha256",
    "model",
    "implementation_sha256",
)

DECISION_LABELS = {True: "correct", False: "incorrect", None: "abstained"}


def _read_json(path: Path, description: str) -> dict:
    try:
        value = read_json(path.read_bytes())
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"无法读取{description}（{path}）：{exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{description}不是 JSON 对象：{path}")
    return value


def _verify_pinned_files(run_path: Path, report: dict, run_dir: str) -> None:
    pinned = report.get("files_sha256")
    if not isinstance(pinned, dict):
        raise ValueError(f"{run_dir}：run-report.json 缺少 files_sha256 钉板")
    for name in RUN_FILES:
        expected = pinned.get(name)
        if not isinstance(expected, str):
            raise ValueError(f"{run_dir}：run-report.json 未钉住 {name}")
        path = run_path / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"{run_dir}：{name} 缺失或是符号链接")
        if digest(path.read_bytes()) != expected:
            raise ValueError(f"{run_dir}：{name} 与运行报告钉住的 SHA-256 不一致，拒绝纳入")


def _check_rep_identity(
    run_dir: str,
    config: dict,
    plan: dict,
    pilot: PreparedPilot,
    config_sha256: str,
) -> None:
    if config.get("schema") != RUN_CONFIG_SCHEMA:
        raise ValueError(f"{run_dir}：run-config.json 不是已知的消融运行身份")
    if config.get("pilot_config_sha256") != config_sha256:
        raise ValueError(f"{run_dir}：运行绑定的是另一份 pilot 配置，拒绝混跑")
    if config.get("conditions") != list(CONDITIONS):
        raise ValueError(f"{run_dir}：条件集合与消融定义不一致，拒绝混跑")
    if config.get("condition_fingerprints") != condition_fingerprints():
        raise ValueError(f"{run_dir}：条件提示词/可见字段/输出契约指纹与当前实现不一致")
    expected_hashes = {item_id: pilot.input_hash(item_id) for item_id in pilot.inputs}
    if config.get("input_hashes") != expected_hashes:
        raise ValueError(f"{run_dir}：逐条输入哈希与当前绑定材料不一致，拒绝混跑")
    if plan.get("schema") != PLAN_SCHEMA:
        raise ValueError(f"{run_dir}：ablation-plan.json 不是已知的消融方案")
    if plan_fingerprint(plan) != config.get("plan_sha256"):
        raise ValueError(f"{run_dir}：方案内容与运行绑定的方案哈希不一致")
    if plan.get("items") != expected_hashes:
        raise ValueError(f"{run_dir}：预注册方案的输入哈希与当前绑定材料不一致")
    order = plan.get("execution_order")
    expected_pairs = {(condition, item_id) for condition in CONDITIONS for item_id in pilot.inputs}
    if (
        not isinstance(order, list)
        or len(order) != len(expected_pairs)
        or {(pair[0], pair[1]) for pair in order if isinstance(pair, list) and len(pair) == 2}
        != expected_pairs
    ):
        raise ValueError(f"{run_dir}：预注册方案的执行顺序不完整或含未知条目")


def _parse_predictions(run_path: Path, run_dir: str) -> list[AblationPrediction]:
    try:
        rows = read_rows((run_path / PREDICTIONS_FILE).read_bytes())
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"{run_dir}：predictions.jsonl 含损坏记录：{exc}") from exc
    predictions = []
    for row in rows:
        try:
            predictions.append(AblationPrediction.model_validate(row))
        except ValueError as exc:
            raise ValueError(f"{run_dir}：predictions.jsonl 含不符合契约的记录：{exc}") from exc
    return predictions


def _decision(prediction: AblationPrediction | None, view: str) -> str | None:
    """'correct'/'incorrect'/'abstained' for an ok judgment, else None."""

    if prediction is None or prediction.status != "ok":
        return None
    assessment = prediction.assessment if view == "merged" else prediction.judge_raw
    observed = public_process_correct(assessment.reasoning_correct, assessment.plan_code_aligned)
    return DECISION_LABELS[observed]


def _load_repetition(
    root: Path, run_dir: str, pilot: PreparedPilot, config_sha256: str
) -> dict[str, Any]:
    run_path = contained(root, run_dir)
    if run_path.is_symlink() or not run_path.is_dir():
        raise ValueError(f"{run_dir}：运行目录缺失或是符号链接")
    report = _read_json(run_path / REPORT_FILE, "运行报告 run-report.json")
    if report.get("schema") != RUN_REPORT_SCHEMA:
        raise ValueError(f"{run_dir}：run-report.json 不是已知的消融运行报告")
    _verify_pinned_files(run_path, report, run_dir)
    config = _read_json(run_path / CONFIG_FILE, "运行身份 run-config.json")
    plan = _read_json(run_path / PLAN_FILE, "预注册方案 ablation-plan.json")
    _check_rep_identity(run_dir, config, plan, pilot, config_sha256)
    predictions = _parse_predictions(run_path, run_dir)
    try:
        scores = score_ablation(pilot, predictions, run_report=report)
    except ValueError as exc:
        raise ValueError(f"{run_dir}：预测记录未通过计分校验：{exc}") from exc
    indexed = {(row.condition, row.item_id): row for row in predictions}
    return {
        "run_dir": run_dir,
        "report": report,
        "identity": config,
        "scores": scores,
        "indexed": indexed,
    }


def tally_repetitions(
    root: Path,
    config_path: Path,
    run_dirs: list[str],
    *,
    expect_plan_sha256: str | None = None,
) -> dict[str, Any]:
    """Verify and pool repeated ablation runs; raises ValueError on any mismatch.

    ``run_dirs`` are repository-relative paths in repetition order.  Every
    listed repetition is reported; the tally never selects a subset.
    """

    if not run_dirs:
        raise ValueError("至少需要一轮运行目录")
    if len(set(run_dirs)) != len(run_dirs):
        raise ValueError("运行目录重复；同一轮不能被计入两次")
    root = root.resolve()
    pilot = prepare_pilot(root, config_path)
    config_file = config_path if config_path.is_absolute() else root / config_path
    config_sha256 = digest(config_file.read_bytes())

    reps = [_load_repetition(root, run_dir, pilot, config_sha256) for run_dir in run_dirs]

    reference = reps[0]["identity"]
    for index, rep in enumerate(reps[1:], start=2):
        for field in IDENTITY_FIELDS:
            if rep["identity"].get(field) != reference.get(field):
                raise ValueError(
                    f"第 {index} 轮（{rep['run_dir']}）的 {field} 与第 1 轮（{reps[0]['run_dir']}）"
                    "不一致：固定配置重复不允许混跑，拒绝汇总"
                )
    plan_sha256 = reference.get("plan_sha256")
    if expect_plan_sha256 is not None and plan_sha256 != expect_plan_sha256:
        raise ValueError(
            f"运行绑定的方案哈希 {plan_sha256} 与预登记值 {expect_plan_sha256} 不一致，拒绝汇总"
        )

    repetitions_meta = []
    for rep in reps:
        report = rep["report"]
        requests = report.get("requests", {})
        repetitions_meta.append(
            {
                "run_dir": rep["run_dir"],
                "status": report.get("status"),
                "stop_reason": report.get("stop_reason"),
                "finished_utc": report.get("finished_utc"),
                "requests": {
                    "dispatched": requests.get("dispatched"),
                    "total_request_seconds": requests.get("total_request_seconds"),
                    "input_tokens": requests.get("input_tokens"),
                    "output_tokens": requests.get("output_tokens"),
                    "token_usage": requests.get("token_usage"),
                },
                "judgments": report.get("judgments"),
                "money": "unknown_not_reported_by_provider",
            }
        )

    items: dict[str, Any] = {}
    flapping = []
    for item_id in sorted(pilot.inputs):
        gold = pilot.labels[item_id].process_correct
        conditions_block = {}
        for condition in CONDITIONS:
            per_rep = []
            for rep in reps:
                prediction = rep["indexed"].get((condition, item_id))
                outcome_row = rep["scores"]["per_sample"][item_id]["conditions"][condition]
                per_rep.append(
                    {
                        "run_dir": rep["run_dir"],
                        "status": prediction.status if prediction is not None else "missing",
                        "decision_merged": _decision(prediction, "merged"),
                        "decision_judge": _decision(prediction, "judge"),
                        "outcome_merged": outcome_row["merged"],
                        "outcome_judge": outcome_row["judge"],
                    }
                )
            counts = {"merged": {}, "judge": {}}
            for view in ("merged", "judge"):
                key = f"decision_{view}"
                counts[view] = {
                    "judged_incorrect": sum(1 for row in per_rep if row[key] == "incorrect"),
                    "judged_correct": sum(1 for row in per_rep if row[key] == "correct"),
                    "abstained": sum(1 for row in per_rep if row[key] == "abstained"),
                    "failed": sum(1 for row in per_rep if row["status"] not in ("ok", "missing")),
                    "missing": sum(1 for row in per_rep if row["status"] == "missing"),
                    "repetitions": len(per_rep),
                }
            decided = counts["merged"]["judged_incorrect"] + counts["merged"]["judged_correct"]
            detected = counts["merged"]["judged_incorrect"]
            if 0 < detected < decided:
                flapping.append(
                    {
                        "item_id": item_id,
                        "condition": condition,
                        "judged_incorrect_merged": detected,
                        "decided_repetitions": decided,
                    }
                )
            conditions_block[condition] = {
                "repetitions": per_rep,
                "merged": {
                    **counts["merged"],
                    "detection_frequency": {
                        "numerator": detected,
                        "denominator": decided,
                        "value": detected / decided if decided else None,
                    },
                },
                "judge": {
                    **counts["judge"],
                    "detection_frequency": {
                        "numerator": counts["judge"]["judged_incorrect"],
                        "denominator": counts["judge"]["judged_incorrect"]
                        + counts["judge"]["judged_correct"],
                        "value": (
                            counts["judge"]["judged_incorrect"]
                            / (
                                counts["judge"]["judged_incorrect"]
                                + counts["judge"]["judged_correct"]
                            )
                            if (
                                counts["judge"]["judged_incorrect"]
                                + counts["judge"]["judged_correct"]
                            )
                            else None
                        ),
                    },
                },
            }
        items[item_id] = {
            "gold_process_correct": gold,
            "conditions": conditions_block,
        }

    return {
        "schema": TALLY_SCHEMA,
        "pilot_config_sha256": config_sha256,
        "plan_sha256": plan_sha256,
        "expect_plan_sha256": expect_plan_sha256,
        "repetition_count": len(reps),
        "run_dirs": [rep["run_dir"] for rep in reps],
        "identity_fields_checked": list(IDENTITY_FIELDS),
        "repetitions": repetitions_meta,
        "items": items,
        "flapping": flapping,
        "per_repetition_scores": [
            {
                "run_dir": rep["run_dir"],
                "views": rep["scores"]["views"],
                "paired_comparisons": rep["scores"]["paired_comparisons"],
                "rule_effects": rep["scores"]["rule_effects"],
            }
            for rep in reps
        ],
        "notes": [
            "检出频率的分母是已完成且未弃答的轮次；弃答、调用失败与结果缺失分别计数，既不算检出也不算未检出",
            "本报告覆盖命令行登记的全部轮次，逐轮结果完整列出，不做择优挑选",
            "每轮数字来自 score_ablation 对预测记录的离线重算（含确定性重合并核验）；未通过核验的轮次在加载阶段即被拒绝",
            "金额未由 Provider 返回，成本保持未知，不做估算",
            "小样本频率只如实报告计数与逐轮清单，不做统计显著性或泛化声明",
            "人工过程标签是反馈后的开发集修订共识，不是独立留出测试集金标",
        ],
    }
