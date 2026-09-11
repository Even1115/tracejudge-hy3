"""Strict pilot contracts: process labels and aggregate functionality stay separate."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from tracejudge_hy3.schemas.evaluation import ErrorType, FaultyLayer, ProcessAssessment
from tracejudge_hy3.schemas.location import FaultLocation
from tracejudge_hy3.schemas.solution import SolutionTrace

METHODS = ("direct_judge", "structured_judge", "full_system")
Method = Literal["direct_judge", "structured_judge", "full_system"]


def public_process_correct(reasoning: bool | None, aligned: bool | None) -> bool | None:
    """Public explanation AND alignment, without importing test outcomes."""
    if reasoning is False or aligned is False:
        return False
    if reasoning is True and aligned is True:
        return True
    return None


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_id: str | None
    code_span: str | None
    requirement_id: str | None
    description: str = Field(min_length=1)


class PilotLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str
    annotation_status: Literal["reviewed"]
    annotator: str = Field(min_length=1)
    reasoning_correct: StrictBool | None
    plan_code_aligned: StrictBool | None
    process_correct: StrictBool | None
    localization_status: Literal["supported", "ambiguous", "unknown", "not_applicable"]
    first_faulty_layer: FaultyLayer | None
    first_faulty_step: str | None
    first_faulty_location: FaultLocation | None = None
    error_type: ErrorType | None
    evidence: list[EvidenceReference]
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if not self.rationale.strip() or not self.annotator.strip():
            raise ValueError("blank annotation identity or rationale")
        if self.process_correct is not public_process_correct(
            self.reasoning_correct, self.plan_code_aligned
        ):
            raise ValueError("pilot process label conflicts with public process dimensions")
        if self.process_correct is True and (
            self.localization_status != "not_applicable"
            or self.first_faulty_layer is not None
            or self.first_faulty_step is not None
            or self.error_type is not None
            or self.first_faulty_location is not None
        ):
            raise ValueError("positive annotation contains error localization")
        if self.localization_status == "supported" and (
            self.process_correct is not False
            or self.first_faulty_layer is None
            or not self.evidence
        ):
            raise ValueError("supported localization requires error evidence and layer")
        if self.first_faulty_location is not None:
            if (
                self.first_faulty_step != self.first_faulty_location.step_id
                or self.localization_status != "supported"
            ):
                raise ValueError("structured gold location conflicts with label")
        return self


class FunctionalEvidence(BaseModel):
    """Official aggregate result, deliberately not ExecutionSummary/test cases."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    scope: Literal["evalplus_mbpp_base_and_plus", "constructed_unexecuted"] = (
        "evalplus_mbpp_base_and_plus"
    )
    source_run_id: str
    source_record_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_code_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    infrastructure_status: Literal["ok", "error", "not_executed"]
    base_status: Literal["pass", "fail", "timeout", "unavailable"]
    plus_status: Literal["pass", "fail", "timeout", "unavailable"]

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.infrastructure_status in ("error", "not_executed") and (
            self.base_status != "unavailable" or self.plus_status != "unavailable"
        ):
            raise ValueError("infrastructure errors cannot imply candidate pass/fail")
        if self.scope == "constructed_unexecuted" and self.infrastructure_status != "not_executed":
            raise ValueError("constructed evidence must stay unexecuted")
        return self

    @property
    def functional_correct(self) -> bool | None:
        if self.infrastructure_status != "ok" or "unavailable" in (
            self.base_status,
            self.plus_status,
        ):
            return None
        return self.base_status == self.plus_status == "pass"


class PublicRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirement_id: str
    content: str


class PublicTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    requirement: str
    function_signature: str
    requirements: list[PublicRequirement]


class PilotInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str
    problem: PublicTask
    solution_trace: SolutionTrace
    functional_evidence: FunctionalEvidence


class Prediction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str
    method: Method
    input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["ok", "provider_error", "parse_error"]
    assessment: ProcessAssessment | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.status == "ok") != (self.assessment is not None):
            raise ValueError("only successful predictions may contain an assessment")
        return self
