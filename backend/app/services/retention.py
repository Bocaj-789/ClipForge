"""Automatic clean-up of files that are no longer needed.

Three rules:

* A declined clip is removed two days after it was declined.
* A clip is removed as soon as YouTube has confirmed the upload for every channel it was selected for.
* A downloaded source video is removed once it is older than the configured number of hours
  (``source_media_retention_hours``), whatever the status of its clips. Videos the user supplied
  themselves are never removed by this rule.

Only the files are deleted. The database rows stay: rendered clips are hidden via ``deleted_at`` so the
publication history and YouTube links are kept, and a source video just loses its ``media_path``. A clip
still needed for a queued or failed upload is never removed, and a source video that a queued or running
job is using is left alone.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Config
from app.core.security import PathContainmentError, contained_path
from app.db.base import utcnow
from app.db.models import (
    CandidateVideo,
    ClipStatus,
    Job,
    JobStatus,
    Publication,
    PublicationStatus,
    RenderedClip,
    ReviewStatus,
)
from app.services.audit import service as audit
from app.services.storage.manager import invalidate_cache

log = logging.getLogger("clipforge.retention")
DECLINED_RETENTION = timedelta(days=2)
IN_FLIGHT = (PublicationStatus.QUEUED, PublicationStatus.UPLOADING)
JOB_ACTIVE = (JobStatus.PENDING, JobStatus.RUNNING, JobStatus.RETRY_WAIT)


def _remove_files(cfg: Config, r: RenderedClip) -> int:
    removed = 0
    for rel in (r.file_path, r.thumbnail_path, r.caption_file_path):
        if not rel:
            continue
        try:
            p: Path = contained_path(cfg.data_dir, rel)
        except PathContainmentError:
            continue
        if p.is_file():
            p.unlink()
            removed += 1
    return removed


def _remove(db: Session, cfg: Config, r: RenderedClip, reason: str) -> None:
    removed = _remove_files(cfg, r)
    r.deleted_at = utcnow()
    if (
        r.clip is not None
        and r.clip.status == ClipStatus.RENDERED
        and all(x.deleted_at for x in r.clip.renders)
    ):
        r.clip.status = ClipStatus.PROPOSED
    invalidate_cache()
    audit.record(
        db,
        "clip.files_removed",
        object_type="rendered_clip",
        object_id=r.id,
        details={"reason": reason, "files": removed},
    )
    log.info("removed %s files for rendered clip %s (%s)", removed, r.id, reason)


def _in_flight(db: Session, rid: str) -> bool:
    return (
        db.scalar(
            select(Publication.id).where(
                Publication.rendered_clip_id == rid, Publication.status.in_(IN_FLIGHT)
            )
        )
        is not None
    )


def _uploaded_everywhere(db: Session, rid: str) -> bool:
    pubs = db.scalars(select(Publication).where(Publication.rendered_clip_id == rid)).all()
    return bool(pubs) and all(p.status == PublicationStatus.UPLOADED and p.external_video_id for p in pubs)


def clear_uploaded_clip(db: Session, cfg: Config, r: RenderedClip) -> bool:
    """Called right after YouTube confirms an upload. Removes the file once no channel still needs it."""
    if r.deleted_at is None and _uploaded_everywhere(db, r.id):
        _remove(db, cfg, r, "YouTube har tagit emot uppladdningen till alla valda kanaler")
        return True
    return False


def _source_in_use(db: Session, cand_id: str) -> bool:
    return (
        db.scalar(
            select(Job.id).where(Job.candidate_video_id == cand_id, Job.status.in_(JOB_ACTIVE)).limit(1)
        )
        is not None
    )


def _remove_source(db: Session, cfg: Config, cand: CandidateVideo, rel: str, reason: str) -> None:
    size = 0
    p: Path | None = None
    try:
        p = contained_path(cfg.data_dir, rel)
    except PathContainmentError:
        log.warning("source media path for candidate %s is outside the data dir; not deleting", cand.id)
    if p is not None and p.is_file():
        size = p.stat().st_size
        p.unlink()
    cand.media_path = None
    cand.media_checksum = None
    cand.media_downloaded_at = None
    invalidate_cache()
    audit.record(
        db,
        "candidate.source_media_removed",
        object_type="candidate",
        object_id=cand.id,
        details={"reason": reason, "bytes": size},
    )
    log.info("removed downloaded source for candidate %s (%s)", cand.id, reason)


def sweep_source_media(db: Session, cfg: Config, now, keep_for: timedelta) -> int:
    """Remove downloaded source videos older than ``keep_for``. Returns how many were removed."""
    rows = db.scalars(
        select(CandidateVideo).where(
            CandidateVideo.media_path.is_not(None),
            CandidateVideo.media_is_user_original.is_(False),
            CandidateVideo.origin != "local_upload",
        )
    ).all()
    removed = 0
    for cand in rows:
        if cand.media_downloaded_at is None:
            # Downloaded before the download time was recorded: the countdown starts now.
            cand.media_downloaded_at = now
            continue
        if now - cand.media_downloaded_at < keep_for or _source_in_use(db, cand.id):
            continue
        rel = cand.media_path
        if rel is None:
            continue
        _remove_source(db, cfg, cand, rel, "nedladdad för längre än gränsen i inställningarna")
        removed += 1
    return removed


def sweep(db: Session, cfg: Config, now=None, *, source_media_hours: int | None = None) -> dict[str, int]:
    """Apply every rule to the clips and sources that still have files. Safe to run repeatedly.

    ``source_media_hours`` is the ``source_media_retention_hours`` setting. ``None`` skips the source rule.
    """
    now = now or utcnow()
    counts = {"declined": 0, "uploaded": 0}
    rows = db.scalars(select(RenderedClip).where(RenderedClip.deleted_at.is_(None))).all()
    for r in rows:
        if r.review_status == ReviewStatus.REJECTED:
            if r.review_decided_at is None:
                # Declined before this rule existed: the two days start now.
                r.review_decided_at = now
                continue
            if now - r.review_decided_at >= DECLINED_RETENTION and not _in_flight(db, r.id):
                _remove(db, cfg, r, "avvisat för mer än två dagar sedan")
                counts["declined"] += 1
        elif clear_uploaded_clip(db, cfg, r):
            counts["uploaded"] += 1
    if source_media_hours is not None:
        counts["source"] = sweep_source_media(db, cfg, now, timedelta(hours=source_media_hours))
    return counts
