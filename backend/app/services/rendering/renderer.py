"""Render a clip range into a validated vertical MP4 with burned-in captions."""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.core.errors import ErrorCode, permanent
from app.services.rendering import captions as cap
from app.services.rendering.crop import video_filter
from app.services.rendering.ffmpeg import MediaInfo, ffmpeg_version, probe, run_ffmpeg
from app.services.rendering.profiles import get_profile


@dataclass
class RenderRequest:
    source: Path
    source_info: MediaInfo
    start: float
    end: float
    segments: list[dict]
    profile: str
    caption_style: str  # clean | bold | karaoke | none
    crop_mode: str
    crop_offset: float | None
    loudnorm: bool
    font_path: str | None
    work_dir: Path  # temp dir (inside data/tmp)
    out_video: Path
    out_thumb: Path
    out_srt: Path


@dataclass
class RenderResult:
    duration: float
    width: int
    height: int
    size_bytes: int
    checksum: str
    tool_version: str
    seconds: float
    warnings: list[str] = field(default_factory=list)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_args(req: RenderRequest, caption_list: Path | None, part: Path) -> list[str]:
    prof = get_profile(req.profile)
    dur = req.end - req.start
    args = ["-ss", f"{req.start:.3f}", "-t", f"{dur:.3f}", "-i", str(req.source)]
    graph = video_filter(req.crop_mode, prof.width, prof.height, prof.fps, req.crop_offset)
    if caption_list is not None:
        args += ["-f", "concat", "-i", str(caption_list)]
        band_y = int(prof.height * 0.58)
        graph += (
            f";[base][1:v]overlay=x=0:y={band_y}:format=auto:eof_action=pass[cv];[cv]format=yuv420p[vout]"
        )
    else:
        graph += ";[base]format=yuv420p[vout]"
    if req.source_info.has_audio:
        afilter = "aresample=48000"
        if req.loudnorm:
            afilter = "loudnorm=I=-14:TP=-1.5:LRA=11," + afilter
        graph += f";[0:a:0]{afilter}[aout]"
    args += ["-filter_complex", graph, "-map", "[vout]"]
    if req.source_info.has_audio:
        args += ["-map", "[aout]", "-c:a", "aac", "-b:a", prof.audio_bitrate, "-ar", "48000"]
    args += [
        *prof.video_args,
        "-r",
        str(prof.fps),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-t",
        f"{dur:.3f}",
        "-f",
        "mp4",
        str(part),
    ]
    return args


def render_clip(
    req: RenderRequest,
    *,
    on_progress: Callable[[float], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> RenderResult:
    t0 = time.monotonic()
    warnings: list[str] = []
    dur = req.end - req.start
    if dur <= 0.5 or req.end > req.source_info.duration + 0.5:
        raise permanent(ErrorCode.INVALID_INPUT, "Klippets tidsintervall är ogiltigt för källfilen.")
    if not req.source_info.has_video:
        raise permanent(ErrorCode.CORRUPT_MEDIA, "Källan saknar videoström.")
    prof = get_profile(req.profile)
    disp = req.source_info.display_size
    if disp and req.crop_mode == "center_crop":
        crop_h = min(disp[1], disp[0] * 16 / 9)
        if crop_h < prof.height * 0.5:
            warnings.append("Källan har låg upplösning; beskärningen skalas upp kraftigt.")
    if (
        req.crop_offset is None
        and req.crop_mode == "center_crop"
        and req.source_info.orientation == "landscape"
    ):
        warnings.append("Centrerad beskärning utan motivspårning – kontrollera att talaren syns.")

    req.work_dir.mkdir(parents=True, exist_ok=True)
    caption_list = None
    words = cap.words_for_range(req.segments, req.start, req.end)
    cues = cap.build_cues(words, max_words=3 if req.caption_style in ("bold", "karaoke") else 5)
    req.out_srt.write_text(cap.to_srt(cues), encoding="utf-8")
    if req.caption_style != "none" and cues:
        font = cap.resolve_font(req.font_path)
        caption_list = cap.build_caption_track(
            cues,
            clip_duration=dur,
            work_dir=req.work_dir / "captions",
            canvas_w=prof.width,
            band_h=int(prof.height * 0.16),
            style_name=req.caption_style,
            font_path=font,
        )
    elif not cues:
        warnings.append("Inga ord i transkriptionen för detta intervall – inga undertexter.")

    part = req.work_dir / "render.part.mp4"
    run_ffmpeg(
        build_args(req, caption_list, part),
        expected_duration=dur,
        on_progress=on_progress,
        should_cancel=should_cancel,
    )

    info = probe(part)
    if not info.has_video or (info.width, info.height) != (prof.width, prof.height):
        raise permanent(ErrorCode.RENDER_FAILED, "Den renderade filen har fel format.")
    if req.source_info.has_audio and not info.has_audio:
        raise permanent(ErrorCode.RENDER_FAILED, "Den renderade filen saknar ljud.")
    if abs(info.duration - dur) > 1.0:
        raise permanent(ErrorCode.RENDER_FAILED, "Den renderade filens längd stämmer inte.")

    # Poster frame from the middle of the clip.
    thumb_part = req.work_dir / "thumb.part.jpg"
    run_ffmpeg(
        [
            "-ss",
            f"{info.duration / 2:.2f}",
            "-i",
            str(part),
            "-frames:v",
            "1",
            "-vf",
            "scale=540:-2",
            "-q:v",
            "3",
            "-f",
            "image2",
            str(thumb_part),
        ]
    )

    checksum = sha256_file(part)
    req.out_video.parent.mkdir(parents=True, exist_ok=True)
    req.out_thumb.parent.mkdir(parents=True, exist_ok=True)
    os.replace(part, req.out_video)  # atomic within the same filesystem
    os.replace(thumb_part, req.out_thumb)
    shutil.rmtree(req.work_dir, ignore_errors=True)
    return RenderResult(
        duration=info.duration,
        width=prof.width,
        height=prof.height,
        size_bytes=req.out_video.stat().st_size,
        checksum=checksum,
        tool_version=ffmpeg_version(),
        seconds=round(time.monotonic() - t0, 2),
        warnings=warnings,
    )
