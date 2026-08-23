"""Sandbox backend interface.

A SandboxBackend runs candidate code against a list of TestCase objects and
returns a structured ExecutionSummary. No backend here claims to be absolutely
secure -- only "basic isolation" (see docs/safety.md).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from tracejudge_hy3.schemas.execution import ExecutionSummary
from tracejudge_hy3.schemas.problem import TestCase


class SandboxBackend(ABC):
    name: str = "base"

    @abstractmethod
    def is_available(self) -> tuple[bool, str | None]:
        """Return (available, reason_if_not)."""

    @abstractmethod
    def run(
        self,
        code: str,
        function_name: str,
        test_cases: list[TestCase],
    ) -> ExecutionSummary: ...
