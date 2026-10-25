from __future__ import annotations

import socket
from pathlib import Path

import pytest

from app.core.security import PathContainmentError, contained_path, redact, redact_mapping, safe_filename
from app.services import safe_fetch
from app.services.urls import UrlKind, UrlRejected, classify_url, ip_is_public, revalidate_redirect

HOSTS = ["archive.org"]


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com/watch?v=dQw4w9WgXcQ&t=42s&list=abc",
        "https://youtu.be/dQw4w9WgXcQ?si=xyz",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "https://www.youtube.com/live/dQw4w9WgXcQ",
        "https://www.youtube.com/embed/dQw4w9WgXcQ",
        "youtube.com/watch?v=dQw4w9WgXcQ",
        "  https://WWW.YOUTUBE.COM/watch?v=dQw4w9WgXcQ#frag  ",
    ],
)
def test_youtube_forms_normalise_to_one_canonical_url(raw: str) -> None:
    c = classify_url(raw, direct_media_hosts=HOSTS)
    assert c.kind == UrlKind.YOUTUBE_VIDEO
    assert c.canonical_url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert c.external_id == "dQw4w9WgXcQ"


def test_youtube_channel_urls() -> None:
    c = classify_url("https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw", direct_media_hosts=[])
    assert c.kind == UrlKind.YOUTUBE_CHANNEL and c.external_id == "UC_x5XG1OV2P6uZZ5FSM9Ttw"
    h = classify_url("https://www.youtube.com/@GoogleDevelopers", direct_media_hosts=[])
    assert h.kind == UrlKind.YOUTUBE_CHANNEL and h.external_id == "@GoogleDevelopers"


@pytest.mark.parametrize(
    "raw",
    [
        "http://www.youtube.com/watch?v=dQw4w9WgXcQ",  # not https
        "file:///etc/passwd",
        "ftp://archive.org/x.mp4",
        "https://127.0.0.1/video.mp4",
        "https://[::1]/video.mp4",
        "https://10.0.0.5/x.mp4",
        "https://localhost/x.mp4",
        "https://router.local/x.mp4",
        "https://intranet/x.mp4",
        "https://user:pass@archive.org/x.mp4",
        "https://archive.org:8443/x.mp4",
        "https://evil.example.com/x.mp4",  # not allowlisted
        "https://www.youtube.com/watch?v=short",  # bad id
        "https://archive.org.evil.com/x.mp4",  # suffix trick
        "https://" + "a" * 2100 + ".com",
        "",
        "https://archive.org/\x00x",
    ],
)
def test_rejected_urls(raw: str) -> None:
    with pytest.raises(UrlRejected):
        classify_url(raw, direct_media_hosts=HOSTS)


def test_direct_media_allowlist_includes_subdomains() -> None:
    c = classify_url("https://ia800.us.archive.org/1/items/x/film.mp4?b=2&a=1#t", direct_media_hosts=HOSTS)
    assert c.kind == UrlKind.DIRECT_MEDIA
    assert c.canonical_url == "https://ia800.us.archive.org/1/items/x/film.mp4?a=1&b=2"


def test_redirect_revalidation_blocks_private_and_foreign_hosts() -> None:
    with pytest.raises(UrlRejected):
        revalidate_redirect("https://127.0.0.1/x", allowed_hosts=HOSTS)
    with pytest.raises(UrlRejected):
        revalidate_redirect("https://evil.com/x", allowed_hosts=HOSTS)
    assert revalidate_redirect("https://ia1.archive.org/x", allowed_hosts=HOSTS)


@pytest.mark.parametrize(
    "addr,public",
    [
        ("8.8.8.8", True),
        ("10.1.2.3", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("192.168.1.1", False),
        ("::1", False),
        ("::ffff:127.0.0.1", False),
        ("100.64.0.1", False),
        ("2606:4700:4700::1111", True),
        ("0.0.0.0", False),
    ],
)
def test_ip_is_public(addr: str, public: bool) -> None:
    assert ip_is_public(addr) is public


def test_dns_resolving_to_private_ip_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(host: str, port: int, type: int = 0):  # noqa: A002
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.0.10", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    with pytest.raises(UrlRejected):
        safe_fetch.resolve_public("rebind.example.com", 443)


def test_redaction() -> None:
    s = (
        "Authorization: Bearer ya29.abcDEF-123 key=AIzaSyA1234567890abcdefghijklmnopqrstu "
        "refresh_token=1//0gXYZ"
    )
    out = redact(s)
    assert "ya29" not in out and "AIzaSy" not in out and "1//0g" not in out
    assert redact_mapping({"refresh_token": "x", "nested": {"api_key": "y", "ok": "fine"}}) == {
        "refresh_token": "[REDACTED]",
        "nested": {"api_key": "[REDACTED]", "ok": "fine"},
    }


def test_path_containment(tmp_path: Path) -> None:
    base = tmp_path / "data"
    (base / "clips").mkdir(parents=True)
    assert contained_path(base, "clips/a.mp4") == (base / "clips/a.mp4").resolve()
    for bad in ("../secret", "/etc/passwd", "clips/../../x"):
        with pytest.raises(PathContainmentError):
            contained_path(base, bad)
    (base / "link").symlink_to("/etc")
    with pytest.raises(PathContainmentError):
        contained_path(base, "link/passwd")


def test_safe_filename() -> None:
    n = safe_filename("../../Räksmörgås <script>.mp4", "MP4")
    assert "/" not in n and ".." not in n.split("-")[0] and n.endswith(".mp4")
    assert n != safe_filename("../../Räksmörgås <script>.mp4", "MP4")
