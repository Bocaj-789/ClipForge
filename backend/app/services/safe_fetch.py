"""SSRF-hardened HTTP fetching for user-supplied URLs (feeds, permitted media files).

* DNS is resolved by us; every resolved address must be public. The connection is made to
  the vetted IP (prevents DNS rebinding between check and connect) while TLS still verifies
  the original hostname via SNI.
* Redirects are followed manually and every hop is revalidated.
* Bodies are streamed with a hard byte cap.
"""

from __future__ import annotations

import socket
import ssl
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpcore

from app.services.urls import UrlRejected, ip_is_public, revalidate_redirect

USER_AGENT = "ClipForge/0.1 (+local-first; respectful polling)"


class FetchError(Exception):
    def __init__(self, message: str, *, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def resolve_public(host: str, port: int) -> str:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise FetchError("Värdnamnet kunde inte slås upp.") from e
    addrs = [str(i[4][0]) for i in infos]
    if not addrs or not all(ip_is_public(a) for a in addrs):
        raise UrlRejected("Värden pekar på en privat eller otillåten nätverksadress.")
    return addrs[0]


class _GuardedBackend(httpcore.NetworkBackend):
    def __init__(self, resolver: Callable[[str, int], str] = resolve_public) -> None:
        self._inner = httpcore.SyncBackend()
        self._resolver = resolver

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        ip = self._resolver(host, port)
        return self._inner.connect_tcp(ip, port, timeout, local_address, socket_options)

    def connect_unix_socket(self, *a: Any, **k: Any) -> httpcore.NetworkStream:  # pragma: no cover
        raise UrlRejected("Unix-socketar är inte tillåtna.")

    def sleep(self, seconds: float) -> None:  # pragma: no cover
        self._inner.sleep(seconds)


@dataclass
class FetchResult:
    url: str
    status: int
    headers: dict[str, str]
    bytes_written: int


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def guarded_stream(
    url: str,
    *,
    sink: Callable[[bytes], object],
    max_bytes: int,
    allowed_hosts: list[str] | None,
    timeout: float = 30.0,
    max_redirects: int = 3,
    accept: str = "*/*",
    on_progress: Callable[[int, int | None], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> FetchResult:
    ctx = ssl.create_default_context()
    with httpcore.ConnectionPool(ssl_context=ctx, network_backend=_GuardedBackend(), retries=0) as pool:
        current = revalidate_redirect(url, allowed_hosts=allowed_hosts)
        for _ in range(max_redirects + 1):
            headers = [(b"User-Agent", USER_AGENT.encode()), (b"Accept", accept.encode())]
            ext = {"timeout": {"connect": 10.0, "read": timeout, "write": timeout, "pool": 10.0}}
            with pool.stream("GET", current, headers=headers, extensions=ext) as resp:
                hdrs = {k.decode().lower(): v.decode(errors="replace") for k, v in resp.headers}
                if resp.status in (301, 302, 303, 307, 308):
                    loc = hdrs.get("location")
                    if not loc:
                        raise FetchError("Omdirigering utan mål.", status=resp.status)
                    current = revalidate_redirect(urljoin(current, loc), allowed_hosts=allowed_hosts)
                    continue
                if resp.status == 429 or resp.status >= 500:
                    raise FetchError(
                        f"Servern svarade {resp.status}.",
                        status=resp.status,
                        retry_after=_parse_retry_after(hdrs.get("retry-after")),
                    )
                if resp.status >= 400:
                    raise FetchError(f"Servern svarade {resp.status}.", status=resp.status)
                declared = hdrs.get("content-length")
                total = int(declared) if declared and declared.isdigit() else None
                if total is not None and total > max_bytes:
                    raise FetchError("Filen är större än tillåten maxstorlek.", status=413)
                written = 0
                for chunk in resp.iter_stream():
                    written += len(chunk)
                    if written > max_bytes:
                        raise FetchError("Filen är större än tillåten maxstorlek.", status=413)
                    sink(chunk)
                    if on_progress:
                        on_progress(written, total)
                    if should_cancel and should_cancel():
                        raise FetchError("Avbruten.")
                return FetchResult(current, resp.status, hdrs, written)
        raise FetchError("För många omdirigeringar.")


def fetch_bytes(url: str, *, max_bytes: int, allowed_hosts: list[str] | None, accept: str = "*/*") -> bytes:
    buf = bytearray()
    guarded_stream(url, sink=buf.extend, max_bytes=max_bytes, allowed_hosts=allowed_hosts, accept=accept)
    return bytes(buf)


def fetch_to_file(url: str, dest: Path, **kwargs: Any) -> FetchResult:
    with dest.open("wb") as fh:
        return guarded_stream(url, sink=fh.write, **kwargs)
