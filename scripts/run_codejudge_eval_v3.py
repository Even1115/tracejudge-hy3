#!/usr/bin/env python3
"""Run the formal CodeJudge-Eval v3-A or v3-B experiment against real Hy3.

This is the only real-provider entry point for the v3 experiments.  It is
fail-closed: the corrected prompt-v4 selection, protocol, source files and
dependency locks must all be tracked and clean at HEAD; the user must pass an
explicit real-provider confirmation; and a completed run requires exact plan
coverage before scoring.

Examples (preflight never calls the provider):

    python scripts/run_codejudge_eval_v3.py --experiment a --preflight
    python scripts/run_codejudge_eval_v3.py --experiment a --confirm-real-provider
    python scripts/run_codejudge_eval_v3.py --experiment b --confirm-real-provider
    python scripts/run_codejudge_eval_v3.py --experiment b --run-id RUN --resume \
        --confirm-real-provider
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tracejudge_hy3.benchmark.codejudge_eval import (  # noqa: E402
    CodeJudgeEvalAdapter,
    CodeJudgeEvalError,
    CodeJudgeEvalSample,
    CodeJudgeEvalSelection,
    file_sha256,
)
from tracejudge_hy3.benchmark.codejudge_v3 import (  # noqa: E402
    CodeJudgeV3Selection,
    select_v3_experiment_a,
    select_v3_experiment_b,
    used_raw_task_ids,
)
from tracejudge_hy3.benchmark.codejudge_v3_execution import (  # noqa: E402
    CodeJudgeV3Record,
    PlannedV3Call,
    build_v3_call_plan,
    call_plan_sha256,
    judge_v3_call,
    validate_resume_prefix,
)
from tracejudge_hy3.benchmark.codejudge_v3_manifest import (  # noqa: E402
    V3RunManifest,
    build_v3_completion_receipt,
    build_v3_run_manifest,
    current_git_state,
    experiment_scope_status,
    formal_scope_files,
)
from tracejudge_hy3.benchmark.codejudge_v3_metrics import (  # noqa: E402
    score_v3_a,
    score_v3_b,
)
from tracejudge_hy3.benchmark.contracts import BenchmarkDataset  # noqa: E402
from tracejudge_hy3.benchmark.hy3_judge_only import Hy3JudgeOnlyProvider  # noqa: E402
from tracejudge_hy3.benchmark.judge_only_prompt_v4 import (  # noqa: E402
    judge_only_prompt_v4_bundle_sha256,
)
from tracejudge_hy3.config import get_settings  # noqa: E402

DATA_DIR = REPO_ROOT / "data" / "codejudge_eval"
RUNS_DIR = REPO_ROOT / "artifacts" / "codejudge_eval_v3"
SELECTION_PATHS = {
    "a": DATA_DIR / "selection_v3a_v4.json",
    "b": DATA_DIR / "selection_v3b_v4.json",
}


def _json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _atomic_write_once_or_equal(path: Path, payload: object) -> None:
    if path.is_symlink():
        raise CodeJudgeEvalError(f"refusing symlinked artifact: {path}")
    content = _json_bytes(payload)
    if path.exists():
        if path.read_bytes() != content:
            raise CodeJudgeEvalError(f"existing artifact differs: {path}")
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _append_jsonl(path: Path, payload: object) -> None:
    if path.is_symlink():
        raise CodeJudgeEvalError(f"refusing symlinked JSONL artifact: {path}")
    line = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def _ensure_private_file(path: Path) -> None:
    if path.is_symlink():
        raise CodeJudgeEvalError(f"refusing symlinked private artifact: {path}")
    if not path.exists():
        with path.open("xb"):
            pass
    os.chmod(path, 0o600)


def _read_records(path: Path) -> tuple[CodeJudgeV3Record, ...]:
    if not path.is_file():
        return ()
    records: list[CodeJudgeV3Record] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            raise CodeJudgeEvalError(f"blank records.jsonl line {line_number}")
        try:
            records.append(CodeJudgeV3Record.model_validate_json(line))
        except Exception as exc:
            raise CodeJudgeEvalError(
                f"invalid records.jsonl line {line_number}: {type(exc).__name__}"
            ) from None
    return tuple(records)


def _count_jsonl(path: Path) -> int:
    if not path.is_file():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _derive_active_selections(
    samples: tuple[CodeJudgeEvalSample, ...], descriptor: BenchmarkDataset
) -> tuple[CodeJudgeV3Selection, CodeJudgeV3Selection]:
    frozen_v2 = CodeJudgeEvalSelection.model_validate_json(
        (DATA_DIR / "selection_v2.json").read_text(encoding="utf-8")
    )
    used_v1 = used_raw_task_ids(frozen_v2)
    prompt_hash = judge_only_prompt_v4_bundle_sha256()
    selection_a = select_v3_experiment_a(
        samples,
        descriptor=descriptor,
        exclude_raw_task_ids=used_v1,
        prompt_bundle_sha256=prompt_hash,
    )
    excluded_b = used_v1 | {entry.raw_task_id for entry in selection_a.entries}
    selection_b = select_v3_experiment_b(
        samples,
        descriptor=descriptor,
        exclude_raw_task_ids=frozenset(excluded_b),
        prompt_bundle_sha256=prompt_hash,
    )
    return selection_a, selection_b


def _load_plan(
    experiment: Literal["a", "b"],
) -> tuple[
    CodeJudgeEvalAdapter,
    CodeJudgeV3Selection,
    tuple[tuple[PlannedV3Call, CodeJudgeEvalSample], ...],
]:
    adapter = CodeJudgeEvalAdapter()
    samples = adapter.load_samples(DATA_DIR, exclude_ambiguous_interface=True)
    derived_a, derived_b = _derive_active_selections(samples, adapter.descriptor)
    derived = derived_a if experiment == "a" else derived_b
    selection_path = SELECTION_PATHS[experiment]
    frozen = CodeJudgeV3Selection.model_validate_json(selection_path.read_text(encoding="utf-8"))
    if frozen != derived:
        raise CodeJudgeEvalError(
            f"re-derived v3-{experiment.upper()} selection differs from {selection_path.name}"
        )
    plan = build_v3_call_plan(frozen, samples, experiment=experiment)
    return adapter, frozen, plan


async def _public_provider_configuration() -> tuple[bool, dict[str, Any]]:
    settings = get_settings()
    configured = settings.hy3_configured()
    if not configured:
        return False, {
            "provider": "hy3",
            "configured": False,
            "base_url_present": bool(settings.hy3_base_url),
            "api_key_present": bool(settings.hy3_api_key),
            "model_present": bool(settings.hy3_model),
        }
    provider = Hy3JudgeOnlyProvider(settings)
    try:
        return True, {**provider.public_configuration(), "configured": True}
    finally:
        await provider.aclose()


async def _preflight(experiment: Literal["a", "b"]) -> int:
    adapter, selection, plan = _load_plan(experiment)
    configured, provider_config = await _public_provider_configuration()
    selection_relative = SELECTION_PATHS[experiment].relative_to(REPO_ROOT).as_posix()
    scope_clean, dirty = experiment_scope_status(REPO_ROOT, formal_scope_files(selection_relative))
    commit, global_dirty = current_git_state(REPO_ROOT)
    manifest_validation_passed = False
    protocol_sha256 = None
    if scope_clean and configured:
        manifest = build_v3_run_manifest(
            repo=REPO_ROOT,
            run_id=f"v3{experiment}-preflight",
            experiment=experiment,
            selection=selection,
            selection_path=SELECTION_PATHS[experiment],
            plan=plan,
            provider_config={
                key: value for key, value in provider_config.items() if key != "configured"
            },
            started_at="2000-01-01T00:00:00+00:00",
        )
        manifest_validation_passed = True
        protocol_sha256 = manifest.protocol_sha256
    payload = {
        "preflight": "codejudge-eval-v3-formal-v1",
        "network_calls": 0,
        "manifest_validation_passed": manifest_validation_passed,
        "protocol_sha256": protocol_sha256,
        "experiment": experiment,
        "experiment_id": selection.experiment_id,
        "dataset_revision": adapter.descriptor.revision,
        "selection_file": selection_relative,
        "selection_file_sha256": file_sha256(SELECTION_PATHS[experiment]),
        "selection_entries_sha256": selection.entries_sha256,
        "prompt_bundle_sha256": selection.prompt_bundle_sha256,
        "planned_call_n": len(plan),
        "planned_calls_sha256": call_plan_sha256(plan),
        "git_commit": commit,
        "git_global_dirty": global_dirty,
        "git_experiment_scope_clean": scope_clean,
        "experiment_scope_issue_count": len(dirty),
        "experiment_scope_issue_preview": list(dirty[:8]),
        "provider": provider_config,
        "ready_for_formal_real_api": scope_clean and configured and manifest_validation_passed,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["ready_for_formal_real_api"] else 2


def _manifest_equivalent_for_resume(stored: V3RunManifest, current: V3RunManifest) -> bool:
    ignored = {"git_global_dirty"}
    return stored.model_dump(exclude=ignored) == current.model_dump(exclude=ignored)


async def _run_formal(
    *,
    experiment: Literal["a", "b"],
    run_id: str,
    resume: bool,
) -> None:
    _, selection, plan = _load_plan(experiment)
    provider = Hy3JudgeOnlyProvider()
    provider_config = provider.public_configuration()
    run_dir = RUNS_DIR / run_id
    records_path = run_dir / "records.jsonl"
    raw_path = run_dir / "provider_raw.jsonl"
    invocations_path = run_dir / "invocations.jsonl"
    manifest_path = run_dir / "manifest.json"
    report_path = run_dir / "report.json"
    receipt_path = run_dir / "completion_receipt.json"
    try:
        if resume:
            if not run_dir.is_dir() or not manifest_path.is_file():
                raise CodeJudgeEvalError("--resume requires an existing run manifest")
            if receipt_path.exists():
                raise CodeJudgeEvalError("run already has a completion receipt")
            stored_manifest = V3RunManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
            current_manifest = build_v3_run_manifest(
                repo=REPO_ROOT,
                run_id=run_id,
                experiment=experiment,
                selection=selection,
                selection_path=SELECTION_PATHS[experiment],
                plan=plan,
                provider_config=provider_config,
                started_at=stored_manifest.started_at,
            )
            if not _manifest_equivalent_for_resume(stored_manifest, current_manifest):
                raise CodeJudgeEvalError("resume environment differs from frozen run manifest")
            manifest = stored_manifest
        else:
            if run_dir.exists():
                raise CodeJudgeEvalError(f"run directory already exists: {run_dir}")
            started_at = datetime.now(UTC).isoformat()
            manifest = build_v3_run_manifest(
                repo=REPO_ROOT,
                run_id=run_id,
                experiment=experiment,
                selection=selection,
                selection_path=SELECTION_PATHS[experiment],
                plan=plan,
                provider_config=provider_config,
                started_at=started_at,
            )
            run_dir.mkdir(parents=True, mode=0o700)
            os.chmod(run_dir, 0o700)
            _atomic_write_once_or_equal(manifest_path, manifest.model_dump(mode="json"))

        for path in (records_path, raw_path, invocations_path):
            _ensure_private_file(path)
        records = list(_read_records(records_path))
        validate_resume_prefix(
            tuple(records),
            plan,
            experiment_id=selection.experiment_id,
        )
        print(
            f"[run] {selection.experiment_id} run_id={run_id} "
            f"records={len(records)}/{len(plan)} manifest={manifest_path}",
            flush=True,
        )
        print(
            "[recovery] "
            f"python3 scripts/run_codejudge_eval_v3.py --experiment {experiment} "
            f"--run-id {run_id} --resume --confirm-real-provider",
            flush=True,
        )
        invocation_index = (
            sum(
                json.loads(line).get("event") == "start"
                for line in invocations_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
            + 1
        )
        _append_jsonl(
            invocations_path,
            {
                "event": "start",
                "invocation_index": invocation_index,
                "started_at": datetime.now(UTC).isoformat(),
                "resume": resume,
                "record_prefix_n": len(records),
            },
        )

        def raw_sink(call: PlannedV3Call, stage: str, raw: str) -> None:
            _append_jsonl(
                raw_path,
                {
                    "planned_call_index": call.planned_call_index,
                    "condition": call.condition,
                    "stage": stage,
                    "raw": raw,
                },
            )

        started = time.perf_counter()
        for call, sample in plan[len(records) :]:
            record = await judge_v3_call(
                provider,
                call,
                sample,
                experiment_id=selection.experiment_id,
                raw_sink=raw_sink,
            )
            _append_jsonl(records_path, record.model_dump(mode="json"))
            records.append(record)
            print(
                f"[progress] {len(records)}/{len(plan)} "
                f"{call.raw_task_id}/{call.data_id}:{call.condition} -> {record.status.value} "
                f"({time.perf_counter() - started:.0f}s this invocation)",
                flush=True,
            )

        final_records = tuple(records)
        validate_resume_prefix(
            final_records,
            plan,
            experiment_id=selection.experiment_id,
        )
        report = (
            score_v3_a(final_records, plan)
            if experiment == "a"
            else score_v3_b(final_records, plan)
        )
        completed_at = datetime.now(UTC).isoformat()
        report.update(
            {
                "run_id": run_id,
                "prompt_bundle_sha256": selection.prompt_bundle_sha256,
                "selection_file_sha256": file_sha256(SELECTION_PATHS[experiment]),
                "protocol_sha256": manifest.protocol_sha256,
                "started_at": manifest.started_at,
                "completed_at": completed_at,
            }
        )
        _atomic_write_once_or_equal(report_path, report)
        counters = provider.audit_counters()
        _append_jsonl(
            invocations_path,
            {
                "event": "complete",
                "invocation_index": invocation_index,
                "completed_at": completed_at,
                "records_n": len(final_records),
                "provider_calls_this_invocation": counters["calls"],
                "provider_attempts_this_invocation": counters["attempts"],
                "provider_retries_this_invocation": counters["retries"],
            },
        )
        receipt = build_v3_completion_receipt(
            run_dir=run_dir,
            manifest=manifest,
            completed_at=completed_at,
            audit={
                "records": len(final_records),
                "valid_judgments": sum(
                    record.status.value == "valid_judgment" for record in final_records
                ),
                "parse_repairs": sum(record.parse_repairs for record in final_records),
                "raw_provider_events": _count_jsonl(raw_path),
                "invocations": invocation_index,
                "provider_calls_final_invocation": counters["calls"],
                "provider_attempts_final_invocation": counters["attempts"],
                "provider_retries_final_invocation": counters["retries"],
            },
        )
        _atomic_write_once_or_equal(receipt_path, receipt)
        print(f"[done] {selection.experiment_id}: {report_path}", flush=True)
    finally:
        await provider.aclose()


def _default_run_id(experiment: str) -> str:
    return f"v3{experiment}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def _validate_run_id(run_id: str, experiment: str) -> None:
    pattern = rf"^v3{experiment}-[A-Za-z0-9][A-Za-z0-9._-]{{0,100}}$"
    if re.fullmatch(pattern, run_id) is None:
        raise SystemExit(f"run id must match v3{experiment}-[A-Za-z0-9][A-Za-z0-9._-]*")


def _validate_run_directory(run_dir: Path) -> None:
    if RUNS_DIR.is_symlink() or run_dir.is_symlink():
        raise CodeJudgeEvalError("run output directory cannot be a symlink")
    if run_dir.parent.resolve() != RUNS_DIR.resolve():
        raise CodeJudgeEvalError("run output directory escapes the frozen runs root")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment", choices=("a", "b"), required=True)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--confirm-real-provider", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.preflight:
        if args.confirm_real_provider or args.resume:
            raise SystemExit("--preflight cannot be combined with execution flags")
        raise SystemExit(asyncio.run(_preflight(args.experiment)))
    if not args.confirm_real_provider:
        raise SystemExit(
            "formal v3 execution requires --confirm-real-provider; no API call was made"
        )
    run_id = args.run_id or _default_run_id(args.experiment)
    _validate_run_id(run_id, args.experiment)
    _validate_run_directory(RUNS_DIR / run_id)
    if args.resume and not args.run_id:
        raise SystemExit("--resume requires the original --run-id")
    asyncio.run(_run_formal(experiment=args.experiment, run_id=run_id, resume=args.resume))


if __name__ == "__main__":
    main()
