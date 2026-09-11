"""Live pilot runner: budgeted real model calls with resumable checkpoints.

Runs only via ``scripts/run_process_pilot.py --phase run --execute``; the
default phase stays offline preflight. Design rules:

- One "request" is one model call attempt, including failures and format-repair
  turns. The SDK's own retries stay disabled (the provider is constructed with
  ``max_retries=0``) and this runner adds no retry layer of its own: the only
  retry loop is the provider's, bounded by the per-judgment attempt cap.
- Every request attempt is charged to the ledger BEFORE it is sent
  (conservative accounting: a request sent but interrupted before its outcome
  is persisted still consumes budget and is never silently repeated).
- Progress is append-only JSONL with fsync per record; resume re-verifies the
  run identity (inputs, prompts, model parameters, implementation hashes,
  budget caps) and refuses to mix changed configurations into an old run.
- No API key, Authorization header or environment dump is ever written to the
  run directory; the report records only the provider's redacted public config.
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
from tracejudge_hy3.evaluator.alignment import combine_assessment
from tracejudge_hy3.evaluator.rule_based import evaluate_alignment_rules
from tracejudge_hy3.exceptions import (
    ConfigurationError,
    ProviderAuthError,
    ProviderError,
)
from tracejudge_hy3.process_eval_v2.aggregate import (
    aggregate_execution_summary,
    apply_aggregate_functional,
    build_scaffold_problem,
)
from tracejudge_hy3.process_eval_v2.consistency import check_public_consistency, public_assessment
from tracejudge_hy3.process_eval_v2.contracts import (
    METHODS,
    Method,
    PilotInput,
    Prediction,
    public_process_correct,
)
from tracejudge_hy3.process_eval_v2.materials import canonical, digest
from tracejudge_hy3.process_eval_v2.preflight import (
    PreparedPilot,
    prepare_pilot,
    read_json,
)
from tracejudge_hy3.process_eval_v2.prompts import (
    DirectJudgment,
    build_direct_judge_prompt,
    build_full_system_prompt,
    build_structured_judge_prompt,
    method_fingerprints,
)
from tracejudge_hy3.providers.hy3_openai import Hy3OpenAIProvider
from tracejudge_hy3.providers.telemetry import failure_observer, request_observer
from tracejudge_hy3.schemas.evaluation import ProcessAssessment
from tracejudge_hy3.static_analysis.ast_analyzer import analyze_code

RUN_CONFIG_SCHEMA = "tracejudge-process-pilot-run-config-v1"
RUN_REPORT_SCHEMA = "tracejudge-process-pilot-run-v1"
PREDICTIONS_FILE = "predictions.jsonl"
REQUESTS_FILE = "requests.jsonl"
CONFIG_FILE = "run-config.json"
LOCK_FILE = "run.lock"
REPORT_FILE = "run-report.json"
DIAGNOSTICS_FILE = "diagnostics.jsonl"

RunStatus = Literal["completed", "budget_exhausted", "interrupted", "partial_execution"]


class BudgetExhaustedError(ProviderError):
    """Raised before sending when the cumulative request budget is exhausted."""


class JudgmentCapExceededError(ProviderError):
    """Raised before sending when one judgment already consumed its attempt cap."""


def _append_jsonl(path: Path, row: dict) -> None:
    """Append one canonical JSON line and fsync before returning."""

    with path.open("ab") as handle:
        handle.write(canonical(row) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


class RequestLedger:
    """Cumulative request accounting backed by an append-only JSONL ledger.

    ``dispatched`` rows are written before a request is sent; ``outcome`` rows
    follow it. Budget consumption counts ``dispatched`` rows only, so a crash
    between dispatch and outcome is conservatively treated as a spent request.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_requests: int,
        max_attempts_per_judgment: int,
        prior_rows: list[dict],
    ) -> None:
        if max_requests < 1 or max_attempts_per_judgment < 1:
            raise ValueError("request budget and per-judgment cap must be positive")
        self.path = path
        self.max_requests = max_requests
        self.max_attempts_per_judgment = max_attempts_per_judgment
        self.total_dispatched = 0
        self.dispatched_per_judgment: Counter[tuple[str, str]] = Counter()
        self.outcomes: list[dict] = []
        self._last_dispatch: tuple[str, str] | None = None
        for row in prior_rows:
            if row["event"] == "dispatched":
                self.total_dispatched += 1
                self.dispatched_per_judgment[(row["method"], row["item_id"])] += 1
            elif row["event"] == "outcome":
                self.outcomes.append(row)
            else:
                raise ValueError(f"unknown ledger event: {row['event']!r}")

    @property
    def remaining(self) -> int:
        return self.max_requests - self.total_dispatched

    def consumed_for(self, method: str, item_id: str) -> int:
        return self.dispatched_per_judgment[(method, item_id)]

    def charge(self, method: str, item_id: str) -> None:
        """Charge one request attempt before it is sent; may raise to block it."""

        if self.total_dispatched >= self.max_requests:
            raise BudgetExhaustedError(
                f"cumulative request budget exhausted ({self.max_requests} request(s)); "
                "no further requests will be sent"
            )
        if self.consumed_for(method, item_id) >= self.max_attempts_per_judgment:
            raise JudgmentCapExceededError(
                f"judgment {method}/{item_id} already consumed its "
                f"{self.max_attempts_per_judgment} request cap"
            )
        _append_jsonl(
            self.path,
            {
                "event": "dispatched",
                "seq": self.total_dispatched + 1,
                "method": method,
                "item_id": item_id,
            },
        )
        self.total_dispatched += 1
        self.dispatched_per_judgment[(method, item_id)] += 1
        self._last_dispatch = (method, item_id)

    def diagnose(self, payload: dict[str, Any]) -> None:
        """Persist each observed failed attempt separately from the budget ledger."""
        if self._last_dispatch is None:
            raise ValueError("failure diagnostic without a dispatched request")
        method, item_id = self._last_dispatch
        _append_jsonl(
            self.path.with_name(DIAGNOSTICS_FILE),
            {
                **payload,
                "seq": self.total_dispatched,
                "method": method,
                "item_id": item_id,
                "attempt_in_judgment": self.consumed_for(method, item_id),
            },
        )

    def observe(self, payload: dict[str, Any]) -> None:
        """Telemetry sink; pairs with the most recent dispatch (runs sequentially)."""

        row = {
            "event": "outcome",
            "seq": self.total_dispatched,
            "status": payload.get("status"),
            "error_type": payload.get("error_type"),
            "request_seconds": payload.get("request_seconds"),
            "input_tokens": payload.get("input_tokens"),
            "output_tokens": payload.get("output_tokens"),
        }
        _append_jsonl(self.path, row)
        self.outcomes.append(row)


