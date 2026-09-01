from __future__ import annotations

import hashlib
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
PUBLIC_REPORT = REPO_ROOT / "docs/releases/phase4/phase3_research_report_public_v1.md"
PUBLICATION_NOTES = (
    REPO_ROOT / "docs/releases/phase4/phase3_research_report_publication_notes_v1.md"
)
IGNORED_GATE_F_REPORT = REPO_ROOT / (
    "artifacts/experiments/phase3-reports/phase3_report_primary_round1_v1/phase3_research_report.md"
)
REPORT_SHA256 = "29eaef9f44a964308ab26b9821c472b0d13837eee587a3e687faa861edb4d725"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def test_public_report_is_hash_bound_and_matches_local_frozen_source_when_available():
    public_payload = PUBLIC_REPORT.read_bytes()

    assert _sha256(public_payload) == REPORT_SHA256
    if IGNORED_GATE_F_REPORT.exists():
        assert public_payload == IGNORED_GATE_F_REPORT.read_bytes()


def test_public_report_preserves_phase3_counts_and_research_boundaries():
    report = PUBLIC_REPORT.read_text(encoding="utf-8")

    assert "`valid_judgment=283`，`provider_error=2`" in report
    assert "Verification Status: **ANALYZED**" in report
    assert "Overall Confidence: **CAUTION**" in report
    assert "Reproducibility: **CANNOT_VERIFY**" in report
    assert "不能支持：完整 TraceJudge 普遍优于简单方法" in report


def test_publication_notes_bind_phase4_evidence_and_disclose_known_gap():
    notes = PUBLICATION_NOTES.read_text(encoding="utf-8")

    assert REPORT_SHA256 in notes
    assert "c1ba43dfe40b19af6929ddc9749a24f335933e22dad43ba626cbfc7c56e1d784" in notes
    assert "9094352967dbe90598d477c8abc0cdf6d0ac2dc311ab1d675b61d4460b477033" in notes
    assert "valid_only_confusion" in notes
    assert "没有重跑 Hy3 主实验" in notes
    assert "`ANALYZED`" in notes
    assert "`CAUTION`" in notes
    assert "`CANNOT_VERIFY`" in notes


def test_public_release_files_do_not_contain_secret_or_absolute_path_canaries():
    combined = PUBLIC_REPORT.read_text(encoding="utf-8") + PUBLICATION_NOTES.read_text(
        encoding="utf-8"
    )
    forbidden = (
        "/Users/",
        "Authorization:",
        "Bearer ",
        "OPENAI_API_KEY=",
        "HY3_API_KEY=",
        "sk-proj-",
    )

    for canary in forbidden:
        assert canary not in combined


def test_phase4_release_index_links_report_and_keeps_evidence_level_unchanged():
    release_index = (REPO_ROOT / "docs/releases/phase4/README.md").read_text(encoding="utf-8")

    assert "phase3_research_report_public_v1.md" in release_index
    assert "phase3_research_report_publication_notes_v1.md" in release_index
    assert "`ANALYZED / CAUTION / CANNOT_VERIFY`" in release_index
