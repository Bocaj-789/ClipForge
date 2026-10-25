"""Media acquisition policy: *whether* media may be fetched and *which* provider does it.

Rights come first. Media of unknown or forbidden rights is never acquired automatically. After
that the decision is delegated to the registered :class:`MediaProvider` list, so supporting a new
source never touches this module.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.db.models import PERMITTED_RIGHTS, CandidateVideo, RightsStatus
from app.services.acquisition.provider import MediaProvider, select_provider


@dataclass(frozen=True)
class AcquisitionDecision:
    allowed: bool
    method: str | None  # "local_file" or a provider name (direct_http, yt_dlp, ...)
    reason: str | None
    # True when rights are fine but no provider can deliver the media (user can attach a file).
    awaiting_media: bool = False
    provider: MediaProvider | None = None


def rights_ok(rights_status: str) -> bool:
    return rights_status in PERMITTED_RIGHTS


def rights_message(rights_status: str) -> str:
    if rights_status == RightsStatus.NOT_PERMITTED:
        return "Materialet är markerat som ej tillåtet att bearbeta."
    return (
        "Rättigheterna är inte bekräftade. Ange att materialet är ditt, licensierat, verifierat "
        "public domain eller att du har tillstånd innan det bearbetas."
    )


NO_PROVIDER_MESSAGE = (
    "Ingen mediaprovider kan hämta den här källan. Bifoga videofilen, eller aktivera en provider under "
    "Inställningar."
)


def decide(candidate: CandidateVideo, *, providers: Sequence[MediaProvider]) -> AcquisitionDecision:
    if not rights_ok(candidate.rights_status):
        return AcquisitionDecision(False, None, rights_message(candidate.rights_status))
    if candidate.media_path:
        return AcquisitionDecision(True, "local_file", None)
    provider = select_provider(providers, candidate.platform, candidate.canonical_url)
    if provider is None:
        return AcquisitionDecision(False, None, NO_PROVIDER_MESSAGE, awaiting_media=True)
    ok, reason = provider.available()
    if not ok:
        return AcquisitionDecision(False, None, reason or NO_PROVIDER_MESSAGE, awaiting_media=True)
    return AcquisitionDecision(True, provider.name, None, provider=provider)
