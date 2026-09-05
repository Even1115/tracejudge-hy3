"""CodeJudge-Eval judge-only adapter for the frozen cross-benchmark contract v1.

Verified provenance (checked 2026-09-04):

- Paper: "CodeJudge-Eval: Can Large Language Models be Good Judges in Code
  Understanding?" (arXiv:2408.10718, COLING 2025).
- Code repository: https://github.com/CodeLLM-Research/CodeJudge-Eval
  (MIT license, HEAD c168cd2702c3da3b7c8d08ec7a572e7d9f7f1fbf, 2024-12-03;
  the repository ships README/LICENSE only, no data files).
- Data: https://huggingface.co/datasets/CodeResearch/CodeJudge-Eval
  revision 6541deb0c42e56ec1300d94d4e9ae7b3b4cc764a (2024-08-21), MIT license.
- Files: ``CodeJudge_Eval_{0shot,1shot}_{easy,middle,hard}.json``.  This
  adapter intentionally loads only the three ``0shot`` files.
- Record fields: ``task_id`` (problem index, shared by all candidates of one
  problem), ``statement`` (public APPS-derived problem statement), ``code``
  (candidate solution, Python), ``answer`` (gold choice letter), ``url``
  (source problem URL), ``input`` (the exact prompt shown to judged LLMs,
  including the choice list), ``source`` (generator model name), ``data_id``
  (per-file unique record id).
- Choice semantics differ per difficulty file and are pinned in
  ``_CHOICE_TABLES`` below; every record's ``input`` field is validated
  against the table of its own file so a mismatched file/label pairing fails
  closed instead of silently mis-mapping labels.

This module never exposes gold answers, generator provenance, source URLs, or
the dataset's own prompt (``input``) to the judge projection.  It does not
implement an execution adapter: CodeJudge-Eval is consumed judge-only.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import Field, field_validator, model_validator

from tracejudge_hy3.exceptions import DatasetError
from tracejudge_hy3.schemas.evaluation import ErrorType

from .contracts import (
    BenchmarkCandidate,
    BenchmarkCapability,
    BenchmarkDataset,
    BenchmarkDifficulty,
    BenchmarkTask,
    BenchmarkTaskIdentity,
    CandidateOrigin,
    EvaluationMode,
    Sha256,
    StrictFrozenModel,
    TaskInterface,
    canonical_sha256,
    ordered_ids_sha256,
    task_public_payload_sha256,
)

DATASET_ID = "codejudge-eval"
ADAPTER_ID = "codejudge-eval-judge-only"
ADAPTER_VERSION = 1

HF_REPO_ID = "CodeResearch/CodeJudge-Eval"
HF_REVISION = "6541deb0c42e56ec1300d94d4e9ae7b3b4cc764a"
GITHUB_REPO = "CodeLLM-Research/CodeJudge-Eval"
GITHUB_REVISION = "c168cd2702c3da3b7c8d08ec7a572e7d9f7f1fbf"
DATASET_LICENSE = "MIT"
SOURCE_URI = f"https://huggingface.co/datasets/{HF_REPO_ID}/tree/{HF_REVISION}"

ZERO_SHOT_FILES: dict[BenchmarkDifficulty, str] = {
    BenchmarkDifficulty.EASY: "CodeJudge_Eval_0shot_easy.json",
    BenchmarkDifficulty.MEDIUM: "CodeJudge_Eval_0shot_middle.json",
    BenchmarkDifficulty.HARD: "CodeJudge_Eval_0shot_hard.json",
}
ONE_SHOT_FILES: tuple[str, ...] = (
    "CodeJudge_Eval_1shot_easy.json",
    "CodeJudge_Eval_1shot_middle.json",
    "CodeJudge_Eval_1shot_hard.json",
)

SELECTION_SEED = 20240904
SELECTION_PER_DIFFICULTY = 30
SELECTION_ALGORITHM = (
    "codejudge_eval_v2:per_difficulty_sort(task_id,data_id);"
    "one_candidate_per_task=min_data_id;"
    "random.Random(20240904).sample(eligible_task_ids,30);"
    "disjoint_task_ids_across_difficulties;ordered=easy,medium,hard"
)

_CHOICE_LINE_PATTERN = re.compile(r"(?m)^\(([A-I])\)\. ")
_STDIN_MARKERS = ("-----Input-----", "-----Output-----", "input()", "sys.stdin")
_FUNCTION_DEF_PATTERN = re.compile(r"def ([A-Za-z_][A-Za-z0-9_]*)\(")


class GoldVerdictClass(StrEnum):
    """Dataset-level verdict semantics behind each gold choice letter."""

    AC = "AC"
    CE = "CE"
    WA = "WA"
    RE = "RE"
    TLE = "TLE"
    MIXED = "MIXED"
    NOT_AC_UNSPECIFIED = "NOT_AC_UNSPECIFIED"


class ChoiceSpec(StrictFrozenModel):
    """One gold choice letter within one difficulty file."""

    letter: str = Field(pattern=r"^[A-I]$")
    verdict_class: GoldVerdictClass
    functional_correct: bool
    normalized_error_type: ErrorType | None = None
    choice_text: str = Field(min_length=1)

    @property
    def crosswalk_unmapped(self) -> bool:
        """True when the label is real but has no reliable TraceJudge ErrorType."""

        return not self.functional_correct and self.normalized_error_type is None


def _spec(
    letter: str,
    verdict_class: GoldVerdictClass,
    normalized: ErrorType | None,
    choice_text: str,
) -> ChoiceSpec:
    return ChoiceSpec(
        letter=letter,
        verdict_class=verdict_class,
        functional_correct=verdict_class is GoldVerdictClass.AC,
        normalized_error_type=normalized,
        choice_text=choice_text,
    )


# Explicit crosswalk from CodeJudge-Eval choice letters (per difficulty file) to
# the frozen TraceJudge ErrorType taxonomy.  Only single-verdict labels with an
# unambiguous counterpart are normalized; CE (no compile-error type exists in
# the v1 taxonomy), mixed-verdict labels, and the coarse easy-file "Not AC"
# stay unmapped on purpose and are counted via ``crosswalk_unmapped``.
_CHOICE_TABLES: dict[BenchmarkDifficulty, dict[str, ChoiceSpec]] = {
    BenchmarkDifficulty.EASY: {
        spec.letter: spec
        for spec in (
            _spec("A", GoldVerdictClass.AC, None, "AC"),
            _spec("B", GoldVerdictClass.CE, None, "CE"),
            _spec("C", GoldVerdictClass.NOT_AC_UNSPECIFIED, None, "Not AC"),
        )
    },
    BenchmarkDifficulty.MEDIUM: {
        spec.letter: spec
        for spec in (
            _spec("A", GoldVerdictClass.AC, None, "AC"),
            _spec("B", GoldVerdictClass.CE, None, "CE"),
            _spec("C", GoldVerdictClass.WA, ErrorType.E03_WRONG_OUTPUT, "Not AC, only WA errors"),
            _spec(
                "D", GoldVerdictClass.RE, ErrorType.E01_RUNTIME_EXCEPTION, "Not AC, only RE errors"
            ),
            _spec(
                "E",
                GoldVerdictClass.TLE,
                ErrorType.E02_TIMEOUT_OR_RESOURCE_ERROR,
                "Not AC, only TLE errors",
            ),
            _spec("F", GoldVerdictClass.MIXED, None, "Not AC for at least two types of errors"),
        )
    },
    BenchmarkDifficulty.HARD: {
        spec.letter: spec
        for spec in (
            _spec("A", GoldVerdictClass.AC, None, "AC"),
            _spec("B", GoldVerdictClass.CE, None, "CE"),
            _spec("C", GoldVerdictClass.WA, ErrorType.E03_WRONG_OUTPUT, "Not AC, only WA errors"),
            _spec(
                "D", GoldVerdictClass.RE, ErrorType.E01_RUNTIME_EXCEPTION, "Not AC, only RE errors"
            ),
            _spec("E", GoldVerdictClass.MIXED, None, "Not AC, both WA and RE errors"),
            _spec(
                "F",
                GoldVerdictClass.TLE,
                ErrorType.E02_TIMEOUT_OR_RESOURCE_ERROR,
                "Not AC, only TLE errors",
            ),
            _spec("G", GoldVerdictClass.MIXED, None, "Not AC, both WA and TLE errors"),
            _spec("H", GoldVerdictClass.MIXED, None, "Not AC, both TLE and RE errors"),
            _spec("I", GoldVerdictClass.MIXED, None, "Not AC, all WA, RE, and TLE errors"),
        )
    },
}

DIFFICULTY_ORDER: tuple[BenchmarkDifficulty, ...] = tuple(ZERO_SHOT_FILES)


class CodeJudgeEvalError(DatasetError):
    """Raised when CodeJudge-Eval raw data fails closed validation."""


class ExcludedRecord(StrictFrozenModel):
    """A record deliberately excluded from loading, with its reason."""

    source_file: str = Field(min_length=1)
    task_id: int = Field(ge=0)
    data_id: int = Field(ge=0)
    reason: str = Field(min_length=1)


class CodeJudgeEvalRawRecord(StrictFrozenModel):
    """One raw JSON record of a 0-shot CodeJudge-Eval file."""

    task_id: int = Field(ge=0)
    statement: str = Field(min_length=1)
    code: str = Field(min_length=1)
    answer: str = Field(pattern=r"^[A-I]$")
    url: str = Field(min_length=1)
    input: str = Field(min_length=1)
    source: str = Field(min_length=1)
    data_id: int = Field(ge=0)

    @field_validator("statement", "code", "input", "source")
    @classmethod
    def validate_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("raw record text must not be blank")
        return value


class CodeJudgeEvalGold(StrictFrozenModel):
    """Gold binding for one supplied candidate; never enters the judge projection."""

    answer_letter: str = Field(pattern=r"^[A-I]$")
    functional_correct: bool
    source_error_type: str = Field(min_length=1, max_length=256)
    normalized_error_type: ErrorType | None = None
    crosswalk_unmapped: bool
    generator_source: str = Field(min_length=1)


class CodeJudgeEvalSample(StrictFrozenModel):
    """One judge-only sample: public task, supplied candidate, and bound gold."""

    task: BenchmarkTask
    candidate: BenchmarkCandidate
    gold: CodeJudgeEvalGold
    source_file: str = Field(min_length=1)
    source_file_sha256: Sha256
    source_record_sha256: Sha256

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        if self.candidate.task != self.task.identity:
            raise ValueError("candidate is not bound to the sample task identity")
        if self.candidate.origin is not CandidateOrigin.DATASET_PROVIDED:
            raise ValueError("CodeJudge-Eval candidates must be dataset-provided")
        if self.task.identity.evaluation_mode is not EvaluationMode.JUDGE_ONLY:
            raise ValueError("CodeJudge-Eval tasks are judge-only")
        if self.task.identity.dataset_id != DATASET_ID:
            raise ValueError("sample task does not belong to CodeJudge-Eval")
        expected_letters = set(_CHOICE_TABLES[_file_difficulty(self.source_file)])
        if self.gold.answer_letter not in expected_letters:
            raise ValueError("gold answer letter is not defined for the source file difficulty")
        return self


class SelectionEntry(StrictFrozenModel):
    """One selected judge-only sample, bound to task, candidate, and file hash."""

    task_id: str
    candidate_id: str
    difficulty: BenchmarkDifficulty
    source_file: str = Field(min_length=1)
    source_file_sha256: Sha256
    source_record_sha256: Sha256
    candidate_code_sha256: Sha256


class CodeJudgeEvalSelection(StrictFrozenModel):
    """Frozen 90-sample held-out selection (30 per difficulty)."""

    dataset_id: str
    dataset_revision: str = Field(min_length=1)
    seed: int
    per_difficulty: int = Field(ge=1)
    algorithm: str = Field(min_length=1)
    file_sha256: dict[str, Sha256] = Field(min_length=1)
    entries: tuple[SelectionEntry, ...] = Field(min_length=1)
    selected_task_ids: tuple[str, ...] = Field(min_length=1)
    selected_task_ids_sha256: Sha256

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        if self.selected_task_ids_sha256 != ordered_ids_sha256(self.selected_task_ids):
            raise ValueError("selected_task_ids_sha256 does not bind selected_task_ids")
        if tuple(entry.task_id for entry in self.entries) != tuple(self.selected_task_ids):
            raise ValueError("entries must follow the selected task order")
        difficulties = {entry.difficulty for entry in self.entries}
        if set(self.file_sha256) != {ZERO_SHOT_FILES[d] for d in difficulties}:
            raise ValueError("file_sha256 must cover exactly the difficulties of the entries")
        return self


def _file_difficulty(source_file: str) -> BenchmarkDifficulty:
    for difficulty, filename in ZERO_SHOT_FILES.items():
        if filename == source_file:
            return difficulty
    raise CodeJudgeEvalError(f"unsupported CodeJudge-Eval source file: {source_file}")


def choice_table(difficulty: BenchmarkDifficulty) -> dict[str, ChoiceSpec]:
    try:
        return _CHOICE_TABLES[difficulty]
    except KeyError:
        raise CodeJudgeEvalError(f"no choice table for difficulty {difficulty}") from None


def file_sha256(path: Path) -> Sha256:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_manifest_sha256(file_hashes: dict[str, Sha256]) -> Sha256:
    """Bind the exact set of 0-shot source files behind the descriptor."""

    if set(file_hashes) != set(ZERO_SHOT_FILES.values()):
        raise CodeJudgeEvalError(
            "source manifest must cover exactly the three 0-shot files; "
            "1-shot files are excluded from this integration"
        )
    return canonical_sha256(dict(sorted(file_hashes.items())))


def hash_source_directory(source: Path) -> dict[str, Sha256]:
    """Hash the three pinned 0-shot files; 1-shot files are never read."""

    hashes: dict[str, Sha256] = {}
    for filename in ZERO_SHOT_FILES.values():
        path = source / filename
        if not path.is_file():
            raise CodeJudgeEvalError(f"missing pinned source file: {filename}")
        hashes[filename] = file_sha256(path)
    return hashes


def _validate_choice_consistency(
    record: CodeJudgeEvalRawRecord, difficulty: BenchmarkDifficulty
) -> ChoiceSpec:
    table = _CHOICE_TABLES[difficulty]
    if record.answer not in table:
        raise CodeJudgeEvalError(
            f"answer letter {record.answer!r} is not defined for the "
            f"{difficulty.value} 0-shot choice table"
        )
    marker = "## Choices"
    if marker not in record.input:
        raise CodeJudgeEvalError("record input does not contain a Choices section")
    # Only the Choices section itself: problem statements may contain math
    # notation like "t(G)." that mimics a choice line.
    choice_section = record.input.split(marker, 1)[1].split("## Problem", 1)[0]
    letters = set(_CHOICE_LINE_PATTERN.findall(choice_section))
    if letters != set(table):
        raise CodeJudgeEvalError(
            f"record input choice letters {sorted(letters)} do not match the pinned "
            f"{difficulty.value} table {sorted(table)}; the source file may be "
            "mis-assigned or revised"
        )
    return table[record.answer]


def _detect_interface(
    record: CodeJudgeEvalRawRecord,
) -> tuple[TaskInterface, str | None]:
    """Infer the genuine task interface instead of forcing one.

    Stdin/stdout markers win.  Otherwise a function-style task is accepted only
    when the statement pins exactly one function name; anything ambiguous fails
    closed rather than being disguised as another interface.
    """

    haystack = f"{record.statement}\n{record.code}"
    if any(marker in haystack for marker in _STDIN_MARKERS):
        return TaskInterface.STANDARD_IO, None
    names = set(_FUNCTION_DEF_PATTERN.findall(record.statement))
    if len(names) == 1:
        return TaskInterface.FUNCTION, names.pop()
    raise CodeJudgeEvalError(
        f"cannot determine the genuine task interface for task {record.task_id} "
        f"(data_id {record.data_id}); refusing to disguise it"
    )


def build_descriptor(file_hashes: dict[str, Sha256]) -> BenchmarkDataset:
    return BenchmarkDataset(
        dataset_id=DATASET_ID,
        revision=HF_REVISION,
        license=DATASET_LICENSE,
        source_uri=SOURCE_URI,
        source_manifest_sha256=source_manifest_sha256(file_hashes),
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        task_interfaces=(TaskInterface.STANDARD_IO, TaskInterface.FUNCTION),
        evaluation_modes=(EvaluationMode.JUDGE_ONLY,),
        languages=("python",),
        capabilities=(
            BenchmarkCapability.PROVIDED_CANDIDATES,
            BenchmarkCapability.ERROR_TAXONOMY,
            BenchmarkCapability.PUBLISHED_DIFFICULTY,
        ),
    )


class CodeJudgeEvalAdapter:
    """DatasetAdapter + supplied-candidate loader for 0-shot CodeJudge-Eval."""

    def __init__(self, *, descriptor: BenchmarkDataset | None = None) -> None:
        if descriptor is not None and descriptor.dataset_id != DATASET_ID:
            raise CodeJudgeEvalError("descriptor does not belong to CodeJudge-Eval")
        self._explicit_descriptor = descriptor
        self._excluded_records: tuple[ExcludedRecord, ...] = ()

    @property
    def excluded_records(self) -> tuple[ExcludedRecord, ...]:
        """Records excluded by the last explicit ``exclude_ambiguous_interface`` load."""

        return self._excluded_records

    @property
    def descriptor(self) -> BenchmarkDataset:
        if self._explicit_descriptor is None:
            raise CodeJudgeEvalError(
                "descriptor requires source file hashes; load from a directory first"
            )
        return self._explicit_descriptor

    def load_samples(
        self, source: Path, *, exclude_ambiguous_interface: bool = False
    ) -> tuple[CodeJudgeEvalSample, ...]:
        """Load and strictly validate every record of the three 0-shot files.

        By default a record whose genuine task interface cannot be determined
        fails closed.  With ``exclude_ambiguous_interface=True`` such records
        are skipped and counted in :attr:`excluded_records` instead — the
        detection rule itself is never relaxed.
        """

        file_hashes = hash_source_directory(source)
        if self._explicit_descriptor is not None:
            expected = source_manifest_sha256(file_hashes)
            if expected != self._explicit_descriptor.source_manifest_sha256:
                raise CodeJudgeEvalError(
                    "source files do not match the pinned descriptor manifest hash"
                )
        descriptor = self._explicit_descriptor or build_descriptor(file_hashes)
        self._explicit_descriptor = descriptor

        samples: list[CodeJudgeEvalSample] = []
        exclusions: list[ExcludedRecord] = []
        tasks: dict[tuple[BenchmarkDifficulty, int], BenchmarkTask] = {}
        for difficulty in DIFFICULTY_ORDER:
            filename = ZERO_SHOT_FILES[difficulty]
            path = source / filename
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise CodeJudgeEvalError(f"{filename} is not valid JSON: {exc}") from None
            if not isinstance(payload, list):
                raise CodeJudgeEvalError(f"{filename} must contain a JSON array")
            seen_data_ids: set[int] = set()
            for raw in payload:
                record = CodeJudgeEvalRawRecord.model_validate(raw)
                if record.data_id in seen_data_ids:
                    raise CodeJudgeEvalError(
                        f"{filename} contains duplicate data_id {record.data_id}"
                    )
                seen_data_ids.add(record.data_id)
                spec = _validate_choice_consistency(record, difficulty)
                try:
                    interface, entry_point = _detect_interface(record)
                except CodeJudgeEvalError as exc:
                    if not exclude_ambiguous_interface:
                        raise
                    exclusions.append(
                        ExcludedRecord(
                            source_file=filename,
                            task_id=record.task_id,
                            data_id=record.data_id,
                            reason=str(exc),
                        )
                    )
                    continue
                key = (difficulty, record.task_id)
                task = tasks.get(key)
                if task is None:
                    task = self._build_task(
                        descriptor=descriptor,
                        record=record,
                        difficulty=difficulty,
                        source_file=filename,
                        interface=interface,
                        entry_point=entry_point,
                    )
                    tasks[key] = task
                else:
                    if task.prompt != record.statement:
                        raise CodeJudgeEvalError(
                            f"{filename} task {record.task_id} has inconsistent statements"
                        )
                    if task.identity.interface is not interface:
                        raise CodeJudgeEvalError(
                            f"{filename} task {record.task_id} has inconsistent interfaces"
                        )
                    if task.entry_point != entry_point:
                        raise CodeJudgeEvalError(
                            f"{filename} task {record.task_id} has inconsistent entry points"
                        )
                record_hash = canonical_sha256(record.model_dump(mode="json"))
                candidate = BenchmarkCandidate(
                    task=task.identity,
                    candidate_id=f"{difficulty.value}/{record.task_id}/{record.data_id}",
                    origin=CandidateOrigin.DATASET_PROVIDED,
                    code=record.code,
                    code_sha256=hashlib.sha256(record.code.encode("utf-8")).hexdigest(),
                )
                gold = CodeJudgeEvalGold(
                    answer_letter=record.answer,
                    functional_correct=spec.functional_correct,
                    source_error_type=spec.verdict_class.value,
                    normalized_error_type=spec.normalized_error_type,
                    crosswalk_unmapped=spec.crosswalk_unmapped,
                    generator_source=record.source,
                )
                samples.append(
                    CodeJudgeEvalSample(
                        task=task,
                        candidate=candidate,
                        gold=gold,
                        source_file=filename,
                        source_file_sha256=file_hashes[filename],
                        source_record_sha256=record_hash,
                    )
                )
        if not samples:
            raise CodeJudgeEvalError("no CodeJudge-Eval samples were loaded")
        self._excluded_records = tuple(exclusions)
        return tuple(samples)

    def load_tasks(self, source: Path) -> tuple[BenchmarkTask, ...]:
        samples = self.load_samples(source)
        seen: dict[str, BenchmarkTask] = {}
        for sample in samples:
            seen.setdefault(sample.task.identity.task_id, sample.task)
        return tuple(seen.values())

    def _build_task(
        self,
        *,
        descriptor: BenchmarkDataset,
        record: CodeJudgeEvalRawRecord,
        difficulty: BenchmarkDifficulty,
        source_file: str,
        interface: TaskInterface,
        entry_point: str | None,
    ) -> BenchmarkTask:
        identity = BenchmarkTaskIdentity(
            dataset_id=descriptor.dataset_id,
            dataset_revision=descriptor.revision,
            task_id=f"{difficulty.value}/{record.task_id}",
            interface=interface,
            evaluation_mode=EvaluationMode.JUDGE_ONLY,
            language="python",
        )
        title = f"CodeJudge-Eval 0-shot {difficulty.value} task {record.task_id}"
        tags = ("codejudge-eval", f"difficulty:{difficulty.value}")
        public_hash = task_public_payload_sha256(
            identity=identity,
            title=title,
            prompt=record.statement,
            entry_point=entry_point,
            difficulty=difficulty,
            tags=tags,
        )
        return BenchmarkTask(
            identity=identity,
            title=title,
            prompt=record.statement,
            entry_point=entry_point,
            difficulty=difficulty,
            tags=tags,
            source_record_sha256=canonical_sha256(record.model_dump(mode="json")),
            public_payload_sha256=public_hash,
        )


def judge_visible_projection(sample: CodeJudgeEvalSample) -> dict[str, object]:
    """The only material a judge-only prompt may ever see.

    Gold answers, choice letters, generator provenance, source URLs, the
    dataset's own prompt (``input``), and record ids are deliberately absent.
    """

    projection: dict[str, object] = {
        "interface": sample.task.identity.interface.value,
        "language": sample.task.identity.language,
        "difficulty": sample.task.difficulty.value,
        "statement": sample.task.prompt,
        "candidate_code": sample.candidate.code,
    }
    if sample.task.entry_point is not None:
        projection["entry_point"] = sample.task.entry_point
    return projection


def select_samples(
    samples: tuple[CodeJudgeEvalSample, ...],
    *,
    descriptor: BenchmarkDataset,
    seed: int = SELECTION_SEED,
    per_difficulty: int = SELECTION_PER_DIFFICULTY,
    algorithm: str = SELECTION_ALGORITHM,
) -> CodeJudgeEvalSelection:
    """Freeze the held-out selection: 30 tasks per difficulty, one candidate each.

    The three 0-shot files share the same underlying APPS task pool (the
    difficulty is the judging granularity), so raw task ids are sampled
    **without replacement across difficulties**: a single
    ``random.Random(seed)`` walks easy/medium/hard in fixed order, each
    drawing from the task ids not already used.  Each chosen task contributes
    its smallest-data_id candidate.
    """

    rng = random.Random(seed)
    entries: list[SelectionEntry] = []
    file_hashes: dict[str, Sha256] = {}
    used_task_ids: set[int] = set()
    for difficulty in DIFFICULTY_ORDER:
        pool = [sample for sample in samples if sample.task.difficulty is difficulty]
        by_task: dict[int, list[CodeJudgeEvalSample]] = {}
        for sample in pool:
            raw_task_id = int(sample.task.identity.task_id.split("/", 1)[1])
            by_task.setdefault(raw_task_id, []).append(sample)
        eligible = sorted(task_id for task_id in by_task if task_id not in used_task_ids)
        if len(eligible) < per_difficulty:
            raise CodeJudgeEvalError(
                f"difficulty {difficulty.value} has {len(eligible)} eligible tasks, "
                f"below the required {per_difficulty}"
            )
        chosen_ids = rng.sample(eligible, per_difficulty)
        used_task_ids.update(chosen_ids)
        for raw_task_id in chosen_ids:
            chosen = min(
                by_task[raw_task_id],
                key=lambda sample: int(sample.candidate.candidate_id.rsplit("/", 1)[1]),
            )
            file_hashes[chosen.source_file] = chosen.source_file_sha256
            entries.append(
                SelectionEntry(
                    task_id=chosen.task.identity.task_id,
                    candidate_id=chosen.candidate.candidate_id,
                    difficulty=difficulty,
                    source_file=chosen.source_file,
                    source_file_sha256=chosen.source_file_sha256,
                    source_record_sha256=chosen.source_record_sha256,
                    candidate_code_sha256=chosen.candidate.code_sha256,
                )
            )
    selected = tuple(entry.task_id for entry in entries)
    return CodeJudgeEvalSelection(
        dataset_id=descriptor.dataset_id,
        dataset_revision=descriptor.revision,
        seed=seed,
        per_difficulty=per_difficulty,
        algorithm=algorithm,
        file_sha256=dict(sorted(file_hashes.items())),
        entries=tuple(entries),
        selected_task_ids=selected,
        selected_task_ids_sha256=ordered_ids_sha256(selected),
    )


def verify_selection_files(selection: CodeJudgeEvalSelection, source: Path) -> None:
    """Re-hash the source files and fail closed on any tampering."""

    for filename, expected in selection.file_sha256.items():
        path = source / filename
        if not path.is_file():
            raise CodeJudgeEvalError(f"selected source file disappeared: {filename}")
        if file_sha256(path) != expected:
            raise CodeJudgeEvalError(
                f"source file {filename} hash mismatch; the frozen selection no "
                "longer binds the data on disk"
            )
