"""Connecting and disconnecting YouTube channels (Google OAuth 2.0 authorization-code flow + PKCE).

Security properties:
* ``state`` is 256 bits of randomness, stored only as a SHA-256 hash, single use, valid for 10 minutes.
* The state is bound to the browser that started the flow through a ``Lax`` cookie, so a callback
  that was not initiated by this browser is refused (the session cookie is ``Strict`` and is not
  sent on the cross-site redirect back from Google, so the callback cannot rely on it).
* PKCE (S256) protects the authorization code; the code verifier never leaves the database.
* The refresh token goes to the OS secret store and nowhere else: not the DB, logs, audit events
  or API responses.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.core.security import clean_text
from app.db.base import utcnow
from app.db.models import Channel, ConnectionState, OAuthState
from app.services.audit import service as audit
from app.services.publishing.deps import PublishDeps
from app.services.publishing.quota import make_charge
from app.services.publishing.secret_store import (
    SecretStoreError,
    dump_token,
    load_refresh_token,
)
from app.services.publishing.youtube import (
    SCOPES,
    YouTubeApi,
    YouTubeError,
    build_auth_url,
    exchange_code,
    new_code_verifier,
    new_state,
    revoke_token,
    sha256_hex,
)
from app.services.settings_service import load_settings

log = logging.getLogger("clipforge.oauth")

STATE_TTL = timedelta(minutes=10)
BROWSER_COOKIE = "cf_oauth"


class OAuthFlowError(Exception):
    """A flow failure with a stable machine code (shown to the user as a Swedish message by the UI)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message


@dataclass(frozen=True)
class StartResult:
    auth_url: str
    browser_token: str
    expires_in_seconds: int


def start(db: Session, deps: PublishDeps, *, channel_id: str | None = None) -> StartResult:
    client = deps.oauth_client()
    if client is None:
        raise OAuthFlowError(
            "client_not_configured",
            "Google OAuth-klienten är inte konfigurerad. Följ docs/YOUTUBE_SETUP.md.",
        )
    ok, why = deps.store.available()
    if not ok:
        raise OAuthFlowError(
            "store_unavailable",
            (why or "Ingen säker lagring för tokens är tillgänglig.")
            + " ClipForge sparar aldrig tokens i klartext i databasen.",
        )
    if channel_id is not None and db.get(Channel, channel_id) is None:
        raise OAuthFlowError("channel_not_found", "Kanalen hittades inte.")
    now = utcnow()
    db.execute(delete(OAuthState).where(OAuthState.expires_at < now - timedelta(hours=1)))
    state, verifier, browser = new_state(), new_code_verifier(), secrets.token_urlsafe(32)
    redirect = deps.cfg.oauth_redirect_uri
    db.add(
        OAuthState(
            state_hash=sha256_hex(state),
            browser_hash=sha256_hex(browser),
            code_verifier=verifier,
            channel_id=channel_id,
            redirect_uri=redirect,
            expires_at=now + STATE_TTL,
        )
    )
    db.commit()
    url = build_auth_url(client, redirect_uri=redirect, state=state, verifier=verifier)
    return StartResult(url, browser, int(STATE_TTL.total_seconds()))


