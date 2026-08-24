"""Problem dataset schemas: a ProblemSpec plus its requirement clauses and test cases."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RequirementItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirement_id: str
    content: str
    verification_hint: str | None = None


class TestCase(BaseModel):
    """A single test case. Inputs are structured args/kwargs, never a raw eval()-able string."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    expected: Any
    category: Literal["visible", "hidden", "challenge"]
    related_requirements: list[str] = Field(default_factory=list)

    @property
    def expected_exception(self) -> str | None:
        """Exception name requested by the documented ``{"raises": ...}`` sentinel."""

        if (
            isinstance(self.expected, dict)
            and set(self.expected) == {"raises"}
            and isinstance(self.expected["raises"], str)
            and self.expected["raises"]
        ):
            return self.expected["raises"]
        return None


class ProblemSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    problem_id: str
    title: str
    requirement: str
    function_signature: str
    requirements: list[RequirementItem] = Field(default_factory=list)
    visible_test_cases: list[TestCase] = Field(default_factory=list)
    hidden_test_cases: list[TestCase] = Field(default_factory=list)
    challenge_test_cases: list[TestCase] = Field(default_factory=list)
    reference_code: str
    # Public benchmarks such as HumanEval+ do not publish a difficulty label.
    # ``unknown`` avoids fabricating one from private tests, solutions, or
    # subjective adapter heuristics.
    difficulty: Literal["easy", "medium", "hard", "unknown"]
    source: str
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_requirement_and_test_links(self) -> Self:
        requirement_ids = [item.requirement_id for item in self.requirements]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("requirement_id values must be unique")

        seen_case_ids: set[str] = set()
        groups = (
            ("visible", self.visible_test_cases),
            ("hidden", self.hidden_test_cases),
            ("challenge", self.challenge_test_cases),
        )
        valid_requirement_ids = set(requirement_ids)
        for expected_category, cases in groups:
            for case in cases:
                if case.category != expected_category:
                    raise ValueError(
                        f"test {case.case_id!r} is in {expected_category}_test_cases "
                        f"but declares category {case.category!r}"
                    )
                if case.case_id in seen_case_ids:
                    raise ValueError(f"duplicate test case_id {case.case_id!r}")
                seen_case_ids.add(case.case_id)
                unknown = set(case.related_requirements) - valid_requirement_ids
                if unknown:
                    raise ValueError(
                        f"test {case.case_id!r} references unknown requirement IDs: "
                        f"{sorted(unknown)}"
                    )
        return self

    @property
    def function_name(self) -> str:
        """Best-effort function name extracted from the signature, e.g. 'def foo(x):' -> 'foo'."""

        sig = self.function_signature.strip()
        if sig.startswith("def "):
            sig = sig[len("def ") :]
        name = sig.split("(", 1)[0].strip()
        return name

    def all_test_cases(self) -> list[TestCase]:
        return [*self.visible_test_cases, *self.hidden_test_cases, *self.challenge_test_cases]
