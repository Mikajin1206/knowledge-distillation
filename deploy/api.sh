#!/usr/bin/env bash
set -euo pipefail
KD_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$KD_ROOT"
export PYTHONPATH="$KD_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export KD_EMBEDDING_MODE=qwen
export LLM_PROVIDER=api
exec "$KD_ROOT/.venv/bin/python" -m uvicorn ai_generate_solution.api:app --host 127.0.0.1 --port 8001
