"""Readiness checks for the one-command local contest demo."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tracejudge_hy3 import __version__

from .overview import load_public_overview
from .regression import build_regression_card
from .runner import demo_status
from .showcase import SHOWCASE_SVG_PATH, load_public_showcase


def demo_preflight(repo_root: str | Path) -> dict[str, Any]:
    root = Path(repo_root)
    checks: list[dict[str, Any]] = []

    def require(condition: bool, detail: str) -> str:
        if not condition:
            raise ValueError(detail)
        return detail

    def run_check(name: str, required: bool, operation: Callable[[], object]) -> None:
        try:
            detail = operation()
            checks.append({"name": name, "required": required, "ok": True, "detail": str(detail)})
        except Exception as exc:
            checks.append(
                {
                    "name": name,
                    "required": required,
                    "ok": False,
                    "detail": type(exc).__name__,
                }
            )

    run_check(
        "Python >= 3.11",
        True,
        lambda: require(sys.version_info >= (3, 11), sys.version.split()[0]),
    )
    run_check("TraceJudge import", True, lambda: f"v{__version__}")
    run_check("公开汇总一致性", True, lambda: f"{load_public_overview(root)['trace_count']} traces")
    run_check("典型案例公开源", True, lambda: f"{len(load_public_showcase(root)['cases'])} cases")
    run_check("版本化回归卡片", True, lambda: build_regression_card(root)["evaluator_version"])
    run_check(
        "前端静态资源",
        True,
        lambda: require(
            all(
                (root / "src/tracejudge_hy3/demo_app/static" / name).is_file()
                for name in ("index.html", "app.js", "styles.css")
            ),
            "index/app/styles",
        ),
    )
    run_check("reasoning_swap 展示图", True, lambda: (root / SHOWCASE_SVG_PATH).stat().st_size)
    run_check(
        "artifacts 目录可写",
        True,
        lambda: require(os.access(root / "artifacts", os.W_OK), "writable"),
    )
    status: dict[str, Any] = {}
    run_check("Demo 模式检查", True, lambda: status.update(demo_status()) or "fixture ready")
    if status:
        hy3 = status["modes"]["hy3"]
        checks.append(
            {
                "name": "真实 Hy3（可选）",
                "required": False,
                "ok": bool(hy3["available"]),
                "detail": "ready"
                if hy3["available"]
                else "未配置或 Docker 不可用；Fixture 不受影响",
            }
        )
    ready = all(item["ok"] for item in checks if item["required"])
    return {"ready": ready, "tracejudge_version": __version__, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    result = demo_preflight(".")
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"TraceJudge-Hy3 v{result['tracejudge_version']} Demo readiness")
        for item in result["checks"]:
            mark = "PASS" if item["ok"] else ("WARN" if not item["required"] else "FAIL")
            print(f"[{mark}] {item['name']}: {item['detail']}")
        print("READY" if result["ready"] else "NOT READY")
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
