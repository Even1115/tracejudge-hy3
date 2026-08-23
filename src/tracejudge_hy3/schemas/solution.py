"""Structured, user-reviewable solver output.

This is deliberately NOT a hidden chain-of-thought: implementation_steps and the
other fields must read as an auditable design document a human could review.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ImplementationStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    content: str
    related_requirements: list[str] = Field(default_factory=list)
    expected_code_behavior: str | None = None


class SolutionTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    problem_id: str
    requirement_understanding: str
    design_summary: str
    edge_cases_considered: list[str] = Field(default_factory=list)
    implementation_steps: list[ImplementationStep] = Field(default_factory=list)
    declared_time_complexity: str | None = None
    declared_space_complexity: str | None = None
    code: str

    @model_validator(mode="after")
    def validate_step_ids(self) -> Self:
        step_ids = [step.step_id for step in self.implementation_steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("implementation step_id values must be unique")
        return self
