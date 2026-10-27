"""Job state machine. All status changes go through :func:`assert_transition`."""

from __future__ import annotations

from app.db.models import JobStatus, JobType

S = JobStatus

ALLOWED: dict[JobStatus, frozenset[JobStatus]] = {
    S.PENDING: frozenset({S.RUNNING, S.CANCELLED, S.BLOCKED, S.AWAITING_MEDIA}),
    S.RETRY_WAIT: frozenset({S.RUNNING, S.CANCELLED, S.PENDING, S.BLOCKED, S.AWAITING_MEDIA}),
    S.RUNNING: frozenset({S.SUCCEEDED, S.FAILED, S.RETRY_WAIT, S.CANCELLED, S.BLOCKED, S.AWAITING_MEDIA}),
    S.BLOCKED: frozenset({S.PENDING, S.CANCELLED}),
    S.AWAITING_MEDIA: frozenset({S.PENDING, S.CANCELLED}),
    S.FAILED: frozenset({S.PENDING, S.CANCELLED}),
    S.CANCELLED: frozenset({S.PENDING}),
    S.SUCCEEDED: frozenset({S.PENDING}),  # explicit reprocess only
}

ACTIVE = frozenset({S.PENDING, S.RETRY_WAIT, S.RUNNING})
# Parked until the user (or a provider change) supplies what is missing; never claimed by workers.
WAITING = frozenset({S.BLOCKED, S.AWAITING_MEDIA})
TERMINAL = frozenset({S.SUCCEEDED, S.FAILED, S.CANCELLED})


class InvalidTransition(ValueError):
    pass


def assert_transition(current: str, target: str) -> None:
    cur, tgt = JobStatus(current), JobStatus(target)
    if tgt not in ALLOWED[cur]:
        raise InvalidTransition(f"{cur} -> {tgt} is not allowed")


# Lanes keep network work, heavy CPU work and publishing from starving each other.
LANE_FOR: dict[str, str] = {
    JobType.DISCOVER: "light",
    JobType.FETCH_METADATA: "light",
    JobType.ANALYTICS: "light",
    JobType.MAINTENANCE: "light",
    JobType.ACQUIRE: "download",
    JobType.PUBLISH: "io",
    JobType.ANALYZE: "heavy",
    # Transcription uses several CPU threads itself, so it gets one job at a time on its own lane.
    JobType.TRANSCRIBE: "transcribe",
    JobType.EXTRACT_CLIPS: "heavy",
    JobType.RENDER: "heavy",
}

# Job types that write significant data to disk; not claimed when storage is critical.
DISK_HEAVY_TYPES = frozenset({JobType.ACQUIRE, JobType.TRANSCRIBE, JobType.RENDER})


class Priority:
    URGENT = 0  # user-pasted URL marked urgent
    USER = 1  # other user submissions
    CREATOR = 2  # configured creator/channel updates
    DISCOVERY = 3  # general discovery
    MAINTENANCE = 4  # analytics refresh, cleanup
