# Implementation Plan: real download progress bar

Goal: the ACQUIRE job's progress_percent follows the real yt-dlp transfer (bytes done vs total), is persisted during the download, and the dashboard shows a determinate bar with a visible percentage. Cancellation keeps working.

No project files were edited while writing this plan. No approved design document was supplied, so the decisions below are mine, grounded in the code I read.

## Current data path (verified by reading code)

1. yt-dlp calls `progress_hooks` in `YouTubeProvider.acquire` (backend/app/services/acquisition/youtube.py). `build_options` already wires the hook, so no change is needed to `build_options` or `_options`.
2. The hook calls `request.progress`, which is `ctx.progress` from `handle_acquire` (backend/app/services/pipeline.py). It sets `state["progress"]` in `Worker.execute` (backend/app/workers/main.py). The hook only writes in memory.
3. `Worker.lease_loop` (backend/app/workers/main.py) runs every 2 s and calls `queue.service.heartbeat`, which sets `job.progress_percent` (rounded to 0.1) and `job.stage`. The API already serves `progress_percent` through `serializers.job`.
4. Frontend pages poll (Queue every 3 s, Inbox every 4 s, VideoDetail every 4 s) and render the shared `Progress` component in frontend/src/components/ui.tsx.

Defects found:

- A. The hook computes `min(88, 88 * done / total)` per stream and keeps a peak. The first stream reaches the cap, so the value stays pinned at 88 while later bytes arrive. Live check: job 2838c55b743c4d4d9be7484d40fc6fd4 showed progress_percent 88.0 while its `.part` file grew from 144,816,576 to 160,848,659 bytes in 10 s (about 1.6 MB/s).
- B. `Progress` shows no value, and its width is clamped to at least 2%, so a real 0% looks like 2%.
- C. Nothing in the worker or queue path needs to change. Persistence already works; the hook only needs to report a correct number.

## Decisions

- D1. Aggregate bytes across all streams. The fraction is the sum of bytes done over the expected total. The expected total is the sum of `filesize` or `filesize_approx` in `info["requested_formats"]` (or `info` itself for a single format). Per-stream capping is what pinned the bar, so it goes.
- D2. Map the download into 2 to 90 percent. 2 keeps the existing start value. 90 to 100 is reserved for yt-dlp's merge, probe and move, which the user asked us to leave out of the bar.
- D3. Keep the hook free of I/O and database writes. The existing 2 s lease flush persists the value, and a throttle is only a cheap "changed by 0.1 or more" check.
- D4. Extend the shared `Progress` component with an opt-in label and value props. Publications gets no label because the props default to off. It does pick up the 0 to 2% floor removal from D5.
- D5. Remove the 2% width floor so the bar matches the real value.
- D6. Leave the cancel path alone. The first line of the hook still raises `DownloadCancelled` before anything else.
- D7. Do not change download speed settings in this task (cachedir, concurrency, resolution). They change load and output, so they are follow-ups that need your approval. See the Answers section.
- D8. No new repo tests. Verification reuses the existing real-yt-dlp integration tests and one temporary script in /tmp that gets deleted afterwards.

## Plan

