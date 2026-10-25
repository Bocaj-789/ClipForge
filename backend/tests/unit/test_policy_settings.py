from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.db.models import Channel, ClipCandidate, RenderedClip
from app.services.publishing.policy import check_publication, next_slot
from app.services.settings_service import AppSettings, apply_patch


def _channel(**kw) -> Channel:
    base = dict(
        display_name="K",
        enabled=True,
        paused=False,
        auto_publish_enabled=True,
        max_uploads_per_day=2,
        min_spacing_minutes=60,
        timezone="Europe/Stockholm",
        token_reference="keychain:x",
        schedule_json={},
    )
    base.update(kw)
    return Channel(**base)


def _clip(**kw) -> RenderedClip:
    base = dict(title_suggestion="En bra titel", review_status="approved", warnings_json=[])
    base.update(kw)
    r = RenderedClip(**base)
    r.clip = ClipCandidate(final_score=70)
    return r


def _check(settings=None, channel=None, clip=None, **kw):
    args = dict(
        settings=settings or AppSettings(auto_publish_global_enabled=True),
        channel=channel or _channel(),
        clip=clip or _clip(),
        rights_status="user_owned",
        automatic=True,
        uploads_today_channel=0,
        uploads_today_global=0,
        last_upload_at=None,
        now=datetime.now(UTC),
        file_ok=True,
    )
    args.update(kw)
    return check_publication(**args)


def test_happy_path_allowed() -> None:
    assert _check().allowed


def test_global_kill_switch_blocks_auto_publish() -> None:
    d = _check(settings=AppSettings(auto_publish_global_enabled=False))
    assert not d.allowed and any("globalt" in r for r in d.reasons)


def test_channel_auto_publish_off_blocks() -> None:
    assert not _check(channel=_channel(auto_publish_enabled=False)).allowed


def test_daily_caps_and_spacing() -> None:
    assert not _check(uploads_today_channel=2).allowed
    assert not _check(uploads_today_global=AppSettings().max_uploads_per_day_global).allowed
    assert not _check(last_upload_at=datetime.now(UTC) - timedelta(minutes=10)).allowed


def test_manual_publish_requires_approval_and_oauth() -> None:
    assert not _check(automatic=False, clip=_clip(review_status="needs_review")).allowed
    assert not _check(channel=_channel(token_reference=None)).allowed
    assert not _check(file_ok=False).allowed
    assert not _check(clip=_clip(title_suggestion="  <> ")).allowed


def test_warnings_only_hold_back_automatic_uploads_that_would_become_public() -> None:
    warned = _clip(warnings_json=["Källan har låg upplösning; beskärningen skalas upp kraftigt."])
    assert _check(clip=warned).allowed  # private: safe to review in YouTube Studio
    assert not _check(clip=warned, channel=_channel(allow_public=True), visibility="public").allowed
    assert not _check(clip=warned, channel=_channel(allow_public=True), scheduled=True).allowed


def test_crop_note_alone_does_not_block_public_release() -> None:
    crop_only = _clip(
        warnings_json=["Centrerad beskärning utan motivspårning – kontrollera att talaren syns."]
    )
    assert _check(clip=crop_only, channel=_channel(allow_public=True), scheduled=True).allowed


def test_next_slot_respects_window_and_dst() -> None:
    ch = _channel(schedule_json={"days": [6], "windows": [["10:00", "12:00"]]})  # Sundays
    # 2026-10-25 is the Sunday DST ends in Europe (03:00 CEST -> 02:00 CET).
    after = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    slot = next_slot(ch, after)
    assert slot == datetime(2026, 10, 25, 9, 0, tzinfo=UTC)  # 10:00 CET == 09:00 UTC
    summer = next_slot(ch, datetime(2026, 7, 4, 12, 0, tzinfo=UTC))  # Saturday
    assert summer == datetime(2026, 7, 5, 8, 0, tzinfo=UTC)  # 10:00 CEST == 08:00 UTC


def test_next_slot_inside_window_returns_now() -> None:
    ch = _channel(schedule_json={"windows": [["00:00", "23:59"]]})
    now = datetime(2026, 3, 3, 12, 0, tzinfo=UTC)
    assert next_slot(ch, now) == now


def test_settings_validation() -> None:
    s = AppSettings()
    assert s.auto_publish_global_enabled is False and s.remote_access_enabled is False
    with pytest.raises(ValueError):
        apply_patch(s, {"min_clip_seconds": 70, "max_clip_seconds": 60})
    with pytest.raises(ValueError):
        apply_patch(s, {"timezone": "Mars/Olympus"})
    with pytest.raises(ValueError):
        apply_patch(s, {"no_such_setting": 1})
    with pytest.raises(ValueError):
        apply_patch(s, {"ollama_model": "llama; rm -rf /"})
    assert apply_patch(
        s, {"allowed_direct_media_hosts": ["Archive.ORG", "archive.org"]}
    ).allowed_direct_media_hosts == ["archive.org"]
