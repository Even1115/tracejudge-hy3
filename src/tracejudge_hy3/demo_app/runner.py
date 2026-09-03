"""Run the real evaluation pipeline behind the recording demo page.

Two strictly separated modes:

- ``fixture``: the public self-constructed ``safe_mean`` fixture through the
  deterministic Mock provider and the trusted-local sandbox.  This is exactly
  the pipeline behind ``tracejudge demo --mock --case faulty`` -- generation is
  a repository-owned fixture, but static analysis, sandboxed test execution,
  four-layer evaluation, counterexample search, certificate building and the
  replay check below all really execute.
- ``hy3``: the real Hy3 OpenAI-compatible provider with the Docker sandbox,
  equivalent to ``tracejudge run --dataset data/sample_problems.jsonl
  --problem-id safe_mean --provider hy3 --sandbox docker``.  Credentials stay
  in server-side environment variables; failures surface as short, safe,
  honest error messages and never fall back to the Mock provider.

Everything returned to the browser is built from an explicit field allowlist:
no ``reference_code``, no absolute paths, no endpoint URLs, no API keys.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from tracejudge_hy3 import __version__
from tracejudge_hy3.config import get_settings
from tracejudge_hy3.dataset.loader import load_problem_by_id
from tracejudge_hy3.exceptions import (
    ConfigurationError,
    ProviderAuthError,
    ProviderResponseError,
    ProviderTimeoutError,
    SandboxError,
    SandboxUnavailableError,
    TraceJudgeError,
    UnsafeExecutionError,
)
from tracejudge_hy3.pipeline.runner import run_pipeline, select_backend
from tracejudge_hy3.providers.base import LLMProvider
from tracejudge_hy3.providers.hy3_openai import Hy3OpenAIProvider
from tracejudge_hy3.providers.mock import MockProvider
from tracejudge_hy3.reporting.serializer import save_result_json, timestamped_artifact_path
from tracejudge_hy3.resources import data_path
from tracejudge_hy3.sandbox.base import SandboxBackend
from tracejudge_hy3.sandbox.trusted_local import TrustedLocalSandbox
from tracejudge_hy3.schemas.problem import ProblemSpec, TestCase

DEMO_PROBLEM_ID = "safe_mean"
DEMO_MODES = ("fixture", "hy3")

_FIXTURE_NOTE = "公开 Fixture；未调用真实 Hy3"
_FIXTURE_PER_TEST_TIMEOUT_SECONDS = 5.0


class DemoModeError(TraceJudgeError):
    """Raised when the requested demo mode is unavailable or unknown."""


def _make_fixture_pipeline() -> tuple[LLMProvider, SandboxBackend]:
    provider = MockProvider(case="faulty")
    backend = TrustedLocalSandbox(per_test_timeout_seconds=_FIXTURE_PER_TEST_TIMEOUT_SECONDS)
    return provider, backend


def _make_hy3_pipeline() -> tuple[LLMProvider, SandboxBackend]:
    settings = get_settings()
    if not settings.hy3_configured():
        raise DemoModeError(
            "真实 Hy3 模式未配置：需要服务端环境变量 "
            "HY3_BASE_URL / HY3_API_KEY / HY3_MODEL（见 .env.example）。"
        )
    backend = select_backend(
        provider_name="hy3",
        sandbox_choice="docker",
        allow_unsafe_local_exec=False,
        settings=settings,
    )
    available, reason = backend.is_available()
    if not available:
        raise SandboxUnavailableError(f"docker sandbox unavailable: {reason or 'unknown'}")
    return Hy3OpenAIProvider(settings=settings), backend


def _replay_certificate(
    result: Any,
    backend: SandboxBackend,
) -> dict[str, Any]:
    """Independently re-execute the certificate's public failing input.

    This is a genuine replay through the same sandbox backend: the candidate
    code runs again on the certificate's counterexample (or, when the
    certificate rests on an already-executed hidden/challenge test, on that
    exact test case) and the failure must reproduce.
    """

    certificate = result.error_certificate
    if certificate is None:
        return {
            "applicable": False,
            "reproduced": None,
            "detail": "本次运行未产生错误证书，没有需要重放的内容。",
        }
    if certificate.verdict == "cleared":
        return {
            "applicable": False,
            "reproduced": None,
            "detail": "证书状态为 cleared：复核证据表明原疑似问题不再成立。",
        }

    problem: ProblemSpec = result.problem
    counterexample = certificate.counterexample
    if counterexample is not None:
        category = {
            "challenge_test": "challenge",
            "hidden_test": "hidden",
        }.get(counterexample.source, "challenge")
        replay_case = TestCase(
            case_id="replay_counterexample",
            args=list(counterexample.args),
            kwargs=dict(counterexample.kwargs),
            expected=counterexample.expected,
            category=category,
        )
        origin = f"最小反例（source={counterexample.source}）"
    else:
        failing_ids = {
            failure.case_id
            for failure in result.execution_result.failures()
            if failure.category in ("hidden", "challenge")
        }
        replay_case = next(
            (case for case in problem.all_test_cases() if case.case_id in failing_ids),
            None,
        )
        if replay_case is None:
            return {
                "applicable": False,
                "reproduced": None,
                "detail": "证书未绑定可重放的公开测试输入。",
            }
        origin = f"失败用例 {replay_case.case_id}"

    summary = backend.run(result.solution.code, problem.function_name, [replay_case])
    replayed = summary.results[0] if summary.results else None
    reproduced = bool(
        summary.runtime_status == "completed" and replayed is not None and not replayed.passed
    )
    if replayed is not None and replayed.exception_type:
        outcome = f"再次抛出 {replayed.exception_type}"
    elif replayed is not None:
        outcome = f"实际输出 {replayed.actual_output!r}（期望 {replayed.expected_output!r}）"
    else:
        outcome = f"沙盒状态 {summary.runtime_status}"
    return {
        "applicable": True,
        "reproduced": reproduced,
        "detail": f"对{origin}重新执行候选代码：{outcome}。",
    }


def _categorized_counts(result: Any) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for category in ("visible", "hidden", "challenge"):
        rows = [r for r in result.execution_result.results if r.category == category]
        counts[category] = {
            "total": len(rows),
            "passed": sum(1 for r in rows if r.passed),
        }
    return counts


def _public_artifact_reference(path: Path) -> str:
    """Return a relative reference without exposing an out-of-repo path."""

    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return f"external-artifact/{path.name}"


def _display_payload(
    *,
    mode: str,
    result: Any,
    provider: LLMProvider,
    backend: SandboxBackend,
    replay: dict[str, Any],
    artifact_relpath: str,
    duration_seconds: float,
) -> dict[str, Any]:
    problem: ProblemSpec = result.problem
    assessment = result.process_assessment
    certificate = result.error_certificate
    counterexample = result.counterexample
    public_config = provider.public_generation_config()
    case_args = {case.case_id: case.args for case in problem.all_test_cases()}

    return {
        "ok": True,
        "mode": mode,
        "mode_note": _FIXTURE_NOTE if mode == "fixture" else "真实 Hy3 生成；结果未经预设",
        "duration_seconds": round(duration_seconds, 2),
        "artifact_relpath": artifact_relpath,
        "metadata": {
            "tracejudge_version": __version__,
            "display_schema_version": 1,
            "mode": mode,
        },
        "problem": {
            "problem_id": problem.problem_id,
            "title": problem.title,
            "requirement": problem.requirement,
            "function_signature": problem.function_signature,
            "source": problem.source,
            "requirements": [
                {"requirement_id": item.requirement_id, "content": item.content}
                for item in problem.requirements
            ],
            "test_counts": {
                "visible": len(problem.visible_test_cases),
                "hidden": len(problem.hidden_test_cases),
                "challenge": len(problem.challenge_test_cases),
            },
        },
        "provider": {
            "name": provider.name,
            "model": public_config.get("model"),
            "sandbox": backend.name,
        },
        "solution": {
            "requirement_understanding": result.solution.requirement_understanding,
            "design_summary": result.solution.design_summary,
            "edge_cases_considered": list(result.solution.edge_cases_considered),
            "implementation_steps": [
                {
                    "step_id": step.step_id,
                    "content": step.content,
                    "related_requirements": list(step.related_requirements),
                    "expected_code_behavior": step.expected_code_behavior,
                }
                for step in result.solution.implementation_steps
            ],
            "declared_time_complexity": result.solution.declared_time_complexity,
            "declared_space_complexity": result.solution.declared_space_complexity,
            "code": result.solution.code,
        },
        "static_evidence": {
            "ast_parse_ok": result.static_evidence.ast_parse_ok,
            "if_count": result.static_evidence.if_count,
            "loop_count": result.static_evidence.loop_count,
            "has_empty_input_check": result.static_evidence.has_empty_input_check,
            "suspicious_hardcoding": result.static_evidence.suspicious_hardcoding,
            "data_structures_used": list(result.static_evidence.data_structures_used),
            "function_start_line": result.static_evidence.function_start_line,
            "function_end_line": result.static_evidence.function_end_line,
        },
        "execution": {
            "runtime_status": result.execution_result.runtime_status,
            "sandbox_backend": result.execution_result.sandbox_backend,
            "categories": _categorized_counts(result),
            "results": [
                {
                    "case_id": row.case_id,
                    "category": row.category,
                    "args": case_args.get(row.case_id),
                    "passed": row.passed,
                    "actual_output": row.actual_output,
                    "expected_output": row.expected_output,
                    "exception_type": row.exception_type,
                    "timed_out": row.timed_out,
                }
                for row in result.execution_result.results
            ],
        },
        "assessment": {
            "functional_correct": assessment.functional_correct,
            "process_correct": assessment.process_correct,
            "first_faulty_layer": assessment.first_faulty_layer,
            "first_faulty_step": assessment.first_faulty_step,
            "violated_requirement": assessment.violated_requirement,
            "error_type": assessment.error_type,
            "secondary_error_types": list(assessment.secondary_error_types),
            "code_span": assessment.code_span,
            "explanation": assessment.explanation,
            "confidence": assessment.confidence,
        },
        "counterexample": (
            {
                "args": counterexample.args,
                "kwargs": counterexample.kwargs,
                "expected": counterexample.expected,
                "candidate_output": counterexample.candidate_output,
                "candidate_exception": counterexample.candidate_exception,
                "source": counterexample.source,
                "minimized": counterexample.minimized,
            }
            if counterexample is not None
            else None
        ),
        "certificate": (
            {
                "verdict": certificate.verdict,
                "error_type": certificate.error_type,
                "violated_requirement": certificate.violated_requirement,
                "first_faulty_step": certificate.first_faulty_step,
                "first_faulty_layer": certificate.first_faulty_layer,
                "code_span": certificate.code_span,
            }
            if certificate is not None
            else None
        ),
        "replay": replay,
    }


_SAFE_ERROR_MESSAGES: tuple[tuple[type[TraceJudgeError], str], ...] = (
    (
        ProviderAuthError,
        "Hy3 未配置或认证失败：请检查服务端环境变量配置（页面前端不接触密钥）。",
    ),
    (
        ProviderTimeoutError,
        "Hy3 请求超时：已超过服务端配置的单次调用时限。",
    ),
    (
        ProviderResponseError,
        "Hy3 输出解析失败：返回内容无法通过结构化校验（含有限修复重试后）。",
    ),
    (
        SandboxUnavailableError,
        "Docker 沙盒不可用：请确认本机 Docker 已安装并正在运行。",
    ),
    (
        UnsafeExecutionError,
        "安全策略拒绝执行：真实模型代码只允许在 Docker 沙盒中运行。",
    ),
    (
        SandboxError,
        "沙盒执行失败：候选代码未产生可用的执行结果。",
    ),
    (
        ConfigurationError,
        "配置错误：演示所需的本地配置不完整。",
    ),
)


def _error_payload(mode: str, exc: BaseException) -> dict[str, Any]:
    """Map a failure to a short, safe message: fixed text plus the exception
    type name only -- never the exception message, which could carry absolute
    paths or provider-controlled text."""

    detail = None
    if isinstance(exc, DemoModeError):
        detail = str(exc)  # our own fixed, safe text
    else:
        for exc_type, message in _SAFE_ERROR_MESSAGES:
            if isinstance(exc, exc_type):
                detail = message
                break
    if detail is None:
        detail = "评估流水线失败：未预期的内部错误。"
    return {
        "ok": False,
        "mode": mode,
        "mode_note": _FIXTURE_NOTE if mode == "fixture" else "真实 Hy3 生成；结果未经预设",
        "error": detail,
        "error_type": type(exc).__name__,
    }


async def _run_pipeline_async(
    problem: ProblemSpec,
    provider: LLMProvider,
    backend: SandboxBackend,
) -> Any:
    try:
        return await run_pipeline(problem, provider, backend)
    finally:
        await provider.aclose()


def run_demo(mode: str) -> dict[str, Any]:
    """Execute one complete, real demo run and return a browser-safe payload."""

    if mode not in DEMO_MODES:
        return {
            "ok": False,
            "mode": "unknown",
            "error": "未知的运行模式。",
            "error_type": "DemoModeError",
        }
    try:
        problem = load_problem_by_id(data_path("sample_problems.jsonl"), DEMO_PROBLEM_ID)
        if mode == "fixture":
            provider, backend = _make_fixture_pipeline()
        else:
            provider, backend = _make_hy3_pipeline()

        started = time.perf_counter()
        result = asyncio.run(_run_pipeline_async(problem, provider, backend))
        duration_seconds = time.perf_counter() - started

        replay = _replay_certificate(result, backend)

        settings = get_settings()
        artifact_path = timestamped_artifact_path(settings.artifact_path, f"demo_web_{mode}")
        suffix = 2
        while artifact_path.exists() or artifact_path.is_symlink():
            artifact_path = artifact_path.with_name(f"{artifact_path.stem}_{suffix}.json")
            suffix += 1
        artifact_path = save_result_json(result, artifact_path)
        artifact_relpath = _public_artifact_reference(artifact_path)

        return _display_payload(
            mode=mode,
            result=result,
            provider=provider,
            backend=backend,
            replay=replay,
            artifact_relpath=artifact_relpath,
            duration_seconds=duration_seconds,
        )
    except TraceJudgeError as exc:
        return _error_payload(mode, exc)
    except Exception as exc:  # never leak details of unexpected failures
        return _error_payload(mode, exc)


def demo_status() -> dict[str, Any]:
    """Availability of each mode plus the public problem card for the page."""

    settings = get_settings()
    hy3_configured = settings.hy3_configured()
    docker_available = False
    if hy3_configured:
        backend = select_backend(
            provider_name="hy3",
            sandbox_choice="docker",
            allow_unsafe_local_exec=False,
            settings=settings,
        )
        docker_available, _reason = backend.is_available()

    problem = load_problem_by_id(data_path("sample_problems.jsonl"), DEMO_PROBLEM_ID)
    return {
        "app": {
            "name": "tracejudge-hy3",
            "version": __version__,
            "display_schema_version": 1,
        },
        "modes": {
            "fixture": {
                "available": True,
                "note": _FIXTURE_NOTE,
                "detail": "Mock 生成 + 真实执行后续评估、测试、反例与证书流程",
            },
            "hy3": {
                "available": hy3_configured and docker_available,
                "configured": hy3_configured,
                "docker_available": docker_available,
                "note": "真实 Hy3 + Docker 沙盒",
                "detail": (
                    "等价于 tracejudge run --problem-id safe_mean --provider hy3 --sandbox docker"
                ),
            },
        },
        "problem": {
            "problem_id": problem.problem_id,
            "title": problem.title,
            "requirement": problem.requirement,
            "function_signature": problem.function_signature,
            "requirements": [
                {"requirement_id": item.requirement_id, "content": item.content}
                for item in problem.requirements
            ],
            "test_counts": {
                "visible": len(problem.visible_test_cases),
                "hidden": len(problem.hidden_test_cases),
                "challenge": len(problem.challenge_test_cases),
            },
            "source": problem.source,
        },
    }
