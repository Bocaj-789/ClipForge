"""OAuth 2.0 connection flow, token secrecy and multi-channel management.

Google is replaced by ``FakeGoogle`` (httpx.MockTransport): these tests prove ClipForge's behaviour
against the protocol, not Google's. No real network call or real Google account is involved.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from urllib.parse import parse_qsl, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Config
from app.core import auth as core_auth
from app.db.base import utcnow
from app.db.models import Channel, ConnectionState, OAuthState
from app.db.session import session_scope
from app.services.publishing.secret_store import load_refresh_token
from app.services.publishing.youtube import SCOPE_READONLY, SCOPE_UPLOAD
from tests.fake_google import UPLOAD_SCOPE, FakeGoogle

CB = "/api/v1/oauth/youtube/callback"


def _start(client: TestClient, h: dict[str, str], channel_id: str | None = None) -> dict[str, str]:
    r = client.post("/api/v1/oauth/youtube/start", json={"channel_id": channel_id}, headers=h)
    assert r.status_code == 200, r.text
    return dict(parse_qsl(urlsplit(r.json()["auth_url"]).query))


def _callback(client: TestClient, **params: str):  # type: ignore[no-untyped-def]
    return client.get(CB, params=params, follow_redirects=False)


def _connect(client, h, google, channel_id, title, *, target=None) -> str:  # type: ignore[no-untyped-def]
    acc = google.accounts.get(channel_id) or google.add_account(channel_id, title)
    params = _start(client, h, target)
    r = _callback(client, state=params["state"], code=google.grant(acc))
    assert r.status_code == 303, r.text
    return r.headers["location"]


# ---------------------------------------------------------------- start
def test_start_requires_login_and_csrf(pclient: TestClient) -> None:
    assert pclient.post("/api/v1/oauth/youtube/start", json={}).status_code == 401


def test_start_builds_a_secure_authorization_url(pauthed, pub_cfg: Config) -> None:
    client, h = pauthed
    r = client.post("/api/v1/oauth/youtube/start", json={}, headers=h)
    assert r.status_code == 200
    url = r.json()["auth_url"]
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    p = dict(parse_qsl(urlsplit(url).query))
    assert p["client_id"] == "test-client.apps.googleusercontent.com"
    assert p["redirect_uri"] == "http://127.0.0.1:8000/api/v1/oauth/youtube/callback"
    assert p["response_type"] == "code" and p["access_type"] == "offline"
    assert set(p["scope"].split()) == {SCOPE_UPLOAD, SCOPE_READONLY}
    assert p["code_challenge_method"] == "S256" and len(p["code_challenge"]) >= 43
    assert len(p["state"]) >= 40
    assert "test-client-secret-value" not in url  # the client secret never goes through the browser
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/api/v1/oauth/youtube" in cookie
    with session_scope() as db:
        row = db.scalars(select(OAuthState)).one()
        assert row.state_hash != p["state"] and len(row.state_hash) == 64  # only the hash is stored


def test_start_reports_missing_google_client(tmp_path) -> None:
    from app.main import create_app
    from tests.conftest import login

    cfg = Config(_env_file=None, data_dir=tmp_path / "d")
    cfg.ensure_dirs()
    with TestClient(create_app(cfg, configure_logging=False), base_url="http://127.0.0.1:8000") as c:
        h = login(c, cfg)
        r = c.post("/api/v1/oauth/youtube/start", json={}, headers=h)
        assert r.status_code == 400 and "OAuth-klienten" in r.json()["error"]["message"]


def test_client_secret_json_file_is_supported(tmp_path) -> None:
    cfg = Config(_env_file=None, data_dir=tmp_path / "d")
    cfg.ensure_dirs()
    assert cfg.google_oauth_client() is None
    cfg.google_client_secret_file.write_text(
        '{"web": {"client_id": "abc.apps.googleusercontent.com", "client_secret": "s3"}}'
    )
    assert cfg.google_oauth_client() == ("abc.apps.googleusercontent.com", "s3")


# ---------------------------------------------------------------- callback / state validation
def test_callback_rejects_unknown_state(pauthed, google: FakeGoogle) -> None:
    client, _ = pauthed
    acc = google.add_account("UC_A", "Kanal A")
    r = _callback(client, state="not-a-real-state", code=google.grant(acc))
    assert r.status_code == 303 and r.headers["location"] == "/kanaler?oauth_error=invalid_state"
    with session_scope() as db:
        assert db.scalars(select(Channel)).all() == []
    assert not any(c[1] == "/token" for c in google.calls)  # never even reached Google


def test_callback_without_state_is_rejected(pauthed) -> None:
    client, _ = pauthed
    assert "invalid_state" in _callback(client, code="x").headers["location"]


def test_state_is_single_use(pauthed, google: FakeGoogle) -> None:
    client, h = pauthed
    acc = google.add_account("UC_A", "Kanal A")
    p = _start(client, h)
    first = _callback(client, state=p["state"], code=google.grant(acc))
    assert "connected=" in first.headers["location"]
    # Replaying the very same callback (browser back/forward, or an attacker) must fail.
    client.cookies.set("cf_oauth", "x", path="/api/v1/oauth/youtube")
    replay = _callback(client, state=p["state"], code=google.grant(acc))
    assert replay.headers["location"] == "/kanaler?oauth_error=invalid_state"
    with session_scope() as db:
        assert len(db.scalars(select(Channel)).all()) == 1


def test_state_is_bound_to_the_browser_that_started_it(pauthed, google: FakeGoogle) -> None:
    client, h = pauthed
    acc = google.add_account("UC_A", "Kanal A")
    p = _start(client, h)
    client.cookies.delete("cf_oauth", path="/api/v1/oauth/youtube")  # a different browser
    r = _callback(client, state=p["state"], code=google.grant(acc))
    assert r.headers["location"] == "/kanaler?oauth_error=invalid_state"
    with session_scope() as db:
        assert db.scalars(select(Channel)).all() == []
        assert db.scalars(select(OAuthState)).one().consumed_at is not None  # burned, cannot be retried


def test_expired_state_is_rejected(pauthed, google: FakeGoogle) -> None:
    client, h = pauthed
    acc = google.add_account("UC_A", "Kanal A")
    p = _start(client, h)
    with session_scope() as db:
        db.scalars(select(OAuthState)).one().expires_at = utcnow() - timedelta(seconds=1)
    r = _callback(client, state=p["state"], code=google.grant(acc))
    assert r.headers["location"] == "/kanaler?oauth_error=expired_state"


def test_user_denying_consent_connects_nothing(pauthed) -> None:
    client, h = pauthed
    p = _start(client, h)
    r = _callback(client, state=p["state"], error="access_denied")
    assert r.headers["location"] == "/kanaler?oauth_error=access_denied"
    with session_scope() as db:
        assert db.scalars(select(Channel)).all() == []


def test_callback_does_not_need_the_session_cookie(pauthed, google: FakeGoogle) -> None:
    """The Strict session cookie is not sent on Google's cross-site redirect; the flow must still work."""
    client, h = pauthed
    acc = google.add_account("UC_A", "Kanal A")
    p = _start(client, h)
    client.cookies.delete(core_auth.SESSION_COOKIE)
    assert core_auth.SESSION_COOKIE not in client.cookies
    assert "connected=" in _callback(client, state=p["state"], code=google.grant(acc)).headers["location"]


