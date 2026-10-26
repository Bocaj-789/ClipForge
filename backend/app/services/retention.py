"""Automatic clean-up of rendered clips that are no longer needed.

Two rules:

* A declined clip is removed two days after it was declined.
* A clip is removed as soon as YouTube has confirmed the upload for every channel it was selected for.

Only the rendered files (video, thumbnail, captions) are deleted. The database row stays, hidden via
``deleted_at``, so the publication history and YouTube links are kept. A clip still needed for a queued or
failed upload is never removed.
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
from app.db.models import ClipStatus, Publication, PublicationStatus, RenderedClip, ReviewStatus
from app.services.audit import service as audit
from app.services.storage.manager import invalidate_cache

log = logging.getLogger("clipforge.retention")
DECLINED_RETENTION = timedelta(days=2)
IN_FLIGHT = (PublicationStatus.QUEUED, PublicationStatus.UPLOADING)


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


def sweep(db: Session, cfg: Config, now=None) -> dict[str, int]:
    """Apply both rules to every clip that still has files. Safe to run repeatedly."""
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
    return counts
