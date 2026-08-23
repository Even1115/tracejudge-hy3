"""Four-layer process-evaluation output, error taxonomy, and the error certificate."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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
    functional_correct: bool
    process_correct: bool | None = None
    first_faulty_layer: FaultyLayer | None = None
    first_faulty_step: str | None = None
    affected_steps: list[str] = Field(default_factory=list)
    violated_requirement: str | None = None
    code_span: str | None = None
    error_type: ErrorType | None = None
    secondary_error_types: list[ErrorType] = Field(default_factory=list)
    explanation: str
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


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
