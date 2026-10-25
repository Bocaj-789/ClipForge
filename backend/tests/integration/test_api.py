from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import auth as core_auth
from app.db.models import Job
from app.db.session import new_session
from tests.conftest import PASSWORD, login

VIDEO = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_health_is_public_but_everything_else_requires_login(client: TestClient) -> None:
    assert client.get("/api/v1/health").json() == {"status": "ok"}
    for path in ("/api/v1/agent/status", "/api/v1/jobs", "/api/v1/settings", "/api/v1/clips"):
        r = client.get(path)
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "unauthorized"


def test_setup_requires_local_token_and_strong_password(client: TestClient, cfg) -> None:
    me = client.get("/api/v1/auth/me").json()
    assert me == {"authenticated": False, "setup_required": True}
    r = client.post(
        "/api/v1/auth/setup", json={"setup_token": "wrong-token-123", "username": "a", "password": PASSWORD}
    )
    assert r.status_code == 403
    token = (cfg.secrets_dir / "setup-token.txt").read_text().strip()
    r = client.post(
        "/api/v1/auth/setup", json={"setup_token": token, "username": "agaren", "password": "kort"}
    )
    assert r.status_code == 400
    headers = login(client, cfg)
    assert not (cfg.secrets_dir / "setup-token.txt").exists()  # one-time
    me = client.get("/api/v1/auth/me").json()
    assert me["authenticated"] and me["csrf_token"] == headers["x-csrf-token"]
    cookie = client.cookies.get(core_auth.SESSION_COOKIE)
    assert cookie and len(cookie) > 30
    # Second setup is refused.
    r = client.post("/api/v1/auth/setup", json={"setup_token": token, "username": "x", "password": PASSWORD})
    assert r.status_code in (403, 409)


def test_csrf_and_origin_enforced(authed) -> None:
    client, headers = authed
    assert client.post("/api/v1/agent/pause").status_code == 403
    r = client.post("/api/v1/agent/pause", headers={**headers, "origin": "https://evil.example"})
    assert r.status_code == 403
    assert client.post("/api/v1/agent/pause", headers=headers).status_code == 200


def test_login_rate_limit_and_logout(client: TestClient, cfg) -> None:
    login(client, cfg)
    client.cookies.clear()
    for _ in range(5):
        assert (
            client.post("/api/v1/auth/login", json={"username": "agaren", "password": "fel"}).status_code
            == 401
        )
    assert (
        client.post("/api/v1/auth/login", json={"username": "agaren", "password": PASSWORD}).status_code
        == 429
    )


def test_session_cookie_flags(client: TestClient, cfg) -> None:
    token = (cfg.secrets_dir / "setup-token.txt").read_text().strip()
    r = client.post(
        "/api/v1/auth/setup", json={"setup_token": token, "username": "agaren", "password": PASSWORD}
    )
    sc = r.headers["set-cookie"].lower()
    assert "httponly" in sc and "samesite=strict" in sc


def test_security_headers(client: TestClient) -> None:
    r = client.get("/api/v1/health")
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"
    assert client.get("/api/docs").status_code in (404, 503)  # docs disabled outside dev mode