# ---------------------------------------------------------------- successful connection
def test_connect_creates_channel_and_keeps_token_only_in_secret_store(pauthed, google, pdeps) -> None:
    client, h = pauthed
    loc = _connect(client, h, google, "UC_A", "Min Kanal")
    assert loc.startswith("/kanaler?connected=")
    ch = client.get("/api/v1/channels").json()["items"][0]
    assert ch["display_name"] == "Min Kanal" and ch["external_channel_id"] == "UC_A"
    assert ch["oauth_connected"] is True and ch["connection_state"] == "connected"
    assert ch["youtube_handle"] == "@kanal"
    # New channels default to private and need explicit opt-ins for anything public/automatic.
    assert ch["default_visibility"] == "private"
    assert ch["allow_public"] is False
    # A channel added with the connect button joins automatic publishing (which stays inert until the
    # global switch is turned on) with limits sized for several clips a day.
    assert ch["auto_publish_enabled"] is True and ch["max_uploads_per_day"] == 10
    with session_scope() as db:
        row = db.scalars(select(Channel)).one()
        ref = row.token_reference
        assert ref and ref.startswith("memory:")
        assert load_refresh_token(pdeps.store.get(ref)) == google.accounts["UC_A"].refresh_token
    # The authorisation code was exchanged with PKCE and the client secret, server side only.
    assert ("POST", "/token") in google.calls


def test_scopes_not_granted_are_refused_and_revoked(pauthed, google: FakeGoogle) -> None:
    client, h = pauthed
    acc = google.add_account("UC_A", "Kanal A")
    p = _start(client, h)
    r = _callback(client, state=p["state"], code=google.grant(acc, scope=UPLOAD_SCOPE))  # unticked read scope
    assert r.headers["location"] == "/kanaler?oauth_error=scopes_missing"
    assert google.revoked, "the partially granted token must be revoked at Google"
    with session_scope() as db:
        assert db.scalars(select(Channel)).all() == []


def test_missing_refresh_token_is_refused(pauthed, google: FakeGoogle) -> None:
    client, h = pauthed
    acc = google.add_account("UC_A", "Kanal A")
    google.fault("omit_refresh_token", True)
    p = _start(client, h)
    r = _callback(client, state=p["state"], code=google.grant(acc))
    assert r.headers["location"] == "/kanaler?oauth_error=no_refresh_token"


