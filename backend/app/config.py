"""Process-level configuration loaded from environment / .env.

Runtime-tunable product settings (discovery intervals, caps, models...) live in the
database and are managed by ``services.settings_service``. This module only holds
values needed to boot the process.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CLIPFORGE_",
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: Path = Field(default=REPO_ROOT / "data")
    host: str = "127.0.0.1"
    port: int = 8000
    dev_mode: bool = False
    session_ttl_hours: int = Field(default=72, ge=1, le=24 * 30)
    youtube_api_key: SecretStr | None = None
    ollama_url: str = "http://127.0.0.1:11434"
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    worker_lease_seconds: int = Field(default=300, ge=30)

    # YouTube publishing (Google OAuth 2.0 client). Alternatively place the JSON downloaded from the
    # Google Cloud console at data/secrets/google_client_secret.json.
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    # Must match an "Authorized redirect URI" of the OAuth client exactly. Default is derived from
    # host/port: http://127.0.0.1:<port>/api/v1/oauth/youtube/callback
    google_redirect_uri: str | None = None
    # Where refresh tokens are stored. "auto" = macOS Keychain (fails closed if unavailable);
    # "file" is an explicit, weaker opt-in (0600 file in data/secrets).
    token_store: Literal["auto", "keychain", "file"] = "auto"  # noqa: S105 - a mode, not a secret

    @field_validator("data_dir", mode="before")
    @classmethod
    def _resolve_data_dir(cls, v: object) -> Path:
        p = Path(str(v)).expanduser()
        if not p.is_absolute():
            p = REPO_ROOT / p
        return p.resolve()

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        # Remote access must go through an authenticated tunnel, never a public bind.
        if v not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("CLIPFORGE_HOST must be a loopback address (127.0.0.1)")
        return v

    @field_validator("ollama_url")
    @classmethod
    def _ollama_loopback(cls, v: str) -> str:
        if not (v.startswith("http://127.0.0.1") or v.startswith("http://localhost")):
            raise ValueError("CLIPFORGE_OLLAMA_URL must point at loopback")
        return v.rstrip("/")

    @field_validator("google_redirect_uri")
    @classmethod
    def _redirect_uri(cls, v: str | None) -> str | None:
        if not v:
            return None
        # Google only accepts https:// or loopback http:// redirect URIs for local apps.
        if not (
            v.startswith("https://") or v.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]"))
        ):
            raise ValueError("CLIPFORGE_GOOGLE_REDIRECT_URI must be https:// or a loopback http:// URI")
        return v

    # YouTube OAuth ------------------------------------------------------
    @property
    def oauth_redirect_uri(self) -> str:
        if self.google_redirect_uri:
            return self.google_redirect_uri
        host = "127.0.0.1" if self.host == "localhost" else self.host
        host = f"[{host}]" if ":" in host else host
        return f"http://{host}:{self.port}/api/v1/oauth/youtube/callback"

    @property
    def google_client_secret_file(self) -> Path:
        return self.secrets_dir / "google_client_secret.json"

    def google_oauth_client(self) -> tuple[str, str] | None:
        """(client_id, client_secret) from the environment or the downloaded Google JSON, else None."""
        if self.google_client_id and self.google_client_secret:
            secret = self.google_client_secret.get_secret_value()
            if secret:
                return self.google_client_id, secret
        path = self.google_client_secret_file
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            block = raw.get("web") or raw.get("installed") or {}
            file_id, file_secret = block.get("client_id"), block.get("client_secret")
            if isinstance(file_id, str) and isinstance(file_secret, str) and file_id and file_secret:
                return file_id, file_secret
        return None

    # Derived paths -------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "clipforge.sqlite3"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"

    @property
    def thumbnails_dir(self) -> Path:
        return self.data_dir / "thumbnails"

    @property
    def captions_dir(self) -> Path:
        return self.data_dir / "captions"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def import_dir(self) -> Path:
        """Watched folder for the local_folder source."""
        return self.data_dir / "import"

    @property
    def secrets_dir(self) -> Path:
        return self.data_dir / "secrets"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    def ensure_dirs(self) -> None:
        for d in (
            self.data_dir,
            self.media_dir,
            self.clips_dir,
            self.thumbnails_dir,
            self.captions_dir,
            self.tmp_dir,
            self.logs_dir,
            self.models_dir,
            self.import_dir,
            self.backups_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        self.secrets_dir.chmod(0o700)


@lru_cache
def get_config() -> Config:
    return Config()
