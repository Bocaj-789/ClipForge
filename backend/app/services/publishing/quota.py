"""Local YouTube Data API quota ledger.

Google resets quota at midnight Pacific Time. ClipForge counts its own calls per PT day in two
buckets and refuses to exceed the configured limits, so it stops *before* Google starts returning
``quotaExceeded`` (which also costs quota). Google remains the source of truth: a real
``quotaExceeded`` response is honoured too (see the uploader).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.db.base import new_id, utcnow
from app.db.models import QuotaUsage
from app.services.publishing.youtube import YouTubeError
from app.services.settings_service import AppSettings

PT = ZoneInfo("America/Los_Angeles")
INSERT = "insert"  # videos.insert calls: their own bucket (default 100/day, 1 per call)
UNITS = "units"  # all other endpoints (default 10,000 units/day)
SEARCH = "search"  # search.list calls: their own Google bucket (default 100/day, 1 per call)


def pt_day(now: datetime | None = None) -> str:
    return (now or utcnow()).astimezone(PT).strftime("%Y-%m-%d")


def next_reset(now: datetime | None = None) -> datetime:
    """The next midnight Pacific Time, as an aware UTC datetime (DST-safe)."""
    local = (now or utcnow()).astimezone(PT)
    tomorrow = (local + timedelta(days=1)).date()
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=PT).astimezone(ZoneInfo("UTC"))


def limit_for(settings: AppSettings, bucket: str) -> int:
    if bucket == INSERT:
        return settings.youtube_daily_insert_limit
    if bucket == SEARCH:
        return settings.youtube_daily_search_limit
    return settings.youtube_daily_unit_limit


def used(db: Session, bucket: str, now: datetime | None = None) -> int:
    return (
        db.scalar(select(QuotaUsage.used).where(QuotaUsage.day == pt_day(now), QuotaUsage.bucket == bucket))
        or 0
    )


def remaining(db: Session, settings: AppSettings, bucket: str, now: datetime | None = None) -> int:
    return max(0, limit_for(settings, bucket) - used(db, bucket, now))


def consume(db: Session, bucket: str, n: int = 1, now: datetime | None = None) -> None:
    """Atomically add ``n`` to today's counter (safe across the API and worker processes)."""
    stmt = sqlite_insert(QuotaUsage).values(
        id=new_id(), day=pt_day(now), bucket=bucket, used=n, updated_at=now or utcnow()
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["day", "bucket"],
        set_={"used": QuotaUsage.used + n, "updated_at": now or utcnow()},
    )
    db.execute(stmt)


def status(db: Session, settings: AppSettings, now: datetime | None = None) -> dict[str, object]:
    now = now or utcnow()
    return {
        "day_pacific": pt_day(now),
        "resets_at": next_reset(now),
        "insert": {
            "used": used(db, INSERT, now),
            "limit": settings.youtube_daily_insert_limit,
            "remaining": remaining(db, settings, INSERT, now),
        },
        "search": {
            "used": used(db, SEARCH, now),
            "limit": settings.youtube_daily_search_limit,
            "remaining": remaining(db, settings, SEARCH, now),
        },
        "units": {
            "used": used(db, UNITS, now),
            "limit": settings.youtube_daily_unit_limit,
            "remaining": remaining(db, settings, UNITS, now),
        },
    }


def make_charge(db: Session, settings: AppSettings):  # type: ignore[no-untyped-def]
    """Return the ``charge(bucket, n)`` callback used by :class:`YouTubeApi`.

    Commits immediately so the count survives a crash right after the HTTP call and so no write
    transaction stays open while the (slow) network request runs.
    """

    def charge(bucket: str, n: int) -> None:
        if remaining(db, settings, bucket) < n:
            raise YouTubeError(
                "quota",
                "ClipForges lokala dagsgräns för YouTube-kvot är nådd. Fortsätter efter nästa nollställning.",
                reason="localQuota",
            )
        consume(db, bucket, n)
        db.commit()

    return charge
