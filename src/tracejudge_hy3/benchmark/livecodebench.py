"""LiveCodeBench code-generation adapter for the frozen cross-benchmark contract v1.

Scope: the ``code_generation_lite`` scenario only, pinned to an exact Hugging
Face dataset revision, the ``release_v6`` release tag, and an exact checker
commit of the official LiveCodeBench repository.  Only standard-I/O problems
(no ``metadata.func_name``, empty ``starter_code``, stdin-typed public tests)
are projected as ``TaskInterface.STANDARD_IO`` + ``EvaluationMode.GENERATION``.

Boundary invariants:

- The host never decodes ``private_test_cases`` (the official loader uses
  ``pickle``; unpickling would execute data).  Hidden tests are hashed as
  opaque source bytes and never enter any contract model.
- The host never executes, imports, or compiles candidate code.  Candidate
  execution belongs exclusively to the official LiveCodeBench checker inside
  an isolated container (see ``docs/livecodebench_adapter_v1.md``); this
  module only normalizes the resulting private raw-result mapping.
- The prompt replicates the official generic stdio template (MIT, (c)
  LiveCodeBench authors) and appends the decoded *public* sample tests only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from .contracts import (
    BenchmarkCandidate,
    BenchmarkCapability,
    BenchmarkDataset,
    BenchmarkDifficulty,
    BenchmarkExecutionResult,
    BenchmarkExperimentManifest,
    BenchmarkTask,
    BenchmarkTaskIdentity,
    EvaluationMode,
    ExecutionStatus,
    TaskInterface,
    TestGroupSummary,
    TestVisibility,
    canonical_sha256,
    ordered_ids_sha256,
    task_public_payload_sha256,
)

LCB_DATASET_ID = "livecodebench/code_generation_lite"
LCB_ADAPTER_NAME = "tracejudge_livecodebench_codegen_stdio"
LCB_ADAPTER_VERSION = 1
LCB_RELEASE_TAG = "release_v6"
LCB_HF_REVISION = "0fe84c3912ea0c4d4a78037083943e8f0c4dd505"
LCB_REVISION = f"{LCB_RELEASE_TAG}@{LCB_HF_REVISION}"
LCB_SOURCE_URI = "https://huggingface.co/datasets/livecodebench/code_generation_lite"
# The HF dataset card frontmatter says only ``license: cc`` (variant
# unspecified) while the pinned loader script's DatasetInfo declares
# "MIT License" and the code repository is MIT.  Record the discrepancy
# verbatim instead of silently upgrading it.
LCB_LICENSE = "cc (HF dataset card, variant unspecified); MIT (loader script DatasetInfo)"

LCB_LOADER_SCRIPT_NAME = "code_generation_lite.py"
LCB_LOADER_SCRIPT_SHA256 = "973e37989fed0f069c8246776e918d0d96600d64a3730cd6242dd76bccf2f7bb"

LCB_CHECKER_REPO_URI = "https://github.com/LiveCodeBench/LiveCodeBench"
# The official repository has no git tags; pin the exact verified commit.
LCB_CHECKER_COMMIT = "28fef95ea8c9f7a547c8329f2cd3d32b92c1fa24"

LCB_EXECUTOR_ID = "official_livecodebench_codegen_stdio"

# Official per-test result codes in the pinned testing_util.py stdio branch:
# True = passed, -2 = wrong answer, -3 = time limit exceeded, -4 = runtime
# error (the official harness also buckets syntax errors raised inside
# compile_code here), and metadata error_code -5 = TestRunnerError, an
# official-harness failure that is infrastructure, never a candidate failure.
_OFFICIAL_WRONG_ANSWER = -2
_OFFICIAL_TIMEOUT = -3
_OFFICIAL_RUNTIME_ERROR = -4
_OFFICIAL_TEST_RUNNER_ERROR = -5

_FAILURE_KIND_BY_CODE = {
    _OFFICIAL_WRONG_ANSWER: "wrong_answer",
    _OFFICIAL_TIMEOUT: "time_limit_exceeded",
    _OFFICIAL_RUNTIME_ERROR: "runtime_error",
}
_STATUS_BY_CODE = {
    _OFFICIAL_WRONG_ANSWER: ExecutionStatus.FAILED,
    _OFFICIAL_TIMEOUT: ExecutionStatus.TIMEOUT,
    _OFFICIAL_RUNTIME_ERROR: ExecutionStatus.RUNTIME_ERROR,
}

INFRASTRUCTURE_ERROR_TYPES = frozenset(
    {
        "container_exit_error",
        "container_start_error",
        "container_timeout",
        "docker_unavailable",
        "executor_error",
        "image_mismatch",
        "invalid_raw_result",
        "missing_raw_result",
        "official_test_runner_error",
    }
)
NOT_RUN_ERROR_TYPES = frozenset({"mock_not_executed", "batch_deadline_not_started"})

_SCHEMA_FIELDS = frozenset(
    {
        "question_title",
        "question_content",
        "platform",
        "question_id",
        "contest_id",
        "contest_date",
        "starter_code",
        "difficulty",
        "public_test_cases",
        "private_test_cases",
        "metadata",
    }
)
_PLATFORMS = frozenset({"leetcode", "codeforces", "atcoder"})
_DIFFICULTY_MAP = {
    "easy": BenchmarkDifficulty.EASY,
    "medium": BenchmarkDifficulty.MEDIUM,
    "hard": BenchmarkDifficulty.HARD,
}
_PUBLIC_TEST_FIELDS = frozenset({"input", "output", "testtype"})
_STDIO_TEST_TYPE = "stdin"

_MAX_RECORD_BYTES = 128 * 1024 * 1024
_MAX_PUBLIC_TESTS_BYTES = 8 * 1024 * 1024
_MAX_PROMPT_CHARS = 1_000_000
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$")

# Official generic stdio prompt template (MIT, (c) LiveCodeBench authors),
# replicated verbatim from the pinned lcb_runner/prompts/code_generation.py.
FORMATTING_WITHOUT_STARTER_CODE = (
    "Read the inputs from stdin solve the problem and write the answer to stdout "
    "(do not directly test on the sample inputs). Enclose your code within delimiters "
    "as follows. Ensure that when the python program runs, it reads the inputs, runs "
    "the algorithm and writes output to STDOUT."
)
PROMPT_TEMPLATE_VERSION = "official-generic-stdio-plus-public-samples-v1"

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class LiveCodeBenchAdapterError(ValueError):
    """Fail-closed LiveCodeBench adapter, selection, or identity violation."""


@dataclass(frozen=True, slots=True)
class LCBFilePin:
    """Exact identity of one pinned source or checker file."""

    name: str
    sha256: str
    size_bytes: int | None = None

    def __post_init__(self) -> None:
        if not self.name or "\n" in self.name or self.name.startswith("/") or ".." in self.name:
            raise LiveCodeBenchAdapterError("pinned file name is not a safe relative path")
        if not _SHA256_RE.fullmatch(self.sha256):
            raise LiveCodeBenchAdapterError("pinned file sha256 is invalid")
        if self.size_bytes is not None and self.size_bytes <= 0:
            raise LiveCodeBenchAdapterError("pinned file size must be positive")


@dataclass(frozen=True, slots=True)
class LiveCodeBenchPin:
    """Frozen external identity of the dataset snapshot and official checker."""

    hf_revision: str
    release_tag: str
    license: str
    source_uri: str
    source_files: tuple[LCBFilePin, ...]
    loader_script_sha256: str
    checker_repo_uri: str
    checker_commit: str
    checker_files: tuple[LCBFilePin, ...]

    def __post_init__(self) -> None:
        if not _GIT_COMMIT_RE.fullmatch(self.hf_revision):
            raise LiveCodeBenchAdapterError("HF dataset revision must be an exact commit")
        if not self.release_tag.startswith("release_v"):
            raise LiveCodeBenchAdapterError("release tag must be an exact release_vN tag")
        if not _GIT_COMMIT_RE.fullmatch(self.checker_commit):
            raise LiveCodeBenchAdapterError("checker commit must be an exact commit")
        if not _SHA256_RE.fullmatch(self.loader_script_sha256):
            raise LiveCodeBenchAdapterError("loader script sha256 is invalid")
        for name, files in (
            ("source_files", self.source_files),
            ("checker_files", self.checker_files),
        ):
            if not files or len({file.name for file in files}) != len(files):
                raise LiveCodeBenchAdapterError(f"{name} must be non-empty and unique")

    @property
    def revision(self) -> str:
        return f"{self.release_tag}@{self.hf_revision}"

    def source_manifest_sha256(self) -> str:
        """Bind the complete external pin: data files, loader, and checker."""

        return canonical_sha256(
            {
                "pin": "tracejudge_livecodebench_pin_v1",
                "hf_revision": self.hf_revision,
                "release_tag": self.release_tag,
                "source_uri": self.source_uri,
                "source_files": [
                    {"name": f.name, "sha256": f.sha256, "size_bytes": f.size_bytes}
                    for f in self.source_files
                ],
                "loader_script_sha256": self.loader_script_sha256,
                "checker_repo_uri": self.checker_repo_uri,
                "checker_commit": self.checker_commit,
                "checker_files": [
                    {"name": f.name, "sha256": f.sha256, "size_bytes": f.size_bytes}
                    for f in self.checker_files
                ],
            }
        )


LCB_PIN = LiveCodeBenchPin(
    hf_revision=LCB_HF_REVISION,
    release_tag=LCB_RELEASE_TAG,
    license=LCB_LICENSE,
    source_uri=LCB_SOURCE_URI,
    source_files=(
        LCBFilePin(
            "test.jsonl",
            "2bd02b38beb48e8c46b5b9987095d999ff38cd8efc255ea5d58974317c48f63f",
            1252609773,
        ),
        LCBFilePin(
            "test2.jsonl",
            "095df7c5daf15f882c51a9deb84085cff1e073495a5dbcf95015a564d485f3a3",
            713377060,
        ),
        LCBFilePin(
            "test3.jsonl",
            "28ed26cc83363ce3f1fe2d5fad9f8393077beb1907b167a31bd3b32f80801b79",
            623360766,
        ),
        LCBFilePin(
            "test4.jsonl",
            "d711138ddaebfcf5f8ec6a4283ee677298c0f5c5d374a235af92aaf0584510da",
            1204644685,
        ),
        LCBFilePin(
            "test5.jsonl",
            "7f77571c2a6df0c2a72a3277650309f67e01e0008e18117e624633df53f81214",
            557699297,
        ),
        LCBFilePin(
            "test6.jsonl",
            "bb4c364f71921c4495a6ad15abe1a927350b720009f4933e2e71f8af0f6fd1f5",
            134303240,
        ),
    ),
    loader_script_sha256=LCB_LOADER_SCRIPT_SHA256,
    checker_repo_uri=LCB_CHECKER_REPO_URI,
    checker_commit=LCB_CHECKER_COMMIT,
    checker_files=(
        LCBFilePin(
            "lcb_runner/evaluation/testing_util.py",
            "b7cb6a8a69807bb868150a61742e25d7bb5328bbe01d514471b6ec43c9fa9ed2",
        ),
        LCBFilePin(
            "lcb_runner/evaluation/compute_code_generation_metrics.py",
            "073f388cc78bf0eb0ad35eaeb8bb509c0a3664963e5305d2be13d6b6eb6f3d21",
        ),
        LCBFilePin(
            "lcb_runner/benchmarks/code_generation.py",
            "0bc93f5a650b55d6a1b54d3db852d80ae64891ece4ff085b2e6b469eb89f8846",
        ),
        LCBFilePin(
            "lcb_runner/prompts/code_generation.py",
            "7f360dc01016c277e10c70959f46efe27b37c1d2c4ef72b077f93f348b3fd826",
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class LCB60SelectionProtocol:
    """Frozen deterministic protocol for the 60-task external cohort.

    The window starts after ``release_v2`` closes (May 2024), so ``test.jsonl``
    and ``test2.jsonl`` can never contribute; it ends at the ``release_v6``
    boundary.  Difficulty strata use the dataset's published per-problem
    difficulty; ranking is ``sha256(seed \\0 question_id)``, lowest first.
    """

    window_start: date
    window_end: date
    seed: int
    stratum_size: int
    total_count: int
    algorithm: str

    def __post_init__(self) -> None:
        if self.window_start > self.window_end:
            raise LiveCodeBenchAdapterError("selection window is inverted")
        strata = tuple(_DIFFICULTY_MAP.values())
        if self.stratum_size * len(strata) != self.total_count:
            raise LiveCodeBenchAdapterError("stratum sizes must sum to the total count")
        if self.seed < 0 or self.stratum_size <= 0:
            raise LiveCodeBenchAdapterError("selection seed/stratum size is invalid")

    @property
    def strata(self) -> tuple[BenchmarkDifficulty, ...]:
        return (
            BenchmarkDifficulty.EASY,
            BenchmarkDifficulty.MEDIUM,
            BenchmarkDifficulty.HARD,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "seed": self.seed,
            "stratum_size": self.stratum_size,
            "total_count": self.total_count,
            "strata": [stratum.value for stratum in self.strata],
        }

    def protocol_sha256(self) -> str:
        return canonical_sha256(self.as_dict())


LCB60_SELECTION_ALGORITHM = (
    "lcb60-stdio-window-2024-06-01..2025-04-30-"
    "difficulty-stratified-20-sha256(seed,NUL,question_id)-lowest-v1"
)
LCB60_PROTOCOL = LCB60SelectionProtocol(
    window_start=date(2024, 6, 1),
    window_end=date(2025, 4, 30),
    seed=20260904,
    stratum_size=20,
    total_count=60,
    algorithm=LCB60_SELECTION_ALGORITHM,
)


@dataclass(frozen=True, slots=True)
class LCBSourceRecord:
    """Private parsed source record; hidden material stays hashed, never stored."""

    question_id: str
    question_title: str
    question_content: str
    platform: str
    contest_date: date
    difficulty: BenchmarkDifficulty
    public_test_cases: tuple[Mapping[str, Any], ...]
    interface: Literal["standard_io", "call_based"]
    source_file: str
    source_record_sha256: str
    # Raw opaque payloads for the isolated container runner.  The private
    # payload is never decoded on the host (the official loader decodes it
    # with pickle); it is staged verbatim into the sandbox and nowhere else.
    public_test_cases_raw: str = ""
    private_test_cases_raw: str = ""


def _sha256_file(path: Path, *, size_limit: int | None = None) -> str:
    digest = hashlib.sha256()
    read = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            read += len(chunk)
            if size_limit is not None and read > size_limit:
                raise LiveCodeBenchAdapterError("file exceeds its pinned size limit")
            digest.update(chunk)
    return digest.hexdigest()


def _require_regular_file(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError:
        raise LiveCodeBenchAdapterError("required file is unavailable") from None
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise LiveCodeBenchAdapterError("required file must be a regular non-symlink file")
    return metadata


def verify_source_directory(source: Path, *, pin: LiveCodeBenchPin = LCB_PIN) -> None:
    """Fail closed unless every pinned dataset file matches its exact bytes."""

    directory = Path(source).expanduser()
    if not directory.is_dir() or directory.is_symlink():
        raise LiveCodeBenchAdapterError("dataset source must be a real directory")
    for file_pin in pin.source_files:
        path = directory / file_pin.name
        metadata = _require_regular_file(path)
        if file_pin.size_bytes is not None and metadata.st_size != file_pin.size_bytes:
            raise LiveCodeBenchAdapterError(f"dataset file size mismatch: {file_pin.name}")
        if _sha256_file(path) != file_pin.sha256:
            raise LiveCodeBenchAdapterError(f"dataset file hash mismatch: {file_pin.name}")
    loader = directory / LCB_LOADER_SCRIPT_NAME
    _require_regular_file(loader)
    if _sha256_file(loader) != pin.loader_script_sha256:
        raise LiveCodeBenchAdapterError("dataset loader script hash mismatch")


def parse_source_record(raw: Mapping[str, Any], *, source_file: str) -> LCBSourceRecord:
    """Validate one raw record; private test payloads stay opaque strings."""

    if not isinstance(raw, Mapping) or set(raw) != _SCHEMA_FIELDS:
        raise LiveCodeBenchAdapterError("source record schema does not match the pinned loader")
    for field in _SCHEMA_FIELDS:
        if not isinstance(raw[field], str):
            raise LiveCodeBenchAdapterError(f"source record field {field!r} must be a string")
    question_id = raw["question_id"]
    if not _TASK_ID_RE.fullmatch(question_id):
        raise LiveCodeBenchAdapterError("question_id is not a safe task identifier")
    if not raw["question_title"].strip() or not raw["question_content"].strip():
        raise LiveCodeBenchAdapterError("question title/content must not be blank")
    platform = raw["platform"]
    if platform not in _PLATFORMS:
        raise LiveCodeBenchAdapterError("source record platform is unsupported")
    difficulty = _DIFFICULTY_MAP.get(raw["difficulty"])
    if difficulty is None:
        raise LiveCodeBenchAdapterError(
            "source record difficulty is missing or unpublished; refusing to fabricate one"
        )
    try:
        contest_date = datetime.fromisoformat(raw["contest_date"]).date()
    except ValueError:
        raise LiveCodeBenchAdapterError("source record contest_date is not ISO 8601") from None
    try:
        metadata = json.loads(raw["metadata"])
    except json.JSONDecodeError:
        raise LiveCodeBenchAdapterError("source record metadata is not valid JSON") from None
    if not isinstance(metadata, dict):
        raise LiveCodeBenchAdapterError("source record metadata must be a JSON object")
    interface: Literal["standard_io", "call_based"] = (
        "call_based" if metadata.get("func_name") is not None else "standard_io"
    )
    if interface == "standard_io" and raw["starter_code"] != "":
        raise LiveCodeBenchAdapterError("standard-I/O records must not carry starter code")

    public_raw = raw["public_test_cases"]
    if len(public_raw.encode("utf-8")) > _MAX_PUBLIC_TESTS_BYTES:
        raise LiveCodeBenchAdapterError("public test cases exceed the size limit")
    try:
        public_cases = json.loads(public_raw)
    except json.JSONDecodeError:
        raise LiveCodeBenchAdapterError("public test cases are not valid JSON") from None
    # The pinned snapshot contains abc350_c with [] public samples.  Parse
    # valid source rows independently of cohort eligibility; this historical
    # out-of-window row must not prevent loading the fixed 60-task cohort.
    if not isinstance(public_cases, list):
        raise LiveCodeBenchAdapterError("public test cases must be a list")
    for case in public_cases:
        if not isinstance(case, dict) or set(case) != _PUBLIC_TEST_FIELDS:
            raise LiveCodeBenchAdapterError("public test case schema is invalid")
        if not isinstance(case["input"], str) or not isinstance(case["output"], str):
            raise LiveCodeBenchAdapterError("public test case input/output must be strings")
        if interface == "standard_io" and case["testtype"] != _STDIO_TEST_TYPE:
            raise LiveCodeBenchAdapterError("standard-I/O records must use stdin public tests")

    return LCBSourceRecord(
        question_id=question_id,
        question_title=raw["question_title"],
        question_content=raw["question_content"],
        platform=platform,
        contest_date=contest_date,
        difficulty=difficulty,
        public_test_cases=tuple(public_cases),
        interface=interface,
        source_file=source_file,
        # Binds the full raw record, including the opaque hidden-test payload,
        # without storing or decoding it.
        source_record_sha256=canonical_sha256(dict(raw)),
        public_test_cases_raw=raw["public_test_cases"],
        private_test_cases_raw=raw["private_test_cases"],
    )


def load_source_records(
    source: Path,
    *,
    pin: LiveCodeBenchPin = LCB_PIN,
    verify_hashes: bool = True,
    retain_private_task_ids: frozenset[str] | None = None,
) -> tuple[LCBSourceRecord, ...]:
    """Verify the pinned snapshot and parse every record exactly once."""

    directory = Path(source).expanduser()
    if verify_hashes:
        verify_source_directory(directory, pin=pin)
    elif pin is LCB_PIN:
        raise LiveCodeBenchAdapterError("the official pin always requires source hash verification")
    records: list[LCBSourceRecord] = []
    seen_ids: set[str] = set()
    for file_pin in pin.source_files:
        path = directory / file_pin.name
        _require_regular_file(path)
        with path.open("rb") as stream:
            for line in stream:
                if not line.strip():
                    continue
                if len(line) > _MAX_RECORD_BYTES:
                    raise LiveCodeBenchAdapterError("source record exceeds the size limit")
                try:
                    raw = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise LiveCodeBenchAdapterError(
                        "source record is not unambiguous UTF-8 JSON"
                    ) from None
                record = parse_source_record(raw, source_file=file_pin.name)
                if record.question_id in seen_ids:
                    raise LiveCodeBenchAdapterError(
                        f"duplicate question_id across snapshot: {record.question_id}"
                    )
                seen_ids.add(record.question_id)
                if (
                    retain_private_task_ids is not None
                    and record.question_id not in retain_private_task_ids
                ):
                    record = replace(record, private_test_cases_raw="")
                records.append(record)
    return tuple(records)


def build_public_prompt(record: LCBSourceRecord) -> str:
    """Replicate the official generic stdio template plus public samples only."""

    prompt = f"### Question:\n{record.question_content}\n\n"
    prompt += f"### Format: {FORMATTING_WITHOUT_STARTER_CODE}\n"
    prompt += "```python\n# YOUR CODE HERE\n```\n\n"
    prompt += "### Public Sample Tests:\n"
    for index, case in enumerate(record.public_test_cases, start=1):
        prompt += f"### Sample Input {index}\n```\n{case['input']}\n```\n"
        prompt += f"### Sample Output {index}\n```\n{case['output']}\n```\n"
    prompt += "\n### Answer: (use the provided format with backticks)\n\n"
    if len(prompt) > _MAX_PROMPT_CHARS:
        raise LiveCodeBenchAdapterError("public prompt exceeds the size limit")
    return prompt


def _selection_rank(*, seed: int, question_id: str) -> str:
    return hashlib.sha256(f"{seed}\0{question_id}".encode()).hexdigest()


def select_lcb60_task_ids(
    records: Sequence[LCBSourceRecord],
    *,
    protocol: LCB60SelectionProtocol = LCB60_PROTOCOL,
) -> tuple[str, ...]:
    """Deterministically select the ordered 60-task cohort; fail closed."""

    seen_ids: set[str] = set()
    eligible: dict[BenchmarkDifficulty, list[LCBSourceRecord]] = {
        stratum: [] for stratum in protocol.strata
    }
    for record in records:
        if record.question_id in seen_ids:
            raise LiveCodeBenchAdapterError("duplicate task ID in selection input")
        seen_ids.add(record.question_id)
        if record.interface != "standard_io":
            continue
        if not (protocol.window_start <= record.contest_date <= protocol.window_end):
            continue
        eligible[record.difficulty].append(record)

    selected: list[str] = []
    for stratum in protocol.strata:
        cohort = sorted(
            eligible[stratum],
            key=lambda record: _selection_rank(seed=protocol.seed, question_id=record.question_id),
        )
        if len(cohort) < protocol.stratum_size:
            raise LiveCodeBenchAdapterError(
                f"difficulty stratum {stratum.value} has fewer than "
                f"{protocol.stratum_size} eligible tasks; refusing to fabricate a cohort"
            )
        selected.extend(record.question_id for record in cohort[: protocol.stratum_size])
    result = tuple(selected)
    ordered_ids_sha256(result)  # enforces non-empty, unique, well-formed IDs
    return result


SELECTION_LOCK_SCHEMA_VERSION = 1
SELECTION_LOCK_KIND = "tracejudge_livecodebench_lcb60_selection_lock"


def build_selection_lock(
    records: Sequence[LCBSourceRecord],
    *,
    pin: LiveCodeBenchPin = LCB_PIN,
    protocol: LCB60SelectionProtocol = LCB60_PROTOCOL,
) -> dict[str, Any]:
    """Produce the freezable lock artifact binding cohort, protocol, and pin."""

    selected = select_lcb60_task_ids(records, protocol=protocol)
    return {
        "schema_version": SELECTION_LOCK_SCHEMA_VERSION,
        "kind": SELECTION_LOCK_KIND,
        "dataset_id": LCB_DATASET_ID,
        "revision": pin.revision,
        "source_manifest_sha256": pin.source_manifest_sha256(),
        "protocol": protocol.as_dict(),
        "protocol_sha256": protocol.protocol_sha256(),
        "selected_task_ids": list(selected),
        "selected_task_ids_sha256": ordered_ids_sha256(selected),
    }


def verify_selection_lock(
    lock: Mapping[str, Any],
    records: Sequence[LCBSourceRecord],
    *,
    pin: LiveCodeBenchPin = LCB_PIN,
    protocol: LCB60SelectionProtocol = LCB60_PROTOCOL,
) -> tuple[str, ...]:
    """Recompute the cohort and fail closed on any drift from a frozen lock."""

    if not isinstance(lock, Mapping):
        raise LiveCodeBenchAdapterError("selection lock must be a mapping")
    expected_fields = {
        "schema_version",
        "kind",
        "dataset_id",
        "revision",
        "source_manifest_sha256",
        "protocol",
        "protocol_sha256",
        "selected_task_ids",
        "selected_task_ids_sha256",
    }
    if set(lock) != expected_fields:
        raise LiveCodeBenchAdapterError("selection lock fields are invalid")
    if lock["schema_version"] != SELECTION_LOCK_SCHEMA_VERSION or lock["kind"] != (
        SELECTION_LOCK_KIND
    ):
        raise LiveCodeBenchAdapterError("selection lock schema is unsupported")
    if lock["dataset_id"] != LCB_DATASET_ID or lock["revision"] != pin.revision:
        raise LiveCodeBenchAdapterError("selection lock dataset identity does not match the pin")
    if lock["source_manifest_sha256"] != pin.source_manifest_sha256():
        raise LiveCodeBenchAdapterError("selection lock source manifest hash does not match")
    if lock["protocol"] != protocol.as_dict() or lock["protocol_sha256"] != (
        protocol.protocol_sha256()
    ):
        raise LiveCodeBenchAdapterError(
            "selection lock protocol does not match the frozen protocol"
        )
    selected = select_lcb60_task_ids(records, protocol=protocol)
    if lock["selected_task_ids"] != list(selected):
        raise LiveCodeBenchAdapterError("selection lock task IDs do not reproduce from the data")
    if lock["selected_task_ids_sha256"] != ordered_ids_sha256(selected):
        raise LiveCodeBenchAdapterError("selection lock selection hash does not match")
    return selected


def build_experiment_manifest(
    *,
    adapter: LiveCodeBenchBenchmarkAdapter,
    tasks: Sequence[BenchmarkTask],
    lock: Mapping[str, Any],
    records: Sequence[LCBSourceRecord],
    experiment_id: str,
    generation_prompt_sha256: str | None,
    git_commit: str,
    git_dirty: bool,
    provider: str | None = None,
    model: str | None = None,
    metrics_scope: Sequence[str] = ("generation", "official_execution"),
    limitations: Sequence[str] = (),
) -> BenchmarkExperimentManifest:
    """Freeze the run manifest: 60 ordered task IDs, prompt, pin, and commit."""

    selected = verify_selection_lock(lock, records, pin=adapter.pin, protocol=adapter.protocol)
    task_by_id = {task.identity.task_id: task for task in tasks}
    if len(task_by_id) != len(tuple(tasks)):
        raise LiveCodeBenchAdapterError("manifest tasks contain duplicate task IDs")
    missing = [task_id for task_id in selected if task_id not in task_by_id]
    if missing:
        raise LiveCodeBenchAdapterError("manifest tasks do not cover the locked selection")
    return BenchmarkExperimentManifest(
        experiment_id=experiment_id,
        dataset=adapter.descriptor,
        selected_task_ids=selected,
        selected_task_ids_sha256=ordered_ids_sha256(selected),
        selection_algorithm=adapter.protocol.algorithm,
        generation_prompt_sha256=generation_prompt_sha256,
        git_commit=git_commit,
        git_dirty=git_dirty,
        provider=provider,
        model=model,
        metrics_scope=tuple(metrics_scope),
        limitations=tuple(limitations),
    )


_RESULT_FIELDS = frozenset(
    {
        "question_id",
        "candidate_sha256",
        "infrastructure_status",
        "error_type",
        "compile_ok",
        "official_test_results",
        "official_error_code",
        "public_test_count",
        "duration_seconds",
        "checker_commit",
        "executor_id",
    }
)


class LiveCodeBenchBenchmarkAdapter:
    """Public projection and disclosure-safe result normalization for LCB."""

    def __init__(
        self,
        *,
        pin: LiveCodeBenchPin = LCB_PIN,
        protocol: LCB60SelectionProtocol = LCB60_PROTOCOL,
    ) -> None:
        self._pin = pin
        self._protocol = protocol
        self._descriptor = BenchmarkDataset(
            dataset_id=LCB_DATASET_ID,
            revision=pin.revision,
            license=pin.license,
            source_uri=pin.source_uri,
            source_manifest_sha256=pin.source_manifest_sha256(),
            adapter_id=LCB_ADAPTER_NAME,
            adapter_version=LCB_ADAPTER_VERSION,
            task_interfaces=(TaskInterface.STANDARD_IO,),
            evaluation_modes=(EvaluationMode.GENERATION,),
            languages=("python",),
            capabilities=(
                BenchmarkCapability.PUBLIC_PROMPT_GENERATION,
                BenchmarkCapability.OFFICIAL_EXECUTION,
                BenchmarkCapability.HIDDEN_TESTS,
                BenchmarkCapability.PROCESS_JUDGING,
                BenchmarkCapability.PUBLISHED_DIFFICULTY,
            ),
        )

    @property
    def pin(self) -> LiveCodeBenchPin:
        return self._pin

    @property
    def protocol(self) -> LCB60SelectionProtocol:
        return self._protocol

    @property
    def descriptor(self) -> BenchmarkDataset:
        return self._descriptor

    def load_tasks(self, source: Path) -> tuple[BenchmarkTask, ...]:
        return self.project_records(
            load_source_records(source, pin=self._pin, retain_private_task_ids=frozenset())
        )

    def project_records(self, records: Sequence[LCBSourceRecord]) -> tuple[BenchmarkTask, ...]:
        """Project every in-window standard-I/O record into the frozen contract."""

        tasks = [
            self.project_record(record)
            for record in records
            if record.interface == "standard_io"
            and self._protocol.window_start <= record.contest_date <= self._protocol.window_end
        ]
        task_ids = [task.identity.task_id for task in tasks]
        if len(task_ids) != len(set(task_ids)):
            raise LiveCodeBenchAdapterError("projected tasks contain duplicate task IDs")
        return tuple(sorted(tasks, key=lambda task: task.identity.task_id))

    def project_record(self, record: LCBSourceRecord) -> BenchmarkTask:
        if record.interface != "standard_io":
            raise LiveCodeBenchAdapterError("only standard-I/O records can be projected")
        identity = BenchmarkTaskIdentity(
            dataset_id=self.descriptor.dataset_id,
            dataset_revision=self.descriptor.revision,
            task_id=record.question_id,
            interface=TaskInterface.STANDARD_IO,
            evaluation_mode=EvaluationMode.GENERATION,
            language="python",
        )
        prompt = build_public_prompt(record)
        tags = (record.platform, "livecodebench", "standard_io")
        return BenchmarkTask(
            identity=identity,
            title=record.question_title,
            prompt=prompt,
            requirements=(),
            entry_point=None,
            difficulty=record.difficulty,
            tags=tags,
            source_record_sha256=record.source_record_sha256,
            public_payload_sha256=task_public_payload_sha256(
                identity=identity,
                title=record.question_title,
                prompt=prompt,
                requirements=(),
                entry_point=None,
                difficulty=record.difficulty,
                tags=tags,
            ),
        )

    def select_tasks(
        self,
        tasks: Sequence[BenchmarkTask],
        selected_task_ids: Sequence[str],
    ) -> tuple[BenchmarkTask, ...]:
        """Order projected tasks by the locked selection; fail on any gap."""

        task_by_id = {task.identity.task_id: task for task in tasks}
        if len(task_by_id) != len(tuple(tasks)):
            raise LiveCodeBenchAdapterError("projected tasks contain duplicate task IDs")
        selected = tuple(selected_task_ids)
        ordered_ids_sha256(selected)
        missing = [task_id for task_id in selected if task_id not in task_by_id]
        if missing:
            raise LiveCodeBenchAdapterError("selected task IDs are missing from the projection")
        return tuple(task_by_id[task_id] for task_id in selected)

    def normalize_execution_result(
        self,
        *,
        task: BenchmarkTask,
        candidate: BenchmarkCandidate,
        result: Mapping[str, Any],
    ) -> BenchmarkExecutionResult:
        """Normalize one private official raw result; never copy hidden data."""

        if task.identity.dataset_id != self.descriptor.dataset_id:
            raise LiveCodeBenchAdapterError("task does not belong to this dataset adapter")
        if task.identity.interface is not TaskInterface.STANDARD_IO:
            raise LiveCodeBenchAdapterError("task interface does not match this adapter")
        if candidate.task != task.identity:
            raise LiveCodeBenchAdapterError("candidate task identity does not match the task")
        if not isinstance(result, Mapping) or set(result) != _RESULT_FIELDS:
            raise LiveCodeBenchAdapterError("raw result schema is incomplete or unknown")
        if result["question_id"] != task.identity.task_id:
            raise LiveCodeBenchAdapterError("raw result task identity does not match the task")
        if result["candidate_sha256"] != candidate.code_sha256:
            raise LiveCodeBenchAdapterError(
                "raw result candidate hash does not match the candidate"
            )
        if result["checker_commit"] != self._pin.checker_commit:
            raise LiveCodeBenchAdapterError("raw result was not produced by the pinned checker")
        if result["executor_id"] != LCB_EXECUTOR_ID:
            raise LiveCodeBenchAdapterError("raw result executor identity is unsupported")

        duration = result["duration_seconds"]
        if duration is not None and (
            isinstance(duration, bool) or not isinstance(duration, int | float) or duration < 0
        ):
            raise LiveCodeBenchAdapterError("raw result duration_seconds is invalid")

        infrastructure_status = result["infrastructure_status"]
        error_type = result["error_type"]
        if error_type is not None and not isinstance(error_type, str):
            raise LiveCodeBenchAdapterError("raw result error_type must be a string or null")

        if infrastructure_status == "error":
            if error_type not in INFRASTRUCTURE_ERROR_TYPES:
                raise LiveCodeBenchAdapterError("infrastructure error_type is not allowlisted")
            status = ExecutionStatus.INFRASTRUCTURE_ERROR
            groups: tuple[TestGroupSummary, ...] = ()
            failure_kind: str | None = error_type
        elif infrastructure_status == "not_run":
            if error_type not in NOT_RUN_ERROR_TYPES:
                raise LiveCodeBenchAdapterError("not-run error_type is not allowlisted")
            status = ExecutionStatus.NOT_RUN
            groups = ()
            failure_kind = error_type
        elif infrastructure_status == "ok":
            status, groups, failure_kind = self._executed_outcome(result)
        else:
            raise LiveCodeBenchAdapterError("raw result infrastructure_status is unsupported")

        return BenchmarkExecutionResult(
            task=task.identity,
            candidate_sha256=candidate.code_sha256,
            executor_id=LCB_EXECUTOR_ID,
            status=status,
            groups=groups,
            failure_kind=failure_kind,
            duration_seconds=float(duration) if duration is not None else None,
            source_result_sha256=canonical_sha256(dict(result)),
        )

    def _executed_outcome(
        self, result: Mapping[str, Any]
    ) -> tuple[ExecutionStatus, tuple[TestGroupSummary, ...], str | None]:
        official_error_code = result["official_error_code"]
        if official_error_code is not None and (
            isinstance(official_error_code, bool) or not isinstance(official_error_code, int)
        ):
            raise LiveCodeBenchAdapterError("raw result official_error_code is invalid")
        if official_error_code == _OFFICIAL_TEST_RUNNER_ERROR:
            # The official harness itself failed; this is never candidate evidence.
            return ExecutionStatus.INFRASTRUCTURE_ERROR, (), "official_test_runner_error"

        compile_ok = result["compile_ok"]
        if not isinstance(compile_ok, bool):
            raise LiveCodeBenchAdapterError("raw result compile_ok must be boolean")
        if not compile_ok:
            # The container-side pure syntax probe replaces the official -4
            # bucket for candidates that never compiled.
            return ExecutionStatus.COMPILE_ERROR, (), "compile_error"

        raw_results = result["official_test_results"]
        if not isinstance(raw_results, list) or not raw_results:
            raise LiveCodeBenchAdapterError("raw result official_test_results are invalid")
        codes: list[Any] = []
        for item in raw_results:
            if item is True:
                codes.append(True)
            elif item is False:
                # The call-based branch can append a bare False; normalize it
                # to the official wrong-answer code.
                codes.append(_OFFICIAL_WRONG_ANSWER)
            elif (
                isinstance(item, int)
                and not isinstance(item, bool)
                and item
                in (
                    _OFFICIAL_WRONG_ANSWER,
                    _OFFICIAL_TIMEOUT,
                    _OFFICIAL_RUNTIME_ERROR,
                )
            ):
                codes.append(item)
            else:
                raise LiveCodeBenchAdapterError("raw result official_test_results are invalid")
        public_count = result["public_test_count"]
        if isinstance(public_count, bool) or not isinstance(public_count, int) or public_count < 0:
            raise LiveCodeBenchAdapterError("raw result public_test_count is invalid")
        # The official stdio checker stops at the first failing test.  Its
        # result prefix can be shorter than the number of public samples.
        if public_count > len(codes) and all(code is True for code in codes):
            raise LiveCodeBenchAdapterError("public_test_count exceeds a successful result")

        groups = tuple(
            self._group(
                group_id=group_id,
                visibility=visibility,
                codes=group_codes,
            )
            for group_id, visibility, group_codes in (
                ("public", TestVisibility.PUBLIC, codes[:public_count]),
                ("private", TestVisibility.HIDDEN, codes[public_count:]),
            )
            if group_codes
        )
        failing = next((code for code in codes if code is not True), None)
        if failing is None:
            return ExecutionStatus.PASSED, groups, None
        return _STATUS_BY_CODE[failing], groups, _FAILURE_KIND_BY_CODE[failing]

    @staticmethod
    def _group(
        *,
        group_id: str,
        visibility: TestVisibility,
        codes: Sequence[Any],
    ) -> TestGroupSummary:
        failing = next((code for code in codes if code is not True), None)
        status = ExecutionStatus.PASSED if failing is None else _STATUS_BY_CODE[failing]
        return TestGroupSummary(
            group_id=group_id,
            visibility=visibility,
            status=status,
            failed_test_count=sum(1 for code in codes if code is not True),
            total_test_count=len(codes),
        )


@dataclass(frozen=True, slots=True)
class SmokeCheckReport:
    """Pre-run gate: data identity, checker identity, container environment."""

    data_identity_ok: bool
    checker_identity_ok: bool
    container_environment_ok: bool
    problems: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return (
            self.data_identity_ok
            and self.checker_identity_ok
            and self.container_environment_ok
            and not self.problems
        )


def run_smoke_check(
    *,
    dataset_dir: Path | None,
    checker_dir: Path | None,
    pin: LiveCodeBenchPin = LCB_PIN,
    command_runner: CommandRunner = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    probe_timeout_seconds: float = 30.0,
) -> SmokeCheckReport:
    """Verify dataset identity, checker identity, and Docker availability.

    This gate runs no candidate code and no official evaluation; it only
    confirms that the bytes, checker files, and container runtime that a later
    real run would rely on are exactly the pinned ones.
    """

    problems: list[str] = []

    data_ok = False
    if dataset_dir is None:
        problems.append("dataset directory not provided")
    else:
        try:
            verify_source_directory(dataset_dir, pin=pin)
            data_ok = True
        except LiveCodeBenchAdapterError as exc:
            problems.append(f"data identity: {exc}")

    checker_ok = False
    if checker_dir is None:
        problems.append("checker directory not provided")
    else:
        root = Path(checker_dir).expanduser()
        if not root.is_dir() or root.is_symlink():
            problems.append("checker identity: checker directory is not a real directory")
        else:
            mismatched = []
            for file_pin in pin.checker_files:
                path = root / file_pin.name
                try:
                    _require_regular_file(path)
                    if _sha256_file(path) != file_pin.sha256:
                        mismatched.append(file_pin.name)
                except LiveCodeBenchAdapterError:
                    mismatched.append(file_pin.name)
            if mismatched:
                problems.append(
                    "checker identity: file hash mismatch: " + ", ".join(sorted(mismatched))
                )
            else:
                try:
                    completed = command_runner(
                        ["git", "-C", str(root), "rev-parse", "HEAD"],
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        text=True,
                        timeout=probe_timeout_seconds,
                        check=False,
                    )
                    head = completed.stdout.strip() if completed.returncode == 0 else ""
                except (OSError, subprocess.TimeoutExpired):
                    head = ""
                if head != pin.checker_commit:
                    problems.append("checker identity: repository commit does not match the pin")
                else:
                    checker_ok = True

    container_ok = False
    if which("docker") is None:
        problems.append("container environment: docker CLI not found")
    else:
        try:
            completed = command_runner(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=probe_timeout_seconds,
                check=False,
            )
            if completed.returncode == 0 and completed.stdout.strip():
                container_ok = True
            else:
                problems.append("container environment: docker daemon unavailable")
        except (OSError, subprocess.TimeoutExpired):
            problems.append("container environment: docker daemon probe failed")

    return SmokeCheckReport(
        data_identity_ok=data_ok,
        checker_identity_ok=checker_ok,
        container_environment_ok=container_ok,
        problems=tuple(problems),
    )


def candidate_from_code(
    *,
    task: BenchmarkTask,
    candidate_id: str,
    code: str,
    solution_trace_sha256: str | None = None,
    source_run_id: str | None = None,
) -> BenchmarkCandidate:
    """Create a generated candidate without executing its source."""

    if task.identity.interface is not TaskInterface.STANDARD_IO:
        raise LiveCodeBenchAdapterError("candidate task must use the standard-I/O interface")
    return BenchmarkCandidate(
        task=task.identity,
        candidate_id=candidate_id,
        origin="generated",
        code=code,
        code_sha256=hashlib.sha256(code.encode("utf-8")).hexdigest(),
        solution_trace_sha256=solution_trace_sha256,
        source_run_id=source_run_id,
    )


__all__ = [
    "FORMATTING_WITHOUT_STARTER_CODE",
    "INFRASTRUCTURE_ERROR_TYPES",
    "LCB60_PROTOCOL",
    "LCB60_SELECTION_ALGORITHM",
    "LCB60SelectionProtocol",
    "LCB_ADAPTER_NAME",
    "LCB_ADAPTER_VERSION",
    "LCB_CHECKER_COMMIT",
    "LCB_CHECKER_REPO_URI",
    "LCB_DATASET_ID",
    "LCB_EXECUTOR_ID",
    "LCB_HF_REVISION",
    "LCB_LICENSE",
    "LCB_PIN",
    "LCB_RELEASE_TAG",
    "LCB_SOURCE_URI",
    "LCBFilePin",
    "LCBSourceRecord",
    "LiveCodeBenchAdapterError",
    "LiveCodeBenchBenchmarkAdapter",
    "LiveCodeBenchPin",
    "NOT_RUN_ERROR_TYPES",
    "PROMPT_TEMPLATE_VERSION",
    "SELECTION_LOCK_KIND",
    "SELECTION_LOCK_SCHEMA_VERSION",
    "SmokeCheckReport",
    "build_experiment_manifest",
    "build_public_prompt",
    "build_selection_lock",
    "candidate_from_code",
    "load_source_records",
    "parse_source_record",
    "run_smoke_check",
    "select_lcb60_task_ids",
    "verify_selection_lock",
    "verify_source_directory",
]
