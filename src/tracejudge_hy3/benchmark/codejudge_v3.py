"""Pre-registered stratified v3 experiments for CodeJudge-Eval (selectors only).

Two separate research questions, two separate confirmatory holdouts:

- **Experiment A** (``codejudge-eval-v3a-functional``): balanced functional
  correctness — stratified by gold class (AC + CE/WA/RE/TLE/MIXED), one
  candidate per task, labels from the hard (finest-granularity) file.
- **Experiment B** (``codejudge-eval-v3b-granularity``): paired label-
  granularity effect — the same (task, candidate) judged under all three
  granularity conditions of prompt bundle v3, with per-granularity gold
  derived deterministically.

Both holdouts exclude every raw task id used by the frozen full-v1 selection
(``selection_v2.json``); experiment B additionally excludes experiment A's
task ids.  No model is called here; selectors emit frozen manifests with
status ``preregistered-not-run``.  If a stratum pool cannot meet its quota,
the pre-registered degradation rule applies: AC shortfall fails closed;
non-AC deficits are redistributed in the fixed class order CE→WA→RE→TLE→MIXED
and recorded in ``degradation_log``; if the total non-AC pool is still short,
selection fails closed.
"""

from __future__ import annotations

import random
from typing import Protocol, Self

from pydantic import Field, model_validator

from .codejudge_eval import (
    DATASET_ID,
    ZERO_SHOT_FILES,
    CodeJudgeEvalError,
    CodeJudgeEvalSample,
)
from .contracts import (
    BenchmarkDataset,
    BenchmarkDifficulty,
    Sha256,
    StrictFrozenModel,
    canonical_sha256,
    ordered_ids_sha256,
)
from .judge_only_prompt_v3 import judge_only_prompt_v3_bundle_sha256

V3_STATUS = "preregistered-not-run"

V3A_EXPERIMENT_ID = "codejudge-eval-v3a-functional"
V3B_EXPERIMENT_ID = "codejudge-eval-v3b-granularity"

V3A_SEED = 20260905
V3B_SEED = 20260906

V3A_AC_QUOTA = 60
V3A_NON_AC_QUOTA_PER_CLASS = 12  # CE/WA/RE/TLE/MIXED -> 60 non-AC total

V3B_PER_HARD_CLASS_QUOTA = 5  # 8 non-AC hard classes (CE incl.) -> 40
V3B_AC_QUOTA = 10  # AC controls -> 50 candidates x 3 conditions

# Fixed non-AC stratum order for experiment A (deficits redistribute this way).
_NON_AC_STRATA = ("CE", "WA", "RE", "TLE", "MIXED")

# Hard-file letter -> class grouping used by experiment B (CE included).
_HARD_NON_AC_CLASSES: dict[str, str] = {
    "B": "CE",
    "C": "WA",
    "D": "RE",
    "E": "WA+RE",
    "F": "TLE",
    "G": "WA+TLE",
    "H": "RE+TLE",
    "I": "WA+RE+TLE",
}

# Experiment B judges each candidate under all three granularity conditions.
# A fixed easy->middle->hard order would confound condition with call position
# (prompt-caching / fatigue drift), so the pre-registered call order is a
# Latin square rotated deterministically by entry index: each condition leads,
# follows, and trails ~equally often, with no RNG involvement.
_V3B_LATIN_SQUARE: tuple[tuple[str, str, str], ...] = (
    ("easy", "middle", "hard"),
    ("middle", "hard", "easy"),
    ("hard", "easy", "middle"),
)


class _HasSelectedTaskIds(Protocol):
    @property
    def selected_task_ids(self) -> tuple[str, ...]: ...


def used_raw_task_ids(selection: _HasSelectedTaskIds) -> frozenset[int]:
    """Extract the raw (pool-level) task ids of a frozen selection."""

    return frozenset(int(task_id.split("/", 1)[1]) for task_id in selection.selected_task_ids)


class V3SelectionEntry(StrictFrozenModel):
    """One frozen v3 holdout candidate."""

    raw_task_id: int = Field(ge=0)
    data_id: int = Field(ge=0)
    candidate_id: str = Field(min_length=1)
    stratum: str = Field(min_length=1)
    candidate_code_sha256: Sha256
    # experiment B only: gold letters per granularity file.
    gold_letters: dict[str, str] = Field(default_factory=dict)
    # experiment B only: pre-registered Latin-square condition call order.
    call_order: tuple[str, ...] = ()
    source_record_sha256: Sha256


