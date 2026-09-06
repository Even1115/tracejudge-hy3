"""Judge-only runner for the frozen CodeJudge-Eval external validation.

Every judged sample produces exactly one frozen ``BenchmarkJudgeRecord``.
Provider failures and parse failures stay in the full denominator as distinct
statuses (``PROVIDER_ERROR`` / ``PARSE_ERROR``) and never fabricate judgment
fields.  Process fields (``process_correct``, ``first_faulty_layer``,
``first_faulty_step``, ``certificate_verdict``) are always ``None``: no
Hy3-native reasoning trace exists for these dataset-provided candidates.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from tracejudge_hy3.exceptions import ProviderError

from .codejudge_eval import CodeJudgeEvalError, CodeJudgeEvalSample
from .contracts import BenchmarkJudgeRecord, JudgeStatus, canonical_sha256
from .judge_only_prompt import (
    JUDGE_ONLY_METHOD_ID,
    MAX_PARSE_REPAIRS_BUNDLE,
    JudgeOnlyVerdict,
    build_judge_only_repair_prompt,
    build_judge_only_user_prompt,
    judge_only_system_prompt,
)

# Parse-repair budget, owned by the frozen v2 prompt bundle.
MAX_PARSE_REPAIRS = MAX_PARSE_REPAIRS_BUNDLE


@runtime_checkable
class JudgeOnlyProvider(Protocol):
    """Minimal text-completion surface a judge-only provider must implement."""

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str: ...


@dataclass(frozen=True, slots=True)
class JudgeOnlyParseError(ValueError):
    diagnostic_code: str
    safe_diagnostic: str

    def __str__(self) -> str:
        return self.safe_diagnostic


def _safe_schema_diagnostic(exc: ValidationError) -> str:
    errors: list[dict[str, Any]] = []
    for item in exc.errors(include_url=False, include_input=False):
        errors.append({key: item[key] for key in ("type", "loc", "msg") if key in item})
    return json.dumps(errors, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def parse_judge_only_verdict(raw_text: str) -> JudgeOnlyVerdict:
    """Fail-closed strict-JSON parse of one judge-only response."""

    if not isinstance(raw_text, str) or not raw_text.strip():
        raise JudgeOnlyParseError("empty_response", "empty_response")
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        diagnostic = f"invalid_json_at_{exc.lineno}_{exc.colno}"
        raise JudgeOnlyParseError("invalid_json", diagnostic) from None
    if not isinstance(payload, dict):
        raise JudgeOnlyParseError("non_object_json", "top_level_json_must_be_object")
    try:
        return JudgeOnlyVerdict.model_validate(payload)
    except ValidationError as exc:
        raise JudgeOnlyParseError(
            "schema_validation_failed",
            _safe_schema_diagnostic(exc),
        ) from None


def _record(
    *,
    sample: CodeJudgeEvalSample,
    status: JudgeStatus,
    raw_text: str,
    verdict: JudgeOnlyVerdict | None = None,
) -> BenchmarkJudgeRecord:
    judgment_fields: dict[str, Any] = {}
    if status is JudgeStatus.VALID_JUDGMENT:
        if verdict is None:
            raise CodeJudgeEvalError("a valid judgment record requires a parsed verdict")
        judgment_fields = {
            "functional_correct": verdict.functional_correct,
            "normalized_error_type": verdict.error_type,
            "source_error_type": verdict.verdict,
            "confidence": verdict.confidence,
        }
    return BenchmarkJudgeRecord(
        task=sample.task.identity,
        candidate_sha256=sample.candidate.code_sha256,
        method_id=JUDGE_ONLY_METHOD_ID,
        status=status,
        process_correct=None,
        first_faulty_layer=None,
        first_faulty_step=None,
        certificate_verdict=None,
        source_judgment_sha256=canonical_sha256({"raw_response": raw_text}),
        **judgment_fields,
    )


async def judge_sample(
    provider: JudgeOnlyProvider,
    sample: CodeJudgeEvalSample,
    *,
    max_parse_repairs: int = MAX_PARSE_REPAIRS,
) -> BenchmarkJudgeRecord:
    """Judge one sample; provider and parse failures stay as distinct records."""

    system_prompt = judge_only_system_prompt()
    user_prompt = build_judge_only_user_prompt(sample)
    try:
        raw_text = await provider.complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
    except ProviderError as exc:
        return _record(
            sample=sample,
            status=JudgeStatus.PROVIDER_ERROR,
            raw_text=f"{type(exc).__name__}: {exc}",
        )

    for attempt in range(max_parse_repairs + 1):
        try:
            verdict = parse_judge_only_verdict(raw_text)
        except JudgeOnlyParseError as exc:
            if attempt >= max_parse_repairs:
                return _record(
                    sample=sample,
                    status=JudgeStatus.PARSE_ERROR,
                    raw_text=exc.safe_diagnostic,
                )
            repair_prompt = build_judge_only_repair_prompt(
                sample,
                invalid_response=raw_text,
                safe_diagnostic=exc.safe_diagnostic,
            )
            try:
                raw_text = await provider.complete(
                    system_prompt=system_prompt,
                    user_prompt=repair_prompt,
                )
            except ProviderError as repair_exc:
                return _record(
                    sample=sample,
                    status=JudgeStatus.PROVIDER_ERROR,
                    raw_text=f"{type(repair_exc).__name__}: {repair_exc}",
                )
        else:
            return _record(
                sample=sample,
                status=JudgeStatus.VALID_JUDGMENT,
                raw_text=raw_text,
                verdict=verdict,
            )
    raise CodeJudgeEvalError("unreachable judge-only parse loop state")


async def judge_samples(
    provider: JudgeOnlyProvider,
    samples: tuple[CodeJudgeEvalSample, ...],
    *,
    max_parse_repairs: int = MAX_PARSE_REPAIRS,
) -> tuple[BenchmarkJudgeRecord, ...]:
    """Judge samples sequentially, preserving order and every failure."""

    records: list[BenchmarkJudgeRecord] = []
    for sample in samples:
        records.append(await judge_sample(provider, sample, max_parse_repairs=max_parse_repairs))
    return tuple(records)
