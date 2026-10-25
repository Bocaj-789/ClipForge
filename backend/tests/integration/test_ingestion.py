"""Video ingestion pipeline: URL submission -> metadata -> provider acquisition -> ffprobe ->
ffmpeg audio -> Whisper (fake engine) -> clip selection -> vertical rendering.

ffmpeg/ffprobe/Pillow are real. The media provider and the Whisper engine are replaced by
deterministic fakes, so the tests need neither network access nor model weights. The real yt-dlp
code path is exercised against a local HTTP server in ``test_yt_dlp_downloads_*``.
"""

from __future__ import annotations

import functools
import http.server
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Config
from app.core.errors import ErrorCode, JobError, awaiting_media, permanent, transient
from app.db.base import utcnow
from app.db.models import (
    CandidateStatus,
    CandidateVideo,
    Job,
    JobStatus,
    JobType,
    RenderedClip,
    RightsStatus,
)
from app.services import inbox, pipeline, workspace
from app.services.acquisition.local_file import store_stream
from app.services.acquisition.provider import AcquiredMedia, MediaRequest, ProviderMetadata
from app.services.acquisition.youtube import YouTubeProvider, build_format_selector, translate_error
from app.services.queue import service as q
from app.services.queue.states import Priority
from app.services.rendering.ffmpeg import Cancelled, probe
from app.services.settings_service import load_settings, save_settings
from app.services.transcription.audio import extract_audio
from app.workers.main import Worker
from tests.conftest import make_video, requires_ffmpeg
from tests.integration.test_pipeline import FakeEngine, _drain

pytestmark = requires_ffmpeg

YT = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
YT2 = "https://www.youtube.com/watch?v=aaaaaaaaaaa"


class FakeProvider:
    """Stands in for a real media provider: copies a prepared video, or runs scripted behaviour."""

    name = "fake"

    def __init__(self, source: Path | None = None, *, duration: float = 45) -> None:
        self.source = source
        self.duration = duration
        self.script: list[Callable[[MediaRequest], AcquiredMedia]] = []
        self.acquire_calls = 0
        self.ok = True

    def available(self) -> tuple[bool, str | None]:
        return (True, None) if self.ok else (False, "Providern är inte installerad.")

    def supports(self, platform: str, url: str) -> bool:
        return platform == "youtube"

    def fetch_metadata(self, url: str) -> ProviderMetadata | None:
        return ProviderMetadata(
            title="Räksmörgåsar på Öland",
            creator_name="Kocken",
            description="Beskrivning",
            duration_seconds=self.duration,
        )

    def acquire(self, request: MediaRequest) -> AcquiredMedia:
        self.acquire_calls += 1
        request.progress(10, "hämtar")
        if self.script:
            return self.script.pop(0)(request)
        assert self.source is not None
        request.workdir.mkdir(parents=True, exist_ok=True)
        dest = request.workdir / "video.mp4"
        shutil.copyfile(self.source, dest)
        return AcquiredMedia(path=dest, provider=self.name, title="Provider titel")


class RecordingEngine(FakeEngine):
    """Records what the transcription handler hands to Whisper."""

    def __init__(self) -> None:
        super().__init__()
        self.paths: list[Path] = []
        self.sizes: list[int] = []

    def transcribe(self, path, *, language, duration, on_progress=None, should_cancel=None):
        self.paths.append(path)
        self.sizes.append(path.stat().st_size)
        return super().transcribe(
            path, language=language, duration=duration, on_progress=on_progress, should_cancel=should_cancel
        )


