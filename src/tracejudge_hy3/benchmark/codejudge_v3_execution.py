"""Strict execution records and condition-aware judge runner for v3-A/v3-B."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, Self, runtime_checkable

from pydantic import Field, ValidationError, model_validator

from tracejudge_hy3.exceptions import ProviderError

from .codejudge_eval import CodeJudgeEvalError, CodeJudgeEvalSample
from .codejudge_v3 import CodeJudgeV3Selection
from .contracts import (
    BenchmarkDifficulty,
    BenchmarkTaskIdentity,
    JudgeStatus,
    Sha256,
    StrictFrozenModel,
    canonical_sha256,
)
from .judge_only_prompt import build_judge_only_user_prompt
from .judge_only_prompt_v4 import (
    MAX_PARSE_REPAIRS_V4,
    V4_CONDITION_SCHEMAS,
    JudgeOnlyVerdictV4Hard,
    V4Condition,
    V4Verdict,
    build_judge_only_v4_repair_prompt,
    derive_v4_label,
    judge_only_v4_system_prompt,
    normalize_v4_verdict,
)

V3_JUDGE_METHOD_ID = "tracejudge-judge-only-v4"
V3Experiment = Literal["a", "b"]

_CONDITION_DIFFICULTY = {
    "easy": BenchmarkDifficulty.EASY,
    "middle": BenchmarkDifficulty.MEDIUM,
    "hard": BenchmarkDifficulty.HARD,
}
_OUTCOME_ORDER = ("WA", "RE", "TLE")


class PlannedV3Call(StrictFrozenModel):
    """One public-input model call in the exact frozen execution order."""

    planned_call_index: int = Field(ge=1)
    raw_task_id: int = Field(ge=0)
    data_id: int = Field(ge=0)
    condition: V4Condition
    call_position: int = Field(ge=1, le=3)
    task: BenchmarkTaskIdentity
    candidate_id: str = Field(min_length=1)
    candidate_sha256: Sha256
    source_record_sha256: Sha256

    @property
    def key(self) -> tuple[int, int, str]:
        return (self.raw_task_id, self.data_id, self.condition)


class CodeJudgeV3Record(StrictFrozenModel):
    """One condition-specific result; no gold or raw response is public."""

    schema_version: Literal[1] = 1
    experiment_id: str = Field(min_length=1)
    planned_call_index: int = Field(ge=1)
    raw_task_id: int = Field(ge=0)
    data_id: int = Field(ge=0)
    condition: V4Condition
    call_position: int = Field(ge=1, le=3)
    task: BenchmarkTaskIdentity
    candidate_id: str = Field(min_length=1)
    candidate_sha256: Sha256
    method_id: Literal["tracejudge-judge-only-v4"] = V3_JUDGE_METHOD_ID
    status: JudgeStatus
    native_verdict: str | None = Field(default=None, min_length=1)
    functional_correct: bool | None = None
    execution_outcomes: tuple[Literal["WA", "RE", "TLE"], ...] | None = None
    normalized_ce: bool | None = None
    normalized_outcomes: tuple[Literal["WA", "RE", "TLE"], ...] | None = None
    derived_label: str | None = Field(default=None, pattern=r"^[A-I]$")
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    parse_repairs: int = Field(default=0, ge=0, le=MAX_PARSE_REPAIRS_V4)
    diagnostic_code: str | None = Field(default=None, min_length=1, max_length=128)
    source_judgment_sha256: Sha256

    @property
    def key(self) -> tuple[int, int, str]:
        return (self.raw_task_id, self.data_id, self.condition)

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        judgment_fields = (
            self.native_verdict,
            self.functional_correct,
            self.execution_outcomes,
            self.normalized_ce,
            self.normalized_outcomes,
            self.derived_label,
            self.confidence,
        )
        if self.status is JudgeStatus.VALID_JUDGMENT:
            if any(
                value is None
                for value in (
                    self.native_verdict,
                    self.functional_correct,
                    self.normalized_ce,
                    self.derived_label,
                )
            ):
                raise ValueError("valid v3 judgment is missing required judgment fields")
            if self.diagnostic_code is not None:
                raise ValueError("valid v3 judgment cannot carry a failure diagnostic")
            if self.condition != "hard" and self.execution_outcomes is not None:
                raise ValueError("only hard records can carry native execution_outcomes")
        else:
            if any(value is not None for value in judgment_fields):
                raise ValueError("failed v3 judgment cannot carry judgment claims")
            if self.diagnostic_code is None:
                raise ValueError("failed v3 judgment requires a safe diagnostic code")
        return self


@runtime_checkable
class V3JudgeProvider(Protocol):
    async def complete(self, *, system_prompt: str, user_prompt: str) -> str: ...


RawSink = Callable[[PlannedV3Call, str, str], None]


@dataclass(frozen=True, slots=True)
class V3ParseError(ValueError):
    diagnostic_code: str
    safe_diagnostic: str

    def __str__(self) -> str:
        return self.safe_diagnostic


def _safe_schema_diagnostic(exc: ValidationError) -> str:
    errors: list[dict[str, Any]] = []
    for item in exc.errors(include_url=False, include_input=False):
        errors.append({key: item[key] for key in ("type", "loc", "msg") if key in item})
    return json.dumps(errors, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def parse_v4_verdict(raw_text: str, condition: V4Condition) -> V4Verdict:
    """Parse one strict JSON object with the schema bound to ``condition``."""

    if not isinstance(raw_text, str) or not raw_text.strip():
        raise V3ParseError("empty_response", "empty_response")
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise V3ParseError("invalid_json", f"invalid_json_at_{exc.lineno}_{exc.colno}") from None
    if not isinstance(payload, dict):
        raise V3ParseError("non_object_json", "top_level_json_must_be_object")
    try:
        return V4_CONDITION_SCHEMAS[condition].model_validate(payload)
    except ValidationError as exc:
        raise V3ParseError("schema_validation_failed", _safe_schema_diagnostic(exc)) from None


def _raw_task_and_data(sample: CodeJudgeEvalSample) -> tuple[int, int]:
    return (
        int(sample.task.identity.task_id.split("/", 1)[1]),
        int(sample.candidate.candidate_id.rsplit("/", 1)[1]),
    )


def build_v3_call_plan(
    selection: CodeJudgeV3Selection,
    samples: tuple[CodeJudgeEvalSample, ...],
    *,
    experiment: V3Experiment,
) -> tuple[tuple[PlannedV3Call, CodeJudgeEvalSample], ...]:
    """Bind a frozen selection to local samples and materialize exact call order."""

    expected_experiment_id = (
        "codejudge-eval-v3a-functional" if experiment == "a" else "codejudge-eval-v3b-granularity"
    )
    if selection.experiment_id != expected_experiment_id:
        raise CodeJudgeEvalError("selection experiment id does not match requested experiment")
    sample_index: dict[tuple[int, int, BenchmarkDifficulty], CodeJudgeEvalSample] = {}
    for sample in samples:
        raw, data_id = _raw_task_and_data(sample)
        key = (raw, data_id, sample.task.difficulty)
        if key in sample_index:
            raise CodeJudgeEvalError(f"duplicate CodeJudge sample key: {key}")
        sample_index[key] = sample

    planned: list[tuple[PlannedV3Call, CodeJudgeEvalSample]] = []
    for entry in selection.entries:
        conditions: tuple[V4Condition, ...]
        if experiment == "a":
            if entry.call_order or entry.gold_letters:
                raise CodeJudgeEvalError("v3-A entries cannot carry B-only condition metadata")
            conditions = ("hard",)
        else:
            conditions = tuple(entry.call_order)  # type: ignore[assignment]
            if conditions not in (
                ("easy", "middle", "hard"),
                ("middle", "hard", "easy"),
                ("hard", "easy", "middle"),
            ):
                raise CodeJudgeEvalError("v3-B call_order is not a frozen Latin-square row")
        for position, condition in enumerate(conditions, start=1):
            difficulty = _CONDITION_DIFFICULTY[condition]
            sample = sample_index.get((entry.raw_task_id, entry.data_id, difficulty))
            if sample is None:
                raise CodeJudgeEvalError(
                    f"selected sample missing for {(entry.raw_task_id, entry.data_id, condition)}"
                )
            if sample.candidate.code_sha256 != entry.candidate_code_sha256:
                raise CodeJudgeEvalError("selected candidate hash differs across granularity files")
            if experiment == "a" and sample.source_record_sha256 != entry.source_record_sha256:
                raise CodeJudgeEvalError("v3-A source record hash does not match selection")
            if experiment == "b":
                gold_key = "medium" if condition == "middle" else condition
                if entry.gold_letters.get(gold_key) != sample.gold.answer_letter:
                    raise CodeJudgeEvalError(
                        "v3-B frozen gold crosswalk does not match local sample"
                    )
            call = PlannedV3Call(
                planned_call_index=len(planned) + 1,
                raw_task_id=entry.raw_task_id,
                data_id=entry.data_id,
                condition=condition,
                call_position=position,
                task=sample.task.identity,
                candidate_id=sample.candidate.candidate_id,
                candidate_sha256=sample.candidate.code_sha256,
                source_record_sha256=sample.source_record_sha256,
            )
            planned.append((call, sample))

    expected_n = 120 if experiment == "a" else 150
    if len(planned) != expected_n:
        raise CodeJudgeEvalError(
            f"v3-{experiment.upper()} plan has {len(planned)} calls; expected {expected_n}"
        )
    keys = [call.key for call, _ in planned]
    if len(keys) != len(set(keys)):
        raise CodeJudgeEvalError("v3 call plan contains duplicate condition keys")
    return tuple(planned)


def call_plan_sha256(plan: tuple[tuple[PlannedV3Call, CodeJudgeEvalSample], ...]) -> str:
    return canonical_sha256([call.model_dump(mode="json") for call, _ in plan])


def _failure_record(
    call: PlannedV3Call,
    *,
    experiment_id: str,
    status: JudgeStatus,
    diagnostic_code: str,
    source_text: str,
    parse_repairs: int,
) -> CodeJudgeV3Record:
    return CodeJudgeV3Record(
        experiment_id=experiment_id,
        **call.model_dump(mode="python", exclude={"source_record_sha256"}),
        method_id=V3_JUDGE_METHOD_ID,
        status=status,
        parse_repairs=parse_repairs,
        diagnostic_code=diagnostic_code,
        source_judgment_sha256=canonical_sha256({"raw_response": source_text}),
    )


async def judge_v3_call(
    provider: V3JudgeProvider,
    call: PlannedV3Call,
    sample: CodeJudgeEvalSample,
    *,
    experiment_id: str,
    raw_sink: RawSink | None = None,
    max_parse_repairs: int = MAX_PARSE_REPAIRS_V4,
) -> CodeJudgeV3Record:
    """Execute one condition-specific call with one bounded parse repair."""

    system_prompt = judge_only_v4_system_prompt(call.condition)
    user_prompt = build_judge_only_user_prompt(sample)
    try:
        raw_text = await provider.complete(system_prompt=system_prompt, user_prompt=user_prompt)
    except ProviderError as exc:
        safe = f"{type(exc).__name__}: {exc}"
        if raw_sink is not None:
            raw_sink(call, "provider_error", safe)
        return _failure_record(
            call,
            experiment_id=experiment_id,
            status=JudgeStatus.PROVIDER_ERROR,
            diagnostic_code=type(exc).__name__,
            source_text=safe,
            parse_repairs=0,
        )
    if raw_sink is not None:
        raw_sink(call, "initial", raw_text)

    repairs = 0
    while True:
        try:
            verdict = parse_v4_verdict(raw_text, call.condition)
        except V3ParseError as exc:
            if repairs >= max_parse_repairs:
                return _failure_record(
                    call,
                    experiment_id=experiment_id,
                    status=JudgeStatus.PARSE_ERROR,
                    diagnostic_code=exc.diagnostic_code,
                    source_text=raw_text,
                    parse_repairs=repairs,
                )
            repairs += 1
            repair_prompt = build_judge_only_v4_repair_prompt(
                sample,
                condition=call.condition,
                invalid_response=raw_text,
                safe_diagnostic=exc.safe_diagnostic,
            )
            try:
                raw_text = await provider.complete(
                    system_prompt=system_prompt,
                    user_prompt=repair_prompt,
                )
            except ProviderError as repair_exc:
                safe = f"{type(repair_exc).__name__}: {repair_exc}"
                if raw_sink is not None:
                    raw_sink(call, "repair_provider_error", safe)
                return _failure_record(
                    call,
                    experiment_id=experiment_id,
                    status=JudgeStatus.PROVIDER_ERROR,
                    diagnostic_code=type(repair_exc).__name__,
                    source_text=safe,
                    parse_repairs=repairs,
                )
            if raw_sink is not None:
                raw_sink(call, "repair", raw_text)
            continue

        normalized = normalize_v4_verdict(call.condition, verdict)
        normalized_outcomes = normalized["outcomes"]
        native_outcomes: tuple[Literal["WA", "RE", "TLE"], ...] | None = None
        if isinstance(verdict, JudgeOnlyVerdictV4Hard):
            native_outcomes = verdict.execution_outcomes
        return CodeJudgeV3Record(
            experiment_id=experiment_id,
            **call.model_dump(mode="python", exclude={"source_record_sha256"}),
            method_id=V3_JUDGE_METHOD_ID,
            status=JudgeStatus.VALID_JUDGMENT,
            native_verdict=verdict.verdict,
            functional_correct=verdict.functional_correct,
            execution_outcomes=native_outcomes,
            normalized_ce=bool(normalized["ce"]),
            normalized_outcomes=(
                tuple(outcome for outcome in _OUTCOME_ORDER if outcome in normalized_outcomes)
                if normalized_outcomes is not None
                else None
            ),
            derived_label=derive_v4_label(call.condition, verdict),
            confidence=verdict.confidence,
            parse_repairs=repairs,
            diagnostic_code=None,
            source_judgment_sha256=canonical_sha256({"raw_response": raw_text}),
        )


def validate_resume_prefix(
    records: tuple[CodeJudgeV3Record, ...],
    plan: tuple[tuple[PlannedV3Call, CodeJudgeEvalSample], ...],
    *,
    experiment_id: str,
) -> None:
    """Resume is allowed only from an exact, duplicate-free plan prefix."""

    if len(records) > len(plan):
        raise CodeJudgeEvalError("resume records exceed frozen call plan")
    keys = [record.key for record in records]
    if len(keys) != len(set(keys)):
        raise CodeJudgeEvalError("resume records contain duplicate condition keys")
    for record, (call, _) in zip(records, plan, strict=False):
        if record.experiment_id != experiment_id:
            raise CodeJudgeEvalError("resume record experiment id mismatch")
        if record.key != call.key or record.planned_call_index != call.planned_call_index:
            raise CodeJudgeEvalError("resume records are not an exact frozen-plan prefix")
        if (
            record.task != call.task
            or record.candidate_id != call.candidate_id
            or record.candidate_sha256 != call.candidate_sha256
            or record.call_position != call.call_position
        ):
            raise CodeJudgeEvalError("resume record binding differs from frozen call plan")


__all__ = [
    "CodeJudgeV3Record",
    "PlannedV3Call",
    "V3Experiment",
    "V3ParseError",
    "build_v3_call_plan",
    "call_plan_sha256",
    "judge_v3_call",
    "parse_v4_verdict",
    "validate_resume_prefix",
]
