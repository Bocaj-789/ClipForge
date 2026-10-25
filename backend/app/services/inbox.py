"""Priority inbox: user-submitted URLs and files. Business logic kept out of route handlers."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, ErrorCode
from app.core.security import clean_text
from app.db.models import CandidateStatus, CandidateVideo, Job, JobStatus, JobType, RightsStatus
from app.services.acquisition.base import rights_message, rights_ok
from app.services.acquisition.local_file import StoredMedia
from app.services.audit import service as audit
from app.services.queue import service as q
from app.services.queue.states import Priority
from app.services.settings_service import AppSettings
from app.services.urls import UrlKind, UrlRejected, classify_url


@dataclass
class SubmitResult:
    candidate: CandidateVideo
    duplicate: bool
    priority_raised: bool


def _priority(urgent: bool) -> int:
    return Priority.URGENT if urgent else Priority.USER


def _cancel_blocked(db: Session, cand_id: str) -> None:
    for j in db.scalars(
        select(Job).where(Job.candidate_video_id == cand_id, Job.status == JobStatus.BLOCKED)
    ):
        q.mark_cancelled(db, j)


def resume_pipeline(db: Session, cand: CandidateVideo) -> Job:
    """(Re)start processing from the earliest needed stage."""
    _cancel_blocked(db, cand.id)
    active = q.active_job_for(db, cand.id)
    if active is not None:
        return active
    cand.status = CandidateStatus.QUEUED
    cand.blocked_reason = None
    cand.last_error = None
    jt = JobType.ANALYZE if cand.media_path else JobType.FETCH_METADATA
    return q.enqueue(db, jt, priority=cand.priority, candidate_video_id=cand.id, source_id=cand.source_id)


def submit_url(
    db: Session,
    raw_url: str,
    *,
    urgent: bool,
    note: str | None,
    rights_status: str,
    rights_note: str | None,
    settings: AppSettings,
) -> SubmitResult:
    try:
        c = classify_url(raw_url, direct_media_hosts=settings.allowed_direct_media_hosts)
    except UrlRejected as e:
        raise AppError(ErrorCode.UNSUPPORTED_URL, str(e)) from e
    if c.kind == UrlKind.YOUTUBE_CHANNEL:
        raise AppError(
            ErrorCode.UNSUPPORTED_URL,
            "Det här är en kanaladress. Lägg till den som källa under Upptäckt istället.",
        )
    pri = _priority(urgent)
    existing = db.scalar(select(CandidateVideo).where(CandidateVideo.canonical_url == c.canonical_url))
    if existing is not None:
        raised = False
        if pri < existing.priority:
            existing.priority = pri
            q.raise_priority_for_candidate(db, existing.id, pri)
            raised = True
        if note:
            existing.user_note = clean_text(note, 1000)
        audit.record(
            db,
            "inbox.duplicate",
            actor="user",
            object_type="candidate",
            object_id=existing.id,
            details={"priority_raised": raised},
        )
        return SubmitResult(existing, True, raised)
    cand = CandidateVideo(
        origin="manual_url",
        canonical_url=c.canonical_url,
        platform=c.platform,
        external_id=c.external_id,
        priority=pri,
        rights_status=RightsStatus(rights_status),
        rights_note=clean_text(rights_note or "", 2000) or None,
        user_note=clean_text(note or "", 1000) or None,
        status=CandidateStatus.QUEUED,
    )
    db.add(cand)
    db.flush()
    q.enqueue(db, JobType.FETCH_METADATA, priority=pri, candidate_video_id=cand.id)
    audit.record(
        db,
        "inbox.submitted",
        actor="user",
        object_type="candidate",
        object_id=cand.id,
        details={"platform": c.platform, "urgent": urgent, "rights": rights_status},
    )
    return SubmitResult(cand, False, False)


def import_file(
    db: Session,
    stored: StoredMedia,
    *,
    title: str | None,
    urgent: bool,
    note: str | None,
    rights_status: str,
    rights_note: str | None,
) -> SubmitResult:
    key = f"local:{stored.checksum}"
    existing = db.scalar(select(CandidateVideo).where(CandidateVideo.canonical_url == key))
    if existing is not None:
        return SubmitResult(existing, True, False)
    cand = CandidateVideo(
        origin="local_upload",
        canonical_url=key,
        platform="local",
        external_id=stored.checksum[:16],
        title=clean_text(title or "", 300) or None,
        priority=_priority(urgent),
        rights_status=RightsStatus(rights_status),
        rights_note=clean_text(rights_note or "", 2000) or None,
        user_note=clean_text(note or "", 1000) or None,
        media_path=stored.rel_path,
        media_checksum=stored.checksum,
        duration_seconds=round(stored.info.duration, 3),
        status=CandidateStatus.QUEUED,
    )
    db.add(cand)
    db.flush()
    if rights_ok(cand.rights_status):
        q.enqueue(db, JobType.ANALYZE, priority=cand.priority, candidate_video_id=cand.id)
    else:
        cand.status = CandidateStatus.BLOCKED
        cand.blocked_reason = rights_message(cand.rights_status)
    audit.record(
        db,
        "inbox.file_imported",
        actor="user",
        object_type="candidate",
        object_id=cand.id,
        details={"bytes": stored.size, "rights": rights_status},
    )
    return SubmitResult(cand, False, False)


def attach_media(db: Session, cand: CandidateVideo, stored: StoredMedia) -> CandidateVideo:
    if cand.media_path:
        raise AppError(ErrorCode.CONFLICT, "Videon har redan en mediefil.", status_code=409)
    cand.media_path = stored.rel_path
    cand.media_checksum = stored.checksum
    cand.duration_seconds = round(stored.info.duration, 3)
    audit.record(db, "candidate.media_attached", actor="user", object_type="candidate", object_id=cand.id)
    if rights_ok(cand.rights_status):
        resume_pipeline(db, cand)
    return cand


def set_rights(db: Session, cand: CandidateVideo, rights_status: str, note: str | None) -> CandidateVideo:
    prev = cand.rights_status
    cand.rights_status = RightsStatus(rights_status)
    cand.rights_note = clean_text(note or "", 2000) or None
    audit.record(
        db,
        "candidate.rights_changed",
        actor="user",
        object_type="candidate",
        object_id=cand.id,
        details={"from": prev, "to": rights_status},
    )
    if rights_ok(cand.rights_status) and cand.status in (CandidateStatus.BLOCKED, CandidateStatus.DISCOVERED):
        resume_pipeline(db, cand)
    elif not rights_ok(cand.rights_status):
        for j in db.scalars(
            select(Job).where(
                Job.candidate_video_id == cand.id, Job.status.in_([JobStatus.PENDING, JobStatus.RETRY_WAIT])
            )
        ):
            q.mark_cancelled(db, j)
        cand.status = CandidateStatus.BLOCKED
        cand.blocked_reason = rights_message(cand.rights_status)
    return cand
