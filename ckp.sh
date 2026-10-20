#!/bin/sh
# Knowledge Distillation command-line entry point.
# 用法: ./ckp.sh <命令>（等价于 python -m ckp，自动使用 .venv 与 src/）
DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$DIR/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
PYTHONPATH="$DIR/src" exec "$PYTHON" -m ckp "$@"
