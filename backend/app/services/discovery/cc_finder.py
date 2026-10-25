"""Finds Creative Commons podcasts, talks and finished livestream recordings on YouTube.

What this does and does not do:

* It uses the official Data API (``search.list`` with ``videoLicense=creativeCommon``, then
  ``videos.list``). It never scrapes and never downloads anything.
* The license is **verified twice**: the search filter asks for it, and the follow-up ``videos.list``
  must report ``status.license == "creativeCommon"`` for every single video, otherwise it is dropped.
  Only verified videos are returned, which is what allows the source to carry the ``licensed`` rights
  status. YouTube's "Creative Commons" license is CC BY: reuse and editing are allowed with
  attribution, which ClipForge puts in the description (creator, title and link).
* Videos are scored with a transparent heuristic (see :func:`score_video`). It ranks; it does not
  predict views.
* Quota: each topic costs one ``search.list`` call (own bucket, 100/day) plus one ``videos.list``
  (1 unit) per check. The adapter is handed a budget and never exceeds it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from app.core.errors import ErrorCode, JobError, permanent, transient
from app.core.security import clean_text
from app.db.models import Source, SourceKind
from app.services.discovery.base import Capabilities, DiscoveredItem, DiscoveryResult

YT_API = "https://www.googleapis.com/youtube/v3"
LICENSE = "creativeCommon"
SEEN_LIMIT = 600
_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_DURATION = re.compile(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")
# Category ids that suit talk-style content (People & Blogs, Education, News & Politics, Science & Tech,
# Entertainment, Nonprofits & Activism, Howto & Style).
_TALK_CATEGORIES = {"22", "27", "25", "28", "24", "29", "26"}
_FORMAT_WORDS = re.compile(
    r"(?i)\b(podcast|podd|avsnitt|episod|episode|intervju|interview|samtal|conversation|talk|panel|debat"
    r"|debate|föreläsning|lecture|q&a|ama|livestream|live|sändning|stream)\b"
)


class CcSearchConfig(BaseModel):
    """Per-source options (stored in ``Source.config_json``). Topics are ``Source.keywords_json``."""

    min_minutes: int = Field(default=20, ge=5, le=600)
    max_minutes: int = Field(default=120, ge=10, le=600)
    language: Literal["any", "sv", "en"] = "any"
    min_score: float = Field(default=45.0, ge=0, le=100)
    max_new_per_check: int = Field(default=3, ge=1, le=20)
    queries_per_check: int = Field(default=2, ge=1, le=5)
    lookback_days: int = Field(default=60, ge=1, le=3650)
    min_views: int = Field(default=0, ge=0)
    max_backlog: int = Field(default=8, ge=1, le=100)  # stop adding while this many videos await processing


@dataclass(frozen=True)
class Scored:
    score: float
    reasons: list[str]


def parse_duration(iso: str | None) -> float | None:
    """ISO-8601 ``PT1H2M3S`` -> seconds. Livestreams in progress report ``P0D``."""
    m = _DURATION.match(iso or "")
    if not m or not any(m.groups()):
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    total = d * 86400 + h * 3600 + mi * 60 + s
    return float(total) or None


def score_video(info: dict[str, Any], cfg: CcSearchConfig, topic: str, now: datetime) -> Scored:
    """0-100, six transparent parts. ``info`` is a ``videos.list`` item plus ``duration`` seconds."""
    sn = info.get("snippet") or {}
    stats = info.get("statistics") or {}
    reasons: list[str] = []
    minutes = (info.get("duration") or 0) / 60

    # Duration (20): full marks for 30-90 minutes, tapering to the configured limits.
    if 30 <= minutes <= 90:
        dur = 20.0
    elif minutes < 30:
        dur = 20.0 * max(0.0, (minutes - cfg.min_minutes) / max(1.0, 30 - cfg.min_minutes))
    else:
        dur = 20.0 * max(0.0, (cfg.max_minutes - minutes) / max(1.0, cfg.max_minutes - 90))
    dur = max(8.0 if cfg.min_minutes <= minutes <= cfg.max_minutes else 0.0, dur)
    reasons.append(f"längd {minutes:.0f} min (+{dur:.0f})")

    # Format (25): looks like a podcast, conversation, talk or finished livestream.
    text = f"{sn.get('title', '')} {' '.join(sn.get('tags') or [])}"
    fmt = 0.0
    if _FORMAT_WORDS.search(text):
        fmt += 15
    if sn.get("categoryId") in _TALK_CATEGORIES:
        fmt += 5
    if info.get("liveStreamingDetails"):
        fmt += 5
        reasons.append("inspelad livesändning")
    fmt = min(25.0, fmt)
    if fmt:
        reasons.append(f"samtals-/talkformat (+{fmt:.0f})")

    # Engagement (20): log-scaled views plus like ratio. Hidden statistics get a neutral score.
    views = int(stats["viewCount"]) if str(stats.get("viewCount", "")).isdigit() else None
    likes = int(stats["likeCount"]) if str(stats.get("likeCount", "")).isdigit() else None
    if views is None:
        eng = 8.0
        reasons.append("statistik dold (neutral)")
    else:
        eng = 14.0 * min(1.0, math.log10(max(views, 1)) / 5)  # 100 000 views = full
        if likes is not None and views >= 100:
            eng += 6.0 * min(1.0, (likes / views) / 0.04)  # 4 % likes = full
        reasons.append(f"{views:,} visningar (+{eng:.0f})".replace(",", " "))

    # Recency (10).
    published = _parse(sn.get("publishedAt"))
    age_days = (now - published).days if published else cfg.lookback_days
    rec = 10.0 * max(0.0, 1 - age_days / max(cfg.lookback_days, 1))
    reasons.append(f"{age_days} dagar gammal (+{rec:.0f})")

    # Language (10).
    lang = (sn.get("defaultAudioLanguage") or sn.get("defaultLanguage") or "").lower()
    if cfg.language == "any":
        lng = 6.0
    elif lang.startswith(cfg.language):
        lng = 10.0
        reasons.append(f"språk {cfg.language}")
    elif not lang:
        lng = 4.0
    else:
        lng = 0.0
        reasons.append(f"annat språk ({lang})")

    # Topic (15): how many of the topic's words appear in the title/description.
    words = [w for w in re.split(r"\W+", topic.lower()) if len(w) > 2]
    hay = f"{sn.get('title', '')} {sn.get('description', '')}".lower()
    top = 15.0 * (sum(1 for w in words if w in hay) / len(words)) if words else 7.0
    reasons.append(f"ämne '{topic}' (+{top:.0f})")

    total = round(min(100.0, dur + fmt + eng + rec + lng + top), 1)
    return Scored(total, reasons)


def _parse(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


class YouTubeCcSearchAdapter:
    kind = SourceKind.YOUTUBE_CC_SEARCH

    def __init__(
        self,
        api_key: str | None,
        *,
        client: httpx.Client | None = None,
        search_budget: int = 0,
        unit_budget: int = 0,
        backlog: int = 0,
        max_input_minutes: int = 180,
        now: datetime | None = None,
    ) -> None:
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=20)
        self.search_budget = search_budget
        self.unit_budget = unit_budget
        self.backlog = backlog
        self.max_input_minutes = max_input_minutes
        self.now = now

    def capabilities(self) -> Capabilities:
        return Capabilities(
            True,
            False,
            True,
            "Hittar bara videor med YouTubes Creative Commons-licens (CC BY) via officiella API:t. "
            "Licensen kontrolleras per video. Upphovspersonen anges i beskrivningen.",
        )

    def validate_config(self, source: Source) -> str | None:
        if not self.api_key:
            return "YouTube API-nyckel saknas (CLIPFORGE_YOUTUBE_API_KEY i .env)."
        if not source.keywords_json:
            return "Ange minst ett ämne att söka efter."
        try:
            CcSearchConfig.model_validate(source.config_json or {})
        except ValueError:
            return "Ogiltiga sökinställningar."
        return None

    # ---------------------------------------------------------------- HTTP
    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
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
            raise transient(ErrorCode.RATE_LIMITED, "YouTube API begränsar anrop.", retry_after=600)
        if r.status_code >= 500:
            raise transient(ErrorCode.NETWORK_TIMEOUT, f"YouTube API-fel {r.status_code}.")
        if r.status_code >= 400:
            raise permanent(ErrorCode.INVALID_INPUT, f"YouTube API avvisade anropet ({r.status_code}).")
        data = r.json()
        return data if isinstance(data, dict) else {}

    # ---------------------------------------------------------------- discovery
    def discover(self, source: Source) -> DiscoveryResult:
        err = self.validate_config(source)
        if err:
            raise JobError(ErrorCode.INVALID_INPUT, err, transient=False, block=True)
        cfg = CcSearchConfig.model_validate(source.config_json or {})
        cursor = dict(source.cursor_json or {})
        notes: list[str] = []
        if self.backlog >= cfg.max_backlog:
            return DiscoveryResult(
                [], cursor=cursor, notes=[f"Kön har redan {self.backlog} videor att bearbeta; väntar."]
            )
        topics = list(source.keywords_json)
        start = int(cursor.get("next_topic", 0)) % len(topics)
        n_queries = min(cfg.queries_per_check, len(topics), self.search_budget)
        if n_queries <= 0:
            return DiscoveryResult(
                [], cursor=cursor, notes=["Dagens sökgräns för YouTube är nådd; fortsätter i morgon."]
            )
        now = self.now or datetime.now(UTC)
        seen: list[str] = list(cursor.get("seen") or [])
        seen_set = set(seen)
        after = (now - timedelta(days=cfg.lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        picked: dict[str, str] = {}  # video id -> topic that found it
        searches = 0
        for i in range(n_queries):
            topic = topics[(start + i) % len(topics)]
            data = self._get(
                "search",
                {
                    "part": "snippet",
                    "type": "video",
                    "q": topic,
                    "videoLicense": LICENSE,
                    "videoDuration": "long" if cfg.min_minutes >= 20 else "any",
                    "order": "relevance",
                    "publishedAfter": after,
                    "maxResults": "25",
                    **({"relevanceLanguage": cfg.language} if cfg.language != "any" else {}),
                },
            )
            searches += 1
            for it in data.get("items") or []:
                vid = (it.get("id") or {}).get("videoId")
                if isinstance(vid, str) and _ID.match(vid) and vid not in seen_set:
                    picked.setdefault(vid, topic)
        cursor["next_topic"] = (start + n_queries) % len(topics)
        if not picked or self.unit_budget < 1:
            return DiscoveryResult([], cursor=cursor, notes=notes, quota_units=0, search_calls=searches)

        details: dict[str, dict[str, Any]] = {}
        ids = list(picked)[:50]
        data = self._get(
            "videos",
            {
                "part": "snippet,contentDetails,status,statistics,liveStreamingDetails",
                "id": ",".join(ids),
                "maxResults": "50",
            },
        )
        for it in data.get("items") or []:
            if isinstance(it, dict) and it.get("id"):
                details[str(it["id"])] = it
        seen = ([*ids, *seen])[:SEEN_LIMIT]  # judged once; never re-fetched or re-scored

        max_minutes = min(cfg.max_minutes, self.max_input_minutes)
        scored: list[tuple[float, DiscoveredItem]] = []
        dropped: dict[str, int] = {}

        def drop(why: str) -> None:
            dropped[why] = dropped.get(why, 0) + 1

        for vid in ids:
            info = details.get(vid)
            if info is None:
                drop("saknas")
                continue
            st = info.get("status") or {}
            sn = info.get("snippet") or {}
            # Deterministic gates. The license check is the one that grants the 'licensed' rights.
            if st.get("license") != LICENSE:
                drop("ej Creative Commons")
                continue
            if st.get("privacyStatus") != "public" or st.get("uploadStatus") != "processed":
                drop("ej publik/klar")
                continue
            if st.get("madeForKids"):
                drop("barninnehåll")
                continue
            if sn.get("liveBroadcastContent", "none") != "none":
                drop("pågående/kommande sändning")
                continue
            secs = parse_duration((info.get("contentDetails") or {}).get("duration"))
            if secs is None or not (cfg.min_minutes * 60 <= secs <= max_minutes * 60):
                drop("fel längd")
                continue
            stats = info.get("statistics") or {}
            if (
                cfg.min_views
                and str(stats.get("viewCount", "0")).isdigit()
                and int(stats["viewCount"]) < cfg.min_views
            ):
                drop("för få visningar")
                continue
            sc = score_video({**info, "duration": secs}, cfg, picked[vid], now)
            if sc.score < cfg.min_score:
                drop("under poänggränsen")
                continue
            thumbs = sn.get("thumbnails") or {}
            thumb = (thumbs.get("high") or thumbs.get("default") or {}).get("url")
            scored.append(
                (
                    sc.score,
                    DiscoveredItem(
                        canonical_url=f"https://www.youtube.com/watch?v={vid}",
                        platform="youtube",
                        external_id=vid,
                        title=clean_text(sn.get("title", ""), 500) or None,
                        creator_name=clean_text(sn.get("channelTitle", ""), 300) or None,
                        creator_channel_id=sn.get("channelId"),
                        description=clean_text(sn.get("description", ""), 5000) or None,
                        published_at=_parse(sn.get("publishedAt")),
                        thumbnail_url=thumb if isinstance(thumb, str) else None,
                        duration_seconds=secs,
                        metadata={
                            "finder": {
                                "score": sc.score,
                                "reasons": sc.reasons,
                                "topic": picked[vid],
                                "license": LICENSE,
                                "license_verified_at": now.isoformat(),
                                "is_live_recording": bool(info.get("liveStreamingDetails")),
                            },
                            "license": LICENSE,
                        },
                    ),
                )
            )
        scored.sort(key=lambda x: x[0], reverse=True)
        items = [it for _s, it in scored[: cfg.max_new_per_check]]
        if dropped:
            notes.append("Sållade bort: " + ", ".join(f"{n} {why}" for why, n in sorted(dropped.items())))
        cursor["seen"] = seen
        return DiscoveryResult(items, cursor=cursor, notes=notes, quota_units=1, search_calls=searches)
