"""Deterministic publication policy. No model output can influence these decisions.

Used to gate every publication attempt (manual or automatic). Uploading itself (OAuth +
videos.insert) is a later milestone; this gate is enforced first so it cannot be bypassed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.db.models import PERMITTED_RIGHTS, Channel, RenderedClip, ReviewStatus
from app.services.intelligence.metadata import sanitize_title
from app.services.settings_service import AppSettings

AUTO_PUBLISH_CONFIRM_PHRASE = "JAG FÖRSTÅR RISKERNA"


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reasons: list[str]


def check_publication(
    *,
    settings: AppSettings,
    channel: Channel,
    clip: RenderedClip,
    rights_status: str,
    automatic: bool,
    uploads_today_channel: int,
    uploads_today_global: int,
    last_upload_at: datetime | None,
    now: datetime,
    file_ok: bool,
    min_score: float = 0,
) -> PolicyDecision:
    reasons: list[str] = []
    if rights_status not in PERMITTED_RIGHTS:
        reasons.append("Rättigheterna för källan är inte bekräftade.")
    if not channel.enabled or channel.paused:
        reasons.append("Kanalen är avstängd eller pausad.")
    if channel.token_reference is None:
        reasons.append("Kanalen är inte ansluten via OAuth.")
    if automatic:
        if not settings.auto_publish_global_enabled:
            reasons.append("Automatisk publicering är avstängd globalt.")
        if not channel.auto_publish_enabled:
            reasons.append("Automatisk publicering är inte aktiverad för kanalen.")
        if (
            clip.review_status != ReviewStatus.APPROVED
            and clip.clip is not None
            and clip.clip.final_score < max(min_score, 0)
        ):
            reasons.append("Klippets poäng är under kanalens gräns.")
        if clip.warnings_json:
            reasons.append("Klippet har varningar och kräver manuell granskning.")
    elif clip.review_status != ReviewStatus.APPROVED:
        reasons.append("Klippet är inte godkänt.")
    if clip.review_status == ReviewStatus.REJECTED:
        reasons.append("Klippet är avvisat.")
    if uploads_today_channel >= channel.max_uploads_per_day:
        reasons.append("Kanalens dagliga uppladdningsgräns är nådd.")
    if uploads_today_global >= settings.max_uploads_per_day_global:
        reasons.append("Den globala dagliga uppladdningsgränsen är nådd.")
    if last_upload_at and now - last_upload_at < timedelta(minutes=channel.min_spacing_minutes):
        reasons.append("Minsta tid mellan uppladdningar har inte passerat.")
    if not sanitize_title(clip.title_suggestion or ""):
        reasons.append("Titeln är tom eller ogiltig.")
    if not file_ok:
        reasons.append("Videofilen klarade inte mediekontrollen.")
    return PolicyDecision(not reasons, reasons)


def next_slot(channel: Channel, after: datetime) -> datetime | None:
    """Next allowed publishing time in the channel's timezone (DST-safe via zoneinfo).

    schedule_json: {"days": [0..6] (Mon=0), "windows": [["09:00","11:00"], ...]}.
    Returns None if the schedule has no valid slot in the next 14 days.
    """
    tz = ZoneInfo(channel.timezone)
    sched = channel.schedule_json or {}
    days = set(sched.get("days") or range(7))
    windows = sched.get("windows") or [["09:00", "21:00"]]
    local = after.astimezone(tz)
    for d in range(15):
        day = (local + timedelta(days=d)).date()
        if day.weekday() not in days:
            continue
        for start_s, end_s in sorted(windows):
            sh, sm = map(int, start_s.split(":"))
            eh, em = map(int, end_s.split(":"))
            start = datetime(day.year, day.month, day.day, sh, sm, tzinfo=tz)
            end = datetime(day.year, day.month, day.day, eh, em, tzinfo=tz)
            if end <= local:
                continue
            candidate = max(start, local)
            if candidate < end:
                return candidate.astimezone(ZoneInfo("UTC"))
    return None