def _consume_state(db: Session, state: str | None, browser_token: str | None) -> OAuthState:
    if not state or len(state) > 200:
        raise OAuthFlowError("invalid_state", "Ogiltig eller saknad state. Starta anslutningen igen.")
    h = sha256_hex(state)
    row = db.get(OAuthState, h)
    if row is None:
        raise OAuthFlowError("invalid_state", "Okänd state. Starta anslutningen igen.")
    now = utcnow()
    # Atomic single-use: only one concurrent callback can flip consumed_at from NULL.
    res = db.execute(
        update(OAuthState)
        .where(OAuthState.state_hash == h, OAuthState.consumed_at.is_(None))
        .values(consumed_at=now)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    if res.rowcount != 1:  # type: ignore[attr-defined]
        raise OAuthFlowError("invalid_state", "Den här anslutningen har redan använts. Starta om den.")
    db.refresh(row)
    if row.expires_at < now:
        raise OAuthFlowError("expired_state", "Anslutningen tog för lång tid. Starta om den.")
    if not browser_token or not hmac.compare_digest(sha256_hex(browser_token), row.browser_hash):
        raise OAuthFlowError(
            "invalid_state", "Anslutningen startades inte i den här webbläsaren. Starta om den."
        )
    return row


def _map_google_error(e: YouTubeError) -> OAuthFlowError:
    if e.reason == "youtubeSignupRequired":
        return OAuthFlowError("no_channel", e.safe_message)
    if e.kind in ("client",):
        return OAuthFlowError(
            "client_config",
            "Google avvisade OAuth-klienten. Kontrollera klient-id, hemlighet och redirect-URI.",
        )
    if e.kind == "auth_revoked":
        return OAuthFlowError("token_exchange_failed", "Google avvisade koden. Starta anslutningen igen.")
    return OAuthFlowError("google_error", f"Google svarade med ett fel: {e.safe_message}")


def complete(
    db: Session,
    deps: PublishDeps,
    *,
    state: str | None,
    code: str | None,
    error: str | None,
    browser_token: str | None,
) -> Channel:
    row = _consume_state(db, state, browser_token)
    if error:
        raise OAuthFlowError(
            "access_denied" if error == "access_denied" else "google_error",
            "Du avbröt anslutningen hos Google."
            if error == "access_denied"
            else "Google avbröt anslutningen.",
        )
    if not code or len(code) > 2048:
        raise OAuthFlowError("invalid_state", "Svaret från Google saknade kod.")
    client = deps.oauth_client()
    if client is None:
        raise OAuthFlowError("client_not_configured", "Google OAuth-klienten är inte konfigurerad.")
    settings = load_settings(db)
    with deps.http_factory() as http:
        try:
            tr = exchange_code(
                http, client, code=code, verifier=row.code_verifier, redirect_uri=row.redirect_uri
            )
        except YouTubeError as e:
            raise _map_google_error(e) from e
        granted = set(tr.scopes)
        if not set(SCOPES) <= granted:
            # The user unticked a permission on Google's consent screen: undo what was granted.
            revoke_token(http, tr.refresh_token or tr.access_token)
            raise OAuthFlowError(
                "scopes_missing",
                "Du måste godkänna både uppladdning och läsning av kanalinformation. "
                "Försök igen och låt alla rutor vara ikryssade.",
            )
        if not tr.refresh_token:
            revoke_token(http, tr.access_token)
            raise OAuthFlowError(
                "no_refresh_token",
                "Google skickade ingen långlivad token. Ta bort ClipForge under "
                "myaccount.google.com/permissions och försök igen.",
            )
        access = tr.access_token
        api = YouTubeApi(http, lambda _force: access, make_charge(db, settings))
        try:
            info = api.my_channel()
        except YouTubeError as e:
            revoke_token(http, tr.refresh_token)
            raise _map_google_error(e) from e

        existing = db.scalar(select(Channel).where(Channel.external_channel_id == info.channel_id))
        target = db.get(Channel, row.channel_id) if row.channel_id else None
        if existing is not None and target is not None and existing.id != target.id:
            revoke_token(http, tr.refresh_token)
            raise OAuthFlowError(
                "channel_in_use",
                f'YouTube-kanalen "{info.title}" är redan ansluten som "{existing.display_name}".',
            )
        if (
            target is not None
            and target.external_channel_id
            and target.external_channel_id != info.channel_id
        ):
            revoke_token(http, tr.refresh_token)
            raise OAuthFlowError(
                "wrong_channel",
                f'Du valde "{info.title}", men den här kanalen är kopplad till en annan YouTube-kanal. '
                "Välj samma kanal hos Google eller lägg till en ny kanal i ClipForge.",
            )
        channel = existing or target
        created = channel is None
        if channel is None:
            # A channel added through "connect" takes part in automatic publishing right away (it only
            # acts once the global switch is on), privately, with limits sized for several clips a day.
            channel = Channel(
                display_name=clean_text(info.title, 200) or "YouTube-kanal",
                auto_publish_enabled=True,
                auto_publish_min_score=0.0,
                max_uploads_per_day=10,
                min_spacing_minutes=10,
            )
            db.add(channel)
            db.flush()
        ref = deps.store.make_ref(f"youtube/{channel.id}")
        try:
            deps.store.set(ref, dump_token(tr.refresh_token, sorted(granted)))
        except SecretStoreError as e:
            db.rollback()
            revoke_token(http, tr.refresh_token)
            raise OAuthFlowError("store_unavailable", f"{e} Kanalen anslöts inte.") from e
    channel.external_channel_id = info.channel_id
    channel.youtube_title = clean_text(info.title, 200)
    channel.youtube_handle = clean_text(info.handle, 100) if info.handle else None
    channel.token_reference = ref
    channel.oauth_connected_at = utcnow()
    channel.connection_state = ConnectionState.CONNECTED
    channel.connection_error = None
    channel.granted_scopes_json = sorted(granted)
    channel.enabled = True
    deps.forget_token(channel.id)
    audit.record(
        db,
        "channel.connected",
        actor="user",
        object_type="channel",
        object_id=channel.id,
        details={"youtube_channel_id": info.channel_id, "new": created, "store": deps.store.name},
    )
    db.commit()
    return channel


def disconnect(db: Session, deps: PublishDeps, channel: Channel) -> bool:
    """Revoke at Google (best effort), delete the stored token and turn off automation.

    Returns True when Google confirmed the revocation.
    """
    revoked = False
    ref = channel.token_reference
    if ref:
        try:
            refresh = load_refresh_token(deps.store.get(ref))
        except SecretStoreError:
            refresh = None
        if refresh:
            try:
                with deps.http_factory() as http:
                    revoked = revoke_token(http, refresh)
            except Exception:  # noqa: BLE001 - revocation must never block a local disconnect
                log.warning("token revocation request failed", extra={"channel_id": channel.id})
        try:
            deps.store.delete(ref)
        except SecretStoreError:
            log.warning("could not delete stored token", extra={"channel_id": channel.id})
    channel.token_reference = None
    channel.connection_state = ConnectionState.DISCONNECTED
    channel.connection_error = None
    channel.oauth_connected_at = None
    channel.granted_scopes_json = []
    channel.auto_publish_enabled = False
    deps.forget_token(channel.id)
    audit.record(
        db,
        "channel.disconnected",
        actor="user",
        object_type="channel",
        object_id=channel.id,
        details={"revoked_at_google": revoked},
    )
    return revoked


def mark_needs_reauth(db: Session, channel: Channel, message: str) -> None:
    """The refresh token no longer works: stop automation and ask the user to connect again."""
    channel.connection_state = ConnectionState.NEEDS_REAUTH
    channel.connection_error = message[:500]
    channel.auto_publish_enabled = False
    audit.record(
        db,
        "channel.needs_reauth",
        object_type="channel",
        object_id=channel.id,
        severity="warning",
        details={"reason": message[:200]},
    )
