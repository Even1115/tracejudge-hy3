"""Read-only adapter for the three-method process-pilot comparison view.

The page renders one frozen candidate judged by ``direct_judge``,
``structured_judge`` and ``full_system`` side by side.  This module only READS
artifacts that were written and hash-pinned by the pilot runner; it never calls
a provider, never executes candidate code and never modifies source files.

Trust boundaries enforced here (any failure raises ``ComparisonSourceError``
and the endpoint reports "results not ready" instead of guessing):

- the run directory is an explicit, configured path -- never "latest" and
  never client-supplied;
- ``run-report.json`` pins the SHA-256 of ``predictions.jsonl``,
  ``requests.jsonl`` and ``run-config.json``; a mismatch means the run is
  still being written or was modified, and the data is refused;
- ``run-config.json`` must bind the exact pilot configuration and per-item
  input hashes reproduced by ``prepare_pilot`` (which itself hash-verifies the
  reviewed annotation sources);
- predictions are revalidated with the same checks as the offline scorer
  (identity, duplicates, input hashes, reference binding) by calling
  ``score_predictions`` itself;
- human labels come only from the hash-bound, review-audited sources loaded by
  ``prepare_pilot`` -- never from a working copy.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from tracejudge_hy3.process_eval_v2.contracts import (
    METHODS,
    Prediction,
    public_process_correct,
)
from tracejudge_hy3.process_eval_v2.materials import digest
from tracejudge_hy3.process_eval_v2.preflight import (
    PreparedPilot,
    prepare_pilot,
    read_json,
    read_rows,
)
from tracejudge_hy3.process_eval_v2.scoring import score_predictions

# Explicit, configured inputs.  Nothing here is auto-discovered: pointing the
# page at a newer run means editing this constant (and the docs) deliberately.
PILOT_CONFIG = Path("data/manifests/process_pilot_v1.json")
PILOT_RUN_DIR = Path("artifacts/experiments/process-pilot/mbpp12-run-20260909-v1")

COMPARISON_SCHEMA = "tracejudge-demo-method-comparison-v1"

#: Identity of the displayed experiment: the historical three-method pilot.
#: The 2026-09-10 36-item validation used baseline/assumption-audit instead
#: and is deliberately NOT shown here; the two must never be merged or ranked
#: together.
IDENTITY_LABEL = "历史三方法试点 · 2026-09-09"

METHOD_LABELS = {
    "direct_judge": "Direct Judge",
    "structured_judge": "Structured Judge",
    "full_system": "Full System",
}

# Scalar fields compared across methods for divergence highlighting.
DIVERGENCE_FIELDS = (
    "reasoning_correct",
    "plan_code_aligned",
    "public_process_correct",
    "first_faulty_layer",
    "first_faulty_step",
    "error_type",
)

GOLD_DIMENSIONS = ("reasoning_correct", "plan_code_aligned", "public_process_correct")


class ComparisonSourceError(Exception):
    """The pinned run artifacts are missing, inconsistent or still being written."""


def _contained_run_dir(repo_root: Path, run_dir: Path) -> Path:
    part = PurePosixPath(run_dir.as_posix())
    if run_dir.is_absolute() or ".." in part.parts:
        raise ComparisonSourceError("run directory must be a relative path inside the repo")
    path = repo_root.joinpath(*part.parts)
    resolved = path.resolve()
    if not resolved.is_relative_to(repo_root.resolve()):
        raise ComparisonSourceError("run directory escapes the repository root")
    if path.is_symlink() or not resolved.is_dir():
        raise ComparisonSourceError("实验结果尚未就绪：配置的运行目录不存在。")
    return resolved


def _read_json_file(path: Path, description: str) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ComparisonSourceError(f"实验结果尚未就绪：缺少 {description}。")
    try:
        value = read_json(path.read_bytes())
    except (ValueError, UnicodeDecodeError) as exc:
        raise ComparisonSourceError(f"{description} 无法解析：{exc}") from exc
    if not isinstance(value, dict):
        raise ComparisonSourceError(f"{description} 不是 JSON 对象。")
    return value


def _verify_report_hashes(
    run_path: Path, report: dict, *, required: set[str] | None = None
) -> dict[str, str]:
    pinned = report.get("files_sha256")
    if not isinstance(pinned, dict):
        raise ComparisonSourceError("run-report.json 缺少文件哈希清单，无法验证结果完整性。")
    if required is None:
        required = {"predictions.jsonl", "requests.jsonl", "run-config.json"}
    if set(pinned) != required:
        raise ComparisonSourceError("run-report.json 的文件哈希清单与预期不符。")
    for name, expected in pinned.items():
        target = run_path / name
        if target.is_symlink() or not target.is_file():
            raise ComparisonSourceError(f"实验结果尚未就绪：缺少 {name}。")
        if not isinstance(expected, str) or digest(target.read_bytes()) != expected:
            raise ComparisonSourceError(
                f"{name} 与运行报告记录的哈希不一致；结果可能仍在写入，页面不展示未验证的数据。"
            )
    return pinned


def _check_run_identity(config: dict, pilot: PreparedPilot, config_raw_sha: str) -> None:
    if config.get("schema") != "tracejudge-process-pilot-run-config-v1":
        raise ComparisonSourceError("run-config.json 不是已知的 pilot 运行身份。")
    if config.get("pilot_config_sha256") != config_raw_sha:
        raise ComparisonSourceError("运行绑定的是另一份 pilot 配置，拒绝混用。")
    if config.get("methods") != list(METHODS):
        raise ComparisonSourceError("运行的方法集合与对比页定义不一致。")
    expected_hashes = {item_id: pilot.input_hash(item_id) for item_id in pilot.inputs}
    if config.get("input_hashes") != expected_hashes:
        raise ComparisonSourceError("运行的逐条输入哈希与当前绑定材料不一致，拒绝混用。")


def _parse_predictions(run_path: Path) -> list[Prediction]:
    raw = (run_path / "predictions.jsonl").read_bytes()
    try:
        rows = read_rows(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ComparisonSourceError(f"predictions.jsonl 含损坏记录：{exc}") from exc
    predictions = []
    for row in rows:
        try:
            predictions.append(Prediction.model_validate(row))
        except ValueError as exc:
            raise ComparisonSourceError(f"predictions.jsonl 含不符合契约的记录：{exc}") from exc
    return predictions


def _parse_usage(
    run_path: Path, report: dict, *, methods: set[str] | None = None
) -> dict[tuple[str, str], dict[str, Any]]:
    """Per (method, item) request usage from the append-only ledger.

    Anything not recorded stays ``None`` -- the UI shows 未记录, never zero.
    """

    if methods is None:
        methods = set(METHOD_LABELS)
    rows = read_rows((run_path / "requests.jsonl").read_bytes())
    dispatched: dict[int, tuple[str, str]] = {}
    outcomes: dict[int, dict] = {}
    for row in rows:
        event = row.get("event")
        seq = row.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            raise ComparisonSourceError("requests.jsonl 含无效序号。")
        if event == "dispatched":
            if seq in dispatched:
                raise ComparisonSourceError("requests.jsonl 含重复的请求序号。")
            item_id, method = row.get("item_id"), row.get("method")
            if not isinstance(item_id, str) or method not in methods:
                raise ComparisonSourceError("requests.jsonl 含无效的请求身份。")
            dispatched[seq] = (method, item_id)
        elif event == "outcome":
            if seq in outcomes:
                raise ComparisonSourceError("requests.jsonl 含重复的请求结果。")
            outcomes[seq] = row
        else:
            raise ComparisonSourceError("requests.jsonl 含未知事件类型。")
    summary = report.get("requests", {})
    if summary.get("dispatched") is not None and summary["dispatched"] != len(dispatched):
        raise ComparisonSourceError("requests.jsonl 与运行报告的请求计数不一致。")

    usage: dict[tuple[str, str], dict[str, Any]] = {}
    for seq, key in dispatched.items():
        entry = usage.setdefault(key, {"seqs": [], "outcomes": []})
        entry["seqs"].append(seq)
        entry["outcomes"].append(outcomes.get(seq))

    result = {}
    for key, entry in usage.items():
        outcome_rows = entry["outcomes"]
        complete = all(row is not None for row in outcome_rows)
        known_in = [
            r["input_tokens"] for r in outcome_rows if r and r.get("input_tokens") is not None
        ]
        known_out = [
            r["output_tokens"] for r in outcome_rows if r and r.get("output_tokens") is not None
        ]
        result[key] = {
            "recorded": True,
            "requests": len(entry["seqs"]),
            "request_seconds": (
                round(sum(r["request_seconds"] for r in outcome_rows), 3)
                if complete and all(r.get("request_seconds") is not None for r in outcome_rows)
                else None
            ),
            "input_tokens": sum(known_in)
            if complete and len(known_in) == len(outcome_rows)
            else None,
            "output_tokens": (
                sum(known_out) if complete and len(known_out) == len(outcome_rows) else None
            ),
            "token_coverage": f"{min(len(known_in), len(known_out))}/{len(outcome_rows)}",
        }
    return result


def _gold_category(status: str, observed: bool | None, expected: bool | None) -> str:
    """One method's outcome on one dimension; the positive class is an error."""

    if status == "missing":
        return "missing"
    if status != "ok":
        return "failed"
    if observed is None:
        return "abstained"
    if expected is None:
        return "gold_unknown"
    if expected is False:
        return "tp" if observed is False else "fn"
    return "fp" if observed is False else "tn"


