#!/usr/bin/env bash
# Graceful shutdown: the worker stops claiming jobs, interrupted jobs are re-queued.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN="${CLIPFORGE_DATA_DIR:-$ROOT/data}/run"

stop_one() {
  local name="$1" pidfile="$RUN/$1.pid" timeout="$2"
  if [[ ! -f "$pidfile" ]]; then echo "$name körs inte."; return; fi
  local pid; pid="$(cat "$pidfile")"
  if ! kill -0 "$pid" 2>/dev/null; then rm -f "$pidfile"; echo "$name körs inte."; return; fi
  kill -TERM "$pid"
  for _ in $(seq 1 "$timeout"); do
    kill -0 "$pid" 2>/dev/null || { rm -f "$pidfile"; echo "$name stoppad."; return; }
    sleep 1
  done
  echo "$name svarade inte inom ${timeout}s; tvingar stopp (jobb återupptas vid nästa start)." >&2
  kill -KILL "$pid" 2>/dev/null || true
  rm -f "$pidfile"
}

stop_one worker 75
stop_one api 15
# Ollama is stopped last because the worker may still be asking it for clip candidates.
if [[ -f "$RUN/ollama.pid" ]]; then
  stop_one ollama 15
fi
