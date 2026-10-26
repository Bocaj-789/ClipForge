"""End-to-end pipeline through the real worker with a fake transcription engine.

ffmpeg/ffprobe/Pillow are real; the Whisper model is replaced by a deterministic engine so
tests don't need to download weights. The real engine has its own opt-in test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Config
from app.db.models import (
    AuditEvent,
    CandidateStatus,
    CandidateVideo,
    ClipCandidate,
    Job,
    JobStatus,
    RenderedClip,
    RightsStatus,
)
from app.services import inbox, pipeline
from app.services.acquisition.local_file import store_stream
from app.services.agent import set_desired
from app.services.queue import service as q
from app.services.rendering.ffmpeg import probe
from app.services.settings_service import load_settings, save_settings
from app.services.transcription.base import TranscriptResult
from app.workers.main import Worker
from tests.conftest import fake_segments, make_video, requires_ffmpeg

pytestmark = requires_ffmpeg


class FakeEngine:
    name = "fake"

    def __init__(self, available: bool = True) -> None:
        self.available = available

    def status(self) -> dict:
        return {
            "available": self.available,
            "downloaded": self.available,
            "model": "fake",
            "reason": None if self.available else "Modellen saknas",
        }

    def transcribe(
        self, path, *, language, duration, on_progress=None, should_cancel=None
    ) -> TranscriptResult:
        if on_progress:
            on_progress(50)
        return TranscriptResult("sv", fake_segments(), duration, "fake", "1", "fake")


def _worker(cfg: Config, monkeypatch: pytest.MonkeyPatch, engine: FakeEngine | None = None) -> Worker:
    eng = engine or FakeEngine()
    monkeypatch.setattr(pipeline, "default_engine", lambda c, s: eng)
    w = Worker(cfg)
    w.check_health()
    return w


def _drain(w: Worker, limit: int = 30) -> int:
    n = 0
    for _ in range(limit):
        ran = [w.run_once(lane) for lane in ("light", "download", "io", "heavy")]
        if not any(ran):
            break
        n += sum(1 for r in ran if r)
    return n


def _import(db: Session, cfg: Config, src: Path, rights: str = RightsStatus.USER_OWNED) -> CandidateVideo:
    settings = load_settings(db)
    settings.min_clip_seconds, settings.max_clip_seconds, settings.auto_render_top_n = 8, 20, 2
    settings.default_render_profile = "fast_draft"
    save_settings(db, settings)
    with src.open("rb") as f:
        stored = store_stream(iter(lambda: f.read(65536), b""), "min video.mp4", cfg, settings)
    r = inbox.import_file(
        db, stored, title="Testvideo", urgent=True, note=None, rights_status=rights, rights_note=None
    )
    db.commit()
    return r.candidate


def test_local_file_to_rendered_vertical_clip(cfg: Config, db: Session, monkeypatch, tmp_path: Path) -> None:
    cand = _import(db, cfg, make_video(tmp_path / "src.mp4", seconds=45))
    w = _worker(cfg, monkeypatch)
    _drain(w)
    db.expire_all()
    cand = db.get(CandidateVideo, cand.id)
    assert cand.status == CandidateStatus.READY, cand.last_error or cand.blocked_reason
    jobs = db.scalars(select(Job).where(Job.candidate_video_id == cand.id)).all()
    assert {j.job_type for j in jobs} >= {"analyze", "transcribe", "extract_clips", "render"}
    assert all(j.status == JobStatus.SUCCEEDED for j in jobs), [
        (j.job_type, j.safe_error_message) for j in jobs
    ]
    proposals = db.scalars(select(ClipCandidate).where(ClipCandidate.candidate_video_id == cand.id)).all()
    assert proposals and all(8 - 1 <= p.duration_seconds <= 20 + 1.5 for p in proposals)
    renders = db.scalars(select(RenderedClip)).all()
    assert len(renders) == 2
    for r in renders:
        path = cfg.data_dir / r.file_path
        info = probe(path)
        assert (info.width, info.height) == (720, 1280) and info.has_audio
        assert abs(info.duration - r.duration_seconds) < 0.2
        assert (cfg.data_dir / r.thumbnail_path).stat().st_size > 1000
        srt = (cfg.data_dir / r.caption_file_path).read_text()
        assert "-->" in srt and any(ch in srt for ch in "åäöÅÄÖ")
        assert r.review_status == "needs_review"
        assert "#shorts" in r.hashtags_json
    assert not list(cfg.tmp_dir.iterdir())  # no leftover temp files


def test_missing_model_blocks_with_clear_reason(
    cfg: Config, db: Session, monkeypatch, tmp_path: Path
) -> None:
    cand = _import(db, cfg, make_video(tmp_path / "src.mp4", seconds=12))
    w = _worker(cfg, monkeypatch, FakeEngine(available=False))
    assert "transcription" in w.degraded
    _drain(w)
    db.expire_all()
    c = db.get(CandidateVideo, cand.id)
    assert c.status == CandidateStatus.BLOCKED and "saknas" in (c.blocked_reason or "")


def test_pause_prevents_new_jobs(cfg: Config, db: Session, monkeypatch, tmp_path: Path) -> None:
    _import(db, cfg, make_video(tmp_path / "src.mp4", seconds=12))
    set_desired(db, "paused", reason="test")
    db.commit()
    w = _worker(cfg, monkeypatch)
    assert _drain(w) == 0
    set_desired(db, "running", reason=None)
    db.commit()
    assert _drain(w) > 0


def test_critical_storage_skips_disk_heavy_jobs(
    cfg: Config, db: Session, monkeypatch, tmp_path: Path
) -> None:
    cand = _import(db, cfg, make_video(tmp_path / "src.mp4", seconds=30))
    w = _worker(cfg, monkeypatch)
    w.run_once("heavy")  # analyze (not disk heavy)
    w.degraded["storage"] = "full"
    assert w.run_once("heavy") is None  # transcribe is disk heavy -> not claimed
    db.expire_all()
    tj = db.scalar(select(Job).where(Job.candidate_video_id == cand.id, Job.job_type == "transcribe"))
    assert tj.status == JobStatus.PENDING


def test_corrupt_upload_is_rejected(cfg: Config, db: Session, tmp_path: Path) -> None:
    from app.core.errors import AppError

    bad = tmp_path / "fake.mp4"
    bad.write_bytes(b"not a video" * 1000)
    with pytest.raises(AppError), bad.open("rb") as f:
        store_stream(iter([f.read()]), "fake.mp4", cfg, load_settings(db))
    assert not list(cfg.media_dir.iterdir()) and not list(cfg.tmp_dir.iterdir())
    with pytest.raises(AppError):
        store_stream(iter([b"x"]), "evil.sh", cfg, load_settings(db))


def test_render_failure_is_retried_then_fails_safely(
    cfg: Config, db: Session, monkeypatch, tmp_path: Path
) -> None:
    cand = _import(db, cfg, make_video(tmp_path / "src.mp4", seconds=30))
    w = _worker(cfg, monkeypatch)
    for _ in range(3):
        w.run_once("heavy")  # analyze, transcribe, extract
    from app.core.errors import ErrorCode, transient

    def boom(*a, **k):
        raise transient(ErrorCode.RENDER_FAILED, "simulerat fel")

    monkeypatch.setattr(pipeline, "render_clip", boom)
    w.run_once("heavy")
    db.expire_all()
    rj = db.scalars(select(Job).where(Job.candidate_video_id == cand.id, Job.job_type == "render")).all()
    failed = [j for j in rj if j.attempt_count == 1]
    assert failed and failed[0].status == JobStatus.RETRY_WAIT
    assert not list(cfg.clips_dir.iterdir())


def test_cancel_running_render(cfg: Config, db: Session, monkeypatch, tmp_path: Path) -> None:
    cand = _import(db, cfg, make_video(tmp_path / "src.mp4", seconds=30))
    w = _worker(cfg, monkeypatch)
    for _ in range(3):
        w.run_once("heavy")
    real = pipeline.render_clip

    def cancelling(req, on_progress=None, should_cancel=None):
        job = db.scalar(select(Job).where(Job.job_type == "render", Job.status == "running"))
        q.request_cancel(db, job.id)
        db.commit()
        w.progress_state[job.id]["cancel"] = True  # what the lease thread would pick up
        return real(req, on_progress=on_progress, should_cancel=should_cancel)

    monkeypatch.setattr(pipeline, "render_clip", cancelling)
    w.run_once("heavy")
    db.expire_all()
    statuses = {
        j.status
        for j in db.scalars(select(Job).where(Job.candidate_video_id == cand.id, Job.job_type == "render"))
    }
    assert JobStatus.CANCELLED in statuses
    assert not list(cfg.clips_dir.iterdir())


def test_render_then_automatic_upload_of_the_real_file(
    cfg: Config, db: Session, monkeypatch, tmp_path: Path
) -> None:
    """Real ffmpeg render -> automatic publish hook -> publish jobs -> (mocked) YouTube upload."""

    from pydantic import SecretStr

    from app.db.models import Channel, Publication
    from app.services.publishing.deps import PublishDeps
    from app.services.publishing.secret_store import MemoryStore
    from tests.fake_google import FakeGoogle
    from tests.publishing_helpers import make_channel

    cfg.google_client_id, cfg.google_client_secret = "cid.apps.googleusercontent.com", SecretStr("secret")
    google = FakeGoogle()
    deps = PublishDeps(
        cfg=cfg, store=MemoryStore(), http_factory=google.client, chunk_size=256 * 1024, sleep=lambda s: None
    )
    make_channel(
        deps, google, "Autokanal", "UC_AUTO",
        auto_publish_enabled=True, auto_publish_min_score=0.0, min_spacing_minutes=0, max_uploads_per_day=5,
    )  # fmt: skip
    cand = _import(db, cfg, make_video(tmp_path / "src.mp4", seconds=45))
    s = load_settings(db)
    s.auto_publish_global_enabled = True
    save_settings(db, s)
    db.commit()

    eng = FakeEngine()
    monkeypatch.setattr(pipeline, "default_engine", lambda c, st: eng)
    w = Worker(cfg, publish_deps=deps)
    w.check_health()
    _drain(w, limit=60)
    db.expire_all()

    assert db.get(CandidateVideo, cand.id).status == CandidateStatus.READY
    renders = db.scalars(select(RenderedClip)).all()
    pubs = db.scalars(select(Publication)).all()
    assert len(renders) == 2 and len(pubs) == 2, [
        (e.action, e.safe_details_json) for e in db.scalars(select(AuditEvent)) if "publication" in e.action
    ]
    assert all(p.automatic and p.status == "uploaded" and p.external_video_id for p in pubs)
    for p in pubs:
        rc = db.get(RenderedClip, p.rendered_clip_id)
        # The local file is removed once YouTube confirms. The uploaded bytes must match the checksum.
        on_disk = rc.checksum
        assert not (cfg.data_dir / rc.file_path).exists()
        assert google.video_sha[p.external_video_id] == on_disk == rc.checksum
        res = google.videos[p.external_video_id]
        assert res["status"]["privacyStatus"] == "private"  # automation defaults to private
        assert res["snippet"]["title"] == rc.title_suggestion
    assert db.scalars(select(Channel)).one().last_upload_at is not None
    jobs = db.scalars(select(Job).where(Job.job_type == "publish")).all()
    assert len(jobs) == 2 and all(j.status == JobStatus.SUCCEEDED for j in jobs)