class CodeJudgeV3Selection(StrictFrozenModel):
    """Frozen v3 selection manifest; status is always preregistered-not-run."""

    selection_schema: str = "codejudge-eval-v3-selection-v1"
    experiment_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    dataset_id: str
    dataset_revision: str = Field(min_length=1)
    seed: int
    algorithm: str = Field(min_length=1)
    quotas: dict[str, int] = Field(min_length=1)
    excluded_raw_task_ids_sha256: Sha256
    file_sha256: dict[str, Sha256] = Field(min_length=1)
    prompt_bundle_sha256: Sha256
    entries: tuple[V3SelectionEntry, ...] = Field(min_length=1)
    entries_sha256: Sha256
    degradation_log: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        if self.status != V3_STATUS:
            raise ValueError("v3 selections are preregistered-not-run; status is fixed")
        if self.dataset_id != DATASET_ID:
            raise ValueError("v3 selection does not belong to CodeJudge-Eval")
        raw_ids = [entry.raw_task_id for entry in self.entries]
        if len(raw_ids) != len(set(raw_ids)):
            raise ValueError("one candidate per task: raw task ids must be unique")
        for entry in self.entries:
            if entry.call_order and sorted(entry.call_order) != ["easy", "hard", "middle"]:
                raise ValueError("call_order must permute the three granularity conditions")
        expected = canonical_sha256([entry.model_dump(mode="json") for entry in self.entries])
        if self.entries_sha256 != expected:
            raise ValueError("entries_sha256 does not bind entries")
        return self


def _stratum_of(sample: CodeJudgeEvalSample) -> str:
    gold = sample.gold
    if gold.functional_correct:
        return "AC"
    return gold.source_error_type  # hard file: CE/WA/RE/TLE/MIXED


def _redistribute(
    targets: dict[str, int], pools: dict[str, list[int]], order: tuple[str, ...]
) -> tuple[dict[str, int], list[str]]:
    """Pre-registered deficit redistribution in fixed class order."""

    log: list[str] = []
    final = dict(targets)
    deficit = 0
    for cls in order:
        available = len(pools.get(cls, ()))
        if available < final[cls]:
            deficit += final[cls] - available
            log.append(
                f"stratum {cls}: pool {available} below target {final[cls]}; "
                f"deficit {final[cls] - available} redistributed"
            )
            final[cls] = available
    for cls in order:
        if deficit == 0:
            break
        surplus = len(pools.get(cls, ())) - final[cls]
        take = min(surplus, deficit)
        if take > 0:
            final[cls] += take
            deficit -= take
            log.append(f"stratum {cls}: absorbed {take} redistributed slot(s)")
    return final, log


