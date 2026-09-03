"""Single source of truth for the public aggregate numbers shown by the demo page.

The recording demo's result-overview area must only show numbers that come from
published, hash-bound project materials.  This module centralizes that loading:

1. Preferred: the structured, hash-bound aggregate artifacts via
   ``phase4.contest_summary.build_contest_summary`` (requires the local
   Git-ignored frozen artifacts; aggregate counts only, never per-trace labels).
2. Fallback: the Git-tracked public Markdown documents
   ``docs/releases/phase4/phase4_contest_results_overview_v1.md`` and
   ``docs/releases/phase4/phase4_difficulty_proxy_analysis_v1.md``.

Both paths return the same normalized dict so the page never hand-writes
numbers, and ``tests/test_demo_app.py`` checks that the two sources agree when
both are available.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

OVERVIEW_MARKDOWN_RELATIVE_PATH = Path("docs/releases/phase4/phase4_contest_results_overview_v1.md")
DIFFICULTY_MARKDOWN_RELATIVE_PATH = Path(
    "docs/releases/phase4/phase4_difficulty_proxy_analysis_v1.md"
)

DIFFICULTY_TIERS = ("easy-proxy", "medium-proxy", "hard-proxy")

DISCLAIMER = (
    "单主标注者、固定 57 条 cohort 下的探索性结果；第二标注者一致性尚未计算，不构成普遍优越性结论。"
)


class OverviewSourceError(ValueError):
    """Raised when a public overview source is missing or no longer matches its
    expected published shape (e.g. a document edit drifted from the schema)."""


def _normalize_structured(summary: dict[str, Any]) -> dict[str, Any]:
    headline = summary["headline"]
    review = summary["human_review"]
    strata = summary["difficulty"]["strata"]
    return {
        "trace_count": headline["trace_count"],
        "pair_count": headline["pair_count"],
        "best_detection": {
            "method": headline["best_detection_method"],
            "numerator": headline["best_detection_numerator"],
            "denominator": headline["best_detection_denominator"],
            # Round to the same precision the published Markdown prints (0.1%).
            "accuracy": round(headline["best_detection_accuracy"], 3),
        },
        "full_false_positive_rate": {
            "numerator": headline["full_false_positive_numerator"],
            "denominator": headline["full_false_positive_denominator"],
            # Round to the same precision the published Markdown prints (0.01%).
            "rate": round(headline["full_false_positive_rate"], 4),
        },
        "human_review": {
            "primary_labeled": review["primary_labeled_trace_count"],
            "primary_total": review["primary_cohort_trace_count"],
            "second_completed": review["second_rater_completed_count"],
            "second_planned": review["second_rater_planned_subset_count"],
            "agreement_status": review["agreement_status"],
        },
        "difficulty": [
            {
                "tier": row["tier"],
                "included": row["included_natural_trace_count"],
                "passed": row["base_and_plus_pass_count"],
            }
            for row in strata
        ],
    }


def _require_match(pattern: str, text: str, *, label: str) -> re.Match[str]:
    match = re.search(pattern, text)
    if match is None:
        raise OverviewSourceError(f"published overview no longer contains {label}")
    return match


def parse_overview_markdown(text: str) -> dict[str, Any]:
    """Extract the four headline numbers and human-review coverage from the
    published contest-overview Markdown.  Fails loudly on drift."""

    headline = _require_match(
        r"\|\s*\*\*(\d+)\*\*（42 自然 \+ 15 反事实）\s*"
        r"\|\s*\*\*(\d+)\*\*（5 方法）\s*"
        r"\|\s*\*\*([\d.]+)%\*\*（(\d+)/(\d+)，([^）]+)）\s*"
        r"\|\s*\*\*([\d.]+)%\*\*（(\d+)/(\d+)）\s*\|",
        text,
        label="the four-headline-numbers row",
    )
    primary = _require_match(
        r"单主标注者盲法标签\s*\|\s*(\d+)/(\d+)\s*\|",
        text,
        label="the primary-rater coverage row",
    )
    second = _require_match(
        r"第二标注者独立复标\s*\|\s*(\d+)/(\d+)\s*\|[^|]*\|\s*尚未收集，agreement=`(\w+)`",
        text,
        label="the second-rater coverage row",
    )
    difficulty: list[dict[str, Any]] = []
    for tier in DIFFICULTY_TIERS:
        row = _require_match(
            rf"\|\s*{re.escape(tier)}\s*\|\s*(\d+)\s*\|\s*(\d+)/(\d+)（",
            text,
            label=f"the {tier} difficulty row",
        )
        included = int(row.group(1))
        passed = int(row.group(2))
        if int(row.group(3)) != included or passed > included:
            raise OverviewSourceError(f"the {tier} difficulty row is inconsistent")
        difficulty.append({"tier": tier, "included": included, "passed": passed})

    return {
        "trace_count": int(headline.group(1)),
        "pair_count": int(headline.group(2)),
        "best_detection": {
            "method": headline.group(6),
            "accuracy": float(headline.group(3)) / 100.0,
            "numerator": int(headline.group(4)),
            "denominator": int(headline.group(5)),
        },
        "full_false_positive_rate": {
            "rate": float(headline.group(7)) / 100.0,
            "numerator": int(headline.group(8)),
            "denominator": int(headline.group(9)),
        },
        "human_review": {
            "primary_labeled": int(primary.group(1)),
            "primary_total": int(primary.group(2)),
            "second_completed": int(second.group(1)),
            "second_planned": int(second.group(2)),
            "agreement_status": second.group(3),
        },
        "difficulty": difficulty,
    }


def _read_tracked_markdown(repo_root: Path, relative: Path) -> str:
    path = repo_root / relative
    if path.is_symlink() or not path.is_file():
        raise OverviewSourceError(f"missing published document: {relative.as_posix()}")
    return path.read_text(encoding="utf-8")


def _load_from_markdown(repo_root: Path) -> dict[str, Any]:
    overview = parse_overview_markdown(
        _read_tracked_markdown(repo_root, OVERVIEW_MARKDOWN_RELATIVE_PATH)
    )
    # The overview document already carries the difficulty rows; also parse the
    # dedicated analysis document and require the two public documents to agree.
    analysis_text = _read_tracked_markdown(repo_root, DIFFICULTY_MARKDOWN_RELATIVE_PATH)
    analysis_difficulty: list[dict[str, Any]] = []
    for tier in DIFFICULTY_TIERS:
        row = _require_match(
            rf"\|\s*{re.escape(tier)}\s*\|\s*\d+\s*\|\s*(\d+)\s*\|\s*\d+\s*\|\s*(\d+)/(\d+)\s*\|",
            analysis_text,
            label=f"the {tier} analysis row",
        )
        analysis_difficulty.append(
            {
                "tier": tier,
                "included": int(row.group(1)),
                "passed": int(row.group(2)),
            }
        )
        if int(row.group(3)) != int(row.group(1)) or int(row.group(2)) > int(row.group(1)):
            raise OverviewSourceError(f"the {tier} analysis row is inconsistent")
    if analysis_difficulty != overview["difficulty"]:
        raise OverviewSourceError(
            "the two published documents disagree on the difficulty-proxy rows"
        )
    return overview


def load_public_overview(repo_root: str | Path) -> dict[str, Any]:
    """Load the public aggregate overview, preferring hash-bound structured
    artifacts and falling back to the published Markdown documents."""

    root = Path(repo_root)
    structured: dict[str, Any] | None = None
    structured_error: Exception | None = None
    try:
        from tracejudge_hy3.phase4.contest_summary import build_contest_summary

        structured = _normalize_structured(build_contest_summary(root))
    except Exception as exc:  # frozen artifacts are Git-ignored and may be absent
        structured_error = exc

    markdown = _load_from_markdown(root)
    if structured is not None:
        if structured != markdown:
            raise OverviewSourceError(
                "structured aggregate artifacts and the published Markdown disagree; "
                "refusing to show inconsistent numbers"
            )
        return {**structured, "source": "structured_artifact", "disclaimer": DISCLAIMER}
    return {
        **markdown,
        "source": "published_markdown",
        "structured_source_unavailable": type(structured_error).__name__,
        "disclaimer": DISCLAIMER,
    }
