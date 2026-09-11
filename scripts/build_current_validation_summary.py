"""Generate or verify the Git-tracked latest-validation summary.

Offline only: reads the four pinned local reports (never calls a provider,
never executes candidate code) and writes the aggregate summary to
``docs/releases/current_validation_summary_v1.json`` so the demo page has a
publishable source even though ``artifacts/`` is Git-ignored.

Usage:
    python scripts/build_current_validation_summary.py          # regenerate
    python scripts/build_current_validation_summary.py --check  # verify, no write
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tracejudge_hy3.demo_app.current_validation import (  # noqa: E402
    SUMMARY_RELATIVE_PATH,
    CurrentValidationError,
    build_current_validation_summary,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the tracked summary matches a fresh regeneration (no write)",
    )
    args = parser.parse_args(argv)

    try:
        summary = build_current_validation_summary(REPO_ROOT)
    except CurrentValidationError as exc:
        print(f"汇总生成失败：{exc}", file=sys.stderr)
        return 1

    import json

    rendered = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    target = REPO_ROOT / SUMMARY_RELATIVE_PATH
    if args.check:
        if not target.is_file():
            print(f"校验失败：缺少 {SUMMARY_RELATIVE_PATH.as_posix()}", file=sys.stderr)
            return 1
        current = target.read_text(encoding="utf-8")
        if current != rendered:
            print(
                "校验失败：跟踪的汇总与绑定产物重新生成的结果不一致；"
                "请运行本脚本（不带 --check）重新生成，或检查实验材料是否被修改。",
                file=sys.stderr,
            )
            return 1
        print(f"校验通过：{SUMMARY_RELATIVE_PATH.as_posix()} 与四个绑定来源一致。")
        return 0

    target.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"已生成 {SUMMARY_RELATIVE_PATH.as_posix()}")
    for key, source in summary["sources"].items():
        print(f"  {key}: {source['path']} sha256={source['sha256'][:16]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