def select_v3_experiment_a(
    samples: tuple[CodeJudgeEvalSample, ...],
    *,
    descriptor: BenchmarkDataset,
    exclude_raw_task_ids: frozenset[int],
    seed: int = V3A_SEED,
    ac_quota: int = V3A_AC_QUOTA,
    non_ac_quota_per_class: int = V3A_NON_AC_QUOTA_PER_CLASS,
    prompt_bundle_sha256: str | None = None,
) -> CodeJudgeV3Selection:
    """Balanced functional-correctness holdout, labels from the hard file."""

    hard_samples = [
        sample
        for sample in samples
        if sample.task.difficulty is BenchmarkDifficulty.HARD
        and int(sample.task.identity.task_id.split("/", 1)[1]) not in exclude_raw_task_ids
    ]
    by_task: dict[int, list[CodeJudgeEvalSample]] = {}
    for sample in hard_samples:
        raw = int(sample.task.identity.task_id.split("/", 1)[1])
        by_task.setdefault(raw, []).append(sample)

    pools: dict[str, list[int]] = {"AC": [], **{cls: [] for cls in _NON_AC_STRATA}}
    for raw, task_samples in by_task.items():
        classes = {_stratum_of(sample) for sample in task_samples}
        for cls in classes:
            pools[cls].append(raw)
    for cls in pools:
        pools[cls] = sorted(set(pools[cls]))

    if len(pools["AC"]) < ac_quota:
        raise CodeJudgeEvalError(
            f"v3a AC pool has {len(pools['AC'])} tasks, below quota {ac_quota}; "
            "no degradation is pre-registered for the AC stratum"
        )
    targets = {cls: non_ac_quota_per_class for cls in _NON_AC_STRATA}
    final, degradation_log = _redistribute(targets, pools, _NON_AC_STRATA)
    total_non_ac = sum(final.values())
    if total_non_ac < sum(targets.values()):
        raise CodeJudgeEvalError(
            f"v3a non-AC pool total {total_non_ac} below target "
            f"{sum(targets.values())} even after redistribution"
        )

    rng = random.Random(seed)
    entries: list[V3SelectionEntry] = []
    used: set[int] = set()
    for cls in ("AC", *_NON_AC_STRATA):
        quota = ac_quota if cls == "AC" else final[cls]
        eligible = [raw for raw in pools[cls] if raw not in used]
        if len(eligible) < quota:
            raise CodeJudgeEvalError(
                f"v3a stratum {cls}: {len(eligible)} unused tasks below quota {quota}"
            )
        for raw in rng.sample(eligible, quota):
            used.add(raw)
            chosen = min(
                (s for s in by_task[raw] if _stratum_of(s) == cls),
                key=lambda s: int(s.candidate.candidate_id.rsplit("/", 1)[1]),
            )
            entries.append(
                V3SelectionEntry(
                    raw_task_id=raw,
                    data_id=int(chosen.candidate.candidate_id.rsplit("/", 1)[1]),
                    candidate_id=chosen.candidate.candidate_id,
                    stratum=cls,
                    candidate_code_sha256=chosen.candidate.code_sha256,
                    source_record_sha256=chosen.source_record_sha256,
                )
            )

    quotas = {"AC": ac_quota, **{cls: final[cls] for cls in _NON_AC_STRATA}}
    return CodeJudgeV3Selection(
        experiment_id=V3A_EXPERIMENT_ID,
        status=V3_STATUS,
        dataset_id=descriptor.dataset_id,
        dataset_revision=descriptor.revision,
        seed=seed,
        algorithm=(
            "codejudge_eval_v3a:hard_file_labels;exclude_full_v1_task_ids;"
            "one_candidate_per_task=min_data_id_in_stratum;"
            f"random.Random({seed}).sample per stratum,order=AC,CE,WA,RE,TLE,MIXED;"
            "preregistered_degradation=fixed_order_redistribution,AC_fail_closed"
        ),
        quotas=quotas,
        excluded_raw_task_ids_sha256=canonical_sha256(sorted(exclude_raw_task_ids)),
        file_sha256={s.source_file: s.source_file_sha256 for s in hard_samples[:1]} or {},
        prompt_bundle_sha256=prompt_bundle_sha256 or judge_only_prompt_v3_bundle_sha256(),
        entries=tuple(entries),
        entries_sha256=canonical_sha256([e.model_dump(mode="json") for e in entries]),
        degradation_log=tuple(degradation_log),
    )


