from __future__ import annotations

import hashlib
import json
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
RELEASE_ROOT = REPO_ROOT / "docs/releases/phase4"
RELEASE_INDEX = RELEASE_ROOT / "README.md"
DEMO_SCRIPT = RELEASE_ROOT / "phase4_fixture_demo_v1.md"
RELEASE_CHECKLIST = RELEASE_ROOT / "phase4_release_checklist_v1.md"
CLOSURE_REPORT = RELEASE_ROOT / "phase4_closure_report_v1.md"
CHART_ROOT = RELEASE_ROOT / "charts/phase4_public_charts_v1"
CHART_MANIFEST = CHART_ROOT / "manifest.json"
IMPLEMENTATION_STATUS = REPO_ROOT / "IMPLEMENTATION_STATUS.md"
ROOT_README = REPO_ROOT / "README.md"
DESIGN_DOCUMENT = REPO_ROOT / (
    "TraceJudge-Hy3：基于四层对齐与可执行错误证书的代码生成过程评估系统.md"
)
PHASE4_PROTOCOL = REPO_ROOT / "docs/experiments/phase4_protocol.md"

CHART_HASHES = {
    "01_cohort_and_execution.svg": "33fc5806172729d2543280954fc09f2774aa13737ae5f922c35bd65905afe98c",
    "02_error_detection_by_source.svg": "a7020cb43b163fc52df533897bac72c4bef011691795ee0445899013808802b2",
    "03_preregistered_paired_comparisons.svg": "08b45448d1329b0c078365e68042f74ba54539e28a197c47bf210cf42b6a197f",
}
CHART_MANIFEST_SHA256 = "20d94ad514400ff7ebe72b8d288eb6a208b571069878091b4b6b481659f30d71"


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
    public_files = tuple(RELEASE_ROOT.rglob("*.md")) + tuple(RELEASE_ROOT.rglob("*.json"))
    combined = "\n".join(path.read_text(encoding="utf-8") for path in public_files)
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
    release_index = RELEASE_INDEX.read_text(encoding="utf-8")

    assert "phase3_research_report_public_v1.md" in release_index
    assert "phase3_research_report_publication_notes_v1.md" in release_index
    assert "charts/phase4_public_charts_v1/" in release_index
    assert "phase4_fixture_demo_v1.md" in release_index
    assert "phase4_release_checklist_v1.md" in release_index
    assert "phase4_closure_report_v1.md" in release_index
    assert "`ANALYZED / CAUTION / CANNOT_VERIFY`" in release_index
    assert "仓库内交付、审查和提交已完成" in release_index
    assert "目标分支已 push" in release_index
    assert "Pull Request #4" in release_index
    assert "merge、tag、Release" in release_index


def test_formal_chart_bundle_is_hash_bound_and_aggregate_only():
    manifest_payload = CHART_MANIFEST.read_bytes()
    manifest = json.loads(manifest_payload)

    assert _sha256(manifest_payload) == CHART_MANIFEST_SHA256
    assert manifest["chart_bundle_id"] == "phase4_public_charts_v1"
    assert manifest["source_statistics_id"] == "phase3_stats_primary_round1_v1"
    assert manifest["source_statistics_manifest_sha256"] == (
        "7efbdc9c36340593be09e192ea0e7b15297d5e69c4192fa4b49583558b368bf8"
    )
    assert manifest["source_statistics_report_sha256"] == (
        "972e7c0f5eac36d59035ec65376133fbcc0dfa941281e97fb7dcc70f02360a10"
    )
    assert manifest["cohort"]["trace_count"] == 57
    assert manifest["cohort"]["pair_count"] == 285
    assert manifest["cohort"]["valid_judgment_count"] == 283
    assert manifest["cohort"]["provider_error_count"] == 2
    assert manifest["contains_annotation_records"] is False
    assert manifest["contains_hidden_evaluation_content"] is False
    assert manifest["contains_per_trace_predictions"] is False
    assert manifest["contains_provider_raw"] is False
    assert manifest["contains_trace_ids"] is False
    assert manifest["verification_status"] == "ANALYZED"
    assert manifest["overall_confidence"] == "CAUTION"
    assert manifest["reproducibility"] == "CANNOT_VERIFY"

    assert {figure["filename"]: figure["sha256"] for figure in manifest["figures"]} == (
        CHART_HASHES
    )
    for filename, expected_sha256 in CHART_HASHES.items():
        assert _sha256((CHART_ROOT / filename).read_bytes()) == expected_sha256


