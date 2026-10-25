from __future__ import annotations

import json
from datetime import timedelta

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_db, require_user
from app.core.security import redact_mapping
from app.db.base import utcnow
from app.db.models import AuditEvent, Job, JobStatus, Publication, RenderedClip, Transcript

router = APIRouter(tags=["logs"])


def _ev(e: AuditEvent) -> dict:
    return {
        "id": e.id,
        "timestamp": e.timestamp,
        "actor": e.actor,
        "severity": e.severity,
        "action": e.action,
        "object_type": e.object_type,
        "object_id": e.object_id,
        "result": e.result,
        "details": e.safe_details_json,
    }


@router.get("/logs")
def list_logs(
    q: str | None = Query(default=None, max_length=100),
    severity: str | None = Query(default=None, max_length=16),
    object_id: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: Principal = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict:
    stmt = select(AuditEvent)
    if q:
        like = f"%{q.replace('%', '').replace('_', '')}%"
        stmt = stmt.where(or_(AuditEvent.action.like(like), AuditEvent.object_type.like(like)))
    if severity:
        stmt = stmt.where(AuditEvent.severity == severity)
    if object_id:
        stmt = stmt.where(AuditEvent.object_id == object_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(AuditEvent.timestamp.desc()).limit(limit).offset(offset)).all()
    return {"items": [_ev(e) for e in rows], "total": total}


@router.get("/logs/export")
def export(_: Principal = Depends(require_user), db: Session = Depends(get_db)) -> Response:
    since = utcnow() - timedelta(days=30)
    rows = db.scalars(
        select(AuditEvent).where(AuditEvent.timestamp >= since).order_by(AuditEvent.timestamp)
    ).all()
    failed = db.scalars(select(Job).where(Job.status.in_([JobStatus.FAILED, JobStatus.BLOCKED]))).all()
    payload = redact_mapping(
        {
            "exported_at": utcnow().isoformat(),
            "events": [_ev(e) for e in rows],
            "problem_jobs": [
                {
                    "id": j.id,
                    "type": j.job_type,
                    "status": j.status,
                    "error_code": j.error_code,
                    "message": j.safe_error_message,
                    "attempts": j.attempt_count,
                }
                for j in failed
            ],
        }
    )
    body = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    return Response(
        body,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="clipforge-diagnostik.json"'},
    )


@router.get("/analytics/summary")
def analytics_summary(_: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    """Only real, locally measured numbers. Platform metrics need OAuth (not yet implemented)."""
    since = utcnow() - timedelta(days=30)
    renders = db.scalars(select(RenderedClip).where(RenderedClip.created_at >= since)).all()
    trs = db.scalars(select(Transcript).where(Transcript.created_at >= since)).all()
    by_day: dict[str, int] = {}
    for r in renders:
        k = r.created_at.date().isoformat()
        by_day[k] = by_day.get(k, 0) + 1
    rtf = [
        t.processing_seconds / t.duration_seconds for t in trs if t.processing_seconds and t.duration_seconds
    ]
    rend_rtf = [
        r.render_seconds / r.duration_seconds for r in renders if r.render_seconds and r.duration_seconds
    ]
    jobs = dict(
        db.execute(select(Job.status, func.count()).where(Job.created_at >= since).group_by(Job.status)).all()
    )

    def median(xs: list[float]) -> float | None:
        if not xs:
            return None
        s = sorted(xs)
        m = len(s) // 2
        return round(s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2, 3)

    return {
        "window_days": 30,
        "renders_per_day": dict(sorted(by_day.items())),
        "renders_total": len(renders),
        "review": {
            s: sum(1 for r in renders if r.review_status == s)
            for s in ("needs_review", "approved", "rejected")
        },
        "transcription_seconds_per_media_second_median": median(rtf),
        "render_seconds_per_clip_second_median": median(rend_rtf),
        "jobs_by_status": {k: int(v) for k, v in jobs.items()},
        "publications_total": db.scalar(select(func.count()).select_from(Publication)) or 0,
        "platform_metrics": {
            "available": False,
            "reason": "Kräver ansluten kanal via OAuth och YouTube Analytics API (planerat).",
        },
    }
