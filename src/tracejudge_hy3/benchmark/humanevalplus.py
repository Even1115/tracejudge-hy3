"""HumanEval+ bridge for the frozen cross-benchmark contract v1."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tracejudge_hy3.dataset.humanevalplus import (
    ADAPTER_NAME,
    ADAPTER_VERSION,
    DATASET_ID,
    DATASET_SOURCE,
    validate_humanevalplus_public_problems,
)
from tracejudge_hy3.dataset.loader import load_problems
from tracejudge_hy3.exceptions import DatasetError
from tracejudge_hy3.schemas.problem import ProblemSpec

from ._evalplus_execution import normalize_evalplus_execution_result
from .contracts import (
    BenchmarkCandidate,
    BenchmarkCapability,
    BenchmarkDataset,
    BenchmarkDifficulty,
    BenchmarkExecutionResult,
    BenchmarkTask,
    BenchmarkTaskIdentity,
    EvaluationMode,
    PublicRequirement,
    TaskInterface,
    canonical_sha256,
    task_public_payload_sha256,
)


class HumanEvalPlusBenchmarkAdapter:
    """Normalize the existing public projection and safe phase-two results."""

    def __init__(
        self,
        *,
        revision: str,
        license: str,
        source_uri: str,
        source_manifest_sha256: str,
    ) -> None:
        self._descriptor = BenchmarkDataset(
            dataset_id=DATASET_ID,
            revision=revision,
            license=license,
            source_uri=source_uri,
            source_manifest_sha256=source_manifest_sha256,
            adapter_id=ADAPTER_NAME,
            adapter_version=ADAPTER_VERSION,
            task_interfaces=(TaskInterface.FUNCTION,),
            evaluation_modes=(EvaluationMode.GENERATION,),
            languages=("python",),
            capabilities=(
                BenchmarkCapability.PUBLIC_PROMPT_GENERATION,
                BenchmarkCapability.OFFICIAL_EXECUTION,
                BenchmarkCapability.HIDDEN_TESTS,
                BenchmarkCapability.PROCESS_JUDGING,
            ),
        )

    @property
    def descriptor(self) -> BenchmarkDataset:
        return self._descriptor

    def load_tasks(self, source: Path) -> tuple[BenchmarkTask, ...]:
        problems = load_problems(source)
        validate_humanevalplus_public_problems(problems)
        return tuple(self.normalize_problem(problem) for problem in problems)

    def normalize_problem(self, problem: ProblemSpec) -> BenchmarkTask:
        if problem.source != DATASET_SOURCE:
            raise DatasetError("HumanEval+ contract adapter received a problem from another source")
        identity = BenchmarkTaskIdentity(
            dataset_id=self.descriptor.dataset_id,
            dataset_revision=self.descriptor.revision,
            task_id=problem.problem_id,
            interface=TaskInterface.FUNCTION,
            evaluation_mode=EvaluationMode.GENERATION,
            language="python",
        )
        requirements = tuple(
            PublicRequirement(requirement_id=item.requirement_id, content=item.content)
            for item in problem.requirements
        )
        difficulty = BenchmarkDifficulty(problem.difficulty)
        tags = tuple(problem.tags)
        public_hash = task_public_payload_sha256(
            identity=identity,
            title=problem.title,
            prompt=problem.requirement,
            requirements=requirements,
            entry_point=problem.function_name,
            difficulty=difficulty,
            tags=tags,
        )
        return BenchmarkTask(
            identity=identity,
            title=problem.title,
            prompt=problem.requirement,
            requirements=requirements,
            entry_point=problem.function_name,
            difficulty=difficulty,
            tags=tags,
            source_record_sha256=canonical_sha256(problem),
            public_payload_sha256=public_hash,
        )

    def normalize_execution_result(
        self,
        *,
        task: BenchmarkTask,
        candidate: BenchmarkCandidate,
        result: Mapping[str, Any],
    ) -> BenchmarkExecutionResult:
        return normalize_evalplus_execution_result(
            dataset_id=self.descriptor.dataset_id,
            executor_id="evalplus_humanevalplus",
            task=task,
            candidate=candidate,
            result=result,
        )


def candidate_from_code(
    *,
    task: BenchmarkTask,
    candidate_id: str,
    code: str,
    solution_trace_sha256: str | None = None,
    source_run_id: str | None = None,
) -> BenchmarkCandidate:
    """Create a generated HumanEval+ candidate without executing its source."""

    return BenchmarkCandidate(
        task=task.identity,
        candidate_id=candidate_id,
        origin="generated",
        code=code,
        code_sha256=hashlib.sha256(code.encode("utf-8")).hexdigest(),
        solution_trace_sha256=solution_trace_sha256,
        source_run_id=source_run_id,
    )
