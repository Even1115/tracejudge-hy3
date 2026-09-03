#!/usr/bin/env bash
# 一键检查并启动 TraceJudge-Hy3 本地过程评估工作台。
# 用法：
#   ./scripts/run_demo.sh
#   PORT=8766 ./scripts/run_demo.sh
set -euo pipefail

cd "$(dirname "$0")/.."

DEMO_PYTHON=".venv/bin/python"
if [[ ! -x "$DEMO_PYTHON" ]]; then
  echo "错误：未找到 .venv/bin/python。请先执行：" >&2
  echo "  python3 -m venv .venv" >&2
  echo "  .venv/bin/pip install -e \".[dev]\"" >&2
  exit 1
fi

echo "正在执行 Demo readiness 检查…"
"$DEMO_PYTHON" -m tracejudge_hy3.demo_app.preflight

DEMO_PORT="${PORT:-8765}"
echo
echo "过程评估工作台：http://127.0.0.1:${DEMO_PORT}/"
echo "录制精简模式：http://127.0.0.1:${DEMO_PORT}/?recording=1"
echo "公开 Fixture 无需 API；真实 Hy3 模式仍要求服务端配置和 Docker。"
echo "按 Ctrl+C 停止服务。"
exec "$DEMO_PYTHON" -m tracejudge_hy3.demo_app.server --port "$DEMO_PORT"
