#!/bin/sh
# Knowledge Distillation MCP server启动器（stdio）。
# 用法：在 WorkBuddy 的自定义 MCP 配置中合并 knowledge-distillation.mcp.json（见该文件说明）。
# 数据目录默认 <仓库根>/data，可在 env 中用 CKP_DATA / CKP_OUTPUT 覆盖。
DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$DIR/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
PYTHONPATH="$DIR/src" exec "$PYTHON" -m ckp.mcp_server
