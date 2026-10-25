from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_db, require_user
from app.core.errors import AppError, ErrorCode, NotFound
from app.db.models import Job, JobStatus
from app.schemas import serializers as ser
from app.services.audit import service as audit
from app.services.queue import service as q
from app.services.queue.states import assert_transition

router = APIRouter(prefix="/jobs", tags=["jobs"])
SORTS = {
    "priority": (Job.priority.asc(), Job.created_at.asc()),
    "created": (Job.created_at.desc(),),
    "updated": (Job.updated_at.desc(),),
}


@router.get("")
def list_jobs(
    status: str | None = Query(default=None, max_length=64),
    job_type: str | None = Query(default=None, max_length=32),
    candidate_id: str | None = Query(default=None, max_length=32),
    sort: Literal["priority", "created", "updated"] = "priority",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: Principal = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict:
    stmt = select(Job)
    if status:
        wanted = [s for s in status.split(",") if s in {x.value for x in JobStatus}]
        stmt = stmt.where(Job.status.in_(wanted))
    if job_type:
        stmt = stmt.where(Job.job_type == job_type)
    if candidate_id:
        stmt = stmt.where(Job.candidate_video_id == candidate_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(*SORTS[sort]).limit(limit).offset(offset)).all()
    counts = dict(db.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    return {
        "items": [ser.job(j) for j in rows],
        "total": total,
        "counts": counts,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{job_id}")
def get_job(job_id: str, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    j = db.get(Job, job_id)
    if j is None:
        raise NotFound("Jobbet")
    return ser.job(j)


@router.post("/{job_id}/retry")
def retry(job_id: str, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    j = q.manual_retry(db, job_id)
    audit.record(db, "job.retry", actor="user", object_type="job", object_id=job_id)
    return ser.job(j)


@router.post("/{job_id}/cancel")
def cancel(job_id: str, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    j = q.request_cancel(db, job_id)
    audit.record(db, "job.cancel", actor="user", object_type="job", object_id=job_id)
    return ser.job(j)


@router.post("/{job_id}/requeue")
def requeue(job_id: str, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    """Run a finished job again (explicit reprocess)."""
    j = db.get(Job, job_id)
    if j is None:
        raise NotFound("Jobbet")
    try:
        assert_transition(j.status, JobStatus.PENDING)
    except ValueError as e:
        raise AppError(
            ErrorCode.CONFLICT, "Jobbet kan inte köas om i sitt nuvarande läge.", status_code=409
        ) from e
    new = q.enqueue(
        db,
        j.job_type,
        priority=j.priority,
        candidate_video_id=j.candidate_video_id,
        source_id=j.source_id,
        channel_id=j.channel_id,
        input_json=dict(j.input_json or {}),
    )
    audit.record(
        db, "job.requeue", actor="user", object_type="job", object_id=job_id, details={"new": new.id}
    )
    db.flush()
    return ser.job(new)
