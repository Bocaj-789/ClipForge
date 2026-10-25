"""Health, agent control, dashboard summary, dependency status."""

from __future__ import annotations

import platform
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_db, require_user
from app.db.base import utcnow
from app.db.migrate import migration_status
from app.db.models import (
    AuditEvent,
    CandidateVideo,
    Channel,
    Job,
    JobStatus,
    Publication,
    RenderedClip,
    ReviewStatus,
    Source,
)
from app.schemas import serializers as ser
from app.services import agent
from app.services.intelligence.llm_provider import OllamaProvider
from app.services.pipeline import default_engine, default_providers
from app.services.rendering import ffmpeg
from app.services.settings_service import load_settings
from app.services.storage.manager import storage_status

router = APIRouter(tags=["system"])


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/health/dependencies")
def dependencies(
    request: Request, _: Principal = Depends(require_user), db: Session = Depends(get_db)
) -> dict:
    cfg = request.app.state.cfg
    s = load_settings(db)
    ok, reason = ffmpeg.available()
    tr = default_engine(cfg, s).status()
    ollama_ok, ollama_reason = (
        OllamaProvider(cfg.ollama_url, s.ollama_model).health()
        if s.llm_provider == "ollama"
        else (None, "Lokal LLM avstängd; heuristik används.")
    )
    return {
        "platform": {
            "system": platform.system(),
            "release": platform.mac_ver()[0] or platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "ffmpeg": {"available": ok, "reason": reason, "version": ffmpeg.ffmpeg_version() if ok else None},
        "transcription": tr,
        "media_providers": [
            {"name": prov.name, "available": av, "reason": why}
            for prov in default_providers(cfg, s)
            for av, why in [prov.available()]
        ],
        "llm": {"provider": s.llm_provider, "available": ollama_ok, "reason": ollama_reason},
        "youtube_api_key_configured": cfg.youtube_api_key is not None
        and bool(cfg.youtube_api_key.get_secret_value()),
        "oauth_client_configured": request.app.state.publish_deps.oauth_client() is not None,
        "token_store": {
            "backend": request.app.state.publish_deps.store.name,
            "available": request.app.state.publish_deps.store.available()[0],
        },
        "database": migration_status(cfg.database_url),
        "remote_access_enabled": s.remote_access_enabled,
        "paid_ai_apis": "disabled",
        "cloud_compute": "not_required",
    }


@router.get("/agent/status")
def agent_status(_: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    return agent.compute_status(db)


class ReasonIn(BaseModel):
    reason: str | None = Field(default=None, max_length=300)


def _set(db: Session, state: str, body: ReasonIn | None) -> dict:
    agent.set_desired(db, state, reason=body.reason if body else None)
    db.flush()
    return agent.compute_status(db)


@router.post("/agent/pause")
def pause(
    body: ReasonIn | None = None, _: Principal = Depends(require_user), db: Session = Depends(get_db)
) -> dict:
    return _set(db, "paused", body or ReasonIn(reason="Pausad av användaren."))


@router.post("/agent/resume")
def resume(_: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    return _set(db, "running", ReasonIn(reason=None))


@router.post("/agent/stop")
def stop(_: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    return _set(db, "stopped", ReasonIn(reason="Nödstopp aktiverat av användaren."))


@router.post("/agent/drain")
def drain(_: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    return _set(db, "draining", ReasonIn(reason="Slutför pågående jobb och stannar sedan."))


@router.get("/dashboard/summary")
def summary(request: Request, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    cfg = request.app.state.cfg
    s = load_settings(db)
    counts = dict(db.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    awaiting = (
        db.scalar(
            select(func.count())
            .select_from(RenderedClip)
            .where(RenderedClip.review_status == ReviewStatus.NEEDS_REVIEW, RenderedClip.deleted_at.is_(None))
        )
        or 0
    )
    since = utcnow() - timedelta(days=1)
    errors = db.scalars(
        select(Job)
        .where(
            Job.status.in_([JobStatus.FAILED, JobStatus.BLOCKED]),
            Job.updated_at >= utcnow() - timedelta(days=7),
        )
        .order_by(Job.updated_at.desc())
        .limit(5)
    ).all()
    sources = db.scalars(select(Source).order_by(Source.created_at)).all()
    recent_pubs = db.scalars(select(Publication).order_by(Publication.created_at.desc()).limit(5)).all()
    tr = default_engine(cfg, s).status()
    return {
        "agent": agent.compute_status(db),
        "jobs": {k: int(v) for k, v in counts.items()},
        "clips_awaiting_review": awaiting,
        "rendered_last_24h": db.scalar(
            select(func.count()).select_from(RenderedClip).where(RenderedClip.created_at >= since)
        )
        or 0,
        "candidates_total": db.scalar(select(func.count()).select_from(CandidateVideo)) or 0,
        "recent_errors": [ser.job(j) for j in errors],
        "storage": storage_status(cfg.data_dir, s).as_dict(),
        "transcription": tr,
        "llm_provider": s.llm_provider,
        "sources": [
            {
                "id": x.id,
                "name": x.name,
                "kind": x.kind,
                "enabled": x.enabled,
                "paused": x.paused,
                "last_success_at": x.last_success_at,
                "last_error": x.last_error,
            }
            for x in sources
        ],
        "channels": db.scalar(select(func.count()).select_from(Channel)) or 0,
        "recent_publications": [
            {
                "id": p.id,
                "status": p.status,
                "availability": p.availability,
                "channel_name": p.channel.display_name if p.channel is not None else None,
                "external_url": p.external_url,
                "created_at": p.created_at,
            }
            for p in recent_pubs
        ],
        "auto_publish_global_enabled": s.auto_publish_global_enabled,
        "youtube_api_key_configured": cfg.youtube_api_key is not None,
        "last_activity": db.scalar(select(func.max(AuditEvent.timestamp))),
        "costs": {"paid_ai_apis": "disabled", "cloud_compute": "not_required"},
    }
