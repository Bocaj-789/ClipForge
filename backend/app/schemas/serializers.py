"""Explicit API representations. Raw ORM objects and filesystem paths are never returned."""

from __future__ import annotations

from typing import Any

from app.db.models import (
    Availability,
    CandidateVideo,
    Channel,
    ClipCandidate,
    ConnectionState,
    Job,
    Publication,
    RenderedClip,
    Source,
)


def job(j: Job) -> dict[str, Any]:
    return {
        "id": j.id,
        "job_type": j.job_type,
        "lane": j.lane,
        "priority": j.priority,
        "status": j.status,
        "progress_percent": j.progress_percent,
        "stage": j.stage,
        "attempt_count": j.attempt_count,
        "max_attempts": j.max_attempts,
        "next_attempt_at": j.next_attempt_at,
        "started_at": j.started_at,
        "finished_at": j.finished_at,
        "error_code": j.error_code,
        "safe_error_message": j.safe_error_message,
        "candidate_video_id": j.candidate_video_id,
        "source_id": j.source_id,
        "channel_id": j.channel_id,
        "cancel_requested": j.cancel_requested,
        "created_at": j.created_at,
        "updated_at": j.updated_at,
        "output": {
            k: v
            for k, v in (j.output_json or {}).items()
            if k
            in {
                "proposed",
                "auto_render",
                "provider",
                "language",
                "segments",
                "warnings",
                "notes",
                "new",
                "seen",
                "quota_units",
                "acquisition",
                "reason",
                "duration",
                "orientation",
                "seconds",
                "rendered_clip_id",
                "method",
                "publication_id",
                "video_id",
                "url",
                "availability",
                "auto_publish",
            }
        },
    }


def candidate(c: CandidateVideo, *, latest_job: Job | None = None) -> dict[str, Any]:
    media = (c.metadata_json or {}).get("media") or {}
    return {
        "id": c.id,
        "origin": c.origin,
        "canonical_url": c.canonical_url if not c.canonical_url.startswith("local:") else None,
        "platform": c.platform,
        "external_id": c.external_id,
        "title": c.title,
        "creator_name": c.creator_name,
        "published_at": c.published_at,
        "duration_seconds": c.duration_seconds,
        "thumbnail_url": c.thumbnail_url,
        "discovered_at": c.discovered_at,
        "priority": c.priority,
        "status": c.status,
        "rights_status": c.rights_status,
        "rights_note": c.rights_note,
        "user_note": c.user_note,
        "transcript_status": c.transcript_status,
        "has_media": bool(c.media_path),
        "media_is_user_original": c.media_is_user_original,
        "blocked_reason": c.blocked_reason,
        "last_error": c.last_error,
        "source_id": c.source_id,
        "media": {k: media.get(k) for k in ("width", "height", "orientation", "fps")} if media else None,
        "finder": (c.metadata_json or {}).get("finder"),
        "latest_job": job(latest_job) if latest_job else None,
        "updated_at": c.updated_at,
    }


def proposal(p: ClipCandidate) -> dict[str, Any]:
    return {
        "id": p.id,
        "candidate_video_id": p.candidate_video_id,
        "start_seconds": p.start_seconds,
        "end_seconds": p.end_seconds,
        "duration_seconds": p.duration_seconds,
        "scores": {
            "hook": p.hook_score,
            "coherence": p.coherence_score,
            "interest": p.interest_score,
            "energy": p.emotional_energy_score,
            "clarity": p.clarity_score,
            "channel_fit": p.channel_fit_score,
            "performance_adjustment": p.performance_adjustment,
        },
        "penalties": p.penalties_json,
        "final_score": p.final_score,
        "score_explanation": p.score_explanation,
        "warnings": p.warnings_json,
        "provider": p.provider,
        "transcript_excerpt": p.transcript_excerpt,
        "title_suggestion": p.title_suggestion,
        "crop_offset": p.crop_offset,
        "status": p.status,
        "created_at": p.created_at,
    }


def rendered(r: RenderedClip, *, with_context: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": r.id,
        "clip_candidate_id": r.clip_candidate_id,
        "title_suggestion": r.title_suggestion,
        "description_suggestion": r.description_suggestion,
        "hashtags": r.hashtags_json,
        "render_profile": r.render_profile,
        "caption_style": r.caption_style,
        "crop_mode": r.crop_mode,
        "duration_seconds": r.duration_seconds,
        "width": r.width,
        "height": r.height,
        "file_size_bytes": r.file_size_bytes,
        "checksum": r.checksum,
        "warnings": r.warnings_json,
        "review_status": r.review_status,
        "render_seconds": r.render_seconds,
        "created_at": r.created_at,
        "video_url": f"/api/v1/clips/{r.id}/video",
        "thumbnail_url": f"/api/v1/clips/{r.id}/thumbnail",
        "captions_url": f"/api/v1/clips/{r.id}/captions",
    }
    if with_context and r.clip is not None:
        out["proposal"] = proposal(r.clip)
        v = r.clip.video
        if v is not None:
            out["source"] = {
                "id": v.id,
                "title": v.title,
                "creator_name": v.creator_name,
                "rights_status": v.rights_status,
                "platform": v.platform,
                "canonical_url": v.canonical_url if v.canonical_url.startswith("https://") else None,
            }
    return out


