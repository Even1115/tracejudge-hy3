"""Frozen, dataset-neutral contracts for cross-benchmark experiments.

Version 1 deliberately contains only disclosure-safe identities, public task
material, candidate source, aggregate execution evidence, and normalized judge
outcomes.  Dataset-specific hidden tests and raw evaluator payloads stay behind
their adapters and official execution harnesses.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol, Self, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from tracejudge_hy3.schemas.evaluation import ErrorType, FaultyLayer, Verdict

BENCHMARK_CONTRACT_VERSION = 1

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$"),
]
GitCommit = Annotated[
    str,
    StringConstraints(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"),
]


class StrictFrozenModel(BaseModel):
    """Base class used by every persisted v1 contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TaskInterface(StrEnum):
    """How submitted source interacts with the official task harness."""

    FUNCTION = "function"
    STANDARD_IO = "standard_io"
    REPOSITORY_PATCH = "repository_patch"


class EvaluationMode(StrEnum):
    """Whether the benchmark asks for generation or judges supplied code."""

    GENERATION = "generation"
    JUDGE_ONLY = "judge_only"


class BenchmarkDifficulty(StrEnum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"
    UNKNOWN = "unknown"


class BenchmarkCapability(StrEnum):
    PUBLIC_PROMPT_GENERATION = "public_prompt_generation"
    OFFICIAL_EXECUTION = "official_execution"
    HIDDEN_TESTS = "hidden_tests"
    PROVIDED_CANDIDATES = "provided_candidates"
    PROCESS_JUDGING = "process_judging"
    ERROR_TAXONOMY = "error_taxonomy"
    PUBLISHED_DIFFICULTY = "published_difficulty"


class CandidateOrigin(StrEnum):
    GENERATED = "generated"
    DATASET_PROVIDED = "dataset_provided"
    COUNTERFACTUAL = "counterfactual"


class ExecutionStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    COMPILE_ERROR = "compile_error"
    RUNTIME_ERROR = "runtime_error"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    NOT_RUN = "not_run"


class TestVisibility(StrEnum):
    PUBLIC = "public"
    HIDDEN = "hidden"


class JudgeStatus(StrEnum):
    VALID_JUDGMENT = "valid_judgment"
    PROVIDER_ERROR = "provider_error"
    PARSE_ERROR = "parse_error"
    UNSUPPORTED = "unsupported"
    SKIPPED = "skipped"


def canonical_sha256(payload: Any) -> str:
    """Hash strict canonical JSON without accepting NaN or lossy fallbacks."""

    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="json")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def ordered_ids_sha256(values: Sequence[str]) -> str:
    """Bind an ordered, duplicate-free task selection."""

    normalized = tuple(values)
    if not normalized or any(not isinstance(value, str) or not value for value in normalized):
        raise ValueError("selected task IDs must be non-empty strings")
    if len(normalized) != len(set(normalized)):
        raise ValueError("selected task IDs must be unique")
    return canonical_sha256(normalized)


