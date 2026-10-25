from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import Principal, client_key, get_db, require_user
from app.core import auth
from app.core.errors import AppError, ErrorCode
from app.db.models import User
from app.services.audit import service as audit

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class SetupIn(LoginIn):
    setup_token: str = Field(min_length=8, max_length=100)


class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


def _is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    peer = request.client.host if request.client else ""
    return peer in ("127.0.0.1", "::1") and request.headers.get("x-forwarded-proto") == "https"


def _set_cookie(request: Request, response: Response, token: str, ttl_hours: int) -> None:
    response.set_cookie(
        auth.SESSION_COOKIE,
        token,
        max_age=ttl_hours * 3600,
        httponly=True,
        samesite="strict",
        secure=_is_https(request),
        path="/",
    )


@router.get("/me")
def me(request: Request, db: Session = Depends(get_db)) -> dict:
    found = auth.lookup_session(db, request.cookies.get(auth.SESSION_COOKIE))
    if found is None:
        return {"authenticated": False, "setup_required": not auth.user_exists(db)}
    sess, user = found
    return {
        "authenticated": True,
        "setup_required": False,
        "username": user.username,
        "csrf_token": sess.csrf_token,
        "expires_at": sess.expires_at,
    }


@router.post("/setup")
def setup(body: SetupIn, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    wait = request.app.state.setup_limiter.hit(client_key(request))
    if wait:
        raise AppError(ErrorCode.RATE_LIMITED, "För många försök. Vänta en stund.", status_code=429)
    if auth.user_exists(db):
        raise AppError(ErrorCode.CONFLICT, "Kontot är redan skapat.", status_code=409)
    st: auth.SetupToken = request.app.state.setup_token
    if not st.check(body.setup_token):
        audit.record(db, "auth.setup_failed", actor="user", result="denied", severity="warning")
        raise AppError(ErrorCode.FORBIDDEN, "Fel installationskod.", status_code=403)
    problem = auth.password_problem(body.password, body.username)
    if problem:
        raise AppError(ErrorCode.INVALID_INPUT, problem)
    user = User(username=body.username.strip(), password_hash=auth.hash_password(body.password))
    db.add(user)
    db.flush()
    token, sess = auth.create_session(
        db, user, request.app.state.cfg.session_ttl_hours, request.headers.get("user-agent")
    )
    st.consume(request.app.state.cfg.secrets_dir)
    audit.record(db, "auth.setup_completed", actor="user", object_type="user", object_id=user.id)
    _set_cookie(request, response, token, request.app.state.cfg.session_ttl_hours)
    return {"ok": True, "csrf_token": sess.csrf_token}


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    key = client_key(request)
    wait = request.app.state.login_limiter.hit(key)
    if wait:
        raise AppError(
            ErrorCode.RATE_LIMITED,
            f"För många inloggningsförsök. Försök igen om {int(wait) + 1} s.",
            status_code=429,
        )
    user = auth.authenticate(db, body.username.strip(), body.password)
    if user is None:
        audit.record(db, "auth.login_failed", actor="user", result="denied", severity="warning")
        raise AppError(ErrorCode.UNAUTHORIZED, "Fel användarnamn eller lösenord.", status_code=401)
    request.app.state.login_limiter.reset(key)
    token, sess = auth.create_session(
        db, user, request.app.state.cfg.session_ttl_hours, request.headers.get("user-agent")
    )
    audit.record(db, "auth.login", actor="user", object_type="user", object_id=user.id)
    _set_cookie(request, response, token, request.app.state.cfg.session_ttl_hours)
    return {"ok": True, "csrf_token": sess.csrf_token}


@router.post("/logout")
def logout(
    request: Request, response: Response, p: Principal = Depends(require_user), db: Session = Depends(get_db)
) -> dict:
    auth.revoke_session(db, request.cookies.get(auth.SESSION_COOKIE))
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


@router.post("/logout-all")
def logout_all(
    response: Response, p: Principal = Depends(require_user), db: Session = Depends(get_db)
) -> dict:
    n = auth.revoke_all(db, p.user.id)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    audit.record(
        db, "auth.logout_all", actor="user", object_type="user", object_id=p.user.id, details={"n": n}
    )
    return {"ok": True, "revoked": n}


@router.post("/password")
def change_password(
    body: ChangePasswordIn,
    request: Request,
    response: Response,
    p: Principal = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict:
    user = db.get(User, p.user.id)
    assert user is not None
    if not auth.verify_password(user.password_hash, body.current_password):
        raise AppError(ErrorCode.UNAUTHORIZED, "Nuvarande lösenord är fel.", status_code=401)
    problem = auth.password_problem(body.new_password, user.username)
    if problem:
        raise AppError(ErrorCode.INVALID_INPUT, problem)
    user.password_hash = auth.hash_password(body.new_password)
    auth.revoke_all(db, user.id)
    token, sess = auth.create_session(
        db, user, request.app.state.cfg.session_ttl_hours, request.headers.get("user-agent")
    )
    _set_cookie(request, response, token, request.app.state.cfg.session_ttl_hours)
    audit.record(db, "auth.password_changed", actor="user", object_type="user", object_id=user.id)
    return {"ok": True, "csrf_token": sess.csrf_token}