def _references(assessment) -> list[dict[str, Any]]:
    """Clickable citations.  Binding was already verified against the frozen
    input; ``verified`` tells the UI a missing anchor is a UI bug, not a guess."""

    refs = []
    location = assessment.first_faulty_location
    if location is not None:
        refs.append(
            {
                "kind": "quote",
                "label": f"首错位置 · {location.source_field}",
                "quote": location.quote,
                "target": {
                    "type": {
                        "implementation_steps": "step",
                        "edge_cases_considered": "edge_case",
                        "code": "code",
                    }.get(location.source_field, "field"),
                    "field": location.source_field,
                    "step_id": location.step_id,
                    "entry_index": location.entry_index,
                    "code_span": location.code_span,
                },
                "verified": True,
            }
        )
    if assessment.code_span is not None:
        refs.append(
            {
                "kind": "code_span",
                "label": f"代码范围 {assessment.code_span}",
                "quote": None,
                "target": {"type": "code", "code_span": assessment.code_span},
                "verified": True,
            }
        )
    for step_id in [assessment.first_faulty_step, *assessment.affected_steps]:
        if step_id is not None:
            refs.append(
                {
                    "kind": "step",
                    "label": f"步骤 {step_id}",
                    "quote": None,
                    "target": {"type": "step", "step_id": step_id},
                    "verified": True,
                }
            )
    if assessment.violated_requirement is not None:
        refs.append(
            {
                "kind": "requirement",
                "label": f"需求 {assessment.violated_requirement}",
                "quote": None,
                "target": {
                    "type": "requirement",
                    "requirement_id": assessment.violated_requirement,
                },
                "verified": True,
            }
        )
    # Deduplicate identical targets (e.g. first step also in affected_steps).
    seen, unique = set(), []
    for ref in refs:
        key = (ref["kind"], repr(sorted(ref["target"].items())))
        if key not in seen:
            seen.add(key)
            unique.append(ref)
    return unique