def test_fixture_demo_is_mock_only_time_bounded_and_replays_public_certificate():
    demo = DEMO_SCRIPT.read_text(encoding="utf-8")

    assert "00:00–00:10" in demo
    assert "01:50–02:00" in demo
    assert ".venv/bin/tracejudge demo" in demo
    assert "--mock" in demo
    assert "--case faulty" in demo
    assert ".venv/bin/tracejudge phase3 replay" in demo
    assert "safe_mean" in demo
    assert "confirmed_bug" in demo
    assert "Provider、Docker 或网络" in demo
    assert "CANNOT_VERIFY" in demo
    assert "真实 Hy3" in demo
    assert not DEMO_SCRIPT.read_bytes().endswith(b"\n\n")


def test_gate_e_documents_preserve_scope_and_release_authority_boundary():
    checklist = RELEASE_CHECKLIST.read_text(encoding="utf-8")
    closure = CLOSURE_REPORT.read_text(encoding="utf-8")
    combined = checklist + closure

    assert "P0 REPOSITORY DELIVERABLES COMPLETE / PUBLICATION PENDING AUTHORIZATION" in closure
    assert "DEFERRED P1" in checklist
    assert "OUT OF SCOPE v0.2+" in checklist
    assert "PENDING AUTHORIZATION" in checklist
    assert "第二位标注者" in combined
    assert "至少间隔 7 天" in combined
    assert "完整 HumanEval+ 164 题" in combined
    assert "没有重跑 Hy3 主实验" in combined
    assert "没有重试两条 Provider 失败" in combined
    assert "ANALYZED / CAUTION / CANNOT_VERIFY" in combined
    assert "不代表已完成外部发布" in closure
    assert "仓库内审查和提交已经完成" in closure
    assert "Pull Request #4" in combined
    assert "push 目标分支并创建或更新 Pull Request" not in checklist
    assert "merge、tag、Release 和附件上传" in combined
    assert "审查并提交本检查单" not in checklist
    assert "git diff --check origin/main...HEAD -- ." in checklist
    assert "docs/releases/phase4/phase3_research_report_public_v1.md'" in checklist
    assert "cmp -s" in checklist
    assert REPORT_SHA256 in checklist
    assert not RELEASE_CHECKLIST.read_bytes().endswith(b"\n\n")
    assert "P1 已完成" not in combined
    assert "v0.2 已实现" not in combined


def test_implementation_status_marks_gate_e_content_complete_without_claiming_release():
    status = IMPLEMENTATION_STATUS.read_text(encoding="utf-8")

    assert "阶段四 P0 Gate E" in status
    assert "phase4_public_charts_v1" in status
    assert "P0 仓库内交付、审查和提交已完成" in status
    assert "目标分支已 push，Pull Request #4 已创建" in status
    assert "merge、tag、Release 和附件上传仍待" in status
    assert "交互式 Web UI 与跨运行可视化看板" in status


def test_canonical_project_documents_mark_gate_e_complete_without_claiming_release():
    readme = ROOT_README.read_text(encoding="utf-8")
    design = DESIGN_DOCUMENT.read_text(encoding="utf-8")
    protocol = PHASE4_PROTOCOL.read_text(encoding="utf-8")
    combined = readme + design + protocol

    assert "P0 Gate A、B、C、E" in readme
    assert "P0 Gate A、B、C、E 仓库内交付已完成" in design
    assert "P0 Gate B/C/E 封版版" in protocol
    assert "charts-verify" in readme
    assert "charts-verify" in protocol
    assert "P0 尚需聚合图表" not in combined
    assert "待完成 Gate E" not in combined
    assert "正式视频仍待阶段四 Gate E" not in combined
    assert "最后一项 Demo 仍是阶段四 Gate E" not in combined
    assert "Gate B 实现草案" not in protocol
    assert "目标分支已 push" in combined
    assert "Pull Request #4 已创建" in combined
    assert "merge、tag、Release 和附件上传" in combined
