"""Generate a candidate, then delegate to the shared frozen-solution evaluator."""

from __future__ import annotations

from tracejudge_hy3.config import Settings
from tracejudge_hy3.exceptions import (
    ConfigurationError,
    SandboxUnavailableError,
    UnsafeExecutionError,
)
from tracejudge_hy3.pipeline.evaluate_solution import PipelineResult, evaluate_solution
from tracejudge_hy3.providers.base import LLMProvider
from tracejudge_hy3.sandbox.base import SandboxBackend
from tracejudge_hy3.sandbox.docker_backend import DockerSandbox
from tracejudge_hy3.sandbox.trusted_local import TrustedLocalSandbox
from tracejudge_hy3.schemas.problem import ProblemSpec


def select_backend(
    *,
    provider_name: str,
    sandbox_choice: str,
    allow_unsafe_local_exec: bool,
    settings: Settings,
) -> SandboxBackend:
    """Enforce: real (non-mock) provider code may not run via trusted-local
    unless the caller explicitly opts in with --allow-unsafe-local-exec."""

    if sandbox_choice == "trusted-local":
        if provider_name != "mock" and not allow_unsafe_local_exec:
            raise UnsafeExecutionError(
                "trusted-local sandbox refuses to run non-mock provider output "
                "without --allow-unsafe-local-exec. Use --sandbox docker instead, "
                "or pass --allow-unsafe-local-exec if you understand the risk."
            )
        return TrustedLocalSandbox(
            per_test_timeout_seconds=settings.tracejudge_test_timeout_seconds,
            allow_untrusted_code=allow_unsafe_local_exec,
        )

    if sandbox_choice == "docker":
        return DockerSandbox(
            image=settings.tracejudge_docker_image,
            memory_limit=settings.tracejudge_memory_limit,
            cpu_limit=settings.tracejudge_cpu_limit,
            per_test_timeout_seconds=settings.tracejudge_test_timeout_seconds,
        )

    raise ConfigurationError(
        f"unknown sandbox {sandbox_choice!r}; expected 'docker' or 'trusted-local'"
    )


async def run_pipeline(
    problem: ProblemSpec,
    provider: LLMProvider,
    backend: SandboxBackend,
) -> PipelineResult:
    available, reason = backend.is_available()
    if not available:
        raise SandboxUnavailableError(f"{backend.name} sandbox unavailable: {reason}")
    solution = await provider.generate_solution(problem)
    return await evaluate_solution(problem, solution, provider, backend)
