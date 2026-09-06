#!/usr/bin/env python3
"""Recover only HumanEval/15 and derive a fully traced 164-task report.

Never mutates the original run or historical diagnostic inputs. No model API.
Historical 103/104 raw hashes are first bound at import, explicitly disclosed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import asdict
from pathlib import Path

import diagnose_humanevalplus_pending as diagnostic

from tracejudge_hy3.evalplus import runner as phase2
from tracejudge_hy3.evalplus.docker_runner import DockerLimits
from tracejudge_hy3.evalplus.exporter import load_validated_phase1_export
from tracejudge_hy3.evalplus.parser import build_summary, parse_official_result

ROOT = diagnostic.ROOT
IDS = [f"HumanEval/{i}" for i in range(164)]
SUPPLEMENTS = {"HumanEval/15", "HumanEval/103", "HumanEval/104"}
IMPORTED = {"HumanEval/103", "HumanEval/104"}
BASE_FILES = (
    "manifest.json",
    "samples.jsonl",
    "results.jsonl",
    "summary.json",
    "execution.log",
    "evalplus_raw_results.json",
)
SAFE_COMPARISON = ("passed_base", "passed_plus", "base_status", "plus_status", "error_type")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    require(path.is_file() and not path.is_symlink(), "missing or symlink input")
    return phase2._read_json(path, label="recovery artifact")


def hash_files(paths):
    result = {}
    for path in paths:
        require(path.is_file() and not path.is_symlink(), "unsafe input path")
        result[str(path.resolve())] = diagnostic.sha(path)
    return result


def document_hash(value):
    return hashlib.sha256(phase2._json_bytes(value)).hexdigest()


def runtime_pins(preflight):
    runtime = dict(preflight["runtime"]["runtime"])
    runtime.pop("verified_task_count", None)
    return runtime


def validate_observation(item):
    require(item.get("infrastructure_error_type") is None, "not a successful execution")
    observations = item.get("container_observations", [])
    require(len(observations) == 1, "ambiguous container evidence")
    observation = observations[0]
    state = observation.get("before_cleanup", {})
    require(
        observation.get("problem_id") == item["problem_id"]
        and observation.get("diagnostic_kill_requested") is False
        and observation.get("cleanup_status") == "removed"
        and state.get("available") is True
        and state.get("Running") is False
        and state.get("ExitCode") == 0
        and state.get("OOMKilled") is False
        and observation.get("control", {}).get("status") == "ok",
        "container evidence is not a clean natural exit",
    )


def validate_imported(item, raw, code_hash):
    validate_observation(item)
    require(item.get("code_sha256") == code_hash, "diagnostic candidate differs")
    parsed = parse_official_result(
        raw, expected_problem_id=item["problem_id"], expected_solution_sha256=code_hash
    )
    require(
        {k: parsed[k] for k in SAFE_COMPARISON} == item.get("official_result"),
        "diagnostic raw/status mismatch",
    )
    return parsed


def load_sources(args):
    source, diag = args.source_run.resolve(), args.diagnostic_run.resolve()
    original = read(source / "manifest.json")
    paths = phase2._run_paths(source.parent, source.name)
    phase2._validate_completed_output_integrity(original, paths, expected_result_count=164)
    exported = load_validated_phase1_export(
        args.baseline_run, args.dataset_manifest, min_success_count=30
    )
    require(asdict(exported.phase1) == original["phase1_source"], "phase-one identity differs")
    require(
        document_hash(asdict(exported.dataset)) == document_hash(original["dataset"]),
        "dataset identity differs",
    )
    require([s.task_id for s in exported.samples] == IDS, "not the full numeric 164-task cohort")
    require(
        diagnostic.sha(source / "samples.jsonl") == exported.samples_sha256,
        "sample export differs",
    )
    old = phase2._validated_existing_results(paths, exported, run_id=source.name)
    original_ids = set(old)
    old_raw = phase2._validated_existing_raw(paths, old, exported)
    require(set(old) == original_ids == set(IDS), "source coverage differs or raw missing")
    require(
        {k for k, v in old.items() if v["infrastructure_status"] == "error"} == SUPPLEMENTS,
        "unexpected pending set",
    )
    require(len(old_raw) == 161, "expected 161 authoritative original evaluations")
    report = read(diag / "report.json")
    require(
        report.get("schema") == "humanevalplus-container-diagnosis-v1", "unknown diagnostic schema"
    )
    require(
        report.get("completed_at") and report.get("source_unchanged") is True,
        "incomplete diagnosis",
    )
    require(report.get("source_run") == str(source), "diagnosis source path differs")
    require(report.get("source_git") == original["git"], "diagnosis source Git differs")
    require(
        report.get("implementation_sha256") == original["git"]["implementation_sha256"],
        "diagnosis implementation differs",
    )
    require(
        report.get("diagnostic_script_sha256") == diagnostic.sha(Path(diagnostic.__file__)),
        "diagnosis code is not available for verification",
    )
    require(report.get("preflight", {}).get("ready") is True, "diagnostic preflight failed")
    require(
        "diagnostic_instrumentation" not in report["executor"],
        "instrumented diagnostic is not reusable",
    )
    require(
        phase2._static_executor_identity(report["executor"])
        == phase2._static_executor_identity(original["executor"]),
        "diagnostic executor config differs",
    )
    require(
        report["source_hashes"] == {name: diagnostic.sha(source / name) for name in BASE_FILES},
        "source changed since diagnosis",
    )
    entries = report["results"]
    require(
        len(entries) == 3 and {v["problem_id"] for v in entries} == SUPPLEMENTS,
        "diagnostic coverage differs",
    )
    imported = {}
    imported_raw = {}
    input_paths = [source / name for name in BASE_FILES] + [diag / "report.json"]
    for item in entries:
        task = item["problem_id"]
        if task not in IMPORTED:
            continue
        task_dir = diag / task.replace("/", "_")
        raw_path = task_dir / "official_evalplus_raw_result.json"
        sample_path = task_dir / "sample.jsonl"
        sample = phase2._read_jsonl(sample_path, label="diagnostic sample")
        require(
            len(sample) == 1 and sample[0].get("task_id") == task, "diagnostic sample task differs"
        )
        code_hash = exported.reference_for(task).code_sha256
        require(
            hashlib.sha256(sample[0]["solution"].encode()).hexdigest() == code_hash,
            "diagnostic sample code differs",
        )
        raw = read(raw_path)
        parsed = validate_imported(item, raw, code_hash)
        imported[task] = {"safe": parsed, "evidence": item, "raw_path": str(raw_path)}
        imported_raw[task] = raw
        input_paths += [raw_path, sample_path]
    phase1 = args.baseline_run.resolve()
    input_paths += [phase1 / name for name in ("manifest.json", "responses.jsonl", "summary.json")]
    input_paths.append(args.dataset_manifest.resolve())
    return exported, original, old, old_raw, report, imported, imported_raw, hash_files(input_paths)


def enrich(parsed, evidence, exported, run_id):
    duration = evidence["total_elapsed_seconds"]
    require(
        type(duration) in (int, float) and math.isfinite(duration) and duration >= 0,
        "invalid elapsed time",
    )
    return {
        "schema_version": 1,
        "run_id": run_id,
        **parsed,
        "duration_seconds": duration,
        "started_at": evidence["started_at"],
        "ended_at": evidence["ended_at"],
        "failure_count_scope": "recorded_by_evalplus_test_details",
        "source_response": phase2._source_reference(exported, parsed["problem_id"]),
    }


def merge_records(old, replacements):
    require(set(old) == set(IDS), "original coverage violation")
    require(set(replacements) == SUPPLEMENTS, "replacement coverage violation")
    for task in SUPPLEMENTS:
        require(old[task]["infrastructure_status"] == "error", "cannot replace an executed result")
        require(
            replacements[task]["problem_id"] == task
            and replacements[task]["infrastructure_status"] == "ok",
            "invalid replacement",
        )
    merged = [replacements.get(task, old[task]) for task in IDS]
    require(
        len(merged) == 164 and all(v["infrastructure_status"] == "ok" for v in merged),
        "incomplete derived evaluation",
    )
    return merged


def snapshot_code(output):
    root = ROOT / "src/tracejudge_hy3"
    paths = list((root / "evalplus").glob("*.py")) + [
        root / "cli.py",
        root / "dataset/humanevalplus.py",
        root / "dataset/loader.py",
        root / "redaction.py",
        root / "resources.py",
        Path(__file__),
        Path(diagnostic.__file__),
    ]
    hashes = {}
    for path in paths:
        relative = path.relative_to(ROOT)
        target = output / "source_snapshot" / relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        phase2._atomic_write_bytes(target, path.read_bytes())
        hashes[str(relative)] = diagnostic.sha(target)
    return hashes


def execute_task15(output, exported, executor):
    diagnostic.require_idle()
    workspace = output / "HumanEval_15"
    workspace.mkdir(mode=0o700)
    sample = next(s for s in exported.samples if s.task_id == "HumanEval/15")
    metadata = next(m for m in exported.task_metadata if m.problem_id == sample.task_id)
    executor.problem_id, executor.output_dir = sample.task_id, output
    print("[run] HumanEval/15 only; existing candidate; gzip private cache; 180s", flush=True)
    outcome = executor.run_task(sample=sample, task_metadata=metadata, workspace=workspace)
    item = {
        "problem_id": sample.task_id,
        "code_sha256": exported.reference_for(sample.task_id).code_sha256,
        "started_at": outcome.started_at,
        "ended_at": outcome.ended_at,
        "total_elapsed_seconds": outcome.duration_seconds,
        "infrastructure_error_type": outcome.infrastructure_error_type,
        "container_observations": executor.observations,
        "docker_calls": executor.calls,
    }
    raw_path = workspace / "official_evalplus_raw_result.json"
    receipt = {
        "schema": "humanevalplus-task15-recovery-v1",
        "evidence": item,
        "raw_sha256": diagnostic.sha(raw_path) if raw_path.is_file() else None,
        "executor": executor.public_identity(),
        "completed_at": diagnostic.now(),
    }
    diagnostic.checkpoint(output / "task15_receipt.json", receipt)
    validate_observation(item)
    return receipt


def report_markdown(summary, run_id):
    n = summary["actual_execution_count"]
    return f"""# HumanEval+ 164 题可追溯补充执行报告

