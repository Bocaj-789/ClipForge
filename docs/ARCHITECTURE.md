# Architecture

Modular monolith: two local processes sharing one SQLite database.

```
Browser (React/Vite build, Swedish UI)
   │  same-origin, cookie session + CSRF header
   ▼
API process   uvicorn app.main:create_app --factory   (127.0.0.1 only)
   │  enqueues jobs, never does heavy work in a request
   ▼
SQLite (WAL)  data/clipforge.sqlite3  ── Alembic migrations
   ▲
   │  claims jobs atomically (UPDATE … RETURNING), leases, heartbeats
Worker process   python -m app.workers.main
   ├─ lane "heavy": analyze → transcribe → extract_clips → render   (concurrency 1 by default)
   ├─ lane "io":    acquire (permitted direct downloads), publish (future)
   ├─ lane "light": fetch_metadata, discover, analytics (future)
   ├─ lease thread: persists progress, extends leases, picks up cancel requests
   └─ maintenance thread: heartbeat, health checks, lease recovery, scheduler, temp cleanup
```

## Decisions

| Area | Choice | Why |
| --- | --- | --- |
| Backend | Python 3.14, FastAPI, Pydantic 2 | Spec default; all deps have cp314 arm64 wheels (verified). |
| ORM | SQLAlchemy 2.1 (not SQLModel) + Alembic | One ORM, explicit migrations, batch mode for SQLite. |
| Queue | DB-backed, no Redis | Survives restarts; single atomic UPDATE claims a job. |
| Scheduler | `sources.next_check_at` in DB, polled by the worker | No process-local timers as the source of truth; idempotency keys prevent duplicates. |
| Transcription | faster-whisper (CTranslate2, int8, CPU) | Native arm64 wheel, word timestamps, VAD, model reuse. Not benchmarked against whisper.cpp yet. |
| Captions | Pillow PNG frames + ffmpeg `overlay` via a concat track | Homebrew's ffmpeg 8.1.1 is built without libass/freetype (`subtitles`/`drawtext` unavailable). Rasterising also removes subtitle-markup injection. |
| Encoding | libx264 (standard/HQ), h264_videotoolbox (fast draft) | Both present in the installed ffmpeg. |
| Clip AI | Deterministic heuristic (default) + optional Ollama | Works with no LLM; LLM output is schema-validated and mapped onto real sentence boundaries. |
| Auth | Single user, Argon2id, server-side sessions, CSRF header, Origin check | Required even locally; remote access later. |
| Frontend | React 19, TypeScript 6.0 (typescript-eslint doesn't support TS 7 yet), Vite 8, no UI kit | Small, typed, served by the API from `frontend/dist`. |

## Pipeline

1. **fetch_metadata** – YouTube oEmbed (keyless) for title/creator; decides the acquisition method via
   `services/acquisition/base.decide()`. YouTube URLs without a local file → blocked with an explanation.
2. **acquire** – SSRF-hardened streaming download (`services/safe_fetch.py`), only allowlisted hosts and
   confirmed rights; size cap; partial files removed on failure.
3. **analyze** – ffprobe validation (content, not file extension), duration cap, audio/video presence.
4. **transcribe** – faster-whisper with word timestamps; blocked (not failed) if the model is missing.
5. **extract_clips** – sentences from word timings and pauses → for each start sentence the best window
   within min/max duration → greedy non-overlapping selection → top N auto-rendered.
6. **render** – 9:16 crop or blurred-fit, caption track, loudnorm, encode to `data/tmp`, ffprobe-verify
   dimensions/duration/audio, poster frame, atomic `os.replace` into `data/clips`.

Every stage re-checks the rights gate. Stage outputs are persisted before the next job is enqueued.

## Job states

`pending → running → succeeded | retry_wait | failed | blocked | cancelled`, validated in
`services/queue/states.py`. Transient errors retry with exponential backoff and jitter (30 s base, 1 h cap,
`Retry-After` honoured); after `max_attempts` the job fails with `max_attempts_exceeded` (dead letter).
`blocked` means the user must act (rights, missing model, missing file). Expired leases (crash, sleep, kill -9)
return jobs to `retry_wait`. Daily render caps and shutdown use `defer`, which doesn't consume an attempt.

## Agent state

Desired state (`running/paused/draining/stopped`) is stored in `agent_control`. Effective state adds worker
liveness (heartbeat < 45 s) and degraded reasons (ffmpeg, model, Ollama, storage): `STOPPED` when no worker
is alive, `DEGRADED` when running with problems, `ERROR` for fatal worker reports.
