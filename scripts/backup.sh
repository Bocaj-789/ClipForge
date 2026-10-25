#!/usr/bin/env bash
# Consistent backup of the database (SQLite online backup API) and settings.
# Excludes media, clips, model weights, sessions and data/secrets.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA="${CLIPFORGE_DATA_DIR:-$ROOT/data}"
KEEP="${CLIPFORGE_BACKUP_KEEP:-10}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$DATA/backups/backup-$STAMP"
[[ -f "$DATA/clipforge.sqlite3" ]] || { echo "Ingen databas i $DATA" >&2; exit 1; }
mkdir -p "$OUT"
"$ROOT/.venv/bin/python" - "$DATA/clipforge.sqlite3" "$OUT/clipforge.sqlite3" <<'EOF'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.execute("DELETE FROM auth_sessions")
dst.commit()
rev = dst.execute("SELECT version_num FROM alembic_version").fetchone()
print(f"schema {rev[0] if rev else 'okänt'}")
dst.close(); src.close()
EOF
echo "{\"created_at\": \"$STAMP\", \"includes_secrets\": false}" >"$OUT/manifest.json"
echo "Säkerhetskopia: $OUT"
# Retention: keep the newest $KEEP backups.
ls -1d "$DATA"/backups/backup-* 2>/dev/null | sort -r | tail -n +"$((KEEP + 1))" | while read -r old; do
  rm -rf "$old"
done
