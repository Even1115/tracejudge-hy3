"""Four-layer process-evaluation output, error taxonomy, and the error certificate."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tracejudge_hy3.schemas.location import FaultLocation
from tracejudge_hy3.schemas.solution import SolutionTrace

FaultyLayer = Literal[
    "requirement",
    "reasoning",
    "alignment",
    "implementation",
    "execution",
]

Verdict = Literal[
    "confirmed_bug",
    "strongly_supported",
    "unverified_suspicion",
    "cleared",
]


class ErrorType(StrEnum):
    R01_REQUIREMENT_MISREAD = "R01_REQUIREMENT_MISREAD"
    R02_CONDITION_OMISSION = "R02_CONDITION_OMISSION"
    R03_UNSUPPORTED_ASSUMPTION = "R03_UNSUPPORTED_ASSUMPTION"

    P01_ALGORITHM_ERROR = "P01_ALGORITHM_ERROR"
    P02_UNJUSTIFIED_STEP = "P02_UNJUSTIFIED_STEP"
    P03_COMPLEXITY_MISMATCH = "P03_COMPLEXITY_MISMATCH"

    A01_PLAN_CODE_MISMATCH = "A01_PLAN_CODE_MISMATCH"
    A02_UNEXPLAINED_IMPLEMENTATION = "A02_UNEXPLAINED_IMPLEMENTATION"

    C01_BOUNDARY_ERROR = "C01_BOUNDARY_ERROR"
    C02_CONTROL_FLOW_ERROR = "C02_CONTROL_FLOW_ERROR"
    C03_DATA_STRUCTURE_ERROR = "C03_DATA_STRUCTURE_ERROR"
    C04_INTERFACE_OR_FORMAT_ERROR = "C04_INTERFACE_OR_FORMAT_ERROR"
    C05_HARDCODED_SHORTCUT = "C05_HARDCODED_SHORTCUT"

    E01_RUNTIME_EXCEPTION = "E01_RUNTIME_EXCEPTION"
    E02_TIMEOUT_OR_RESOURCE_ERROR = "E02_TIMEOUT_OR_RESOURCE_ERROR"
    E03_WRONG_OUTPUT = "E03_WRONG_OUTPUT"


class ProcessAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reasoning_correct: bool | None = None
    plan_code_aligned: bool | None = None
    functional_correct: bool | None
    process_correct: bool | None = None
    first_faulty_layer: FaultyLayer | None = None
    first_faulty_step: str | None = None
    first_faulty_location: FaultLocation | None = None
    affected_steps: list[str] = Field(default_factory=list)
    violated_requirement: str | None = None
    code_span: str | None = None
    error_type: ErrorType | None = None
    secondary_error_types: list[ErrorType] = Field(default_factory=list)
    explanation: str
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def check_location(self) -> Self:
        if self.first_faulty_location is not None:
            if self.first_faulty_step != self.first_faulty_location.step_id:
                raise ValueError("first_faulty_step conflicts with structured location")
            if (
                self.error_type is None
                or self.first_faulty_layer is None
                or self.process_correct is True
            ):
                raise ValueError("an error location requires an error assessment")
        return self

    def validate_location_against(self, solution: SolutionTrace) -> None:
        # Revalidate even model_copy(update=...) objects supplied by local providers.
        self.check_location()
        if self.first_faulty_location is not None:
            self.first_faulty_location.validate_against(solution)


class Counterexample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    expected: Any = None
    reference_output: Any = None
    candidate_output: Any = None
    candidate_exception: str | None = None
    reference_exception: str | None = None
    source: Literal[
        "challenge_test",
        "hidden_test",
        "boundary_candidate",
        "differential_search",
        "minimized",
    ]
    minimized: bool = False


class ErrorCertificate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    violated_requirement: str | None = None
    first_faulty_step: str | None = None
    first_faulty_layer: FaultyLayer | None = None
    code_span: str | None = None
    error_type: ErrorType | None = None
    counterexample: Counterexample | None = None
    supporting_evidence: list[str] = Field(default_factory=list)
