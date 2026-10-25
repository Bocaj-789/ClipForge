"""Channel configuration. OAuth connection and uploads are a later milestone (not implemented)."""

from __future__ import annotations

import re
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_db, require_user
from app.core.errors import AppError, ErrorCode, NotFound
from app.core.security import clean_text
from app.db.base import utcnow
from app.db.models import Channel, Publication
from app.schemas import serializers as ser
from app.services.audit import service as audit
from app.services.publishing.policy import AUTO_PUBLISH_CONFIRM_PHRASE, next_slot
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


class AutoPublishIn(BaseModel):
    enabled: bool
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
        if k in ("schedule", "keywords") or v is None:
            continue
        setattr(c, k, clean_text(v, 3000) if isinstance(v, str) and k != "description_template" else v)
    if c.min_clip_seconds >= c.max_clip_seconds:
        raise AppError(ErrorCode.INVALID_INPUT, "Minsta klipplängd måste vara kortare än längsta.")


def _out(c: Channel) -> dict:
    nxt = next_slot(c, utcnow()) if c.enabled else None
    return {**ser.channel(c), "next_slot": nxt}


@router.get("")
def list_channels(_: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    return {
        "items": [_out(c) for c in db.scalars(select(Channel).order_by(Channel.created_at))],
        "oauth_status": "planned",
        "oauth_note": "YouTube-anslutning via OAuth och uppladdning är ännu inte implementerade (fas 6). "
        "Kanaler kan konfigureras, men ingenting publiceras.",
    }


@router.post("")
def create(body: ChannelIn, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    c = Channel(display_name=body.display_name)
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
    if body.enabled:
        if (body.confirm_phrase or "").strip().upper() != AUTO_PUBLISH_CONFIRM_PHRASE:
            raise AppError(
                ErrorCode.INVALID_INPUT, f'Skriv "{AUTO_PUBLISH_CONFIRM_PHRASE}" för att bekräfta.'
            )
        if c.token_reference is None:
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


@router.delete("/{cid}")
def delete(cid: str, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    c = _get(db, cid)
    if c.token_reference:
        raise AppError(ErrorCode.CONFLICT, "Koppla från kanalen innan den tas bort.", status_code=409)
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
    return {
        "items": [
            {
                "id": p.id,
                "status": p.status,
                "external_url": p.external_url,
                "scheduled_for": p.scheduled_for,
                "uploaded_at": p.uploaded_at,
                "visibility": p.visibility,
                "safe_error_message": p.safe_error_message,
            }
            for p in rows
        ]
    }


@router.post("/oauth/start")
def oauth_start(_: Principal = Depends(require_user)) -> dict:
    raise AppError(
        ErrorCode.INVALID_INPUT,
        "YouTube OAuth är planerat (fas 6) och ännu inte implementerat.",
        status_code=501,
    )
