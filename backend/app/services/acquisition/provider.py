"""Media acquisition provider interface.

The rest of ClipForge never talks to a downloader directly. Every way of obtaining media
(direct HTTP from an allow-listed host, yt-dlp for YouTube, a future licensed API...) is a
:class:`MediaProvider`. Swapping or adding a method means writing one class and registering it in
:func:`build_providers`; the queue, pipeline and dashboard stay untouched.

Contract:

* ``supports`` is a cheap, offline check on platform/URL.
* ``available`` reports whether the provider can run at all (binary missing, disabled...).
* ``fetch_metadata`` is best effort and read-only. It may return ``None``.
* ``acquire`` writes exactly one media file below ``request.workdir`` and returns it. It must
  raise :class:`~app.core.errors.JobError` for expected failures: use ``awaiting_media(...)`` when
  the source simply cannot deliver the media (private, sign-in required, DRM, geo...) so the
  job parks without blocking the queue, ``transient(...)`` for retryable network trouble and
  ``permanent(...)`` otherwise. It raises :class:`~app.services.rendering.ffmpeg.Cancelled` on
  cancellation. A provider never works around DRM, logins or other access controls.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.config import Config
from app.services.settings_service import AppSettings

ProgressFn = Callable[[float, str | None], None]


@dataclass(frozen=True)
class MediaRequest:
    url: str
    platform: str
    external_id: str | None
    workdir: Path  # per-job temporary directory; the caller deletes it
    max_bytes: int
    max_duration_seconds: float
    max_height: int | None
    progress: ProgressFn
    cancelled: Callable[[], bool]


@dataclass(frozen=True)
class AcquiredMedia:
    path: Path
    provider: str
    title: str | None = None
    duration_seconds: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderMetadata:
    title: str | None = None
    creator_name: str | None = None
    creator_channel_id: str | None = None
    description: str | None = None
    published_at: datetime | None = None
    duration_seconds: float | None = None
    thumbnail_url: str | None = None
    is_live: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class MediaProvider(Protocol):
    name: str

    def available(self) -> tuple[bool, str | None]: ...

    def supports(self, platform: str, url: str) -> bool: ...

    def fetch_metadata(self, url: str) -> ProviderMetadata | None: ...

    def acquire(self, request: MediaRequest) -> AcquiredMedia: ...


ProvidersFactory = Callable[[Config, AppSettings], Sequence[MediaProvider]]


def build_providers(cfg: Config, settings: AppSettings) -> list[MediaProvider]:
    """The configured providers in priority order. This is the single registration point."""
    from app.services.acquisition.direct_http import DirectHttpProvider
    from app.services.acquisition.youtube import YouTubeProvider

    providers: list[MediaProvider] = [DirectHttpProvider(settings.allowed_direct_media_hosts)]
    if settings.youtube_media_provider == "yt_dlp":
        providers.append(YouTubeProvider(ffmpeg_bin=cfg.ffmpeg_bin))
    return providers


def select_provider(providers: Sequence[MediaProvider], platform: str, url: str) -> MediaProvider | None:
    return next((p for p in providers if p.supports(platform, url)), None)