def _assessment_view(assessment) -> dict[str, Any]:
    return {
        "reasoning_correct": assessment.reasoning_correct,
        "plan_code_aligned": assessment.plan_code_aligned,
        "public_process_correct": public_process_correct(
            assessment.reasoning_correct, assessment.plan_code_aligned
        ),
        "functional_correct": assessment.functional_correct,
        "joint_process_correct": assessment.process_correct,
        "first_faulty_layer": assessment.first_faulty_layer,
        "first_faulty_step": assessment.first_faulty_step,
        "first_faulty_location": (
            assessment.first_faulty_location.model_dump(mode="json")
            if assessment.first_faulty_location is not None
            else None
        ),
        "error_type": assessment.error_type,
        "secondary_error_types": list(assessment.secondary_error_types),
        "violated_requirement": assessment.violated_requirement,
        "code_span": assessment.code_span,
        "affected_steps": list(assessment.affected_steps),
        "explanation": assessment.explanation,
        "confidence": assessment.confidence,
        "references": _references(assessment),
    }


def _label_view(label) -> dict[str, Any]:
    return {
        "reasoning_correct": label.reasoning_correct,
        "plan_code_aligned": label.plan_code_aligned,
        "process_correct": label.process_correct,
        "localization_status": label.localization_status,
        "first_faulty_layer": label.first_faulty_layer,
        "first_faulty_step": label.first_faulty_step,
        "first_faulty_location": (
            label.first_faulty_location.model_dump(mode="json")
            if label.first_faulty_location is not None
            else None
        ),
        "error_type": label.error_type,
        "evidence": [entry.model_dump(mode="json") for entry in label.evidence],
        "rationale": label.rationale,
    }


