#!/usr/bin/env bash
# Start ClipForge (API + worker) in the background. Logs: data/logs/*.out
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Optional private runtime configuration; kept outside the repository.
CONFIG_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/clipforge/clipforge.env"
if [[ -f "$CONFIG_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$CONFIG_FILE"
  set +a
fi

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

# Optional local LLM. Started only when nothing already answers on its port, so an Ollama app or
# service the user runs themselves is left alone (and stop.sh only stops what this script started).
if command -v ollama >/dev/null 2>&1; then
  if curl -fsS -m 2 "http://127.0.0.1:11434/api/tags" >/dev/null 2>&1; then
    echo "Ollama körs redan."
  elif running "$RUN/ollama.pid"; then
    echo "Ollama körs redan (pid $(cat "$RUN/ollama.pid"))."
  else
    nohup ollama serve >>"$DATA/logs/ollama.out" 2>&1 &
    echo $! >"$RUN/ollama.pid"
    echo "Ollama startad (pid $!). Om den inte svarar används heuristik."
  fi
fi

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
