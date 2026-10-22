#!/bin/sh
# AI 解决方案生成 Agent 的 MCP 服务启动器（stdio）。
DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$DIR/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
PYTHONPATH="$DIR/src" exec "$PYTHON" -m ai_generate_solution.main