"""Publication requests: one publication + one background job per destination channel.

Everything that decides *whether* and *what* to upload lives here, so the manual API, the
automatic post-render hook and retries all go through the same checks:

* a unique ``(rendered clip, channel)`` pair makes a duplicate upload impossible at database level;
* metadata is frozen on the publication, so a retry uploads exactly what was approved;
* each channel gets its own job, so one channel failing never blocks or cancels the others.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Config
from app.core.errors import AppError, ErrorCode
from app.core.security import clean_text, contained_path
from app.db.base import utcnow
from app.db.models import (
    Availability,
    Channel,
    ClipStatus,
    ConnectionState,
    Job,
    JobStatus,
    JobType,
    Publication,
    PublicationStatus,
    RenderedClip,
    Visibility,
)
from app.services.audit import service as audit
from app.services.intelligence.metadata import sanitize_description, sanitize_title
from app.services.publishing.policy import check_publication, next_slot
from app.services.queue import service as q
from app.services.queue.states import Priority
from app.services.settings_service import AppSettings

log = logging.getLogger("clipforge.publishing")

MIN_SCHEDULE_LEAD = timedelta(minutes=15)
MAX_DESCRIPTION_BYTES = 4900  # YouTube allows 5000 bytes
MAX_TAGS_CHARS = 450  # YouTube allows 500 characters in total
IN_PROGRESS = (PublicationStatus.QUEUED, PublicationStatus.UPLOADING)
RECOVERABLE = (PublicationStatus.FAILED, PublicationStatus.CANCELLED, PublicationStatus.BLOCKED)


@dataclass
class PublishOptions:
    visibility: str | None = None  # None = the channel's default
    schedule_at: datetime | None = None
    title: str | None = None
    description: str | None = None
    automatic: bool = False


@dataclass
class ChannelOutcome:
    channel_id: str
    channel_name: str
    outcome: str  # created | retried | in_progress | already_uploaded | blocked | not_found
    publication_id: str | None = None
    reasons: list[str] = field(default_factory=list)
    url: str | None = None


# ------------------------------------------------------------------ metadata
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _render_template(template: str, values: dict[str, str]) -> str:
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), ""), template)


def _fit_bytes(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", errors="ignore").rstrip()


def build_tags(hashtags: list[str]) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    total = 0
    for h in hashtags:
        t = clean_text(h.lstrip("#"), 30).replace("<", "").replace(">", "").replace(",", " ").strip()
        if len(t) < 2 or t.lower() in seen:
            continue
        if total + len(t) + 1 > MAX_TAGS_CHARS:
            break
        seen.add(t.lower())
        tags.append(t)
        total += len(t) + 1
    return tags[:15]


def build_metadata(channel: Channel, clip: RenderedClip, opts: PublishOptions) -> dict[str, Any]:
    src = clip.clip.video if clip.clip is not None else None
    base_title = sanitize_title(opts.title if opts.title is not None else clip.title_suggestion)
    base_desc = opts.description if opts.description is not None else clip.description_suggestion
    desc_l = base_desc.lower()
    missing_tags = [h for h in clip.hashtags_json or [] if h.lower() not in desc_l]
    values = {
        "title": base_title,
        "description": base_desc,
        "hashtags": " ".join(missing_tags),
        "source_title": (src.title or "") if src else "",
        "creator": (src.creator_name or "") if src else "",
    }
    title = sanitize_title(_render_template(channel.title_template or "{title}", values)) or base_title
    description = sanitize_description(
        _render_template(channel.description_template or "{description}", values)
    )
    return {
        "title": title,
        "description": _fit_bytes(description, MAX_DESCRIPTION_BYTES),
        "tags": build_tags(list(clip.hashtags_json or [])),
        "category_id": channel.category_id or "22",
        "language": channel.default_language,
        "made_for_kids": bool(channel.made_for_kids),
        "notify_subscribers": bool(channel.notify_subscribers),
    }


# ------------------------------------------------------------------ gates
def clip_file(cfg: Config, clip: RenderedClip):  # type: ignore[no-untyped-def]
    """The rendered MP4 if it is still on disk and unchanged in size, else None."""
    try:
        p = contained_path(cfg.data_dir, clip.file_path)
    except ValueError:
        return None
    if not p.is_file() or p.stat().st_size != clip.file_size_bytes or clip.file_size_bytes <= 0:
        return None
    return p


def _local_midnight(tz: str, now: datetime) -> datetime:
    local = now.astimezone(ZoneInfo(tz))
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def uploads_today(db: Session, settings: AppSettings, channel_id: str | None, now: datetime) -> int:
    """Publications created since local midnight that were not abandoned (they use up a daily slot)."""
    start = _local_midnight(settings.timezone, now)
    stmt = (
        select(func.count())
        .select_from(Publication)
        .where(
            Publication.created_at >= start,
            Publication.status.not_in([PublicationStatus.FAILED, PublicationStatus.CANCELLED]),
        )
    )
    if channel_id is not None:
        stmt = stmt.where(Publication.channel_id == channel_id)
    return db.scalar(stmt) or 0


def _resolve_timing(
    channel: Channel, opts: PublishOptions, now: datetime
) -> tuple[str, datetime | None, list[str]]:
    """Returns (privacy to upload with, publish_at or None, problems)."""
    problems: list[str] = []
    vis = opts.visibility or channel.default_visibility or Visibility.PRIVATE
    if vis not in {v.value for v in Visibility}:
        problems.append("Ogiltig synlighet.")
        vis = Visibility.PRIVATE
    when = opts.schedule_at
    if when is None and opts.automatic and vis == Visibility.PUBLIC:
        # Automation respects the channel's publishing windows: outside them the clip is uploaded
        # privately and YouTube releases it at the next allowed time.
        slot = next_slot(channel, now)
        if slot is not None and slot - now >= MIN_SCHEDULE_LEAD:
            when = slot
    if when is not None:
        if when.tzinfo is None:
            problems.append("Planerad tid saknar tidszon.")
        elif when - now < MIN_SCHEDULE_LEAD:
            problems.append("Planerad tid måste ligga minst 15 minuter fram i tiden.")
        if vis == Visibility.UNLISTED:
            problems.append("Olistade videor kan inte schemaläggas. Välj privat eller offentlig.")
        vis = Visibility.PRIVATE if not problems else vis
    return str(vis), when, problems


# ------------------------------------------------------------------ requests
def enqueue_job(db: Session, pub: Publication, clip: RenderedClip, channel: Channel, automatic: bool) -> Job:
    job = q.enqueue(
        db,
        JobType.PUBLISH,
        priority=Priority.CREATOR if automatic else Priority.USER,
        candidate_video_id=clip.clip.candidate_video_id if clip.clip is not None else None,
        channel_id=channel.id,
        input_json={"publication_id": pub.id},
        idempotency_key=f"publish:{pub.id}",
        max_attempts=5,
    )
    pub.job_id = job.id
    return job


def request_publication(
    db: Session,
    cfg: Config,
    settings: AppSettings,
    clip: RenderedClip,
    channel_ids: list[str],
    opts: PublishOptions,
) -> list[ChannelOutcome]:
    """Create (or recover) one publication and one job for every selected channel."""
    now = utcnow()
    file_ok = clip_file(cfg, clip) is not None
    rights = clip.clip.video.rights_status if clip.clip is not None and clip.clip.video else "unknown"
    out: list[ChannelOutcome] = []
    for cid in dict.fromkeys(channel_ids):  # de-duplicate, keep order
        channel = db.get(Channel, cid)
        if channel is None:
            out.append(ChannelOutcome(cid, "?", "not_found", reasons=["Kanalen finns inte."]))
            continue
        res = ChannelOutcome(channel.id, channel.display_name, "blocked")
        out.append(res)
        existing = db.scalar(
            select(Publication).where(
                Publication.rendered_clip_id == clip.id, Publication.channel_id == channel.id
            )
        )
        if existing is not None and existing.external_video_id:
            res.outcome, res.publication_id, res.url = "already_uploaded", existing.id, existing.external_url
            res.reasons = ["Klippet är redan uppladdat till den här kanalen."]
            continue
        if existing is not None and existing.status in IN_PROGRESS:
            res.outcome, res.publication_id = "in_progress", existing.id
            res.reasons = ["Uppladdningen till den här kanalen pågår redan."]
            continue

        privacy, publish_at, problems = _resolve_timing(channel, opts, now)
        decision = check_publication(
            settings=settings,
            channel=channel,
            clip=clip,
            rights_status=rights,
            automatic=opts.automatic,
            uploads_today_channel=uploads_today(db, settings, channel.id, now),
            uploads_today_global=uploads_today(db, settings, None, now),
            last_upload_at=channel.last_upload_at,
            now=now,
            file_ok=file_ok,
            min_score=channel.auto_publish_min_score,
            visibility=privacy,
            scheduled=publish_at is not None,
        )
        reasons = [*problems, *decision.reasons]
        if reasons:
            res.reasons = reasons
            if opts.automatic:
                audit.record(
                    db,
                    "publication.auto_skipped",
                    object_type="channel",
                    object_id=channel.id,
                    details={"rendered_clip_id": clip.id, "reasons": reasons},
                )
            continue

        meta = build_metadata(channel, clip, opts)
        if existing is None:
            pub = Publication(rendered_clip_id=clip.id, channel_id=channel.id)
            db.add(pub)
            outcome = "created"
        else:
            pub = existing
            outcome = "retried"
        # Metadata can only change before YouTube created an upload session for this publication.
        if outcome == "created" or not pub.upload_session_uri:
            pub.title, pub.description, pub.tags_json = meta["title"], meta["description"], meta["tags"]
            pub.category_id, pub.language = meta["category_id"], meta["language"]
            pub.made_for_kids, pub.notify_subscribers = meta["made_for_kids"], meta["notify_subscribers"]
            pub.visibility, pub.scheduled_for = privacy, publish_at
        pub.automatic = opts.automatic
        pub.file_size_bytes = clip.file_size_bytes
        pub.status = PublicationStatus.QUEUED
        pub.error_code = None
        pub.safe_error_message = None
        pub.final_chunk_sent = pub.final_chunk_sent if pub.upload_session_uri else False
        try:
            with db.begin_nested():
                db.flush()
        except IntegrityError:
            # Lost a race with a parallel request for the same clip + channel: that one wins.
            res.outcome, res.reasons = "in_progress", ["Uppladdningen till den här kanalen pågår redan."]
            continue

        job = db.get(Job, pub.job_id) if pub.job_id else None
        if job is not None and job.status in (
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.BLOCKED,
            JobStatus.AWAITING_MEDIA,
        ):
            q.manual_retry(db, job.id)
        elif job is None or job.status == JobStatus.SUCCEEDED:
            enqueue_job(db, pub, clip, channel, opts.automatic)
        res.outcome, res.publication_id = outcome, pub.id
        audit.record(
            db,
            "publication.requested",
            actor="system" if opts.automatic else "user",
            object_type="publication",
            object_id=pub.id,
            details={
                "channel_id": channel.id,
                "rendered_clip_id": clip.id,
                "visibility": pub.visibility,
                "scheduled": publish_at is not None,
                "automatic": opts.automatic,
                "retry": outcome == "retried",
            },
        )
    return out


def auto_publish_after_render(
    db: Session, cfg: Config, settings: AppSettings, clip: RenderedClip
) -> list[ChannelOutcome]:
    """Hook called when a clip finished rendering. Only channels that opted in are considered."""
    if not settings.auto_publish_global_enabled:
        return []
    ids = list(
        db.scalars(
            select(Channel.id).where(
                Channel.auto_publish_enabled.is_(True),
                Channel.enabled.is_(True),
                Channel.paused.is_(False),
                Channel.token_reference.is_not(None),
                Channel.connection_state == ConnectionState.CONNECTED,
            )
        )
    )
    if not ids:
        return []
    return request_publication(db, cfg, settings, clip, ids, PublishOptions(automatic=True))


# ------------------------------------------------------------------ job <-> publication sync
def sync_with_job(db: Session, pub: Publication, job: Job | None) -> None:
    """Make the publication reflect its job after a cancel/fail/crash that the handler could not record."""
    if pub.external_video_id or pub.status == PublicationStatus.UPLOADED or job is None:
        return
    if job.status == JobStatus.CANCELLED:
        pub.status = PublicationStatus.CANCELLED
        pub.error_code, pub.safe_error_message = None, None
    elif job.status == JobStatus.FAILED:
        pub.status = PublicationStatus.FAILED
        pub.error_code, pub.safe_error_message = job.error_code, job.safe_error_message
    elif job.status in (JobStatus.BLOCKED, JobStatus.AWAITING_MEDIA):
        pub.status = PublicationStatus.BLOCKED
        pub.error_code, pub.safe_error_message = job.error_code, job.safe_error_message
    elif job.status in (JobStatus.PENDING, JobStatus.RETRY_WAIT):
        if pub.status in (PublicationStatus.UPLOADING, PublicationStatus.FAILED, PublicationStatus.CANCELLED):
            pub.status = PublicationStatus.QUEUED
        if job.status == JobStatus.PENDING and job.attempt_count == 0:
            pub.error_code, pub.safe_error_message = None, None


def reconcile(db: Session) -> int:
    """Worker housekeeping: repair publications whose job moved on without the handler noticing."""
    n = 0
    rows = db.scalars(
        select(Publication).where(
            Publication.status.in_([PublicationStatus.QUEUED, PublicationStatus.UPLOADING]),
            Publication.job_id.is_not(None),
        )
    ).all()
    for pub in rows:
        job = db.get(Job, pub.job_id) if pub.job_id else None
        before = pub.status
        if job is not None and job.status != JobStatus.RUNNING:
            sync_with_job(db, pub, job)
        if pub.status != before:
            n += 1
    return n


def sync_job_change(db: Session, job: Job) -> None:
    """Called by the generic job endpoints (cancel/retry) so publications never go stale."""
    if job.job_type != JobType.PUBLISH:
        return
    pub_id = (job.input_json or {}).get("publication_id")
    pub = db.get(Publication, pub_id) if pub_id else None
    if pub is not None:
        sync_with_job(db, pub, job)


# ------------------------------------------------------------------ user actions on one publication
def cancel_publication(db: Session, pub: Publication) -> Publication:
    if pub.external_video_id or pub.status not in (*IN_PROGRESS, PublicationStatus.BLOCKED):
        raise AppError(
            ErrorCode.CONFLICT, "Uppladdningen kan inte avbrytas i sitt nuvarande läge.", status_code=409
        )
    job = db.get(Job, pub.job_id) if pub.job_id else None
    if job is not None and job.status in (
        JobStatus.PENDING,
        JobStatus.RETRY_WAIT,
        JobStatus.RUNNING,
        JobStatus.BLOCKED,
        JobStatus.AWAITING_MEDIA,
    ):
        q.request_cancel(db, job.id)
    if job is None or job.status != JobStatus.RUNNING:
        pub.status = PublicationStatus.CANCELLED
    audit.record(db, "publication.cancel", actor="user", object_type="publication", object_id=pub.id)
    return pub


def forget_publication(db: Session, pub: Publication, *, allow_uploaded_missing: bool = False) -> None:
    """Delete a failed/cancelled record (frees the clip + channel pair for a fresh start).

    An uploaded record may only be removed if YouTube says the video no longer exists.
    """
    if pub.status in IN_PROGRESS:
        raise AppError(ErrorCode.CONFLICT, "Avbryt uppladdningen först.", status_code=409)
    if pub.external_video_id and not (allow_uploaded_missing and pub.availability == Availability.MISSING):
        raise AppError(
            ErrorCode.CONFLICT,
            "Videon finns på YouTube och kan inte tas bort från historiken. "
            "Ta bort den i YouTube Studio först.",
            status_code=409,
        )
    audit.record(db, "publication.forgotten", actor="user", object_type="publication", object_id=pub.id)
    job = db.get(Job, pub.job_id) if pub.job_id else None
    pid = pub.id
    db.delete(pub)
    db.flush()
    if job is not None:
        db.delete(job)
    log.info("publication %s removed", pid)


def mark_clip_published(clip: RenderedClip) -> None:
    if clip.clip is not None:
        clip.clip.status = ClipStatus.PUBLISHED
