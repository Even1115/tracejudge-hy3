"""Budgeted live runner for the 2x2 evidence ablation.

Reuses the pilot's proven run machinery (RequestLedger, PilotJudgeProvider,
run lock, append-only checkpoints, conservative budget accounting, resume
identity checks) instead of reimplementing it, so budget/resume behaviour is
identical to ``--phase run``. Differences are only what the experiment needs:
four conditions judged in the plan's fixed-seed interleaved order, an
AblationPrediction record per judgment, and a pre-registered plan file that is
written before any request and hashed into the run identity.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from tracejudge_hy3.config import Settings, get_settings
from tracejudge_hy3.exceptions import (
    ConfigurationError,
    ProviderAuthError,
    ProviderError,
)
from tracejudge_hy3.process_eval_v2.ablation import (
    CONDITIONS,
    PLAN_SCHEMA,
    RUN_CONFIG_SCHEMA,
    RUN_REPORT_SCHEMA,
    AblationPrediction,
    _functional_snapshot,
    build_plan,
    judge_ablation,
    plan_fingerprint,
)
from tracejudge_hy3.process_eval_v2.live import (
    BudgetExhaustedError,
    JudgmentCapExceededError,
    PilotJudgeProvider,
    RequestLedger,
    _append_jsonl,
    _check_resume_identity,
    _classify_failure,
    _default_settings,
    _implementation_fingerprints,
    _load_ledger_rows,
    _RunLock,
    _token_totals,
)
from tracejudge_hy3.process_eval_v2.materials import canonical, digest
from tracejudge_hy3.process_eval_v2.preflight import PreparedPilot, prepare_pilot, read_json
from tracejudge_hy3.providers.telemetry import failure_observer, request_observer

PLAN_FILE = "ablation-plan.json"
PREFLIGHT_FILE = "ablation-preflight.json"
PREDICTIONS_FILE = "predictions.jsonl"
REQUESTS_FILE = "requests.jsonl"
CONFIG_FILE = "run-config.json"
LOCK_FILE = "run.lock"
REPORT_FILE = "run-report.json"

RunStatus = Literal["completed", "budget_exhausted", "interrupted", "partial_execution"]


def _load_ablation_predictions(
    path: Path, pilot: PreparedPilot
) -> dict[tuple[str, str], AblationPrediction]:
    done: dict[tuple[str, str], AblationPrediction] = {}
    if not path.exists():
        return done
    for number, line in enumerate(path.read_bytes().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            prediction = AblationPrediction.model_validate(read_json(line))
        except ValueError as exc:
            raise ValueError(
                f"{PREDICTIONS_FILE} is corrupt at line {number}; refusing to resume"
            ) from exc
        key = (prediction.condition, prediction.item_id)
        if prediction.item_id not in pilot.inputs or key in done:
            raise ValueError(f"{PREDICTIONS_FILE} has a duplicate or unknown row at line {number}")
        if prediction.input_sha256 != pilot.input_hash(prediction.item_id):
            raise ValueError(f"{PREDICTIONS_FILE} row at line {number} binds a different input")
        done[key] = prediction
    return done


def _ablation_identity(
    pilot: PreparedPilot,
    *,
    config_sha256: str,
    plan: dict,
    provider: PilotJudgeProvider,
    max_requests: int,
    max_attempts_per_judgment: int,
) -> dict:
    return {
        "schema": RUN_CONFIG_SCHEMA,
        "pilot_config_sha256": config_sha256,
        "plan_sha256": plan_fingerprint(plan),
        "input_hashes": {key: pilot.input_hash(key) for key in sorted(pilot.inputs)},
        "conditions": list(plan["conditions"]),
        "condition_fingerprints": {
            key: value["fingerprint"] for key, value in plan["conditions"].items()
        },
        "prediction_schema_sha256": digest(canonical(AblationPrediction.model_json_schema())),
        "model": provider.public_generation_config(),
        "budget": {
            "max_requests": max_requests,
            "max_attempts_per_judgment": max_attempts_per_judgment,
        },
        "implementation_sha256": _implementation_fingerprints(),
    }


def _write_json(path: Path, payload: dict) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _check_plan_file(target: Path, plan: dict) -> None:
    """A stored plan must match the rebuilt one exactly (ignoring timestamps)."""

    path = target / PLAN_FILE
    if not path.exists():
        raise ValueError(f"{PLAN_FILE} is missing; refusing to use this run directory")
    try:
        stored = read_json(path.read_bytes())
    except ValueError as exc:
        raise ValueError(f"{PLAN_FILE} is corrupt; refusing to resume") from exc
    if not isinstance(stored, dict) or stored.get("schema") != PLAN_SCHEMA:
        raise ValueError(f"{PLAN_FILE} is not a {PLAN_SCHEMA} plan")
    if plan_fingerprint(stored) != plan_fingerprint(plan):
        raise ValueError(
            "the pre-registered plan changed since it was written; refusing to mix runs. "
            "Use a new output directory instead."
        )


def _build_ablation_report(
    identity: dict,
    ledger: RequestLedger,
    done: dict[tuple[str, str], AblationPrediction],
    pilot: PreparedPilot,
    plan: dict,
    target: Path,
    *,
    status: RunStatus,
    stop_reason: str | None,
    resumed: bool,
) -> dict:
    judgments = {}
    pending = []
    for condition in plan["conditions"]:
        coverage: Counter[str] = Counter()
        for item_id in sorted(pilot.inputs):
            prediction = done.get((condition, item_id))
            if prediction is None:
                coverage["pending"] += 1
                pending.append(
                    {
                        "condition": condition,
                        "item_id": item_id,
                        "attempts_consumed": ledger.consumed_for(condition, item_id),
                    }
                )
            else:
                coverage[prediction.status] += 1
        judgments[condition] = dict(coverage)
    outcomes = ledger.outcomes
    seconds = [row.get("request_seconds") for row in outcomes]
    files = {}
    for name in (PREDICTIONS_FILE, REQUESTS_FILE, CONFIG_FILE, PLAN_FILE, "diagnostics.jsonl"):
        path = target / name
        if path.exists():
            files[name] = digest(path.read_bytes())
    return {
        "schema": RUN_REPORT_SCHEMA,
        "status": status,
        "stop_reason": stop_reason,
        "resumed": resumed,
        "finished_utc": datetime.now(UTC).isoformat(),
        "run_identity": identity,
        "execution_order": plan["execution_order"],
        "budget": {
            "max_requests": ledger.max_requests,
            "max_attempts_per_judgment": ledger.max_attempts_per_judgment,
            "consumed_requests": ledger.total_dispatched,
            "remaining_requests": ledger.remaining,
            "accounting": (
                "one request is one model call attempt, including failed attempts and "
                "format-repair turns; SDK retries are disabled and the runner adds no "
                "retry layer of its own; consumption survives restarts"
            ),
            "money_estimate": None,
            "money_note": "no per-token price is configured; cost stays unknown",
        },
        "requests": {
            "dispatched": ledger.total_dispatched,
            "outcomes_recorded": len(outcomes),
            "outcome_status": dict(Counter(row.get("status") for row in outcomes)),
            "error_types": dict(
                Counter(row["error_type"] for row in outcomes if row.get("error_type"))
            ),
            "total_request_seconds": round(sum(value for value in seconds if value is not None), 3),
            **_token_totals(outcomes),
        },
        "judgments": judgments,
        "pending": pending,
        "files_sha256": files,
        "notes": [
            "coverage counts come from recorded predictions only; planned judgments are not results",
            "a pending judgment with attempts_consumed > 0 was interrupted mid-flight and its "
            "spent budget is never reclaimed",
            "rule merging is offline post-processing of the same judge output and never "
            "consumes request budget",
            "labels are post-feedback development consensus, not independent held-out gold",
            "token usage depends on the provider response; money is never estimated",
        ],
    }


def _failure_prediction(
    pilot: PreparedPilot, condition: str, item_id: str, status: str
) -> AblationPrediction:
    return AblationPrediction(
        item_id=item_id,
        condition=condition,  # type: ignore[arg-type]
        input_sha256=pilot.input_hash(item_id),
        status=status,  # type: ignore[arg-type]
        functional_evidence=_functional_snapshot(pilot.inputs[item_id]),
    )


def run_ablation(
    root: Path,
    config_path: Path,
    target: Path,
    *,
    execute: bool,
    resume: bool,
    max_requests: int = 96,
    max_attempts_per_judgment: int = 2,
    settings: Settings | None = None,
) -> dict:
    """Write the pre-registered plan (always) and optionally run real judgments.

    Without ``execute`` only the plan and an offline preflight summary are
    written -- no provider is constructed and no credential is read. With
    ``execute`` the interleaved judgments run under the enforced budget;
    ``resume`` continues an existing run directory after identity checks.
    """

    root = root.resolve()
    pilot = prepare_pilot(root, config_path)
    config_file = config_path if config_path.is_absolute() else root / config_path
    return _run_ablation(
        pilot,
        digest(config_file.read_bytes()),
        target,
        execute=execute,
        resume=resume,
        max_requests=max_requests,
        max_attempts_per_judgment=max_attempts_per_judgment,
        settings=settings if settings is not None else _default_settings(root),
    )


def _run_ablation(
    pilot: PreparedPilot,
    config_sha256: str,
    target: Path,
    *,
    execute: bool,
    resume: bool,
    max_requests: int,
    max_attempts_per_judgment: int,
    settings: Settings | None = None,
    rubric: str = "baseline",
    conditions: tuple[str, ...] = CONDITIONS,
) -> dict:
    plan = build_plan(
        pilot,
        max_requests=max_requests,
        max_attempts_per_judgment=max_attempts_per_judgment,
        rubric=rubric,
        conditions=conditions,
    )

    if target.exists():
        _check_plan_file(target, plan)
    else:
        if resume:
            raise ValueError(f"cannot resume: run directory does not exist: {target}")
        target.mkdir(parents=True, exist_ok=False)
        _write_json(target / PLAN_FILE, plan)

    if not execute:
        preflight = {
            "schema": "tracejudge-process-ablation-preflight-v1",
            "status": "plan_registered_offline",
            "plan_sha256": plan_fingerprint(plan),
            "planned_judgments": plan["budget"]["planned_judgments"],
            "budget": plan["budget"],
            "conditions": plan["conditions"],
            "calls_made": {"solver": 0, "judge": 0, "candidate_executions": 0},
            "notes": [
                "the plan is fixed before any request; rerun with --execute to dispatch",
                "planned judgments are not results",
            ],
        }
        if not (target / PREFLIGHT_FILE).exists():
            _write_json(target / PREFLIGHT_FILE, preflight)
        return preflight

    base_settings = settings or get_settings()
    # One retry layer only: the provider's, bounded by the per-judgment cap.
    run_settings = base_settings.model_copy(
        update={
            "hy3_max_retries": max_attempts_per_judgment - 1,
            "hy3_max_parse_repairs": min(1, max_attempts_per_judgment - 1),
        }
    )

    ledger_box: list[RequestLedger | None] = [None]

    def charge(method: str, item_id: str) -> None:
        ledger = ledger_box[0]
        if ledger is None:  # pragma: no cover - defensive
            raise ConfigurationError("request attempted before the ledger was opened")
        ledger.charge(method, item_id)

    provider = PilotJudgeProvider(run_settings, charge)
    identity = _ablation_identity(
        pilot,
        config_sha256=config_sha256,
        plan=plan,
        provider=provider,
        max_requests=max_requests,
        max_attempts_per_judgment=max_attempts_per_judgment,
    )

    config_path_in_dir = target / CONFIG_FILE
    if config_path_in_dir.exists():
        if not resume:
            raise ValueError(
                f"run config already exists in {target}; pass --resume to continue it "
                "or choose a new directory"
            )
        try:
            stored = read_json(config_path_in_dir.read_bytes())
        except ValueError as exc:
            raise ValueError(f"{CONFIG_FILE} is corrupt; refusing to resume") from exc
        _check_resume_identity(stored, identity)
    # A plan-only directory (offline phase ran earlier) starts fresh dispatch.

    lock = _RunLock(target / LOCK_FILE)
    lock.acquire()
    try:
        if not config_path_in_dir.exists():
            _append_jsonl(config_path_in_dir, identity)
        ledger = RequestLedger(
            target / REQUESTS_FILE,
            max_requests=max_requests,
            max_attempts_per_judgment=max_attempts_per_judgment,
            prior_rows=_load_ledger_rows(target / REQUESTS_FILE),
        )
        ledger_box[0] = ledger
        done = _load_ablation_predictions(target / PREDICTIONS_FILE, pilot)

        if resume:
            # Failed judgments with attempts left are retried; their stale rows are
            # replaced atomically so the file never holds two rows for one judgment.
            retryable = sorted(
                key
                for key, prediction in done.items()
                if prediction.status != "ok"
                and ledger.consumed_for(*key) < max_attempts_per_judgment
            )
            if retryable:
                kept = [row for key, row in done.items() if key not in retryable]
                temporary = target / f"{PREDICTIONS_FILE}.tmp"
                with temporary.open("wb") as handle:
                    for row in kept:
                        handle.write(canonical(row.model_dump(mode="json")) + b"\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target / PREDICTIONS_FILE)
                for key in retryable:
                    del done[key]

        state: dict[str, Any] = {"stop_reason": None}
        order = [(condition, item_id) for condition, item_id in plan["execution_order"]]

        async def _dispatch() -> None:
            token = request_observer.set(ledger.observe)
            failure_token = failure_observer.set(ledger.diagnose)
            try:
                for condition, item_id in order:
                    key = (condition, item_id)
                    if key in done:
                        continue  # recorded rows are final for this invocation
                    if ledger.consumed_for(condition, item_id) >= max_attempts_per_judgment:
                        continue  # attempts were spent by an earlier interrupted run
                    if ledger.remaining <= 0:
                        state["stop_reason"] = "budget_exhausted"
                        break
                    provider.begin_judgment(condition, item_id)
                    outcomes_before = len(ledger.outcomes)
                    try:
                        if rubric == "baseline":
                            prediction = await judge_ablation(
                                condition, pilot.inputs[item_id], provider, pilot
                            )
                        else:
                            prediction = await judge_ablation(
                                condition, pilot.inputs[item_id], provider, pilot, rubric=rubric
                            )
                    except BudgetExhaustedError:
                        # Spent attempts stay spent; this judgment stays pending.
                        state["stop_reason"] = "budget_exhausted"
                        break
                    except JudgmentCapExceededError:
                        status = _classify_failure(ledger.outcomes[outcomes_before:])
                        prediction = _failure_prediction(pilot, condition, item_id, status)
                    except ProviderAuthError:
                        prediction = _failure_prediction(
                            pilot, condition, item_id, "provider_error"
                        )
                        state["stop_reason"] = "auth_error"
                    except ProviderError:
                        status = _classify_failure(ledger.outcomes[outcomes_before:])
                        prediction = _failure_prediction(pilot, condition, item_id, status)
                    _append_jsonl(target / PREDICTIONS_FILE, prediction.model_dump(mode="json"))
                    done[key] = prediction
                    if state["stop_reason"] == "auth_error":
                        break
            finally:
                failure_observer.reset(failure_token)
                request_observer.reset(token)

        interrupted = False
        try:
            asyncio.run(_dispatch())
        except KeyboardInterrupt:
            interrupted = True
            state["stop_reason"] = "interrupted"

        if state["stop_reason"] is not None:
            status: RunStatus = (
                "interrupted"
                if interrupted
                else "budget_exhausted"
                if state["stop_reason"] == "budget_exhausted"
                else "partial_execution"
            )
        elif len(done) == len(order):
            status = "completed"
        else:
            status = "partial_execution"

        report = _build_ablation_report(
            identity,
            ledger,
            done,
            pilot,
            plan,
            target,
            status=status,
            stop_reason=state["stop_reason"],
            resumed=resume,
        )
        _write_json(target / REPORT_FILE, report)
        return report
    finally:
        lock.release()
        # Closing the client must never lose an already-written report.
        with contextlib.suppress(Exception):
            asyncio.run(provider.aclose())
