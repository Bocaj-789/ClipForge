"""Direct HTTPS download from allow-listed hosts (archive.org and similar)."""

from __future__ import annotations

from urllib.parse import urlsplit

from app.core.errors import ErrorCode, permanent, transient
from app.services.acquisition.provider import AcquiredMedia, MediaRequest, ProviderMetadata
from app.services.rendering.ffmpeg import Cancelled
from app.services.safe_fetch import FetchError, fetch_to_file
from app.services.urls import UrlRejected, host_allowed


class DirectHttpProvider:
    name = "direct_http"

    def __init__(self, allowed_hosts: list[str]) -> None:
        self.allowed_hosts = list(allowed_hosts)

    def available(self) -> tuple[bool, str | None]:
        return True, None

    def supports(self, platform: str, url: str) -> bool:
        return platform == "direct" and host_allowed(urlsplit(url).hostname or "", self.allowed_hosts)

    def fetch_metadata(self, url: str) -> ProviderMetadata | None:
        return None

    def acquire(self, request: MediaRequest) -> AcquiredMedia:
        request.workdir.mkdir(parents=True, exist_ok=True)
        dest = request.workdir / "download.bin"
        request.progress(1, "hämtar")
        try:
            fetch_to_file(
                request.url,
                dest,
                max_bytes=request.max_bytes,
                allowed_hosts=self.allowed_hosts,
                timeout=60,
                accept="video/*, audio/*",
                on_progress=lambda n, total: request.progress(90 * n / total if total else 5, "hämtar"),
                should_cancel=request.cancelled,
            )
        except FetchError as e:
            if request.cancelled():
                raise Cancelled() from e
            if e.status is None or e.status == 429 or e.status >= 500:
                raise transient(ErrorCode.NETWORK_TIMEOUT, str(e), retry_after=e.retry_after) from e
            raise permanent(ErrorCode.ACQUISITION_BLOCKED, str(e)) from e
        except UrlRejected as e:
            raise permanent(ErrorCode.UNSUPPORTED_URL, str(e)) from e
        return AcquiredMedia(path=dest, provider=self.name)