@pytest.fixture
def env(cfg: Config, db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A worker wired to a fake provider + engine, with fast render settings."""
    s = load_settings(db)
    s.min_clip_seconds, s.max_clip_seconds, s.auto_render_top_n = 8, 20, 2
    s.default_render_profile = "fast_draft"
    save_settings(db, s)
    db.commit()
    provider = FakeProvider(make_video(tmp_path / "remote.mp4", seconds=45))
    engine = RecordingEngine()
    monkeypatch.setattr(pipeline, "default_providers", lambda c, st: [provider])
    monkeypatch.setattr(pipeline, "default_engine", lambda c, st: engine)
    monkeypatch.setattr(
        pipeline,
        "fetch_youtube_oembed",
        lambda url: {
            "title": "oEmbed-titel",
            "author_name": "Kanal",
            "thumbnail_url": "https://i.ytimg.com/vi/x/h.jpg",
        },
    )
    w = Worker(cfg)
    w.check_health()
    return w, provider, engine


def _submit(db: Session, url: str = YT, *, rights: str = "user_owned", urgent: bool = True):
    r = inbox.submit_url(
        db,
        url,
        urgent=urgent,
        note=None,
        rights_status=rights,
        rights_note=None,
        settings=load_settings(db),
    )
    db.commit()
    return r


def _cand(db: Session, cid: str) -> CandidateVideo:
    db.expire_all()
    c = db.get(CandidateVideo, cid)
    assert c is not None
    return c


def _jobs(db: Session, cid: str, job_type: str | None = None) -> list[Job]:
    db.expire_all()
    stmt = select(Job).where(Job.candidate_video_id == cid).order_by(Job.created_at)
    if job_type:
        stmt = stmt.where(Job.job_type == job_type)
    return list(db.scalars(stmt))


# --------------------------------------------------------------------------- end to end
def test_youtube_url_to_rendered_vertical_clips(env, cfg: Config, db: Session) -> None:
    w, provider, engine = env
    cid = _submit(db).candidate.id
    _drain(w)
    c = _cand(db, cid)
    assert c.status == CandidateStatus.READY, c.last_error or c.blocked_reason
    # Metadata from oEmbed + the provider, persisted on the candidate.
    assert c.creator_name == "Kocken" and c.description == "Beskrivning"
    assert c.metadata_json["metadata_sources"] == ["oembed", "fake"]
    # Media was acquired into the data dir and inspected with ffprobe.
    assert c.media_path and (cfg.data_dir / c.media_path).is_file()
    assert c.media_checksum and c.metadata_json["media"]["orientation"] == "landscape"
    jobs = _jobs(db, cid)
    assert [j.job_type for j in jobs[:5]] == [
        "fetch_metadata",
        "acquire",
        "analyze",
        "transcribe",
        "extract_clips",
    ]
    assert all(j.status == JobStatus.SUCCEEDED for j in jobs), [
        (j.job_type, j.safe_error_message) for j in jobs
    ]
    assert provider.acquire_calls == 1
    # Whisper was fed the FFmpeg-extracted 16 kHz mono WAV, not the video.
    assert engine.paths and engine.paths[0].suffix == ".wav" and engine.sizes[0] > 100_000
    renders = db.scalars(select(RenderedClip)).all()
    assert len(renders) == 2
    for r in renders:
        info = probe(cfg.data_dir / r.file_path)
        assert (info.width, info.height) == (720, 1280) and info.has_audio
        assert "-->" in (cfg.data_dir / r.caption_file_path).read_text()
    assert not list(cfg.tmp_dir.iterdir()), "per-job temp directories must be removed"


def test_audio_is_extracted_as_16khz_mono_wav(cfg: Config, tmp_path: Path) -> None:
    src = make_video(tmp_path / "a.mp4", seconds=4)
    out = extract_audio(src, tmp_path / "w" / "audio.wav", duration=4)
    meta = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,sample_rate,channels",
            "-of",
            "csv=p=0",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert meta == "pcm_s16le,16000,1"
    assert not list(out.parent.glob("*.part*"))
    silent = make_video(tmp_path / "v.mp4", seconds=2, audio=False)
    with pytest.raises(Exception):  # noqa: B017 - ffmpeg fails on a missing stream
        extract_audio(silent, tmp_path / "w" / "none.wav")


# --------------------------------------------------------------------------- priority
def test_user_submitted_urls_outrank_discovered_videos(env, cfg: Config, db: Session) -> None:
    discovered = CandidateVideo(
        origin="discovery",
        canonical_url=YT2,
        platform="youtube",
        external_id="aaaaaaaaaaa",
        priority=Priority.DISCOVERY,
        rights_status=RightsStatus.USER_OWNED,
        status=CandidateStatus.DISCOVERED,
    )
    db.add(discovered)
    db.flush()
    q.enqueue(db, JobType.FETCH_METADATA, priority=Priority.DISCOVERY, candidate_video_id=discovered.id)
    db.commit()  # the discovered video is older and first in line...
    user = _submit(db, YT, urgent=False).candidate  # ...but the user's URL still goes first
    assert user.priority == Priority.USER
    first = q.claim(db, lane="light", worker_id="t", lease_seconds=60)
    assert first is not None and first.candidate_video_id == user.id and first.priority == Priority.USER
    # Follow-up stages inherit the priority of the job that spawned them.
    w, _p, _e = env
    q.complete(db, first)
    db.commit()
    first.status = JobStatus.PENDING  # let the worker run it for real
    db.commit()
    w.run_once("light")
    assert [j.priority for j in _jobs(db, user.id, "acquire")] == [Priority.USER]


def test_resubmitting_a_discovered_video_adopts_and_prioritises_it(env, db: Session) -> None:
    cand = CandidateVideo(
        origin="discovery",
        canonical_url=YT,
        platform="youtube",
        external_id="dQw4w9WgXcQ",
        priority=Priority.DISCOVERY,
        rights_status=RightsStatus.UNKNOWN,
        status=CandidateStatus.DISCOVERED,
    )
    db.add(cand)
    db.flush()
    job = q.enqueue(db, JobType.FETCH_METADATA, priority=Priority.DISCOVERY, candidate_video_id=cand.id)
    db.commit()
    r = _submit(db, "https://youtu.be/dQw4w9WgXcQ", urgent=True)
    assert r.duplicate and r.priority_raised and r.candidate.id == cand.id
    db.expire_all()
    assert db.get(Job, job.id).priority == Priority.URGENT
    assert db.get(CandidateVideo, cand.id).origin == "manual_url"


# --------------------------------------------------------------------------- duplicates
def test_equivalent_urls_create_one_candidate_and_one_job(env, db: Session) -> None:
    forms = [
        YT,
        "https://youtu.be/dQw4w9WgXcQ?si=x",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "youtube.com/watch?v=dQw4w9WgXcQ&t=9",
    ]
    results = [_submit(db, u, urgent=False) for u in forms]
    assert [r.duplicate for r in results] == [False, True, True, True]
    assert len({r.candidate.id for r in results}) == 1
    assert len(_jobs(db, results[0].candidate.id)) == 1
    # A later urgent submission raises the priority of the existing job instead of adding another.
    again = _submit(db, YT, urgent=True)
    assert again.duplicate and again.priority_raised
    jobs = _jobs(db, results[0].candidate.id)
    assert len(jobs) == 1 and jobs[0].priority == Priority.URGENT


def test_concurrent_duplicate_insert_is_handled(env, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _submit(db).candidate
    real = db.scalar
    calls = {"n": 0}

    def blind_first_lookup(stmt: Any, *a: Any, **k: Any) -> Any:
        calls["n"] += 1
        return None if calls["n"] == 1 else real(stmt, *a, **k)  # simulates losing the race

    monkeypatch.setattr(db, "scalar", blind_first_lookup)
    r = _submit(db, YT)
    assert r.duplicate and r.candidate.id == first.id
    monkeypatch.undo()
    assert db.scalar(select(CandidateVideo.id).where(CandidateVideo.id != first.id)) is None


def test_failed_video_is_revived_by_resubmission(env, db: Session) -> None:
    w, provider, _e = env
    provider.script = [lambda r: (_ for _ in ()).throw(permanent(ErrorCode.CORRUPT_MEDIA, "trasig"))]
    cid = _submit(db).candidate.id
    _drain(w)
    assert _cand(db, cid).status == CandidateStatus.FAILED
    r = _submit(db)
    assert r.duplicate
    _drain(w)
    assert _cand(db, cid).status == CandidateStatus.READY


# --------------------------------------------------------------------------- awaiting media
def test_inaccessible_source_awaits_media_while_the_queue_continues(
    env, cfg: Config, db: Session, tmp_path: Path
) -> None:
    w, provider, _e = env

    def refuse(req: MediaRequest) -> AcquiredMedia:
        (req.workdir / "half.part").write_bytes(b"x" * 1000)
        raise awaiting_media("Videon är privat.")

    provider.script = [refuse]
    blocked_id = _submit(db, YT).candidate.id
    # A second, perfectly accessible video submitted afterwards must not be held back.
    settings = load_settings(db)
    with (tmp_path / "remote.mp4").open("rb") as f:
        stored = store_stream(iter(lambda: f.read(65536), b""), "lokal.mp4", cfg, settings)
    ok = inbox.import_file(
        db, stored, title="Lokal", urgent=False, note=None, rights_status="user_owned", rights_note=None
    ).candidate
    db.commit()
    _drain(w)
    c = _cand(db, blocked_id)
    job = _jobs(db, blocked_id, "acquire")[0]
    assert job.status == JobStatus.AWAITING_MEDIA and job.error_code == ErrorCode.MEDIA_ACCESS_UNAVAILABLE
    assert c.status == CandidateStatus.AWAITING_MEDIA and c.blocked_reason == "Videon är privat."
    assert _cand(db, ok.id).status == CandidateStatus.READY
    assert not list(cfg.tmp_dir.iterdir())
    assert w.run_once("io") is None  # parked jobs are not re-claimed
    # The user supplies the media; processing resumes and finishes.
    with (tmp_path / "remote.mp4").open("rb") as f:
        mine = store_stream(iter(lambda: f.read(65536), b""), "min.mp4", cfg, settings)
    inbox.attach_media(db, _cand(db, blocked_id), mine)
    db.commit()
    _drain(w)
    c = _cand(db, blocked_id)
    assert c.status == CandidateStatus.READY, c.last_error or c.blocked_reason
    assert _jobs(db, blocked_id, "acquire")[0].status == JobStatus.CANCELLED  # superseded


def test_missing_provider_binary_parks_job_as_awaiting_media(env, db: Session) -> None:
    w, provider, _e = env
    provider.ok = False
    cid = _submit(db).candidate.id
    _drain(w)
    c = _cand(db, cid)
    meta = _jobs(db, cid, "fetch_metadata")[0]
    assert meta.status == JobStatus.AWAITING_MEDIA and c.status == CandidateStatus.AWAITING_MEDIA
    assert "inte installerad" in (c.blocked_reason or "") and c.title  # metadata survived the park
    provider.ok = True
    inbox.resume_pipeline(db, c)
    db.commit()
    _drain(w)
    assert _cand(db, cid).status == CandidateStatus.READY


def test_provider_disabled_in_settings_awaits_media(
    cfg: Config, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "fetch_youtube_oembed", lambda url: {"title": "t", "author_name": "a"})
    s = load_settings(db)
    s.youtube_media_provider = "off"
    save_settings(db, s)
    db.commit()
    cid = _submit(db).candidate.id
    w = Worker(cfg)
    w.run_once("light")
    c = _cand(db, cid)
    assert c.status == CandidateStatus.AWAITING_MEDIA and "mediaprovider" in (c.blocked_reason or "")


def test_awaiting_media_job_can_be_retried_or_cancelled(env, db: Session) -> None:
    w, provider, _e = env
    provider.script = [lambda r: (_ for _ in ()).throw(awaiting_media("Inloggning krävs."))]
    cid = _submit(db).candidate.id
    _drain(w)
    job = _jobs(db, cid, "acquire")[0]
    assert job.status == JobStatus.AWAITING_MEDIA
    q.manual_retry(db, job.id)
    db.commit()
    _drain(w)
    assert _cand(db, cid).status == CandidateStatus.READY
    # And a parked job can simply be cancelled.
    cid2 = _submit(db, YT2).candidate.id
    provider.script = [lambda r: (_ for _ in ()).throw(awaiting_media("Privat."))]
    _drain(w)
    parked = _jobs(db, cid2, "acquire")[0]
    q.request_cancel(db, parked.id)
    db.commit()
    assert _jobs(db, cid2, "acquire")[0].status == JobStatus.CANCELLED


# --------------------------------------------------------------------------- retries & temp files
def test_transient_download_failure_retries_and_resumes_partial_files(env, cfg: Config, db: Session) -> None:
    w, provider, _e = env
    seen: list[list[str]] = []

    def flaky(req: MediaRequest) -> AcquiredMedia:
        (req.workdir / "video.mp4.part").write_bytes(b"partial")
        raise transient(ErrorCode.NETWORK_TIMEOUT, "nätverksfel")

    def second(req: MediaRequest) -> AcquiredMedia:
        seen.append(sorted(p.name for p in req.workdir.iterdir()))  # the retry sees the partial file
        assert provider.source is not None
        dest = req.workdir / "video.mp4"
        shutil.copyfile(provider.source, dest)
        return AcquiredMedia(path=dest, provider="fake")

    provider.script = [flaky, second]
    cid = _submit(db).candidate.id
    _drain(w)
    job = _jobs(db, cid, "acquire")[0]
    assert job.status == JobStatus.RETRY_WAIT and job.attempt_count == 1 and job.next_attempt_at
    assert workspace.job_dir(cfg, job.id).is_dir()  # kept for the resume
    job.next_attempt_at = utcnow() - timedelta(seconds=1)
    db.commit()
    _drain(w)
    assert seen == [["video.mp4.part"]]
    assert _cand(db, cid).status == CandidateStatus.READY
    assert not list(cfg.tmp_dir.iterdir())


def test_download_failures_exhaust_attempts_then_fail_and_clean_up(env, cfg: Config, db: Session) -> None:
    w, provider, _e = env

    def always(req: MediaRequest) -> AcquiredMedia:
        (req.workdir / "x.part").write_bytes(b"1")
        raise transient(ErrorCode.NETWORK_TIMEOUT, "nätverksfel")

    provider.script = [always, always, always]
    cid = _submit(db).candidate.id
    for _ in range(3):
        _drain(w)
        for j in _jobs(db, cid, "acquire"):
            if j.status == JobStatus.RETRY_WAIT:
                j.next_attempt_at = utcnow() - timedelta(seconds=1)
        db.commit()
    job = _jobs(db, cid, "acquire")[0]
    assert (
        job.status == JobStatus.FAILED and job.error_code == ErrorCode.MAX_ATTEMPTS and job.attempt_count == 3
    )
    assert _cand(db, cid).status == CandidateStatus.FAILED
    assert not list(cfg.tmp_dir.iterdir())


def test_cancelled_download_removes_its_temp_directory(env, cfg: Config, db: Session) -> None:
    w, provider, _e = env

    def cancel(req: MediaRequest) -> AcquiredMedia:
        (req.workdir / "x.part").write_bytes(b"1")
        raise Cancelled()

    provider.script = [cancel]
    cid = _submit(db).candidate.id
    _drain(w)
    assert _jobs(db, cid, "acquire")[0].status == JobStatus.CANCELLED
    assert not list(cfg.tmp_dir.iterdir())


def test_non_media_download_is_rejected(env, cfg: Config, db: Session) -> None:
    w, provider, _e = env

    def junk(req: MediaRequest) -> AcquiredMedia:
        dest = req.workdir / "video.mp4"
        dest.write_bytes(b"<html>not a video</html>" * 100)
        return AcquiredMedia(path=dest, provider="fake")

    provider.script = [junk]
    cid = _submit(db).candidate.id
    _drain(w)
    job = _jobs(db, cid, "acquire")[0]
    assert job.status == JobStatus.FAILED and job.error_code == ErrorCode.CORRUPT_MEDIA
    assert not list(cfg.media_dir.iterdir()) and not list(cfg.tmp_dir.iterdir())


def test_too_long_video_is_rejected_before_download(env, db: Session) -> None:
    w, provider, _e = env
    provider.duration = 6 * 3600
    cid = _submit(db).candidate.id
    _drain(w)
    meta = _jobs(db, cid, "fetch_metadata")[0]
    assert meta.status == JobStatus.FAILED and meta.error_code == ErrorCode.MEDIA_TOO_LONG
    assert provider.acquire_calls == 0


def test_orphaned_job_directories_are_swept(cfg: Config, db: Session) -> None:
    running = q.enqueue(db, JobType.TRANSCRIBE, priority=1)
    retrying = q.enqueue(db, JobType.ACQUIRE, priority=1)
    done = q.enqueue(db, JobType.RENDER, priority=1)
    running.status, done.status = JobStatus.RUNNING, JobStatus.SUCCEEDED
    db.commit()
    for jid in (running.id, retrying.id, done.id, "deadbeef"):
        (workspace.job_dir(cfg, jid) / "sub").mkdir(parents=True)
    stray = cfg.tmp_dir / "other-file.part"
    stray.write_bytes(b"x")
    assert workspace.cleanup_orphans(db, cfg) == 2
    assert workspace.job_dir(cfg, running.id).exists() and workspace.job_dir(cfg, retrying.id).exists()
    assert not workspace.job_dir(cfg, done.id).exists() and stray.exists()


# --------------------------------------------------------------------------- API / dashboard
def test_dashboard_api_accepts_youtube_url_and_creates_prioritised_job(
    authed: tuple[TestClient, dict[str, str]],
) -> None:
    client, h = authed
    r = client.post(
        "/api/v1/inbox", json={"url": YT, "urgent": True, "rights_status": "user_owned"}, headers=h
    )
    assert r.status_code == 200, r.text
    body = r.json()
    cand = body["candidate"]
    assert not body["duplicate"] and cand["status"] == "queued" and cand["priority"] == Priority.URGENT
    assert cand["latest_job"]["job_type"] == "fetch_metadata" and cand["latest_job"]["priority"] == 0
    dup = client.post("/api/v1/inbox", json={"url": "https://youtu.be/dQw4w9WgXcQ"}, headers=h).json()
    assert dup["duplicate"] and dup["candidate"]["id"] == cand["id"]
    jobs = client.get("/api/v1/jobs", params={"status": "awaiting_media,pending"}).json()
    assert jobs["total"] == 1
    deps = client.get("/api/v1/health/dependencies").json()
    assert {p["name"] for p in deps["media_providers"]} >= {"direct_http", "yt_dlp"}


# --------------------------------------------------------------------------- yt-dlp provider
@pytest.mark.parametrize(
    ("message", "awaiting", "transient_", "code"),
    [
        (
            "ERROR: [youtube] x: Sign in to confirm you’re not a bot",
            True,
            False,
            ErrorCode.MEDIA_ACCESS_UNAVAILABLE,
        ),
        (
            "ERROR: [youtube] x: Private video. Sign in if you've been granted access",
            True,
            False,
            ErrorCode.MEDIA_ACCESS_UNAVAILABLE,
        ),
        ("ERROR: [youtube] x: This video is DRM protected", True, False, ErrorCode.MEDIA_ACCESS_UNAVAILABLE),
        (
            "ERROR: [youtube] x: Join this channel to get access to members-only content",
            True,
            False,
            ErrorCode.MEDIA_ACCESS_UNAVAILABLE,
        ),
        (
            "ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate for some users",
            True,
            False,
            ErrorCode.MEDIA_ACCESS_UNAVAILABLE,
        ),
        (
            "ERROR: [youtube] x: The uploader has not made this video available in your country",
            True,
            False,
            ErrorCode.MEDIA_ACCESS_UNAVAILABLE,
        ),
        ("ERROR: [youtube] x: Video unavailable", True, False, ErrorCode.MEDIA_ACCESS_UNAVAILABLE),
        (
            "ERROR: [youtube] x: Requested format is not available. Use --list-formats",
            True,
            False,
            ErrorCode.MEDIA_ACCESS_UNAVAILABLE,
        ),
        ("HTTP Error 429: Too Many Requests", False, True, ErrorCode.NETWORK_TIMEOUT),
        (
            "ERROR: unable to download video data: <urlopen error timed out>",
            False,
            True,
            ErrorCode.NETWORK_TIMEOUT,
        ),
        ("File is larger than max-filesize (100 bytes > 10 bytes)", False, False, ErrorCode.MEDIA_TOO_LARGE),
        ("something entirely different", False, False, ErrorCode.ACQUISITION_BLOCKED),
    ],
)
def test_yt_dlp_errors_are_classified(
    message: str, awaiting: bool, transient_: bool, code: ErrorCode
) -> None:
    err = translate_error(RuntimeError(message))
    assert isinstance(err, JobError)
    assert (err.awaiting_media, err.transient, err.code) == (awaiting, transient_, code)
    assert not err.block
    if awaiting:  # never suggests working around the restriction
        assert "kringgår inte" in err.safe_message


def test_yt_dlp_provider_only_handles_single_youtube_videos() -> None:
    p = YouTubeProvider()
    assert p.supports("youtube", YT)
    for platform, url in [
        ("youtube", "https://www.youtube.com/@kanal"),
        ("youtube", "https://evil.example.com/watch?v=dQw4w9WgXcQ"),
        ("youtube", "https://www.youtube.com/playlist?list=PL123"),
        ("direct", "https://archive.org/a.mp4"),
    ]:
        assert not p.supports(platform, url)
    sel = build_format_selector(1080)
    assert "height<=?1080" in sel and "av01" in sel and "bestvideo" in build_format_selector(None)


class _LocalYouTube(YouTubeProvider):
    """Real yt-dlp against a local HTTP server using the generic extractor (no network needed)."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.allowed_extractors = ("generic",)

    def _video_id(self, url: str) -> str:
        return "local"


@pytest.fixture
def local_http(tmp_path: Path) -> Iterator[str]:
    root = tmp_path / "www"
    root.mkdir()
    make_video(root / "clip.mp4", seconds=6)

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a: Any, **k: Any) -> None:
            return None

        def handle(self) -> None:
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError):  # yt-dlp aborts on purpose in some tests
                pass

    handler = functools.partial(Quiet, directory=str(root))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def _request(url: str, workdir: Path, **kw: Any) -> MediaRequest:
    base: dict[str, Any] = {
        "url": url,
        "platform": "youtube",
        "external_id": "local",
        "workdir": workdir,
        "max_bytes": 50 * 1024 * 1024,
        "max_duration_seconds": 3600,
        "max_height": 1080,
        "progress": lambda p, stage: None,
        "cancelled": lambda: False,
    }
    base.update(kw)
    return MediaRequest(**base)


