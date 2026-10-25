"""Durable SQLite-backed priority queue with leases, retries and fair scheduling."""

from __future__ import annotations

import random
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError, ErrorCode, JobError, NotFound
from app.core.security import redact
from app.db.base import utcnow
from app.db.models import Channel, Job, JobStatus, Source
from app.services.queue.states import ACTIVE, LANE_FOR, WAITING, assert_transition

BACKOFF_BASE_SECONDS = 30.0
BACKOFF_CAP_SECONDS = 3600.0


def backoff_seconds(attempt: int, *, rng: random.Random | None = None) -> float:
    """Exponential backoff with 'equal jitter': half fixed, half random. attempt starts at 1."""
    r = rng or random.Random()  # noqa: S311 - jitter, not crypto
    exp = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2 ** max(0, attempt - 1)))
    return exp / 2 + r.uniform(0, exp / 2)


def enqueue(
    db: Session,
    job_type: str,
    *,
    priority: int,
    candidate_video_id: str | None = None,
    source_id: str | None = None,
    channel_id: str | None = None,
    input_json: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    max_attempts: int = 3,
    not_before: datetime | None = None,
) -> Job:
    """Create a job. With an idempotency key, an existing job with that key is returned instead."""
    if idempotency_key:
        existing = db.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
        if existing is not None:
            if existing.priority > priority and existing.status in ACTIVE:
                existing.priority = priority
            return existing
    job = Job(
        job_type=job_type,
        lane=LANE_FOR[job_type],
        priority=priority,
        candidate_video_id=candidate_video_id,
        source_id=source_id,
        channel_id=channel_id,
        input_json=input_json or {},
        idempotency_key=idempotency_key,
        max_attempts=max_attempts,
        next_attempt_at=not_before,
        status=JobStatus.PENDING,
    )
    db.add(job)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        # Lost a race on the idempotency key: return the winner.
        found = db.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
        if found is None:
            raise
        return found
    return job


def claim(
    db: Session,
    *,
    lane: str,
    worker_id: str,
    lease_seconds: int,
    fairness: bool = False,
    exclude_types: Iterable[str] = (),
    now: datetime | None = None,
) -> Job | None:
    """Atomically claim the next eligible job in ``lane``.

    Ordering is priority then FIFO. With ``fairness`` the oldest eligible job is picked
    regardless of priority, which the worker does periodically to prevent starvation.
    """
    now = now or utcnow()
    exclude = list(exclude_types)
    cond = [
        Job.lane == lane,
        Job.status.in_([JobStatus.PENDING, JobStatus.RETRY_WAIT]),
        or_(Job.next_attempt_at.is_(None), Job.next_attempt_at <= now),
        Job.cancel_requested.is_(False),
        or_(
            Job.source_id.is_(None),
            Job.source_id.in_(select(Source.id).where(Source.paused.is_(False), Source.enabled.is_(True))),
        ),
        or_(
            Job.channel_id.is_(None),
            Job.channel_id.in_(
                select(Channel.id).where(Channel.paused.is_(False), Channel.enabled.is_(True))
            ),
        ),
    ]
    if exclude:
        cond.append(Job.job_type.not_in(exclude))
    order = [Job.created_at.asc()] if fairness else [Job.priority.asc(), Job.created_at.asc()]
    pick = select(Job.id).where(and_(*cond)).order_by(*order).limit(1).scalar_subquery()
    stmt = (
        update(Job)
        .where(Job.id == pick, Job.status.in_([JobStatus.PENDING, JobStatus.RETRY_WAIT]))
        .values(
            status=JobStatus.RUNNING,
            worker_id=worker_id,
            locked_at=now,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            started_at=now,
            finished_at=None,
            attempt_count=Job.attempt_count + 1,
            updated_at=now,
        )
        .returning(Job.id)
        .execution_options(synchronize_session=False)
    )
    job_id = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if job_id is None:
        return None
    return db.get(Job, job_id, populate_existing=True)


def heartbeat(
    db: Session,
    job_id: str,
    worker_id: str,
    *,
    lease_seconds: int,
    progress: float | None = None,
    stage: str | None = None,
) -> bool:
    """Extend the lease and persist progress. Returns True if cancellation was requested."""
    job = db.get(Job, job_id, populate_existing=True)
    if job is None or job.worker_id != worker_id or job.status != JobStatus.RUNNING:
        return True
    job.lease_expires_at = utcnow() + timedelta(seconds=lease_seconds)
    if progress is not None:
        job.progress_percent = max(0.0, min(100.0, round(progress, 1)))
    if stage is not None:
        job.stage = stage[:100]
    db.commit()
    return bool(job.cancel_requested)


def _transition(job: Job, target: JobStatus) -> None:
    assert_transition(job.status, target)
    job.status = target


def complete(db: Session, job: Job, output: dict[str, Any] | None = None) -> None:
    _transition(job, JobStatus.SUCCEEDED)
    job.progress_percent = 100.0
    job.output_json = output or {}
    job.finished_at = utcnow()
    job.lease_expires_at = None
    job.error_code = None
    job.safe_error_message = None


