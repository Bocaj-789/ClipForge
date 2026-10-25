"""YouTube media provider backed by yt-dlp (Python API).

What this provider does: for a single, public YouTube video whose rights the user has confirmed,
it reads metadata and downloads one merged MP4 into the job's temporary directory.

What it deliberately does not do:

* No circumvention. It never uses cookies, logins, browser profiles, proxies, DRM tooling or
  anything that defeats an access control. When YouTube asks for sign-in or a bot check, the
  video is private / members-only / age-gated / region-locked / DRM-protected, or no downloadable
  format is offered, the job is parked as *awaiting media access* and the user can attach the file
  themselves (for example the original from YouTube Studio).
* No remote code. ``remote_components`` stays off; the challenge solver ships in the pinned
  ``yt-dlp-ejs`` package and runs on the local Node.js/Deno runtime.
* No open-ended downloader. The URL is re-validated as a single-video YouTube URL, extractors are
  restricted to ``youtube``, playlists are off, user yt-dlp config files are ignored.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.errors import ErrorCode, JobError, awaiting_media, blocked, permanent, transient
from app.core.security import clean_text
from app.services.acquisition.provider import AcquiredMedia, MediaRequest, ProgressFn, ProviderMetadata
from app.services.rendering.ffmpeg import Cancelled
from app.services.urls import YOUTUBE_HOSTS, UrlKind, UrlRejected, classify_url

log = logging.getLogger("clipforge.youtube")

_MAX_ERROR_DETAIL = 200
_SKIP_SUFFIXES = {".part", ".ytdl", ".temp", ".tmp", ".json", ".vtt", ".srt", ".jpg", ".webp", ".png"}


def build_format_selector(max_height: int | None) -> str:
    """yt-dlp format selector. ``None`` means the best available quality.

    Prefers native H.264/AAC (cheap to decode on the M1) and never AV1.
    """
    no_av1 = "[vcodec!*=?av01]"  # "?" keeps formats whose codec is unknown
    if not max_height:
        return f"bestvideo{no_av1}+bestaudio/best{no_av1}"
    h = f"[height<=?{int(max_height)}]"
    return (
        f"bestvideo{h}[ext=mp4]{no_av1}+bestaudio[ext=m4a]/"
        f"bestvideo{h}{no_av1}+bestaudio/"
        f"best{h}[ext=mp4]{no_av1}/"
        f"best{h}{no_av1}"
    )


def js_runtime() -> dict[str, dict[str, str]] | None:
    """The local JavaScript runtime yt-dlp needs for YouTube player challenges."""
    for name in ("deno", "node"):
        path = shutil.which(name)
        if path:
            return {name: {"path": path}}
    return None


def build_options(
    outtmpl: str,
    *,
    max_height: int | None,
    ffmpeg_location: str | None,
    runtime: dict[str, dict[str, str]] | None,
    progress_hook: Callable[[dict[str, Any]], None] | None,
    allowed_extractors: tuple[str, ...] = ("youtube",),
) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "format": build_format_selector(max_height),
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ignoreconfig": True,
        "restrictfilenames": True,
        "allowed_extractors": list(allowed_extractors),
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "cachedir": False,
        "overwrites": True,
        "progress_hooks": [progress_hook] if progress_hook else [],
    }
    if ffmpeg_location:
        opts["ffmpeg_location"] = ffmpeg_location
    if runtime:
        opts["js_runtimes"] = runtime
    return opts


def _detail(e: BaseException) -> str:
    msg = str(e).replace("ERROR:", "").strip()
    return clean_text(msg.splitlines()[-1] if msg else "", _MAX_ERROR_DETAIL)


_NO_CIRCUMVENTION = (
    " ClipForge kringgår inte det. Bifoga videofilen i stället (t.ex. originalet från YouTube Studio)."
)

# (needles, user message). Order matters: the first match wins. All of these mean "the source will
# not hand over the media", so the job parks as awaiting media access.
_ACCESS_RULES: list[tuple[tuple[str, ...], str]] = [
    (
        ("not a bot", "sign in to confirm"),
        "YouTube kräver inloggning eller bot-kontroll för den här videon." + _NO_CIRCUMVENTION,
    ),
    (("drm",), "Videon är DRM-skyddad och kan inte hämtas." + _NO_CIRCUMVENTION),
    (("private video",), "Videon är privat." + _NO_CIRCUMVENTION),
    (
        ("members-only", "members only", "join this channel"),
        "Videon är endast för kanalmedlemmar." + _NO_CIRCUMVENTION,
    ),
    (
        ("premium", "rental", "purchase", "requires payment"),
        "Videon kräver betalning eller prenumeration." + _NO_CIRCUMVENTION,
    ),
    (
        ("confirm your age", "age-restricted", "age restricted", "inappropriate for some users"),
        "Videon är åldersbegränsad och kräver inloggning." + _NO_CIRCUMVENTION,
    ),
    (
        (
            "not available in your country",
            "geo restrict",
            "geo-restrict",
            "blocked it in your country",
            "available in your country",
        ),
        "Videon är inte tillgänglig i ditt land." + _NO_CIRCUMVENTION,
    ),
    (
        (
            "video unavailable",
            "has been removed",
            "no longer available",
            "this video is not available",
            "been terminated",
        ),
        "Videon är borttagen eller otillgänglig." + _NO_CIRCUMVENTION,
    ),
    (
        ("live event", "is live", "premieres in", "will begin in"),
        "Videon är en direktsändning eller premiär som inte är klar än. Försök igen när den har publicerats.",
    ),
    (
        (
            "requested format is not available",
            "no video formats found",
            "no formats",
            "only images are available",
        ),
        "YouTube erbjuder inga nedladdningsbara format för videon just nu." + _NO_CIRCUMVENTION,
    ),
]


def translate_error(e: BaseException) -> JobError:
    """Map a yt-dlp failure to a stable, Swedish, user-safe job error."""
    low = str(e).lower()
    for needles, message in _ACCESS_RULES:
        if any(n in low for n in needles):
            return awaiting_media(message)
    if "larger than max-filesize" in low or "max-filesize" in low:
        return permanent(ErrorCode.MEDIA_TOO_LARGE, "Videon är större än tillåten maxstorlek.")
    if "http error 429" in low or "too many requests" in low:
        return transient(ErrorCode.NETWORK_TIMEOUT, "YouTube begränsar förfrågningarna just nu.", 900)
    if "ffmpeg" in low and ("not found" in low or "not installed" in low):
        return blocked(ErrorCode.FFMPEG_MISSING, "ffmpeg krävs för att slå ihop video och ljud.")
    if any(
        s in low
        for s in (
            "timed out",
            "timeout",
            "connection",
            "temporary failure",
            "network",
            "http error 5",
            "unable to download",
            "remote end closed",
        )
    ):
        return transient(ErrorCode.NETWORK_TIMEOUT, "Nätverksfel vid hämtning från YouTube.", 120)
    return permanent(ErrorCode.ACQUISITION_BLOCKED, f"Kunde inte hämta videon från YouTube: {_detail(e)}")


def _parse_date(info: dict[str, Any]) -> datetime | None:
    ts = info.get("timestamp") or info.get("release_timestamp")
    if isinstance(ts, int | float):
        return datetime.fromtimestamp(float(ts), tz=UTC)
    raw = info.get("upload_date")
    if isinstance(raw, str) and len(raw) == 8 and raw.isdigit():
        return datetime(int(raw[:4]), int(raw[4:6]), int(raw[6:]), tzinfo=UTC)
    return None


class YouTubeProvider:
    name = "yt_dlp"

    def __init__(
        self,
        *,
        ffmpeg_bin: str = "ffmpeg",
        ydl_factory: Callable[[dict[str, Any]], Any] | None = None,
        runtime: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self.ffmpeg_bin = ffmpeg_bin
        self._ydl_factory = ydl_factory
        self._runtime = runtime
        self.allowed_extractors: tuple[str, ...] = ("youtube",)

    # ---------------------------------------------------------- policy
    def _video_id(self, url: str) -> str:
        """Validate ``url`` as one canonical YouTube video. Subclassed in tests only."""
        try:
            c = classify_url(url, direct_media_hosts=[])
        except UrlRejected as e:
            raise permanent(ErrorCode.UNSUPPORTED_URL, str(e)) from e
        if c.kind != UrlKind.YOUTUBE_VIDEO or c.external_id is None or c.host not in YOUTUBE_HOSTS:
            raise permanent(ErrorCode.UNSUPPORTED_URL, "Endast enskilda YouTube-videor kan hämtas.")
        return c.external_id

    def supports(self, platform: str, url: str) -> bool:
        if platform != "youtube":
            return False
        try:
            self._video_id(url)
        except JobError:
            return False
        return True

    def available(self) -> tuple[bool, str | None]:
        try:
            import yt_dlp  # noqa: F401
        except ImportError:
            return False, "yt-dlp är inte installerat (pip install yt-dlp)."
        if shutil.which(self.ffmpeg_bin) is None:
            return False, f"{self.ffmpeg_bin} hittades inte. Installera med: brew install ffmpeg"
        if (self._runtime or js_runtime()) is None:
            return False, "Node.js eller Deno krävs för YouTube (brew install node)."
        return True, None

    # ---------------------------------------------------------- yt-dlp plumbing
    def _factory(self) -> Callable[[dict[str, Any]], Any]:
        if self._ydl_factory is not None:
            return self._ydl_factory
        from yt_dlp import YoutubeDL

        return YoutubeDL

    def _options(
        self,
        outtmpl: str,
        *,
        max_height: int | None,
        hook: Callable[[dict[str, Any]], None] | None,
    ) -> dict[str, Any]:
        return build_options(
            outtmpl,
            max_height=max_height,
            ffmpeg_location=shutil.which(self.ffmpeg_bin) or None,
            runtime=self._runtime or js_runtime(),
            progress_hook=hook,
            allowed_extractors=self.allowed_extractors,
        )

    # ---------------------------------------------------------- metadata
    def fetch_metadata(self, url: str) -> ProviderMetadata | None:
        """Best-effort read-only metadata. Failures are not fatal; acquisition reports access issues."""
        try:
            self._video_id(url)
            opts = self._options("%(id)s.%(ext)s", max_height=None, hook=None)
            opts["skip_download"] = True
            opts["ignore_no_formats_error"] = True  # metadata is useful even when nothing is downloadable
            with self._factory()(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:  # noqa: BLE001 - metadata must never fail the pipeline
            log.info("yt-dlp metadata unavailable: %s", _detail(e))
            return None
        if not isinstance(info, dict):
            return None
        duration = info.get("duration")
        live = bool(info.get("is_live")) or info.get("live_status") in ("is_live", "is_upcoming")
        thumb = str(info.get("thumbnail") or "")
        return ProviderMetadata(
            title=clean_text(str(info.get("title") or ""), 500) or None,
            creator_name=clean_text(str(info.get("channel") or info.get("uploader") or ""), 300) or None,
            creator_channel_id=clean_text(str(info.get("channel_id") or ""), 128) or None,
            description=clean_text(str(info.get("description") or ""), 5000) or None,
            published_at=_parse_date(info),
            duration_seconds=float(duration) if isinstance(duration, int | float) else None,
            thumbnail_url=thumb if thumb.startswith("https://i.ytimg.com/") else None,
            is_live=live,
            extra={"availability": info.get("availability"), "live_status": info.get("live_status")},
        )

    # ---------------------------------------------------------- download
    def acquire(self, request: MediaRequest) -> AcquiredMedia:
        from yt_dlp.utils import DownloadCancelled, DownloadError, ExtractorError

        self._video_id(request.url)
        request.workdir.mkdir(parents=True, exist_ok=True)
        peak = [0.0]
        stream_sizes: dict[str, int] = {}
        too_large = [False]
        on_progress: ProgressFn = request.progress

        def hook(d: dict[str, Any]) -> None:
            if request.cancelled():
                raise DownloadCancelled("cancelled")
            if d.get("status") != "downloading":
                return
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            if total:
                # Cap the combined size of the video + audio streams (max_filesize would instead hide
                # the formats and look like "nothing downloadable").
                stream_sizes[str(d.get("filename"))] = int(total)
                if sum(stream_sizes.values()) > request.max_bytes:
                    too_large[0] = True
                    raise DownloadCancelled("too large")
            if total:
                peak[0] = max(peak[0], min(88.0, 88.0 * done / total))
            on_progress(peak[0] or 2.0, "hämtar")

        opts = self._options(
            str(request.workdir / "%(id)s.%(ext)s"),
            max_height=request.max_height,
            hook=hook,
        )
        try:
            with self._factory()(opts) as ydl:
                info = ydl.extract_info(request.url, download=False)
                if not isinstance(info, dict):
                    raise permanent(
                        ErrorCode.ACQUISITION_BLOCKED, "YouTube returnerade ingen videoinformation."
                    )
                if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
                    raise awaiting_media(
                        "Videon är en direktsändning eller premiär som inte är klar än. "
                        "Försök igen när den har publicerats."
                    )
                duration = info.get("duration")
                if isinstance(duration, int | float) and duration > request.max_duration_seconds:
                    raise permanent(ErrorCode.MEDIA_TOO_LONG, "Videon är längre än tillåten maxlängd.")
                on_progress(2.0, "hämtar")
                done_info = ydl.process_ie_result(info, download=True)
        except DownloadCancelled as e:
            if too_large[0]:
                raise permanent(ErrorCode.MEDIA_TOO_LARGE, "Videon är större än tillåten maxstorlek.") from e
            raise Cancelled() from e
        except JobError:
            raise
        except (DownloadError, ExtractorError) as e:
            cause = e.exc_info[1] if getattr(e, "exc_info", None) else None
            if too_large[0]:
                raise permanent(ErrorCode.MEDIA_TOO_LARGE, "Videon är större än tillåten maxstorlek.") from e
            if isinstance(cause, DownloadCancelled) or request.cancelled():
                raise Cancelled() from e
            raise translate_error(e) from e
        except OSError as e:
            raise transient(ErrorCode.NETWORK_TIMEOUT, "Nätverks- eller filfel vid hämtning.", 120) from e

        path = self._find_output(request.workdir, done_info if isinstance(done_info, dict) else info)
        if path is None:
            raise awaiting_media(
                "Hämtningen gav ingen videofil – videon är troligen inte nedladdningsbar." + _NO_CIRCUMVENTION
            )
        return AcquiredMedia(
            path=path,
            provider=self.name,
            title=clean_text(str(info.get("title") or ""), 300) or None,
            duration_seconds=float(duration) if isinstance(duration, int | float) else None,
            extra={
                "height": info.get("height"),
                "vcodec": info.get("vcodec"),
                "video_id": info.get("id"),
            },
        )

    @staticmethod
    def _find_output(workdir: Path, info: dict[str, Any]) -> Path | None:
        for item in info.get("requested_downloads") or []:
            fp = item.get("filepath") if isinstance(item, dict) else None
            if fp and Path(fp).is_file() and Path(fp).resolve().is_relative_to(workdir.resolve()):
                return Path(fp)
        files = sorted(
            (p for p in workdir.iterdir() if p.is_file() and p.suffix.lower() not in _SKIP_SUFFIXES),
            key=lambda p: p.stat().st_size,
            reverse=True,
        )
        return files[0] if files else None