def select_v3_experiment_b(
    samples: tuple[CodeJudgeEvalSample, ...],
    *,
    descriptor: BenchmarkDataset,
    exclude_raw_task_ids: frozenset[int],
    seed: int = V3B_SEED,
    per_hard_class_quota: int = V3B_PER_HARD_CLASS_QUOTA,
    ac_quota: int = V3B_AC_QUOTA,
    prompt_bundle_sha256: str | None = None,
) -> CodeJudgeV3Selection:
    """Paired granularity holdout: same candidates under all three conditions."""

    # Group by (raw task, data_id) across the three granularity files.
    by_key: dict[tuple[int, int], dict[BenchmarkDifficulty, CodeJudgeEvalSample]] = {}
    for sample in samples:
        raw = int(sample.task.identity.task_id.split("/", 1)[1])
        if raw in exclude_raw_task_ids:
            continue
        data_id = int(sample.candidate.candidate_id.rsplit("/", 1)[1])
        by_key.setdefault((raw, data_id), {})[sample.task.difficulty] = sample

    complete: dict[tuple[int, int], dict[BenchmarkDifficulty, CodeJudgeEvalSample]] = {}
    for key, per_difficulty in by_key.items():
        if set(per_difficulty) == set(ZERO_SHOT_FILES):
            codes = {s.candidate.code_sha256 for s in per_difficulty.values()}
            if len(codes) == 1:
                complete[key] = per_difficulty

    # Pool per hard gold class: tasks with at least one candidate of the class.
    pools: dict[str, list[int]] = {"AC": []}
    hard_class_by_key: dict[tuple[int, int], str] = {}
    for key, per_difficulty in complete.items():
        hard_gold = per_difficulty[BenchmarkDifficulty.HARD].gold
        letter = hard_gold.answer_letter
        cls = "AC" if hard_gold.functional_correct else _HARD_NON_AC_CLASSES[letter]
        hard_class_by_key[key] = cls
        pools.setdefault(cls, [])
    for cls in pools:
        pools[cls] = sorted({key[0] for key, c in hard_class_by_key.items() if c == cls})

    if len(pools["AC"]) < ac_quota:
        raise CodeJudgeEvalError(
            f"v3b AC pool has {len(pools['AC'])} tasks, below quota {ac_quota}"
        )
    non_ac_classes = tuple(_HARD_NON_AC_CLASSES.values())
    targets = {cls: per_hard_class_quota for cls in non_ac_classes}
    final, degradation_log = _redistribute(targets, pools, non_ac_classes)
    if sum(final.values()) < sum(targets.values()):
        raise CodeJudgeEvalError("v3b non-AC pool below target after redistribution")

    rng = random.Random(seed)
    entries: list[V3SelectionEntry] = []
    used: set[int] = set()
    for cls in ("AC", *non_ac_classes):
        quota = ac_quota if cls == "AC" else final[cls]
        eligible = [raw for raw in pools[cls] if raw not in used]
        if len(eligible) < quota:
            raise CodeJudgeEvalError(
                f"v3b class {cls}: {len(eligible)} unused tasks below quota {quota}"
            )
        for raw in rng.sample(eligible, quota):
            used.add(raw)
            chosen_key = min(
                (key for key in complete if key[0] == raw and hard_class_by_key[key] == cls),
                key=lambda key: key[1],
            )
            per_difficulty = complete[chosen_key]
            hard_sample = per_difficulty[BenchmarkDifficulty.HARD]
            entries.append(
                V3SelectionEntry(
                    raw_task_id=raw,
                    data_id=chosen_key[1],
                    candidate_id=hard_sample.candidate.candidate_id,
                    stratum=cls,
                    candidate_code_sha256=hard_sample.candidate.code_sha256,
                    gold_letters={
                        difficulty.value: per_difficulty[difficulty].gold.answer_letter
                        for difficulty in sorted(per_difficulty, key=lambda d: d.value)
                    },
                    call_order=_V3B_LATIN_SQUARE[len(entries) % 3],
                    source_record_sha256=hard_sample.source_record_sha256,
                )
            )

    quotas = {"AC": ac_quota, **{cls: final[cls] for cls in non_ac_classes}}
    return CodeJudgeV3Selection(
        experiment_id=V3B_EXPERIMENT_ID,
        status=V3_STATUS,
        dataset_id=descriptor.dataset_id,
        dataset_revision=descriptor.revision,
        seed=seed,
        algorithm=(
            "codejudge_eval_v3b:paired_same_candidate_three_granularities;"
            "exclude_full_v1_and_v3a_task_ids;stratify=hard_gold_class;"
            f"random.Random({seed}).sample per class;one_candidate_per_task=min_data_id;"
            "call_order=latin_square_rotation_by_entry_index_mod_3;"
            "preregistered_degradation=fixed_order_redistribution,AC_fail_closed"
        ),
        quotas=quotas,
        excluded_raw_task_ids_sha256=canonical_sha256(sorted(exclude_raw_task_ids)),
        file_sha256={
            ZERO_SHOT_FILES[d]: next(
                s.source_file_sha256 for s in samples if s.task.difficulty is d
            )
            for d in (
                BenchmarkDifficulty.EASY,
                BenchmarkDifficulty.MEDIUM,
                BenchmarkDifficulty.HARD,
            )
        },
        prompt_bundle_sha256=prompt_bundle_sha256 or judge_only_prompt_v3_bundle_sha256(),
        entries=tuple(entries),
        entries_sha256=canonical_sha256([e.model_dump(mode="json") for e in entries]),
        degradation_log=tuple(degradation_log),
    )


def entries_ordered_ids_sha256(selection: CodeJudgeV3Selection) -> str:
    """Ordered-id binding helper for downstream manifests."""

    return ordered_ids_sha256(tuple(entry.candidate_id for entry in selection.entries))
