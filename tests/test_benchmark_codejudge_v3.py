"""Tests for the pre-registered stratified v3 experiments (selectors only).

No real model calls and no downloads: all fixtures are synthetic records in the
verified CodeJudge-Eval 0-shot schema, with the same candidates mirrored into
all three difficulty files (as in the real data).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from tracejudge_hy3.benchmark.codejudge_eval import (
    ZERO_SHOT_FILES,
    CodeJudgeEvalAdapter,
    CodeJudgeEvalError,
)
from tracejudge_hy3.benchmark.codejudge_v3 import (
    V3_STATUS,
    CodeJudgeV3Selection,
    select_v3_experiment_a,
    select_v3_experiment_b,
    used_raw_task_ids,
)
from tracejudge_hy3.benchmark.contracts import BenchmarkDifficulty
from tracejudge_hy3.benchmark.judge_only_prompt import JUDGE_ONLY_PROMPT_VERSION
from tracejudge_hy3.benchmark.judge_only_prompt_v3 import (
    JUDGE_ONLY_PROMPT_V3_BUNDLE_VERSION,
    JudgeOnlyVerdictV3Easy,
    JudgeOnlyVerdictV3Hard,
    JudgeOnlyVerdictV3Middle,
    derive_v3_label,
    judge_only_prompt_v3_bundle_sha256,
    judge_only_v3_system_prompt,
    normalize_v3_verdict,
)

# Logical execution-outcome class -> (easy, middle, hard) gold letters.
_LOGICAL_TO_LETTERS = {
    "AC": ("A", "A", "A"),
    "CE": ("B", "B", "B"),
    "WA": ("C", "C", "C"),
    "RE": ("C", "D", "D"),
    "TLE": ("C", "E", "F"),
    "WA+RE": ("C", "F", "E"),
    "WA+TLE": ("C", "F", "G"),
    "RE+TLE": ("C", "F", "H"),
    "WA+RE+TLE": ("C", "F", "I"),
}

_DIFFICULTY_INDEX = {
    BenchmarkDifficulty.EASY: 0,
    BenchmarkDifficulty.MEDIUM: 1,
    BenchmarkDifficulty.HARD: 2,
}

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


def write_v3_fixture(root: Path, tasks: dict[int, list[str]]) -> Path:
    """Mirror the same candidates into all three difficulty files.

    ``tasks`` maps raw task id -> per-candidate logical outcome class.  The
    real dataset property holds here: identical (task_id, data_id, code)
    across files, only the gold letter (granularity) differs.
    """

    per_file: dict[str, list[dict]] = {f: [] for f in ZERO_SHOT_FILES.values()}
    data_id = 0
    for task_id in sorted(tasks):
        statement = (
            f"V3 synthetic problem {task_id}.\n\n-----Input-----\n\nx\n\n-----Output-----\n\ny."
        )
        for logical in tasks[task_id]:
            code = f"print({task_id} + {data_id})  # synthetic\n"
            prompt_problem = f"### Problem Description\n\n{statement}"
            for difficulty, filename in ZERO_SHOT_FILES.items():
                letter = _LOGICAL_TO_LETTERS[logical][_DIFFICULTY_INDEX[difficulty]]
                prompt = (
                    "# Task Requirement\n\nJudge the code.\n\n## Explanation of Choices\n\n"
                    "defs.\n\n## Choices\n\n"
                    f"{_CHOICES_BLOCK[difficulty]}\n\n## Problem\n\n{prompt_problem}\n\n"
                    f"### Solution to be Judged\n\n{code}\n"
                )
                per_file[filename].append(
                    {
                        "task_id": task_id,
                        "statement": statement,
                        "code": code,
                        "answer": letter,
                        "url": f"https://example.invalid/problem/{task_id}",
                        "input": prompt,
                        "source": "synthetic-generator",
                        "data_id": data_id,
                    }
                )
            data_id += 1
    for filename, records in per_file.items():
        (root / filename).write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return root


def _rich_fixture(root: Path) -> Path:
    """Enough tasks for small v3 quotas: per class >= 4 tasks x 2 candidates."""

    tasks: dict[int, list[str]] = {}
    task_id = 100
    for logical in (
        "AC",
        "CE",
        "WA",
        "RE",
        "TLE",
        "WA+RE",
        "WA+TLE",
        "RE+TLE",
        "WA+RE+TLE",
    ):
        for _ in range(4):
            tasks[task_id] = [logical, logical]
            task_id += 1
    return write_v3_fixture(root, tasks)


@pytest.fixture()
def v3_source(tmp_path: Path) -> Path:
    return _rich_fixture(tmp_path)


# --- prompt bundle v3 ---------------------------------------------------------


def test_prompt_v3_bundle_is_new_and_deterministic() -> None:
    digest = judge_only_prompt_v3_bundle_sha256()
    assert digest == judge_only_prompt_v3_bundle_sha256()
    assert len(digest) == 64
    assert JUDGE_ONLY_PROMPT_V3_BUNDLE_VERSION != JUDGE_ONLY_PROMPT_VERSION
    for condition in ("easy", "middle", "hard"):
        prompt = judge_only_v3_system_prompt(condition)
        assert "judge-only" in prompt
        assert "JSON Schema" in prompt
    with pytest.raises(CodeJudgeEvalError):
        judge_only_v3_system_prompt("unknown-granularity")


def test_prompt_v3_conditions_have_truly_distinct_output_spaces() -> None:
    """The granularity manipulation is real: each condition has its own Schema."""

    easy_enum = set(JudgeOnlyVerdictV3Easy.model_json_schema()["properties"]["verdict"]["enum"])
    middle_enum = set(JudgeOnlyVerdictV3Middle.model_json_schema()["properties"]["verdict"]["enum"])
    hard_schema = JudgeOnlyVerdictV3Hard.model_json_schema()
    hard_enum = set(hard_schema["properties"]["verdict"]["enum"])
    assert easy_enum == {"AC", "CE", "NOT_AC"}
    assert middle_enum == {"AC", "CE", "WA", "RE", "TLE", "MIXED"}
    assert hard_enum == {"AC", "CE", "ERRORED"}
    # Only the hard condition can express outcome sets.
    assert "execution_outcomes" not in JudgeOnlyVerdictV3Easy.model_json_schema()["properties"]
    assert "execution_outcomes" not in JudgeOnlyVerdictV3Middle.model_json_schema()["properties"]
    assert "execution_outcomes" in hard_schema["properties"]
    # The three conditions embed different schemas in their system prompts.
    prompts = {c: judge_only_v3_system_prompt(c) for c in ("easy", "middle", "hard")}
    assert len(set(prompts.values())) == 3
    assert "NOT_AC" in prompts["easy"] and "NOT_AC" not in prompts["hard"]


def _v3_easy(verdict: str) -> JudgeOnlyVerdictV3Easy:
    return JudgeOnlyVerdictV3Easy.model_validate(
        {
            "functional_correct": verdict == "AC",
            "verdict": verdict,
            "error_type": None,
            "explanation": "x",
        }
    )


def _v3_middle(verdict: str) -> JudgeOnlyVerdictV3Middle:
    return JudgeOnlyVerdictV3Middle.model_validate(
        {
            "functional_correct": verdict == "AC",
            "verdict": verdict,
            "error_type": None,
            "explanation": "x",
        }
    )


def _v3_hard(outcomes: tuple[str, ...], name: str = "ERRORED") -> JudgeOnlyVerdictV3Hard:
    return JudgeOnlyVerdictV3Hard.model_validate(
        {
            "functional_correct": name == "AC",
            "verdict": name,
            "execution_outcomes": list(outcomes),
            "explanation": "x",
        }
    )


def test_prompt_v3_easy_and_middle_schema_consistency() -> None:
    with pytest.raises(Exception, match="functional"):
        JudgeOnlyVerdictV3Easy.model_validate(
            {"functional_correct": True, "verdict": "NOT_AC", "explanation": "x"}
        )
    with pytest.raises(Exception, match="AC"):
        JudgeOnlyVerdictV3Middle.model_validate(
            {"functional_correct": False, "verdict": "AC", "explanation": "x"}
        )


def test_prompt_v3_hard_schema_consistency() -> None:
    ok = _v3_hard((), "AC")
    assert ok.execution_outcomes == ()
    with pytest.raises(Exception, match="functional"):
        _v3_hard(("WA",), "AC")
    # ERRORED requires a non-empty outcome set.
    with pytest.raises(Exception, match="execution_outcomes"):
        _v3_hard(())
    # CE cannot declare execution outcomes.
    with pytest.raises(Exception, match="CE"):
        _v3_hard(("RE",), "CE")
    # Outcome sets are normalized to sorted unique tuples.
    assert _v3_hard(("TLE", "WA", "WA")).execution_outcomes == ("WA", "TLE")


def test_prompt_v3_hard_rejects_unknown_outcome_labels() -> None:
    """Fail closed on non-WA/RE/TLE values, including mixed valid+invalid."""

    with pytest.raises(Exception, match="execution_outcomes"):
        _v3_hard(("WA", "BOGUS"))
    with pytest.raises(Exception, match="execution_outcomes"):
        _v3_hard(("BOGUS",))
    with pytest.raises(ValidationError):
        _v3_hard(("wa",))  # case-sensitive


def test_prompt_v3_derivation_per_condition_native_space() -> None:
    # easy: native 3-letter space.
    assert derive_v3_label("easy", _v3_easy("AC")) == "A"
    assert derive_v3_label("easy", _v3_easy("CE")) == "B"
    assert derive_v3_label("easy", _v3_easy("NOT_AC")) == "C"
    # middle: native 6-letter space.
    assert derive_v3_label("middle", _v3_middle("AC")) == "A"
    assert derive_v3_label("middle", _v3_middle("MIXED")) == "F"
    # hard: every WA/RE/TLE combination stays distinguishable.
    hard_labels = {
        derive_v3_label("hard", _v3_hard(combo))
        for combo in (
            ("WA",),
            ("RE",),
            ("TLE",),
            ("WA", "RE"),
            ("WA", "TLE"),
            ("RE", "TLE"),
            ("WA", "RE", "TLE"),
        )
    }
    assert hard_labels == {"C", "D", "E", "F", "G", "H", "I"}
    assert derive_v3_label("hard", _v3_hard((), "AC")) == "A"
    assert derive_v3_label("hard", _v3_hard((), "CE")) == "B"


def test_prompt_v3_canonical_normalization_for_paired_comparison() -> None:
    # functional correctness comparable across all three conditions.
    assert normalize_v3_verdict("easy", _v3_easy("AC"))["functional_correct"] is True
    assert normalize_v3_verdict("middle", _v3_middle("WA"))["functional_correct"] is False
    # outcome sets: easy NOT_AC and middle MIXED cannot be refined -> None.
    assert normalize_v3_verdict("easy", _v3_easy("NOT_AC"))["outcomes"] is None
    assert normalize_v3_verdict("middle", _v3_middle("MIXED"))["outcomes"] is None
    assert normalize_v3_verdict("middle", _v3_middle("WA"))["outcomes"] == frozenset({"WA"})
    assert normalize_v3_verdict("hard", _v3_hard(("RE", "TLE")))["outcomes"] == frozenset(
        {"RE", "TLE"}
    )
    assert normalize_v3_verdict("easy", _v3_easy("CE"))["ce"] is True


# --- experiment A selector ------------------------------------------------------


def test_v3a_stratified_selection_meets_quotas_and_exclusions(v3_source: Path) -> None:
    adapter = CodeJudgeEvalAdapter()
    samples = adapter.load_samples(v3_source)
    # Pretend task 100 and 104 were consumed by full-v1.
    exclude = frozenset({100, 104})
    selection = select_v3_experiment_a(
        samples,
        descriptor=adapter.descriptor,
        exclude_raw_task_ids=exclude,
        seed=42,
        ac_quota=3,
        non_ac_quota_per_class=2,
    )
    assert selection.status == V3_STATUS
    assert selection.experiment_id == "codejudge-eval-v3a-functional"
    entries = selection.entries
    # 3 AC + 5 non-AC classes x 2 = 13 entries, one candidate per task.
    assert len(entries) == 3 + 5 * 2
    assert len({entry.raw_task_id for entry in entries}) == len(entries)
    assert not {entry.raw_task_id for entry in entries} & exclude
    by_stratum: dict[str, int] = {}
    for entry in entries:
        by_stratum[entry.stratum] = by_stratum.get(entry.stratum, 0) + 1
    assert by_stratum == {"AC": 3, "CE": 2, "WA": 2, "RE": 2, "TLE": 2, "MIXED": 2}
    # The chosen candidate's gold actually belongs to the stratum.
    index = {s.candidate.candidate_id: s for s in samples}
    for entry in entries:
        gold = index[entry.candidate_id].gold
        expected_class = (
            "AC"
            if gold.functional_correct
            else ("MIXED" if gold.source_error_type == "MIXED" else gold.source_error_type)
        )
        assert expected_class == entry.stratum
    # Deterministic.
    again = select_v3_experiment_a(
        samples,
        descriptor=adapter.descriptor,
        exclude_raw_task_ids=exclude,
        seed=42,
        ac_quota=3,
        non_ac_quota_per_class=2,
    )
    assert again.entries == selection.entries
    assert again.entries_sha256 == selection.entries_sha256


def test_v3a_fails_closed_when_ac_pool_insufficient(v3_source: Path) -> None:
    adapter = CodeJudgeEvalAdapter()
    samples = adapter.load_samples(v3_source)
    with pytest.raises(CodeJudgeEvalError, match="AC"):
        select_v3_experiment_a(
            samples,
            descriptor=adapter.descriptor,
            exclude_raw_task_ids=frozenset(),
            seed=42,
            ac_quota=99,
            non_ac_quota_per_class=2,
        )


def test_v3a_preregistered_degradation_redistributes_deficit(tmp_path: Path) -> None:
    # Only 1 CE task; deficit must be redistributed in fixed class order and
    # recorded, never silently dropped.
    tasks: dict[int, list[str]] = {}
    task_id = 0
    for logical, count in (
        ("AC", 5),
        ("CE", 1),
        ("WA", 5),
        ("RE", 5),
        ("TLE", 5),
        ("WA+RE", 5),
    ):
        for _ in range(count):
            tasks[task_id] = [logical]
            task_id += 1
    source = write_v3_fixture(tmp_path, tasks)
    adapter = CodeJudgeEvalAdapter()
    samples = adapter.load_samples(source)
    selection = select_v3_experiment_a(
        samples,
        descriptor=adapter.descriptor,
        exclude_raw_task_ids=frozenset(),
        seed=7,
        ac_quota=4,
        non_ac_quota_per_class=3,
    )
    by_stratum: dict[str, int] = {}
    for entry in selection.entries:
        by_stratum[entry.stratum] = by_stratum.get(entry.stratum, 0) + 1
    assert by_stratum["CE"] == 1  # pool exhausted
    assert sum(v for k, v in by_stratum.items() if k != "AC") == 15  # total kept
    assert selection.degradation_log  # redistribution is on the record
    assert any("CE" in note for note in selection.degradation_log)


def test_v3a_hard_fail_when_total_pool_insufficient(tmp_path: Path) -> None:
    source = write_v3_fixture(tmp_path, {0: ["AC"], 1: ["WA"]})
    adapter = CodeJudgeEvalAdapter()
    samples = adapter.load_samples(source)
    with pytest.raises(CodeJudgeEvalError, match="pool"):
        select_v3_experiment_a(
            samples,
            descriptor=adapter.descriptor,
            exclude_raw_task_ids=frozenset(),
            seed=7,
            ac_quota=1,
            non_ac_quota_per_class=2,
        )


# --- experiment B selector ------------------------------------------------------


def test_v3b_paired_selection_same_candidates_three_granularities(v3_source: Path) -> None:
    adapter = CodeJudgeEvalAdapter()
    samples = adapter.load_samples(v3_source)
    selection_a = select_v3_experiment_a(
        samples,
        descriptor=adapter.descriptor,
        exclude_raw_task_ids=frozenset(),
        seed=42,
        ac_quota=2,
        non_ac_quota_per_class=1,
    )
    exclude = {entry.raw_task_id for entry in selection_a.entries}
    selection_b = select_v3_experiment_b(
        samples,
        descriptor=adapter.descriptor,
        exclude_raw_task_ids=frozenset(exclude),
        seed=43,
        per_hard_class_quota=1,
        ac_quota=2,
    )
    assert selection_b.status == V3_STATUS
    assert selection_b.experiment_id == "codejudge-eval-v3b-granularity"
    # 8 non-AC hard classes (CE included) x 1 + 2 AC controls = 10 candidates,
    # each judged under all three granularity conditions.
    assert len(selection_b.entries) == 10
    for entry in selection_b.entries:
        assert set(entry.gold_letters) == {"easy", "medium", "hard"}
        # The candidate is byte-identical across granularities: one hash.
        assert len(entry.candidate_code_sha256) == 64
        assert entry.raw_task_id not in exclude
        # Pre-registered Latin-square call order: a permutation of the three
        # granularity conditions, balancing condition against call position.
        assert sorted(entry.call_order) == ["easy", "hard", "middle"]
    first_positions = [entry.call_order[0] for entry in selection_b.entries]
    # Latin-square balance: each condition leads (and trails) ~equally often.
    for condition in ("easy", "middle", "hard"):
        assert abs(first_positions.count(condition) - len(selection_b.entries) / 3) <= 1
    again = select_v3_experiment_b(
        samples,
        descriptor=adapter.descriptor,
        exclude_raw_task_ids=frozenset(exclude),
        seed=43,
        per_hard_class_quota=1,
        ac_quota=2,
    )
    assert again.entries == selection_b.entries


def test_used_raw_task_ids_extraction() -> None:
    class _Stub:
        selected_task_ids = ("easy/135", "medium/83", "hard/171")

    assert used_raw_task_ids(_Stub()) == frozenset({135, 83, 171})


def test_v3_selection_serializes_roundtrip(v3_source: Path) -> None:
    adapter = CodeJudgeEvalAdapter()
    samples = adapter.load_samples(v3_source)
    selection = select_v3_experiment_a(
        samples,
        descriptor=adapter.descriptor,
        exclude_raw_task_ids=frozenset(),
        seed=42,
        ac_quota=2,
        non_ac_quota_per_class=1,
    )
    restored = CodeJudgeV3Selection.model_validate(json.loads(selection.model_dump_json()))
    assert restored == selection