def test_yt_dlp_downloads_and_reads_metadata_for_real(local_http: str, tmp_path: Path) -> None:
    p = _LocalYouTube()
    meta = p.fetch_metadata(f"{local_http}/clip.mp4")
    assert meta is not None and meta.title == "clip" and not meta.is_live
    got = p.acquire(_request(f"{local_http}/clip.mp4", tmp_path / "ws"))
    assert got.provider == "yt_dlp" and got.path.is_file() and got.path.parent == tmp_path / "ws"
    info = probe(got.path)
    assert info.has_video and info.has_audio and 5 < info.duration < 8
    assert not list((tmp_path / "ws").glob("*.part"))


def test_yt_dlp_download_limits_and_failures_for_real(local_http: str, tmp_path: Path) -> None:
    p = _LocalYouTube()
    with pytest.raises(JobError) as e:
        p.acquire(_request(f"{local_http}/clip.mp4", tmp_path / "b", max_bytes=1000))
    assert e.value.code == ErrorCode.MEDIA_TOO_LARGE
    with pytest.raises(JobError) as e:
        p.acquire(_request(f"{local_http}/missing.mp4", tmp_path / "c"))
    assert not e.value.block  # a 404 never silently blocks the queue
    with pytest.raises(Cancelled):
        p.acquire(_request(f"{local_http}/clip.mp4", tmp_path / "d", cancelled=lambda: True))


