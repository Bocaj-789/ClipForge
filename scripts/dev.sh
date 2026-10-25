#!/usr/bin/env bash
# Development mode: API with reload + worker + Vite dev server. Ctrl-C stops everything.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
export CLIPFORGE_DEV_MODE=true
trap 'kill 0' EXIT
(cd "$ROOT/backend" && "$PY" -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --reload) &
(cd "$ROOT/backend" && "$PY" -m app.workers.main) &
(cd "$ROOT/frontend" && npm run dev) &
wait
