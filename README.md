# ClipForge

Local-first short-form clip workshop for a MacBook Pro M1. ClipForge takes long videos you have the rights to,
transcribes them locally, proposes self-contained 20–60 s moments with transparent scores, and renders vertical
9:16 MP4s with burned-in captions for you to review. It runs entirely on your Mac with no paid services.

Status of every feature: [docs/CHECKLIST.md](docs/CHECKLIST.md). Short version: the queue, rights gate, local
pipeline, dashboard, discovery, multi-channel YouTube connection and uploading work and are tested against a
simulated Google. **A real upload has not been verified yet** because it needs your Google Cloud credentials
(see [docs/YOUTUBE_SETUP.md](docs/YOUTUBE_SETUP.md)). YouTube analytics are not built yet.

## What it does not do

- It does not download videos from YouTube. There is no official API for that and ClipForge does not bypass
  platform terms. For your own videos, download the original from YouTube Studio and attach the file.
- It does not assume a public video may be clipped. Unknown rights block processing.
- It does not predict views. Scores are a ranking heuristic and say so.
- No paid AI APIs, no cloud compute, no DRM/paywall circumvention, no watermark removal, no fake engagement.

## Requirements

macOS on Apple Silicon (tested on macOS 27.0.1, arm64), Python 3.12+ (tested 3.14.5), FFmpeg (tested 8.1.1 from
Homebrew), Node 20+ only to build the dashboard (tested 26.7). About 1 GB for the app and dependencies, plus
the Whisper model you choose (base ≈ 145 MB) and room for media. See [docs/SETUP_MACOS.md](docs/SETUP_MACOS.md).

## Quick start

```bash
brew install ffmpeg                      # if missing
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
cp .env.example .env                     # optional; defaults are safe
(cd frontend && npm ci && npm run build)
./scripts/doctor.sh                      # checks everything, prints no secrets
./scripts/start.sh                       # API + worker in the background
```

Open http://127.0.0.1:8000. The first start prints a one-time setup code (also in
`data/secrets/setup-token.txt`); use it to create your account. Then go to **Inställningar → Transkribering**,
pick a model and allow its download (ClipForge never downloads weights without that switch).

Stop with `./scripts/stop.sh` (jobs in progress are re-queued and resume on the next start). Logs live in
`data/logs/` (`api.log`, `worker.log` are JSON with secrets redacted; `*.out` is raw process output).

## Using it

- **Import a local video:** Inkorg → *Ladda upp videofil*. Pick the rights status first; *Okänd* blocks processing.
- **Submit a URL:** paste it in Inkorg. *Brådskande* (default) puts it first in line when a worker is free.
  YouTube links fetch metadata only; attach the original file to process them. Direct media files are fetched
  only from hosts listed under Inställningar (default `archive.org`) and only with confirmed rights.
- **Watch folder:** add a *Importmapp* source under Upptäckt and drop files into `data/import/`. Files are read in
  place and never moved or deleted.
- **Feeds:** add a YouTube channel (by `youtube.com/channel/UC…` URL) as an RSS source for free, keyless new-upload
  discovery, or use the YouTube Data API source with `CLIPFORGE_YOUTUBE_API_KEY` set.
- **Review:** Klipp shows rendered clips. Approve, reject, edit the suggested title/description, download the MP4
  or the SRT. On a video's page you can render any other proposal with a different caption style or crop.
- **Pause/stop:** Översikt has *Pausa*, *Avsluta pågående och stanna* and *Nödstopp*.

## Priority

`0` urgent manual URL → `1` other manual submissions → `2` watched creators → `3` general discovery → `4`
maintenance. FIFO within a priority. Every fifth claim per lane takes the oldest job regardless of priority so
nothing starves. Running jobs are never interrupted by new arrivals. Heavy work (analysis, transcription,
rendering), downloads, and network discovery run in separate lanes so they don't block each other.

## Rights gate

Every video carries a rights status: `user_owned`, `licensed`, `public_domain_verified`,
`permission_confirmed` allow processing; `unknown` and `not_permitted` block acquisition, transcription,
rendering and (later) publishing. The check runs in every pipeline stage, not only at submission. Discovery
never grants rights; discovered videos inherit only what you declared for the source.

## Publishing
Press *Lägg till YouTube-kanal* on the Kanaler page as many times as you like to connect channels through Google's
OAuth consent screen (refresh tokens go to the macOS Keychain). Turn on automatic publishing and every finished
clip is uploaded to all connected channels, **privately** unless you explicitly allow public publishing per
channel. Each channel gets its own job with resumable upload, retries, duplicate protection and history; the
*Publicering* page shows progress, YouTube links, and whether a video is merely uploaded or actually public. The
deterministic policy (rights, approval, privacy opt-in, daily caps, API quota) gates every upload. One-time
Google Cloud setup, quota limits and troubleshooting: [docs/YOUTUBE_SETUP.md](docs/YOUTUBE_SETUP.md).
## Remote access

Off. The server binds to 127.0.0.1 and refuses any other bind address. See [docs/SECURITY.md](docs/SECURITY.md)
for the checklist before putting it behind a tunnel on your domain.

## Backup, upgrade, removal

- Backup: `./scripts/backup.sh` (or the button in Inställningar). Copies the database with SQLite's online
  backup API, strips sessions, never includes `data/secrets`, keeps the newest 10.
- Upgrade: `./scripts/stop.sh`, back up, `git pull`, `.venv/bin/pip install -r requirements.lock`,
  `(cd frontend && npm ci && npm run build)`, `./scripts/start.sh`. Migrations run automatically on start.
- Remove: stop, then delete the repository folder. All runtime data is under `data/` (or `CLIPFORGE_DATA_DIR`).

## Development

```bash
./scripts/dev.sh                          # API with reload, worker, Vite on :5173
.venv/bin/pytest                          # 111 backend tests (needs ffmpeg)
.venv/bin/ruff check backend && .venv/bin/mypy backend/app
cd frontend && npm test && npm run lint && npm run typecheck
```

More: [ARCHITECTURE](docs/ARCHITECTURE.md), [OPERATIONS](docs/OPERATIONS.md),
[TROUBLESHOOTING](docs/TROUBLESHOOTING.md).
