#!/usr/bin/env bash
# 兼容入口：转交给带 readiness 检查的一键 Demo 启动脚本。
# 用法：
#   scripts/run_recording_demo.sh            # 默认端口 8765
#   PORT=8766 scripts/run_recording_demo.sh  # 自定义端口
# 该脚本不启动录屏工具，也不调用真实 Hy3。
set -euo pipefail

cd "$(dirname "$0")/.."
exec ./scripts/run_demo.sh
