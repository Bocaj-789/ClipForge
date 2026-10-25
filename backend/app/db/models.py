"""ORM models. All timestamps are UTC. IDs are opaque hex UUIDs."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UTCDateTime, new_id, utcnow


# ---------------------------------------------------------------- enums
class SourceKind(StrEnum):
    MANUAL_URL = "manual_url"
    YOUTUBE_CHANNEL = "youtube_channel"
    RSS = "rss"
    PERMITTED_FEED = "permitted_feed"
    LOCAL_FOLDER = "local_folder"


class RightsStatus(StrEnum):
    UNKNOWN = "unknown"
    USER_OWNED = "user_owned"
    LICENSED = "licensed"
    PUBLIC_DOMAIN_VERIFIED = "public_domain_verified"
    PERMISSION_CONFIRMED = "permission_confirmed"
    NOT_PERMITTED = "not_permitted"


PERMITTED_RIGHTS = frozenset(
    {
        RightsStatus.USER_OWNED,
        RightsStatus.LICENSED,
        RightsStatus.PUBLIC_DOMAIN_VERIFIED,
        RightsStatus.PERMISSION_CONFIRMED,
    }
)


class CandidateStatus(StrEnum):
    DISCOVERED = "discovered"
    QUEUED = "queued"
    ANALYZING = "analyzing"
    READY = "ready"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    AWAITING_MEDIA = "awaiting_media"  # rights are fine but no provider could deliver the media
    FAILED = "failed"


class JobType(StrEnum):
    DISCOVER = "discover"
    FETCH_METADATA = "fetch_metadata"
    ACQUIRE = "acquire"
    ANALYZE = "analyze"
    TRANSCRIBE = "transcribe"
    EXTRACT_CLIPS = "extract_clips"
    RENDER = "render"
    PUBLISH = "publish"
    ANALYTICS = "analytics"
    MAINTENANCE = "maintenance"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    AWAITING_MEDIA = "awaiting_media"  # needs media access (provider refused / unavailable)


class ClipStatus(StrEnum):
    PROPOSED = "proposed"
    SELECTED = "selected"
    REJECTED = "rejected"
    RENDERED = "rendered"
    PUBLISHED = "published"


class ReviewStatus(StrEnum):
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class Visibility(StrEnum):
    PRIVATE = "private"
    UNLISTED = "unlisted"
    PUBLIC = "public"


class PublicationStatus(StrEnum):
    """Upload lifecycle only. ``UPLOADED`` means YouTube accepted the file and returned a video id;
    it says nothing about whether the video is publicly visible (see :class:`Availability`)."""

    QUEUED = "queued"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    FAILED = "failed"
    BLOCKED = "blocked"  # needs the user (re-authorise, uncertain outcome, rights, public not allowed)
    CANCELLED = "cancelled"


class Availability(StrEnum):
    """What YouTube itself reports about an uploaded video."""

    UNKNOWN = "unknown"  # not checked yet
    PROCESSING = "processing"  # YouTube is still processing the file
    PRIVATE = "private"
    UNLISTED = "unlisted"
    SCHEDULED = "scheduled"  # private now, becomes public at ``scheduled_for``
    PUBLIC = "public"
    REJECTED = "rejected"
    FAILED = "failed"  # YouTube could not process the file
    MISSING = "missing"  # the video no longer exists on YouTube


class ConnectionState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    NEEDS_REAUTH = "needs_reauth"  # refresh token revoked/expired; the user must connect again


# ---------------------------------------------------------------- tables
class Source(Base):
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str | None] = mapped_column(String(2048))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    polling_interval_minutes: Mapped[int] = mapped_column(Integer, default=180)
    category: Mapped[str | None] = mapped_column(String(100))
    keywords_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    rights_status: Mapped[str] = mapped_column(String(32), default=RightsStatus.UNKNOWN)
    rights_note: Mapped[str | None] = mapped_column(Text)
    cursor_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    next_check_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class CandidateVideo(Base):
    __tablename__ = "candidate_videos"
    __table_args__ = (
        UniqueConstraint("canonical_url", name="uq_candidate_canonical_url"),
        Index("ix_candidate_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"))
    origin: Mapped[str] = mapped_column(String(32))  # manual_url | local_upload | discovery
    canonical_url: Mapped[str] = mapped_column(String(2048))
    platform: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str | None] = mapped_column(String(128))
    title: Mapped[str | None] = mapped_column(String(500))
    creator_name: Mapped[str | None] = mapped_column(String(300))
    creator_channel_id: Mapped[str | None] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    thumbnail_url: Mapped[str | None] = mapped_column(String(2048))
    discovered_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    priority: Mapped[int] = mapped_column(Integer, default=3)
    status: Mapped[str] = mapped_column(String(32), default=CandidateStatus.DISCOVERED)
    rights_status: Mapped[str] = mapped_column(String(32), default=RightsStatus.UNKNOWN)
    rights_note: Mapped[str | None] = mapped_column(Text)
    user_note: Mapped[str | None] = mapped_column(Text)
    transcript_status: Mapped[str] = mapped_column(String(32), default="none")
    media_path: Mapped[str | None] = mapped_column(String(1024))  # relative to data dir
    media_checksum: Mapped[str | None] = mapped_column(String(64), index=True)
    media_is_user_original: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked_reason: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    clips: Mapped[list[ClipCandidate]] = relationship(back_populates="video", cascade="all, delete-orphan")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_claim", "status", "lane", "priority", "created_at"),
        Index("ix_jobs_candidate", "candidate_video_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    candidate_video_id: Mapped[str | None] = mapped_column(
        ForeignKey("candidate_videos.id", ondelete="CASCADE")
    )
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    channel_id: Mapped[str | None] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"))
    job_type: Mapped[str] = mapped_column(String(32))
    lane: Mapped[str] = mapped_column(String(16))  # heavy | light
    priority: Mapped[int] = mapped_column(Integer, default=3)
    status: Mapped[str] = mapped_column(String(16), default=JobStatus.PENDING)
    progress_percent: Mapped[float] = mapped_column(Float, default=0.0)
    stage: Mapped[str | None] = mapped_column(String(100))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    locked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    worker_id: Mapped[str | None] = mapped_column(String(64))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    error_code: Mapped[str | None] = mapped_column(String(64))
    safe_error_message: Mapped[str | None] = mapped_column(Text)
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Transcript(Base):
    __tablename__ = "transcripts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    candidate_video_id: Mapped[str] = mapped_column(
        ForeignKey("candidate_videos.id", ondelete="CASCADE"), index=True
    )
    language: Mapped[str | None] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    segments_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    engine_name: Mapped[str] = mapped_column(String(64))
    engine_version: Mapped[str] = mapped_column(String(64))
    model_name: Mapped[str | None] = mapped_column(String(64))
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    processing_seconds: Mapped[float | None] = mapped_column(Float)
    checksum: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="complete")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ClipCandidate(Base):
    __tablename__ = "clip_candidates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    candidate_video_id: Mapped[str] = mapped_column(
        ForeignKey("candidate_videos.id", ondelete="CASCADE"), index=True
    )
    start_seconds: Mapped[float] = mapped_column(Float)
    end_seconds: Mapped[float] = mapped_column(Float)
    duration_seconds: Mapped[float] = mapped_column(Float)
    hook_score: Mapped[float] = mapped_column(Float, default=0)
    coherence_score: Mapped[float] = mapped_column(Float, default=0)
    interest_score: Mapped[float] = mapped_column(Float, default=0)
    emotional_energy_score: Mapped[float] = mapped_column(Float, default=0)
    clarity_score: Mapped[float] = mapped_column(Float, default=0)
    channel_fit_score: Mapped[float] = mapped_column(Float, default=0)
    penalties_json: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    performance_adjustment: Mapped[float] = mapped_column(Float, default=0)
    final_score: Mapped[float] = mapped_column(Float, default=0)
    score_explanation: Mapped[str] = mapped_column(Text, default="")
    warnings_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    provider: Mapped[str] = mapped_column(String(64), default="heuristic")
    transcript_excerpt: Mapped[str] = mapped_column(Text, default="")
    title_suggestion: Mapped[str | None] = mapped_column(String(200))
    crop_offset: Mapped[float | None] = mapped_column(Float)  # manual crop override 0..1
    status: Mapped[str] = mapped_column(String(16), default=ClipStatus.PROPOSED)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    video: Mapped[CandidateVideo] = relationship(back_populates="clips")
    renders: Mapped[list[RenderedClip]] = relationship(back_populates="clip", cascade="all, delete-orphan")


class RenderedClip(Base):
    __tablename__ = "rendered_clips"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    clip_candidate_id: Mapped[str] = mapped_column(
        ForeignKey("clip_candidates.id", ondelete="CASCADE"), index=True
    )
    file_path: Mapped[str] = mapped_column(String(1024))  # relative to data dir
    thumbnail_path: Mapped[str | None] = mapped_column(String(1024))
    caption_file_path: Mapped[str | None] = mapped_column(String(1024))
    title_suggestion: Mapped[str] = mapped_column(String(200), default="")
    description_suggestion: Mapped[str] = mapped_column(Text, default="")
    hashtags_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    render_profile: Mapped[str] = mapped_column(String(32))
    caption_style: Mapped[str] = mapped_column(String(32), default="clean")
    crop_mode: Mapped[str] = mapped_column(String(32), default="center_crop")
    tool_version: Mapped[str | None] = mapped_column(String(128))
    duration_seconds: Mapped[float] = mapped_column(Float)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    file_size_bytes: Mapped[int] = mapped_column(Integer)
    checksum: Mapped[str] = mapped_column(String(64))
    warnings_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    review_status: Mapped[str] = mapped_column(String(16), default=ReviewStatus.NEEDS_REVIEW)
    render_seconds: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    clip: Mapped[ClipCandidate] = relationship(back_populates="renders")


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    platform: Mapped[str] = mapped_column(String(32), default="youtube")
    display_name: Mapped[str] = mapped_column(String(200))
    external_channel_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_publish_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    max_uploads_per_day: Mapped[int] = mapped_column(Integer, default=1)
    min_spacing_minutes: Mapped[int] = mapped_column(Integer, default=240)
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Stockholm")
    schedule_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    category_rules_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    title_template: Mapped[str] = mapped_column(String(300), default="{title}")
    description_template: Mapped[str] = mapped_column(Text, default="{description}\n\n{hashtags}")
    default_language: Mapped[str] = mapped_column(String(16), default="sv")
    default_visibility: Mapped[str] = mapped_column(String(16), default=Visibility.PRIVATE)
    render_profile: Mapped[str] = mapped_column(String(32), default="shorts_standard")
    caption_style: Mapped[str] = mapped_column(String(32), default="bold")
    min_clip_seconds: Mapped[int] = mapped_column(Integer, default=20)
    max_clip_seconds: Mapped[int] = mapped_column(Integer, default=60)
    late_policy: Mapped[str] = mapped_column(String(16), default="next_slot")  # next_slot | skip
    # Pointer into the OS secret store (macOS Keychain). The refresh token itself is never in the DB.
    token_reference: Mapped[str | None] = mapped_column(String(200))
    oauth_connected_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    connection_state: Mapped[str] = mapped_column(String(16), default=ConnectionState.DISCONNECTED)
    connection_error: Mapped[str | None] = mapped_column(Text)
    granted_scopes_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    youtube_title: Mapped[str | None] = mapped_column(String(200))
    youtube_handle: Mapped[str | None] = mapped_column(String(100))
    # Public (and scheduled-to-become-public) uploads need this explicit opt-in per channel.
    allow_public: Mapped[bool] = mapped_column(Boolean, default=False)
    category_id: Mapped[str] = mapped_column(String(8), default="22")
    made_for_kids: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_subscribers: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_publish_min_score: Mapped[float] = mapped_column(Float, default=60.0)
    last_upload_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Publication(Base):
    __tablename__ = "publications"
    __table_args__ = (UniqueConstraint("rendered_clip_id", "channel_id", name="uq_publication_clip_channel"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    rendered_clip_id: Mapped[str] = mapped_column(ForeignKey("rendered_clips.id", ondelete="CASCADE"))
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16), default=PublicationStatus.QUEUED)
    external_video_id: Mapped[str | None] = mapped_column(String(64))
    external_url: Mapped[str | None] = mapped_column(String(300))
    scheduled_for: Mapped[datetime | None] = mapped_column(UTCDateTime)
    uploaded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    visibility: Mapped[str] = mapped_column(String(16), default=Visibility.PRIVATE)  # requested
    request_id: Mapped[str] = mapped_column(String(64), default=new_id, unique=True)
    upload_session_uri: Mapped[str | None] = mapped_column(String(2048))
    error_code: Mapped[str | None] = mapped_column(String(64))
    safe_error_message: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Frozen at request time so a retry uploads exactly what the user approved.
    title: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    tags_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    category_id: Mapped[str] = mapped_column(String(8), default="22")
    language: Mapped[str | None] = mapped_column(String(16))
    made_for_kids: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_subscribers: Mapped[bool] = mapped_column(Boolean, default=False)
    automatic: Mapped[bool] = mapped_column(Boolean, default=False)
    job_id: Mapped[str | None] = mapped_column(String(32))
    file_size_bytes: Mapped[int | None] = mapped_column(Integer)
    bytes_uploaded: Mapped[int] = mapped_column(Integer, default=0)
    session_created_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # True once the last byte was sent but YouTube's answer was not received (outcome unknown).
    final_chunk_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    # What YouTube reports (separate from ``status`` which is only the upload lifecycle).
    availability: Mapped[str] = mapped_column(String(16), default=Availability.UNKNOWN)
    privacy_actual: Mapped[str | None] = mapped_column(String(16))
    youtube_upload_status: Mapped[str | None] = mapped_column(String(32))
    rejection_reason: Mapped[str | None] = mapped_column(String(100))
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    rendered_clip: Mapped[RenderedClip] = relationship()
    channel: Mapped[Channel] = relationship()


class OAuthState(Base):
    """A pending authorisation: single use, short lived, bound to the browser that started it."""

    __tablename__ = "oauth_states"

    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of the state value
    browser_hash: Mapped[str] = mapped_column(String(64))  # sha256 of the cookie set at start
    code_verifier: Mapped[str] = mapped_column(String(128))  # PKCE
    channel_id: Mapped[str | None] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"))
    redirect_uri: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class QuotaUsage(Base):
    """Local ledger of YouTube API calls per Pacific-time day (Google resets quota at PT midnight)."""

    __tablename__ = "quota_usage"
    __table_args__ = (UniqueConstraint("day", "bucket", name="uq_quota_day_bucket"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    day: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD in America/Los_Angeles
    bucket: Mapped[str] = mapped_column(String(16))  # "insert" (videos.insert calls) | "units"
    used: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class AnalyticsSnapshot(Base):
    __tablename__ = "analytics_snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    publication_id: Mapped[str] = mapped_column(ForeignKey("publications.id", ondelete="CASCADE"), index=True)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    views: Mapped[int | None] = mapped_column(Integer)
    likes: Mapped[int | None] = mapped_column(Integer)
    comments: Mapped[int | None] = mapped_column(Integer)
    watch_time_seconds: Mapped[float | None] = mapped_column(Float)
    average_view_duration_seconds: Mapped[float | None] = mapped_column(Float)
    average_view_percentage: Mapped[float | None] = mapped_column(Float)
    impressions: Mapped[int | None] = mapped_column(Integer)
    ctr: Mapped[float | None] = mapped_column(Float)
    raw_metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    data_source: Mapped[str] = mapped_column(String(64))


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_ts", "timestamp"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    actor: Mapped[str] = mapped_column(String(16))  # system | user
    severity: Mapped[str] = mapped_column(String(16), default="info")
    action: Mapped[str] = mapped_column(String(100))
    object_type: Mapped[str | None] = mapped_column(String(64))
    object_id: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[str] = mapped_column(String(32), default="ok")
    safe_details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AppSettingsRow(Base):
    """Single-row table holding the validated AppSettings JSON document."""

    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    data_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class AgentControl(Base):
    """Single-row table with the desired global agent state."""

    __tablename__ = "agent_control"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    desired_state: Mapped[str] = mapped_column(String(16), default="running")
    reason: Mapped[str | None] = mapped_column(Text)
    maintenance_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    current_job_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    degraded_json: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    stopped_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    password_changed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of cookie value
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    user_agent: Mapped[str | None] = mapped_column(String(300))
