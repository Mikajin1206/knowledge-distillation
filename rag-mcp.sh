#!/usr/bin/env bash
set -euo pipefail
KD_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$KD_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$KD_ROOT/.venv/bin/python" -m chunks_and_in_vector.mcp_server
