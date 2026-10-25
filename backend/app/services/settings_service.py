"""Typed, validated runtime settings persisted in the database."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from app.db.models import AppSettingsRow

TranscriptionModel = Literal["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"]

# Approximate download sizes (faster-whisper CTranslate2 int8/fp16 weights) for user disclosure.
TRANSCRIPTION_MODEL_INFO: dict[str, dict[str, str]] = {
    "tiny": {"size": "~75 MB", "ram": "~0.5 GB", "note": "Snabbast, lägst precision"},
    "base": {"size": "~145 MB", "ram": "~0.7 GB", "note": "Rekommenderad standard på M1"},
    "small": {"size": "~485 MB", "ram": "~1.2 GB", "note": "Bättre svenska, långsammare"},
    "medium": {"size": "~1.5 GB", "ram": "~2.5 GB", "note": "Hög precision, långsam på CPU"},
    "large-v3-turbo": {"size": "~1.6 GB", "ram": "~3 GB", "note": "Hög precision, mycket långsam"},
    "large-v3": {"size": "~3 GB", "ram": "~4.5 GB", "note": "Högst precision, mycket långsam"},
}


def validate_timezone(v: str) -> str:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(v)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"unknown timezone: {v!r}") from e
    return v


class AppSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # Discovery
    discovery_enabled: bool = True
    default_polling_interval_minutes: int = Field(default=180, ge=15, le=7 * 24 * 60)
    max_discoveries_per_day: int = Field(default=200, ge=0, le=5000)
    allowed_source_kinds: list[str] = Field(
        default_factory=lambda: ["manual_url", "youtube_channel", "rss", "local_folder", "permitted_feed"]
    )
    # Hosts from which direct media files may be fetched when rights are confirmed.
    allowed_direct_media_hosts: list[str] = Field(default_factory=lambda: ["archive.org"])

    # Processing caps
    max_renders_per_day: int = Field(default=30, ge=0, le=1000)
    max_input_file_mb: int = Field(default=4096, ge=10, le=50_000)
    max_input_duration_minutes: int = Field(default=180, ge=1, le=24 * 60)
    # Highest source resolution fetched from YouTube; 0 = best available (needs far more disk).
    youtube_max_height: Literal[0, 480, 720, 1080, 1440, 2160] = 1080
    # Media provider for YouTube videos whose rights the user has confirmed. "off" = metadata only;
    # such videos wait for an uploaded file. Providers never bypass logins, DRM or other restrictions.
    youtube_media_provider: Literal["yt_dlp", "off"] = "yt_dlp"
    heavy_concurrency: int = Field(default=1, ge=1, le=4)
    auto_render_top_n: int = Field(default=3, ge=0, le=20)
    low_resource_mode: bool = False

    # Clip defaults
    min_clip_seconds: int = Field(default=20, ge=5, le=600)
    max_clip_seconds: int = Field(default=60, ge=5, le=600)
    default_render_profile: Literal["fast_draft", "shorts_standard", "high_quality"] = "shorts_standard"
    default_caption_style: Literal["clean", "bold", "karaoke"] = "bold"
    default_crop_mode: Literal["center_crop", "fit_blur"] = "center_crop"
    loudness_normalization: bool = True
    caption_font_path: str | None = None

    # Storage
    storage_budget_gb: float = Field(default=15.0, ge=1, le=10_000)
    storage_warn_percent: int = Field(default=80, ge=10, le=99)
    storage_critical_percent: int = Field(default=90, ge=20, le=100)
    min_free_disk_gb: float = Field(default=5.0, ge=0.5, le=1000)
    temp_media_retention_days: int = Field(default=14, ge=0, le=3650)
    log_retention_days: int = Field(default=30, ge=1, le=3650)

    # AI
    llm_provider: Literal["heuristic", "ollama"] = "heuristic"
    ollama_model: str = Field(default="llama3.2:3b", max_length=100, pattern=r"^[A-Za-z0-9._:\-/]+$")
    transcription_model: TranscriptionModel = "base"
    transcription_language: Literal["auto", "sv", "en"] = "auto"
    # Weights are only fetched after the user explicitly allows it.
    transcription_download_allowed: bool = False

    # Learning
    learning_enabled: bool = True

    # Publishing (deterministic policy, never controlled by a model)
    auto_publish_global_enabled: bool = False
    max_uploads_per_day_global: int = Field(default=3, ge=0, le=100)

    # Remote access (informational toggle; the tunnel itself is configured outside the app)
    remote_access_enabled: bool = False

    # Display
    timezone: str = "Europe/Stockholm"

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        return validate_timezone(v)

    @field_validator("allowed_direct_media_hosts")
    @classmethod
    def _hosts(cls, v: list[str]) -> list[str]:
        out = []
        for h in v:
            h = h.strip().lower().strip(".")
            if not h or "/" in h or ":" in h or len(h) > 253:
                raise ValueError(f"invalid host: {h!r}")
            out.append(h)
        return sorted(set(out))

    @model_validator(mode="after")
    def _ranges(self) -> AppSettings:
        if self.min_clip_seconds >= self.max_clip_seconds:
            raise ValueError("min_clip_seconds must be less than max_clip_seconds")
        if self.storage_warn_percent >= self.storage_critical_percent:
            raise ValueError("storage_warn_percent must be below storage_critical_percent")
        return self


# Fields that require an explicit confirmation phrase to switch on.
CONFIRM_REQUIRED = {"auto_publish_global_enabled", "remote_access_enabled", "transcription_download_allowed"}


def load_settings(db: Session) -> AppSettings:
    row = db.get(AppSettingsRow, 1)
    if row is None:
        s = AppSettings()
        db.add(AppSettingsRow(id=1, data_json=s.model_dump()))
        db.flush()
        return s
    merged = {**AppSettings().model_dump(), **(row.data_json or {})}
    known = set(AppSettings.model_fields)
    return AppSettings.model_validate({k: v for k, v in merged.items() if k in known})


def save_settings(db: Session, settings: AppSettings) -> AppSettings:
    row = db.get(AppSettingsRow, 1)
    data = settings.model_dump()
    if row is None:
        db.add(AppSettingsRow(id=1, data_json=data))
    else:
        row.data_json = data
    db.flush()
    return settings


def apply_patch(current: AppSettings, patch: dict[str, object]) -> AppSettings:
    unknown = set(patch) - set(AppSettings.model_fields)
    if unknown:
        raise ValueError(f"unknown settings: {sorted(unknown)}")
    return AppSettings.model_validate({**current.model_dump(), **patch})
