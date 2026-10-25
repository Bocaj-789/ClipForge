# Operations

| Task | Command |
| --- | --- |
| Start | `./scripts/start.sh` (API first, then worker; migrations run on start) |
| Stop | `./scripts/stop.sh` (worker gets 75 s to checkpoint; interrupted jobs re-queue) |
| Diagnose | `./scripts/doctor.sh` |
| Agent state | `cd backend && ../.venv/bin/python -m app.cli status` |
| Backup | `./scripts/backup.sh` (keeps 10; `CLIPFORGE_BACKUP_KEEP` to change) |
| Reset password | `cd backend && ../.venv/bin/python -m app.cli reset-password <user>` |
| Dev mode | `./scripts/dev.sh` |

## Data layout (`data/`, gitignored)

`clipforge.sqlite3` database · `media/` uploaded/acquired sources · `import/` watched folder (never deleted) ·
`clips/`, `thumbnails/`, `captions/` outputs · `tmp/` work files (stale ones removed hourly and at worker start) ·
`models/` Whisper weights · `logs/` rotating JSON logs (5 × 5 MB per process) · `backups/` · `secrets/` (0700) ·
`run/` PID files.

## Restore a backup

1. `./scripts/stop.sh`
2. Move the live `data/clipforge.sqlite3*` files aside (don't delete them).
3. Copy `data/backups/backup-…/clipforge.sqlite3` to `data/clipforge.sqlite3`.
4. `./scripts/start.sh`; you'll need to log in again (sessions aren't backed up).

ClipForge never overwrites a live database itself, and never repairs a corrupt one automatically.

## Disk pressure

At the warning threshold (80 % of budget) the dashboard warns. At the critical threshold (90 %) or below the
minimum free disk (5 GB), the worker stops claiming downloads, transcriptions and renders; metadata and
discovery continue. Free space by deleting clips/videos in the dashboard, then the worker resumes on its own.

## Upgrades

Stop, back up, pull, `pip install -r requirements.lock`, `npm ci && npm run build`, start. Never upgrade while
a render is running; `stop.sh` waits for a checkpoint first.