def test_bad_authorization_code_is_reported(pauthed) -> None:
    client, h = pauthed
    p = _start(client, h)
    r = _callback(client, state=p["state"], code="4/forged")
    assert r.headers["location"] == "/kanaler?oauth_error=token_exchange_failed"


# ---------------------------------------------------------------- several channels
def test_multiple_channels_are_independent(pauthed, google, pdeps) -> None:
    client, h = pauthed
    _connect(client, h, google, "UC_A", "Kanal A")
    _connect(client, h, google, "UC_B", "Kanal B")
    items = client.get("/api/v1/channels").json()["items"]
    assert [c["display_name"] for c in items] == ["Kanal A", "Kanal B"]
    with session_scope() as db:
        rows = db.scalars(select(Channel).order_by(Channel.created_at)).all()
        assert rows[0].token_reference != rows[1].token_reference
        tokens = {load_refresh_token(pdeps.store.get(r.token_reference)) for r in rows}
        assert tokens == {google.accounts["UC_A"].refresh_token, google.accounts["UC_B"].refresh_token}
    # Per-channel settings do not leak between channels.
    a, b = items
    r = client.patch(f"/api/v1/channels/{a['id']}/settings", json={"max_uploads_per_day": 7}, headers=h)
    assert r.json()["max_uploads_per_day"] == 7
    assert client.get("/api/v1/channels").json()["items"][1]["max_uploads_per_day"] == 10
    # Disconnecting one leaves the other connected.
    assert client.post(f"/api/v1/channels/{a['id']}/disconnect", headers=h).status_code == 200
    states = {
        c["display_name"]: c["connection_state"] for c in client.get("/api/v1/channels").json()["items"]
    }
    assert states == {"Kanal A": "disconnected", "Kanal B": "connected"}


def test_reconnecting_updates_instead_of_duplicating(pauthed, google) -> None:
    client, h = pauthed
    _connect(client, h, google, "UC_A", "Kanal A")
    cid = client.get("/api/v1/channels").json()["items"][0]["id"]
    _connect(client, h, google, "UC_A", "Kanal A", target=cid)
    _connect(client, h, google, "UC_A", "Kanal A")  # even without a target the same channel is matched
    assert len(client.get("/api/v1/channels").json()["items"]) == 1


def test_reconnect_with_a_different_youtube_channel_is_refused(pauthed, google) -> None:
    client, h = pauthed
    _connect(client, h, google, "UC_A", "Kanal A")
    _connect(client, h, google, "UC_B", "Kanal B")
    items = client.get("/api/v1/channels").json()["items"]
    # Reconnecting entry A but choosing channel B at Google:
    loc = _connect(client, h, google, "UC_B", "Kanal B", target=items[0]["id"])
    assert loc == "/kanaler?oauth_error=channel_in_use"
    # Reconnecting an entry for A while authorising a brand new channel C:
    loc = _connect(client, h, google, "UC_C", "Kanal C", target=items[0]["id"])
    assert loc == "/kanaler?oauth_error=wrong_channel"
    assert len(client.get("/api/v1/channels").json()["items"]) == 2
    assert google.revoked, "tokens obtained for refused connections must be revoked"


def test_account_without_a_youtube_channel(pauthed, google) -> None:
    client, h = pauthed
    acc = google.add_account("UC_A", "Kanal A")
    from tests.fake_google import error_response

    google.fault("channels", error_response(403, "youtubeSignupRequired", "no channel"))
    p = _start(client, h)
    r = _callback(client, state=p["state"], code=google.grant(acc))
    assert r.headers["location"] == "/kanaler?oauth_error=no_channel"


def test_disconnect_revokes_wipes_token_and_disables_automation(pauthed, google, pdeps) -> None:
    client, h = pauthed
    _connect(client, h, google, "UC_A", "Kanal A")
    cid = client.get("/api/v1/channels").json()["items"][0]["id"]
    with session_scope() as db:
        ref = db.get(Channel, cid).token_reference
        db.get(Channel, cid).auto_publish_enabled = True
    r = client.post(f"/api/v1/channels/{cid}/disconnect", headers=h)
    body = r.json()
    assert r.status_code == 200 and body["connection_state"] == "disconnected"
    assert body["oauth_connected"] is False and body["auto_publish_enabled"] is False
    assert body["revoked_at_google"] is True
    assert google.accounts["UC_A"].refresh_token in google.revoked
    assert pdeps.store.get(ref) is None
    # A disconnected channel can now be removed; a connected one cannot.
    assert client.delete(f"/api/v1/channels/{cid}", headers=h).status_code == 200


