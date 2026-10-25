"""Pure-function and storage tests for the publishing building blocks."""

from __future__ import annotations

import logging
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.db.models import Channel, ClipCandidate, RenderedClip
from app.logging_config import AccessLogRedactor
from app.services.publishing.policy import check_publication
from app.services.publishing.secret_store import (
    FileStore,
    KeychainStore,
    MemoryStore,
    SecretStoreError,
    dump_token,
    load_refresh_token,
)
from app.services.publishing.service import MAX_DESCRIPTION_BYTES, PublishOptions, build_metadata, build_tags
from app.services.publishing.youtube import (
    OAuthClient,
    build_auth_url,
    build_video_resource,
    classify,
    code_challenge,
    derive_availability,
    valid_session_uri,
)
from app.services.settings_service import AppSettings, apply_patch


# ---------------------------------------------------------------- PKCE / URL
def test_pkce_challenge_matches_the_rfc_7636_vector() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert code_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_auth_url_never_contains_the_client_secret() -> None:
    url = build_auth_url(
        OAuthClient("cid", "super-secret"),
        redirect_uri="http://127.0.0.1:8000/cb",
        state="s",
        verifier="v" * 50,
    )
    assert "super-secret" not in url and "client_id=cid" in url and "prompt=consent+select_account" in url


@pytest.mark.parametrize(
    ("uri", "ok"),
    [
        ("https://www.googleapis.com/upload/youtube/v3/videos?upload_id=x", True),
        ("https://upload.googleapis.com/x", True),
        ("http://www.googleapis.com/upload", False),
        ("https://evil.example/upload?host=googleapis.com", False),
        ("https://googleapis.com.evil.example/x", False),
    ],
)
def test_upload_session_uri_must_be_a_google_https_host(uri: str, ok: bool) -> None:
    assert valid_session_uri(uri) is ok


# ---------------------------------------------------------------- error classification
def _resp(status: int, reason: str | None = None, message: str = "m", **headers: str) -> httpx.Response:
    body: dict = {"error": {"code": status, "message": message}}
    if reason:
        body["error"]["errors"] = [{"reason": reason}]
    return httpx.Response(status, json=body, headers=headers)


@pytest.mark.parametrize(
    ("status", "reason", "kind"),
    [
        (403, "quotaExceeded", "quota"),
        (403, "dailyLimitExceeded", "quota"),
        (403, "rateLimitExceeded", "rate"),
        (429, None, "rate"),
        (400, "uploadLimitExceeded", "upload_limit"),
        (403, "insufficientPermissions", "scope"),
        (403, "forbidden", "forbidden"),
        (403, "youtubeSignupRequired", "forbidden"),
        (403, "accessNotConfigured", "client"),
        (401, "authError", "auth"),
        (400, "invalidTitle", "invalid"),
        (404, "videoNotFound", "not_found"),
        (500, "backendError", "rate"),
        (502, None, "transient"),
        (503, None, "transient"),
    ],
)
def test_classification_of_youtube_errors(status: int, reason: str | None, kind: str) -> None:
    assert classify(_resp(status, reason)).kind == kind


def test_oauth_style_errors_and_retry_after() -> None:
    r = httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token revoked"})
    assert classify(r).kind == "auth_revoked"
    r = httpx.Response(401, json={"error": "invalid_client"})
    assert classify(r).kind == "client"
    assert classify(_resp(429, "rateLimitExceeded", **{"Retry-After": "120"})).retry_after == 120
    assert classify(httpx.Response(500, text="<html>not json</html>")).kind == "transient"


def test_error_messages_are_redacted() -> None:
    e = classify(_resp(400, "invalidTitle", "bad ya29.A0ARrdaM-secretSecretSecret and Bearer abc.def"))
    assert "ya29" not in e.safe_message and "abc.def" not in e.safe_message


