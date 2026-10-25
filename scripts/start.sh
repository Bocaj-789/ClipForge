#!/usr/bin/env bash
# Start ClipForge (API + worker) in the background. Logs: data/logs/*.out
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
DATA="${CLIPFORGE_DATA_DIR:-$ROOT/data}"
RUN="$DATA/run"
PY="$ROOT/.venv/bin/python"
mkdir -p "$RUN" "$DATA/logs"

if [[ ! -x "$PY" ]]; then
  echo "Python-miljön saknas. Kör först: python3 -m venv .venv && .venv/bin/pip install -r requirements.lock" >&2
  exit 1
fi
if [[ ! -f "$ROOT/frontend/dist/index.html" ]]; then
  echo "Bygger dashboarden (första gången)…"
  (cd frontend && npm ci --no-fund --no-audit && npm run build) >"$DATA/logs/frontend-build.out" 2>&1 \
    || { echo "Frontend-bygget misslyckades, se $DATA/logs/frontend-build.out" >&2; exit 1; }
fi

running() { [[ -f "$1" ]] && kill -0 "$(cat "$1")" 2>/dev/null; }

PORT="${CLIPFORGE_PORT:-8000}"
if running "$RUN/api.pid"; then
  echo "API körs redan (pid $(cat "$RUN/api.pid"))."
else
  cd "$ROOT/backend"
  # Loopback only. Remote access must go through an authenticated tunnel.
  nohup "$PY" -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port "$PORT" --no-server-header \
    >>"$DATA/logs/api.out" 2>&1 &
  echo $! >"$RUN/api.pid"
  cd "$ROOT"
fi
for _ in $(seq 1 30); do
  curl -fsS "http://127.0.0.1:$PORT/api/v1/health" >/dev/null 2>&1 && break
  sleep 0.5
done
if ! curl -fsS "http://127.0.0.1:$PORT/api/v1/health" >/dev/null 2>&1; then
  echo "API:t svarar inte. Se $DATA/logs/api.out" >&2
  exit 1
fi
# The worker starts after the API so database migrations never run concurrently.
if running "$RUN/worker.pid"; then
  echo "Arbetaren körs redan (pid $(cat "$RUN/worker.pid"))."
else
  cd "$ROOT/backend"
  nohup "$PY" -m app.workers.main >>"$DATA/logs/worker.out" 2>&1 &
  echo $! >"$RUN/worker.pid"
  cd "$ROOT"
fi
echo "ClipForge körs: http://127.0.0.1:$PORT"
if [[ -f "$DATA/secrets/setup-token.txt" ]]; then
  echo "Första start – installationskod: $(cat "$DATA/secrets/setup-token.txt")"
fi
echo "Stoppa med: ./scripts/stop.sh"