def test_connected_channel_cannot_be_deleted(pauthed, google) -> None:
    client, h = pauthed
    _connect(client, h, google, "UC_A", "Kanal A")
    cid = client.get("/api/v1/channels").json()["items"][0]["id"]
    assert client.delete(f"/api/v1/channels/{cid}", headers=h).status_code == 409


# ---------------------------------------------------------------- privacy defaults and opt-ins
def test_public_requires_explicit_opt_in_with_phrase(pauthed, google) -> None:
    client, h = pauthed
    _connect(client, h, google, "UC_A", "Kanal A")
    cid = client.get("/api/v1/channels").json()["items"][0]["id"]
    r = client.patch(f"/api/v1/channels/{cid}/settings", json={"default_visibility": "public"}, headers=h)
    assert r.status_code == 400  # public default is impossible without the opt-in
    assert (
        client.post(f"/api/v1/channels/{cid}/allow-public", json={"enabled": True}, headers=h).status_code
        == 400
    )
    r = client.post(
        f"/api/v1/channels/{cid}/allow-public",
        json={"enabled": True, "confirm_phrase": "offentlig publicering"},
        headers=h,
    )
    assert r.status_code == 200 and r.json()["allow_public"] is True
    r = client.patch(f"/api/v1/channels/{cid}/settings", json={"default_visibility": "public"}, headers=h)
    assert r.status_code == 200 and r.json()["default_visibility"] == "public"
    # Withdrawing the opt-in also resets the default back to private.
    r = client.post(f"/api/v1/channels/{cid}/allow-public", json={"enabled": False}, headers=h)
    assert r.json()["allow_public"] is False and r.json()["default_visibility"] == "private"


def test_new_manual_channel_defaults_to_private_and_no_automation(pauthed) -> None:
    client, h = pauthed
    r = client.post("/api/v1/channels", json={"display_name": "Utkast"}, headers=h)
    ch = r.json()
    assert ch["default_visibility"] == "private" and ch["allow_public"] is False
    assert ch["auto_publish_enabled"] is False and ch["made_for_kids"] is False
    r = client.post("/api/v1/channels", json={"display_name": "X", "default_visibility": "public"}, headers=h)
    assert r.status_code == 400


def test_auto_publish_needs_a_working_connection(pauthed, google) -> None:
    client, h = pauthed
    _connect(client, h, google, "UC_A", "Kanal A")
    cid = client.get("/api/v1/channels").json()["items"][0]["id"]
    with session_scope() as db:
        db.get(Channel, cid).connection_state = ConnectionState.NEEDS_REAUTH
    r = client.post(
        f"/api/v1/channels/{cid}/auto-publish",
        json={"enabled": True, "confirm_phrase": "JAG FÖRSTÅR RISKERNA"},
        headers=h,
    )
    assert r.status_code == 400


# ---------------------------------------------------------------- secrecy
def test_tokens_never_leak_into_api_responses_database_or_logs(
    pauthed, google, pdeps, pub_cfg, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    client, h = pauthed
    _connect(client, h, google, "UC_A", "Kanal A")
    cid = client.get("/api/v1/channels").json()["items"][0]["id"]
    secrets_ = [
        google.accounts["UC_A"].refresh_token,
        "test-client-secret-value",
        *google.access.keys(),
    ]
    assert all(len(s) > 8 for s in secrets_)
    # Provoke more logging and traffic: a publication and a status refresh.
    from tests.publishing_helpers import make_clip

    rid = make_clip(pub_cfg)
    assert (
        client.post(f"/api/v1/clips/{rid}/publish", json={"channel_ids": [cid]}, headers=h).status_code == 200
    )
    bodies = []
    for path in (
        "/api/v1/channels",
        f"/api/v1/channels/{cid}/publications",
        "/api/v1/publishing/status",
        "/api/v1/publications",
        "/api/v1/health/dependencies",
        "/api/v1/dashboard/summary",
        "/api/v1/logs",
        "/api/v1/logs/export",
        "/api/v1/jobs",
    ):
        r = client.get(path)
        assert r.status_code == 200, path
        bodies.append(r.text)
    for text in bodies:
        for s in secrets_:
            assert s not in text
        assert "token_reference" not in text and "memory:" not in text
    # The database files contain no credential, only a reference-less state.
    for f in pub_cfg.data_dir.glob("clipforge.sqlite3*"):
        raw = f.read_bytes()
        for s in secrets_:
            assert s.encode() not in raw
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for s in secrets_:
        assert s not in logged
    for f in (pub_cfg.logs_dir).glob("*"):
        for s in secrets_:
            assert s.encode() not in f.read_bytes()
    # Positive control: the token really is retrievable from the secret store.
    with session_scope() as db:
        ref = db.get(Channel, cid).token_reference
    assert google.accounts["UC_A"].refresh_token in (pdeps.store.get(ref) or "")
