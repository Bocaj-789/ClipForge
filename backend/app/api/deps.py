from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.config import Config
from app.core import auth
from app.core.errors import AppError, ErrorCode
from app.db.models import AuthSession, User
from app.db.session import new_session

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def get_db() -> Iterator[Session]:
    db = new_session()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_cfg(request: Request) -> Config:
    return request.app.state.cfg


@dataclass
class Principal:
    user: User
    session: AuthSession


def require_user(request: Request, db: Session = Depends(get_db)) -> Principal:
    found = auth.lookup_session(db, request.cookies.get(auth.SESSION_COOKIE))
    if found is None:
        raise AppError(ErrorCode.UNAUTHORIZED, "Du behöver logga in.", status_code=401)
    sess, user = found
    if request.method not in SAFE_METHODS and not auth.csrf_ok(sess, request.headers.get(auth.CSRF_HEADER)):
        raise AppError(ErrorCode.CSRF_FAILED, "Ogiltig CSRF-token. Ladda om sidan.", status_code=403)
    return Principal(user, sess)


def client_key(request: Request) -> str:
    # Behind a local tunnel every request arrives from loopback; use the forwarded client IP
    # only when the direct peer is loopback (the tunnel daemon), never from remote peers.
    peer = request.client.host if request.client else "unknown"
    if peer in ("127.0.0.1", "::1"):
        fwd = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()[:64]
    return peer
