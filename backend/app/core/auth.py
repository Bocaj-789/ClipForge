"""Single-user authentication: Argon2id passwords, server-side sessions, CSRF, rate limiting."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import AuthSession, User

SESSION_COOKIE = "cf_session"
CSRF_HEADER = "x-csrf-token"
MIN_PASSWORD_LENGTH = 12

_hasher = PasswordHasher()  # argon2id with library-maintained parameters


def hash_password(pw: str) -> str:
    return _hasher.hash(pw)


def verify_password(stored: str, pw: str) -> bool:
    try:
        return _hasher.verify(stored, pw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def password_problem(pw: str, username: str) -> str | None:
    if len(pw) < MIN_PASSWORD_LENGTH:
        return f"Lösenordet måste vara minst {MIN_PASSWORD_LENGTH} tecken."
    if len(pw) > 256:
        return "Lösenordet är för långt."
    if username.lower() in pw.lower():
        return "Lösenordet får inte innehålla användarnamnet."
    if len(set(pw)) < 6:
        return "Lösenordet är för enkelt."
    return None


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def user_exists(db: Session) -> bool:
    return db.scalar(select(User.id).limit(1)) is not None


def create_session(
    db: Session, user: User, ttl_hours: int, user_agent: str | None
) -> tuple[str, AuthSession]:
    token = secrets.token_urlsafe(32)
    s = AuthSession(
        id_hash=_digest(token),
        user_id=user.id,
        csrf_token=secrets.token_urlsafe(24),
        expires_at=utcnow() + timedelta(hours=ttl_hours),
        user_agent=(user_agent or "")[:300],
    )
    db.add(s)
    return token, s


def lookup_session(db: Session, token: str | None) -> tuple[AuthSession, User] | None:
    if not token or len(token) > 200:
        return None
    s = db.get(AuthSession, _digest(token))
    if s is None or s.revoked_at is not None or s.expires_at <= utcnow():
        return None
    user = db.get(User, s.user_id)
    if user is None:
        return None
    now = utcnow()
    if (now - s.last_seen_at).total_seconds() > 60:
        s.last_seen_at = now
    return s, user


def revoke_session(db: Session, token: str | None) -> None:
    if not token:
        return
    s = db.get(AuthSession, _digest(token))
    if s and s.revoked_at is None:
        s.revoked_at = utcnow()


def revoke_all(db: Session, user_id: str) -> int:
    n = 0
    for s in db.scalars(
        select(AuthSession).where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
    ):
        s.revoked_at = utcnow()
        n += 1
    return n


def csrf_ok(session: AuthSession, header_value: str | None) -> bool:
    return bool(header_value) and hmac.compare_digest(session.csrf_token, header_value or "")


def authenticate(db: Session, username: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.username == username))
    if user is None:
        verify_password(_DUMMY_HASH, password)  # equalise timing
        return None
    if verify_password(user.password_hash, password):
        if _hasher.check_needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)
        return user
    return None


class RateLimiter:
    """Sliding-window limiter keyed by client; in-memory is adequate for a single-user local app."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str) -> float | None:
        """Register a hit. Returns seconds to wait if over the limit, else None."""
        now = time.monotonic()
        with self._lock:
            dq = self._hits[key]
            while dq and now - dq[0] > self.window:
                dq.popleft()
            if len(dq) >= self.limit:
                return self.window - (now - dq[0])
            dq.append(now)
            return None

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


class SetupToken:
    """One-time token that authorises creating the first account. Shown only locally."""

    def __init__(self) -> None:
        self._digest: str | None = None

    def issue(self, secrets_dir: Path) -> str:
        token = secrets.token_urlsafe(18)
        self._digest = _digest(token)
        f = secrets_dir / "setup-token.txt"
        f.write_text(token + "\n", encoding="utf-8")
        f.chmod(0o600)
        return token

    def check(self, token: str) -> bool:
        return self._digest is not None and hmac.compare_digest(self._digest, _digest(token.strip()))

    def consume(self, secrets_dir: Path) -> None:
        self._digest = None
        (secrets_dir / "setup-token.txt").unlink(missing_ok=True)
