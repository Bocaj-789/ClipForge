"""Extract a speech-ready audio track with FFmpeg (16 kHz mono PCM WAV, the format Whisper uses)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from app.core.errors import ErrorCode, permanent
from app.services.rendering.ffmpeg import run_ffmpeg


def extract_audio(
    source: Path,
    dest: Path,
    *,
    duration: float | None = None,
    on_progress: Callable[[float], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Path:
    """Write ``dest`` (WAV, 16 kHz, mono). Raises a permanent JobError if nothing was produced."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".part.wav")
    run_ffmpeg(
        [
            "-i",
            str(source),
            "-vn",
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            str(part),
        ],
        expected_duration=duration,
        on_progress=on_progress,
        should_cancel=should_cancel,
    )
    if not part.is_file() or part.stat().st_size < 1000:
        part.unlink(missing_ok=True)
        raise permanent(ErrorCode.NO_AUDIO, "Ljudspåret kunde inte extraheras.")
    part.replace(dest)
    return dest
