from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_db, require_user
from app.core.errors import AppError, ErrorCode, NotFound
from app.core.security import clean_text
from app.db.base import new_id
from app.db.models import CandidateVideo, JobType, Source
from app.schemas import serializers as ser
from app.services.audit import service as audit
from app.services.queue import service as q
from app.services.queue.states import Priority
from app.services.urls import UrlKind, UrlRejected, classify_url

router = APIRouter(prefix="/sources", tags=["sources"])
Kind = Literal["youtube_channel", "rss", "local_folder"]
Rights = Literal[
    "unknown", "user_owned", "licensed", "public_domain_verified", "permission_confirmed", "not_permitted"
]


class SourceIn(BaseModel):
    kind: Kind
    name: str = Field(min_length=1, max_length=200)
    url: str | None = Field(default=None, max_length=2048)
    polling_interval_minutes: int = Field(default=180, ge=15, le=7 * 24 * 60)
    category: str | None = Field(default=None, max_length=100)
    keywords: list[str] = Field(default_factory=list, max_length=30)
    rights_status: Rights = "unknown"
    rights_note: str | None = Field(default=None, max_length=2000)
    enabled: bool = True


class SourcePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    polling_interval_minutes: int | None = Field(default=None, ge=15, le=7 * 24 * 60)
    category: str | None = Field(default=None, max_length=100)
    keywords: list[str] | None = Field(default=None, max_length=30)
    rights_status: Rights | None = None
    rights_note: str | None = Field(default=None, max_length=2000)
    enabled: bool | None = None


def _validate_url(kind: str, url: str | None) -> str | None:
    if kind == "local_folder":
        return None
    try:
        c = classify_url(url or "", direct_media_hosts=[], allow_feed=(kind == "rss"))
    except UrlRejected as e:
        raise AppError(ErrorCode.UNSUPPORTED_URL, str(e)) from e
    if kind == "youtube_channel" and c.kind != UrlKind.YOUTUBE_CHANNEL:
        raise AppError(
            ErrorCode.UNSUPPORTED_URL, "Ange en YouTube-kanaladress (youtube.com/@namn eller /channel/UC…)."
        )
    if kind == "rss" and c.kind not in (UrlKind.FEED, UrlKind.YOUTUBE_CHANNEL):
        raise AppError(ErrorCode.UNSUPPORTED_URL, "Ange en RSS/Atom-adress.")
    if kind == "rss" and c.kind == UrlKind.YOUTUBE_CHANNEL:
        # A YouTube channel's public Atom feed; only channel IDs map to feeds directly.
        if not (c.external_id or "").startswith("UC"):
            raise AppError(
                ErrorCode.UNSUPPORTED_URL, "RSS för YouTube kräver kanal-ID (youtube.com/channel/UC…)."
            )
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={c.external_id}"
    return c.canonical_url


def _keywords(kws: list[str]) -> list[str]:
    return [k for k in (clean_text(x, 60) for x in kws) if k][:30]


@router.get("")
def list_sources(_: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(Source).order_by(Source.created_at)).all()
    counts = dict(
        db.execute(select(CandidateVideo.source_id, func.count()).group_by(CandidateVideo.source_id)).all()
    )
    return {"items": [{**ser.source(s), "candidates": counts.get(s.id, 0)} for s in rows]}


@router.post("")
def create(
    body: SourceIn, request: Request, _: Principal = Depends(require_user), db: Session = Depends(get_db)
) -> dict:
    if body.kind == "youtube_channel" and not request.app.state.cfg.youtube_api_key:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            "YouTube-kanaler kräver en API-nyckel (CLIPFORGE_YOUTUBE_API_KEY). "
            "Använd RSS-källan som nyckelfritt alternativ.",
        )
    url = _validate_url(body.kind, body.url)
    if url and db.scalar(select(Source.id).where(Source.url == url)):
        raise AppError(ErrorCode.CONFLICT, "Källan finns redan.", status_code=409)
    s = Source(
        kind=body.kind,
        name=clean_text(body.name, 200),
        url=url,
        enabled=body.enabled,
        polling_interval_minutes=body.polling_interval_minutes,
        category=clean_text(body.category or "", 100) or None,
        keywords_json=_keywords(body.keywords),
        rights_status=body.rights_status,
        rights_note=clean_text(body.rights_note or "", 2000) or None,
    )
    db.add(s)
    db.flush()
    audit.record(
        db,
        "source.created",
        actor="user",
        object_type="source",
        object_id=s.id,
        details={"kind": s.kind, "rights": s.rights_status},
    )
    return ser.source(s)


def _get(db: Session, sid: str) -> Source:
    s = db.get(Source, sid)
    if s is None:
        raise NotFound("Källan")
    return s


@router.patch("/{sid}")
def update(
    sid: str, body: SourcePatch, _: Principal = Depends(require_user), db: Session = Depends(get_db)
) -> dict:
    s = _get(db, sid)
    data = body.model_dump(exclude_unset=True)
    if "keywords" in data:
        s.keywords_json = _keywords(data.pop("keywords") or [])
    for k, v in data.items():
        setattr(s, k, clean_text(v, 2000) if isinstance(v, str) else v)
    audit.record(db, "source.updated", actor="user", object_type="source", object_id=sid, details=data)
    return ser.source(s)


@router.delete("/{sid}")
def delete(sid: str, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    s = _get(db, sid)
    db.delete(s)
    audit.record(db, "source.deleted", actor="user", object_type="source", object_id=sid)
    return {"ok": True}


@router.post("/{sid}/check")
def check_now(sid: str, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    s = _get(db, sid)
    job = q.enqueue(
        db,
        JobType.DISCOVER,
        priority=Priority.USER,
        source_id=s.id,
        idempotency_key=f"discover:{s.id}:manual:{new_id()}",
    )
    db.flush()
    return {"job": ser.job(job)}


@router.post("/{sid}/pause")
def pause(sid: str, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    s = _get(db, sid)
    s.paused = True
    audit.record(db, "source.paused", actor="user", object_type="source", object_id=sid)
    return ser.source(s)


@router.post("/{sid}/resume")
def resume(sid: str, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    s = _get(db, sid)
    s.paused = False
    s.consecutive_failures = 0
    audit.record(db, "source.resumed", actor="user", object_type="source", object_id=sid)
    return ser.source(s)