本报告复用原 161 条有效执行、已完成的 103/104 两题诊断执行，并补跑第 15 题。
未重新生成代码，未调用模型 API，原实验与诊断文件均保持不变。

| 指标 | 结果 |
| --- | --- |
| 有效执行覆盖 | {n}/164 |
| Base 通过 | {summary["base_pass_count"]}/{n}（{summary["base_pass_rate"]:.2%}） |
| Base+Extra 通过 | {summary["base_plus_pass_count"]}/{n}（{summary["base_plus_pass_rate"]:.2%}） |
| 基础设施错误 | {summary["infrastructure_error_count"]} |
| 错误答案/候选异常 | {summary["wrong_answer_or_candidate_exception_count"]} |
| 官方 timeout | {summary["timeout_count"]} |

## 方法与边界

- 固定 164 题，每题复用一份已有候选；固定官方镜像、数据版本、测试、判题参数。
- 第 15 题仅启用临时参考答案 pickle 缓存的流式 gzip 压缩；逻辑内容不变，128 MiB 文件上限、1 GiB tmpfs、4 GiB 内存、输出检查均保留。
- 原 161 条结果（含失败判定）原样保留；103、104 的原始执行结果经题号、候选 hash、判定与容器证据核验后复用，未重复执行。
- 这是跨执行批次汇总的全量单候选结果，不是同一原始 run 的续跑，也不是官方排行榜认证。每题来源见 `provenance.jsonl`。
- 原诊断未在当时记录 103/104 raw 文件 hash；本次导入时首次记录，并以原候选 hash 和当时安全判定交叉核对。不能将此次绑定称为历史防篡改证明。
- 公开基准可能存在训练污染；固定 EvalPlus 的 fail 无法区分错误答案与候选异常。本报告不推断模型间优势或统计显著性。
- 平均耗时跨多个执行批次且包含管理开销，不适合进行严格性能比较。

