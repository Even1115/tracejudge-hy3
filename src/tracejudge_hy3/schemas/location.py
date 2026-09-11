"""Locations in the public explanation; quotes bind claims to frozen text."""

from __future__ import annotations

import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tracejudge_hy3.schemas.solution import SolutionTrace


class FaultLocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_field: Literal[
        "requirement_understanding",
        "design_summary",
        "edge_cases_considered",
        "implementation_steps",
        "declared_time_complexity",
        "declared_space_complexity",
        "code",
    ]
    quote: str = Field(min_length=1)
    step_id: str | None = None
    entry_index: int | None = Field(default=None, ge=0, strict=True)
    code_span: str | None = None

    @model_validator(mode="after")
    def check_shape(self) -> Self:
        if not self.quote.strip():
            raise ValueError("location quote cannot be whitespace")
        if (self.source_field == "implementation_steps") != (self.step_id is not None):
            raise ValueError("step_id is required only for implementation_steps locations")
        if (self.source_field == "edge_cases_considered") != (self.entry_index is not None):
            raise ValueError("entry_index is required only for edge_cases_considered locations")
        if self.code_span is not None and not re.fullmatch(
            r"L[1-9]\d*(?:-L[1-9]\d*)?", self.code_span
        ):
            raise ValueError("invalid location code span")
        return self

    def validate_against(self, solution: SolutionTrace) -> None:
        self.check_shape()
        if self.source_field == "implementation_steps":
            steps = {step.step_id: step.content for step in solution.implementation_steps}
            if self.step_id not in steps:
                raise ValueError("location references an unknown step")
            text = steps[self.step_id]
        elif self.source_field == "edge_cases_considered":
            if self.entry_index >= len(solution.edge_cases_considered):
                raise ValueError("location edge case index is out of range")
            text = solution.edge_cases_considered[self.entry_index]
        else:
            text = getattr(solution, self.source_field) or ""
        if self.quote not in text:
            raise ValueError("location quote is absent from the specified source field")
        if self.code_span is not None:
            bounds = [int(part.removeprefix("L")) for part in self.code_span.split("-")]
            start, end = bounds[0], bounds[-1]
            lines = solution.code.splitlines()
            if not 1 <= start <= end <= len(lines):
                raise ValueError("location code span is outside the frozen code")
            if self.source_field == "code" and self.quote not in "\n".join(lines[start - 1 : end]):
                raise ValueError("code quote is outside the referenced lines")