def test_inbox_priority_and_duplicates(authed) -> None:
    client, h = authed
    r = client.post("/api/v1/inbox", json={"url": VIDEO, "urgent": False}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    cid = body["candidate"]["id"]
    assert body["duplicate"] is False and body["candidate"]["priority"] == 1
    # Same video in another URL form, marked urgent: no new job, priority raised.
    r = client.post("/api/v1/inbox", json={"url": "https://youtu.be/dQw4w9WgXcQ", "urgent": True}, headers=h)
    body = r.json()
    assert body["duplicate"] and body["priority_raised"] and body["candidate"]["id"] == cid
    with new_session() as s:
        jobs = s.scalars(select(Job).where(Job.candidate_video_id == cid)).all()
        assert len(jobs) == 1 and jobs[0].priority == 0


def test_inbox_rejects_unsafe_urls(authed) -> None:
    client, h = authed
    for bad in (
        "http://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
        "https://10.0.0.1/a.mp4",
        "https://www.youtube.com/@kanal",
    ):
        r = client.post("/api/v1/inbox", json={"url": bad}, headers=h)
        assert r.status_code == 400, bad
        assert r.json()["error"]["code"] == "unsupported_url"


def test_bulk_inbox(authed) -> None:
    client, h = authed
    r = client.post(
        "/api/v1/inbox/bulk", json={"urls": [VIDEO, "nonsense", "https://youtu.be/aaaaaaaaaaa"]}, headers=h
    )
    res = r.json()["results"]
    assert [x["ok"] for x in res] == [True, False, True]


def test_settings_confirmation_required_for_risky_switches(authed) -> None:
    client, h = authed
    r = client.patch("/api/v1/settings", json={"changes": {"auto_publish_global_enabled": True}}, headers=h)
    assert r.status_code == 400
    r = client.patch(
        "/api/v1/settings",
        json={"changes": {"auto_publish_global_enabled": True}, "confirm": "JA"},
        headers=h,
    )
    assert r.status_code == 200 and r.json()["settings"]["auto_publish_global_enabled"] is True
    r = client.patch("/api/v1/settings", json={"changes": {"max_clip_seconds": 3}}, headers=h)
    assert r.status_code == 400
    r = client.patch("/api/v1/settings", json={"changes": {"secret_backdoor": 1}}, headers=h)
    assert r.status_code == 400


def test_agent_control_and_status(authed) -> None:
    client, h = authed
    st = client.get("/api/v1/agent/status").json()
    assert st["state"] == "stopped" and not st["worker_alive"]  # no worker in tests
    client.post("/api/v1/agent/stop", headers=h)
    assert client.get("/api/v1/agent/status").json()["desired_state"] == "stopped"
    client.post("/api/v1/agent/resume", headers=h)
    assert client.get("/api/v1/agent/status").json()["desired_state"] == "running"


def test_channels_auto_publish_requires_oauth_and_phrase(authed) -> None:
    client, h = authed
    r = client.post(
        "/api/v1/channels", json={"display_name": "Min kanal", "max_uploads_per_day": 2}, headers=h
    )
    assert r.status_code == 200, r.text
    ch = r.json()
    assert ch["auto_publish_enabled"] is False and ch["default_visibility"] == "private"
    assert ch["oauth_connected"] is False and ch["next_slot"] is not None
    r = client.post(
        f"/api/v1/channels/{ch['id']}/auto-publish",
        json={"enabled": True, "confirm_phrase": "JAG FÖRSTÅR RISKERNA"},
        headers=h,
    )
    assert r.status_code == 400  # not connected
    assert client.post("/api/v1/channels/oauth/start", headers=h).status_code == 501
    r = client.patch(f"/api/v1/channels/{ch['id']}/settings", json={"timezone": "Nowhere/City"}, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_input"


def test_sources_validation(authed) -> None:
    client, h = authed
    r = client.post(
        "/api/v1/sources",
        json={
            "kind": "rss",
            "name": "Kanal",
            "url": "https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw",
        },
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["url"] == "https://www.youtube.com/feeds/videos.xml?channel_id=UC_x5XG1OV2P6uZZ5FSM9Ttw"
    r = client.post(
        "/api/v1/sources",
        json={"kind": "youtube_channel", "name": "x", "url": "https://www.youtube.com/@x123"},
        headers=h,
    )
    assert r.status_code == 400  # no API key configured
    r = client.post(
        "/api/v1/sources", json={"kind": "rss", "name": "x", "url": "https://192.168.1.1/feed"}, headers=h
    )
    assert r.status_code == 400


def test_dependencies_never_leak_secrets(authed, cfg) -> None:
    client, _ = authed
    body = client.get("/api/v1/health/dependencies").json()
    assert body["paid_ai_apis"] == "disabled"
    assert body["remote_access_enabled"] is False
    assert "youtube_api_key" not in str(body).replace("youtube_api_key_configured", "")