def source(s: Source) -> dict[str, Any]:
    return {
        "id": s.id,
        "kind": s.kind,
        "name": s.name,
        "url": s.url,
        "enabled": s.enabled,
        "paused": s.paused,
        "polling_interval_minutes": s.polling_interval_minutes,
        "category": s.category,
        "keywords": s.keywords_json,
        "config": s.config_json or {},
        "rights_status": s.rights_status,
        "rights_note": s.rights_note,
        "next_check_at": s.next_check_at,
        "last_checked_at": s.last_checked_at,
        "last_success_at": s.last_success_at,
        "last_error": s.last_error,
        "consecutive_failures": s.consecutive_failures,
        "created_at": s.created_at,
    }


def channel(c: Channel) -> dict[str, Any]:
    return {
        "id": c.id,
        "platform": c.platform,
        "display_name": c.display_name,
        "external_channel_id": c.external_channel_id,
        "enabled": c.enabled,
        "paused": c.paused,
        "auto_publish_enabled": c.auto_publish_enabled,
        "max_uploads_per_day": c.max_uploads_per_day,
        "min_spacing_minutes": c.min_spacing_minutes,
        "timezone": c.timezone,
        "schedule": c.schedule_json,
        "category_rules": c.category_rules_json,
        "title_template": c.title_template,
        "description_template": c.description_template,
        "default_language": c.default_language,
        "default_visibility": c.default_visibility,
        "render_profile": c.render_profile,
        "caption_style": c.caption_style,
        "min_clip_seconds": c.min_clip_seconds,
        "max_clip_seconds": c.max_clip_seconds,
        "late_policy": c.late_policy,
        # Never the token or its secret-store reference: only whether a usable connection exists.
        "oauth_connected": c.token_reference is not None and c.connection_state == ConnectionState.CONNECTED,
        "connection_state": c.connection_state,
        "connection_error": c.connection_error,
        "oauth_connected_at": c.oauth_connected_at,
        "youtube_title": c.youtube_title,
        "youtube_handle": c.youtube_handle,
        "youtube_url": f"https://www.youtube.com/channel/{c.external_channel_id}"
        if c.external_channel_id
        else None,
        "allow_public": c.allow_public,
        "category_id": c.category_id,
        "made_for_kids": c.made_for_kids,
        "notify_subscribers": c.notify_subscribers,
        "auto_publish_min_score": c.auto_publish_min_score,
        "release_spacing_minutes": c.release_spacing_minutes,
        "last_upload_at": c.last_upload_at,
        "created_at": c.created_at,
    }


def publication(p: Publication, *, job: Job | None = None, clip_title: str | None = None) -> dict[str, Any]:
    """Upload success (``status == 'uploaded'``) and public visibility (``availability``) are separate."""
    size = p.file_size_bytes or 0
    return {
        "id": p.id,
        "rendered_clip_id": p.rendered_clip_id,
        "channel_id": p.channel_id,
        "channel_name": p.channel.display_name if p.channel is not None else None,
        "clip_title": clip_title,
        "status": p.status,
        "upload_succeeded": p.external_video_id is not None,
        "availability": p.availability,
        "is_public": p.availability == Availability.PUBLIC,
        "visibility_requested": p.visibility,
        "privacy_actual": p.privacy_actual,
        "scheduled_for": p.scheduled_for,
        "uploaded_at": p.uploaded_at,
        "external_video_id": p.external_video_id,
        "external_url": p.external_url,
        "youtube_upload_status": p.youtube_upload_status,
        "rejection_reason": p.rejection_reason,
        "title": p.title,
        "bytes_uploaded": p.bytes_uploaded,
        "file_size_bytes": p.file_size_bytes,
        "progress_percent": round(100 * p.bytes_uploaded / size, 1)
        if size and not p.external_video_id
        else None,
        "error_code": p.error_code,
        "safe_error_message": p.safe_error_message,
        "notice": (p.metadata_json or {}).get("notice"),
        "automatic": p.automatic,
        "last_checked_at": p.last_checked_at,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
        "job": {
            "id": job.id,
            "status": job.status,
            "stage": job.stage,
            "attempt_count": job.attempt_count,
            "max_attempts": job.max_attempts,
            "next_attempt_at": job.next_attempt_at,
            "cancel_requested": job.cancel_requested,
        }
        if job is not None
        else None,
    }
