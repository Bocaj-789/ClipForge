from __future__ import annotations

import random
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app.core.errors import ErrorCode, JobError, blocked, permanent, transient
from app.db.base import utcnow
from app.db.models import Job, JobStatus, JobType, Source
from app.services.queue import service as q
from app.services.queue.states import InvalidTransition, Priority, assert_transition


def _claim(db: Session, lane: str = "heavy", **kw) -> Job | None:
    return q.claim(db, lane=lane, worker_id="w1", lease_seconds=60, **kw)


def test_priority_then_fifo(db: Session) -> None:
    a = q.enqueue(db, JobType.ANALYZE, priority=Priority.DISCOVERY)
    b = q.enqueue(db, JobType.ANALYZE, priority=Priority.USER)
    c = q.enqueue(db, JobType.ANALYZE, priority=Priority.URGENT)
    d = q.enqueue(db, JobType.ANALYZE, priority=Priority.USER)
    db.commit()
    order = [_claim(db).id for _ in range(4)]
    assert order == [c.id, b.id, d.id, a.id]
    assert _claim(db) is None


def test_fairness_picks_oldest(db: Session) -> None:
    old = q.enqueue(db, JobType.RENDER, priority=Priority.MAINTENANCE)
    db.commit()
    q.enqueue(db, JobType.RENDER, priority=Priority.URGENT)
    db.commit()
    assert _claim(db, fairness=True).id == old.id


def test_lanes_are_independent(db: Session) -> None:
    q.enqueue(db, JobType.DISCOVER, priority=Priority.URGENT)
    db.commit()
    assert _claim(db, "heavy") is None
    assert _claim(db, "light") is not None


def test_claim_is_exclusive(db: Session) -> None:
    q.enqueue(db, JobType.ANALYZE, priority=1)
    db.commit()
    first = _claim(db)
    assert first and first.status == JobStatus.RUNNING and first.attempt_count == 1
    assert _claim(db) is None


def test_paused_source_jobs_are_not_claimed(db: Session) -> None:
    src = Source(kind="rss", name="x", url="https://e.com/f", paused=True)
    db.add(src)
    db.flush()
    q.enqueue(db, JobType.DISCOVER, priority=1, source_id=src.id)
    db.commit()
    assert _claim(db, "light") is None
    src.paused = False
    db.commit()
    assert _claim(db, "light") is not None


def test_exclude_types(db: Session) -> None:
    q.enqueue(db, JobType.RENDER, priority=0)
    q.enqueue(db, JobType.ANALYZE, priority=1)
    db.commit()
    j = _claim(db, exclude_types=[JobType.RENDER])
    assert j and j.job_type == JobType.ANALYZE


def test_transient_failure_backs_off_then_dead_letters(db: Session) -> None:
    q.enqueue(db, JobType.ANALYZE, priority=1, max_attempts=2)
    db.commit()
    j = _claim(db)
    q.fail(db, j, transient(ErrorCode.NETWORK_TIMEOUT, "timeout"), rng=random.Random(1))
    db.commit()
    assert j.status == JobStatus.RETRY_WAIT and j.next_attempt_at > utcnow()
    assert _claim(db) is None  # not before next_attempt_at
    j.next_attempt_at = utcnow() - timedelta(seconds=1)
    db.commit()
    j = _claim(db)
    assert j.attempt_count == 2
    q.fail(db, j, transient(ErrorCode.NETWORK_TIMEOUT, "timeout"))
    assert j.status == JobStatus.FAILED and j.error_code == ErrorCode.MAX_ATTEMPTS


def test_permanent_and_blocked_failures(db: Session) -> None:
    q.enqueue(db, JobType.ANALYZE, priority=1)
    q.enqueue(db, JobType.ANALYZE, priority=2)
    db.commit()
    a = _claim(db)
    q.fail(db, a, permanent(ErrorCode.CORRUPT_MEDIA, "bad"))
    assert a.status == JobStatus.FAILED and a.attempt_count == 1
    b = _claim(db)
    q.fail(db, b, blocked(ErrorCode.RIGHTS_NOT_CONFIRMED, "rights"))
    assert b.status == JobStatus.BLOCKED


