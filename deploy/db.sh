#!/usr/bin/env bash
set -euo pipefail
KD_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# Respect Docker Desktop's selected context; never force the cloud socket.
exec docker compose --project-name kd-local --project-directory "$KD_ROOT/deploy" --env-file "$KD_ROOT/.env" -f "$KD_ROOT/deploy/compose.yaml" "$@"
