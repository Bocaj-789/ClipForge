"""An in-process stand-in for Google's OAuth and YouTube Data API endpoints.

It speaks just enough of the real protocols (authorization-code + refresh grants, ``channels.list``,
resumable ``videos.insert`` sessions with ``308``/``Range``, ``videos.list``, ``playlistItems.list``,
revocation) to exercise ClipForge end to end through ``httpx.MockTransport``. Nothing here talks to
the network, so tests built on it say nothing about Google's real behaviour; see
docs/YOUTUBE_SETUP.md for the manual live check.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
READ_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
BOTH = f"{UPLOAD_SCOPE} {READ_SCOPE}"


@dataclass
class Account:
    channel_id: str
    title: str
    handle: str = "@kanal"
    refresh_token: str = field(default_factory=lambda: "1//rt-" + secrets.token_hex(12))
    revoked: bool = False


@dataclass
class Session:
    upload_id: str
    channel_id: str
    resource: dict[str, Any]
    size: int
    received: int = 0
    video_id: str | None = None
    hasher: Any = field(default_factory=hashlib.sha256)
    put_calls: int = 0


def error_response(status: int, reason: str, message: str = "boom", **headers: str) -> httpx.Response:
    body = {"error": {"code": status, "message": message, "errors": [{"reason": reason, "message": message}]}}
    return httpx.Response(status, json=body, headers=headers)


class FakeGoogle:
    def __init__(self) -> None:
        self.accounts: dict[str, Account] = {}  # by channel id
        self.codes: dict[str, tuple[Account, str]] = {}  # auth code -> (account, granted scope string)
        self.access: dict[str, Account] = {}  # access token -> account
        self.sessions: dict[str, Session] = {}
        self.videos: dict[str, dict[str, Any]] = {}  # video id -> resource (as returned by the API)
        self.video_channel: dict[str, str] = {}
        self.video_sha: dict[str, str] = {}  # sha256 of the bytes that were actually uploaded
        self.calls: list[tuple[str, str]] = []
        self.insert_calls = 0
        self.revoked: list[str] = []
        self.faults: dict[str, deque[Any]] = defaultdict(deque)
        self.account_faults: dict[tuple[str, str], deque[Any]] = defaultdict(deque)
        self.next_privacy_override: str | None = None  # simulate YouTube forcing a privacy value
        self.processed = False  # whether videos.list reports "processed"
        self.expire_sessions_on_query = False
        self.last_auth_params: dict[str, str] = {}

    # ---- test helpers
    def add_account(self, channel_id: str, title: str, handle: str = "@kanal") -> Account:
        acc = Account(channel_id, title, handle)
        self.accounts[channel_id] = acc
        return acc

    def grant(self, acc: Account, scope: str = BOTH) -> str:
        code = "4/code-" + secrets.token_hex(8)
        self.codes[code] = (acc, scope)
        return code

    def fault(self, kind: str, *items: Any) -> None:
        """Queue one-shot faults for an endpoint kind: an Exception is raised, a Response returned."""
        self.faults[kind].extend(items)

    def uploads_for(self, channel_id: str) -> list[str]:
        return [v for v, c in self.video_channel.items() if c == channel_id]

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    # ---- request routing
    def fault_for(self, channel_id: str, kind: str, *items: Any) -> None:
        """Like :meth:`fault` but only for requests authorised as that channel."""
        self.account_faults[(channel_id, kind)].extend(items)

    def _take(self, kind: str, acc: Account | None = None) -> httpx.Response | None:
        q = self.account_faults.get((acc.channel_id, kind)) if acc is not None else None
        if not q:
            q = self.faults.get(kind)
        if not q:
            return None
        item = q.popleft()
        if isinstance(item, BaseException):
            raise item
        return item  # type: ignore[no-any-return]

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        path, query = url.path, parse_qs(url.query)
        self.calls.append((request.method, path))
        host = url.hostname
        if host == "oauth2.googleapis.com" and path == "/token":
            return self._token(request)
        if host == "oauth2.googleapis.com" and path == "/revoke":
            form = parse_qs(request.content.decode())
            self.revoked.append(form.get("token", [""])[0])
            return httpx.Response(200, json={})
        acc = self._auth(request)
        if isinstance(acc, httpx.Response):
            return acc
        if path == "/youtube/v3/channels":
            return self._channels(acc, query)
        if path == "/youtube/v3/playlistItems":
            return self._playlist(acc)
        if path == "/youtube/v3/videos":
            return self._videos_list(acc, query)
        if path == "/upload/youtube/v3/videos" and request.method == "POST":
            return self._start(acc, request, query)
        if path == "/upload/youtube/v3/videos" and request.method == "PUT":
            return self._put(acc, request, query)
        return httpx.Response(404, json={"error": {"code": 404, "message": "unknown route"}})

    def _auth(self, request: httpx.Request) -> Account | httpx.Response:
        header = request.headers.get("authorization", "")
        acc = self.access.get(header.removeprefix("Bearer ").strip())
        if acc is None or acc.revoked:
            return error_response(401, "authError", "Invalid Credentials")
        return acc

    # ---- OAuth
    def _token(self, request: httpx.Request) -> httpx.Response:
        if (f := self._take("token")) is not None:
            return f
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if form.get("grant_type") == "authorization_code":
            entry = self.codes.pop(form.get("code", ""), None)
            if entry is None or not form.get("code_verifier"):
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Bad code"})
            acc, scope = entry
            tok = "ya29.at-" + secrets.token_hex(10)
            self.access[tok] = acc
            body: dict[str, Any] = {
                "access_token": tok,
                "expires_in": 3599,
                "scope": scope,
                "token_type": "Bearer",
            }
            if not self.faults.pop("omit_refresh_token", None):
                body["refresh_token"] = acc.refresh_token
            return httpx.Response(200, json=body)
        acc2 = next((a for a in self.accounts.values() if a.refresh_token == form.get("refresh_token")), None)
        if acc2 is None or acc2.revoked:
            return httpx.Response(
                400,
                json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."},
            )
        tok = "ya29.at-" + secrets.token_hex(10)
        self.access[tok] = acc2
        return httpx.Response(200, json={"access_token": tok, "expires_in": 3599, "scope": BOTH})

    # ---- reads
    def _channels(self, acc: Account, query: dict[str, list[str]]) -> httpx.Response:
        if (f := self._take("channels", acc)) is not None:
            return f
        part = query.get("part", [""])[0]
        item: dict[str, Any] = {"id": acc.channel_id}
        if "snippet" in part:
            item["snippet"] = {"title": acc.title, "customUrl": acc.handle}
        if "contentDetails" in part:
            item["contentDetails"] = {"relatedPlaylists": {"uploads": "UU" + acc.channel_id}}
        return httpx.Response(200, json={"items": [item]})

    def _playlist(self, acc: Account) -> httpx.Response:
        if (f := self._take("playlist", acc)) is not None:
            return f
        items = []
        for vid in self.uploads_for(acc.channel_id):
            res = self.videos[vid]
            items.append(
                {
                    "snippet": {"title": res["snippet"]["title"], "publishedAt": "2999-01-01T00:00:00Z"},
                    "contentDetails": {"videoId": vid},
                }
            )
        return httpx.Response(200, json={"items": items})

    def _videos_list(self, acc: Account, query: dict[str, list[str]]) -> httpx.Response:
        if (f := self._take("videos", acc)) is not None:
            return f
        ids = query.get("id", [""])[0].split(",")
        items = []
        for vid in ids:
            if vid in self.videos and self.video_channel.get(vid) == acc.channel_id:
                items.append(self._view(vid))
        return httpx.Response(200, json={"items": items})

    def _view(self, vid: str) -> dict[str, Any]:
        res = json.loads(json.dumps(self.videos[vid]))
        res["id"] = vid
        res["status"]["uploadStatus"] = "processed" if self.processed else "uploaded"
        res["processingDetails"] = {"processingStatus": "succeeded" if self.processed else "processing"}
        return res

    # ---- upload
    def _start(self, acc: Account, request: httpx.Request, query: dict[str, list[str]]) -> httpx.Response:
        self.insert_calls += 1
        if (f := self._take("start", acc)) is not None:
            return f
        resource = json.loads(request.content)
        size = int(request.headers["x-upload-content-length"])
        upload_id = secrets.token_hex(8)
        self.sessions[upload_id] = Session(upload_id, acc.channel_id, resource, size)
        loc = (
            f"https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id={upload_id}"
        )
        return httpx.Response(200, headers={"location": loc})

    def _finish(self, s: Session) -> httpx.Response:
        if s.video_id is None:
            s.video_id = "vid" + secrets.token_hex(4)
            res = json.loads(json.dumps(s.resource))
            if self.next_privacy_override:
                res["status"]["privacyStatus"] = self.next_privacy_override
                res["status"].pop("publishAt", None)
            self.videos[s.video_id] = res
            self.video_channel[s.video_id] = s.channel_id
            self.video_sha[s.video_id] = s.hasher.hexdigest()
        body = json.loads(json.dumps(self.videos[s.video_id]))
        body["id"] = s.video_id
        body["status"]["uploadStatus"] = "uploaded"
        return httpx.Response(201, json=body)

    def _put(self, acc: Account, request: httpx.Request, query: dict[str, list[str]]) -> httpx.Response:
        s = self.sessions.get(query.get("upload_id", [""])[0])
        crange = request.headers.get("content-range", "")
        if s is None or s.channel_id != acc.channel_id:
            return httpx.Response(404, json={"error": {"code": 404, "message": "Not Found"}})
        if crange.startswith("bytes */"):  # status query
            if (f := self._take("query", acc)) is not None:
                return f
            if self.expire_sessions_on_query and s.video_id is None:
                del self.sessions[s.upload_id]
                return httpx.Response(404, json={"error": {"code": 404, "message": "Not Found"}})
            if s.video_id is not None:
                return self._finish(s)
            return self._resume(s)
        if (f := self._take("put", acc)) is not None:
            return f
        spec, total = crange.removeprefix("bytes ").split("/")
        first, last = (int(x) for x in spec.split("-"))
        data = request.content
        if first != s.received or len(data) != last - first + 1:
            return self._resume(s)
        s.put_calls += 1
        s.hasher.update(data)
        pending = self.faults.get("put_apply_then_fail")
        if pending:
            # The bytes are applied on Google's side, but the client never sees the answer.
            item = pending.popleft()
            s.received = last + 1
            if s.received >= s.size:
                self._finish(s)
            if isinstance(item, BaseException):
                raise item
            return item  # type: ignore[no-any-return]
        s.received = last + 1
        if s.received >= int(total):
            return self._finish(s)
        return self._resume(s)

    def _resume(self, s: Session) -> httpx.Response:
        headers = {"range": f"bytes=0-{s.received - 1}"} if s.received else {}
        return httpx.Response(308, headers=headers)
