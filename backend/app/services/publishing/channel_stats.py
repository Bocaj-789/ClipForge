"""Read-only channel statistics from the YouTube Data API.

Per channel: subscribers, total channel views and video count (channels.list), plus the totals of the
videos ClipForge uploaded to that channel (videos.list). Each read costs one quota unit per call.
"""

from __future__ import annotations

from typing import Any

from app.services.publishing.youtube import YouTubeApi

SUM_FIELDS = (
    "channel_views",
    "video_count",
    "clipforge_videos",
    "clipforge_views",
    "clipforge_likes",
    "clipforge_comments",
)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _empty(channel_id: str, name: str, error: str | None) -> dict[str, Any]:
    return {
        "channel_id": channel_id,
        "name": name,
        "error": error,
        "subscribers": None,
        "subscribers_hidden": False,
        **{f: 0 for f in SUM_FIELDS},
    }


def failed(channel_id: str, name: str, error: str) -> dict[str, Any]:
    """A channel whose statistics could not be read. The other channels are still shown."""
    return _empty(channel_id, name, error)


def channel_snapshot(api: YouTubeApi, *, channel_id: str, name: str, video_ids: list[str]) -> dict[str, Any]:
    """Statistics for one authorised channel. Raises YouTubeError if the channel itself cannot be read."""
    data = api.get_json("channels", {"part": "statistics", "mine": "true"})
    items = data.get("items") or []
    if not items:
        return _empty(channel_id, name, "YouTube hittade ingen kanal för den här anslutningen.")
    stats = items[0].get("statistics") or {}
    out = _empty(channel_id, name, None)
    out["subscribers"] = _int(stats.get("subscriberCount"))
    out["subscribers_hidden"] = bool(stats.get("hiddenSubscriberCount"))
    out["channel_views"] = _int(stats.get("viewCount")) or 0
    out["video_count"] = _int(stats.get("videoCount")) or 0

    # Only videos that still exist on YouTube are counted, so deleted uploads drop out on their own.
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        data = api.get_json("videos", {"part": "statistics", "id": ",".join(batch)})
        for video in data.get("items") or []:
            if not isinstance(video, dict):
                continue
            st = video.get("statistics") or {}
            out["clipforge_videos"] += 1
            out["clipforge_views"] += _int(st.get("viewCount")) or 0
            out["clipforge_likes"] += _int(st.get("likeCount")) or 0
            out["clipforge_comments"] += _int(st.get("commentCount")) or 0
    return out


def totals(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Sums over the channels that could be read. Subscribers are None when no channel shows a count."""
    ok = [i for i in items if i["error"] is None]
    subs = [i["subscribers"] for i in ok if i["subscribers"] is not None]
    out: dict[str, Any] = {
        "channels": len(ok),
        "subscribers": sum(subs) if subs else None,
        "subscribers_hidden": any(i["subscribers_hidden"] for i in ok),
    }
    for f in SUM_FIELDS:
        out[f] = sum(i[f] for i in ok)
    return out