def build_comparison_view(
    pilot: PreparedPilot,
    run_path: Path,
    *,
    run_dir_display: str,
    report: dict,
    config: dict,
) -> dict[str, Any]:
    """Assemble the page payload from an already verified run directory."""

    if config.get("methods") != list(METHODS):
        raise ComparisonSourceError("运行的方法集合与对比页定义不一致。")
    predictions = _parse_predictions(run_path)
    # score_predictions applies the scorer's own strictness: duplicate or
    # unexpected identities, per-item input hashes and reference binding.
    try:
        scores = score_predictions(pilot, predictions)
    except ValueError as exc:
        raise ComparisonSourceError(f"预测记录未通过计分校验：{exc}") from exc
    usage = _parse_usage(run_path, report)

    indexed: dict[tuple[str, str], Prediction] = {
        (row.method, row.item_id): row for row in predictions
    }

    items = []
    for item_id in sorted(pilot.inputs):
        item = pilot.inputs[item_id]
        label = pilot.labels[item_id]
        trace = item.solution_trace
        methods_view = {}
        for method in METHODS:
            row = indexed.get((method, item_id))
            status = row.status if row is not None else "missing"
            assessment = row.assessment if row is not None else None
            observed = (
                {
                    "reasoning_correct": assessment.reasoning_correct,
                    "plan_code_aligned": assessment.plan_code_aligned,
                    "public_process_correct": public_process_correct(
                        assessment.reasoning_correct, assessment.plan_code_aligned
                    ),
                }
                if assessment is not None
                else None
            )
            expected = {
                "reasoning_correct": label.reasoning_correct,
                "plan_code_aligned": label.plan_code_aligned,
                "public_process_correct": label.process_correct,
            }
            methods_view[method] = {
                "status": status,
                "assessment": _assessment_view(assessment) if assessment is not None else None,
                "gold_comparison": {
                    dim: _gold_category(status, observed[dim] if observed else None, expected[dim])
                    for dim in GOLD_DIMENSIONS
                },
                "outcome": _gold_category(
                    status,
                    observed["public_process_correct"] if observed else None,
                    expected["public_process_correct"],
                ),
                "usage": usage.get((method, item_id), {"recorded": False}),
            }
        decided = {
            method: view["assessment"]
            for method, view in methods_view.items()
            if view["status"] == "ok"
        }
        divergence = [
            field
            for field in DIVERGENCE_FIELDS
            if len({view[field] for view in decided.values()}) > 1
        ]
        items.append(
            {
                "item_id": item_id,
                "input_sha256": pilot.input_hash(item_id),
                "problem": item.problem.model_dump(mode="json"),
                "solution_trace": trace.model_dump(mode="json"),
                "functional_evidence": {
                    "scope": item.functional_evidence.scope,
                    "infrastructure_status": item.functional_evidence.infrastructure_status,
                    "base_status": item.functional_evidence.base_status,
                    "plus_status": item.functional_evidence.plus_status,
                    "functional_correct": item.functional_evidence.functional_correct,
                    "candidate_code_sha256": item.functional_evidence.candidate_code_sha256,
                    "source_run_id": item.functional_evidence.source_run_id,
                },
                "methods": methods_view,
                "disagreement": {"any": bool(divergence), "fields": divergence},
                "human_reference": _label_view(label),
            }
        )

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

    requests_summary = report.get("requests", {})
    run_status = report.get("status")
    notes = [
        f"本页为{IDENTITY_LABEL}：12 条开发试点（{label_counts.get('correct', 0)} 正确、{label_counts.get('incorrect', 0)} 错误、{label_counts.get('unknown', 0)} unknown），历史预测、历史计分与三列判断保持原样。",
        "标签为反馈后的开发共识，不是独立留出测试集金标；标注者独立性未核实。",
        "功能证据是 MBPP base/plus 官方汇总状态，不是逐用例执行结果；功能失败不等同于过程错误。",
        "公开过程口径为 reasoning_correct AND plan_code_aligned 的三值逻辑；联合 process_correct 仅 Full System 含功能证据，跨方法比较须注意该证据差。",
        "引用出现在原文只证明绑定成功，不证明错误判断成立。",
        "首错步骤与结构化位置金标均为 0：页面展示的位置不能被解释为定位准确率，null/null 不算命中。",
        "2026-09-10 的 36 条开发验证使用 baseline 与 assumption audit 两方法，不是本页三种历史方法；实验方法、标签阶段与口径不同，不能直接合并排名。",
        "页面只读取已落盘并哈希验证的记录；浏览与切换样本不会发起模型请求、不运行 Solver、不执行候选代码。",
        "金额未由 Provider 返回，成本保持未知。",
    ]
    if run_status != "completed":
        notes.insert(0, f"该运行的状态为 {run_status}，以下仅为已完整落盘并验证的记录。")

    return {
        "ok": True,
        "schema": COMPARISON_SCHEMA,
        "data_kind": "real_experiment",
        "experiment": {
            "name": run_path.name,
            "identity_label": IDENTITY_LABEL,
            "run_dir": run_dir_display,
            "run_status": run_status,
            "finished_utc": report.get("finished_utc"),
            "item_count": len(items),
            "methods": [{"id": method, "label": METHOD_LABELS[method]} for method in METHODS],
            "pilot_config": PILOT_CONFIG.as_posix(),
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
            "model": config.get("model"),
            "budget": report.get("budget"),
            "requests": {
                "dispatched": requests_summary.get("dispatched"),
                "request_seconds": requests_summary.get("total_request_seconds"),
                "input_tokens": requests_summary.get("input_tokens"),
                "output_tokens": requests_summary.get("output_tokens"),
                "token_usage": requests_summary.get("token_usage"),
            },
        },
        "scores": {
            method: {
                "coverage": scores["methods"][method]["coverage"],
                "public_process": scores["methods"][method]["dimensions"]["public_process_correct"],
            }
            for method in METHODS
        },
        "items": items,
        "notes": notes,
    }


