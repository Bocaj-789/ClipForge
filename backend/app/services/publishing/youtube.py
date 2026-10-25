"""Thin, testable client for Google OAuth 2.0 and the official YouTube Data API v3.

Everything goes over an injected ``httpx.Client`` so tests can mock Google completely. Only fixed
Google hosts are contacted; the resumable-upload session URI returned by Google is validated
before any request (and the bearer token) is sent to it. No scraping, no browser automation.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

import httpx

from app.core.security import redact
from app.db.models import Availability

log = logging.getLogger("clipforge.youtube")

SCOPE_UPLOAD = "https://www.googleapis.com/auth/youtube.upload"
SCOPE_READONLY = "https://www.googleapis.com/auth/youtube.readonly"
SCOPES: tuple[str, ...] = (SCOPE_UPLOAD, SCOPE_READONLY)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - public endpoint, not a secret
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API_BASE = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"

TIMEOUT = httpx.Timeout(connect=15.0, read=120.0, write=120.0, pool=15.0)

ErrorKind = Literal[
    "auth",  # access token rejected (refreshable)
    "auth_revoked",  # refresh token revoked/expired: the user must connect again
    "client",  # OAuth client misconfigured (invalid_client, redirect mismatch, API disabled)
    "scope",  # token lacks a required scope
    "quota",  # daily quota used up: wait for the Pacific-time reset
    "rate",  # short-term rate limit: retry soon
    "upload_limit",  # the channel hit YouTube's own daily upload limit
    "forbidden",  # channel/account may not do this (no channel, suspended, ...)
    "invalid",  # the request itself is wrong (title, tags, category...): retrying cannot help
    "not_found",
    "transient",  # 5xx / network: retry with backoff
    "unknown",
]


class YouTubeError(Exception):
    """A classified Google API failure. ``safe_message`` never contains credentials."""

    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        *,
        status: int | None = None,
        reason: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.safe_message = redact(message)[:300]
        self.status = status
        self.reason = reason
        self.retry_after = retry_after


# ------------------------------------------------------------------ error handling
_QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "dailyLimitExceededUnreg"}
_RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "backendError", "internalError"}
_CLIENT_REASONS = {"accessNotConfigured", "ipRefererBlocked", "keyInvalid", "invalid_client"}
_SCOPE_REASONS = {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}
_UPLOAD_LIMIT_REASONS = {"uploadLimitExceeded"}


def _body(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    if raw and raw.strip().isdigit():
        return float(min(int(raw), 24 * 3600))
    return None


def classify(resp: httpx.Response) -> YouTubeError:
    """Map a non-success Google API response to a :class:`YouTubeError`."""
    body = _body(resp)
    err = body.get("error")
    reason: str | None = None
    message = ""
    if isinstance(err, dict):
        message = str(err.get("message") or "")
        errors = err.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            reason = str(errors[0].get("reason") or "") or None
        details = err.get("details")
        if reason is None and isinstance(details, list):
            for d in details:
                if isinstance(d, dict) and d.get("reason"):
                    reason = str(d["reason"])
                    break
    elif isinstance(err, str):  # OAuth-style {"error": "invalid_grant", "error_description": "..."}
        reason = err
        message = str(body.get("error_description") or err)
    st = resp.status_code
    ra = _retry_after(resp)
    message = message or f"YouTube svarade HTTP {st}."
    if reason == "invalid_grant":
        return YouTubeError("auth_revoked", message, status=st, reason=reason)
    if reason in _CLIENT_REASONS or reason in {"unauthorized_client", "redirect_uri_mismatch"}:
        return YouTubeError("client", message, status=st, reason=reason)
    if reason in _QUOTA_REASONS:
        return YouTubeError("quota", message, status=st, reason=reason)
    if reason in _UPLOAD_LIMIT_REASONS:
        return YouTubeError("upload_limit", message, status=st, reason=reason)
    if reason in _SCOPE_REASONS:
        return YouTubeError("scope", message, status=st, reason=reason)
    if st == 401:
        return YouTubeError("auth", message, status=st, reason=reason)
    if st in (408, 429) or reason in _RATE_REASONS:
        return YouTubeError("rate", message, status=st, reason=reason, retry_after=ra)
    if st >= 500:
        return YouTubeError("transient", message, status=st, reason=reason, retry_after=ra)
    if st == 403:
        return YouTubeError("forbidden", message, status=st, reason=reason)
    if st == 404:
        return YouTubeError("not_found", message, status=st, reason=reason)
    if st == 400:
        return YouTubeError("invalid", message, status=st, reason=reason)
    return YouTubeError("unknown", message, status=st, reason=reason)


def _transport_error(e: httpx.HTTPError) -> YouTubeError:
    return YouTubeError("transient", f"Nätverksfel mot Google ({type(e).__name__}).")


# ------------------------------------------------------------------ OAuth
@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    client_secret: str = field(repr=False)


def new_state() -> str:
    return secrets.token_urlsafe(32)


def new_code_verifier() -> str:
    return secrets.token_urlsafe(64)[:96]  # RFC 7636: 43..128 characters


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_auth_url(client: OAuthClient, *, redirect_uri: str, state: str, verifier: str) -> str:
    params = {
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": code_challenge(verifier),
        "code_challenge_method": "S256",
        # offline + consent => Google always returns a refresh token; select_account lets the user
        # pick which Google account (and thereby which YouTube channel) to connect.
        "access_type": "offline",
        "prompt": "consent select_account",
        "include_granted_scopes": "false",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


@dataclass(frozen=True)
class TokenResponse:
    access_token: str = field(repr=False)
    expires_in: int
    scopes: tuple[str, ...]
    refresh_token: str | None = field(default=None, repr=False)


def _token_response(resp: httpx.Response) -> TokenResponse:
    if resp.status_code != 200:
        raise classify(resp)
    body = _body(resp)
    access = body.get("access_token")
    if not isinstance(access, str) or not access:
        raise YouTubeError("unknown", "Google returnerade ingen åtkomsttoken.")
    refresh = body.get("refresh_token")
    scopes = tuple(str(body.get("scope") or "").split())
    return TokenResponse(
        access_token=access,
        expires_in=int(body.get("expires_in") or 3600),
        scopes=scopes,
        refresh_token=refresh if isinstance(refresh, str) and refresh else None,
    )


def exchange_code(
    http: httpx.Client, client: OAuthClient, *, code: str, verifier: str, redirect_uri: str
) -> TokenResponse:
    try:
        resp = http.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
                "client_id": client.client_id,
                "client_secret": client.client_secret,
            },
        )
    except httpx.HTTPError as e:
        raise _transport_error(e) from e
    return _token_response(resp)


def refresh_access_token(http: httpx.Client, client: OAuthClient, refresh_token: str) -> TokenResponse:
    try:
        resp = http.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client.client_id,
                "client_secret": client.client_secret,
            },
        )
    except httpx.HTTPError as e:
        raise _transport_error(e) from e
    return _token_response(resp)


def revoke_token(http: httpx.Client, token: str) -> bool:
    """Best effort. Returns True if Google confirmed (or the token was already invalid)."""
    try:
        resp = http.post(REVOKE_URL, data={"token": token})
    except httpx.HTTPError:
        return False
    return resp.status_code in (200, 400)


# ------------------------------------------------------------------ API client
@dataclass(frozen=True)
class ChannelInfo:
    channel_id: str
    title: str
    handle: str | None


@dataclass
class SessionState:
    """Result of querying/continuing a resumable upload."""

    video: dict[str, Any] | None = None  # set when YouTube finished and returned the video resource
    committed: int = 0  # bytes YouTube has durably received
    gone: bool = False  # session expired or unknown (404/410)


Charge = Callable[[str, int], None]
TokenFn = Callable[[bool], str]  # (force_refresh) -> access token


def _noop_charge(_bucket: str, _n: int) -> None:
    return None


def valid_session_uri(uri: str) -> bool:
    p = urlsplit(uri)
    host = (p.hostname or "").lower()
    return p.scheme == "https" and (host == "googleapis.com" or host.endswith(".googleapis.com"))


class YouTubeApi:
    """Authorised calls for one channel. ``charge(bucket, n)`` records quota use before each call."""

    def __init__(self, http: httpx.Client, token: TokenFn, charge: Charge = _noop_charge) -> None:
        self.http = http
        self._token = token
        self._charge = charge

    # -- plumbing
    def _send(
        self,
        method: str,
        url: str,
        *,
        bucket: str | None = "units",
        cost: int = 1,
        headers: dict[str, str] | None = None,
        **kw: Any,
    ) -> httpx.Response:
        """Send an authorised request; on 401 refresh the access token once and retry."""
        for attempt in (0, 1):
            if bucket:
                self._charge(bucket, cost)
            h = {"Authorization": f"Bearer {self._token(attempt == 1)}", **(headers or {})}
            try:
                resp = self.http.request(method, url, headers=h, **kw)
            except httpx.HTTPError as e:
                raise _transport_error(e) from e
            if resp.status_code == 401 and attempt == 0:
                continue
            return resp
        raise AssertionError("unreachable")  # pragma: no cover

    def _json(self, resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code >= 300:
            raise classify(resp)
        return _body(resp)

    # -- reads (1 quota unit each)
    def my_channel(self) -> ChannelInfo:
        data = self._json(
            self._send("GET", f"{API_BASE}/channels", params={"part": "snippet", "mine": "true"})
        )
        items = data.get("items") or []
        if not items:
            raise YouTubeError(
                "forbidden",
                "Google-kontot har ingen YouTube-kanal. Skapa en kanal på youtube.com och försök igen.",
                reason="youtubeSignupRequired",
            )
        item = items[0]
        snippet = item.get("snippet") or {}
        return ChannelInfo(
            channel_id=str(item.get("id")),
            title=str(snippet.get("title") or "YouTube-kanal")[:200],
            handle=(str(snippet["customUrl"])[:100] if snippet.get("customUrl") else None),
        )

    def videos_status(self, ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """One call (1 unit) for up to 50 ids. Missing ids are simply absent from the result."""
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(ids), 50):
            batch = list(ids[i : i + 50])
            data = self._json(
                self._send(
                    "GET",
                    f"{API_BASE}/videos",
                    params={"part": "status,processingDetails", "id": ",".join(batch)},
                )
            )
            for item in data.get("items") or []:
                if isinstance(item, dict) and item.get("id"):
                    out[str(item["id"])] = item
        return out

    def recent_uploads(self, limit: int = 15) -> list[dict[str, Any]]:
        """Newest items of the channel's uploads playlist (2 units): used to reconcile lost uploads."""
        data = self._json(
            self._send("GET", f"{API_BASE}/channels", params={"part": "contentDetails", "mine": "true"})
        )
        items = data.get("items") or []
        playlist = (
            ((items[0] if items else {}).get("contentDetails") or {}).get("relatedPlaylists") or {}
        ).get("uploads")
        if not playlist:
            return []
        page = self._json(
            self._send(
                "GET",
                f"{API_BASE}/playlistItems",
                params={
                    "part": "snippet,contentDetails",
                    "playlistId": playlist,
                    "maxResults": min(limit, 50),
                },
            )
        )
        return [x for x in (page.get("items") or []) if isinstance(x, dict)]

    # -- resumable upload
    def start_resumable(
        self, resource: dict[str, Any], *, size: int, mime: str, notify_subscribers: bool
    ) -> str:
        resp = self._send(
            "POST",
            UPLOAD_URL,
            bucket="insert",
            params={
                "uploadType": "resumable",
                "part": "snippet,status",
                "notifySubscribers": "true" if notify_subscribers else "false",
            },
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(size),
                "X-Upload-Content-Type": mime,
            },
            json=resource,
        )
        if resp.status_code != 200:
            raise classify(resp)
        uri = resp.headers.get("location", "")
        if not valid_session_uri(uri):
            raise YouTubeError("unknown", "Google returnerade ingen giltig uppladdningssession.")
        return uri

    def _session_response(self, resp: httpx.Response) -> SessionState:
        if resp.status_code in (200, 201):
            video = _body(resp)
            if not video.get("id"):
                raise YouTubeError("unknown", "Uppladdningen slutfördes men svaret saknade video-id.")
            return SessionState(video=video)
        if resp.status_code == 308:
            rng = resp.headers.get("range", "")
            committed = 0
            if rng.startswith("bytes=") and "-" in rng:
                last = rng.split("=", 1)[1].split("-", 1)[1]
                if last.isdigit():
                    committed = int(last) + 1
            return SessionState(committed=committed)
        if resp.status_code in (404, 410):
            return SessionState(gone=True)
        raise classify(resp)

    def query_session(self, uri: str, size: int) -> SessionState:
        """Ask YouTube how much of the file it has (and whether it already finished)."""
        if not valid_session_uri(uri):
            return SessionState(gone=True)
        resp = self._send(
            "PUT",
            uri,
            bucket=None,
            headers={"Content-Length": "0", "Content-Range": f"bytes */{size}"},
            content=b"",
        )
        return self._session_response(resp)

    def put_chunk(self, uri: str, data: bytes, *, start: int, total: int, mime: str) -> SessionState:
        if not valid_session_uri(uri):
            return SessionState(gone=True)
        end = start + len(data) - 1
        resp = self._send(
            "PUT",
            uri,
            bucket=None,
            headers={"Content-Range": f"bytes {start}-{end}/{total}", "Content-Type": mime},
            content=data,
        )
        return self._session_response(resp)


