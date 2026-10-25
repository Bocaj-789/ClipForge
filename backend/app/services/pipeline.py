"""Job handlers: metadata -> acquire -> analyze -> transcribe -> extract_clips -> render.

Each handler persists its result and enqueues the next stage, so progress survives restarts
at every stage boundary.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Config
from app.core.errors import ErrorCode, JobError, awaiting_media, blocked, permanent, transient
from app.core.security import clean_text, contained_path, safe_filename
from app.db.base import utcnow
from app.db.models import (
    CandidateStatus,
    CandidateVideo,
    ClipCandidate,
    ClipStatus,
    Job,
    JobType,
    RenderedClip,
    Source,
    Transcript,
)
from app.services.acquisition import base as acq
from app.services.acquisition.local_file import sha256_path
from app.services.acquisition.provider import (
    MediaProvider,
    MediaRequest,
    build_providers,
    select_provider,
)
from app.services.audit import service as audit
from app.services.discovery.base import DiscoveryResult
from app.services.intelligence.candidate_extractor import sentences_from_segments, validate_range
from app.services.intelligence.llm_provider import ClipProvider, HeuristicProvider, OllamaProvider
from app.services.intelligence.metadata import suggest_description, suggest_hashtags, suggest_title
from app.services.queue import service as q
from app.services.queue.states import Priority
from app.services.rendering.ffmpeg import Cancelled, probe
from app.services.rendering.renderer import RenderRequest, render_clip
from app.services.settings_service import AppSettings
from app.services.storage.manager import has_free_space, invalidate_cache
from app.services.transcription.audio import extract_audio
from app.services.transcription.base import TranscriptionEngine
from app.services.transcription.whisper_local import FasterWhisperEngine, TranscriptionCancelled
from app.services.workspace import job_dir, job_workspace
from app.services.workspace import remove as remove_workspace

log = logging.getLogger("clipforge.pipeline")


class Defer(Exception):  # noqa: N818 - control-flow signal, not an error
    def __init__(self, until: datetime, reason: str) -> None:
        super().__init__(reason)
        self.until = until
        self.reason = reason


@dataclass
class JobContext:
    db: Session
    job: Job
    cfg: Config
    settings: AppSettings
    progress: Callable[[float, str | None], None]
    cancelled: Callable[[], bool]
    engine_factory: Callable[[Config, AppSettings], TranscriptionEngine] | None = None
    provider_factory: Callable[[Config, AppSettings], tuple[ClipProvider, str | None]] | None = None
    discovery_factory: Callable[[Source], Any] | None = None
    providers_factory: Callable[[Config, AppSettings], Sequence[MediaProvider]] | None = None
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ helpers
def _candidate(ctx: JobContext) -> CandidateVideo:
    cand = ctx.db.get(CandidateVideo, ctx.job.candidate_video_id) if ctx.job.candidate_video_id else None
    if cand is None:
        raise permanent(ErrorCode.NOT_FOUND, "Videon som jobbet gäller finns inte längre.")
    return cand


def _require_rights(cand: CandidateVideo) -> None:
    if not acq.rights_ok(cand.rights_status):
        raise blocked(ErrorCode.RIGHTS_NOT_CONFIRMED, acq.rights_message(cand.rights_status))


def _media_path(ctx: JobContext, cand: CandidateVideo) -> Path:
    if not cand.media_path:
        raise blocked(ErrorCode.ACQUISITION_BLOCKED, "Ingen mediefil finns för videon.")
    p = contained_path(ctx.cfg.data_dir, cand.media_path)
    if not p.is_file():
        raise blocked(ErrorCode.ACQUISITION_BLOCKED, "Mediefilen saknas på disken.")
    return p


def _next(
    ctx: JobContext,
    job_type: str,
    cand: CandidateVideo,
    *,
    key: str | None = None,
    input_json: dict[str, Any] | None = None,
) -> Job:
    return q.enqueue(
        ctx.db,
        job_type,
        priority=ctx.job.priority,
        candidate_video_id=cand.id,
        source_id=ctx.job.source_id,
        input_json=input_json,
        idempotency_key=key,
    )


def default_engine(cfg: Config, s: AppSettings) -> TranscriptionEngine:
    return FasterWhisperEngine(
        cfg.models_dir,
        s.transcription_model,
        download_allowed=s.transcription_download_allowed,
        low_resource=s.low_resource_mode,
    )


def default_provider(cfg: Config, s: AppSettings) -> tuple[ClipProvider, str | None]:
    if s.llm_provider == "ollama":
        p = OllamaProvider(cfg.ollama_url, s.ollama_model)
        ok, reason = p.health()
        if ok:
            return p, None
        return HeuristicProvider(), f"Lokal AI otillgänglig, använder heuristik: {reason}"
    return HeuristicProvider(), None


def default_providers(cfg: Config, s: AppSettings) -> Sequence[MediaProvider]:
    return build_providers(cfg, s)


def _providers(ctx: JobContext) -> Sequence[MediaProvider]:
    return (ctx.providers_factory or default_providers)(ctx.cfg, ctx.settings)


def fetch_youtube_oembed(url: str) -> dict[str, Any] | None:
    """Public, keyless oEmbed metadata (title/author/thumbnail) from a fixed YouTube endpoint.

    Returns ``None`` when YouTube refuses (private, removed or embedding disabled); the media
    provider's metadata and acquisition then decide what that means for the video.
    """
    try:
        r = httpx.get("https://www.youtube.com/oembed", params={"url": url, "format": "json"}, timeout=15)
    except httpx.HTTPError as e:
        raise transient(ErrorCode.NETWORK_TIMEOUT, "Kunde inte nå YouTube för metadata.") from e
    if r.status_code in (401, 403, 404):
        return None
    if r.status_code >= 400:
        raise transient(ErrorCode.NETWORK_TIMEOUT, f"YouTube svarade {r.status_code}.")
    data = r.json()
    return data if isinstance(data, dict) else {}


# ------------------------------------------------------------------ handlers
def _apply_oembed(cand: CandidateVideo, meta: dict[str, Any]) -> None:
    cand.title = clean_text(str(meta.get("title", "")), 500) or cand.title
    cand.creator_name = clean_text(str(meta.get("author_name", "")), 300) or cand.creator_name
    thumb = str(meta.get("thumbnail_url", ""))
    if thumb.startswith("https://i.ytimg.com/"):
        cand.thumbnail_url = thumb


def _park_awaiting_media(ctx: JobContext, cand: CandidateVideo, reason: str) -> JobError:
    """Persist what we learned, then return the error that parks the job as awaiting media."""
    cand.status = CandidateStatus.AWAITING_MEDIA
    cand.blocked_reason = reason
    ctx.db.commit()  # the worker rolls back on error; keep metadata gathered so far
    return awaiting_media(reason)


def handle_fetch_metadata(ctx: JobContext) -> dict[str, Any]:
    cand = _candidate(ctx)
    providers = _providers(ctx)
    meta_sources: list[str] = []
    if cand.platform == "youtube":
        oembed = fetch_youtube_oembed(cand.canonical_url)
        if oembed is not None:
            _apply_oembed(cand, oembed)
            meta_sources.append("oembed")
    provider = select_provider(providers, cand.platform, cand.canonical_url)
    live = False
    if provider is not None and not cand.media_path:
        pm = provider.fetch_metadata(cand.canonical_url)
        if pm is not None:
            cand.title = pm.title or cand.title
            cand.creator_name = pm.creator_name or cand.creator_name
            cand.creator_channel_id = pm.creator_channel_id or cand.creator_channel_id
            cand.description = pm.description or cand.description
            cand.published_at = pm.published_at or cand.published_at
            cand.thumbnail_url = pm.thumbnail_url or cand.thumbnail_url
            if pm.duration_seconds:
                cand.duration_seconds = round(pm.duration_seconds, 3)
            live = pm.is_live
            meta_sources.append(provider.name)
    if cand.platform == "youtube" and not meta_sources:
        raise permanent(
            ErrorCode.INVALID_INPUT, "Videon är privat, borttagen eller kan inte läsas av ClipForge."
        )
    cand.metadata_json = {**(cand.metadata_json or {}), "metadata_sources": meta_sources}
    decision = acq.decide(cand, providers=providers)
    if not decision.allowed and decision.awaiting_media:
        raise _park_awaiting_media(ctx, cand, decision.reason or acq.NO_PROVIDER_MESSAGE)
    if not decision.allowed:
        cand.status = CandidateStatus.BLOCKED
        cand.blocked_reason = decision.reason
        return {"acquisition": "blocked", "reason": decision.reason}
    if (
        cand.duration_seconds
        and not cand.media_path
        and cand.duration_seconds > ctx.settings.max_input_duration_minutes * 60
    ):
        raise permanent(ErrorCode.MEDIA_TOO_LONG, "Videon är längre än tillåten maxlängd.")
    if live:
        raise _park_awaiting_media(
            ctx,
            cand,
            "Videon är en direktsändning eller premiär som inte är klar än. "
            "Försök igen när den har publicerats.",
        )
    cand.status = CandidateStatus.QUEUED
    cand.blocked_reason = None
    nxt = JobType.ANALYZE if decision.method == "local_file" else JobType.ACQUIRE
    _next(ctx, nxt, cand)
    return {"acquisition": decision.method}


def _is_retryable(e: BaseException) -> bool:
    return isinstance(e, JobError) and e.transient and not e.block and not e.awaiting_media


def handle_acquire(ctx: JobContext) -> dict[str, Any]:
    cand = _candidate(ctx)
    _require_rights(cand)
    decision = acq.decide(cand, providers=_providers(ctx))
    if not decision.allowed:
        if decision.awaiting_media:
            raise _park_awaiting_media(ctx, cand, decision.reason or acq.NO_PROVIDER_MESSAGE)
        raise blocked(ErrorCode.ACQUISITION_BLOCKED, decision.reason or "Hämtning blockerad.")
    if decision.method == "local_file" or decision.provider is None:
        _next(ctx, JobType.ANALYZE, cand)
        return {"method": "local_file"}
    s = ctx.settings
    max_bytes = s.max_input_file_mb * 1024 * 1024
    if not has_free_space(ctx.cfg.data_dir, s, max_bytes // 4):
        raise transient(ErrorCode.DISK_FULL, "För lite diskutrymme för att hämta media.", retry_after=1800)
    ctx.progress(1, "hämtar")
    # Transient failures keep the workspace so a retry can resume partial files (see workspace.release).
    with job_workspace(ctx.cfg, ctx.job.id, keep_if=_is_retryable) as ws:
        try:
            got = decision.provider.acquire(
                MediaRequest(
                    url=cand.canonical_url,
                    platform=cand.platform,
                    external_id=cand.external_id,
                    workdir=ws,
                    max_bytes=max_bytes,
                    max_duration_seconds=s.max_input_duration_minutes * 60,
                    max_height=s.youtube_max_height or None,
                    progress=ctx.progress,
                    cancelled=ctx.cancelled,
                )
            )
        except JobError as e:
            if e.awaiting_media:
                raise _park_awaiting_media(ctx, cand, e.safe_message) from e
            raise
        info = probe(got.path)
        if info.duration > s.max_input_duration_minutes * 60:
            raise permanent(ErrorCode.MEDIA_TOO_LONG, "Videon är längre än tillåten maxlängd.")
        if not info.has_video:
            raise permanent(ErrorCode.CORRUPT_MEDIA, "Hämtningen gav ingen videoström.")
        ext = "mp4" if "mp4" in info.format_name else ("webm" if "webm" in info.format_name else "mkv")
        final = ctx.cfg.media_dir / safe_filename(cand.title or got.title or "video", ext)
        final.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(got.path), final)
    invalidate_cache()
    if got.title and not cand.title:
        cand.title = got.title
    cand.media_path = str(final.relative_to(ctx.cfg.data_dir))
    cand.media_checksum = sha256_path(final)
    cand.duration_seconds = round(info.duration, 3)
    cand.blocked_reason = None
    _next(ctx, JobType.ANALYZE, cand)
    return {"method": got.provider, "bytes": final.stat().st_size, "provider": got.provider}


def handle_analyze(ctx: JobContext) -> dict[str, Any]:
    cand = _candidate(ctx)
    _require_rights(cand)
    path = _media_path(ctx, cand)
    cand.status = CandidateStatus.ANALYZING
    info = probe(path)
    if info.duration > ctx.settings.max_input_duration_minutes * 60:
        raise permanent(ErrorCode.MEDIA_TOO_LONG, "Videon är längre än tillåten maxlängd.")
    if not info.has_audio:
        raise permanent(ErrorCode.NO_AUDIO, "Videon saknar ljudspår och kan inte transkriberas.")
    if not info.has_video:
        raise permanent(ErrorCode.CORRUPT_MEDIA, "Filen saknar videospår och kan inte klippas till video.")
    cand.duration_seconds = round(info.duration, 3)
    cand.metadata_json = {
        **(cand.metadata_json or {}),
        "media": {
            "width": info.width,
            "height": info.height,
            "fps": info.fps,
            "rotation": info.rotation,
            "orientation": info.orientation,
            "video_codec": info.video_codec,
            "audio_codec": info.audio_codec,
            "size_bytes": info.size_bytes,
        },
    }
    _next(ctx, JobType.TRANSCRIBE, cand)
    return {"duration": info.duration, "orientation": info.orientation}


def handle_transcribe(ctx: JobContext) -> dict[str, Any]:
    cand = _candidate(ctx)
    _require_rights(cand)
    path = _media_path(ctx, cand)
    engine = (ctx.engine_factory or default_engine)(ctx.cfg, ctx.settings)
    st = engine.status()
    if not st["available"]:
        cand.transcript_status = "unavailable"
        raise blocked(ErrorCode.TRANSCRIPTION_UNAVAILABLE, str(st["reason"]))
    cand.transcript_status = "running"
    lang = None if ctx.settings.transcription_language == "auto" else ctx.settings.transcription_language
    t0 = utcnow()
    duration = cand.duration_seconds or 0
    # ~2 MB per minute of 16 kHz mono PCM, plus headroom.
    if not has_free_space(
        ctx.cfg.data_dir, ctx.settings, int(duration / 60 * 2.2 * 1024 * 1024) + 50_000_000
    ):
        raise transient(ErrorCode.DISK_FULL, "För lite diskutrymme för att extrahera ljud.", retry_after=1800)
    try:
        with job_workspace(ctx.cfg, ctx.job.id, fresh=True) as ws:
            ctx.progress(1, "extraherar ljud")
            wav = extract_audio(
                path,
                ws / "audio.wav",
                duration=duration or None,
                on_progress=lambda p: ctx.progress(p * 0.08, "extraherar ljud"),
                should_cancel=ctx.cancelled,
            )
            res = engine.transcribe(
                wav,
                language=lang,
                duration=duration,
                on_progress=lambda p: ctx.progress(8 + p * 0.92, "transkriberar"),
                should_cancel=ctx.cancelled,
            )
    except TranscriptionCancelled as e:
        raise Cancelled() from e
    if not res.segments:
        cand.transcript_status = "empty"
        raise permanent(ErrorCode.TRANSCRIPTION_FAILED, "Inget tal hittades i videon.")
    for old in ctx.db.scalars(select(Transcript).where(Transcript.candidate_video_id == cand.id)):
        ctx.db.delete(old)
    text = res.text
    ctx.db.add(
        Transcript(
            candidate_video_id=cand.id,
            language=res.language,
            text=text,
            segments_json=res.segments,
            engine_name=res.engine_name,
            engine_version=res.engine_version,
            model_name=res.model_name,
            duration_seconds=res.duration,
            processing_seconds=(utcnow() - t0).total_seconds(),
            checksum=hashlib.sha256(text.encode()).hexdigest(),
            status="complete",
        )
    )
    cand.transcript_status = "complete"
    _next(ctx, JobType.EXTRACT_CLIPS, cand)
    return {"language": res.language, "segments": len(res.segments), "warnings": res.warnings}


def _keywords(ctx: JobContext, cand: CandidateVideo) -> tuple[list[str], str | None]:
    if cand.source_id:
        src = ctx.db.get(Source, cand.source_id)
        if src:
            return list(src.keywords_json or []), src.category
    return [], (cand.metadata_json or {}).get("category")


def handle_extract_clips(ctx: JobContext) -> dict[str, Any]:
    cand = _candidate(ctx)
    _require_rights(cand)
    tr = ctx.db.scalar(
        select(Transcript)
        .where(Transcript.candidate_video_id == cand.id)
        .order_by(Transcript.created_at.desc())
    )
    if tr is None:
        raise blocked(ErrorCode.TRANSCRIPTION_UNAVAILABLE, "Transkription saknas.")
    duration = cand.duration_seconds or tr.duration_seconds or 0
    s = ctx.settings
    sentences = sentences_from_segments(tr.segments_json)
    provider, note = (ctx.provider_factory or default_provider)(ctx.cfg, s)
    if note:
        ctx.notes.append(note)
    keywords, category = _keywords(ctx, cand)
    limit = max(s.auto_render_top_n * 3, 6)
    wins = provider.propose(
        sentences,
        min_s=s.min_clip_seconds,
        max_s=s.max_clip_seconds,
        media_duration=duration,
        keywords=keywords,
        limit=limit,
    )
    if not wins and provider.name != "heuristic":
        wins = HeuristicProvider().propose(
            sentences,
            min_s=s.min_clip_seconds,
            max_s=s.max_clip_seconds,
            media_duration=duration,
            keywords=keywords,
            limit=limit,
        )
    # Replace earlier un-rendered proposals on reprocess; keep anything already rendered.
    for old in ctx.db.scalars(
        select(ClipCandidate).where(
            ClipCandidate.candidate_video_id == cand.id,
            ClipCandidate.status.in_([ClipStatus.PROPOSED, ClipStatus.SELECTED]),
        )
    ):
        if not old.renders:
            ctx.db.delete(old)
    ctx.db.flush()
    created: list[ClipCandidate] = []
    for w in wins:
        if not validate_range(
            w.start, w.end, media_duration=duration, min_s=s.min_clip_seconds, max_s=s.max_clip_seconds
        ):
            continue
        sc = w.score
        clip = ClipCandidate(
            candidate_video_id=cand.id,
            start_seconds=w.start,
            end_seconds=w.end,
            duration_seconds=round(w.end - w.start, 3),
            hook_score=sc.hook,
            coherence_score=sc.coherence,
            interest_score=sc.interest,
            emotional_energy_score=sc.energy,
            clarity_score=sc.clarity,
            channel_fit_score=sc.channel_fit,
            penalties_json=sc.penalties,
            final_score=sc.final,
            score_explanation=(w.rationale + " — " if w.rationale else "") + sc.explanation,
            warnings_json=sc.warnings,
            provider=w.provider,
            transcript_excerpt=w.excerpt,
            title_suggestion=w.title or suggest_title(w.excerpt),
            status=ClipStatus.PROPOSED,
        )
        ctx.db.add(clip)
        created.append(clip)
    ctx.db.flush()
    created.sort(key=lambda c: c.final_score, reverse=True)
    for c in created[: s.auto_render_top_n]:
        c.status = ClipStatus.SELECTED
        _next(ctx, JobType.RENDER, cand, key=f"render:{c.id}:auto", input_json={"clip_id": c.id})
    cand.status = CandidateStatus.READY
    return {
        "proposed": len(created),
        "auto_render": min(len(created), s.auto_render_top_n),
        "provider": provider.name,
        "keywords": len(keywords),
        "category": category,
    }


def _local_midnight(tz: str) -> tuple[datetime, datetime]:
    now = datetime.now(ZoneInfo(tz))
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def handle_render(ctx: JobContext) -> dict[str, Any]:
    cand = _candidate(ctx)
    _require_rights(cand)
    clip = ctx.db.get(ClipCandidate, ctx.job.input_json.get("clip_id"))
    if clip is None or clip.candidate_video_id != cand.id:
        raise permanent(ErrorCode.NOT_FOUND, "Klippförslaget finns inte längre.")
    s = ctx.settings
    day_start, day_end = _local_midnight(s.timezone)
    done_today = (
        ctx.db.scalar(
            select(func.count()).select_from(RenderedClip).where(RenderedClip.created_at >= day_start)
        )
        or 0
    )
    if done_today >= s.max_renders_per_day:
        raise Defer(day_end, "Dagens renderingsgräns är nådd; fortsätter i morgon.")
    path = _media_path(ctx, cand)
    need = int((clip.duration_seconds + 5) * 1.5 * 1024 * 1024)
    if not has_free_space(ctx.cfg.data_dir, s, need):
        raise transient(ErrorCode.DISK_FULL, "För lite diskutrymme för rendering.", retry_after=1800)
    tr = ctx.db.scalar(
        select(Transcript)
        .where(Transcript.candidate_video_id == cand.id)
        .order_by(Transcript.created_at.desc())
    )
    opts = ctx.job.input_json
    profile = opts.get("profile") or s.default_render_profile
    style = opts.get("caption_style") or s.default_caption_style
    crop_mode = opts.get("crop_mode") or s.default_crop_mode
    stem = safe_filename((cand.title or "klipp")[:40], "mp4").rsplit(".", 1)[0]
    work = job_dir(ctx.cfg, ctx.job.id) / "render"
    remove_workspace(ctx.cfg, ctx.job.id)  # start from a clean scratch directory
    req = RenderRequest(
        source=path,
        source_info=probe(path),
        start=clip.start_seconds,
        end=clip.end_seconds,
        segments=tr.segments_json if tr else [],
        profile=profile,
        caption_style=style,
        crop_mode=crop_mode,
        crop_offset=clip.crop_offset,
        loudnorm=s.loudness_normalization,
        font_path=s.caption_font_path,
        work_dir=work,
        out_video=ctx.cfg.clips_dir / f"{stem}.mp4",
        out_thumb=ctx.cfg.thumbnails_dir / f"{stem}.jpg",
        out_srt=ctx.cfg.captions_dir / f"{stem}.srt",
    )
    req.out_srt.parent.mkdir(parents=True, exist_ok=True)
    try:
        res = render_clip(req, on_progress=lambda p: ctx.progress(p, "renderar"), should_cancel=ctx.cancelled)
    except BaseException:
        req.out_srt.unlink(missing_ok=True)
        raise
    finally:
        remove_workspace(ctx.cfg, ctx.job.id)
    invalidate_cache()
    keywords, category = _keywords(ctx, cand)
    tags = suggest_hashtags(keywords, category)
    title = clip.title_suggestion or suggest_title(clip.transcript_excerpt)
    rel = ctx.cfg.data_dir
    rc = RenderedClip(
        clip_candidate_id=clip.id,
        file_path=str(req.out_video.relative_to(rel)),
        thumbnail_path=str(req.out_thumb.relative_to(rel)),
        caption_file_path=str(req.out_srt.relative_to(rel)),
        title_suggestion=title,
        description_suggestion=suggest_description(
            clip.transcript_excerpt,
            source_title=cand.title,
            creator=cand.creator_name,
            source_url=cand.canonical_url if cand.canonical_url.startswith("https://") else None,
            hashtags=tags,
        ),
        hashtags_json=tags,
        render_profile=profile,
        caption_style=style,
        crop_mode=crop_mode,
        tool_version=res.tool_version,
        duration_seconds=res.duration,
        width=res.width,
        height=res.height,
        file_size_bytes=res.size_bytes,
        checksum=res.checksum,
        warnings_json=res.warnings,
        render_seconds=res.seconds,
    )
    ctx.db.add(rc)
    clip.status = ClipStatus.RENDERED
    ctx.db.flush()
    audit.record(
        ctx.db,
        "clip.rendered",
        object_type="rendered_clip",
        object_id=rc.id,
        details={"profile": profile, "seconds": res.seconds},
    )
    return {"rendered_clip_id": rc.id, "seconds": res.seconds, "warnings": res.warnings}


def handle_discover(ctx: JobContext) -> dict[str, Any]:
    src = ctx.db.get(Source, ctx.job.source_id) if ctx.job.source_id else None
    if src is None:
        raise permanent(ErrorCode.NOT_FOUND, "Källan finns inte längre.")
    if ctx.discovery_factory is None:
        raise permanent(ErrorCode.INTERNAL, "Ingen upptäcktsadapter konfigurerad.")
    adapter = ctx.discovery_factory(src)
    now = utcnow()
    src.last_checked_at = now
    try:
        result: DiscoveryResult = adapter.discover(src)
    except JobError as e:
        src.last_error = e.safe_message
        src.consecutive_failures += 1
        raise
    src.last_success_at = now
    src.last_error = None
    src.consecutive_failures = 0
    src.cursor_json = result.cursor or src.cursor_json
    priority = Priority.CREATOR if src.kind in ("youtube_channel", "rss") else Priority.DISCOVERY
    new = 0
    for item in result.items:
        exists = ctx.db.scalar(
            select(CandidateVideo.id).where(CandidateVideo.canonical_url == item.canonical_url)
        )
        if exists:
            continue
        cand = CandidateVideo(
            source_id=src.id,
            origin="discovery",
            canonical_url=item.canonical_url,
            platform=item.platform,
            external_id=item.external_id,
            title=item.title,
            creator_name=item.creator_name,
            creator_channel_id=item.creator_channel_id,
            description=item.description,
            published_at=item.published_at,
            thumbnail_url=item.thumbnail_url,
            priority=priority,
            rights_status=src.rights_status,
            rights_note=src.rights_note,
            media_path=item.media_rel_path,
            media_checksum=item.media_checksum,
            media_is_user_original=item.media_rel_path is not None,
            metadata_json={"category": src.category, **item.metadata},
            status=CandidateStatus.DISCOVERED,
        )
        ctx.db.add(cand)
        ctx.db.flush()
        q.enqueue(
            ctx.db, JobType.FETCH_METADATA, priority=priority, candidate_video_id=cand.id, source_id=src.id
        )
        new += 1
    audit.record(
        ctx.db,
        "source.checked",
        object_type="source",
        object_id=src.id,
        details={"new": new, "seen": len(result.items), "quota_units": result.quota_units},
    )
    return {"new": new, "seen": len(result.items), "quota_units": result.quota_units}


HANDLERS: dict[str, Callable[[JobContext], dict[str, Any]]] = {
    JobType.FETCH_METADATA: handle_fetch_metadata,
    JobType.ACQUIRE: handle_acquire,
    JobType.ANALYZE: handle_analyze,
    JobType.TRANSCRIBE: handle_transcribe,
    JobType.EXTRACT_CLIPS: handle_extract_clips,
    JobType.RENDER: handle_render,
    JobType.DISCOVER: handle_discover,
}


def on_job_failed(db: Session, job: Job) -> None:
    """Reflect a job's terminal failure/block on its candidate video."""
    if not job.candidate_video_id:
        return
    cand = db.get(CandidateVideo, job.candidate_video_id)
    if cand is None:
        return
    if job.status == "blocked":
        cand.status = CandidateStatus.BLOCKED
        cand.blocked_reason = job.safe_error_message
    elif job.status == "awaiting_media":
        cand.status = CandidateStatus.AWAITING_MEDIA
        cand.blocked_reason = job.safe_error_message
    elif job.status == "failed" and job.job_type != JobType.RENDER:
        cand.status = CandidateStatus.FAILED
        cand.last_error = job.safe_error_message
