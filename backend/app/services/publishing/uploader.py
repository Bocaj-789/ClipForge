"""The ``publish`` job: upload one rendered clip to one YouTube channel.

Guarantees, in order of importance:

1. **No duplicate uploads.** The resumable-session URI and the "last byte sent" flag are persisted
   before/while bytes move. A retry first asks YouTube what it has (``PUT bytes */N``). If the
   previous attempt may have completed but the answer was lost and the session is gone, ClipForge
   looks the video up in the channel's recent uploads; if that is inconclusive it stops and asks
   the user (``upload_outcome_unknown``) instead of guessing.
2. **No fake success.** A publication is ``uploaded`` only after YouTube returned a video id.
   Whether the video is *visible* is a separate field (``availability``) read back from the API.
3. **Failures stay local.** Every outcome is classified: quota → defer to the Pacific-time reset,
   rate limit / 5xx / network → retry with backoff, revoked token → block that channel only,
   invalid metadata → fail permanently.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Config
from app.core.errors import ErrorCode, blocked, permanent, transient
from app.db.base import utcnow
from app.db.models import (
    Availability,
    Channel,
    ConnectionState,
    Job,
    Publication,
    PublicationStatus,
    RenderedClip,
    ReviewStatus,
    Visibility,
)
from app.services.acquisition.base import rights_ok
from app.services.acquisition.local_file import sha256_path
from app.services.audit import service as audit
from app.services.publishing import quota
from app.services.publishing.deps import PublishDeps
from app.services.publishing.oauth import mark_needs_reauth
from app.services.publishing.policy import next_slot
from app.services.publishing.service import MIN_SCHEDULE_LEAD, clip_file, mark_clip_published
from app.services.publishing.youtube import (
    SessionState,
    YouTubeApi,
    YouTubeError,
    build_video_resource,
    derive_availability,
)
from app.services.rendering.ffmpeg import Cancelled
from app.services.retention import clear_uploaded_clip
from app.services.settings_service import AppSettings

log = logging.getLogger("clipforge.upload")

MIME = "video/mp4"
QUOTA_RESET_MARGIN = timedelta(minutes=5)


class UploadDeferred(Exception):  # noqa: N818 - control-flow signal, not an error
    """Put the job back in the queue without using up a retry (quota, schedule)."""

    def __init__(self, until: datetime, reason: str) -> None:
        super().__init__(reason)
        self.until = until
        self.reason = reason


class _SessionLost(Exception):  # noqa: N818
    pass


Progress = Callable[[float, str | None], None]


# ------------------------------------------------------------------ error mapping
def _record(db: Session, pub: Publication, code: str, message: str, *, status: str | None = None) -> None:
    pub.error_code = code
    pub.safe_error_message = message[:1000]
    if status:
        pub.status = status
    db.commit()


def _raise_for(db: Session, pub: Publication, channel: Channel, e: YouTubeError) -> NoReturn:
    """Translate a classified Google failure into the right queue behaviour. Never returns."""
    msg = e.safe_message
    if e.kind == "quota":
        until = quota.next_reset() + QUOTA_RESET_MARGIN
        _record(
            db,
            pub,
            ErrorCode.API_QUOTA_EXCEEDED,
            "YouTube-kvoten är slut för i dag. Fortsätter automatiskt efter nollställningen.",
            status=PublicationStatus.QUEUED,
        )
        raise UploadDeferred(until, "Väntar på YouTube-kvot")
    if e.kind in ("auth", "auth_revoked", "scope"):
        text = "YouTube nekade åtkomst. Anslut kanalen igen under Kanaler."
        mark_needs_reauth(db, channel, text)
        _record(db, pub, ErrorCode.API_AUTH_EXPIRED, text, status=PublicationStatus.BLOCKED)
        raise blocked(ErrorCode.API_AUTH_EXPIRED, text)
    if e.kind == "client":
        text = f"Google avvisade ClipForges OAuth-klient eller API-åtkomst: {msg}"
        _record(db, pub, ErrorCode.FORBIDDEN, text, status=PublicationStatus.BLOCKED)
        raise blocked(ErrorCode.FORBIDDEN, text)
    if e.kind == "forbidden":
        text = f"YouTube tillåter inte uppladdningen: {msg}"
        _record(db, pub, ErrorCode.FORBIDDEN, text, status=PublicationStatus.BLOCKED)
        raise blocked(ErrorCode.FORBIDDEN, text)
    if e.kind == "invalid":
        text = f"YouTube avvisade uppgifterna ({e.reason or 'ogiltigt'}): {msg}"
        _record(db, pub, ErrorCode.UPLOAD_FAILED, text, status=PublicationStatus.FAILED)
        raise permanent(ErrorCode.UPLOAD_FAILED, text)
    if e.kind == "upload_limit":
        text = "Kanalen har nått YouTubes egen dagsgräns för uppladdningar. Nytt försök senare."
        _record(db, pub, ErrorCode.UPLOAD_FAILED, text, status=PublicationStatus.QUEUED)
        raise transient(ErrorCode.UPLOAD_FAILED, text, retry_after=6 * 3600)
    if e.kind == "not_found":
        text = f"YouTube hittade inte resursen: {msg}"
        _record(db, pub, ErrorCode.UPLOAD_FAILED, text, status=PublicationStatus.FAILED)
        raise permanent(ErrorCode.UPLOAD_FAILED, text)
    # rate / transient / unknown: retry with the queue's backoff
    code = ErrorCode.RATE_LIMITED if e.kind == "rate" else ErrorCode.UPLOAD_FAILED
    text = f"Tillfälligt fel mot YouTube, försöker igen: {msg}"
    _record(db, pub, code, text, status=PublicationStatus.QUEUED)
    raise transient(code, text, retry_after=e.retry_after)


# ------------------------------------------------------------------ checks
def _load(db: Session, job: Job) -> tuple[Publication, Channel, RenderedClip]:
    pub = db.get(Publication, (job.input_json or {}).get("publication_id") or "")
    if pub is None:
        raise permanent(ErrorCode.NOT_FOUND, "Publiceringen finns inte längre.")
    channel = db.get(Channel, pub.channel_id)
    clip = db.get(RenderedClip, pub.rendered_clip_id)
    if channel is None or clip is None:
        raise permanent(ErrorCode.NOT_FOUND, "Kanalen eller klippet finns inte längre.")
    return pub, channel, clip


def _preflight(db: Session, cfg: Config, pub: Publication, channel: Channel, clip: RenderedClip) -> Path:
    """Re-check everything at upload time: rights, approval, connection, privacy opt-in, file."""

    def stop(code: ErrorCode, text: str) -> NoReturn:
        _record(db, pub, code, text, status=PublicationStatus.BLOCKED)
        raise blocked(code, text)

    if clip.deleted_at is not None:
        text = "Klippet har tagits bort."
        _record(db, pub, ErrorCode.NOT_FOUND, text, status=PublicationStatus.FAILED)
        raise permanent(ErrorCode.NOT_FOUND, text)
    if not channel.token_reference or channel.connection_state != ConnectionState.CONNECTED:
        stop(ErrorCode.API_AUTH_EXPIRED, "Kanalen är inte ansluten. Anslut den under Kanaler.")
    video = clip.clip.video if clip.clip is not None else None
    if video is None or not rights_ok(video.rights_status):
        stop(ErrorCode.RIGHTS_NOT_CONFIRMED, "Rättigheterna för källan är inte bekräftade.")
    if clip.review_status == ReviewStatus.REJECTED:
        stop(ErrorCode.FORBIDDEN, "Klippet har avvisats och laddas inte upp.")
    if (pub.visibility == Visibility.PUBLIC or pub.scheduled_for is not None) and not channel.allow_public:
        stop(
            ErrorCode.FORBIDDEN,
            "Offentlig publicering är inte tillåten för kanalen. Slå på det i kanalinställningarna.",
        )
    path = clip_file(cfg, clip)
    if path is None:
        text = "Den renderade videofilen saknas eller har ändrats."
        _record(db, pub, ErrorCode.CORRUPT_MEDIA, text, status=PublicationStatus.FAILED)
        raise permanent(ErrorCode.CORRUPT_MEDIA, text)
    return path


def _effective_publish_at(db: Session, pub: Publication, channel: Channel) -> datetime | None:
    """A schedule that slipped into the past is handled by the channel's late policy."""
    when = pub.scheduled_for
    if when is None:
        return None
    now = utcnow()
    if when - now >= MIN_SCHEDULE_LEAD:
        return when
    if channel.late_policy == "skip":
        text = "Den planerade tiden har passerat och kanalens regel är att hoppa över."
        _record(db, pub, ErrorCode.UPLOAD_FAILED, text, status=PublicationStatus.FAILED)
        raise permanent(ErrorCode.UPLOAD_FAILED, text)
    if channel.release_spacing_minutes:
        # Spaced releases keep their place in the chain; only a release time that slipped is moved.
        pub.scheduled_for = now + MIN_SCHEDULE_LEAD + timedelta(minutes=1)
        db.commit()
        return pub.scheduled_for
    slot = next_slot(channel, now + MIN_SCHEDULE_LEAD)
    if slot is None:
        text = "Ingen ledig publiceringstid hittades de närmaste två veckorna."
        _record(db, pub, ErrorCode.UPLOAD_FAILED, text, status=PublicationStatus.FAILED)
        raise permanent(ErrorCode.UPLOAD_FAILED, text)
    pub.scheduled_for = slot
    db.commit()
    return slot


