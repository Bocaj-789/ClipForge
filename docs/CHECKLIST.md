# Implementation checklist

Labels: **Done** (implemented and tested) · **Partial** · **Untested** (implemented, not fully verified) ·
**Planned** · **Blocked**. Last updated 2026-10-25.

## Phase 0–1 Audit and bootstrap — Done
- Empty repo; stack chosen as in the guide (see ARCHITECTURE.md). Python 3.14 compatibility verified for all deps.
- Backend/frontend skeleton, Alembic migration `0001`, health endpoint, doctor/start/stop/dev/backup scripts.
- Lint/type/test: ruff, mypy (clean), pytest; ESLint, tsc, Vitest (clean).

## Phase 2 Queue and manual input — Done
- Persistent queue, atomic claims, leases + recovery, backoff with jitter, Retry-After, dead-lettering,
  idempotency keys, lanes, anti-starvation, per-source/per-channel pause, cancel/retry/requeue.
- URL inbox with normalisation, SSRF policy, duplicate detection that raises priority, bulk submit.
- Local upload (streamed, ffprobe-validated, deduped by SHA-256); attach file to an existing video.
- Global pause/resume/drain/emergency stop; agent state with worker heartbeat and degraded reasons.

## Phase 3 Local media pipeline — Partial
- ffprobe analysis, duration/size caps, rights gate in every stage — Done.
- faster-whisper engine with consent-gated model download — **Untested with a real model** (no weights were
  downloaded without your consent; pipeline tests use a deterministic fake engine).
- Heuristic candidate extraction and transparent scoring — Done.
- Optional Ollama provider with schema validation and fallback — **Untested against a live Ollama**.
- Transcription/render benchmarks on the M1 — **Planned** (needs a model and real speech).

## Phase 4 Rendering — Done
- 9:16 center crop with manual offset, blurred-fit mode, 3 profiles, loudnorm, PNG caption track (clean, bold,
  karaoke) with å/ä/ö, SRT sidecar, poster frame, ffprobe verification, atomic move, cancellation, disk checks.
- Face/subject tracking — Planned (center crop shows a warning on landscape sources).

## Phase 5 Discovery — Partial
- RSS/Atom (incl. YouTube channel feeds), YouTube Data API uploads-playlist adapter, local import folder,
  persistent scheduling with jitter and failure backoff, daily cap, quota handling — Done (adapters tested with
  mocks; RSS + oEmbed verified live once).
- YouTube Data API with a real key — **Untested** (no key configured).

## Phase 6 Channel management — Partial
- Channel configuration UI/API, schedules (DST-safe next slot), caps, templates, typed confirmation for
  auto-publish — Done.
- Deterministic publication policy (kill switch, caps, spacing, rights, review, media) — Done (unit tested).
- OAuth 2.0 (PKCE, single-use state bound to the browser), several channels, Keychain token storage — Done
  (tested against a simulated Google; **not yet verified with real Google credentials**).
- Resumable upload, retries with backoff, cancellation between chunks, reconciliation of lost answers,
  duplicate protection (DB unique pair + persisted session + job idempotency), quota ledger — Done (simulated).
- Upload vs. public availability tracked separately and read back from YouTube — Done (simulated).
## Phase 7 Multi-channel autopilot — Partial
- Automatic publishing of every finished clip to all connected channels (private by default) — Done.
- Real-world run against YouTube — **Needs your Google Cloud client** (docs/YOUTUBE_SETUP.md, section 6).
- YouTube Analytics / learning from performance — **Planned**.

## Phase 8 Analytics and learning — Partial
- Local processing metrics (renders/day, review counts, real-time factors) — Done.
- YouTube Analytics snapshots and the capped performance adjustment — **Planned**. The score field and cap
  (±10) exist; nothing feeds it yet.

## Phase 9 Domain and remote access — Blocked
Needs your domain and DNS provider. Checklist in SECURITY.md.

## Phase 10 Hardening — Partial
- Restart/recovery, graceful stop, disk thresholds, backup verified manually on 2026-10-25.
- Restore procedure documented, not automated. Expired-token, quota and lost-answer cases are covered by simulated-Google tests.
- Dashboard verified by component tests and a live API run; **not visually reviewed in a browser** (no headless
  browser on this Mac).
