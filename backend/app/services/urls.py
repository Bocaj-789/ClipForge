"""URL validation, normalization and SSRF policy.

Every user-supplied URL passes through :func:`classify_url` before it is stored or fetched.
Only https on the default port with a public DNS hostname is accepted; IP literals,
credentials, localhost-style names and unknown hosts are rejected.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

MAX_URL_LENGTH = 2048

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtube-nocookie.com",
    "youtube-nocookie.com",
}
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_HANDLE = re.compile(r"^@[A-Za-z0-9._-]{3,30}$")
_BLOCKED_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".arpa", ".onion")
_HOST_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


class UrlKind(StrEnum):
    YOUTUBE_VIDEO = "youtube_video"
    YOUTUBE_CHANNEL = "youtube_channel"
    DIRECT_MEDIA = "direct_media"
    FEED = "feed"


class UrlRejected(ValueError):
    """Raised with a safe, user-facing (Swedish) reason."""


@dataclass(frozen=True)
class ClassifiedUrl:
    kind: UrlKind
    canonical_url: str
    platform: str
    external_id: str | None
    host: str


def _validate_host(host: str | None) -> str:
    if not host:
        raise UrlRejected("Adressen saknar värdnamn.")
    host = host.lower().rstrip(".")
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        raise UrlRejected("IP-adresser accepteras inte, använd ett publikt domännamn.")
    if host == "localhost" or host.endswith(_BLOCKED_SUFFIXES) or "." not in host:
        raise UrlRejected("Lokala eller interna värdnamn accepteras inte.")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as e:
        raise UrlRejected("Ogiltigt värdnamn.") from e
    if len(ascii_host) > 253 or not all(_HOST_LABEL.match(lbl) for lbl in ascii_host.split(".")):
        raise UrlRejected("Ogiltigt värdnamn.")
    return ascii_host


def _base_checks(raw: str) -> tuple[str, str, str, str]:
    url = raw.strip()
    if not url:
        raise UrlRejected("Adressen är tom.")
    if len(url) > MAX_URL_LENGTH:
        raise UrlRejected("Adressen är för lång.")
    if any(c.isspace() or ord(c) < 32 for c in url):
        raise UrlRejected("Adressen innehåller otillåtna tecken.")
    if "://" not in url and url.lower().startswith(
        ("youtube.com", "www.youtube.com", "youtu.be", "m.youtube")
    ):
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        raise UrlRejected("Endast https-adresser stöds.")
    if parts.username or parts.password:
        raise UrlRejected("Adresser med inloggningsuppgifter accepteras inte.")
    try:
        port = parts.port
    except ValueError as e:
        raise UrlRejected("Ogiltig port.") from e
    if port not in (None, 443):
        raise UrlRejected("Endast standardporten för https stöds.")
    host = _validate_host(parts.hostname)
    return host, parts.path or "/", parts.query, url


def _youtube(host: str, path: str, query: str) -> ClassifiedUrl:
    q = parse_qs(query)
    vid: str | None = None
    segs = [s for s in path.split("/") if s]
    if host == "youtu.be":
        vid = segs[0] if segs else None
    elif segs and segs[0] == "watch":
        vid = (q.get("v") or [""])[0] or None
    elif len(segs) >= 2 and segs[0] in {"shorts", "live", "embed", "v", "e"}:
        vid = segs[1]
    elif len(segs) >= 2 and segs[0] == "channel" and _CHANNEL_ID.match(segs[1]):
        cid = segs[1]
        return ClassifiedUrl(
            UrlKind.YOUTUBE_CHANNEL, f"https://www.youtube.com/channel/{cid}", "youtube", cid, host
        )
    elif segs and _HANDLE.match(segs[0]):
        handle = segs[0]
        return ClassifiedUrl(
            UrlKind.YOUTUBE_CHANNEL, f"https://www.youtube.com/{handle}", "youtube", handle, host
        )
    if not vid or not _VIDEO_ID.match(vid):
        raise UrlRejected("Kunde inte hitta ett giltigt YouTube-video-ID i adressen.")
    return ClassifiedUrl(
        UrlKind.YOUTUBE_VIDEO, f"https://www.youtube.com/watch?v={vid}", "youtube", vid, host
    )


def host_allowed(host: str, allowlist: list[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == a or host.endswith("." + a) for a in allowlist)


def classify_url(raw: str, *, direct_media_hosts: list[str], allow_feed: bool = False) -> ClassifiedUrl:
    """Validate and normalise a user-supplied URL. Raises :class:`UrlRejected`."""
    host, path, query, url = _base_checks(raw)
    if host in YOUTUBE_HOSTS:
        return _youtube(host, path, query)
    parts = urlsplit(url)
    # Drop fragments and sort the query so equivalent URLs dedupe.
    clean_query = urlencode(sorted(parse_qs(parts.query, keep_blank_values=True).items()), doseq=True)
    canonical = urlunsplit(("https", host, parts.path or "/", clean_query, ""))
    if allow_feed:
        return ClassifiedUrl(UrlKind.FEED, canonical, "feed", None, host)
    if host_allowed(host, direct_media_hosts):
        return ClassifiedUrl(UrlKind.DIRECT_MEDIA, canonical, "direct", None, host)
    raise UrlRejected(
        "Värden stöds inte. Stödda källor: YouTube-länkar (metadata) och tillåtna mediavärdar "
        "under Inställningar."
    )


def revalidate_redirect(location: str, *, allowed_hosts: list[str] | None) -> str:
    """Validate a redirect target with the same policy as the original URL."""
    host, _path, _q, url = _base_checks(location)
    if allowed_hosts is not None and not host_allowed(host, allowed_hosts):
        raise UrlRejected("Omdirigering till en otillåten värd stoppades.")
    return url


def ip_is_public(addr: str) -> bool:
    ip = ipaddress.ip_address(addr)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return bool(
        ip.is_global
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_private
    )
