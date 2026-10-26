from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_db, require_user
from app.core.errors import AppError, ErrorCode, NotFound
from app.core.security import contained_path
from app.db.base import new_id, utcnow
from app.db.models import ClipCandidate, ClipStatus, JobType, RenderedClip, ReviewStatus
from app.schemas import serializers as ser
from app.services.acquisition.base import rights_ok
from app.services.audit import service as audit
from app.services.intelligence.metadata import sanitize_description, sanitize_title
from app.services.queue import service as q
from app.services.queue.states import Priority
from app.services.storage.manager import invalidate_cache

router = APIRouter(tags=["clips"])


class RenderIn(BaseModel):
    profile: Literal["fast_draft", "shorts_standard", "high_quality"] | None = None
    caption_style: Literal["clean", "bold", "karaoke", "none"] | None = None
    crop_mode: Literal["center_crop", "fit_blur"] | None = None
    crop_offset: float | None = Field(default=None, ge=0, le=1)


class EditIn(BaseModel):
    title_suggestion: str | None = Field(default=None, max_length=300)
    description_suggestion: str | None = Field(default=None, max_length=5000)


@router.get("/clips")
def list_clips(
    review_status: Literal["needs_review", "approved", "rejected", "all"] = "all",
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    _: Principal = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict:
    stmt = select(RenderedClip).where(RenderedClip.deleted_at.is_(None))
    if review_status != "all":
        stmt = stmt.where(RenderedClip.review_status == review_status)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(RenderedClip.created_at.desc()).limit(limit).offset(offset)).all()
    return {"items": [ser.rendered(r) for r in rows], "total": total, "limit": limit, "offset": offset}


def _clip(db: Session, rid: str) -> RenderedClip:
    r = db.get(RenderedClip, rid)
    if r is None or r.deleted_at is not None:
        raise NotFound("Klippet")
    return r


@router.get("/clips/{rid}")
def get_clip(rid: str, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    return ser.rendered(_clip(db, rid))


def _file(request: Request, rel: str | None, media_type: str) -> FileResponse:
    if not rel:
        raise NotFound("Filen")
    path = contained_path(request.app.state.cfg.data_dir, rel)
    if not path.is_file():
        raise NotFound("Filen")
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, max-age=300"})


@router.get("/clips/{rid}/video")
def video(
    rid: str,
    request: Request,
    download: bool = False,
    _: Principal = Depends(require_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    r = _clip(db, rid)
    resp = _file(request, r.file_path, "video/mp4")
    if download:
        resp.headers["Content-Disposition"] = f'attachment; filename="clipforge-{r.id[:8]}.mp4"'
    return resp


@router.get("/clips/{rid}/thumbnail")
def thumbnail(
    rid: str, request: Request, _: Principal = Depends(require_user), db: Session = Depends(get_db)
) -> FileResponse:
    return _file(request, _clip(db, rid).thumbnail_path, "image/jpeg")


@router.get("/clips/{rid}/captions")
def captions(
    rid: str, request: Request, _: Principal = Depends(require_user), db: Session = Depends(get_db)
) -> FileResponse:
    resp = _file(request, _clip(db, rid).caption_file_path, "application/x-subrip")
    resp.headers["Content-Disposition"] = f'attachment; filename="clipforge-{rid[:8]}.srt"'
    return resp


@router.post("/clips/{rid}/approve")
def approve(rid: str, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    r = _clip(db, rid)
    if not rights_ok(r.clip.video.rights_status):
        raise AppError(ErrorCode.RIGHTS_NOT_CONFIRMED, "Rättigheterna för källan är inte bekräftade.")
    r.review_status = ReviewStatus.APPROVED
    audit.record(db, "clip.approved", actor="user", object_type="rendered_clip", object_id=rid)
    return ser.rendered(r)


@router.post("/clips/{rid}/reject")
def reject(rid: str, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    r = _clip(db, rid)
    r.review_status = ReviewStatus.REJECTED
    r.review_decided_at = utcnow()
    audit.record(db, "clip.rejected", actor="user", object_type="rendered_clip", object_id=rid)
    return ser.rendered(r)


@router.patch("/clips/{rid}")
def edit(rid: str, body: EditIn, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    r = _clip(db, rid)
    if body.title_suggestion is not None:
        t = sanitize_title(body.title_suggestion)
        if not t:
            raise AppError(ErrorCode.INVALID_INPUT, "Titeln får inte vara tom.")
        r.title_suggestion = t
    if body.description_suggestion is not None:
        r.description_suggestion = sanitize_description(body.description_suggestion)
    return ser.rendered(r)


@router.delete("/clips/{rid}")
def delete_clip(
    rid: str, request: Request, _: Principal = Depends(require_user), db: Session = Depends(get_db)
) -> dict:
    r = _clip(db, rid)
    removed = 0
    for rel in (r.file_path, r.thumbnail_path, r.caption_file_path):
        if rel:
            p = contained_path(request.app.state.cfg.data_dir, rel)
            if p.is_file():
                p.unlink()
                removed += 1
    r.deleted_at = utcnow()
    if r.clip.status == ClipStatus.RENDERED and all(x.deleted_at for x in r.clip.renders):
        r.clip.status = ClipStatus.PROPOSED
    invalidate_cache()
    audit.record(
        db,
        "clip.deleted",
        actor="user",
        object_type="rendered_clip",
        object_id=rid,
        details={"files_removed": removed},
    )
    return {"ok": True}


@router.post("/proposals/{pid}/render")
def render_proposal(
    pid: str, body: RenderIn, _: Principal = Depends(require_user), db: Session = Depends(get_db)
) -> dict:
    p = db.get(ClipCandidate, pid)
    if p is None:
        raise NotFound("Klippförslaget")
    if not rights_ok(p.video.rights_status):
        raise AppError(ErrorCode.RIGHTS_NOT_CONFIRMED, "Rättigheterna för källan är inte bekräftade.")
    if body.crop_offset is not None:
        p.crop_offset = body.crop_offset
    p.status = ClipStatus.SELECTED if p.status == ClipStatus.PROPOSED else p.status
    opts = {k: v for k, v in body.model_dump().items() if v is not None and k != "crop_offset"}
    job = q.enqueue(
        db,
        JobType.RENDER,
        priority=Priority.USER,
        candidate_video_id=p.candidate_video_id,
        input_json={"clip_id": p.id, **opts},
        idempotency_key=f"render:{p.id}:{new_id()}",
    )
    audit.record(
        db,
        "proposal.render_requested",
        actor="user",
        object_type="clip_candidate",
        object_id=pid,
        details=opts,
    )
    db.flush()
    return {"job": ser.job(job), "proposal": ser.proposal(p)}


@router.post("/proposals/{pid}/reject")
def reject_proposal(pid: str, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    p = db.get(ClipCandidate, pid)
    if p is None:
        raise NotFound("Klippförslaget")
    p.status = ClipStatus.REJECTED
    return ser.proposal(p)