class _FakeYDL:
    """Minimal yt-dlp stand-in to drive the provider's pre-download checks."""

    info: dict[str, Any] = {}
    downloaded = False

    def __init__(self, opts: dict[str, Any]) -> None:
        self.opts = opts

    def __enter__(self) -> _FakeYDL:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
        return dict(self.info)

    def process_ie_result(self, info: dict[str, Any], download: bool = True) -> dict[str, Any]:
        type(self).downloaded = True
        return info


@pytest.mark.parametrize(
    ("info", "code", "awaiting"),
    [
        ({"id": "x", "title": "t", "duration": 7200}, ErrorCode.MEDIA_TOO_LONG, False),
        ({"id": "x", "title": "t", "is_live": True}, ErrorCode.MEDIA_ACCESS_UNAVAILABLE, True),
        ({"id": "x", "title": "t", "live_status": "is_upcoming"}, ErrorCode.MEDIA_ACCESS_UNAVAILABLE, True),
    ],
)
def test_yt_dlp_checks_duration_and_live_status_before_downloading(
    info: dict[str, Any], code: ErrorCode, awaiting: bool, tmp_path: Path
) -> None:
    _FakeYDL.info, _FakeYDL.downloaded = info, False
    p = YouTubeProvider(ydl_factory=_FakeYDL)
    with pytest.raises(JobError) as e:
        p.acquire(_request(YT, tmp_path, max_duration_seconds=3600))
    assert e.value.code == code and e.value.awaiting_media is awaiting
    assert _FakeYDL.downloaded is False


def test_yt_dlp_options_never_use_credentials_or_remote_code() -> None:
    seen: dict[str, Any] = {}

    class Spy(_FakeYDL):
        def __init__(self, opts: dict[str, Any]) -> None:
            seen.update(opts)
            super().__init__(opts)

    Spy.info = {"id": "x", "title": "t", "duration": 60, "is_live": False}
    with pytest.raises(JobError):  # no file is produced by the fake -> awaiting media, not a crash
        YouTubeProvider(ydl_factory=Spy).acquire(_request(YT, Path("/tmp/clipforge-spy")))  # noqa: S108
    shutil.rmtree("/tmp/clipforge-spy", ignore_errors=True)  # noqa: S108
    assert seen["noplaylist"] and seen["ignoreconfig"] and seen["allowed_extractors"] == ["youtube"]
    for forbidden in (
        "cookiefile",
        "cookiesfrombrowser",
        "username",
        "password",
        "proxy",
        "remote_components",
    ):
        assert forbidden not in seen


def test_yt_dlp_availability_reports_missing_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.acquisition.youtube.js_runtime", lambda: None)
    ok, why = YouTubeProvider().available()
    assert not ok and "Node.js" in (why or "")