- [ ] 1. Replace the progress math in `YouTubeProvider.acquire` with stream-aggregated, monotonic percent.
      Add module constants `DOWNLOAD_START = 2.0` and `DOWNLOAD_END = 90.0`. After the `extract_info(download=False)` checks, compute `expected_total` from `info["requested_formats"]` (or `info` if absent). Use `None` when any size is missing. Replace the `stream_sizes` / `peak` logic with these closure variables: `stream_done: dict[str, int]`, `stream_total: dict[str, int]`, `peak: float` (initial `DOWNLOAD_START`, updated with `nonlocal`).
      Hook order and rules:
      - Keep `if request.cancelled(): raise DownloadCancelled("cancelled")` as the first line.
      - Accept only `status` in ("downloading", "finished"). Key streams by `str(d.get("filename"))`, as the current code does.
      - On "downloading": `stream_done[name] = max(previous, downloaded_bytes or 0)`. If a total exists (`total_bytes` or `total_bytes_estimate`, nonzero), set `stream_total[name] = max(total, stream_done[name])`, so a low estimate never produces a fraction above 1.
      - Keep the existing `max_bytes` check, now summing `stream_total.values()`, in the same place.
      - On "finished": set `stream_done[name] = stream_total[name] = total_bytes` (or the known total) when it is nonzero.
      - Numerator and denominator: with `expected_total`, use `sum(stream_done.values()) / expected_total`. Without it, use only streams that have a known total, `sum(stream_done[k] for k in stream_total) / sum(stream_total.values())`.
      - If the denominator is 0, return without reporting. Clamp the fraction to [0, 1].
      - `value = DOWNLOAD_START + (DOWNLOAD_END - DOWNLOAD_START) * fraction`. Report only if `value > peak + 0.1`, then set `peak = value` and call `on_progress(round(value, 1), "hämtar")`.
      Leave `on_progress(2.0, "hämtar")` before `process_ie_result` in place. Keep `pipeline.handle_acquire` unchanged.
      Files: backend/app/services/acquisition/youtube.py
      Verify: `.venv/bin/pytest backend/tests/integration/test_ingestion.py -k yt_dlp` passes (this includes the real download through a local server, the MEDIA_TOO_LARGE path, and the cancel path that must raise `Cancelled`). Then `.venv/bin/ruff check backend` and `.venv/bin/mypy backend/app` report no new findings.

- [ ] 2. Exercise the hook math with a one-off script, then delete it.
      Write /tmp/clipforge_hook_check.py. It builds `YouTubeProvider(ydl_factory=Fake)` with a fake `YoutubeDL` whose `extract_info` returns `requested_formats` for a video stream and an audio stream, and whose `process_ie_result` calls every hook in `opts["progress_hooks"]` with synthetic events. The `progress` callback collects values. Use `YT = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"` and a `MediaRequest` built the same way as `_request` in test_ingestion.py.
      Assert these cases:
      (a) Video then audio, with sizes from `requested_formats`: values never decrease, stay within [2, 90], and the value after the video finishes is near 90 * video / total, not 88.
      (b) No sizes at all: no exception, no division by zero, no callback.
      (c) `total_bytes_estimate` smaller than downloaded: value stays at or below 90.
      (d) Cancel: the `cancelled` callable returns True mid-stream, so `acquire` raises `Cancelled`.
      Run it with `.venv/bin/python /tmp/clipforge_hook_check.py`, expect exit 0, then `rm /tmp/clipforge_hook_check.py`.
      Files: none in the repo (temporary /tmp file, deleted)
      Verify: exit code 0 with all assertions passing. The file no longer exists.

- [ ] 3. Give the shared `Progress` component a truthful bar, an optional label and an optional visible value. Add the percent unit to i18n.
      New signature: `Progress({ value, label, showValue = false })`. Clamp `pct = Math.min(100, Math.max(0, value))`. Remove the 2% floor and use `width: ${pct}%`. Keep `role="progressbar"`, `aria-valuemin={0}`, `aria-valuemax={100}`, and `aria-valuenow={Math.round(pct)}`. Add `aria-label={label}` and `aria-valuetext` using `t.common.percent`.
      With `showValue`, wrap the bar and a visible label in `<span className="progress-wrap">`. The label is `<span className="progress-value num" aria-hidden="true">{Math.round(pct)} {t.common.percent}</span>`.
      In frontend/src/i18n/sv.ts, add `percent: "%"` under `common`, next to `seconds: "s"`.
      Files: frontend/src/components/ui.tsx, frontend/src/i18n/sv.ts
      Verify: `cd frontend && npm run typecheck` passes.