class PilotJudgeProvider(Hy3OpenAIProvider):
    """Hy3 provider whose every request attempt passes through the budget gate.

    Retry and format-repair behaviour is entirely the base class's
    ``_call_with_retries``; this subclass only adds the pre-send charge and a
    public entry point with an explicit output model, so no second retry layer
    exists anywhere.
    """

    def __init__(self, settings: Settings, charge) -> None:
        super().__init__(settings)
        self._pilot_charge = charge
        self._judgment_context: tuple[str, str] | None = None

    def begin_judgment(self, method: str, item_id: str) -> None:
        self._judgment_context = (method, item_id)

    async def _call_model(self, messages: list[dict[str, str]]) -> str:
        if self._judgment_context is None:
            raise ConfigurationError("pilot judge called outside a judgment context")
        self._pilot_charge(*self._judgment_context)
        return await super()._call_model(messages)

    async def judge_model(
        self, system_prompt: str, user_prompt: str, model_cls: type, extra_check=None
    ):
        return await self._call_with_retries(
            system_prompt, user_prompt, model_cls, extra_check=extra_check
        )


def _context_check(item: PilotInput, *, public_process: bool = False):
    """Validate step/requirement/location references against the frozen item."""

    steps = {step.step_id for step in item.solution_trace.implementation_steps}
    requirements = {req.requirement_id for req in item.problem.requirements}

    def check(assessment: ProcessAssessment) -> None:
        if public_process:
            check_public_consistency(assessment)
        assessment.validate_location_against(item.solution_trace)
        referenced = set(assessment.affected_steps)
        if assessment.first_faulty_step is not None:
            referenced.add(assessment.first_faulty_step)
        unknown_steps = referenced - steps
        if unknown_steps:
            raise ValueError(f"assessment references unknown step IDs: {sorted(unknown_steps)}")
        if (
            assessment.violated_requirement is not None
            and assessment.violated_requirement not in requirements
        ):
            raise ValueError(
                f"assessment references unknown requirement ID: {assessment.violated_requirement!r}"
            )

    return check


