"""Stable, machine-readable error codes and safe user-facing messages."""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    CSRF_FAILED = "csrf_failed"
    RATE_LIMITED = "rate_limited"
    UNSUPPORTED_URL = "unsupported_url"
    RIGHTS_NOT_CONFIRMED = "rights_not_confirmed"
    ACQUISITION_BLOCKED = "acquisition_blocked"
    API_QUOTA_EXCEEDED = "api_quota_exceeded"
    API_AUTH_EXPIRED = "api_auth_expired"
    NETWORK_TIMEOUT = "network_timeout"
    CORRUPT_MEDIA = "corrupt_media"
    MEDIA_TOO_LARGE = "media_too_large"
    MEDIA_TOO_LONG = "media_too_long"
    NO_AUDIO = "no_audio"
    TRANSCRIPTION_UNAVAILABLE = "transcription_model_unavailable"
    TRANSCRIPTION_FAILED = "transcription_failed"
    LLM_UNAVAILABLE = "llm_unavailable"
    LLM_INVALID_RESPONSE = "llm_invalid_response"
    FFMPEG_MISSING = "ffmpeg_missing"
    RENDER_FAILED = "render_failed"
    DISK_FULL = "disk_full"
    STORAGE_BUDGET = "storage_budget_exceeded"
    UPLOAD_FAILED = "upload_failed"
    UPLOAD_UNCERTAIN = "upload_outcome_unknown"
    ANALYTICS_UNAVAILABLE = "analytics_unavailable"
    CANCELLED = "cancelled"
    MAX_ATTEMPTS = "max_attempts_exceeded"
    LEASE_EXPIRED = "lease_expired"
    INTERNAL = "internal_error"


class AppError(Exception):
    """An error with a stable code and a message that is safe to show the user."""

    status_code = 400

    def __init__(self, code: ErrorCode, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        if status_code is not None:
            self.status_code = status_code


class NotFound(AppError):
    status_code = 404

    def __init__(self, what: str = "Objektet") -> None:
        super().__init__(ErrorCode.NOT_FOUND, f"{what} hittades inte.")


class Conflict(AppError):
    status_code = 409


class JobError(Exception):
    """Raised by job handlers. ``transient`` decides whether the queue retries."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        transient: bool,
        retry_after: float | None = None,
        block: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.transient = transient
        self.retry_after = retry_after
        # ``block`` = needs user action (rights, missing model, missing media); not a failure.
        self.block = block


def transient(code: ErrorCode, message: str, retry_after: float | None = None) -> JobError:
    return JobError(code, message, transient=True, retry_after=retry_after)


def permanent(code: ErrorCode, message: str) -> JobError:
    return JobError(code, message, transient=False)


def blocked(code: ErrorCode, message: str) -> JobError:
    return JobError(code, message, transient=False, block=True)