- [ ] 4. Style the value label and turn it on for the three job views.
      Add `.progress-wrap { display: inline-flex; align-items: center; gap: 0.5rem; }` and `.progress-value { min-width: 3.5em; text-align: right; }` right after the `.progress > span` rule in frontend/src/styles/app.css (around line 436).
      Pass `showValue` and `label` at these call sites, all of which render the running job's `progress_percent`:
      - frontend/src/pages/Queue.tsx, around line 100: `label={t.pipeline.stages[j.job_type] ?? j.job_type}`
      - frontend/src/pages/Inbox.tsx, around line 51: `label={t.pipeline.stages[j.job_type]}`
      - frontend/src/pages/VideoDetail.tsx, around line 202: `label={t.pipeline.stages[j.job_type]}`
      Leave frontend/src/pages/Publications.tsx unchanged.
      Files: frontend/src/styles/app.css, frontend/src/pages/Queue.tsx, frontend/src/pages/Inbox.tsx, frontend/src/pages/VideoDetail.tsx
      Verify: `cd frontend && npm run typecheck && npm run lint && npm test` all pass.

- [ ] 5. Confirm the persistence path needs no code change.
      Do not edit backend/app/workers/main.py, backend/app/services/pipeline.py, or backend/app/services/queue/service.py. Those files already contain uncommitted changes from other work, so do not touch them.
      Files: none
      Verify: `.venv/bin/pytest backend/tests/unit/test_queue.py` passes. It includes the heartbeat test (around line 142) that checks `progress_percent` is persisted.

- [ ] 6. Full verification with the project's real commands.
      Backend: `.venv/bin/ruff check backend`, `.venv/bin/mypy backend/app`, `.venv/bin/pytest` (README says 111 tests, which need ffmpeg on PATH).
      Frontend: `cd frontend && npm test && npm run lint && npm run typecheck && npm run build`. The build rewrites `frontend/dist`, which the backend serves, so the served UI needs this step.
      Files: none
      Verify: every command exits 0.

- [ ] 7. Live check (needs your go-ahead first).
      Restarting the worker is required so the new yt-dlp code loads. Restarting also interrupts the download in progress (job 2838c55b…). Per README, interrupted jobs are re-queued and resume from partial files. Once you confirm: `./scripts/stop.sh`, then `./scripts/start.sh`, then submit a YouTube URL whose rights you have confirmed. Poll `GET /api/v1/jobs/<id>` and check that `progress_percent` rises while the `.part` file in `data/tmp/job-<id>/` grows, then check that the Inbox or Queue bar shows the same number.
      Files: none
      Verify: manual. Not verifiable in this environment because it needs a real YouTube download.

## Cancellation (unchanged on purpose)

- The hook's first statement still checks `request.cancelled()` and raises `DownloadCancelled`, so every progress event is a cancel point.
- `acquire` already maps `DownloadCancelled` to `Cancelled`. `Worker.execute` already handles that. The cancel flag is set by the 2 s lease heartbeat, unchanged.
- The regression check is the existing `test_yt_dlp_download_limits_and_failures_for_real` (cancelled=lambda: True expects `Cancelled`), run in item 1.

## Assumptions and open points

- yt-dlp emits a `finished` event per stream. Confirmed in yt-dlp's downloader/http.py (around line 353). The one-off script in item 2 covers the event sequence.
- `filesize_approx` may be off. If it is low, the bar reaches 90 early and waits there. If it is high, the bar ends below 90 and jumps at completion. Both are bounded and monotonic.
- Only the percent is shown, not byte counts. Byte counts would need new API fields and were not requested.
- Speed-up follow-ups (D7) are out of scope until you approve them.

## Environment and risks

