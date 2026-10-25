"""Wiring for everything that talks to Google: HTTP client, secret store, token cache, clock.

One :class:`PublishDeps` is created per process (API and worker). Tests build their own with a
mock transport and an in-memory secret store.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
from sqlalchemy.orm import Session

from app.config import Config
from app.db.models import Channel
from app.services.publishing.quota import make_charge
from app.services.publishing.secret_store import (
    SecretStore,
    SecretStoreError,
    get_secret_store,
    load_refresh_token,
)
from app.services.publishing.youtube import (
    SCOPE_UPLOAD,
    TIMEOUT,
    OAuthClient,
    YouTubeApi,
    YouTubeError,
    refresh_access_token,
)
from app.services.settings_service import AppSettings

log = logging.getLogger("clipforge.publishing")

CHUNK_SIZE = 8 * 1024 * 1024  # multiple of 256 KiB, as the resumable protocol requires


def default_http() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, follow_redirects=False)


@dataclass
class PublishDeps:
    cfg: Config
    store: SecretStore
    http_factory: Callable[[], httpx.Client] = default_http
    sleep: Callable[[float], None] = time.sleep
    chunk_size: int = CHUNK_SIZE
    chunk_retries: int = 4
    _tokens: dict[str, tuple[str, float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- OAuth client
    def oauth_client(self) -> OAuthClient | None:
        found = self.cfg.google_oauth_client()
        return OAuthClient(*found) if found else None

    # -- tokens
    def forget_token(self, channel_id: str) -> None:
        with self._lock:
            self._tokens.pop(channel_id, None)

    def _access_token(self, http: httpx.Client, channel: Channel, force: bool) -> str:
        with self._lock:
            cached = self._tokens.get(channel.id)
            if cached and not force and cached[1] > time.monotonic() + 60:
                return cached[0]
        client = self.oauth_client()
        if client is None:
            raise YouTubeError("client", "Google OAuth-klienten är inte konfigurerad.")
        if not channel.token_reference:
            raise YouTubeError("auth_revoked", "Kanalen är inte ansluten.")
        try:
            refresh = load_refresh_token(self.store.get(channel.token_reference))
        except SecretStoreError as e:
            raise YouTubeError("client", str(e)) from e
        if not refresh:
            raise YouTubeError("auth_revoked", "Åtkomsttoken saknas i nyckelringen. Anslut kanalen igen.")
        tr = refresh_access_token(http, client, refresh)
        if tr.scopes and SCOPE_UPLOAD not in tr.scopes:
            raise YouTubeError("scope", "Behörigheten för uppladdning saknas. Anslut kanalen igen.")
        with self._lock:
            self._tokens[channel.id] = (tr.access_token, time.monotonic() + tr.expires_in)
        return tr.access_token

    def api_for(self, db: Session, settings: AppSettings, channel: Channel, http: httpx.Client) -> YouTubeApi:
        return YouTubeApi(
            http,
            lambda force: self._access_token(http, channel, force),
            make_charge(db, settings),
        )


def make_deps(cfg: Config) -> PublishDeps:
    return PublishDeps(cfg=cfg, store=get_secret_store(cfg))
