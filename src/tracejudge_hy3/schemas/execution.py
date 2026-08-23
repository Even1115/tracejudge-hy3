"""Sandbox execution results and AST static-analysis evidence."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TestExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    category: Literal["visible", "hidden", "challenge", "counterexample"]
    passed: bool
    actual_output: Any = None
    expected_output: Any = None
    exception_type: str | None = None
    exception_message: str | None = None
    execution_time_ms: float | None = None
    timed_out: bool = False
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    related_requirements: list[str] = Field(default_factory=list)


class ExecutionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    problem_id: str
    function_name: str
    sandbox_backend: str
    results: list[TestExecutionResult] = Field(default_factory=list)
    runtime_status: Literal["completed", "import_error", "syntax_error", "backend_error"] = (
        "completed"
    )
    setup_error: str | None = None

    @property
    def visible_results(self) -> list[TestExecutionResult]:
        return [r for r in self.results if r.category == "visible"]

    @property
    def hidden_results(self) -> list[TestExecutionResult]:
        return [r for r in self.results if r.category == "hidden"]

    @property
    def challenge_results(self) -> list[TestExecutionResult]:
        return [r for r in self.results if r.category == "challenge"]

    def all_passed(self, categories: tuple[str, ...] = ("visible", "hidden", "challenge")) -> bool:
        relevant = [r for r in self.results if r.category in categories]
        return bool(relevant) and all(r.passed for r in relevant)

    def failures(self) -> list[TestExecutionResult]:
        return [r for r in self.results if not r.passed]


class StaticEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    function_name: str | None = None
    function_start_line: int | None = None
    function_end_line: int | None = None
    parameters: list[str] = Field(default_factory=list)
    if_count: int = 0
    loop_count: int = 0
    for_loop_count: int = 0
    while_loop_count: int = 0
    input_dependent_loop_count: int = 0
    max_loop_nesting_depth: int = 0
    comparison_operators: list[str] = Field(default_factory=list)
    data_structures_used: list[str] = Field(default_factory=list)
    called_functions: list[str] = Field(default_factory=list)
    return_statement_lines: list[int] = Field(default_factory=list)
    notable_literals: list[Any] = Field(default_factory=list)
    has_empty_input_check: bool = False
    empty_input_check_lines: list[int] = Field(default_factory=list)
    ast_parse_ok: bool = True
    ast_parse_error: str | None = None
    suspicious_hardcoding: bool = False
    suspicious_hardcoding_reason: str | None = None
