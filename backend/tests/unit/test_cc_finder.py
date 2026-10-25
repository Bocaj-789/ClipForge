from __future__ import annotations

from datetime import UTC, datetime

import httpx

from app.db.models import Source
from app.services.discovery.cc_finder import (
    CcSearchConfig,
    YouTubeCcSearchAdapter,
    parse_duration,
    score_video,
)

NOW = datetime(2026, 10, 25, tzinfo=UTC)


def _video(vid: str, *, license_: str = "creativeCommon", duration: str = "PT40M", **over) -> dict:
    item = {
        "id": vid,
        "snippet": {
            "title": f"Podcast avsnitt {vid}",
            "description": "Ett samtal om sparande.",
            "tags": ["podcast"],
            "categoryId": "27",
            "publishedAt": "2026-10-20T10:00:00Z",
            "channelTitle": "Skaparen",
            "channelId": "UC_test",
            "defaultAudioLanguage": "sv",
            "thumbnails": {"high": {"url": "https://i.ytimg.com/x.jpg"}},
            "liveBroadcastContent": "none",
        },
        "status": {"license": license_, "privacyStatus": "public", "uploadStatus": "processed"},
        "contentDetails": {"duration": duration},
        "statistics": {"viewCount": "5000", "likeCount": "300"},
    }
    item.update(over)
    return item


def _source(topics: list[str] | None = None) -> Source:
    return Source(
        kind="youtube_cc_search",
        name="CC",
        url=None,
        keywords_json=["podcast"] if topics is None else topics,
        config_json=CcSearchConfig().model_dump(),
        cursor_json={},
        rights_status="licensed",
    )


def _adapter(handler, **kw) -> YouTubeCcSearchAdapter:  # type: ignore[no-untyped-def]
    defaults = dict(search_budget=5, unit_budget=100, backlog=0, now=NOW)
    defaults.update(kw)
    return YouTubeCcSearchAdapter(
        "test-key", client=httpx.Client(transport=httpx.MockTransport(handler)), **defaults
    )


def test_parse_duration() -> None:
    assert parse_duration("PT1H2M3S") == 3723.0
    assert parse_duration("PT40M") == 2400.0
    assert parse_duration("P0D") is None  # livestream still running reports no duration
    assert parse_duration(None) is None


def test_score_rewards_talk_length_and_topic() -> None:
    cfg = CcSearchConfig()
    good = score_video({**_video("aaaaaaaaaaa"), "duration": 2400}, cfg, "podcast", NOW)
    weak = score_video({**_video("bbbbbbbbbbb"), "duration": 2400}, cfg, "rymdteknik", NOW)
    assert good.score > weak.score
    assert good.score >= cfg.min_score
    assert any("ämne" in r for r in good.reasons)


def test_only_verified_creative_commons_videos_are_returned() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path.endswith("/search"):
            assert request.url.params["videoLicense"] == "creativeCommon"
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": {"videoId": v}}
                        for v in ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc", "ddddddddddd"]
                    ]
                },
            )
        assert request.url.path.endswith("/videos")
        return httpx.Response(
            200,
            json={
                "items": [
                    _video("aaaaaaaaaaa"),  # good
                    _video("bbbbbbbbbbb", license_="youtube"),  # standard license: dropped
                    _video("ccccccccccc", duration="PT5M"),  # too short
                    _video(
                        "ddddddddddd",
                        status={
                            "license": "creativeCommon",
                            "privacyStatus": "unlisted",
                            "uploadStatus": "processed",
                        },
                    ),  # not public: dropped
                ]
            },
        )

    result = _adapter(handler).discover(_source())
    assert [i.external_id for i in result.items] == ["aaaaaaaaaaa"]
    item = result.items[0]
    assert item.metadata["license"] == "creativeCommon"
    assert item.metadata["finder"]["topic"] == "podcast"
    assert item.duration_seconds == 2400
    assert result.search_calls == 1 and result.quota_units == 1
    assert "Sållade bort" in " ".join(result.notes)


def test_already_seen_videos_are_not_fetched_again() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"items": [{"id": {"videoId": "aaaaaaaaaaa"}}]})
        raise AssertionError("videos.list must not run for a video already judged")

    src = _source()
    src.cursor_json = {"seen": ["aaaaaaaaaaa"]}
    result = _adapter(handler).discover(src)
    assert result.items == [] and result.quota_units == 0


def test_backlog_pauses_searching_entirely() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no API call expected while the queue is full")

    result = _adapter(handler, backlog=50).discover(_source())
    assert result.items == [] and result.search_calls == 0


def test_search_budget_is_respected_and_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    result = _adapter(handler, search_budget=0).discover(_source())
    assert result.search_calls == 0 and result.items == []
    assert "sökgräns" in " ".join(result.notes)


def test_missing_key_or_topics_blocks_the_source() -> None:
    adapter = YouTubeCcSearchAdapter(None)
    assert "API-nyckel" in (adapter.validate_config(_source()) or "")
    assert "ämne" in (YouTubeCcSearchAdapter("k").validate_config(_source([])) or "")
