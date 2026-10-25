"""Publishing: choose destinations for a rendered clip, monitor uploads, retry or cancel them."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_db, require_user
from app.core.errors import AppError, ErrorCode, NotFound
from app.db.models import Channel, ConnectionState, Job, Publication, PublicationStatus, RenderedClip
from app.schemas import serializers as ser
from app.services.audit import service as audit
from app.services.publishing import quota, uploader
from app.services.publishing import service as publishing
from app.services.publishing.deps import PublishDeps
from app.services.publishing.youtube import SCOPES, YouTubeError
from app.services.queue import service as q
from app.services.settings_service import load_settings

router = APIRouter(tags=["publishing"])


class PublishIn(BaseModel):
    channel_ids: list[str] = Field(min_length=1, max_length=20)
    visibility: Literal["private", "unlisted", "public"] | None = None
    schedule_at: datetime | None = None
    title: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=5000)


class RetryIn(BaseModel):
    # Only for "outcome unknown": discard the lost session and upload again from scratch.
    start_over: bool = False


def _deps(request: Request) -> PublishDeps:
    return request.app.state.publish_deps


def _clip(db: Session, rid: str) -> RenderedClip:
    r = db.get(RenderedClip, rid)
    if r is None or r.deleted_at is not None:
        raise NotFound("Klippet")
    return r


def _pub(db: Session, pid: str) -> Publication:
    p = db.get(Publication, pid)
    if p is None:
        raise NotFound("Publiceringen")
    return p


def _out(db: Session, p: Publication) -> dict:
    job = db.get(Job, p.job_id) if p.job_id else None
    return ser.publication(p, job=job, clip_title=p.rendered_clip.title_suggestion)


def _outs(db: Session, rows: list[Publication]) -> list[dict]:
    ids = [p.job_id for p in rows if p.job_id]
    jobs = {j.id: j for j in db.scalars(select(Job).where(Job.id.in_(ids)))} if ids else {}
    return [
        ser.publication(p, job=jobs.get(p.job_id or ""), clip_title=p.rendered_clip.title_suggestion)
        for p in rows
    ]


@router.get("/publishing/status")
def status(request: Request, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    deps = _deps(request)
    s = load_settings(db)
    ok, why = deps.store.available()
    connected = (
        db.scalar(
            select(func.count())
            .select_from(Channel)
            .where(Channel.connection_state == ConnectionState.CONNECTED)
        )
        or 0
    )
    return {
        "client_configured": deps.oauth_client() is not None,
        "redirect_uri": request.app.state.cfg.oauth_redirect_uri,
        "scopes": list(SCOPES),
        "token_store": {"backend": deps.store.name, "available": ok, "reason": why},
        "quota": quota.status(db, s),
        "auto_publish_global_enabled": s.auto_publish_global_enabled,
        "connected_channels": connected,
    }


@router.post("/clips/{rid}/publish")
def publish_clip(
    rid: str,
    body: PublishIn,
    request: Request,
    _: Principal = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict:
    clip = _clip(db, rid)
    results = publishing.request_publication(
        db,
        request.app.state.cfg,
        load_settings(db),
        clip,
        body.channel_ids,
        publishing.PublishOptions(
            visibility=body.visibility,
            schedule_at=body.schedule_at,
            title=body.title,
            description=body.description,
        ),
    )
    db.flush()
    pubs = [db.get(Publication, r.publication_id) for r in results if r.publication_id]
    return {
        "results": [
            {
                "channel_id": r.channel_id,
                "channel_name": r.channel_name,
                "outcome": r.outcome,
                "publication_id": r.publication_id,
                "reasons": r.reasons,
                "url": r.url,
            }
            for r in results
        ],
        "created": sum(1 for r in results if r.outcome in ("created", "retried")),
        "publications": _outs(db, [p for p in pubs if p is not None]),
    }


@router.get("/clips/{rid}/publications")
def clip_publications(rid: str, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    _clip(db, rid)
    rows = list(
        db.scalars(
            select(Publication).where(Publication.rendered_clip_id == rid).order_by(Publication.created_at)
        )
    )
    return {"items": _outs(db, rows)}


@router.get("/publications")
def list_publications(
    status: str | None = Query(default=None, max_length=100),
    channel_id: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: Principal = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict:
    stmt = select(Publication)
    if status:
        wanted = [x for x in status.split(",") if x in {s.value for s in PublicationStatus}]
        stmt = stmt.where(Publication.status.in_(wanted))
    if channel_id:
        stmt = stmt.where(Publication.channel_id == channel_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(db.scalars(stmt.order_by(Publication.created_at.desc()).limit(limit).offset(offset)))
    counts = dict(db.execute(select(Publication.status, func.count()).group_by(Publication.status)).all())
    return {"items": _outs(db, rows), "total": total, "counts": counts, "limit": limit, "offset": offset}


@router.get("/publications/{pid}")
def get_publication(pid: str, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    return _out(db, _pub(db, pid))


@router.post("/publications/{pid}/retry")
def retry_publication(
    pid: str,
    body: RetryIn | None = None,
    _: Principal = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict:
    """Retry one failed/cancelled/blocked upload. Channels that already succeeded are never touched."""
    p = _pub(db, pid)
    if p.external_video_id:
        raise AppError(ErrorCode.CONFLICT, "Videon är redan uppladdad.", status_code=409)
    if p.status not in publishing.RECOVERABLE:
        raise AppError(
            ErrorCode.CONFLICT,
            "Endast misslyckade, blockerade eller avbrutna uppladdningar kan köras om.",
            status_code=409,
        )
    if body and body.start_over:
        # The user confirmed the video is not on YouTube: forget the lost session and start fresh.
        p.upload_session_uri = None
        p.bytes_uploaded = 0
        p.final_chunk_sent = False
    elif p.error_code == ErrorCode.UPLOAD_UNCERTAIN:
        raise AppError(
            ErrorCode.CONFLICT,
            "Utfallet av förra uppladdningen är okänt. Kontrollera YouTube Studio "
            "och välj sedan 'Ladda upp på nytt'.",
            status_code=409,
        )
    p.status = PublicationStatus.QUEUED
    p.error_code = None
    p.safe_error_message = None
    job = db.get(Job, p.job_id) if p.job_id else None
    if job is None or job.status == "succeeded":
        clip = _clip(db, p.rendered_clip_id)
        publishing.enqueue_job(db, p, clip, p.channel, p.automatic)
    else:
        q.manual_retry(db, job.id)
    audit.record(
        db,
        "publication.retry",
        actor="user",
        object_type="publication",
        object_id=pid,
        details={"start_over": bool(body and body.start_over)},
    )
    db.flush()
    return _out(db, p)


@router.post("/publications/{pid}/cancel")
def cancel_publication(pid: str, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    p = publishing.cancel_publication(db, _pub(db, pid))
    db.flush()
    return _out(db, p)


@router.post("/publications/{pid}/refresh")
def refresh_publication(
    pid: str, request: Request, _: Principal = Depends(require_user), db: Session = Depends(get_db)
) -> dict:
    """Ask YouTube what the video's current state is (costs 1 quota unit)."""
    p = _pub(db, pid)
    if not p.external_video_id:
        raise AppError(ErrorCode.CONFLICT, "Videon är inte uppladdad ännu.", status_code=409)
    try:
        uploader.refresh_availability(db, _deps(request), load_settings(db), [p], strict=True)
    except YouTubeError as e:
        raise AppError(
            ErrorCode.UPLOAD_FAILED, f"Kunde inte läsa status från YouTube: {e.safe_message}"
        ) from e
    db.refresh(p)
    return _out(db, p)


@router.delete("/publications/{pid}")
def forget_publication(pid: str, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    publishing.forget_publication(db, _pub(db, pid), allow_uploaded_missing=True)
    return {"ok": True}