def mark_cancelled(db: Session, job: Job) -> None:
    _transition(job, JobStatus.CANCELLED)
    job.finished_at = utcnow()
    job.lease_expires_at = None
    job.error_code = ErrorCode.CANCELLED
    job.safe_error_message = "Jobbet avbröts."


def fail(db: Session, job: Job, err: BaseException, *, rng: random.Random | None = None) -> None:
    """Record a failure: block, retry with backoff, or fail permanently (dead-letter)."""
    now = utcnow()
    job.lease_expires_at = None
    if isinstance(err, JobError):
        job.error_code = err.code
        job.safe_error_message = redact(err.safe_message)[:1000]
        if err.awaiting_media:
            _transition(job, JobStatus.AWAITING_MEDIA)
            job.finished_at = now
            return
        if err.block:
            _transition(job, JobStatus.BLOCKED)
            job.finished_at = now
            return
        retryable = err.transient
        retry_after = err.retry_after
    else:
        job.error_code = ErrorCode.INTERNAL
        job.safe_error_message = "Ett oväntat fel inträffade. Se loggen för detaljer."
        retryable = True
        retry_after = None
    if retryable and job.attempt_count < job.max_attempts:
        _transition(job, JobStatus.RETRY_WAIT)
        delay = backoff_seconds(job.attempt_count, rng=rng)
        if retry_after is not None:
            delay = max(delay, min(retry_after, 24 * 3600))
        job.next_attempt_at = now + timedelta(seconds=delay)
        return
    _transition(job, JobStatus.FAILED)
    job.finished_at = now
    if retryable:
        job.error_code = ErrorCode.MAX_ATTEMPTS
        job.safe_error_message = (
            f"Misslyckades efter {job.attempt_count} försök. Senaste fel: {job.safe_error_message}"
        )[:1000]


def defer(db: Session, job: Job, *, until: datetime, reason: str) -> None:
    """Put a running job back without consuming an attempt (caps, schedules, shutdown)."""
    _transition(job, JobStatus.RETRY_WAIT)
    job.attempt_count = max(0, job.attempt_count - 1)
    job.next_attempt_at = until
    job.lease_expires_at = None
    job.stage = reason[:100]


def recover_expired_leases(db: Session, *, now: datetime | None = None) -> int:
    """Return crashed/abandoned running jobs to the queue (or dead-letter them)."""
    now = now or utcnow()
    stale = db.scalars(select(Job).where(Job.status == JobStatus.RUNNING, Job.lease_expires_at < now)).all()
    for job in stale:
        if job.cancel_requested:
            mark_cancelled(db, job)
            continue
        fail(
            db,
            job,
            JobError(
                ErrorCode.LEASE_EXPIRED,
                "Arbetaren slutade svara (krasch eller omstart). Jobbet återupptas.",
                transient=True,
            ),
        )
    db.commit()
    return len(stale)


def request_cancel(db: Session, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise NotFound("Jobbet")
    if job.status == JobStatus.RUNNING:
        job.cancel_requested = True
    elif job.status in (JobStatus.PENDING, JobStatus.RETRY_WAIT, *WAITING):
        mark_cancelled(db, job)
    else:
        raise AppError(ErrorCode.CONFLICT, "Jobbet kan inte avbrytas i sitt nuvarande läge.", status_code=409)
    return job


def manual_retry(db: Session, job_id: str) -> Job:
    """User-initiated retry of a failed/blocked/awaiting-media/cancelled job. Resets the attempt budget."""
    job = db.get(Job, job_id)
    if job is None:
        raise NotFound("Jobbet")
    if job.status not in (JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.RETRY_WAIT, *WAITING):
        raise AppError(
            ErrorCode.CONFLICT,
            "Endast misslyckade, blockerade eller avbrutna jobb kan köras om.",
            status_code=409,
        )
    _transition(job, JobStatus.PENDING)
    job.attempt_count = 0
    job.next_attempt_at = None
    job.cancel_requested = False
    job.error_code = None
    job.safe_error_message = None
    job.finished_at = None
    job.progress_percent = 0.0
    return job


def raise_priority_for_candidate(db: Session, candidate_id: str, priority: int) -> int:
    jobs = db.scalars(
        select(Job).where(
            Job.candidate_video_id == candidate_id,
            Job.status.in_([JobStatus.PENDING, JobStatus.RETRY_WAIT, *WAITING]),
            Job.priority > priority,
        )
    ).all()
    for j in jobs:
        j.priority = priority
    return len(jobs)


def active_job_for(db: Session, candidate_id: str) -> Job | None:
    return db.scalar(
        select(Job)
        .where(Job.candidate_video_id == candidate_id, Job.status.in_(list(ACTIVE)))
        .order_by(Job.created_at.desc())
        .limit(1)
    )
