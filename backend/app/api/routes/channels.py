"""Channel configuration, connection state and per-channel upload history.

Connecting a channel (Google OAuth) lives in ``routes/oauth.py``; this module never touches tokens.
"""

from __future__ import annotations

import re
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_db, require_user
from app.core.errors import AppError, ErrorCode, NotFound
from app.core.security import clean_text
from app.db.base import utcnow
from app.db.models import Channel, ConnectionState, Job, Publication
from app.schemas import serializers as ser
from app.services.audit import service as audit
from app.services.publishing import oauth
from app.services.publishing.policy import ALLOW_PUBLIC_CONFIRM_PHRASE, next_slot
from app.services.settings_service import validate_timezone

router = APIRouter(prefix="/channels", tags=["channels"])
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class Schedule(BaseModel):
    days: list[int] = Field(default_factory=lambda: list(range(7)), max_length=7)
    windows: list[tuple[str, str]] = Field(default_factory=lambda: [("09:00", "21:00")], max_length=6)

    @field_validator("days")
    @classmethod
    def _days(cls, v: list[int]) -> list[int]:
        if any(d < 0 or d > 6 for d in v):
            raise ValueError("days must be 0..6")
        return sorted(set(v))

    @field_validator("windows")
    @classmethod
    def _windows(cls, v: list[tuple[str, str]]) -> list[tuple[str, str]]:
        for a, b in v:
            if not (_TIME.match(a) and _TIME.match(b)) or a >= b:
                raise ValueError("windows must be HH:MM-HH:MM with start < end")
        return v


class ChannelIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    max_uploads_per_day: int = Field(default=1, ge=0, le=20)
    min_spacing_minutes: int = Field(default=240, ge=10, le=24 * 60)
    timezone: str = "Europe/Stockholm"
    schedule: Schedule = Field(default_factory=Schedule)
    keywords: list[str] = Field(default_factory=list, max_length=30)
    title_template: str = Field(default="{title}", max_length=300)
    description_template: str = Field(default="{description}\n\n{hashtags}", max_length=3000)
    default_language: Literal["sv", "en"] = "sv"
    default_visibility: Literal["private", "unlisted", "public"] = "private"
    render_profile: Literal["fast_draft", "shorts_standard", "high_quality"] = "shorts_standard"
    caption_style: Literal["clean", "bold", "karaoke", "none"] = "bold"
    min_clip_seconds: int = Field(default=20, ge=5, le=600)
    max_clip_seconds: int = Field(default=60, ge=5, le=600)
    late_policy: Literal["next_slot", "skip"] = "next_slot"
    enabled: bool = True
    category_id: str = Field(default="22", pattern=r"^\d{1,3}$")
    made_for_kids: bool = False
    notify_subscribers: bool = False
    auto_publish_min_score: float = Field(default=60.0, ge=0, le=100)
    release_spacing_minutes: int | None = Field(default=None, ge=30, le=7 * 24 * 60)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        return validate_timezone(v)


class ChannelPatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    max_uploads_per_day: int | None = Field(default=None, ge=0, le=20)
    min_spacing_minutes: int | None = Field(default=None, ge=10, le=24 * 60)
    timezone: str | None = None
    schedule: Schedule | None = None
    keywords: list[str] | None = Field(default=None, max_length=30)
    title_template: str | None = Field(default=None, max_length=300)
    description_template: str | None = Field(default=None, max_length=3000)
    default_language: Literal["sv", "en"] | None = None
    default_visibility: Literal["private", "unlisted", "public"] | None = None
    render_profile: Literal["fast_draft", "shorts_standard", "high_quality"] | None = None
    caption_style: Literal["clean", "bold", "karaoke", "none"] | None = None
    min_clip_seconds: int | None = Field(default=None, ge=5, le=600)
    max_clip_seconds: int | None = Field(default=None, ge=5, le=600)
    late_policy: Literal["next_slot", "skip"] | None = None
    enabled: bool | None = None
    paused: bool | None = None
    category_id: str | None = Field(default=None, pattern=r"^\d{1,3}$")
    made_for_kids: bool | None = None
    notify_subscribers: bool | None = None
    auto_publish_min_score: float | None = Field(default=None, ge=0, le=100)
    release_spacing_minutes: int | None = Field(default=None, ge=30, le=7 * 24 * 60)


class AutoPublishIn(BaseModel):
    enabled: bool
    confirm_phrase: str | None = Field(default=None, max_length=100)