def test_unknown_exception_is_safe(db: Session) -> None:
    q.enqueue(db, JobType.ANALYZE, priority=1)
    db.commit()
    j = _claim(db)
    q.fail(db, j, RuntimeError("secret path /Users/x token=abc"))
    assert "secret" not in (j.safe_error_message or "")
    assert j.status == JobStatus.RETRY_WAIT


def test_retry_after_is_honoured(db: Session) -> None:
    q.enqueue(db, JobType.DISCOVER, priority=1)
    db.commit()
    j = _claim(db, "light")
    q.fail(db, j, JobError(ErrorCode.RATE_LIMITED, "slow", transient=True, retry_after=7200))
    assert (j.next_attempt_at - utcnow()).total_seconds() > 7000


def test_backoff_grows_and_is_capped() -> None:
    r = random.Random(0)
    vals = [q.backoff_seconds(n, rng=r) for n in range(1, 12)]
    assert vals[0] <= 30 and vals[3] > vals[0]
    assert max(vals) <= q.BACKOFF_CAP_SECONDS


def test_lease_expiry_recovers_crashed_job(db: Session) -> None:
    q.enqueue(db, JobType.RENDER, priority=1)
    db.commit()
    j = _claim(db)
    j.lease_expires_at = utcnow() - timedelta(seconds=5)
    db.commit()
    assert q.recover_expired_leases(db) == 1
    db.refresh(j)
    assert j.status == JobStatus.RETRY_WAIT and j.error_code == ErrorCode.LEASE_EXPIRED


def test_heartbeat_extends_lease_and_reports_cancel(db: Session) -> None:
    q.enqueue(db, JobType.RENDER, priority=1)
    db.commit()
    j = _claim(db)
    before = j.lease_expires_at
    assert q.heartbeat(db, j.id, "w1", lease_seconds=600, progress=42, stage="renderar") is False
    db.refresh(j)
    assert j.lease_expires_at > before and j.progress_percent == 42
    q.request_cancel(db, j.id)
    db.commit()
    assert q.heartbeat(db, j.id, "w1", lease_seconds=600) is True
    assert q.heartbeat(db, j.id, "other-worker", lease_seconds=600) is True


def test_cancel_and_manual_retry(db: Session) -> None:
    j = q.enqueue(db, JobType.ANALYZE, priority=1)
    db.commit()
    q.request_cancel(db, j.id)
    assert j.status == JobStatus.CANCELLED
    q.manual_retry(db, j.id)
    assert j.status == JobStatus.PENDING and j.attempt_count == 0


def test_idempotency_key_returns_existing_and_raises_priority(db: Session) -> None:
    a = q.enqueue(db, JobType.RENDER, priority=3, idempotency_key="render:x")
    db.commit()
    b = q.enqueue(db, JobType.RENDER, priority=1, idempotency_key="render:x")
    assert a.id == b.id and b.priority == 1


def test_defer_does_not_consume_attempt(db: Session) -> None:
    q.enqueue(db, JobType.RENDER, priority=1)
    db.commit()
    j = _claim(db)
    q.defer(db, j, until=utcnow() + timedelta(hours=1), reason="cap")
    assert j.status == JobStatus.RETRY_WAIT and j.attempt_count == 0


def test_invalid_transitions_rejected() -> None:
    with pytest.raises(InvalidTransition):
        assert_transition("succeeded", "running")
    with pytest.raises(InvalidTransition):
        assert_transition("pending", "succeeded")
    assert_transition("running", "succeeded")


def test_queue_persists_across_engine_restart(cfg, db: Session) -> None:
    from app.db.session import init_engine, new_session

    j = q.enqueue(db, JobType.ANALYZE, priority=0)
    db.commit()
    db.close()
    init_engine(cfg.database_url)  # simulates a process restart
    s2 = new_session()
    assert s2.get(Job, j.id).status == JobStatus.PENDING
    s2.close()