async def run_judgment(
    method: Method, item: PilotInput, provider: PilotJudgeProvider, pilot: PreparedPilot
) -> Prediction:
    """One judged prediction; every retry stays inside the provider call."""

    if method == "direct_judge":
        system, user = build_direct_judge_prompt(item)
        raw: DirectJudgment = await provider.judge_model(system, user, DirectJudgment)
        assessment = ProcessAssessment(
            reasoning_correct=raw.reasoning_correct,
            plan_code_aligned=raw.plan_code_aligned,
            functional_correct=None,
            process_correct=public_process_correct(raw.reasoning_correct, raw.plan_code_aligned),
            explanation=raw.explanation,
        )
    elif method == "structured_judge":
        system, user = build_structured_judge_prompt(item)
        judged: ProcessAssessment = await provider.judge_model(
            system, user, ProcessAssessment, extra_check=_context_check(item, public_process=True)
        )
        # The judge saw no functional evidence; any claimed value is a guess.
        assessment = public_assessment(judged)
    else:
        problem = build_scaffold_problem(item)
        static_evidence = analyze_code(
            item.solution_trace.code,
            function_name=problem.function_name,
            visible_test_values=[],
        )
        execution = aggregate_execution_summary(item)
        rule_assessment = evaluate_alignment_rules(
            problem, item.solution_trace, static_evidence, execution
        )
        system, user = build_full_system_prompt(item, static_evidence)
        llm_assessment = await provider.judge_model(
            system, user, ProcessAssessment, extra_check=_context_check(item, public_process=True)
        )
        combined = combine_assessment(
            problem,
            item.solution_trace,
            static_evidence,
            execution,
            llm_assessment,
            rule_assessment=rule_assessment,
        )
        assessment = apply_aggregate_functional(combined, item.functional_evidence)
    return Prediction(
        item_id=item.item_id,
        method=method,
        input_sha256=pilot.input_hash(item.item_id),
        status="ok",
        assessment=assessment,
    )


