#!/usr/bin/env python3
"""Run the frozen CodeJudge-Eval judge-only experiment against real Hy3.

Order of operations (freeze-before-run):

1. Load the pinned 0-shot files (ambiguous-interface records excluded and
   counted), re-derive the deterministic selection, and verify it matches the
   frozen ``selection_v2.json`` plus current on-disk file hashes.
2. Freeze the experiment manifest (prompt bundle hash, judged-subset binding,
   git identity, provider/model, exclusions), the CodeJudge run sidecar
   (source/lock/data/selection hashes, Python version, ordered judged
   triples), and the provider config — all BEFORE any model call.  A
   ``--limit`` run is marked as smoke and binds only the judged subset.
3. Judge the selected samples sequentially; raw responses are teed to a
   private ``provider_raw.jsonl``; every sample yields one frozen
   ``BenchmarkJudgeRecord`` in ``records.jsonl``.
4. Emit ``report.json`` from ``score_judge_only`` (allowed metrics only) and a
   ``completion_receipt.json`` binding times, retry/repair audit counts, and
   output hashes.

Usage: python3 scripts/run_codejudge_eval_judge_only.py [--limit N] [--run-id ID]
       [--allow-dirty]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tracejudge_hy3.benchmark.codejudge_eval import (  # noqa: E402
    CodeJudgeEvalAdapter,
    CodeJudgeEvalSample,
    file_sha256,
    select_samples,
    verify_selection_files,
)
from tracejudge_hy3.benchmark.contracts import BenchmarkJudgeRecord  # noqa: E402
from tracejudge_hy3.benchmark.hy3_judge_only import Hy3JudgeOnlyProvider  # noqa: E402
from tracejudge_hy3.benchmark.judge_only_manifest import (  # noqa: E402
    build_completion_receipt,
    build_experiment_manifest,
    build_run_sidecar,
    current_git_state,
)
from tracejudge_hy3.benchmark.judge_only_metrics import score_judge_only  # noqa: E402
from tracejudge_hy3.benchmark.judge_only_runner import judge_samples  # noqa: E402
from tracejudge_hy3.exceptions import ProviderError  # noqa: E402

DATA_DIR = REPO_ROOT / "data" / "codejudge_eval"
FROZEN_SELECTION = DATA_DIR / "selection_v2.json"


class TeeProvider:
    """Wrap the real provider, persisting every raw response privately."""

    def __init__(self, inner: Hy3JudgeOnlyProvider, raw_path: Path) -> None:
        self._inner = inner
        self._raw_path = raw_path
        self._call_index = 0

    @property
    def call_count(self) -> int:
        return self._call_index

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        self._call_index += 1
        call_index = self._call_index
        try:
            raw = await self._inner.complete(system_prompt=system_prompt, user_prompt=user_prompt)
        except ProviderError as exc:
            self._append(call_index, f"__PROVIDER_ERROR__ {type(exc).__name__}: {exc}")
            raise
        self._append(call_index, raw)
        return raw

    def _append(self, call_index: int, raw: str) -> None:
        with self._raw_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"call_index": call_index, "raw": raw}, ensure_ascii=False) + "\n")


def _canonical_line(model: object) -> str:
    return json.dumps(model, ensure_ascii=False, allow_nan=False, sort_keys=True)  # type: ignore[arg-type]


def _write_json(path: Path, payload: object) -> str:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return file_sha256(path)


async def run(*, limit: int | None, run_id: str, allow_dirty: bool) -> None:
    smoke = limit is not None
    git_commit, git_dirty = current_git_state(REPO_ROOT)
    if git_dirty and not smoke and not allow_dirty:
        raise SystemExit(
            "formal (non-smoke) runs require a clean commit; pass --allow-dirty to "
            "override (the sidecar will then bind source-file hashes instead)"
        )

    adapter = CodeJudgeEvalAdapter()
    samples = adapter.load_samples(DATA_DIR, exclude_ambiguous_interface=True)
    descriptor = adapter.descriptor
    selection = select_samples(samples, descriptor=descriptor)
    verify_selection_files(selection, DATA_DIR)

    frozen = json.loads(FROZEN_SELECTION.read_text(encoding="utf-8"))
    if selection.model_dump(mode="json") != frozen:
        raise SystemExit("re-derived selection does not match frozen selection_v2.json")

    sample_index = {(s.task.identity.task_id, s.candidate.candidate_id): s for s in samples}
    selected_samples: list[CodeJudgeEvalSample] = []
    for entry in selection.entries:
        selected_samples.append(sample_index[(entry.task_id, entry.candidate_id)])
    judged_entries = selection.entries
    if limit is not None:
        if limit < 1 or limit > len(selection.entries):
            raise SystemExit(f"--limit must be within 1..{len(selection.entries)}")
        selected_samples = selected_samples[:limit]
        judged_entries = selection.entries[:limit]

    provider = Hy3JudgeOnlyProvider()
    config = provider.public_configuration()
    manifest = build_experiment_manifest(
        descriptor=descriptor,
        selection=selection,
        git_commit=git_commit,
        git_dirty=git_dirty,
        provider="hy3",
        model=config["model"],
        extra_limitations=(
            f"excluded-ambiguous-interface-records:{len(adapter.excluded_records)}",
        ),
        judged_entries=judged_entries,
        smoke=smoke,
    )

    run_dir = DATA_DIR / "runs" / run_id
    if run_dir.exists():
        raise SystemExit(f"run directory already exists: {run_dir}")
    run_dir.mkdir(parents=True)

    # Freeze-before-run: manifest, sidecar and provider config hit disk before
    # any model call; started_at is captured before the first call.
    started_at = datetime.now(UTC).isoformat()
    sidecar = build_run_sidecar(
        manifest=manifest,
        provider_config=config,
        judged_entries=judged_entries,
        selection=selection,
        selection_file_sha256=file_sha256(FROZEN_SELECTION),
        repo=REPO_ROOT,
        started_at=started_at,
    )
    manifest_sha = _write_json(run_dir / "manifest.json", manifest.model_dump(mode="json"))
    _write_json(run_dir / "provider_config.json", config)
    _write_json(
        run_dir / "excluded_records.json",
        [e.model_dump(mode="json") for e in adapter.excluded_records],
    )
    sidecar_sha = _write_json(run_dir / "manifest_sidecar.json", sidecar)

    raw_path = run_dir / "provider_raw.jsonl"
    raw_path.touch()
    tee = TeeProvider(provider, raw_path)

    print(f"[run] manifest frozen at {run_dir} (experiment {manifest.experiment_id})", flush=True)
    print(f"[run] judging {len(selected_samples)} samples ...", flush=True)
    started = time.perf_counter()
    records: list[BenchmarkJudgeRecord] = []
    for index, sample in enumerate(selected_samples, start=1):
        record = (await judge_samples(tee, (sample,)))[0]
        records.append(record)
        elapsed = time.perf_counter() - started
        print(
            f"[progress] {index}/{len(selected_samples)} "
            f"{sample.task.identity.task_id} -> {record.status.value} "
            f"({elapsed:.0f}s elapsed)",
            flush=True,
        )
        with (run_dir / "records.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(_canonical_line(record.model_dump(mode="json")) + "\n")

    completed_at = datetime.now(UTC).isoformat()
    report = score_judge_only(records, selected_samples)
    report["run_id"] = run_id
    report["experiment_id"] = manifest.experiment_id
    report["started_at"] = started_at
    report["completed_at"] = completed_at
    report["n_judged"] = len(records)
    report["n_selected"] = len(selection.entries)
    _write_json(run_dir / "report.json", report)

    counters = provider.audit_counters()
    receipt = build_completion_receipt(
        run_dir=run_dir,
        run_id=run_id,
        started_at=started_at,
        completed_at=completed_at,
        audit={
            "provider_calls": tee.call_count,
            "provider_attempts": counters["attempts"],
            "provider_retries": counters["retries"],
            "parse_repairs": tee.call_count - len(records),
        },
        manifest_sha256=manifest_sha,
        sidecar_sha256=sidecar_sha,
    )
    _write_json(run_dir / "completion_receipt.json", receipt)
    await provider.aclose()
    print(f"[done] report at {run_dir / 'report.json'}", flush=True)
    print(json.dumps(report["correctness_accuracy_full_denominator"], ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--run-id",
        default=datetime.now(UTC).strftime("run-%Y%m%dT%H%M%SZ"),
    )
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(limit=args.limit, run_id=args.run_id, allow_dirty=args.allow_dirty))


if __name__ == "__main__":
    main()
