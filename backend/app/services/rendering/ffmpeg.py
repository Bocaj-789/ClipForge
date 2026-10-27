"""Safe ffmpeg/ffprobe wrappers. Arguments are always passed as lists, never via a shell."""

from __future__ import annotations

import json
import logging
import os
import select
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.config import get_config
from app.core.errors import ErrorCode, JobError, permanent

log = logging.getLogger("clipforge.ffmpeg")

CHECK_INTERVAL = 0.5  # seconds between cancel/timeout checks while ffmpeg runs


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    width: int | None
    height: int | None
    fps: float | None
    rotation: int
    has_video: bool
    has_audio: bool
    video_codec: str | None
    audio_codec: str | None
    format_name: str
    size_bytes: int

    @property
    def display_size(self) -> tuple[int, int] | None:
        if not self.width or not self.height:
            return None
        if self.rotation in (90, 270):
            return self.height, self.width
        return self.width, self.height

    @property
    def orientation(self) -> str:
        size = self.display_size
        if not size:
            return "unknown"
        w, h = size
        if abs(w - h) / max(w, h) < 0.05:
            return "square"
        return "landscape" if w > h else "portrait"


def ffmpeg_bin() -> str:
    return get_config().ffmpeg_bin


def ffprobe_bin() -> str:
    return get_config().ffprobe_bin


def available() -> tuple[bool, str | None]:
    for b in (ffmpeg_bin(), ffprobe_bin()):
        if shutil.which(b) is None:
            return False, f"{b} hittades inte. Installera med: brew install ffmpeg"
    return True, None


@lru_cache
def ffmpeg_version() -> str:
    try:
        out = subprocess.run(
            [ffmpeg_bin(), "-hide_banner", "-version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return out.stdout.splitlines()[0][:120] if out.stdout else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"


def _ensure() -> None:
    ok, reason = available()
    if not ok:
        raise JobError(ErrorCode.FFMPEG_MISSING, reason or "ffmpeg saknas", transient=False, block=True)


def _fraction(v: str | None) -> float | None:
    if not v or v in ("0/0", "N/A"):
        return None
    try:
        if "/" in v:
            a, b = v.split("/", 1)
            return float(a) / float(b) if float(b) else None
        return float(v)
    except ValueError:
        return None


def _rotation(stream: dict) -> int:
    rot = 0
    tag = (stream.get("tags") or {}).get("rotate")
    if tag:
        try:
            rot = int(tag)
        except ValueError:
            rot = 0
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                rot = int(float(sd["rotation"]))
            except (TypeError, ValueError):
                pass
    return rot % 360


def probe(path: Path, *, timeout: float = 60) -> MediaInfo:
    """Inspect a media file. Raises a permanent JobError if it is not valid media."""
    _ensure()
    cmd = [ffprobe_bin(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        raise permanent(ErrorCode.CORRUPT_MEDIA, "Mediefilen kunde inte analyseras (tidsgräns).") from e
    if out.returncode != 0:
        log.warning("ffprobe failed: %s", out.stderr[-500:])
        raise permanent(ErrorCode.CORRUPT_MEDIA, "Filen är inte en giltig video- eller ljudfil.")
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError as e:
        raise permanent(ErrorCode.CORRUPT_MEDIA, "Oväntat svar från ffprobe.") from e
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    v = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = _fraction(fmt.get("duration")) or (_fraction(v.get("duration")) if v else None) or 0.0
    return MediaInfo(
        duration=float(duration),
        width=int(v["width"]) if v and v.get("width") else None,
        height=int(v["height"]) if v and v.get("height") else None,
        fps=_fraction(v.get("avg_frame_rate")) if v else None,
        rotation=_rotation(v) if v else 0,
        has_video=v is not None,
        has_audio=a is not None,
        video_codec=v.get("codec_name") if v else None,
        audio_codec=a.get("codec_name") if a else None,
        format_name=str(fmt.get("format_name", "")),
        size_bytes=int(fmt.get("size") or path.stat().st_size),
    )


class Cancelled(Exception):
    pass


def _report_progress(
    raw: bytes, expected_duration: float | None, on_progress: Callable[[float], None] | None
) -> None:
    """Parse one line of ffmpeg's ``-progress pipe:1`` output."""
    if not expected_duration or not on_progress:
        return
    line = raw.decode(errors="replace").strip()
    if not line.startswith("out_time_us="):
        return
    try:
        us = int(line.split("=", 1)[1])
    except ValueError:
        return
    on_progress(min(99.0, 100.0 * us / 1e6 / expected_duration))


def run_ffmpeg(
    args: Sequence[str],
    *,
    expected_duration: float | None = None,
    on_progress: Callable[[float], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    timeout: float = 3 * 3600,
) -> None:
    """Run ffmpeg with progress reporting and cooperative cancellation.

    Cancellation and the timeout are checked on a timer, not only when ffmpeg prints progress,
    so a stalled ffmpeg that stops writing output still gets cancelled and killed.
    """
    _ensure()
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats", *args]
    with tempfile.TemporaryFile(mode="w+b") as errf:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, stdin=subprocess.DEVNULL)
        start = time.monotonic()
        last_check = 0.0
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        pending = b""
        try:
            eof = False
            while not eof:
                ready, _, _ = select.select([fd], [], [], CHECK_INTERVAL)
                if ready:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        eof = True
                    else:
                        *lines, pending = (pending + chunk).split(b"\n")
                        for raw in lines:
                            _report_progress(raw, expected_duration, on_progress)
                now = time.monotonic()
                if now - last_check >= CHECK_INTERVAL:
                    last_check = now
                    if should_cancel and should_cancel():
                        raise Cancelled()
                    if now - start > timeout:
                        raise TimeoutError()
            proc.wait(timeout=30)
        except (Cancelled, TimeoutError, subprocess.TimeoutExpired) as e:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            if isinstance(e, Cancelled):
                raise
            raise permanent(ErrorCode.RENDER_FAILED, "Renderingen tog för lång tid och stoppades.") from e
        except BaseException:
            # Unexpected error: never leave ffmpeg running behind a failed job.
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            raise
        if proc.returncode != 0:
            errf.seek(0)
            tail = errf.read()[-2000:].decode(errors="replace")
            log.error("ffmpeg exited %s: %s", proc.returncode, tail)
            raise JobError(
                ErrorCode.RENDER_FAILED, "ffmpeg misslyckades. Se loggen för detaljer.", transient=False
            )
