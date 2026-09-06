"""Strict, execution-free schemas for the MBPP+ EvalPlus execution boundary.

The objects in this module describe only validated inputs to the official
EvalPlus executor.  They intentionally contain no method that imports or runs
candidate code.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MbppPlusSample(BaseModel):
    """The minimal JSONL row accepted by the pinned EvalPlus executor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(pattern=r"^Mbpp/(?:0|[1-9][0-9]*)$")
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
class MbppCandidateRecord:
    """One supplied candidate bound to a public MBPP+ task identity."""

    task_id: str
    candidate_id: str
    code_sha256: str


@dataclass(frozen=True, slots=True)
class MbppPlusDatasetIdentity:
    """Allowlisted identity fields from a validated MBPP+ selection bundle."""

    manifest_sha256: str
    dataset_id: str
    source: str
    revision: str
    release_tag: str
    license: str
    adapter_name: str
    adapter_version: int
    source_manifest_sha256: str
    parent_manifest_sha256: str
    raw_snapshot_aggregate_sha256: str
    raw_jsonl_sha256: str
    official_dataset_md5: str
    problems_sha256: str
    ordered_problem_ids_sha256: str
    selection_algorithm: str
    selection_seed: int
    selected_problem_ids: tuple[str, ...]
    selected_problem_ids_sha256: str
    excluded_problem_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MbppPlusTaskMetadata:
    """Public per-task identity used by the container preflight.

    The prompt itself is deliberately not retained.  Its digest binds the
    public official ``prompt`` text while ``entry_point`` is the published
    function name.
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
class ValidatedMbppInputs:
    """Fully validated, in-memory execution inputs ready for an executor."""

    dataset: MbppPlusDatasetIdentity
    samples: tuple[MbppPlusSample, ...]
    candidates: tuple[MbppCandidateRecord, ...]
    task_metadata: tuple[MbppPlusTaskMetadata, ...]
    samples_sha256: str
    candidates_sha256: str

    def candidate_for(self, problem_id: str) -> MbppCandidateRecord:
        for record in self.candidates:
            if record.task_id == problem_id:
                return record
        raise KeyError(problem_id)