- Working tree is dirty with unrelated uncommitted changes (backend/app/workers/main.py, backend/app/db/models.py, backend/app/api/routes/clips.py, backend/app/services/publishing/uploader.py, backend/tests/integration/*, and new untracked files). Do not revert, stage, or commit them. The files this plan edits (youtube.py, ui.tsx, sv.ts, app.css, Queue.tsx, Inbox.tsx, VideoDetail.tsx) are clean in git today.
- `.agents/` is not git-ignored. Do not run `git add .` with this plan folder present.
- Use `.venv/bin/...` for Python tools. Use `cd frontend && npm ...` for the dashboard.
- Not verifiable here: a live YouTube download, the real accuracy of YouTube's size estimates, and whether any fragmented (DASH or HLS) format gets selected.

## Answers

### (a) Where downloaded videos are stored

- Final file: `/Users/Gamer/Documents/ClipForge/data/media/<title-slug>-<12 hex chars>.mp4`. Seven files are there now, about 401 MB in total. The database stores `media/<name>` relative to the data directory on `candidate_videos.media_path`.
- How the path is resolved: `.env` sets `CLIPFORGE_DATA_DIR=./data`. `Config.data_dir` in backend/app/config.py resolves that against the repo root. `Config.media_dir` is `data_dir / "media"`. The file name comes from `safe_filename(title, ext)` in `handle_acquire` (backend/app/services/pipeline.py, and `safe_filename` in backend/app/core/security.py). That helper adds a random 12-character hex suffix.
- Temporary download folder: `/Users/Gamer/Documents/ClipForge/data/tmp/job-<job id>/` (`workspace.job_dir`). yt-dlp writes `.part` and `.ytdl` files there. After probing, the file is moved into `media/`. The folder is removed when the job ends. A transient failure keeps the partial files so the retry can resume.
- The README says "It does not download videos from YouTube", but the default `youtube_media_provider` is `yt_dlp` and downloads do happen. I did not change anything for this, but you should decide which is right.

### (b) Why downloads are slow

Each cause is labelled by how I checked it.

1. Confirmed by code reading and the worker log: the `io` lane has one thread (`Worker.run` starts `count = 1` for every lane except `heavy`). Downloads and publish jobs both use that lane (`LANE_FOR` in backend/app/services/queue/states.py). The worker log shows downloads run strictly one after another. For example cd12cefd… finished at 17:59:09 UTC and 1511d879… started at 17:59:10 UTC. With 24 acquire jobs still pending, this is the main cause of slow queue throughput.
2. Confirmed by code reading, magnitude a hypothesis: `"cachedir": False` in `build_options` turns off yt-dlp's cache. For YouTube that cache holds the player JavaScript and the EJS solver data (yt_dlp/extractor/youtube/jsc/_builtin/ejs.py). Every job runs two extractions: `fetch_metadata` and then `acquire`. Each one re-fetches and re-processes the player.
3. Confirmed by code reading, effect a hypothesis: the `io` lane uses an idle backoff that grows to 5 s (`lane_loop`, `min(5.0, idle * 1.5)`). A download that is enqueued by the light-lane metadata job can wait up to about 5 s before it starts.
4. Measured, cause unknown: the 340.9 MB file (job ad1bf974…, 33.7 min of video) took 468.6 s, an average of about 0.73 MB/s. The in-progress file averaged about 1.6 MB/s over 10 s. I cannot tell YouTube throttling apart from a slow connection without a controlled test. The usual throttling cause, a missing JS challenge solver, looks unlikely here. Node is at /opt/homebrew/bin/node and yt-dlp-ejs 0.8.0 is installed. yt-dlp runs in quiet mode, though, so warnings are not logged and I could not confirm that solving succeeds.
5. Resolution cap, confirmed by code reading as a byte-count factor: `youtube_max_height` defaults to 1080 (backend/app/services/settings_service.py), and 0 means best available. Lower caps mean fewer bytes. The effect on time is a hypothesis.
6. Rejected from the code: the separate video and audio streams merged by ffmpeg. The merge runs `-c copy` (yt-dlp `stream_copy_opts`), so it does not re-encode and is only disk I/O.
7. Rejected from the code: `concurrent_fragment_downloads` is not set, which defaults to 1. yt-dlp selects plain https formats for YouTube, and the fragment downloader does not handle those. The setting only matters for DASH or HLS fragment formats. Whether the 340 MB file used one is unverified. YouTube formats also already get `http_chunk_size` (yt-dlp extractor/youtube/_video.py around line 3612), so chunking is not the problem.
8. Rejected from the code: `heavy_concurrency`. Acquire jobs run in the `io` lane, not `heavy`. That setting only affects analyze, transcribe, extract_clips and render.
9. Rejected from the code: the 2 s lease flush. The hook does no I/O and writes no database rows. The flush runs on its own thread and does one small commit per running job every 2 s.
10. Perceived slowness, confirmed: the bar sat at 88% while bytes kept arriving (see defect A), which makes an active download look stalled.

Changes for speed (cachedir, concurrency, resolution cap, lane backoff) are follow-ups in D7 and are not in this plan. Tell me which, if any, you want.