# ---------------------------------------------------------------- what YouTube reports
@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"status": {"uploadStatus": "uploaded", "privacyStatus": "public"}}, "processing"),
        (
            {
                "status": {"uploadStatus": "processed", "privacyStatus": "public"},
                "processingDetails": {"processingStatus": "succeeded"},
            },
            "public",
        ),
        ({"status": {"uploadStatus": "processed", "privacyStatus": "unlisted"}}, "unlisted"),
        ({"status": {"uploadStatus": "processed", "privacyStatus": "private"}}, "private"),
        (
            {
                "status": {
                    "uploadStatus": "processed",
                    "privacyStatus": "private",
                    "publishAt": "2999-01-01T10:00:00Z",
                }
            },
            "scheduled",
        ),
        ({"status": {"uploadStatus": "rejected", "rejectionReason": "copyright"}}, "rejected"),
        ({"status": {"uploadStatus": "failed", "failureReason": "codec"}}, "failed"),
        (
            {"status": {"uploadStatus": "processed"}, "processingDetails": {"processingStatus": "failed"}},
            "failed",
        ),
        ({}, "unknown"),
    ],
)
def test_availability_is_derived_from_the_youtube_resource(item: dict, expected: str) -> None:
    assert derive_availability(item).availability == expected


def test_video_resource_schedules_only_private_uploads() -> None:
    when = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
    res = build_video_resource(
        title="T", description="D", tags=["a"], category_id="22", language="sv",
        privacy="public", made_for_kids=False, publish_at=when,
    )  # fmt: skip
    assert res["status"] == {
        "privacyStatus": "private",
        "selfDeclaredMadeForKids": False,
        "publishAt": "2030-01-02T03:04:05.000Z",
    }
    assert res["snippet"]["defaultLanguage"] == "sv" and res["snippet"]["tags"] == ["a"]
    plain = build_video_resource(
        title="T", description="", tags=[], category_id="22", language=None,
        privacy="unlisted", made_for_kids=True, publish_at=None,
    )  # fmt: skip
    assert plain["status"] == {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": True}
    assert "tags" not in plain["snippet"] and "defaultLanguage" not in plain["snippet"]


# ---------------------------------------------------------------- metadata
def _clip(**kw) -> RenderedClip:  # type: ignore[no-untyped-def]
    base = dict(
        title_suggestion="En <b>bra</b> titel",
        description_suggestion="Text.\n\n#shorts #sverige",
        hashtags_json=["#shorts", "#sverige"],
        review_status="approved",
        warnings_json=[],
    )
    base.update(kw)
    r = RenderedClip(**base)
    r.clip = ClipCandidate(final_score=70)
    return r


def test_metadata_sanitises_title_and_does_not_duplicate_hashtags() -> None:
    ch = Channel(
        display_name="K",
        title_template="{title}",
        description_template="{description}\n\n{hashtags}",
        category_id="24",
        default_language="sv",
        made_for_kids=False,
        notify_subscribers=False,
    )
    meta = build_metadata(ch, _clip(), PublishOptions())
    assert "<" not in meta["title"] and ">" not in meta["title"]
    assert meta["description"].count("#shorts") == 1  # already in the description; template adds nothing
    assert meta["category_id"] == "24" and meta["tags"] == ["shorts", "sverige"]


def test_metadata_respects_youtube_limits() -> None:
    ch = Channel(
        display_name="K", title_template="{title}", description_template="{description}", category_id="22"
    )
    long = _clip(description_suggestion="å" * 4000, title_suggestion="x" * 300)
    meta = build_metadata(ch, long, PublishOptions(description="ö" * 4400))
    assert len(meta["title"]) <= 100
    assert len(meta["description"].encode()) <= MAX_DESCRIPTION_BYTES
    tags = build_tags([f"#tag{i:03d}" + "x" * 20 for i in range(60)])
    assert sum(len(t) + 1 for t in tags) <= 450 and all(len(t) <= 30 for t in tags)
    assert build_tags(["#a", "#ok", "#OK", "#<b>", "#<ab>"]) == [
        "ok",
        "ab",
    ]  # too short / duplicate / stripped


def test_title_template_placeholders_are_not_code() -> None:
    ch = Channel(
        display_name="K",
        title_template="{title} {__class__} {creator}",
        description_template="{description}",
        category_id="22",
    )
    meta = build_metadata(ch, _clip(title_suggestion="Hej"), PublishOptions())
    assert meta["title"] == "Hej"  # unknown placeholders vanish, nothing is evaluated


# ---------------------------------------------------------------- policy additions
def _chan(**kw) -> Channel:  # type: ignore[no-untyped-def]
    base = dict(
        display_name="K", enabled=True, paused=False, auto_publish_enabled=True, max_uploads_per_day=1,
        min_spacing_minutes=60, timezone="Europe/Stockholm", token_reference="memory:x", schedule_json={},
        allow_public=False, connection_state="connected",
    )  # fmt: skip
    base.update(kw)
    return Channel(**base)


def _check(channel: Channel | None = None, **kw):  # type: ignore[no-untyped-def]
    args = dict(
        settings=AppSettings(auto_publish_global_enabled=True), channel=channel or _chan(), clip=_clip(),
        rights_status="user_owned", automatic=False, uploads_today_channel=5, uploads_today_global=50,
        last_upload_at=None, now=datetime.now(UTC), file_ok=True,
    )  # fmt: skip
    args.update(kw)
    return check_publication(**args)


def test_private_manual_upload_ignores_daily_caps_but_public_needs_opt_in() -> None:
    assert _check().allowed  # caps only throttle automation
    d = _check(visibility="public")
    assert not d.allowed and any("Offentlig" in r for r in d.reasons)
    assert not _check(scheduled=True).allowed
    assert _check(_chan(allow_public=True), visibility="public").allowed
    assert not _check(automatic=True).allowed  # automation does honour the caps


def test_reauth_state_blocks_publishing() -> None:
    d = _check(_chan(connection_state="needs_reauth"))
    assert not d.allowed and any("anslut" in r.lower() for r in d.reasons)


def test_quota_settings_are_validated() -> None:
    s = AppSettings()
    assert s.youtube_daily_insert_limit == 100 and s.youtube_daily_unit_limit == 10_000
    with pytest.raises(ValueError):
        apply_patch(s, {"youtube_daily_insert_limit": -1})


# ---------------------------------------------------------------- secret stores
def test_file_store_is_private_and_roundtrips(tmp_path: Path) -> None:
    store = FileStore(tmp_path / "secrets")
    ref = store.make_ref("youtube/abc123")
    store.set(ref, dump_token("1//refresh", ["s1"]))
    assert load_refresh_token(store.get(ref)) == "1//refresh"
    files = list((tmp_path / "secrets" / "tokens").glob("*.secret"))
    assert len(files) == 1
    assert stat.S_IMODE(os.stat(files[0]).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(files[0].parent).st_mode) == 0o700
    store.delete(ref)
    assert store.get(ref) is None
    store.delete(ref)  # idempotent


@pytest.mark.parametrize("ref", ["keychain:x", "file:../../etc/passwd", "file:a b", "nonsense", "file:"])
def test_stores_reject_foreign_or_unsafe_references(tmp_path: Path, ref: str) -> None:
    with pytest.raises(SecretStoreError):
        FileStore(tmp_path).get(ref)


def test_memory_store_scheme_and_helpers() -> None:
    m = MemoryStore()
    ref = m.make_ref("youtube/1")
    m.set(ref, "v")
    assert m.get(ref) == "v"
    with pytest.raises(SecretStoreError):
        m.get("file:youtube/1")
    assert load_refresh_token(None) is None and load_refresh_token("not json") is None
    assert load_refresh_token('{"refresh_token": ""}') is None


def test_keychain_store_fails_closed_without_a_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring
    from keyring.backends import fail

    monkeypatch.setattr(keyring, "get_keyring", lambda: fail.Keyring())
    ok, why = KeychainStore().available()
    assert ok is False and why


@pytest.mark.keychain
def test_real_macos_keychain_roundtrip() -> None:
    """Opt-in (`pytest -m keychain`): touches the real login Keychain with a throwaway item."""
    store = KeychainStore(service="ClipForge-selftest")
    ok, why = store.available()
    assert ok, why
    ref = store.make_ref(f"youtube/selftest-{os.getpid()}")
    try:
        store.set(ref, dump_token("1//selftest", []))
        assert load_refresh_token(store.get(ref)) == "1//selftest"
    finally:
        store.delete(ref)
    assert store.get(ref) is None


# ---------------------------------------------------------------- log redaction
def test_access_log_redacts_oauth_query_parameters() -> None:
    rec = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        (
            "127.0.0.1:5",
            "GET",
            "/api/v1/oauth/youtube/callback?state=SECRETSTATE&code=4%2FSECRETCODE&x=1",
            "1.1",
            303,
        ),
        None,
    )
    assert AccessLogRedactor().filter(rec)
    out = rec.getMessage()
    assert "SECRETSTATE" not in out and "SECRETCODE" not in out and "x=1" in out
