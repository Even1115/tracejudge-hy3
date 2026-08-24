"""Strict, execution-free schemas for the EvalPlus export boundary.

The objects in this module describe only validated inputs to the official
EvalPlus executor.  They intentionally contain no method that imports or runs
candidate code.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EvalPlusSample(BaseModel):
    """The minimal JSONL row accepted by the pinned EvalPlus executor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(pattern=r"^HumanEval/(?:0|[1-9][0-9]*)$")
    solution: str

    @field_validator("solution")
    @classmethod
    def validate_non_empty_utf8_solution(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("solution must contain non-whitespace source code")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("solution must be valid UTF-8 text") from None
        return value


@dataclass(frozen=True, slots=True)
class Phase1ResponseReference:
    """Stable reference to one exact source record in ``responses.jsonl``.

    ``response_record_sha256`` hashes the exact UTF-8 JSONL line bytes,
    including the final LF written by the phase-one runner.  ``code_sha256``
    hashes the decoded ``solution_trace.code`` UTF-8 bytes.
    """

    phase1_run_id: str
    problem_id: str
    invocation_id: str
    response_line_number: int
    response_record_sha256: str
    code_sha256: str


@dataclass(frozen=True, slots=True)
class Phase1SourceIdentity:
    """Reproducibility identity of the completed phase-one run."""

    run_id: str
    experiment_label: str
    manifest_sha256: str
    summary_sha256: str
    responses_sha256: str
    git_commit: str
    git_branch: str | None
    git_dirty: bool
    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class HumanEvalPlusDatasetIdentity:
    """Allowlisted identity fields from the validated pilot bundle."""

    manifest_sha256: str
    dataset_id: str
    source: str
    revision: str
    license: str
    adapter_name: str
    adapter_version: int
    source_manifest_sha256: str
    parent_manifest_sha256: str
    raw_snapshot_aggregate_sha256: str
    raw_test_jsonl_sha256: str
    problems_sha256: str
    ordered_problem_ids_sha256: str
    selection_algorithm: str
    selection_seed: int
    selected_problem_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HumanEvalPlusTaskMetadata:
    """Public per-task identity used by the container preflight.

    The prompt itself is deliberately not retained.  Its digest binds the
    public text while ``entry_point`` is derived by static AST parsing.
    """

    problem_id: str
    prompt_sha256: str
    entry_point: str

    def to_preflight_dict(self) -> dict[str, str]:
        """Return the official-container request spelling for this task."""

        return {
            "task_id": self.problem_id,
            "prompt_sha256": self.prompt_sha256,
            "entry_point": self.entry_point,
        }


@dataclass(frozen=True, slots=True)
class ValidatedSampleExport:
    """Fully validated, in-memory export ready for an isolated executor."""

    phase1: Phase1SourceIdentity
    dataset: HumanEvalPlusDatasetIdentity
    samples: tuple[EvalPlusSample, ...]
    response_references: tuple[Phase1ResponseReference, ...]
    task_metadata: tuple[HumanEvalPlusTaskMetadata, ...]
    samples_sha256: str

    def reference_for(self, problem_id: str) -> Phase1ResponseReference:
        for reference in self.response_references:
            if reference.problem_id == problem_id:
                return reference
        raise KeyError(problem_id)
