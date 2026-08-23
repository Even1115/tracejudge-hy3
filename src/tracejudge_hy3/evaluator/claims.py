"""Small, conservative helpers for matching claims in user-facing solution text.

These helpers deliberately require wording that describes an *input condition*.
For example, "initialize an empty result list" must not be mistaken for a claim
that the implementation handles empty input.
"""

from __future__ import annotations

import re

_EMPTY_INPUT_PHRASES = (
    "空输入",
    "空列表输入",
    "空数组输入",
    "空序列输入",
    "输入为空",
    "输入是空",
    "输入若为空",
    "输入如果为空",
    "列表为空",
    "数组为空",
    "序列为空",
    "为空列表",
    "为空数组",
    "为空序列",
)

_EMPTY_INPUT_ENGLISH_RE = re.compile(
    r"\b(?:empty[- ](?:input|argument|sequence)|"
    r"(?:input|argument|list|array|sequence)\s+is\s+empty|"
    r"(?:if|when)\s+(?:the\s+)?(?:input|argument|list|array|sequence)\s+"
    r"(?:is\s+)?empty)\b",
    re.IGNORECASE,
)


def claims_empty_input_handling(text: str) -> bool:
    """Return whether *text* explicitly claims to handle an empty input case."""

    normalized = " ".join(text.split())
    return any(phrase in normalized for phrase in _EMPTY_INPUT_PHRASES) or bool(
        _EMPTY_INPUT_ENGLISH_RE.search(normalized)
    )


def claims_explicit_empty_input_branch(text: str) -> bool:
    """Whether text claims an explicit guard/branch for empty input.

    A branch-free implementation can legitimately map an empty input to the
    right output (for example ``return list(items)``). Therefore the AST rule
    may only demand a branch when the write-up explicitly claims one.
    """

    if not claims_empty_input_handling(text):
        return False
    lowered = text.lower()
    explicit_markers = (
        "检查",
        "判断",
        "如果",
        "若",
        "当",
        "直接返回",
        "提前返回",
        "分支",
        "check",
        " if ",
        "when ",
        "guard",
        "early return",
    )
    padded = f" {lowered} "
    return any(marker in padded for marker in explicit_markers)