# ------------------------------------------------------------------ video resource helpers
def rfc3339(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def build_video_resource(
    *,
    title: str,
    description: str,
    tags: list[str],
    category_id: str,
    language: str | None,
    privacy: str,
    made_for_kids: bool,
    publish_at: datetime | None,
) -> dict[str, Any]:
    snippet: dict[str, Any] = {
        "title": title,
        "description": description,
        "categoryId": category_id,
    }
    if tags:
        snippet["tags"] = tags
    if language:
        snippet["defaultLanguage"] = language
        snippet["defaultAudioLanguage"] = language
    status: dict[str, Any] = {"privacyStatus": privacy, "selfDeclaredMadeForKids": made_for_kids}
    if publish_at is not None:
        # YouTube only accepts publishAt on a private video; it flips to public at that time.
        status["privacyStatus"] = "private"
        status["publishAt"] = rfc3339(publish_at)
    return {"snippet": snippet, "status": status}


@dataclass(frozen=True)
class VideoState:
    availability: str
    privacy: str | None
    upload_status: str | None
    rejection_reason: str | None


def derive_availability(item: dict[str, Any]) -> VideoState:
    """Translate a ``videos`` resource (status + processingDetails) into what a viewer would see."""
    st = item.get("status") or {}
    pd = item.get("processingDetails") or {}
    upload = st.get("uploadStatus")
    privacy = st.get("privacyStatus")
    processing = pd.get("processingStatus")
    reason = st.get("rejectionReason") or st.get("failureReason") or pd.get("processingFailureReason")
    reason = str(reason)[:100] if reason else None
    if upload == "deleted":
        avail = Availability.MISSING
    elif upload == "rejected":
        avail = Availability.REJECTED
    elif upload == "failed" or processing in ("failed", "terminated"):
        avail = Availability.FAILED
    elif upload == "uploaded" or processing == "processing":
        avail = Availability.PROCESSING
    elif privacy == "public":
        avail = Availability.PUBLIC
    elif privacy == "unlisted":
        avail = Availability.UNLISTED
    elif privacy == "private":
        avail = Availability.SCHEDULED if st.get("publishAt") else Availability.PRIVATE
    else:
        avail = Availability.UNKNOWN
    return VideoState(
        availability=avail,
        privacy=str(privacy) if privacy else None,
        upload_status=str(upload) if upload else None,
        rejection_reason=reason,
    )
