#!/usr/bin/env bash
set -euo pipefail
KD_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$KD_ROOT"
mkdir -p .cloud/logs .cloud/run
case "${1:-status}" in
  start)
    if [[ -f .cloud/run/api.pid ]] && kill -0 "$(cat .cloud/run/api.pid)" 2>/dev/null; then
      KD_ARGS="$(ps -p "$(cat .cloud/run/api.pid)" -o args=)"
      if [[ "$KD_ARGS" == *'uvicorn ai_generate_solution.api:app'* ]]; then
        echo 'Development API already running'; exit 0
      fi
    fi
    # Explicit fixture default; qwen runs in a separate vector namespace.
    nohup setsid env PYTHONPATH="$KD_ROOT/src" KD_EMBEDDING_MODE="${KD_EMBEDDING_MODE:-fixture}" \
      "$KD_ROOT/.venv/bin/python" -m uvicorn ai_generate_solution.api:app \
      --host 127.0.0.1 --port 8001 > .cloud/logs/api.log 2>&1 < /dev/null &
    echo "$!" > .cloud/run/api.pid
    for KD_ATTEMPT in {1..15}; do
      if curl --noproxy '*' -fsS http://127.0.0.1:8001/health >/dev/null 2>&1; then
        echo 'Development API ready: http://127.0.0.1:8001/docs'; exit 0
      fi
      sleep 1
    done
    echo 'Development API failed; inspect .cloud/logs/api.log' >&2; exit 1
    ;;
  stop)
    if [[ -f .cloud/run/api.pid ]]; then
      KD_PID="$(cat .cloud/run/api.pid)"
      if kill -0 "$KD_PID" 2>/dev/null; then
        KD_ARGS="$(ps -p "$KD_PID" -o args=)"
        if [[ "$KD_ARGS" == *'uvicorn ai_generate_solution.api:app'* ]]; then kill "$KD_PID"; fi
      fi
      rm -f .cloud/run/api.pid
    fi
    ;;
  status) curl --noproxy '*' -fsS http://127.0.0.1:8001/health; echo ;;
  *) echo 'Usage: dev-services.sh start|stop|status' >&2; exit 2 ;;
esac
