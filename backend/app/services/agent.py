"""Global agent control (desired state) and effective status computation."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import AgentControl, Job, JobStatus, WorkerHeartbeat
from app.services.audit import service as audit

HEARTBEAT_STALE_SECONDS = 45


class AgentState(StrEnum):
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"
    DRAINING = "draining"
    DEGRADED = "degraded"
    ERROR = "error"


DESIRED = {"running", "paused", "stopped", "draining"}


def get_control(db: Session) -> AgentControl:
    ctl = db.get(AgentControl, 1)
    if ctl is None:
        ctl = AgentControl(id=1, desired_state="running")
        db.add(ctl)
        db.flush()
    return ctl


def set_desired(db: Session, state: str, *, reason: str | None, actor: str = "user") -> AgentControl:
    if state not in DESIRED:
        raise ValueError(state)
    ctl = get_control(db)
    prev = ctl.desired_state
    ctl.desired_state = state
    ctl.reason = reason
    if state == "stopped":
        # Emergency stop: ask every running job to abort at its next checkpoint.
        for job in db.scalars(select(Job).where(Job.status == JobStatus.RUNNING)):
            job.cancel_requested = True
    audit.record(
        db, f"agent.{state}", actor=actor, object_type="agent", details={"from": prev, "reason": reason}
    )
    return ctl


def live_workers(db: Session, now: datetime | None = None) -> list[WorkerHeartbeat]:
    now = now or utcnow()
    cutoff = now - timedelta(seconds=HEARTBEAT_STALE_SECONDS)
    return list(
        db.scalars(
            select(WorkerHeartbeat).where(
                WorkerHeartbeat.last_seen_at >= cutoff, WorkerHeartbeat.stopped_at.is_(None)
            )
        )
    )


def compute_status(db: Session, now: datetime | None = None) -> dict[str, Any]:
    ctl = get_control(db)
    workers = live_workers(db, now)
    running = db.scalar(select(func.count()).select_from(Job).where(Job.status == JobStatus.RUNNING)) or 0
    degraded: dict[str, str] = {}
    for w in workers:
        degraded.update(w.degraded_json or {})
    fatal = degraded.pop("fatal", None)

    reason = ctl.reason
    if not workers:
        state = AgentState.STOPPED
        reason = "Arbetarprocessen körs inte. Starta ClipForge med ./scripts/start.sh."
    elif fatal:
        state, reason = AgentState.ERROR, fatal
    elif ctl.desired_state == "stopped":
        state = AgentState.STOPPED
    elif ctl.desired_state == "paused":
        state = AgentState.PAUSED
    elif ctl.desired_state == "draining":
        state = AgentState.DRAINING
    elif degraded:
        state = AgentState.DEGRADED
        reason = "; ".join(degraded.values())
    else:
        state = AgentState.RUNNING
    return {
        "state": state.value,
        "desired_state": ctl.desired_state,
        "reason": reason,
        "maintenance_mode": ctl.maintenance_mode,
        "worker_alive": bool(workers),
        "workers": [
            {"worker_id": w.worker_id, "last_seen_at": w.last_seen_at, "current_job_ids": w.current_job_ids}
            for w in workers
        ],
        "running_jobs": running,
        "degraded": degraded,
    }
