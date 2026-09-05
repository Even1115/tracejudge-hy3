"""Synthetic-fixture tests for the CodeJudge-Eval judge-only integration.

All fixtures are synthetic records in the verified CodeJudge-Eval 0-shot
schema; no real dataset content is downloaded or embedded.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from tracejudge_hy3.benchmark.codejudge_eval import (
    ADAPTER_ID,
    DATASET_ID,
    DATASET_LICENSE,
    HF_REVISION,
    SELECTION_PER_DIFFICULTY,
    SELECTION_SEED,
    ZERO_SHOT_FILES,
    CodeJudgeEvalAdapter,
    CodeJudgeEvalError,
    CodeJudgeEvalSample,
    build_descriptor,
    choice_table,
    file_sha256,
    hash_source_directory,
    judge_visible_projection,
    select_samples,
    verify_selection_files,
)
from tracejudge_hy3.benchmark.contracts import (
    BenchmarkCandidate,
    BenchmarkDifficulty,
    BenchmarkJudgeRecord,
    CandidateOrigin,
    EvaluationMode,
    JudgeStatus,
    TaskInterface,
    canonical_sha256,
    ordered_ids_sha256,
)
from tracejudge_hy3.benchmark.hy3_judge_only import Hy3JudgeOnlyProvider
from tracejudge_hy3.benchmark.judge_only_manifest import (
    EXPERIMENT_ID,
    HISTORICAL_EXPERIMENT_ID_V1,
    SMOKE_EXPERIMENT_ID,
    build_completion_receipt,
    build_experiment_manifest,
    build_run_sidecar,
)
from tracejudge_hy3.benchmark.judge_only_metrics import (
    ALLOWED_METRICS,
    FORBIDDEN_METRICS,
    macro_f1_zero_division,
    score_judge_only,
)
from tracejudge_hy3.benchmark.judge_only_prompt import (
    JUDGE_ONLY_PROMPT_BUNDLE_VERSION,
    JUDGE_ONLY_PROMPT_VERSION,
    REPAIR_INVALID_RESPONSE_MAX_CHARS,
    JudgeOnlyVerdict,
    build_judge_only_repair_prompt,
    build_judge_only_user_prompt,
    judge_only_prompt_bundle_sha256,
    judge_only_prompt_sha256,
    judge_only_system_prompt,
)
from tracejudge_hy3.benchmark.judge_only_runner import (
    judge_sample,
    judge_samples,
    parse_judge_only_verdict,
)
from tracejudge_hy3.config import Settings
from tracejudge_hy3.exceptions import ProviderResponseError, ProviderTimeoutError
from tracejudge_hy3.schemas.evaluation import ErrorType

_CHOICES_BLOCK = {
    BenchmarkDifficulty.EASY: "(A). AC\n(B). CE\n(C). Not AC",
    BenchmarkDifficulty.MEDIUM: (
        "(A). AC\n(B). CE\n(C). Not AC, only WA errors\n(D). Not AC, only RE errors\n"
        "(E). Not AC, only TLE errors\n(F). Not AC for at least two types of errors"
    ),
    BenchmarkDifficulty.HARD: (
        "(A). AC\n(B). CE\n(C). Not AC, only WA errors\n(D). Not AC, only RE errors\n"
        "(E). Not AC, both WA and RE errors\n(F). Not AC, only TLE errors\n"
        "(G). Not AC, both WA and TLE errors\n(H). Not AC, both TLE and RE errors\n"
        "(I). Not AC, all WA, RE, and TLE errors"
    ),
}

_ANSWER_CYCLE = {
    BenchmarkDifficulty.EASY: ("A", "B", "C"),
    BenchmarkDifficulty.MEDIUM: ("A", "B", "C", "D", "E", "F"),
    BenchmarkDifficulty.HARD: ("A", "B", "C", "D", "E", "F", "G", "H", "I"),
}


def make_record(
    *,
    difficulty: BenchmarkDifficulty,
    task_id: int,
    data_id: int,
    answer: str,
    statement: str | None = None,
    code: str | None = None,
    source: str = "synthetic-generator",
    choices_override: str | None = None,
) -> dict:
    statement = statement or (
        f"Synthetic problem {task_id}: read two integers and print their sum.\n\n"
        "-----Input-----\n\nTwo integers a and b.\n\n-----Output-----\n\nPrint a + b."
    )
    code = (
        code
        or f"a, b = map(int, input().split())  # task {task_id} record {data_id}\nprint(a + b)\n"
    )
    choices = choices_override or _CHOICES_BLOCK[difficulty]
    prompt = (
        "# Task Requirement\n\nJudge the code.\n\n## Explanation of Choices\n\n"
        "AC/CE/WA/RE/TLE definitions.\n\n## Choices\n\n"
        f"{choices}\n\n## Problem\n\n### Problem Description\n\n{statement}\n\n"
        f"### Solution to be Judged\n\n{code}\n"
    )
    return {
        "task_id": task_id,
        "statement": statement,
        "code": code,
        "answer": answer,
        "url": f"https://example.invalid/problem/{task_id}",
        "input": prompt,
        "source": source,
        "data_id": data_id,
    }


def write_fixture(
    root: Path,
    *,
    tasks_per_difficulty: int = 3,
    candidates_per_task: int = 2,
) -> Path:
    for difficulty, filename in ZERO_SHOT_FILES.items():
        records = []
        answers = _ANSWER_CYCLE[difficulty]
        data_id = 0
        for task_id in range(tasks_per_difficulty):
            for candidate_index in range(candidates_per_task):
                records.append(
                    make_record(
                        difficulty=difficulty,
                        task_id=task_id,
                        data_id=data_id,
                        answer=answers[(task_id + candidate_index) % len(answers)],
                    )
                )
                data_id += 1
        (root / filename).write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return root


@pytest.fixture()
def small_source(tmp_path: Path) -> Path:
    return write_fixture(tmp_path, tasks_per_difficulty=3, candidates_per_task=2)


@pytest.fixture()
def large_source(tmp_path: Path) -> Path:
    # Mirrors the real data property that all difficulties share one task pool,
    # with enough tasks for 30 disjoint draws per difficulty.
    return write_fixture(tmp_path, tasks_per_difficulty=95, candidates_per_task=2)


class ScriptedProvider:
    """Deterministic judge-only provider for tests."""

    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[return-value]


def _verdict_json(functional_correct: bool, verdict: str, error_type: str | None = None) -> str:
    return json.dumps(
        {
            "functional_correct": functional_correct,
            "verdict": verdict,
            "error_type": error_type,
            "explanation": "synthetic judgment",
            "confidence": 0.75,
        }
    )


# --- loading and binding -------------------------------------------------


def test_loads_public_tasks_and_dataset_provided_candidates(small_source: Path) -> None:
    adapter = CodeJudgeEvalAdapter()
    samples = adapter.load_samples(small_source)
    assert len(samples) == 3 * 3 * 2
    descriptor = adapter.descriptor
    assert descriptor.dataset_id == DATASET_ID
    assert descriptor.revision == HF_REVISION
    assert descriptor.license == DATASET_LICENSE
    assert descriptor.adapter_id == ADAPTER_ID
    assert descriptor.evaluation_modes == (EvaluationMode.JUDGE_ONLY,)
    for sample in samples:
        assert sample.task.identity.evaluation_mode is EvaluationMode.JUDGE_ONLY
        assert sample.candidate.origin is CandidateOrigin.DATASET_PROVIDED
        assert sample.candidate.task == sample.task.identity
        assert sample.task.identity.interface is TaskInterface.STANDARD_IO
        assert sample.task.identity.language == "python"
    tasks = adapter.load_tasks(small_source)
    assert len(tasks) == 9  # one task per (difficulty, task_id), candidates grouped


def test_descriptor_binds_source_file_hashes(small_source: Path) -> None:
    hashes = hash_source_directory(small_source)
    assert set(hashes) == set(ZERO_SHOT_FILES.values())
    descriptor = build_descriptor(hashes)
    adapter = CodeJudgeEvalAdapter(descriptor=descriptor)
    adapter.load_samples(small_source)
    # Tamper: flip one byte of the easy file.
    easy = small_source / ZERO_SHOT_FILES[BenchmarkDifficulty.EASY]
    easy.write_text(easy.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(CodeJudgeEvalError, match="manifest hash"):
        adapter.load_samples(small_source)


def test_gold_binding_to_candidate_revision_and_file(small_source: Path) -> None:
    samples = CodeJudgeEvalAdapter().load_samples(small_source)
    for sample in samples:
        table = choice_table(sample.task.difficulty)
        spec = table[sample.gold.answer_letter]
        assert sample.gold.functional_correct is spec.functional_correct
        assert sample.gold.source_error_type == spec.verdict_class.value
        assert sample.gold.normalized_error_type == spec.normalized_error_type
        assert sample.source_file_sha256 == file_sha256(small_source / sample.source_file)
        assert sample.task.identity.dataset_revision == HF_REVISION


def test_function_interface_detection(small_source: Path) -> None:
    record = make_record(
        difficulty=BenchmarkDifficulty.EASY,
        task_id=99,
        data_id=900,
        answer="A",
        statement=(
            "Implement the function.\n\n```python\ndef add_two(a, b):\n    ...\n```\n"
            "Return the sum."
        ),
        code="def add_two(a, b):\n    return a + b\n",
    )
    path = small_source / ZERO_SHOT_FILES[BenchmarkDifficulty.EASY]
    records = json.loads(path.read_text(encoding="utf-8"))
    records.append(record)
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    samples = CodeJudgeEvalAdapter().load_samples(small_source)
    sample = next(s for s in samples if s.task.identity.task_id == "easy/99")
    assert sample.task.identity.interface is TaskInterface.FUNCTION
    assert sample.task.entry_point == "add_two"


def test_ambiguous_interface_fails_closed(small_source: Path) -> None:
    record = make_record(
        difficulty=BenchmarkDifficulty.EASY,
        task_id=98,
        data_id=901,
        answer="A",
        statement="Write code that reads values and prints the result.",
        code="x = 1\nprint(x)\n",
    )
    path = small_source / ZERO_SHOT_FILES[BenchmarkDifficulty.EASY]
    records = json.loads(path.read_text(encoding="utf-8"))
    records.append(record)
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(CodeJudgeEvalError, match="genuine task interface"):
        CodeJudgeEvalAdapter().load_samples(small_source)


def test_ambiguous_interface_exclude_and_count(small_source: Path) -> None:
    record = make_record(
        difficulty=BenchmarkDifficulty.EASY,
        task_id=98,
        data_id=901,
        answer="A",
        statement="Write code that reads values and prints the result.",
        code="x = 1\nprint(x)\n",
    )
    path = small_source / ZERO_SHOT_FILES[BenchmarkDifficulty.EASY]
    records = json.loads(path.read_text(encoding="utf-8"))
    records.append(record)
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    adapter = CodeJudgeEvalAdapter()
    samples = adapter.load_samples(small_source, exclude_ambiguous_interface=True)
    assert len(samples) == 18  # the ambiguous record is skipped, nothing else
    assert len(adapter.excluded_records) == 1
    excluded = adapter.excluded_records[0]
    assert excluded.task_id == 98 and excluded.data_id == 901
    assert excluded.source_file == ZERO_SHOT_FILES[BenchmarkDifficulty.EASY]
    assert "genuine task interface" in excluded.reason


# --- crosswalk ------------------------------------------------------------


def test_crosswalk_reliable_mappings() -> None:
    medium = choice_table(BenchmarkDifficulty.MEDIUM)
    assert medium["C"].normalized_error_type is ErrorType.E03_WRONG_OUTPUT
    assert medium["D"].normalized_error_type is ErrorType.E01_RUNTIME_EXCEPTION
    assert medium["E"].normalized_error_type is ErrorType.E02_TIMEOUT_OR_RESOURCE_ERROR
    hard = choice_table(BenchmarkDifficulty.HARD)
    assert hard["F"].normalized_error_type is ErrorType.E02_TIMEOUT_OR_RESOURCE_ERROR


def test_crosswalk_unmapped_stays_empty() -> None:
    for difficulty in BenchmarkDifficulty:
        if difficulty is BenchmarkDifficulty.UNKNOWN:
            continue
        table = choice_table(difficulty)
        assert table["A"].functional_correct is True
        assert table["B"].normalized_error_type is None  # CE: no compile-error type
        assert table["B"].crosswalk_unmapped is True
    assert choice_table(BenchmarkDifficulty.EASY)["C"].crosswalk_unmapped is True
    hard = choice_table(BenchmarkDifficulty.HARD)
    for letter in ("E", "G", "H", "I"):
        assert hard[letter].normalized_error_type is None
        assert hard[letter].crosswalk_unmapped is True
    assert choice_table(BenchmarkDifficulty.MEDIUM)["F"].crosswalk_unmapped is True


def test_unknown_answer_letter_fails(small_source: Path) -> None:
    record = make_record(difficulty=BenchmarkDifficulty.EASY, task_id=0, data_id=500, answer="D")
    path = small_source / ZERO_SHOT_FILES[BenchmarkDifficulty.EASY]
    records = json.loads(path.read_text(encoding="utf-8"))
    records[0] = record
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(CodeJudgeEvalError, match="not defined"):
        CodeJudgeEvalAdapter().load_samples(small_source)


def test_choice_section_mismatch_fails(small_source: Path) -> None:
    record = make_record(
        difficulty=BenchmarkDifficulty.EASY,
        task_id=0,
        data_id=501,
        answer="C",
        choices_override=_CHOICES_BLOCK[BenchmarkDifficulty.HARD],
    )
    path = small_source / ZERO_SHOT_FILES[BenchmarkDifficulty.EASY]
    records = json.loads(path.read_text(encoding="utf-8"))
    records[0] = record
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(CodeJudgeEvalError, match="do not match the pinned"):
        CodeJudgeEvalAdapter().load_samples(small_source)


def test_duplicate_data_id_fails(small_source: Path) -> None:
    path = small_source / ZERO_SHOT_FILES[BenchmarkDifficulty.MEDIUM]
    records = json.loads(path.read_text(encoding="utf-8"))
    records.append(dict(records[0]))
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(CodeJudgeEvalError, match="duplicate data_id"):
        CodeJudgeEvalAdapter().load_samples(small_source)


def test_inconsistent_statement_fails(small_source: Path) -> None:
    path = small_source / ZERO_SHOT_FILES[BenchmarkDifficulty.HARD]
    records = json.loads(path.read_text(encoding="utf-8"))
    records[1]["statement"] = "A completely different statement."
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(CodeJudgeEvalError, match="inconsistent statements"):
        CodeJudgeEvalAdapter().load_samples(small_source)


def test_one_shot_files_are_never_read(small_source: Path) -> None:
    garbage = small_source / "CodeJudge_Eval_1shot_easy.json"
    garbage.write_text("this is not json", encoding="utf-8")
    samples = CodeJudgeEvalAdapter().load_samples(small_source)
    assert samples


# --- disclosure safety ------------------------------------------------------


def test_judge_projection_excludes_labels_and_provenance(small_source: Path) -> None:
    samples = CodeJudgeEvalAdapter().load_samples(small_source)
    for sample in samples:
        projection = judge_visible_projection(sample)
        assert set(projection) <= {
            "interface",
            "language",
            "difficulty",
            "statement",
            "candidate_code",
            "entry_point",
        }
        blob = json.dumps(projection, ensure_ascii=False)
        assert sample.gold.generator_source not in blob
        assert "example.invalid" not in blob  # source URL
        assert sample.source_record_sha256 not in blob
        prompt = build_judge_only_user_prompt(sample)
        assert "## Choices" not in prompt  # the dataset's own prompt never leaks
        assert sample.gold.generator_source not in prompt


# --- selection protocol -----------------------------------------------------


def test_selection_is_fixed_and_deterministic(large_source: Path) -> None:
    adapter = CodeJudgeEvalAdapter()
    samples = adapter.load_samples(large_source)
    first = select_samples(samples, descriptor=adapter.descriptor)
    second = select_samples(samples, descriptor=adapter.descriptor)
    assert first.selected_task_ids == second.selected_task_ids
    assert first.selected_task_ids_sha256 == second.selected_task_ids_sha256
    assert len(first.entries) == 3 * SELECTION_PER_DIFFICULTY
    assert first.seed == SELECTION_SEED
    for difficulty in (
        BenchmarkDifficulty.EASY,
        BenchmarkDifficulty.MEDIUM,
        BenchmarkDifficulty.HARD,
    ):
        assert (
            sum(1 for entry in first.entries if entry.difficulty is difficulty)
            == SELECTION_PER_DIFFICULTY
        )
    assert len(set(first.selected_task_ids)) == len(first.selected_task_ids)
    # Cross-difficulty disjointness: the three files share one task pool, so
    # raw task ids must not repeat across difficulties.
    raw_ids_by_difficulty: dict[str, set[int]] = {"easy": set(), "medium": set(), "hard": set()}
    for entry in first.entries:
        raw_ids_by_difficulty[entry.difficulty.value].add(int(entry.task_id.split("/", 1)[1]))
    combined = (
        raw_ids_by_difficulty["easy"]
        | raw_ids_by_difficulty["medium"]
        | raw_ids_by_difficulty["hard"]
    )
    assert len(combined) == 3 * SELECTION_PER_DIFFICULTY
    by_task = {}
    for sample in samples:
        if sample.task.identity.task_id in first.selected_task_ids:
            by_task.setdefault(sample.task.identity.task_id, []).append(sample)
    for entry in first.entries:
        candidates = by_task[entry.task_id]
        chosen = min(candidates, key=lambda s: int(s.candidate.candidate_id.rsplit("/", 1)[1]))
        assert entry.candidate_code_sha256 == chosen.candidate.code_sha256
        assert entry.source_file_sha256 == file_sha256(large_source / entry.source_file)


def test_selection_requires_enough_tasks(small_source: Path) -> None:
    adapter = CodeJudgeEvalAdapter()
    samples = adapter.load_samples(small_source)
    with pytest.raises(CodeJudgeEvalError, match="below the required"):
        select_samples(samples, descriptor=adapter.descriptor)


def test_selection_detects_source_file_tampering(large_source: Path) -> None:
    adapter = CodeJudgeEvalAdapter()
    samples = adapter.load_samples(large_source)
    selection = select_samples(samples, descriptor=adapter.descriptor)
    verify_selection_files(selection, large_source)
    hard = large_source / ZERO_SHOT_FILES[BenchmarkDifficulty.HARD]
    hard.write_text(hard.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(CodeJudgeEvalError, match="hash mismatch"):
        verify_selection_files(selection, large_source)


def test_candidate_hash_tampering_fails(small_source: Path) -> None:
    sample = CodeJudgeEvalAdapter().load_samples(small_source)[0]
    with pytest.raises(ValidationError, match="code_sha256"):
        BenchmarkCandidate(
            task=sample.candidate.task,
            candidate_id=sample.candidate.candidate_id,
            origin=CandidateOrigin.DATASET_PROVIDED,
            code=sample.candidate.code + "\n# tampered",
            code_sha256=sample.candidate.code_sha256,
        )


def test_cross_task_candidate_binding_fails(small_source: Path) -> None:
    samples = CodeJudgeEvalAdapter().load_samples(small_source)
    first, second = samples[0], samples[-1]
    assert first.task.identity != second.task.identity
    with pytest.raises(ValidationError, match="not bound to the sample task"):
        CodeJudgeEvalSample(
            task=first.task,
            candidate=second.candidate,
            gold=first.gold,
            source_file=first.source_file,
            source_file_sha256=first.source_file_sha256,
            source_record_sha256=first.source_record_sha256,
        )


# --- prompt freeze ----------------------------------------------------------


# Historical full-v1 prompt hash (system-prompt-only binding, v1).  The v1
# prompt content and its hash must stay verifiable forever.
_FULL_V1_PROMPT_SHA256 = "a10cdc748b31c419e1cd54dc127a759d8be0518c9e36a1dfa17a3f7d0780ed8b"


def test_judge_only_prompt_is_frozen_by_content_hash() -> None:
    digest = judge_only_prompt_sha256()
    assert digest == judge_only_prompt_sha256()
    assert len(digest) == 64
    assert digest == _FULL_V1_PROMPT_SHA256  # v1 remains verifiable for full-v1
    system_prompt = judge_only_system_prompt()
    assert JUDGE_ONLY_PROMPT_VERSION
    assert "judge-only" in system_prompt
    assert "不得" in system_prompt  # guardrails present
    assert "JSON Schema" in system_prompt
    # The schema forbids any process/first-step output field.
    schema = JudgeOnlyVerdict.model_json_schema()
    assert "process_correct" not in json.dumps(schema)
    assert "first_faulty_step" not in json.dumps(schema)


def test_prompt_bundle_v2_binds_templates_and_schema() -> None:
    bundle_digest = judge_only_prompt_bundle_sha256()
    assert bundle_digest == judge_only_prompt_bundle_sha256()
    assert len(bundle_digest) == 64
    # The v2 bundle binds more than the v1 system-prompt-only hash and must not
    # masquerade as v1.
    assert JUDGE_ONLY_PROMPT_BUNDLE_VERSION != JUDGE_ONLY_PROMPT_VERSION
    assert bundle_digest != judge_only_prompt_sha256()


def test_repair_prompt_contains_context_diagnostic_and_invalid_output(
    small_source: Path,
) -> None:
    sample = CodeJudgeEvalAdapter().load_samples(small_source)[0]
    invalid = '{"functional_correct": "maybe", "verdict": 42}'
    diagnostic = '[{"type":"literal_error","loc":["verdict"]}]'
    repair = build_judge_only_repair_prompt(
        sample, invalid_response=invalid, safe_diagnostic=diagnostic
    )
    # The repair request re-includes the original judge input verbatim: the
    # full user prompt with the public projection (statement) and the
    # candidate code.
    assert build_judge_only_user_prompt(sample) in repair
    assert json.dumps(sample.task.prompt, ensure_ascii=False)[1:-1] in repair
    assert json.dumps(sample.candidate.code, ensure_ascii=False)[1:-1] in repair
    # ...plus the sanitized diagnostic and the exact invalid output to repair.
    assert diagnostic in repair
    assert invalid in repair


def test_repair_prompt_excludes_gold_and_provenance(small_source: Path) -> None:
    samples = CodeJudgeEvalAdapter().load_samples(small_source)
    for sample in samples:
        repair = build_judge_only_repair_prompt(
            sample, invalid_response="garbage", safe_diagnostic="invalid_json_at_1_1"
        )
        assert sample.gold.generator_source not in repair
        assert "example.invalid" not in repair  # source URL
        assert "## Choices" not in repair  # the dataset's own prompt never leaks
        assert sample.source_record_sha256 not in repair
        assert str(sample.candidate.candidate_id) not in repair  # data_id path


def test_repair_prompt_caps_invalid_response_length(small_source: Path) -> None:
    sample = CodeJudgeEvalAdapter().load_samples(small_source)[0]
    huge_invalid = "x" * (REPAIR_INVALID_RESPONSE_MAX_CHARS * 4)
    repair = build_judge_only_repair_prompt(
        sample, invalid_response=huge_invalid, safe_diagnostic="invalid_json_at_1_1"
    )
    # The echoed invalid output is truncated to the cap; the full blob must not
    # appear, keeping prompt size bounded against pathological responses.
    assert huge_invalid not in repair
    assert "x" * (REPAIR_INVALID_RESPONSE_MAX_CHARS + 1) not in repair


def test_verdict_schema_rejects_inconsistent_judgment() -> None:
    with pytest.raises(ValidationError):
        JudgeOnlyVerdict.model_validate(
            {"functional_correct": True, "verdict": "WA", "explanation": "x"}
        )
    with pytest.raises(ValidationError):
        JudgeOnlyVerdict.model_validate(
            {
                "functional_correct": False,
                "verdict": "AC",
                "explanation": "x",
            }
        )


# --- runner ------------------------------------------------------------------


async def test_runner_emits_valid_frozen_record(small_source: Path) -> None:
    sample = CodeJudgeEvalAdapter().load_samples(small_source)[0]
    provider = ScriptedProvider([_verdict_json(True, "AC")])
    record = await judge_sample(provider, sample)
    assert record.status is JudgeStatus.VALID_JUDGMENT
    assert record.functional_correct is True
    assert record.source_error_type == "AC"
    assert record.normalized_error_type is None
    assert record.process_correct is None
    assert record.first_faulty_layer is None
    assert record.first_faulty_step is None
    assert record.certificate_verdict is None
    assert len(record.source_judgment_sha256) == 64
    with pytest.raises(ValidationError):
        record.functional_correct = False  # type: ignore[misc]


async def test_runner_keeps_provider_error_in_denominator(small_source: Path) -> None:
    sample = CodeJudgeEvalAdapter().load_samples(small_source)[0]
    provider = ScriptedProvider([ProviderTimeoutError("boom")])
    record = await judge_sample(provider, sample)
    assert record.status is JudgeStatus.PROVIDER_ERROR
    assert record.functional_correct is None
    assert record.normalized_error_type is None
    assert record.source_error_type is None


async def test_runner_parse_error_after_single_repair(small_source: Path) -> None:
    sample = CodeJudgeEvalAdapter().load_samples(small_source)[0]
    provider = ScriptedProvider(["not json at all", "still not json"])
    record = await judge_sample(provider, sample)
    assert record.status is JudgeStatus.PARSE_ERROR
    assert len(provider.calls) == 2  # one original + exactly one repair
    assert record.functional_correct is None


async def test_runner_repair_recovers(small_source: Path) -> None:
    sample = CodeJudgeEvalAdapter().load_samples(small_source)[0]
    provider = ScriptedProvider(["garbage", _verdict_json(False, "WA", "E03_WRONG_OUTPUT")])
    record = await judge_sample(provider, sample)
    assert record.status is JudgeStatus.VALID_JUDGMENT
    assert record.functional_correct is False
    assert record.normalized_error_type is ErrorType.E03_WRONG_OUTPUT
    assert record.source_error_type == "WA"
    # The repair call (second provider request) must be self-contained: the
    # original judge input, the invalid output, and a sanitized diagnostic.
    _, repair_prompt = provider.calls[1]
    assert build_judge_only_user_prompt(sample) in repair_prompt
    assert json.dumps(sample.candidate.code, ensure_ascii=False)[1:-1] in repair_prompt
    assert "garbage" in repair_prompt
    assert "invalid_json" in repair_prompt
    # ...and it must not leak gold or provenance.
    assert sample.gold.generator_source not in repair_prompt
    assert "## Choices" not in repair_prompt


async def test_runner_repair_provider_error_stays_distinct(small_source: Path) -> None:
    sample = CodeJudgeEvalAdapter().load_samples(small_source)[0]
    provider = ScriptedProvider(["garbage", ProviderTimeoutError("repair boom")])
    record = await judge_sample(provider, sample)
    assert record.status is JudgeStatus.PROVIDER_ERROR  # not PARSE_ERROR
    assert record.functional_correct is None
    assert len(provider.calls) == 2


def test_strict_parser_rejects_fenced_or_prose_json() -> None:
    with pytest.raises(Exception, match="invalid_json"):
        parse_judge_only_verdict("```json\n{}\n```")
    with pytest.raises(Exception, match="top_level_json_must_be_object"):
        parse_judge_only_verdict("[1, 2]")
    with pytest.raises(Exception, match="empty_response"):
        parse_judge_only_verdict("   ")


# --- metrics ------------------------------------------------------------------


async def test_metrics_full_denominator_and_breakdowns(small_source: Path) -> None:
    samples = CodeJudgeEvalAdapter().load_samples(small_source)
    responses: list[object] = []
    for sample in samples:
        if sample.gold.functional_correct:
            responses.append(_verdict_json(True, "AC"))
        else:
            verdict = {
                "CE": "CE",
                "WA": "WA",
                "RE": "RE",
                "TLE": "TLE",
                "MIXED": "MIXED",
                "NOT_AC_UNSPECIFIED": "WA",
            }[sample.gold.source_error_type]
            error_type = (
                sample.gold.normalized_error_type.value
                if sample.gold.normalized_error_type
                else None
            )
            responses.append(_verdict_json(False, verdict, error_type))
    # Force one provider error (sample 0) and one parse error (sample 1: the
    # repair consumes the next scripted response, so both failures sit at the
    # head of the queue).
    responses = [ProviderTimeoutError("timeout"), "unparseable", "still unparseable"] + (
        responses[2:]
    )
    provider = ScriptedProvider(responses)
    records = await judge_samples(provider, samples)

    report = score_judge_only(records, samples)
    assert report["n_samples"] == len(samples)
    assert report["status_counts"]["provider_error"] == 1
    assert report["status_counts"]["parse_error"] == 1
    expected_accuracy = (len(samples) - 2) / len(samples)
    assert report["correctness_accuracy_full_denominator"]["numerator"] == len(samples) - 2
    assert (
        abs(report["correctness_accuracy_full_denominator"]["estimate"] - expected_accuracy) < 1e-9
    )
    functional = report["functional_correctness"]
    assert functional["n_valid"] == len(samples) - 2
    assert functional["correct_code_fpr"]["numerator"] == 0
    assert report["majority_class_baseline_accuracy"]["denominator"] == len(samples)
    assert set(report["per_difficulty"]) == {"easy", "medium", "hard"}
    assert set(report["forbidden_metrics_not_computed"]) == set(FORBIDDEN_METRICS)
    crosswalk = report["crosswalk"]
    assert crosswalk["gold_unmapped_incorrect"] > 0
    assert crosswalk["gold_mapped_incorrect"] > 0


def test_metrics_reject_unbound_records(small_source: Path) -> None:
    samples = CodeJudgeEvalAdapter().load_samples(small_source)
    sample = samples[0]
    other = BenchmarkJudgeRecord(
        task=sample.task.identity,
        candidate_sha256=hashlib.sha256(b"other code").hexdigest(),
        method_id="tracejudge-judge-only",
        status=JudgeStatus.PROVIDER_ERROR,
        source_judgment_sha256=hashlib.sha256(b"raw").hexdigest(),
    )
    # Full coverage except one record swapped for a foreign candidate hash.
    records = _valid_records_for(samples)
    records[0] = other
    with pytest.raises(CodeJudgeEvalError, match="coverage"):
        score_judge_only(records, samples)


def _valid_records_for(samples: tuple[CodeJudgeEvalSample, ...]) -> list[BenchmarkJudgeRecord]:
    return [
        _synthetic_record(sample, "AC" if sample.gold.functional_correct else "WA")
        for sample in samples
    ]


def test_metrics_reject_missing_records(small_source: Path) -> None:
    samples = CodeJudgeEvalAdapter().load_samples(small_source)
    records = _valid_records_for(samples)
    incomplete = records[1:]  # silently drop one record
    with pytest.raises(CodeJudgeEvalError, match="coverage"):
        score_judge_only(incomplete, samples)


def test_metrics_reject_duplicate_records(small_source: Path) -> None:
    samples = CodeJudgeEvalAdapter().load_samples(small_source)
    records = _valid_records_for(samples)
    duplicated = records + [records[0]]
    with pytest.raises(CodeJudgeEvalError, match="coverage"):
        score_judge_only(duplicated, samples)


# --- metrics: label semantics (phase-1 fixes) ---------------------------------


def _full_v1_confusion_pairs() -> list[tuple[str, str]]:
    """Mechanical reconstruction of the full-v1 gold->verdict confusion matrix.

    Mirrors docs/codejudge_eval_error_analysis.md (90 valid judgments); used to
    pin zero_division=0 semantics against the known buggy value 0.688675.
    """

    pairs: list[tuple[str, str]] = []
    pairs += [("AC", "AC")] * 3 + [("AC", "TLE")]
    pairs += [("CE", "CE")] * 4
    pairs += [("WA", "AC")] * 2 + [("WA", "WA")] * 25 + [("WA", "MIXED")] * 8
    pairs += [("RE", "RE")] * 10
    pairs += [("TLE", "TLE")] * 2
    pairs += [("MIXED", "WA")] * 2 + [("MIXED", "MIXED")] * 7
    pairs += (
        [("NOT_AC_UNSPECIFIED", "AC")]
        + [("NOT_AC_UNSPECIFIED", "WA")] * 12
        + [("NOT_AC_UNSPECIFIED", "RE")] * 4
        + [("NOT_AC_UNSPECIFIED", "TLE")] * 2
        + [("NOT_AC_UNSPECIFIED", "MIXED")] * 7
    )
    return pairs


def test_macro_f1_zero_division_counts_unpredicted_gold_class() -> None:
    pairs = [("WA", "WA"), ("WA", "RE"), ("RE", "RE"), ("TLE", "WA")]
    result = macro_f1_zero_division(pairs)
    # union of gold/predicted labels = {RE, TLE, WA}; TLE has gold support
    # but zero predictions and must contribute F1 = 0, not be skipped.
    assert result["classes"] == ["RE", "TLE", "WA"]
    assert result["n_classes_averaged"] == len(result["classes"]) == 3
    tle = result["per_class"]["TLE"]
    assert tle["tp"] == 0 and tle["fn"] == 1
    assert tle["precision"] == 0.0
    assert tle["f1"] == 0.0
    assert all(scores["f1"] is not None for scores in result["per_class"].values())
    expected = (0.5 + 2.0 / 3.0 + 0.0) / 3.0
    assert abs(result["macro_f1"] - expected) < 1e-12


def test_macro_f1_full_v1_mechanical_recompute() -> None:
    result = macro_f1_zero_division(_full_v1_confusion_pairs())
    assert result["n_classes_averaged"] == 7
    # zero_division=0 fixes the historical 0.688675 (which silently dropped the
    # NOT_AC_UNSPECIFIED class) to the correct mechanical value.
    assert abs(result["macro_f1"] - 0.590293) < 1e-5
    assert result["per_class"]["NOT_AC_UNSPECIFIED"]["f1"] == 0.0


def _synthetic_record(
    sample: CodeJudgeEvalSample, verdict: str, error_type: ErrorType | None = None
) -> BenchmarkJudgeRecord:
    functional = verdict == "AC"
    return BenchmarkJudgeRecord(
        task=sample.task.identity,
        candidate_sha256=sample.candidate.code_sha256,
        method_id="tracejudge-judge-only",
        status=JudgeStatus.VALID_JUDGMENT,
        functional_correct=functional,
        normalized_error_type=None if functional else error_type,
        source_error_type=verdict,
        source_judgment_sha256=hashlib.sha256(verdict.encode("utf-8")).hexdigest(),
    )


def test_functional_binary_metrics_balanced_mcc_baseline(small_source: Path) -> None:
    samples = CodeJudgeEvalAdapter().load_samples(small_source)
    # small_source: 4 gold-AC, 14 gold-incorrect. Judge all correctly except
    # one false positive (gold AC judged WA) and one false negative
    # (gold WA judged AC): tp=13, fp=1, fn=1, tn=3.
    records: list[BenchmarkJudgeRecord] = []
    flipped_fp = flipped_fn = False
    for sample in samples:
        if sample.gold.functional_correct and not flipped_fp:
            records.append(_synthetic_record(sample, "WA", ErrorType.E03_WRONG_OUTPUT))
            flipped_fp = True
        elif sample.gold.source_error_type == "WA" and not flipped_fn:
            records.append(_synthetic_record(sample, "AC"))
            flipped_fn = True
        elif sample.gold.functional_correct:
            records.append(_synthetic_record(sample, "AC"))
        else:
            records.append(_synthetic_record(sample, "WA", ErrorType.E03_WRONG_OUTPUT))
    report = score_judge_only(records, samples)
    functional = report["functional_correctness"]
    assert (functional["tp"], functional["fp"], functional["fn"], functional["tn"]) == (
        13,
        1,
        1,
        3,
    )
    sensitivity = functional["sensitivity"]
    specificity = functional["specificity"]
    assert (sensitivity["numerator"], sensitivity["denominator"]) == (13, 14)
    assert (specificity["numerator"], specificity["denominator"]) == (3, 4)
    assert sensitivity["wilson_95_lower"] < sensitivity["estimate"]
    expected_balanced = (13 / 14 + 3 / 4) / 2
    assert abs(functional["balanced_accuracy"]["estimate"] - expected_balanced) < 1e-12
    # MCC = (13*3 - 1*1) / sqrt(14 * 14 * 4 * 4) = 38/56
    assert abs(functional["mcc"]["estimate"] - 38 / 56) < 1e-12
    baseline = report["majority_class_baseline_accuracy"]
    assert (baseline["numerator"], baseline["denominator"]) == (14, 18)
    assert abs(baseline["estimate"] - 7 / 9) < 1e-12


def test_execution_outcome_agreement_derives_from_verdict(small_source: Path) -> None:
    samples = CodeJudgeEvalAdapter().load_samples(small_source)
    records: list[BenchmarkJudgeRecord] = []
    for sample in samples:
        gold = sample.gold
        if gold.functional_correct:
            records.append(_synthetic_record(sample, "AC"))
        elif gold.source_error_type == "WA":
            # verdict WA derives execution outcome E03: agrees with gold.
            records.append(_synthetic_record(sample, "WA"))
        elif gold.source_error_type == "RE":
            # verdict MIXED has no single execution outcome: disagrees even
            # though the root-cause error_type matches the gold mapping.
            records.append(_synthetic_record(sample, "MIXED", ErrorType.E01_RUNTIME_EXCEPTION))
        elif gold.source_error_type == "TLE":
            records.append(_synthetic_record(sample, "RE"))
        else:
            records.append(_synthetic_record(sample, "MIXED"))
    report = score_judge_only(records, samples)
    outcome = report["execution_outcome"]
    assert outcome["judge_verdict_crosswalk"] == {
        "WA": "E03_WRONG_OUTPUT",
        "RE": "E01_RUNTIME_EXCEPTION",
        "TLE": "E02_TIMEOUT_OR_RESOURCE_ERROR",
    }
    agreement = outcome["agreement_on_mapped"]
    gold_mapped = sum(
        1 for s in samples if not s.gold.functional_correct and s.gold.normalized_error_type
    )
    assert agreement["denominator"] == gold_mapped
    # only the WA-gold/WA-verdict samples agree.
    n_wa = sum(1 for s in samples if s.gold.source_error_type == "WA")
    assert agreement["numerator"] == n_wa
    assert agreement["wilson_95_lower"] is not None
    # root-cause error_type must never be scored as an agreement rate against
    # execution-outcome gold.
    root_cause = report["root_cause_error_type"]
    assert root_cause["no_agreement_metric_computed"] is True
    assert "agreement" not in root_cause
    assert "error_type_agreement_on_mapped" not in report.get("crosswalk", {})


def test_coarse_verdict_macro_f1_compatible_subset(small_source: Path) -> None:
    samples = CodeJudgeEvalAdapter().load_samples(small_source)
    records = [
        _synthetic_record(sample, "WA", ErrorType.E03_WRONG_OUTPUT)
        if not sample.gold.functional_correct
        else _synthetic_record(sample, "AC")
        for sample in samples
    ]
    report = score_judge_only(records, samples)
    coarse = report["coarse_verdict"]
    assert coarse["judge_output_labels"] == ["AC", "CE", "MIXED", "RE", "TLE", "WA"]
    full = coarse["macro_f1_full_gold_space"]
    assert full["diagnostic_only"] is True
    assert full["n_classes_averaged"] == len(full["classes"])
    # The full-gold-space view includes the judge-unreachable label.
    assert "NOT_AC_UNSPECIFIED" in full["classes"]
    assert full["per_class"]["NOT_AC_UNSPECIFIED"]["f1"] == 0.0
    subset = coarse["macro_f1_compatible_subset"]
    n_not_ac = sum(1 for s in samples if s.gold.source_error_type == "NOT_AC_UNSPECIFIED")
    assert n_not_ac > 0
    assert subset["excluded_n"] == n_not_ac
    assert subset["excluded_gold_labels"] == {"NOT_AC_UNSPECIFIED": n_not_ac}
    assert subset["included_n"] == len(samples) - n_not_ac
    assert "NOT_AC_UNSPECIFIED" not in subset["classes"]
    assert subset["n_classes_averaged"] == len(subset["classes"])
    # every averaged class has a numeric F1 (zero_division=0).
    assert all(s["f1"] is not None for s in subset["per_class"].values())


# --- manifest ------------------------------------------------------------------


def test_manifest_freezes_prompt_selection_and_scope(large_source: Path) -> None:
    adapter = CodeJudgeEvalAdapter()
    samples = adapter.load_samples(large_source)
    selection = select_samples(samples, descriptor=adapter.descriptor)
    manifest = build_experiment_manifest(
        descriptor=adapter.descriptor,
        selection=selection,
        git_commit="0" * 40,
        git_dirty=False,
        provider="hy3",
        model="hy3-judge",
    )
    assert manifest.judge_prompt_sha256 == judge_only_prompt_bundle_sha256()
    assert manifest.generation_prompt_sha256 is None
    assert manifest.selected_task_ids_sha256 == selection.selected_task_ids_sha256
    assert tuple(manifest.metrics_scope) == ALLOWED_METRICS
    for forbidden in FORBIDDEN_METRICS:
        assert f"no-{forbidden}" in manifest.limitations
    assert "held-out-never-used-for-prompt-or-threshold-tuning" in manifest.limitations


# --- manifest: judged-subset binding, sidecar, audit (phase-4 fixes) -----------


def test_manifest_full_run_binds_full_selection(large_source: Path) -> None:
    adapter = CodeJudgeEvalAdapter()
    samples = adapter.load_samples(large_source)
    selection = select_samples(samples, descriptor=adapter.descriptor)
    manifest = build_experiment_manifest(
        descriptor=adapter.descriptor,
        selection=selection,
        git_commit="0" * 40,
        git_dirty=False,
    )
    assert manifest.experiment_id == EXPERIMENT_ID
    assert manifest.selected_task_ids == selection.selected_task_ids


def test_experiment_ids_separate_historical_v1_from_bundle_v2() -> None:
    # New runs bind the v2 prompt bundle, so they must NOT reuse the
    # historical full-v1 experiment id.
    assert HISTORICAL_EXPERIMENT_ID_V1 == "codejudge-eval-judge-only-v1"
    assert EXPERIMENT_ID == "codejudge-eval-judge-only-v2"
    assert SMOKE_EXPERIMENT_ID == "codejudge-eval-judge-only-v2-smoke"
    assert EXPERIMENT_ID != HISTORICAL_EXPERIMENT_ID_V1


def test_manifest_binds_judged_subset_and_marks_smoke(large_source: Path) -> None:
    adapter = CodeJudgeEvalAdapter()
    samples = adapter.load_samples(large_source)
    selection = select_samples(samples, descriptor=adapter.descriptor)
    judged = selection.entries[:5]
    manifest = build_experiment_manifest(
        descriptor=adapter.descriptor,
        selection=selection,
        git_commit="0" * 40,
        git_dirty=True,
        judged_entries=judged,
        smoke=True,
    )
    # A limited run must not masquerade as the formal experiment.
    assert manifest.experiment_id == SMOKE_EXPERIMENT_ID
    assert "smoke-run" in manifest.limitations
    assert "judged-n:5" in manifest.limitations
    # The manifest binds exactly the judged subset: ordered task IDs + hash.
    assert manifest.selected_task_ids == tuple(entry.task_id for entry in judged)
    assert manifest.selected_task_ids_sha256 == ordered_ids_sha256(manifest.selected_task_ids)
    assert manifest.selected_task_ids != selection.selected_task_ids


def test_manifest_rejects_judged_entries_outside_selection(large_source: Path) -> None:
    adapter = CodeJudgeEvalAdapter()
    samples = adapter.load_samples(large_source)
    selection = select_samples(samples, descriptor=adapter.descriptor)
    foreign = selection.entries[-1].model_copy(
        update={"candidate_id": "easy/999999/0", "task_id": "easy/999999"}
    )
    with pytest.raises(CodeJudgeEvalError, match="not part of the frozen selection"):
        build_experiment_manifest(
            descriptor=adapter.descriptor,
            selection=selection,
            git_commit="0" * 40,
            git_dirty=True,
            judged_entries=(foreign,),
            smoke=True,
        )


def test_hy3_public_configuration_never_leaks_credentials() -> None:
    settings = Settings(
        hy3_base_url="https://secret-endpoint.example.invalid/v1",
        hy3_api_key="sk-test-secret-12345",
        hy3_model="sk-test-secret-12345-model",
        _env_file=None,
    )
    provider = Hy3JudgeOnlyProvider(settings)
    blob = json.dumps(provider.public_configuration())
    assert "sk-test-secret-12345" not in blob  # not even as a substring of model
    assert "secret-endpoint" not in blob  # raw endpoint never leaves the process
    assert len(provider.public_configuration()["endpoint_sha256"]) == 64


async def test_hy3_provider_audit_counters() -> None:
    settings = Settings(
        hy3_base_url="https://provider.invalid/v1",
        hy3_api_key="k",
        hy3_model="m",
        _env_file=None,
    )
    provider = Hy3JudgeOnlyProvider(settings, max_retries=2)
    calls = {"n": 0}

    async def fake_call_once(*, system_prompt: str, user_prompt: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ProviderResponseError("boom")
        return "ok"

    provider._call_once = fake_call_once  # type: ignore[method-assign]
    assert await provider.complete(system_prompt="s", user_prompt="u") == "ok"
    assert provider.audit_counters() == {"calls": 1, "attempts": 2, "retries": 1}

    calls["n"] = 0

    async def always_fail(*, system_prompt: str, user_prompt: str) -> str:
        raise ProviderTimeoutError("nope")

    provider._call_once = always_fail  # type: ignore[method-assign]
    with pytest.raises(ProviderTimeoutError):
        await provider.complete(system_prompt="s", user_prompt="u")
    assert provider.audit_counters() == {"calls": 2, "attempts": 5, "retries": 3}


def test_run_sidecar_binds_code_data_and_judged_samples(large_source: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    adapter = CodeJudgeEvalAdapter()
    samples = adapter.load_samples(large_source)
    selection = select_samples(samples, descriptor=adapter.descriptor)
    judged = selection.entries[:3]
    manifest = build_experiment_manifest(
        descriptor=adapter.descriptor,
        selection=selection,
        git_commit="0" * 40,
        git_dirty=True,
        judged_entries=judged,
        smoke=True,
    )
    sidecar = build_run_sidecar(
        manifest=manifest,
        provider_config={"provider": "hy3", "model": "m"},
        judged_entries=judged,
        selection=selection,
        selection_file_sha256="ab" * 32,
        repo=repo,
        started_at="2026-09-05T00:00:00+00:00",
    )
    judged_block = sidecar["judged_samples"]
    assert judged_block["selected_n"] == len(selection.entries)
    assert judged_block["judged_n"] == 3
    triples = [
        {
            "task_id": entry.task_id,
            "candidate_id": entry.candidate_id,
            "candidate_code_sha256": entry.candidate_code_sha256,
        }
        for entry in judged
    ]
    assert judged_block["ordered"] == triples
    assert judged_block["ordered_judged_sha256"] == canonical_sha256(triples)
    assert sidecar["smoke"] is True
    assert sidecar["git"] == {"commit": "0" * 40, "dirty": True}
    assert "not-fully-reproducible" in sidecar["reproducibility_note"]
    assert sidecar["prompt_bundle_sha256"] == judge_only_prompt_bundle_sha256()
    # Code, dependency-lock, data and selection hashes all bind offline.
    assert sidecar["code_sha256"]
    assert all(len(digest) == 64 for digest in sidecar["code_sha256"].values())
    assert sidecar["dependency_lock_sha256"]
    assert sidecar["data"]["file_sha256"] == dict(sorted(selection.file_sha256.items()))
    assert sidecar["data"]["selection_file_sha256"] == "ab" * 32
    assert sidecar["python_version"].count(".") >= 2
    assert sidecar["started_at"] == "2026-09-05T00:00:00+00:00"


def test_completion_receipt_binds_times_audit_and_output_hashes(tmp_path: Path) -> None:
    (tmp_path / "records.jsonl").write_text('{"a": 1}\n', encoding="utf-8")
    (tmp_path / "report.json").write_text('{"b": 2}\n', encoding="utf-8")
    (tmp_path / "provider_raw.jsonl").write_text('{"c": 3}\n', encoding="utf-8")
    receipt = build_completion_receipt(
        run_dir=tmp_path,
        run_id="smoke-test",
        started_at="2026-09-05T00:00:00+00:00",
        completed_at="2026-09-05T00:01:30+00:00",
        audit={
            "provider_calls": 3,
            "provider_attempts": 4,
            "provider_retries": 1,
            "parse_repairs": 1,
        },
        manifest_sha256="cd" * 32,
        sidecar_sha256="ef" * 32,
    )
    assert receipt["duration_seconds"] == 90.0
    assert receipt["audit"]["provider_retries"] == 1
    assert receipt["audit"]["parse_repairs"] == 1
    outputs = receipt["outputs"]
    assert outputs["records_jsonl_sha256"] == hashlib.sha256(b'{"a": 1}\n').hexdigest()
    assert outputs["report_json_sha256"] == hashlib.sha256(b'{"b": 2}\n').hexdigest()
    assert outputs["provider_raw_jsonl_sha256"] == hashlib.sha256(b'{"c": 3}\n').hexdigest()
    assert outputs["manifest_sha256"] == "cd" * 32