恢复记录：`{run_id}`。`manifest.json` 绑定原输入、各执行器、源码快照及派生产物 hash；`results.jsonl` 为安全结果，原始结果 bundle 为私有评测数据。
"""


def recover(args):
    exported, original, old, old_raw, diag, imported, imported_raw, inputs = load_sources(args)
    output = args.output_dir.resolve()
    phase2._require_non_trackable_run_directory(output)
    executor = diagnostic.DiagnosticRunner(
        limits=DockerLimits(per_task_timeout_seconds=180), reference_cache_compression="gzip"
    )
    # The declared delta is only cache storage, not test or resource policy.
    expected = dict(executor.public_identity())
    expected.pop("reference_cache")
    require(
        phase2._static_executor_identity(expected)
        == phase2._static_executor_identity(original["executor"]),
        "undeclared executor change",
    )
    if args.assemble_only:
        manifest = read(output / "manifest.json")
        require(manifest.get("status") == "prepared", "assembly requires a prepared recovery")
        require(manifest["input_hashes"] == inputs, "input drift after preparation")
        for relative, digest in manifest["source_snapshot_hashes"].items():
            require(
                diagnostic.sha(output / "source_snapshot" / relative) == digest,
                "source snapshot drift",
            )
        receipt = read(output / "task15_receipt.json")
        require(
            manifest["implementation_sha256"] == phase2._implementation_sha256(),
            "recovery implementation drift",
        )
    else:
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
        manifest = {
            "schema": "humanevalplus-derived-recovery-v1",
            "status": "prepared",
            "run_id": output.name,
            "started_at": diagnostic.now(),
            "model_api_calls": 0,
            "format": "derived_cross_run_evaluation_not_standard_resume",
            "original_run": str(args.source_run.resolve()),
            "diagnostic_run": str(args.diagnostic_run.resolve()),
            "input_hashes": inputs,
            "phase1_source": original["phase1_source"],
            "dataset": original["dataset"],
            "expected_problem_ids": IDS,
            "preserved_count": 161,
            "imported_ids": sorted(IMPORTED),
            "newly_executed_ids": ["HumanEval/15"],
            "original_git": original["git"],
            "recovery_git": phase2._git_metadata(),
            "implementation_sha256": phase2._implementation_sha256(),
            "source_snapshot_hashes": snapshot_code(output),
            "executor": executor.public_identity(),
            "diagnostic_executor": diag["executor"],
            "original_executor": original["executor"],
            "historical_diagnostic_raw_hash_first_bound_at_recovery": True,
            "selection_rule": "preserve_all_original_valid_results_replace_only_15_103_104_infrastructure_errors",
            "task_parallelism": 1,
            "per_task_outer_seconds": 180,
        }
        diagnostic.checkpoint(output / "manifest.json", manifest)
        diagnostic.require_idle()
        preflight_dir = output / "preflight"
        preflight_dir.mkdir(mode=0o700)
        metadata = [m for m in exported.task_metadata if m.problem_id == "HumanEval/15"]
        preflight = asdict(executor.preflight(task_metadata=metadata, workspace=preflight_dir))
        manifest["preflight"] = preflight
        diagnostic.checkpoint(output / "manifest.json", manifest)
        require(preflight["ready"], "recovery preflight failed")
        require(
            runtime_pins(preflight) == runtime_pins(diag["preflight"]), "runtime/data pins drifted"
        )
        receipt = execute_task15(output, exported, executor)
    item = receipt["evidence"]
    validate_observation(item)
    require(item["problem_id"] == "HumanEval/15", "unexpected newly executed task")
    require(
        phase2._static_executor_identity(receipt["executor"])
        == phase2._static_executor_identity(manifest["executor"]),
        "receipt executor differs",
    )
    raw_path = output / "HumanEval_15/official_evalplus_raw_result.json"
    require(receipt["raw_sha256"] == diagnostic.sha(raw_path), "task15 raw hash mismatch")
    raw15 = read(raw_path)
    safe15 = parse_official_result(
        raw15,
        expected_problem_id="HumanEval/15",
        expected_solution_sha256=exported.reference_for("HumanEval/15").code_sha256,
    )
    replacements = {
        task: enrich(v["safe"], v["evidence"], exported, output.name)
        for task, v in imported.items()
    }
    replacements["HumanEval/15"] = enrich(safe15, item, exported, output.name)
    merged = merge_records(old, replacements)
    summary = build_summary(merged, expected_problem_ids=IDS)
    summary.update(
        {
            "metrics_scope": "full_humanevalplus_164_single_candidate_derived_recovery",
            "run_id": output.name,
            "preserved_result_count": 161,
            "imported_diagnostic_count": 2,
            "new_execution_count": 1,
            "model_api_calls": 0,
            "limitations": [
                "cross_run_derived_not_original_resume",
                "single_candidate_not_official_ranking",
                "cache_storage_compressed_only_for_task15",
                "historical_diagnostic_raw_hash_bound_at_import",
                "fail_combines_wrong_answer_and_candidate_exception",
                "public_benchmark_contamination_possible",
            ],
        }
    )
    provenance = []
    raw_all = {**old_raw, **imported_raw, "HumanEval/15": raw15}
    require(set(raw_all) == set(IDS), "raw coverage mismatch")
    for record in merged:
        task = record["problem_id"]
        parsed = parse_official_result(
            raw_all[task],
            expected_problem_id=task,
            expected_solution_sha256=exported.reference_for(task).code_sha256,
        )
        require(all(record[k] == value for k, value in parsed.items()), "merged raw/safe mismatch")
        source_kind = (
            "original_valid"
            if task not in SUPPLEMENTS
            else "diagnostic_import"
            if task in IMPORTED
            else "cache_recovery_execution"
        )
        provenance.append(
            {
                "problem_id": task,
                "source_kind": source_kind,
                "record_run_id": record["run_id"],
                "candidate_sha256": record["solution_sha256"],
                "raw_document_sha256": document_hash(raw_all[task]),
                "safe_record_sha256": document_hash(record),
                "source_artifact": str(args.source_run.resolve() / "evalplus_raw_results.json")
                if source_kind == "original_valid"
                else imported[task]["raw_path"]
                if task in IMPORTED
                else str(raw_path),
            }
        )
    require(hash_files(Path(p) for p in inputs) == inputs, "inputs changed during recovery")
    phase2._atomic_write_bytes(output / "results.jsonl", phase2._jsonl_bytes(merged))
    diagnostic.checkpoint(output / "evalplus_raw_results.json", phase2._raw_bundle(raw_all, IDS))
    phase2._atomic_write_bytes(output / "provenance.jsonl", phase2._jsonl_bytes(provenance))
    diagnostic.checkpoint(output / "summary.json", summary)
    phase2._atomic_write_bytes(output / "report.md", report_markdown(summary, output.name).encode())
    names = (
        "results.jsonl",
        "evalplus_raw_results.json",
        "provenance.jsonl",
        "summary.json",
        "report.md",
        "task15_receipt.json",
    )
    manifest.update(
        {
            "status": "completed",
            "completed_at": diagnostic.now(),
            "source_unchanged": True,
            "outputs": {name: diagnostic.sha(output / name) for name in names},
        }
    )
    diagnostic.checkpoint(output / "manifest.json", manifest)
    print(
        json.dumps(
            {
                k: summary[k]
                for k in (
                    "actual_execution_count",
                    "base_pass_count",
                    "base_plus_pass_count",
                    "infrastructure_error_count",
                    "evaluation_complete",
                )
            }
        ),
        flush=True,
    )
    print(f"[done] {output / 'report.md'}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("source-run", "diagnostic-run", "baseline-run", "dataset-manifest", "output-dir"):
        parser.add_argument(f"--{flag}", type=Path, required=True)
    parser.add_argument(
        "--assemble-only", action="store_true", help="reuse a completed task15 receipt; no Docker"
    )
    args = parser.parse_args()
    try:
        recover(args)
    except Exception as exc:
        print(
            f"[blocked] recovery failed ({type(exc).__name__}); no raw exception echoed", flush=True
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