def _load_ledger_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for number, line in enumerate(path.read_bytes().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = read_json(line)
        except ValueError as exc:
            raise ValueError(
                f"{REQUESTS_FILE} is corrupt at line {number}; refusing to resume "
                "(fix or remove the run directory, never silently rerun)"
            ) from exc
        if (
            not isinstance(row, dict)
            or row.get("event") not in ("dispatched", "outcome")
            or (row["event"] == "dispatched" and not isinstance(row.get("item_id"), str))
        ):
            raise ValueError(f"{REQUESTS_FILE} has an invalid row at line {number}")
        rows.append(row)
    return rows


def _load_predictions(path: Path, pilot: PreparedPilot) -> dict[tuple[str, str], Prediction]:
    done: dict[tuple[str, str], Prediction] = {}
    if not path.exists():
        return done
    for number, line in enumerate(path.read_bytes().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            prediction = Prediction.model_validate(read_json(line))
        except ValueError as exc:
            raise ValueError(
                f"{PREDICTIONS_FILE} is corrupt at line {number}; refusing to resume"
            ) from exc
        key = (prediction.method, prediction.item_id)
        if prediction.item_id not in pilot.inputs or key in done:
            raise ValueError(f"{PREDICTIONS_FILE} has a duplicate or unknown row at line {number}")
        if prediction.input_sha256 != pilot.input_hash(prediction.item_id):
            raise ValueError(f"{PREDICTIONS_FILE} row at line {number} binds a different input")
        done[key] = prediction
    return done


def _implementation_fingerprints() -> dict[str, str]:
    root = Path(__file__).resolve().parents[3]
    paths = sorted((root / "src/tracejudge_hy3/process_eval_v2").glob("*.py"))
    paths += [
        root / "src/tracejudge_hy3/schemas/location.py",
        root / "src/tracejudge_hy3/schemas/evaluation.py",
        root / "src/tracejudge_hy3/providers/hy3_openai.py",
        root / "src/tracejudge_hy3/providers/telemetry.py",
        root / "src/tracejudge_hy3/redaction.py",
    ]
    # New runs also bind the static rules and schemas used by offline merging.
    for directory in ("evaluator", "static_analysis", "schemas", "parsing"):
        paths.extend((root / "src/tracejudge_hy3" / directory).glob("*.py"))
    paths.append(root / "src/tracejudge_hy3/prompts/evaluator.py")
    return {
        path.relative_to(root).as_posix(): digest(path.read_bytes()) for path in sorted(set(paths))
    }


def _run_identity(
    pilot: PreparedPilot,
    *,
    config_sha256: str,
    provider: PilotJudgeProvider,
    max_requests: int,
    max_attempts_per_judgment: int,
) -> dict:
    return {
        "schema": RUN_CONFIG_SCHEMA,
        "pilot_config_sha256": config_sha256,
        "input_hashes": {key: pilot.input_hash(key) for key in sorted(pilot.inputs)},
        "methods": list(METHODS),
        "method_fingerprints": method_fingerprints(),
        "prediction_schema_sha256": digest(canonical(Prediction.model_json_schema())),
        "model": provider.public_generation_config(),
        "budget": {
            "max_requests": max_requests,
            "max_attempts_per_judgment": max_attempts_per_judgment,
        },
        "implementation_sha256": _implementation_fingerprints(),
    }


def _check_resume_identity(stored: Any, current: dict) -> None:
    if not isinstance(stored, dict) or stored.get("schema") != current.get("schema"):
        raise ValueError(
            f"{CONFIG_FILE} is missing or does not match the expected run config schema"
        )
    keys = set(stored) | set(current)
    changed = sorted(
        key for key in keys if key != "created_utc" and stored.get(key) != current.get(key)
    )
    if changed:
        raise ValueError(
            "run configuration changed since the previous run; refusing to mix runs "
            f"(changed: {', '.join(changed)}). Use a new output directory instead."
        )


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return True  # unreadable lock: conservatively assume a live process
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # permission or platform doubt: assume alive, never double-run
    return True


class _RunLock:
    """Exclusive advisory lock; stale locks from dead processes are adopted."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.held = False

    def acquire(self) -> None:
        for attempt in (0, 1):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                pid = None
                try:
                    pid = json.loads(self.path.read_text(encoding="utf-8")).get("pid")
                except (OSError, ValueError):
                    pass
                if _pid_alive(pid):
                    raise ValueError(
                        f"{LOCK_FILE} is held by pid {pid!r}; another run may be active. "
                        "Delete run.lock only if you are sure no process is using it."
                    ) from None
                self.path.unlink()
                if attempt == 1:
                    raise
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid()}, handle)
            self.held = True
            return

    def release(self) -> None:
        if self.held:
            self.held = False
            self.path.unlink(missing_ok=True)


def _classify_failure(new_outcomes: list[dict]) -> Literal["provider_error", "parse_error"]:
    """A usable-looking response that still failed means invalid content."""

    if new_outcomes and new_outcomes[-1].get("status") == "response_received":
        return "parse_error"
    return "provider_error"


def _token_totals(outcomes: list[dict]) -> dict:
    if not outcomes:
        return {"input_tokens": None, "output_tokens": None, "token_usage": "none"}
    inputs = [row.get("input_tokens") for row in outcomes]
    outputs = [row.get("output_tokens") for row in outcomes]
    complete = all(value is not None for value in inputs + outputs)
    return {
        "input_tokens": sum(v for v in inputs if v is not None),
        "output_tokens": sum(v for v in outputs if v is not None),
        "token_usage": "complete" if complete else "partial",
    }


def _build_report(
    identity: dict,
    ledger: RequestLedger,
    done: dict[tuple[str, str], Prediction],
    pilot: PreparedPilot,
    target: Path,
    *,
    status: RunStatus,
    stop_reason: str | None,
    resumed: bool,
) -> dict:
    judgments = {}
    pending = []
    for method in METHODS:
        coverage: Counter[str] = Counter()
        for item_id in sorted(pilot.inputs):
            prediction = done.get((method, item_id))
            if prediction is None:
                coverage["pending"] += 1
                pending.append(
                    {
                        "method": method,
                        "item_id": item_id,
                        "attempts_consumed": ledger.consumed_for(method, item_id),
                    }
                )
            else:
                coverage[prediction.status] += 1
        judgments[method] = dict(coverage)
    outcomes = ledger.outcomes
    seconds = [row.get("request_seconds") for row in outcomes]
    files = {}
    for name in (PREDICTIONS_FILE, REQUESTS_FILE, CONFIG_FILE, DIAGNOSTICS_FILE):
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
            "labels are post-feedback development consensus, not independent held-out gold",
            "token usage depends on the provider response; money is never estimated",
        ],
    }


def _failure_prediction(
    pilot: PreparedPilot,
    method: str,
    item_id: str,
    status: Literal["provider_error", "parse_error"],
) -> Prediction:
    return Prediction(
        item_id=item_id,
        method=method,  # type: ignore[arg-type]
        input_sha256=pilot.input_hash(item_id),
        status=status,
    )


def _default_settings(root: Path) -> Settings:
    """Load settings, preferring the project root .env over the process cwd.

    Real environment variables still take precedence over either file. Without
    this, launching the CLI from a directory other than the project root would
    silently miss the credentials file.
    """

    env_file = root / ".env"
    return Settings(_env_file=env_file) if env_file.is_file() else get_settings()


def run_pilot(
    root: Path,
    config_path: Path,
    target: Path,
    *,
    resume: bool,
    max_requests: int = 72,
    max_attempts_per_judgment: int = 2,
    settings: Settings | None = None,
) -> dict:
    """Run (or resume) real pilot judgments under an enforced request budget.

    Returns the run report; the CLI maps the status to an exit code. Raises
    ValueError for refused resumes and ProviderError for unusable credentials
    before any request is sent.
    """

    root = root.resolve()
    pilot = prepare_pilot(root, config_path)
    config_file = config_path if config_path.is_absolute() else root / config_path
    return _run(
        pilot,
        digest(config_file.read_bytes()),
        target,
        resume=resume,
        max_requests=max_requests,
        max_attempts_per_judgment=max_attempts_per_judgment,
        settings=settings if settings is not None else _default_settings(root),
    )


def _run(
    pilot: PreparedPilot,
    config_sha256: str,
    target: Path,
    *,
    resume: bool,
    max_requests: int,
    max_attempts_per_judgment: int,
    settings: Settings | None = None,
) -> dict:
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
    identity = _run_identity(
        pilot,
        config_sha256=config_sha256,
        provider=provider,
        max_requests=max_requests,
        max_attempts_per_judgment=max_attempts_per_judgment,
    )

    if target.exists():
        if not resume:
            raise ValueError(
                f"output directory already exists: {target}; pass --resume to continue it "
                "or choose a new directory"
            )
        stored_raw = (target / CONFIG_FILE).read_bytes() if (target / CONFIG_FILE).exists() else b""
        try:
            stored = read_json(stored_raw) if stored_raw else None
        except ValueError as exc:
            raise ValueError(f"{CONFIG_FILE} is corrupt; refusing to resume") from exc
        _check_resume_identity(stored, identity)
    else:
        if resume:
            raise ValueError(f"cannot resume: run directory does not exist: {target}")
        target.mkdir(parents=True, exist_ok=False)

    lock = _RunLock(target / LOCK_FILE)
    lock.acquire()
    try:
        if not resume:
            _append_jsonl(target / CONFIG_FILE, identity)
        ledger = RequestLedger(
            target / REQUESTS_FILE,
            max_requests=max_requests,
            max_attempts_per_judgment=max_attempts_per_judgment,
            prior_rows=_load_ledger_rows(target / REQUESTS_FILE),
        )
        ledger_box[0] = ledger
        done = _load_predictions(target / PREDICTIONS_FILE, pilot)

        if resume:
            # Failed judgments with attempts left are retried; their stale rows are
            # replaced atomically so the file never holds two rows for one judgment.
            # Successful rows are never rewritten, and exhausted failures stay final.
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

        async def _dispatch() -> None:
            token = request_observer.set(ledger.observe)
            failure_token = failure_observer.set(ledger.diagnose)
            try:
                stop = False
                for method in METHODS:
                    if stop:
                        break
                    for item_id in sorted(pilot.inputs):
                        key = (method, item_id)
                        if key in done:
                            continue  # recorded rows are final for this invocation
                        if ledger.consumed_for(method, item_id) >= max_attempts_per_judgment:
                            continue  # attempts were spent by an earlier interrupted run
                        if ledger.remaining <= 0:
                            state["stop_reason"] = "budget_exhausted"
                            stop = True
                            break
                        provider.begin_judgment(method, item_id)
                        outcomes_before = len(ledger.outcomes)
                        try:
                            prediction = await run_judgment(
                                method, pilot.inputs[item_id], provider, pilot
                            )
                        except BudgetExhaustedError:
                            # Spent attempts stay spent; this judgment stays pending.
                            state["stop_reason"] = "budget_exhausted"
                            stop = True
                            break
                        except JudgmentCapExceededError:
                            status = _classify_failure(ledger.outcomes[outcomes_before:])
                            prediction = _failure_prediction(pilot, method, item_id, status)
                        except ProviderAuthError:
                            prediction = _failure_prediction(
                                pilot, method, item_id, "provider_error"
                            )
                            state["stop_reason"] = "auth_error"
                            stop = True
                        except ProviderError:
                            status = _classify_failure(ledger.outcomes[outcomes_before:])
                            prediction = _failure_prediction(pilot, method, item_id, status)
                        _append_jsonl(target / PREDICTIONS_FILE, prediction.model_dump(mode="json"))
                        done[key] = prediction
                        if stop:
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

        planned = len(pilot.inputs) * len(METHODS)
        if state["stop_reason"] is not None:
            status: RunStatus = (
                "interrupted"
                if interrupted
                else "budget_exhausted"
                if state["stop_reason"] == "budget_exhausted"
                else "partial_execution"
            )
        elif len(done) == planned:
            status = "completed"
        else:
            status = "partial_execution"

        report = _build_report(
            identity,
            ledger,
            done,
            pilot,
            target,
            status=status,
            stop_reason=state["stop_reason"],
            resumed=resume,
        )
        (target / REPORT_FILE).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return report
    finally:
        lock.release()
        # Closing the client must never lose an already-written report.
        with contextlib.suppress(Exception):
            asyncio.run(provider.aclose())
