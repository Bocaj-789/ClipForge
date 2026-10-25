"""Builders shared by the publishing tests (no ffmpeg needed: clips are plain files)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from app.config import Config
from app.db.base import utcnow
from app.db.models import (
    CandidateVideo,
    Channel,
    ClipCandidate,
    ConnectionState,
    Job,
    JobStatus,
    Publication,
    RenderedClip,
)
from app.db.session import session_scope
from app.services.publishing.deps import PublishDeps
from app.services.publishing.secret_store import dump_token
from app.services.publishing.youtube import SCOPES
from app.workers.main import Worker
from tests.fake_google import FakeGoogle


def make_clip(
    cfg: Config,
    *,
    size: int = 700_000,
    rights: str = "user_owned",
    review: str = "approved",
    title: str = "Ett riktigt bra klipp",
    warnings: list[str] | None = None,
    score: float = 80.0,
) -> str:
    """Create source video + proposal + rendered clip with a real file. Returns the rendered clip id."""
    rel = Path("clips") / f"clip-{os.urandom(4).hex()}.mp4"
    path = cfg.data_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    data = os.urandom(size)
    path.write_bytes(data)
    with session_scope() as db:
        video = CandidateVideo(
            origin="manual_url",
            canonical_url=f"https://example.com/{os.urandom(4).hex()}",
            platform="web",
            title="Originalvideo",
            creator_name="Skaparen",
            rights_status=rights,
        )
        db.add(video)
        db.flush()
        prop = ClipCandidate(
            candidate_video_id=video.id,
            start_seconds=0,
            end_seconds=30,
            duration_seconds=30,
            final_score=score,
            transcript_excerpt="Ett utdrag.",
        )
        db.add(prop)
        db.flush()
        rc = RenderedClip(
            clip_candidate_id=prop.id,
            file_path=str(rel),
            title_suggestion=title,
            description_suggestion="En beskrivning.\n\n#shorts #test",
            hashtags_json=["#shorts", "#test"],
            render_profile="fast_draft",
            duration_seconds=30,
            width=1080,
            height=1920,
            file_size_bytes=size,
            checksum=hashlib.sha256(data).hexdigest(),
            review_status=review,
            warnings_json=warnings or [],
        )
        db.add(rc)
        db.flush()
        return rc.id


def make_channel(
    deps: PublishDeps,
    google: FakeGoogle,
    name: str,
    channel_id: str,
    **overrides: object,
) -> str:
    """A connected channel: Google account known to the fake, refresh token in the secret store."""
    acc = google.accounts.get(channel_id) or google.add_account(channel_id, name)
    with session_scope() as db:
        ch = Channel(
            display_name=name,
            external_channel_id=channel_id,
            max_uploads_per_day=10,
            min_spacing_minutes=10,
            connection_state=ConnectionState.CONNECTED,
            oauth_connected_at=utcnow(),
            granted_scopes_json=list(SCOPES),
        )
        db.add(ch)
        db.flush()
        ref = deps.store.make_ref(f"youtube/{ch.id}")
        deps.store.set(ref, dump_token(acc.refresh_token, list(SCOPES)))
        ch.token_reference = ref
        for k, v in overrides.items():
            setattr(ch, k, v)
        return ch.id


def worker_for(cfg: Config, deps: PublishDeps) -> Worker:
    return Worker(cfg, publish_deps=deps)


def run_io(worker: Worker, limit: int = 30) -> int:
    """Run queued publish jobs (the 'io' lane) until none is claimable."""
    n = 0
    for _ in range(limit):
        if not worker.run_once("io"):
            break
        n += 1
    return n


def make_due() -> None:
    """Pretend time passed: every waiting job becomes claimable now."""
    with session_scope() as db:
        for j in db.query(Job).filter(Job.status == JobStatus.RETRY_WAIT):
            j.next_attempt_at = None


def pubs_by_channel() -> dict[str, Publication]:
    with session_scope() as db:
        rows = db.query(Publication).all()
        for r in rows:
            db.expunge(r)
        return {r.channel_id: r for r in rows}
