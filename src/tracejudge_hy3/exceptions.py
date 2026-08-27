"""Project-wide custom exceptions.

All error paths that would otherwise fail silently or return an empty/placeholder
result must raise one of these instead.
"""

from __future__ import annotations


class TraceJudgeError(Exception):
    """Base class for all TraceJudge-Hy3 errors."""


class ConfigurationError(TraceJudgeError):
    """Raised when required configuration (e.g. env vars) is missing or invalid."""


class ProviderError(TraceJudgeError):
    """Base class for LLM provider errors."""


class ProviderAuthError(ProviderError):
    """Raised when the provider rejects credentials or credentials are missing."""


class ProviderResponseError(ProviderError):
    """Raised when a provider response cannot be parsed/validated after retries."""


class ProviderParseError(ProviderResponseError):
    """Raised when model text cannot be parsed/validated after finite retries."""


class ProviderTimeoutError(ProviderError):
    """Raised when a provider call exceeds the configured timeout after retries."""


class DatasetError(TraceJudgeError):
    """Raised for problem dataset loading/validation errors."""


class SandboxError(TraceJudgeError):
    """Base class for sandbox execution errors."""


class SandboxUnavailableError(SandboxError):
    """Raised when the requested sandbox backend is not usable (e.g. Docker missing)."""


class UnsafeExecutionError(SandboxError):
    """Raised when untrusted code would be executed without an appropriate sandbox."""


class StaticAnalysisError(TraceJudgeError):
    """Raised for unexpected static analysis failures (not syntax errors, which are captured)."""


class ParsingError(TraceJudgeError):
    """Raised when structured model output cannot be parsed into a valid schema."""
