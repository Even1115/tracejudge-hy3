"""Safe downloadable result formats for completed in-memory demo runs."""

from __future__ import annotations

import html
from typing import Any

from tracejudge_hy3 import __version__


def result_export_payload(run_id: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "tracejudge_demo_result_export",
        "run_id": run_id,
        "tracejudge_version": __version__,
        "result": result,
    }


def certificate_export_payload(run_id: str, result: dict[str, Any]) -> dict[str, Any] | None:
    certificate = result.get("certificate")
    if not isinstance(certificate, dict):
        return None
    return {
        "schema_version": 1,
        "kind": "tracejudge_demo_certificate_export",
        "run_id": run_id,
        "tracejudge_version": __version__,
        "mode": result.get("mode"),
        "problem": result.get("problem"),
        "assessment": result.get("assessment"),
        "counterexample": result.get("counterexample"),
        "certificate": certificate,
        "replay": result.get("replay"),
        "evidence_boundary": result.get("mode_note"),
    }


def _value(value: Any) -> str:
    if value is None:
        return "—"
    return html.escape(str(value))


def render_result_html(run_id: str, result: dict[str, Any]) -> str:
    problem = result.get("problem") if isinstance(result.get("problem"), dict) else {}
    assessment = result.get("assessment") if isinstance(result.get("assessment"), dict) else {}
    provider = result.get("provider") if isinstance(result.get("provider"), dict) else {}
    certificate = result.get("certificate") if isinstance(result.get("certificate"), dict) else {}
    replay = result.get("replay") if isinstance(result.get("replay"), dict) else {}
    rows = [
        ("Run ID", run_id),
        ("TraceJudge version", __version__),
        ("模式", result.get("mode_note")),
        ("题目", f"{problem.get('title', '—')} ({problem.get('problem_id', '—')})"),
        ("Provider / model", f"{provider.get('name', '—')} / {provider.get('model') or '—'}"),
        ("Sandbox", provider.get("sandbox")),
        ("functional_correct", assessment.get("functional_correct")),
        ("process_correct", assessment.get("process_correct")),
        ("first_faulty_layer", assessment.get("first_faulty_layer")),
        ("first_faulty_step", assessment.get("first_faulty_step")),
        ("error_type", assessment.get("error_type")),
        ("certificate verdict", certificate.get("verdict")),
        ("证书重放", replay.get("reproduced")),
        ("耗时", f"{result.get('duration_seconds', '—')}s"),
    ]
    table = "".join(
        f"<tr><th>{html.escape(label)}</th><td>{_value(value)}</td></tr>" for label, value in rows
    )
    explanation = _value(assessment.get("explanation"))
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TraceJudge-Hy3 报告 · {_value(run_id)}</title>
<style>body{{font:16px/1.6 -apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;max-width:920px;margin:40px auto;padding:0 24px;color:#14202b}}h1{{font-size:28px}}.note{{padding:12px 16px;background:#eef7fa;border-left:4px solid #1687a7}}table{{width:100%;border-collapse:collapse;margin:24px 0}}th,td{{border:1px solid #d6e0e7;padding:10px;text-align:left;vertical-align:top}}th{{width:220px;background:#f5f8fa}}code{{font-family:ui-monospace,monospace}}small{{color:#5f7180}}</style>
</head><body><h1>TraceJudge-Hy3 人类可读报告</h1>
<p class="note">{_value(result.get("mode_note"))}。本报告仅复述当前本地 Demo 运行的白名单字段。</p>
<table>{table}</table><h2>判定依据</h2><p>{explanation}</p>
<small>配置与版本已随本报告记录；完整结构化内容请下载同一 Run ID 的 JSON。</small>
</body></html>"""
