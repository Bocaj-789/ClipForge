"""Priority inbox and candidate videos."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_db, require_user
from app.core.errors import AppError, ErrorCode, NotFound
from app.core.security import contained_path
from app.db.models import CandidateStatus, CandidateVideo, ClipCandidate, Job, RenderedClip, Transcript
from app.db.session import new_session
from app.schemas import serializers as ser
from app.services import inbox
from app.services.acquisition.local_file import store_async
from app.services.audit import service as audit
from app.services.queue import service as q
from app.services.queue.states import Priority
from app.services.settings_service import load_settings
from app.services.storage.manager import invalidate_cache

router = APIRouter(tags=["inbox"])
Rights = Literal[
    "unknown", "user_owned", "licensed", "public_domain_verified", "permission_confirmed", "not_permitted"
]


class SubmitIn(BaseModel):
    url: str = Field(min_length=4, max_length=2048)
    urgent: bool = True
    note: str | None = Field(default=None, max_length=1000)
    rights_status: Rights = "user_owned"
    rights_note: str | None = Field(default=None, max_length=2000)


class BulkIn(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=50)
    urgent: bool = False
    rights_status: Rights = "user_owned"
    rights_note: str | None = Field(default=None, max_length=2000)


class RightsIn(BaseModel):
    rights_status: Rights
    rights_note: str | None = Field(default=None, max_length=2000)


class PrioritizeIn(BaseModel):
    urgent: bool = True


def _latest_job(db: Session, cand_id: str) -> Job | None:
    return db.scalar(
        select(Job).where(Job.candidate_video_id == cand_id).order_by(Job.created_at.desc()).limit(1)
    )


def _out(db: Session, c: CandidateVideo) -> dict:
    return ser.candidate(c, latest_job=_latest_job(db, c.id))


@router.post("/inbox")
def submit(body: SubmitIn, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    r = inbox.submit_url(
        db,
        body.url,
        urgent=body.urgent,
        note=body.note,
        rights_status=body.rights_status,
        rights_note=body.rights_note,
        settings=load_settings(db),
    )
    db.flush()
    return {
        "candidate": _out(db, r.candidate),
        "duplicate": r.duplicate,
        "priority_raised": r.priority_raised,
    }


@router.post("/inbox/bulk")
def submit_bulk(body: BulkIn, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    s = load_settings(db)
    results = []
    for url in body.urls:
        try:
            with db.begin_nested():
                r = inbox.submit_url(
                    db,
                    url,
                    urgent=body.urgent,
                    note=None,
                    rights_status=body.rights_status,
                    rights_note=body.rights_note,
                    settings=s,
                )
            results.append(
                {"url": url[:300], "ok": True, "candidate_id": r.candidate.id, "duplicate": r.duplicate}
            )
        except AppError as e:
            results.append({"url": url[:300], "ok": False, "error": e.safe_message})
    return {"results": results}


@router.post("/inbox/upload")
async def upload(
    request: Request,
    filename: str = Query(min_length=1, max_length=255),
    title: str | None = Query(default=None, max_length=300),
    rights_status: Rights = Query(default="user_owned"),
    rights_note: str | None = Query(default=None, max_length=2000),
    urgent: bool = Query(default=True),
    candidate_id: str | None = Query(default=None, max_length=32),
    _: Principal = Depends(require_user),
) -> dict:
    """Raw-body upload (Content-Type: application/octet-stream), streamed to disk."""
    cfg = request.app.state.cfg
    db = new_session()
    try:
        settings = load_settings(db)
        target = None
        if candidate_id:
            target = db.get(CandidateVideo, candidate_id)
            if target is None:
                raise NotFound("Videon")
        db.commit()
        stored = await store_async(request.stream(), filename, cfg, settings)
        if target is not None:
            target = db.get(CandidateVideo, candidate_id)
            assert target is not None
            inbox.attach_media(db, target, stored)
            db.commit()
            return {"candidate": _out(db, target), "duplicate": False}
        r = inbox.import_file(
            db,
            stored,
            title=title or filename.rsplit(".", 1)[0],
            urgent=urgent,
            note=None,
            rights_status=rights_status,
            rights_note=rights_note,
        )
        if r.duplicate:
            # Same content already imported: discard the new copy.
            contained_path(cfg.data_dir, stored.rel_path).unlink(missing_ok=True)
            invalidate_cache()
        db.commit()
        return {"candidate": _out(db, r.candidate), "duplicate": r.duplicate}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.get("/candidates")
def list_candidates(
    status: str | None = Query(default=None, max_length=32),
    origin: Literal["manual", "discovery", "all"] = "all",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: Principal = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict:
    stmt = select(CandidateVideo)
    if status:
        stmt = stmt.where(CandidateVideo.status == status)
    if origin == "manual":
        stmt = stmt.where(CandidateVideo.origin.in_(["manual_url", "local_upload"]))
    elif origin == "discovery":
        stmt = stmt.where(CandidateVideo.origin == "discovery")
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(CandidateVideo.priority.asc(), CandidateVideo.discovered_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return {"items": [_out(db, c) for c in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/candidates/{cid}")
def get_candidate(cid: str, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    c = db.get(CandidateVideo, cid)
    if c is None:
        raise NotFound("Videon")
    tr = db.scalar(
        select(Transcript).where(Transcript.candidate_video_id == cid).order_by(Transcript.created_at.desc())
    )
    jobs = db.scalars(
        select(Job).where(Job.candidate_video_id == cid).order_by(Job.created_at.desc()).limit(50)
    ).all()
    props = db.scalars(
        select(ClipCandidate)
        .where(ClipCandidate.candidate_video_id == cid)
        .order_by(ClipCandidate.final_score.desc())
    ).all()
    return {
        "candidate": _out(db, c),
        "description": c.description,
        "transcript": None
        if tr is None
        else {
            "language": tr.language,
            "engine": tr.engine_name,
            "engine_version": tr.engine_version,
            "model": tr.model_name,
            "processing_seconds": tr.processing_seconds,
            "chars": len(tr.text),
            "preview": tr.text[:1500],
        },
        "proposals": [ser.proposal(p) for p in props],
        "jobs": [ser.job(j) for j in jobs],
    }


@router.post("/candidates/{cid}/rights")
def set_rights(
    cid: str, body: RightsIn, _: Principal = Depends(require_user), db: Session = Depends(get_db)
) -> dict:
    c = db.get(CandidateVideo, cid)
    if c is None:
        raise NotFound("Videon")
    inbox.set_rights(db, c, body.rights_status, body.rights_note)
    db.flush()
    return {"candidate": _out(db, c)}


@router.post("/candidates/{cid}/prioritize")
def prioritize(
    cid: str, body: PrioritizeIn, _: Principal = Depends(require_user), db: Session = Depends(get_db)
) -> dict:
    c = db.get(CandidateVideo, cid)
    if c is None:
        raise NotFound("Videon")
    pri = Priority.URGENT if body.urgent else Priority.USER
    c.priority = min(c.priority, pri)
    n = q.raise_priority_for_candidate(db, cid, c.priority)
    audit.record(
        db,
        "candidate.prioritized",
        actor="user",
        object_type="candidate",
        object_id=cid,
        details={"priority": c.priority, "jobs": n},
    )
    db.flush()
    return {"candidate": _out(db, c), "jobs_updated": n}


@router.post("/candidates/{cid}/retry")
def retry(cid: str, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    c = db.get(CandidateVideo, cid)
    if c is None:
        raise NotFound("Videon")
    # Handlers re-check rights and block again if they are still unconfirmed.
    inbox.resume_pipeline(db, c)
    db.flush()
    return {"candidate": _out(db, c)}


@router.post("/candidates/{cid}/reprocess")
def reprocess(cid: str, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    c = db.get(CandidateVideo, cid)
    if c is None:
        raise NotFound("Videon")
    if q.active_job_for(db, cid):
        raise AppError(ErrorCode.CONFLICT, "Videon bearbetas redan.", status_code=409)
    inbox.resume_pipeline(db, c)
    db.flush()
    return {"candidate": _out(db, c)}


@router.post("/candidates/{cid}/skip")
def skip(cid: str, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    c = db.get(CandidateVideo, cid)
    if c is None:
        raise NotFound("Videon")
    for j in db.scalars(
        select(Job).where(
            Job.candidate_video_id == cid, Job.status.in_(["pending", "retry_wait", "blocked", "running"])
        )
    ):
        q.request_cancel(db, j.id)
    c.status = CandidateStatus.SKIPPED
    audit.record(db, "candidate.skipped", actor="user", object_type="candidate", object_id=cid)
    db.flush()
    return {"candidate": _out(db, c)}


@router.delete("/candidates/{cid}")
def delete_candidate(
    cid: str, request: Request, _: Principal = Depends(require_user), db: Session = Depends(get_db)
) -> dict:
    """Delete a video record and ClipForge-generated files. User originals in the import folder are kept."""
    cfg = request.app.state.cfg
    c = db.get(CandidateVideo, cid)
    if c is None:
        raise NotFound("Videon")
    if db.scalar(select(Job.id).where(Job.candidate_video_id == cid, Job.status == "running")):
        raise AppError(ErrorCode.CONFLICT, "Videon bearbetas just nu. Avbryt jobbet först.", status_code=409)
    files = []
    for p in c.clips:
        for r in p.renders:
            files += [r.file_path, r.thumbnail_path, r.caption_file_path]
    if c.media_path and not c.media_is_user_original:
        files.append(c.media_path)
    removed = 0
    for rel in files:
        if rel:
            path = contained_path(cfg.data_dir, rel)
            if path.is_file():
                path.unlink()
                removed += 1
    db.query(RenderedClip).filter(RenderedClip.clip_candidate_id.in_([p.id for p in c.clips])).delete(
        synchronize_session=False
    )
    db.delete(c)
    invalidate_cache()
    audit.record(
        db,
        "candidate.deleted",
        actor="user",
        object_type="candidate",
        object_id=cid,
        details={"files_removed": removed},
    )
    return {"ok": True, "files_removed": removed}