class AllowPublicIn(AutoPublishIn):
    pass


class BulkChannelsIn(BaseModel):
    """Settings applied to every channel at once. Only the fields that are sent are changed."""

    changes: ChannelPatch = Field(default_factory=ChannelPatch)
    auto_publish_enabled: bool | None = None
    allow_public: bool | None = None
    confirm_phrase: str | None = Field(default=None, max_length=100)


def _apply(c: Channel, data: dict) -> None:
    if "schedule" in data and data["schedule"] is not None:
        c.schedule_json = {
            "days": data["schedule"]["days"],
            "windows": [list(w) for w in data["schedule"]["windows"]],
        }
    if "keywords" in data and data["keywords"] is not None:
        c.category_rules_json = {
            **(c.category_rules_json or {}),
            "keywords": [k for k in (clean_text(x, 60) for x in data["keywords"]) if k],
        }
    if data.get("timezone"):
        try:
            validate_timezone(data["timezone"])
        except ValueError as e:
            raise AppError(ErrorCode.INVALID_INPUT, "Okänd tidszon.") from e
    for k, v in data.items():
        if k in ("schedule", "keywords") or (v is None and k != "release_spacing_minutes"):
            continue
        setattr(c, k, clean_text(v, 3000) if isinstance(v, str) and k != "description_template" else v)
    if c.min_clip_seconds >= c.max_clip_seconds:
        raise AppError(ErrorCode.INVALID_INPUT, "Minsta klipplängd måste vara kortare än längsta.")
    if c.default_visibility == "public" and not c.allow_public:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            "Offentlig standardsynlighet kräver att offentlig publicering först tillåts för kanalen.",
        )


def _out(c: Channel) -> dict:
    nxt = next_slot(c, utcnow()) if c.enabled else None
    return {**ser.channel(c), "next_slot": nxt}


@router.get("")
def list_channels(
    request: Request, _: Principal = Depends(require_user), db: Session = Depends(get_db)
) -> dict:
    deps = request.app.state.publish_deps
    ok, why = deps.store.available()
    configured = deps.oauth_client() is not None
    return {
        "items": [_out(c) for c in db.scalars(select(Channel).order_by(Channel.created_at))],
        "oauth": {
            "client_configured": configured,
            "store": deps.store.name,
            "store_available": ok,
            "store_reason": why,
            "redirect_uri": request.app.state.cfg.oauth_redirect_uri,
            "ready": configured and ok,
        },
    }


