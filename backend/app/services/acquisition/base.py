"""Media acquisition policy. Deliberately strict: no downloaders for platforms without a
documented, permitted media-access method; unknown rights never acquire automatically."""

from __future__ import annotations

from dataclasses import dataclass

from app.db.models import PERMITTED_RIGHTS, CandidateVideo, RightsStatus
from app.services.urls import host_allowed


@dataclass(frozen=True)
class AcquisitionDecision:
    allowed: bool
    method: str | None  # local_file | direct_http
    reason: str | None


YOUTUBE_RESTRICTION = (
    "YouTube erbjuder inget officiellt API för att ladda ned videofiler, och ClipForge kringgår inte "
    "plattformens villkor. Om det är ditt eget material: ladda ned originalet från YouTube Studio och "
    "bifoga filen här."
)


def rights_ok(rights_status: str) -> bool:
    return rights_status in PERMITTED_RIGHTS


def rights_message(rights_status: str) -> str:
    if rights_status == RightsStatus.NOT_PERMITTED:
        return "Materialet är markerat som ej tillåtet att bearbeta."
    return (
        "Rättigheterna är inte bekräftade. Ange att materialet är ditt, licensierat, verifierat "
        "public domain eller att du har tillstånd innan det bearbetas."
    )


def decide(candidate: CandidateVideo, *, direct_media_hosts: list[str]) -> AcquisitionDecision:
    if not rights_ok(candidate.rights_status):
        return AcquisitionDecision(False, None, rights_message(candidate.rights_status))
    if candidate.media_path:
        return AcquisitionDecision(True, "local_file", None)
    if candidate.platform == "youtube":
        return AcquisitionDecision(False, None, YOUTUBE_RESTRICTION)
    if candidate.platform == "direct":
        from urllib.parse import urlsplit

        host = urlsplit(candidate.canonical_url).hostname or ""
        if host_allowed(host, direct_media_hosts):
            return AcquisitionDecision(True, "direct_http", None)
        return AcquisitionDecision(False, None, "Värden finns inte bland tillåtna mediavärdar.")
    return AcquisitionDecision(False, None, "Ingen tillåten metod för att hämta media från denna källa.")