class BenchmarkDataset(StrictFrozenModel):
    """Pinned public identity and capabilities of one dataset adapter."""

    schema_version: Literal[1] = 1
    dataset_id: Identifier
    revision: str = Field(min_length=1, max_length=256)
    license: str = Field(min_length=1, max_length=128)
    source_uri: str = Field(min_length=1, max_length=2048)
    source_manifest_sha256: Sha256
    adapter_id: Identifier
    adapter_version: int = Field(ge=1)
    task_interfaces: tuple[TaskInterface, ...] = Field(min_length=1)
    evaluation_modes: tuple[EvaluationMode, ...] = Field(min_length=1)
    languages: tuple[Identifier, ...] = Field(min_length=1)
    capabilities: tuple[BenchmarkCapability, ...] = ()

    @field_validator("revision", "license", "source_uri")
    @classmethod
    def validate_non_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("dataset identity text must not be blank")
        return value

    @model_validator(mode="after")
    def validate_unique_sequences(self) -> Self:
        for name, values in (
            ("task_interfaces", self.task_interfaces),
            ("evaluation_modes", self.evaluation_modes),
            ("languages", self.languages),
            ("capabilities", self.capabilities),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{name} values must be unique")
        return self


class BenchmarkTaskIdentity(StrictFrozenModel):
    schema_version: Literal[1] = 1
    dataset_id: Identifier
    dataset_revision: str = Field(min_length=1, max_length=256)
    task_id: Identifier
    interface: TaskInterface
    evaluation_mode: EvaluationMode
    language: Identifier

    @field_validator("dataset_revision")
    @classmethod
    def validate_revision(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("dataset_revision must not be blank")
        return value


class PublicRequirement(StrictFrozenModel):
    requirement_id: Identifier
    content: str = Field(min_length=1)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("requirement content must not be blank")
        return value


def _task_public_payload(
    *,
    identity: BenchmarkTaskIdentity,
    title: str,
    prompt: str,
    requirements: Sequence[PublicRequirement],
    entry_point: str | None,
    difficulty: BenchmarkDifficulty,
    tags: Sequence[str],
) -> dict[str, Any]:
    return {
        "identity": identity.model_dump(mode="json"),
        "title": title,
        "prompt": prompt,
        "requirements": [item.model_dump(mode="json") for item in requirements],
        "entry_point": entry_point,
        "difficulty": difficulty.value,
        "tags": list(tags),
    }


def task_public_payload_sha256(
    *,
    identity: BenchmarkTaskIdentity,
    title: str,
    prompt: str,
    requirements: Sequence[PublicRequirement] = (),
    entry_point: str | None = None,
    difficulty: BenchmarkDifficulty = BenchmarkDifficulty.UNKNOWN,
    tags: Sequence[str] = (),
) -> str:
    """Hash exactly the public fields an adapter may expose downstream."""

    return canonical_sha256(
        _task_public_payload(
            identity=identity,
            title=title,
            prompt=prompt,
            requirements=requirements,
            entry_point=entry_point,
            difficulty=difficulty,
            tags=tags,
        )
    )


class BenchmarkTask(StrictFrozenModel):
    """Dataset-neutral public task projection; never stores hidden tests."""

    schema_version: Literal[1] = 1
    identity: BenchmarkTaskIdentity
    title: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    requirements: tuple[PublicRequirement, ...] = ()
    entry_point: str | None = Field(default=None, min_length=1, max_length=256)
    difficulty: BenchmarkDifficulty = BenchmarkDifficulty.UNKNOWN
    tags: tuple[Identifier, ...] = ()
    source_record_sha256: Sha256
    public_payload_sha256: Sha256

    @field_validator("title", "prompt")
    @classmethod
    def validate_non_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("public task text must not be blank")
        return value

    @model_validator(mode="after")
    def validate_public_projection(self) -> Self:
        if self.identity.interface is TaskInterface.FUNCTION and not self.entry_point:
            raise ValueError("function tasks require an entry_point")
        if self.identity.interface is not TaskInterface.FUNCTION and self.entry_point is not None:
            raise ValueError("only function tasks may declare an entry_point")
        requirement_ids = [item.requirement_id for item in self.requirements]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("requirement IDs must be unique")
        if len(self.tags) != len(set(self.tags)):
            raise ValueError("task tags must be unique")
        expected = task_public_payload_sha256(
            identity=self.identity,
            title=self.title,
            prompt=self.prompt,
            requirements=self.requirements,
            entry_point=self.entry_point,
            difficulty=self.difficulty,
            tags=self.tags,
        )
        if self.public_payload_sha256 != expected:
            raise ValueError("public_payload_sha256 does not bind the public task payload")
        return self


class BenchmarkCandidate(StrictFrozenModel):
    """Candidate source plus hashes; the full trace remains a separate artifact."""

    schema_version: Literal[1] = 1
    task: BenchmarkTaskIdentity
    candidate_id: Identifier
    origin: CandidateOrigin
    code: str = Field(min_length=1)
    code_sha256: Sha256
    solution_trace_sha256: Sha256 | None = None
    source_run_id: Identifier | None = None

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("candidate code must not be blank")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("candidate code must be valid UTF-8") from None
        return value

    @model_validator(mode="after")
    def validate_code_hash(self) -> Self:
        expected = hashlib.sha256(self.code.encode("utf-8")).hexdigest()
        if self.code_sha256 != expected:
            raise ValueError("code_sha256 does not bind candidate code")
        return self


class TestGroupSummary(StrictFrozenModel):
    """Disclosure-safe aggregate for one official test group."""

    group_id: Identifier
    visibility: TestVisibility
    status: ExecutionStatus
    failed_test_count: int = Field(ge=0)
    total_test_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.total_test_count is not None and self.failed_test_count > self.total_test_count:
            raise ValueError("failed_test_count cannot exceed total_test_count")
        if self.status is ExecutionStatus.PASSED and self.failed_test_count != 0:
            raise ValueError("a passing test group cannot contain failed tests")
        if self.status is ExecutionStatus.FAILED and self.failed_test_count == 0:
            raise ValueError("a failed test group must report at least one failed test")
        return self


class BenchmarkExecutionResult(StrictFrozenModel):
    """Normalized, disclosure-safe result emitted by an official harness."""

    schema_version: Literal[1] = 1
    task: BenchmarkTaskIdentity
    candidate_sha256: Sha256
    executor_id: Identifier
    status: ExecutionStatus
    groups: tuple[TestGroupSummary, ...] = ()
    failure_kind: str | None = Field(default=None, min_length=1, max_length=256)
    duration_seconds: float | None = Field(default=None, ge=0.0)
    source_result_sha256: Sha256

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        group_ids = [group.group_id for group in self.groups]
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("execution group IDs must be unique")
        if self.status is ExecutionStatus.PASSED:
            if not self.groups or any(
                group.status is not ExecutionStatus.PASSED for group in self.groups
            ):
                raise ValueError("a passing execution requires only passing test groups")
        if self.status is ExecutionStatus.FAILED and not any(
            group.status is ExecutionStatus.FAILED for group in self.groups
        ):
            raise ValueError("a failed execution requires a failed test group")
        if self.status is ExecutionStatus.TIMEOUT and not any(
            group.status is ExecutionStatus.TIMEOUT for group in self.groups
        ):
            raise ValueError("a timed-out execution requires a timed-out test group")
        if self.status in {ExecutionStatus.INFRASTRUCTURE_ERROR, ExecutionStatus.NOT_RUN}:
            if self.groups:
                raise ValueError("non-executed outcomes cannot claim test-group evidence")
            if not self.failure_kind:
                raise ValueError("non-executed outcomes require a failure_kind")
        return self


class BenchmarkJudgeRecord(StrictFrozenModel):
    """Common aggregate boundary for process judges and judge-only datasets."""

    schema_version: Literal[1] = 1
    task: BenchmarkTaskIdentity
    candidate_sha256: Sha256
    method_id: Identifier
    status: JudgeStatus
    functional_correct: bool | None = None
    process_correct: bool | None = None
    first_faulty_layer: FaultyLayer | None = None
    first_faulty_step: str | None = Field(default=None, min_length=1, max_length=512)
    normalized_error_type: ErrorType | None = None
    source_error_type: str | None = Field(default=None, min_length=1, max_length=256)
    certificate_verdict: Verdict | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    source_judgment_sha256: Sha256

    @model_validator(mode="after")
    def validate_judgment(self) -> Self:
        judgment_fields = (
            self.functional_correct,
            self.process_correct,
            self.first_faulty_layer,
            self.first_faulty_step,
            self.normalized_error_type,
            self.source_error_type,
            self.certificate_verdict,
            self.confidence,
        )
        if self.status is JudgeStatus.VALID_JUDGMENT:
            if self.functional_correct is None:
                raise ValueError("a valid judgment requires functional_correct")
            if self.process_correct is True and any(
                value is not None
                for value in (
                    self.first_faulty_layer,
                    self.first_faulty_step,
                    self.normalized_error_type,
                    self.source_error_type,
                )
            ):
                raise ValueError("a process-correct judgment cannot declare a process fault")
        elif any(value is not None for value in judgment_fields):
            raise ValueError("non-judgment outcomes cannot contain judgment claims")
        return self


class BenchmarkExperimentManifest(StrictFrozenModel):
    """Minimal cross-dataset identity frozen before a benchmark run."""

    schema_version: Literal[1] = 1
    contract_version: Literal[1] = BENCHMARK_CONTRACT_VERSION
    experiment_id: Identifier
    dataset: BenchmarkDataset
    selected_task_ids: tuple[Identifier, ...] = Field(min_length=1)
    selected_task_ids_sha256: Sha256
    selection_algorithm: str = Field(min_length=1, max_length=256)
    generation_prompt_sha256: Sha256 | None = None
    judge_prompt_sha256: Sha256 | None = None
    git_commit: GitCommit
    git_dirty: bool
    provider: Identifier | None = None
    model: str | None = Field(default=None, min_length=1, max_length=256)
    metrics_scope: tuple[Identifier, ...] = Field(min_length=1)
    limitations: tuple[Identifier, ...] = ()

    @field_validator("selection_algorithm")
    @classmethod
    def validate_selection_algorithm(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("selection_algorithm must not be blank")
        return value

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        if self.selected_task_ids_sha256 != ordered_ids_sha256(self.selected_task_ids):
            raise ValueError("selected_task_ids_sha256 does not bind selected_task_ids")
        if len(self.metrics_scope) != len(set(self.metrics_scope)):
            raise ValueError("metrics_scope values must be unique")
        if len(self.limitations) != len(set(self.limitations)):
            raise ValueError("limitations values must be unique")
        if (self.provider is None) != (self.model is None):
            raise ValueError("provider and model must either both be set or both be omitted")
        return self


@runtime_checkable
class DatasetAdapter(Protocol):
    """Required public-task surface shared by every dataset integration."""

    @property
    def descriptor(self) -> BenchmarkDataset: ...

    def load_tasks(self, source: Path) -> tuple[BenchmarkTask, ...]: ...


@runtime_checkable
class ExecutionResultAdapter(Protocol):
    """Optional capability for datasets with an official executable harness."""

    def normalize_execution_result(
        self,
        *,
        task: BenchmarkTask,
        candidate: BenchmarkCandidate,
        result: Mapping[str, Any],
    ) -> BenchmarkExecutionResult: ...
