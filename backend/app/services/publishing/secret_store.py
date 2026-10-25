"""Secret storage for OAuth refresh tokens.

Refresh tokens live in the macOS Keychain (via ``keyring``). The database only keeps a *reference*
(``Channel.token_reference``) so a database backup or leaked ``clipforge.sqlite3`` never contains a
credential. If no secure store is available the app refuses to connect channels instead of silently
falling back to plaintext; ``CLIPFORGE_TOKEN_STORE=file`` is an explicit, documented opt-in.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Protocol

from app.config import Config

log = logging.getLogger("clipforge.secrets")

KEYCHAIN_SERVICE = "ClipForge"
_SAFE_ID = re.compile(r"^[A-Za-z0-9._/-]{1,120}$")


class SecretStoreError(Exception):
    """Storage failed. The message never contains secret material."""


class SecretStore(Protocol):
    name: str

    def available(self) -> tuple[bool, str | None]: ...
    def make_ref(self, key: str) -> str: ...
    def get(self, ref: str) -> str | None: ...
    def set(self, ref: str, value: str) -> None: ...
    def delete(self, ref: str) -> None: ...


def _split(ref: str, scheme: str) -> str:
    prefix, _, account = ref.partition(":")
    if prefix != scheme or not _SAFE_ID.match(account) or ".." in account or account[0] in "./-":
        raise SecretStoreError("Okänd tokenreferens för den här lagringen.")
    return account


class KeychainStore:
    """macOS Keychain through the ``keyring`` package (Security.framework, no secrets in argv)."""

    name = "keychain"

    def __init__(self, service: str = KEYCHAIN_SERVICE) -> None:
        self.service = service

    def available(self) -> tuple[bool, str | None]:
        try:
            import keyring
        except ImportError:  # pragma: no cover - dependency is pinned
            return False, "Python-paketet keyring saknas."
        try:
            kr = keyring.get_keyring()
        except Exception:  # noqa: BLE001
            return False, "Ingen nyckelringsbackend kunde laddas."
        module = type(kr).__module__
        if module.startswith(("keyring.backends.fail", "keyring.backends.null")):
            return False, "Ingen säker nyckelring är tillgänglig på den här datorn."
        backends = getattr(kr, "backends", None)
        if backends is not None and not list(backends):
            return False, "Ingen säker nyckelring är tillgänglig på den här datorn."
        return True, None

    def make_ref(self, key: str) -> str:
        return f"keychain:{key}"

    def get(self, ref: str) -> str | None:
        import keyring
        from keyring.errors import KeyringError

        account = _split(ref, "keychain")
        try:
            return keyring.get_password(self.service, account)
        except KeyringError as e:
            raise SecretStoreError("Nyckelringen kunde inte läsas.") from e

    def set(self, ref: str, value: str) -> None:
        import keyring
        from keyring.errors import KeyringError

        account = _split(ref, "keychain")
        try:
            keyring.set_password(self.service, account, value)
        except KeyringError as e:
            raise SecretStoreError("Nyckelringen kunde inte skrivas.") from e

    def delete(self, ref: str) -> None:
        import keyring
        from keyring.errors import KeyringError, PasswordDeleteError

        account = _split(ref, "keychain")
        try:
            keyring.delete_password(self.service, account)
        except PasswordDeleteError:
            return  # already gone
        except KeyringError as e:
            raise SecretStoreError("Posten i nyckelringen kunde inte tas bort.") from e


class FileStore:
    """Weaker fallback: one 0600 file per secret inside the 0700 secrets directory.

    Only used when explicitly requested (``CLIPFORGE_TOKEN_STORE=file``). Protects against other
    local users and accidental backups of the database, not against malware running as you.
    """

    name = "file"

    def __init__(self, directory: Path) -> None:
        self.dir = directory / "tokens"

    def available(self) -> tuple[bool, str | None]:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.dir.chmod(0o700)
        except OSError as e:
            return False, f"Tokenmappen kunde inte skapas: {e.strerror}"
        return True, None

    def make_ref(self, key: str) -> str:
        return f"file:{key}"

    def _path(self, ref: str) -> Path:
        account = _split(ref, "file").replace("/", "__")
        return self.dir / f"{account}.secret"

    def get(self, ref: str) -> str | None:
        p = self._path(ref)
        try:
            return p.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as e:
            raise SecretStoreError("Tokenfilen kunde inte läsas.") from e

    def set(self, ref: str, value: str) -> None:
        p = self._path(ref)
        ok, why = self.available()
        if not ok:
            raise SecretStoreError(why or "Tokenmappen är inte tillgänglig.")
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".tmp-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                os.fchmod(fh.fileno(), 0o600)
                fh.write(value)
            os.replace(tmp, p)
        except OSError as e:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise SecretStoreError("Tokenfilen kunde inte skrivas.") from e

    def delete(self, ref: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._path(ref).unlink()


class MemoryStore:
    """In-process store for tests."""

    name = "memory"

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._lock = threading.Lock()

    def available(self) -> tuple[bool, str | None]:
        return True, None

    def make_ref(self, key: str) -> str:
        return f"memory:{key}"

    def get(self, ref: str) -> str | None:
        _split(ref, "memory")
        with self._lock:
            return self._data.get(ref)

    def set(self, ref: str, value: str) -> None:
        _split(ref, "memory")
        with self._lock:
            self._data[ref] = value

    def delete(self, ref: str) -> None:
        with self._lock:
            self._data.pop(ref, None)


def get_secret_store(cfg: Config) -> SecretStore:
    if cfg.token_store == "file":  # noqa: S105 - a mode name, not a secret
        log.warning("token store: using the weaker file store because CLIPFORGE_TOKEN_STORE=file")
        return FileStore(cfg.secrets_dir)
    return KeychainStore()


# --------------------------------------------------------------- token payload helpers
def dump_token(refresh_token: str, scopes: list[str]) -> str:
    return json.dumps({"refresh_token": refresh_token, "scopes": scopes}, separators=(",", ":"))


def load_refresh_token(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    token = data.get("refresh_token") if isinstance(data, dict) else None
    return token if isinstance(token, str) and token else None