# ------------------------------------------------------------------ reconcile after a lost answer
def _adopt_recent_upload(api: YouTubeApi, pub: Publication) -> dict[str, Any] | None:
    """Did the upload we could not confirm actually land? Match on title + time in recent uploads."""
    since = (pub.session_created_at or pub.created_at) - timedelta(minutes=5)
    for item in api.recent_uploads():
        sn = item.get("snippet") or {}
        vid = (item.get("contentDetails") or {}).get("videoId") or (sn.get("resourceId") or {}).get("videoId")
        if not vid or sn.get("title") != pub.title:
            continue
        try:
            published = datetime.fromisoformat(str(sn.get("publishedAt", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if published >= since:
            full = api.videos_status([vid]).get(vid)
            return full or {"id": vid}
    return None


# ------------------------------------------------------------------ transfer
def _put_chunk(
    api: YouTubeApi, deps: PublishDeps, uri: str, data: bytes, *, start: int, total: int
) -> SessionState:
    """Send one chunk. On a transient failure wait, ask YouTube where it stands and return that."""
    attempt = 0
    while True:
        try:
            return api.put_chunk(uri, data, start=start, total=total, mime=MIME)
        except YouTubeError as e:
            if e.kind not in ("transient", "rate") or attempt >= deps.chunk_retries:
                raise
            delay = max(e.retry_after or 0.0, min(60.0, 2.0**attempt + random.uniform(0, 1)))  # noqa: S311
            log.warning("chunk upload failed (%s); retrying in %.0fs", e.kind, delay)
            deps.sleep(delay)
            attempt += 1
            try:
                return api.query_session(uri, total)
            except YouTubeError as qe:
                if qe.kind not in ("transient", "rate"):
                    raise
                # Could not even ask: loop and try the chunk again (resumable PUTs are idempotent).


def _transfer(
    db: Session,
    api: YouTubeApi,
    deps: PublishDeps,
    pub: Publication,
    path: Path,
    offset: int,
    progress: Progress,
    cancelled: Callable[[], bool],
) -> dict[str, Any]:
    size = path.stat().st_size
    uri = pub.upload_session_uri
    assert uri is not None
    with path.open("rb") as fh:
        while True:
            if cancelled():
                db.commit()
                raise Cancelled()
            n = min(deps.chunk_size, size - offset)
            fh.seek(offset)
            data = fh.read(n)
            if len(data) != n:
                raise permanent(ErrorCode.CORRUPT_MEDIA, "Videofilen kunde inte läsas helt.")
            last = offset + n >= size
            if last:
                # Persist *before* sending: if the answer to this request is lost we must know that
                # the whole file may have reached YouTube.
                pub.final_chunk_sent = True
                db.commit()
            st = _put_chunk(api, deps, uri, data, start=offset, total=size)
            if st.video is not None:
                return st.video
            if st.gone:
                raise _SessionLost()
            if st.committed < size:
                pub.final_chunk_sent = False
            offset = st.committed
            pub.bytes_uploaded = offset
            db.commit()
            progress(min(95.0, offset / size * 95.0), "laddar upp")


def _upload(
    db: Session,
    api: YouTubeApi,
    deps: PublishDeps,
    pub: Publication,
    channel: Channel,
    clip: RenderedClip,
    path: Path,
    progress: Progress,
    cancelled: Callable[[], bool],
) -> dict[str, Any]:
    size = path.stat().st_size
    for _round in range(2):  # at most one fresh session after an expired one
        offset = 0
        if pub.upload_session_uri:
            progress(1, "kontrollerar tidigare uppladdning")
            st = api.query_session(pub.upload_session_uri, size)
            if st.video is not None:
                return st.video
            if st.gone:
                if pub.final_chunk_sent:
                    found = _adopt_recent_upload(api, pub)
                    if found is not None:
                        log.info("adopted an upload that completed without confirmation")
                        return found
                    text = (
                        "ClipForge vet inte om förra uppladdningen gick igenom: sessionen har gått ut och "
                        "videon hittades inte bland kanalens senaste uppladdningar. "
                        "Kontrollera YouTube Studio. "
                        "Saknas videon där, välj 'Ladda upp på nytt'."
                    )
                    _record(db, pub, ErrorCode.UPLOAD_UNCERTAIN, text, status=PublicationStatus.BLOCKED)
                    raise blocked(ErrorCode.UPLOAD_UNCERTAIN, text)
                pub.upload_session_uri = None
                pub.bytes_uploaded = 0
                db.commit()
            else:
                offset = st.committed
        if not pub.upload_session_uri:
            if clip.checksum and sha256_path(path) != clip.checksum:
                text = "Den renderade filen har ändrats sedan rendering och laddas inte upp."
                _record(db, pub, ErrorCode.CORRUPT_MEDIA, text, status=PublicationStatus.FAILED)
                raise permanent(ErrorCode.CORRUPT_MEDIA, text)
            publish_at = _effective_publish_at(db, pub, channel)
            resource = build_video_resource(
                title=pub.title,
                description=pub.description,
                tags=list(pub.tags_json or []),
                category_id=pub.category_id,
                language=pub.language,
                privacy=pub.visibility,
                made_for_kids=pub.made_for_kids,
                publish_at=publish_at,
            )
            progress(2, "startar uppladdning")
            pub.upload_session_uri = api.start_resumable(
                resource, size=size, mime=MIME, notify_subscribers=pub.notify_subscribers
            )
            pub.session_created_at = utcnow()
            pub.bytes_uploaded = 0
            pub.final_chunk_sent = False
            db.commit()
            offset = 0
        try:
            return _transfer(db, api, deps, pub, path, offset, progress, cancelled)
        except _SessionLost:
            if pub.final_chunk_sent:
                continue  # the next round queries/reconciles instead of re-uploading blindly
            pub.upload_session_uri = None
            pub.bytes_uploaded = 0
            db.commit()
    raise transient(ErrorCode.UPLOAD_FAILED, "Uppladdningssessionen avbröts upprepade gånger.")


# ------------------------------------------------------------------ success bookkeeping
def apply_video_state(pub: Publication, video: dict[str, Any]) -> None:
    state = derive_availability(video)
    pub.availability = state.availability
    pub.privacy_actual = state.privacy
    pub.youtube_upload_status = state.upload_status
    pub.rejection_reason = state.rejection_reason
    pub.last_checked_at = utcnow()
    notice = None
    if (
        state.privacy == "private"
        and pub.visibility in (Visibility.PUBLIC, Visibility.UNLISTED)
        and pub.scheduled_for is None
    ):
        notice = (
            "YouTube gjorde videon privat trots att den begärdes som "
            f"{'offentlig' if pub.visibility == Visibility.PUBLIC else 'olistad'}. "
            "Det kan bero på kontoinställningar eller begränsningar för API-projektet. "
            "Ändra synlighet i YouTube Studio."
        )
    meta = dict(pub.metadata_json or {})
    if notice:
        meta["notice"] = notice
    else:
        meta.pop("notice", None)
    pub.metadata_json = meta


def _record_success(
    db: Session, pub: Publication, channel: Channel, clip: RenderedClip, video: dict[str, Any]
) -> None:
    vid = str(video["id"])
    now = utcnow()
    pub.external_video_id = vid
    pub.external_url = f"https://www.youtube.com/watch?v={vid}"
    pub.status = PublicationStatus.UPLOADED
    pub.uploaded_at = now
    pub.upload_session_uri = None
    pub.final_chunk_sent = False
    pub.bytes_uploaded = pub.file_size_bytes or pub.bytes_uploaded
    pub.error_code = None
    pub.safe_error_message = None
    apply_video_state(pub, video)
    channel.last_upload_at = now
    mark_clip_published(clip)
    audit.record(
        db,
        "publication.uploaded",
        object_type="publication",
        object_id=pub.id,
        details={
            "channel_id": channel.id,
            "video_id": vid,
            "availability": pub.availability,
            "privacy": pub.privacy_actual,
        },
    )
    db.commit()  # the video id must survive whatever happens next


# ------------------------------------------------------------------ entry point
def run_publish(
    db: Session,
    job: Job,
    settings: AppSettings,
    deps: PublishDeps,
    cfg: Config,
    *,
    progress: Progress,
    cancelled: Callable[[], bool],
) -> dict[str, Any]:
    pub, channel, clip = _load(db, job)
    if pub.external_video_id:  # a previous attempt already finished; never upload twice
        pub.status = PublicationStatus.UPLOADED
        db.commit()
        return {
            "publication_id": pub.id,
            "video_id": pub.external_video_id,
            "url": pub.external_url,
            "reused": True,
        }
    path = _preflight(db, cfg, pub, channel, clip)
    pub.status = PublicationStatus.UPLOADING
    pub.file_size_bytes = path.stat().st_size
    db.commit()
    progress(0.5, "förbereder")
    with deps.http_factory() as http:
        api = deps.api_for(db, settings, channel, http)
        try:
            video = _upload(db, api, deps, pub, channel, clip, path, progress, cancelled)
        except YouTubeError as e:
            _raise_for(db, pub, channel, e)
        _record_success(db, pub, channel, clip, video)
        clear_uploaded_clip(db, cfg, clip)
        db.commit()
        progress(97, "kontrollerar status hos YouTube")
        try:
            fresh = api.videos_status([str(video["id"])]).get(str(video["id"]))
            if fresh is not None:
                apply_video_state(pub, fresh)
                db.commit()
        except YouTubeError as e:  # non-fatal: the upload itself succeeded
            log.warning("post-upload status check failed (%s)", e.kind)
            db.rollback()
    return {
        "publication_id": pub.id,
        "video_id": pub.external_video_id,
        "url": pub.external_url,
        "availability": pub.availability,
    }


# ------------------------------------------------------------------ periodic status refresh
def refresh_availability(
    db: Session,
    deps: PublishDeps,
    settings: AppSettings,
    pubs: list[Publication],
    *,
    strict: bool = False,
) -> int:
    """Read back what YouTube reports for uploaded videos (1 quota unit per channel per 50 videos)."""
    by_channel: dict[str, list[Publication]] = {}
    for p in pubs:
        if p.external_video_id:
            by_channel.setdefault(p.channel_id, []).append(p)
    updated = 0
    for cid, items in by_channel.items():
        channel = db.get(Channel, cid)
        if (
            channel is None
            or not channel.token_reference
            or channel.connection_state != ConnectionState.CONNECTED
        ):
            continue
        try:
            with deps.http_factory() as http:
                api = deps.api_for(db, settings, channel, http)
                found = api.videos_status([str(p.external_video_id) for p in items])
        except YouTubeError as e:
            if e.kind in ("auth", "auth_revoked", "scope"):
                mark_needs_reauth(db, channel, "YouTube nekade åtkomst. Anslut kanalen igen.")
                db.commit()
            log.warning("availability refresh failed for a channel (%s)", e.kind)
            if strict:
                raise
            continue
        for p in items:
            item = found.get(str(p.external_video_id))
            if item is None:
                p.availability = Availability.MISSING
                p.last_checked_at = utcnow()
            else:
                apply_video_state(p, item)
            updated += 1
        db.commit()
    return updated


def due_for_refresh(db: Session, *, now: datetime | None = None) -> list[Publication]:
    """Uploaded videos whose YouTube-side state may still change."""
    now = now or utcnow()
    rows = db.scalars(
        select(Publication).where(
            Publication.status == PublicationStatus.UPLOADED,
            Publication.availability.in_(
                [Availability.UNKNOWN, Availability.PROCESSING, Availability.SCHEDULED]
            ),
            Publication.uploaded_at >= now - timedelta(days=14),
        )
    ).all()
    out = []
    for p in rows:
        if p.availability == Availability.SCHEDULED and p.scheduled_for and p.scheduled_for > now:
            continue  # nothing can change before the release time
        if p.last_checked_at and now - p.last_checked_at < timedelta(minutes=10):
            continue
        out.append(p)
    return out
