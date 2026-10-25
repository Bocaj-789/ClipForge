"""Discovery adapters: RSS/Atom feeds, YouTube Data API (uploads playlist), local folder."""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET  # noqa: S405 - DTDs are rejected before parsing
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from app.config import Config
from app.core.errors import ErrorCode, JobError, permanent, transient
from app.core.security import clean_text
from app.db.models import Source, SourceKind
from app.services.acquisition.local_file import ALLOWED_EXT, sha256_path
from app.services.discovery.base import Capabilities, DiscoveredItem, DiscoveryResult
from app.services.safe_fetch import FetchError, fetch_bytes
from app.services.urls import UrlKind, UrlRejected, classify_url

log = logging.getLogger("clipforge.discovery")
MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_ITEMS = 50

YT_API = "https://www.googleapis.com/youtube/v3"


def _parse_dt(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        try:
            return parsedate_to_datetime(v).astimezone(UTC)
        except (TypeError, ValueError):
            return None


def _safe_xml(data: bytes) -> ET.Element:
    # Reject DTDs outright: prevents entity-expansion (billion laughs) and external entities.
    head = data[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in data.lower():
        raise permanent(ErrorCode.INVALID_INPUT, "Flödet innehåller DTD/entiteter och avvisades.")
    try:
        return ET.fromstring(data)  # noqa: S314
    except ET.ParseError as e:
        raise permanent(ErrorCode.INVALID_INPUT, "Flödet kunde inte tolkas som RSS/Atom.") from e


_NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


class RssAdapter:
    kind = SourceKind.RSS

    def __init__(self, direct_media_hosts: list[str]) -> None:
        self.direct_media_hosts = direct_media_hosts

    def capabilities(self) -> Capabilities:
        return Capabilities(
            True,
            False,
            False,
            "Läser endast flödesmetadata. Media hämtas bara från tillåtna värdar med bekräftade rättigheter.",
        )

    def validate_config(self, source: Source) -> str | None:
        try:
            classify_url(source.url or "", direct_media_hosts=[], allow_feed=True)
        except UrlRejected as e:
            return str(e)
        return None

    def _item_url(self, link: str | None) -> tuple[str, str, str | None] | None:
        if not link:
            return None
        try:
            c = classify_url(link, direct_media_hosts=self.direct_media_hosts)
        except UrlRejected:
            return None
        if c.kind not in (UrlKind.YOUTUBE_VIDEO, UrlKind.DIRECT_MEDIA):
            return None
        return c.canonical_url, c.platform, c.external_id

    def discover(self, source: Source) -> DiscoveryResult:
        url = source.url or ""
        try:
            raw = fetch_bytes(
                url,
                max_bytes=MAX_FEED_BYTES,
                allowed_hosts=None,
                accept="application/atom+xml, application/rss+xml, application/xml, text/xml",
            )
        except FetchError as e:
            if e.status in (429, 500, 502, 503, 504) or e.status is None:
                raise transient(ErrorCode.NETWORK_TIMEOUT, str(e), retry_after=e.retry_after) from e
            raise permanent(ErrorCode.INVALID_INPUT, str(e)) from e
        except UrlRejected as e:
            raise permanent(ErrorCode.UNSUPPORTED_URL, str(e)) from e
        root = _safe_xml(raw)
        items: list[DiscoveredItem] = []
        if root.tag.endswith("feed"):  # Atom (incl. YouTube channel feeds)
            for entry in root.findall("a:entry", _NS)[:MAX_ITEMS]:
                link_el = entry.find("a:link[@rel='alternate']", _NS)
                if link_el is None:
                    link_el = entry.find("a:link", _NS)
                parsed = self._item_url(link_el.get("href") if link_el is not None else None)
                if not parsed:
                    continue
                desc = entry.find("media:group/media:description", _NS)
                thumb = entry.find("media:group/media:thumbnail", _NS)
                items.append(
                    DiscoveredItem(
                        canonical_url=parsed[0],
                        platform=parsed[1],
                        external_id=parsed[2],
                        title=clean_text(entry.findtext("a:title", "", _NS), 500) or None,
                        creator_name=clean_text(entry.findtext("a:author/a:name", "", _NS), 300) or None,
                        creator_channel_id=entry.findtext("yt:channelId", None, _NS),
                        description=clean_text(desc.text or "", 5000) if desc is not None else None,
                        published_at=_parse_dt(entry.findtext("a:published", None, _NS)),
                        thumbnail_url=thumb.get("url") if thumb is not None else None,
                    )
                )
        else:  # RSS 2.0
            for it in root.findall("./channel/item")[:MAX_ITEMS]:
                enc = it.find("enclosure")
                link = enc.get("url") if enc is not None else it.findtext("link")
                parsed = self._item_url(link)
                if not parsed:
                    continue
                items.append(
                    DiscoveredItem(
                        canonical_url=parsed[0],
                        platform=parsed[1],
                        external_id=parsed[2],
                        title=clean_text(it.findtext("title", ""), 500) or None,
                        creator_name=clean_text(root.findtext("./channel/title", ""), 300) or None,
                        description=clean_text(it.findtext("description", ""), 5000) or None,
                        published_at=_parse_dt(it.findtext("pubDate")),
                    )
                )
        return DiscoveryResult(items)


class YouTubeApiAdapter:
    """Official YouTube Data API v3: channels.list -> uploads playlist -> playlistItems.list.

    channels.list and playlistItems.list cost 1 unit each from the shared 10,000 units/day
    bucket (official quota table, checked 2026-10-25, page updated 2026-10-15). search.list has
    its own bucket of only 100 calls/day, so it is deliberately not used for routine polling.
    """

    kind = SourceKind.YOUTUBE_CHANNEL

    def __init__(self, api_key: str | None, client: httpx.Client | None = None) -> None:
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=20)

    def capabilities(self) -> Capabilities:
        return Capabilities(
            True,
            False,
            True,
            "Endast metadata via officiellt API inom gratis kvot. YouTube-videor kan inte laddas "
            "ned via API:t.",
        )

    def validate_config(self, source: Source) -> str | None:
        if not self.api_key:
            return "YouTube API-nyckel saknas (CLIPFORGE_YOUTUBE_API_KEY i .env)."
        try:
            c = classify_url(source.url or "", direct_media_hosts=[])
        except UrlRejected as e:
            return str(e)
        if c.kind != UrlKind.YOUTUBE_CHANNEL:
            return "Ange en kanaladress (youtube.com/channel/UC… eller youtube.com/@namn)."
        return None

    def _get(self, path: str, params: dict[str, str]) -> dict:
        try:
            r = self.client.get(f"{YT_API}/{path}", params={**params, "key": self.api_key or ""})
        except httpx.TimeoutException as e:
            raise transient(ErrorCode.NETWORK_TIMEOUT, "YouTube API svarade inte i tid.") from e
        except httpx.HTTPError as e:
            raise transient(ErrorCode.NETWORK_TIMEOUT, "Nätverksfel mot YouTube API.") from e
        if r.status_code == 403 and "quota" in r.text.lower():
            raise JobError(
                ErrorCode.API_QUOTA_EXCEEDED,
                "YouTube API-kvoten är slut för idag.",
                transient=True,
                retry_after=6 * 3600,
            )
        if r.status_code == 429:
            ra = r.headers.get("retry-after")
            raise transient(
                ErrorCode.RATE_LIMITED,
                "YouTube API begränsar anrop.",
                retry_after=float(ra) if ra and ra.isdigit() else 600,
            )
        if r.status_code >= 500:
            raise transient(ErrorCode.NETWORK_TIMEOUT, f"YouTube API-fel {r.status_code}.")
        if r.status_code >= 400:
            raise permanent(ErrorCode.INVALID_INPUT, f"YouTube API avvisade anropet ({r.status_code}).")
        return r.json()

    def discover(self, source: Source) -> DiscoveryResult:
        err = self.validate_config(source)
        if err:
            raise JobError(ErrorCode.INVALID_INPUT, err, transient=False, block=True)
        c = classify_url(source.url or "", direct_media_hosts=[])
        cursor = dict(source.cursor_json or {})
        units = 0
        uploads = cursor.get("uploads_playlist")
        if not uploads:
            params = {"part": "contentDetails,snippet"}
            ext = c.external_id or ""
            if ext.startswith("@"):
                params["forHandle"] = ext
            else:
                params["id"] = ext
            data = self._get("channels", params)
            units += 1
            items = data.get("items") or []
            if not items:
                raise permanent(ErrorCode.INVALID_INPUT, "Kanalen hittades inte via YouTube API.")
            uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
            cursor["uploads_playlist"] = uploads
            cursor["channel_id"] = items[0]["id"]
        data = self._get(
            "playlistItems", {"part": "snippet,contentDetails", "playlistId": uploads, "maxResults": "25"}
        )
        units += 1
        seen_latest = cursor.get("latest_published")
        out: list[DiscoveredItem] = []
        newest = seen_latest
        for it in data.get("items") or []:
            sn = it.get("snippet") or {}
            vid = (it.get("contentDetails") or {}).get("videoId") or (sn.get("resourceId") or {}).get(
                "videoId"
            )
            if not vid or not re.match(r"^[A-Za-z0-9_-]{11}$", vid):
                continue
            pub = (it.get("contentDetails") or {}).get("videoPublishedAt") or sn.get("publishedAt")
            if seen_latest and pub and pub <= seen_latest:
                continue
            newest = max(newest or "", pub or "")
            thumbs = sn.get("thumbnails") or {}
            thumb = (thumbs.get("high") or thumbs.get("default") or {}).get("url")
            out.append(
                DiscoveredItem(
                    canonical_url=f"https://www.youtube.com/watch?v={vid}",
                    platform="youtube",
                    external_id=vid,
                    title=clean_text(sn.get("title", ""), 500) or None,
                    creator_name=clean_text(sn.get("channelTitle", ""), 300) or None,
                    creator_channel_id=sn.get("channelId"),
                    description=clean_text(sn.get("description", ""), 5000) or None,
                    published_at=_parse_dt(pub),
                    thumbnail_url=thumb,
                )
            )
        if newest:
            cursor["latest_published"] = newest
        return DiscoveryResult(out, cursor=cursor, quota_units=units)


class LocalFolderAdapter:
    """Picks up media files dropped into data/import. Files are referenced in place, never deleted."""

    kind = SourceKind.LOCAL_FOLDER

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def capabilities(self) -> Capabilities:
        return Capabilities(True, True, False, "Endast filer i ClipForges importmapp.")

    def validate_config(self, source: Source) -> str | None:
        return None

    def discover(self, source: Source) -> DiscoveryResult:
        base = self.cfg.import_dir
        base.mkdir(parents=True, exist_ok=True)
        known: dict[str, str] = dict((source.cursor_json or {}).get("files", {}))
        out: list[DiscoveredItem] = []
        for p in sorted(base.iterdir()):
            if p.is_symlink() or not p.is_file() or p.suffix.lstrip(".").lower() not in ALLOWED_EXT:
                continue
            st = p.stat()
            sig = f"{st.st_size}:{int(st.st_mtime)}"
            if known.get(p.name) == sig:
                continue
            if time_since(st.st_mtime) < 30:  # still being copied
                continue
            checksum = sha256_path(p)
            known[p.name] = sig
            out.append(
                DiscoveredItem(
                    canonical_url=f"local:{checksum}",
                    platform="local",
                    external_id=checksum[:16],
                    title=clean_text(p.stem, 300),
                    media_rel_path=str(p.relative_to(self.cfg.data_dir)),
                    media_checksum=checksum,
                )
            )
        return DiscoveryResult(out, cursor={"files": known})


def time_since(mtime: float) -> float:
    return time.time() - mtime