def load_method_comparison(
    repo_root: str | Path,
    run_dir: Path = PILOT_RUN_DIR,
    config_path: Path = PILOT_CONFIG,
) -> dict[str, Any]:
    """Load the pinned run read-only; raises ComparisonSourceError if unverifiable."""

    root = Path(repo_root).resolve()
    run_path = _contained_run_dir(root, run_dir)
    report = _read_json_file(run_path / "run-report.json", "运行报告 run-report.json")
    _verify_report_hashes(run_path, report)
    config = _read_json_file(run_path / "run-config.json", "运行身份 run-config.json")
    try:
        pilot = prepare_pilot(root, config_path)
    except (ValueError, OSError, KeyError) as exc:
        raise ComparisonSourceError(f"绑定材料或人工参考校验失败：{exc}") from exc
    config_raw = (
        digest((root / config_path).read_bytes())
        if not config_path.is_absolute()
        else digest(config_path.read_bytes())
    )
    _check_run_identity(config, pilot, config_raw)
    return build_comparison_view(
        pilot,
        run_path,
        run_dir_display=run_dir.as_posix(),
        report=report,
        config=config,
    )


# ------------------------------------------------------- synthetic UI fixture


def synthetic_method_comparison() -> dict[str, Any]:
    """Clearly marked synthetic payload for exercising page states only.

    This data is NOT an experiment result: it exists so the page's success,
    divergence, unknown, failure, missing and long-text states can be checked
    without touching real runs.  It is served only via an explicit allowlisted
    query and is always labelled 界面测试数据 in the UI.
    """

    long_code = "\n".join(
        f"def step_{index:02d}(value):  # synthetic filler line {index}\n    return value"
        for index in range(1, 41)
    )
    long_text = "这是一段用于验证长文本折叠与滚动行为的合成说明。" * 12

    def assessment(reasoning, aligned, **extra):
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
            "explanation": "合成解释文本，仅用于界面状态验证。包含不可信文本探针：<b>不应渲染为HTML</b><img src=x onerror=window.__tj_xss=1>。",
            "confidence": None,
            "references": [],
        }
        view.update(extra)
        return view

    def usage(requests=1, seconds=12.5, tokens=(900, 700)):
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
            "infrastructure_status": "ok",
            "base_status": "pass" if correct else "fail",
            "plus_status": "pass" if correct else "fail",
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

    def method(status, assessment_view=None, usage_view=None, gold=None, outcome="tn"):
        return {
            "status": status,
            "assessment": assessment_view,
            "gold_comparison": gold or {dim: "gold_unknown" for dim in GOLD_DIMENSIONS},
            "outcome": outcome,
            "usage": usage_view if usage_view is not None else usage(),
        }

    def item(item_id, prob, tr, func, methods_view, divergence, human_view):
        return {
            "item_id": item_id,
            "input_sha256": "1" * 64,
            "problem": prob,
            "solution_trace": tr,
            "functional_evidence": func,
            "methods": methods_view,
            "disagreement": divergence,
            "human_reference": human_view,
        }

    gold_tp = {"reasoning_correct": "tp", "plan_code_aligned": "tn", "public_process_correct": "tp"}
    gold_tn = {"reasoning_correct": "tn", "plan_code_aligned": "tn", "public_process_correct": "tn"}
    gold_fn = {"reasoning_correct": "fn", "plan_code_aligned": "tn", "public_process_correct": "fn"}

    located = assessment(
        False,
        True,
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

    items = [
        item(
            "ui-fixture-agree",
            problem("合成样本 · 三方法一致"),
            trace(),
            functional(True),
            {
                "direct_judge": method("ok", assessment(True, True), gold=gold_tn),
                "structured_judge": method("ok", assessment(True, True), gold=gold_tn),
                "full_system": method(
                    "ok", {**assessment(True, True), "functional_correct": True}, gold=gold_tn
                ),
            },
            {"any": False, "fields": []},
            human(True, True),
        ),
        item(
            "ui-fixture-diverge",
            problem("合成样本 · 方法分歧与误报/漏报"),
            trace(),
            functional(False),
            {
                "direct_judge": method("ok", assessment(True, True), gold=gold_fn, outcome="fn"),
                "structured_judge": method("ok", located, gold=gold_tp, outcome="tp"),
                "full_system": method(
                    "ok",
                    {
                        **assessment(False, True),
                        "functional_correct": False,
                        "first_faulty_layer": "reasoning",
                        "first_faulty_step": "S1",
                        "error_type": "P01_ALGORITHM_ERROR",
                    },
                    gold=gold_tp,
                    outcome="tp",
                ),
            },
            {
                "any": True,
                "fields": [
                    "reasoning_correct",
                    "public_process_correct",
                    "first_faulty_layer",
                    "first_faulty_step",
                    "error_type",
                ],
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
        ),
        item(
            "ui-fixture-unknown-failed",
            problem("合成样本 · 参考未知与调用失败"),
            trace(),
            functional(None),
            {
                "direct_judge": method(
                    "ok",
                    assessment(None, None),
                    gold={dim: "abstained" for dim in GOLD_DIMENSIONS},
                    outcome="abstained",
                ),
                "structured_judge": method(
                    "provider_error", None, {"recorded": False}, outcome="failed"
                ),
                "full_system": method(
                    "ok",
                    {**assessment(True, True), "functional_correct": None},
                    outcome="gold_unknown",
                ),
            },
            {"any": False, "fields": []},
            human(None, None, localization_status="unknown"),
        ),
        item(
            "ui-fixture-missing-long",
            problem("合成样本 · 结果缺失与长文本", requirement=long_text),
            trace(code=long_code),
            functional(True),
            {
                "direct_judge": method("ok", assessment(True, True), gold=gold_tn),
                "structured_judge": method("missing", None, {"recorded": False}, outcome="missing"),
                "full_system": method(
                    "parse_error",
                    None,
                    usage(requests=2, seconds=40.1, tokens=None),
                    outcome="failed",
                ),
            },
            {"any": False, "fields": []},
            human(True, True),
        ),
    ]

    return {
        "ok": True,
        "schema": COMPARISON_SCHEMA,
        "data_kind": "synthetic_ui_test",
        "experiment": {
            "name": "界面测试数据（合成，非实验结果）",
            "identity_label": "合成界面数据 · 非实验身份",
            "run_dir": "synthetic://ui-fixture",
            "run_status": "synthetic",
            "finished_utc": None,
            "item_count": len(items),
            "methods": [{"id": m, "label": METHOD_LABELS[m]} for m in METHODS],
            "pilot_config": None,
            "label_stage": "synthetic_ui_test",
            "annotation_provenance": "synthetic_ui_test",
            "label_summary": {"correct": 2, "incorrect": 1, "unknown": 1},
            "location_gold": {
                "first_step_supported": 0,
                "structured_location": 0,
                "has_reference": False,
            },
            "model": None,
            "budget": None,
            "requests": {
                "dispatched": None,
                "request_seconds": None,
                "input_tokens": None,
                "output_tokens": None,
                "token_usage": "missing",
            },
        },
        "scores": None,
        "items": items,
        "notes": [
            "这是界面测试数据：用于验证成功、分歧、未知、失败、缺失与长文本等展示状态，不是实验结果，不得计入任何成绩。",
        ],
    }