@router.post("")
def create(body: ChannelIn, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    c = Channel(display_name=body.display_name, allow_public=False)
    _apply(c, body.model_dump())
    c.auto_publish_enabled = False  # always off on creation
    db.add(c)
    db.flush()
    audit.record(db, "channel.created", actor="user", object_type="channel", object_id=c.id)
    return _out(c)


def _get(db: Session, cid: str) -> Channel:
    c = db.get(Channel, cid)
    if c is None:
        raise NotFound("Kanalen")
    return c


@router.patch("/{cid}/settings")
def update(
    cid: str, body: ChannelPatch, _: Principal = Depends(require_user), db: Session = Depends(get_db)
) -> dict:
    c = _get(db, cid)
    data = body.model_dump(exclude_unset=True)
    _apply(c, data)
    audit.record(
        db,
        "channel.updated",
        actor="user",
        object_type="channel",
        object_id=cid,
        details={"fields": sorted(data)},
    )
    return _out(c)


@router.post("/{cid}/auto-publish")
def auto_publish(
    cid: str, body: AutoPublishIn, _: Principal = Depends(require_user), db: Session = Depends(get_db)
) -> dict:
    c = _get(db, cid)
    if body.enabled and (c.token_reference is None or c.connection_state != ConnectionState.CONNECTED):
        raise AppError(
            ErrorCode.INVALID_INPUT, "Kanalen måste anslutas via OAuth innan automatisk publicering."
        )
    c.auto_publish_enabled = body.enabled
    audit.record(
        db,
        "channel.auto_publish",
        actor="user",
        object_type="channel",
        object_id=cid,
        details={"enabled": body.enabled},
        severity="warning" if body.enabled else "info",
    )
    return _out(c)


@router.post("/{cid}/allow-public")
def allow_public(
    cid: str, body: AllowPublicIn, _: Principal = Depends(require_user), db: Session = Depends(get_db)
) -> dict:
    """Explicit per-channel opt-in for anything that becomes publicly visible (now or scheduled)."""
    c = _get(db, cid)
    if body.enabled and (body.confirm_phrase or "").strip().upper() != ALLOW_PUBLIC_CONFIRM_PHRASE:
        raise AppError(ErrorCode.INVALID_INPUT, f'Skriv "{ALLOW_PUBLIC_CONFIRM_PHRASE}" för att bekräfta.')
    c.allow_public = body.enabled
    if not body.enabled and c.default_visibility == "public":
        c.default_visibility = "private"
    audit.record(
        db,
        "channel.allow_public",
        actor="user",
        object_type="channel",
        object_id=cid,
        details={"enabled": body.enabled},
        severity="warning" if body.enabled else "info",
    )
    return _out(c)


@router.post("/bulk-settings")
def bulk_settings(
    body: BulkChannelsIn, _: Principal = Depends(require_user), db: Session = Depends(get_db)
) -> dict:
    """Apply the same settings to every channel. All or nothing: a rejected value rolls back everything."""
    typed = (body.confirm_phrase or "").strip().upper()
    if body.allow_public is True and typed != ALLOW_PUBLIC_CONFIRM_PHRASE:
        raise AppError(ErrorCode.INVALID_INPUT, f'Skriv "{ALLOW_PUBLIC_CONFIRM_PHRASE}" för att bekräfta.')
    data = body.changes.model_dump(exclude_unset=True)
    channels = list(db.scalars(select(Channel).order_by(Channel.created_at)))
    auto_not_applied: list[dict] = []
    for c in channels:
        if body.allow_public is not None:
            c.allow_public = body.allow_public
            if not body.allow_public and c.default_visibility == "public":
                c.default_visibility = "private"
        if body.auto_publish_enabled is not None:
            connected = c.token_reference is not None and c.connection_state == ConnectionState.CONNECTED
            if body.auto_publish_enabled and not connected:
                auto_not_applied.append(
                    {
                        "channel_id": c.id,
                        "channel_name": c.display_name,
                        "reason": "Kanalen är inte ansluten.",
                    }
                )
            else:
                c.auto_publish_enabled = body.auto_publish_enabled
        _apply(c, data)
        audit.record(
            db,
            "channel.bulk_updated",
            actor="user",
            object_type="channel",
            object_id=c.id,
            details={
                "fields": sorted(data),
                "allow_public": body.allow_public,
                "auto_publish_enabled": body.auto_publish_enabled,
            },
        )
    return {
        "changed": [c.display_name for c in channels],
        "auto_publish_not_applied": auto_not_applied,
        "channels": [_out(c) for c in channels],
    }


@router.post("/{cid}/disconnect")
def disconnect(
    cid: str, request: Request, _: Principal = Depends(require_user), db: Session = Depends(get_db)
) -> dict:
    c = _get(db, cid)
    revoked = oauth.disconnect(db, request.app.state.publish_deps, c)
    return {**_out(c), "revoked_at_google": revoked}


@router.delete("/{cid}")
def delete(cid: str, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    c = _get(db, cid)
    if c.token_reference:
        raise AppError(ErrorCode.CONFLICT, "Koppla från kanalen innan den tas bort.", status_code=409)
    uploaded = db.scalar(
        select(func.count())
        .select_from(Publication)
        .where(Publication.channel_id == cid, Publication.external_video_id.is_not(None))
    )
    if uploaded:
        raise AppError(
            ErrorCode.CONFLICT,
            "Kanalen har uppladdningshistorik som skulle försvinna. Inaktivera kanalen i stället.",
            status_code=409,
        )
    db.delete(c)
    audit.record(db, "channel.deleted", actor="user", object_type="channel", object_id=cid)
    return {"ok": True}


@router.get("/{cid}/publications")
def publications(cid: str, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    _get(db, cid)
    rows = db.scalars(
        select(Publication)
        .where(Publication.channel_id == cid)
        .order_by(Publication.created_at.desc())
        .limit(100)
    ).all()
    jobs = {j.id: j for j in db.scalars(select(Job).where(Job.id.in_([p.job_id for p in rows if p.job_id])))}
    return {
        "items": [
            ser.publication(p, job=jobs.get(p.job_id or ""), clip_title=p.rendered_clip.title_suggestion)
            for p in rows
        ]
    }
