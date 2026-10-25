"""Google OAuth 2.0 for YouTube: start (authenticated) and callback (browser redirect from Google).

The callback cannot rely on the session cookie: it is ``SameSite=Strict`` and the redirect back
from accounts.google.com is a cross-site navigation, so the browser does not send it. The callback is
instead protected by the single-use ``state`` (256-bit, stored hashed) plus a ``Lax`` cookie that
binds the flow to the browser that started it, and by PKCE. Tokens never appear in any response.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_db, require_user
from app.core.errors import AppError, ErrorCode
from app.services.audit import service as audit
from app.services.publishing import oauth
from app.services.publishing.deps import PublishDeps

log = logging.getLogger("clipforge.oauth")
router = APIRouter(prefix="/oauth/youtube", tags=["oauth"])
COOKIE_PATH = "/api/v1/oauth/youtube"


class StartIn(BaseModel):
    # Reconnect an existing channel entry; omit to add a new one.
    channel_id: str | None = Field(default=None, max_length=32)


def _deps(request: Request) -> PublishDeps:
    return request.app.state.publish_deps


@router.post("/start")
def start(
    body: StartIn,
    request: Request,
    response: Response,
    _: Principal = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        res = oauth.start(db, _deps(request), channel_id=body.channel_id)
    except oauth.OAuthFlowError as e:
        raise AppError(ErrorCode.INVALID_INPUT, e.safe_message) from e
    secure = request.url.scheme == "https"
    response.set_cookie(
        oauth.BROWSER_COOKIE,
        res.browser_token,
        max_age=res.expires_in_seconds,
        httponly=True,
        samesite="lax",  # must accompany the top-level redirect back from Google
        secure=secure,
        path=COOKIE_PATH,
    )
    audit.record(db, "oauth.started", actor="user", object_type="channel", object_id=body.channel_id)
    return {"auth_url": res.auth_url, "expires_in": res.expires_in_seconds}


@router.get("/callback", include_in_schema=False)
def callback(
    request: Request,
    state: str | None = Query(default=None, max_length=300),
    code: str | None = Query(default=None, max_length=2100),
    error: str | None = Query(default=None, max_length=100),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Finish the flow, then bounce to the dashboard with a short status code (never a token)."""
    try:
        channel = oauth.complete(
            db,
            _deps(request),
            state=state,
            code=code,
            error=error,
            browser_token=request.cookies.get(oauth.BROWSER_COOKIE),
        )
        target = f"/kanaler?connected={channel.id}"
    except oauth.OAuthFlowError as e:
        log.info("oauth callback refused: %s", e.code)
        audit.record(db, "oauth.failed", result=e.code, severity="warning", details={"code": e.code})
        db.commit()
        target = f"/kanaler?oauth_error={e.code}"
    except Exception:
        log.exception("oauth callback crashed")
        db.rollback()
        target = "/kanaler?oauth_error=internal"
    resp = RedirectResponse(target, status_code=303)
    resp.delete_cookie(oauth.BROWSER_COOKIE, path=COOKIE_PATH)
    resp.headers["Cache-Control"] = "no-store"
    return resp
