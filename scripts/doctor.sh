#!/usr/bin/env bash
# Environment and dependency diagnostics. Never prints secret values.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "[FEL ] Python-miljön saknas: python3 -m venv .venv && .venv/bin/pip install -r requirements.lock"
  exit 1
fi
echo "[INFO] Node                       $(node --version 2>/dev/null || echo 'saknas (behövs bara för att bygga dashboarden)')"
if curl -fsS -m 2 "http://127.0.0.1:${CLIPFORGE_PORT:-8000}/api/v1/health" >/dev/null 2>&1; then
  echo "[OK  ] API                        svarar på 127.0.0.1:${CLIPFORGE_PORT:-8000}"
else
  echo "[INFO] API                        körs inte"
fi
if curl -fsS -m 2 https://www.youtube.com/robots.txt >/dev/null 2>&1; then
  echo "[OK  ] Nätverk                    internet nåbart"
else
  echo "[INFO] Nätverk                    offline – upptäckt pausar tills nätet är tillbaka"
fi
cd "$ROOT/backend" && exec "$ROOT/.venv/bin/python" -m app.cli doctor
