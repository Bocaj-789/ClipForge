"""Store user-provided media files safely inside the data directory."""

from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from pathlib import Path

from starlette.concurrency import run_in_threadpool

from app.config import Config
from app.core.errors import AppError, ErrorCode
from app.core.security import safe_filename
from app.services.rendering.ffmpeg import MediaInfo, probe
from app.services.settings_service import AppSettings
from app.services.storage.manager import has_free_space, invalidate_cache

ALLOWED_EXT = {"mp4", "mov", "m4v", "mkv", "webm", "avi", "mpg", "mpeg", "m4a", "mp3", "wav"}


@dataclass
class StoredMedia:
    rel_path: str
    checksum: str
    size: int
    info: MediaInfo


def _check_ext(original_name: str) -> str:
    ext = Path(original_name).suffix.lstrip(".").lower()
    if ext not in ALLOWED_EXT:
        raise AppError(ErrorCode.INVALID_INPUT, "Filtypen stöds inte. Använd t.ex. MP4, MOV, MKV eller WEBM.")
    return ext


def _prepare(cfg: Config, settings: AppSettings, ext: str) -> Path:
    if not has_free_space(cfg.data_dir, settings, 0):
        raise AppError(
            ErrorCode.DISK_FULL, "För lite ledigt diskutrymme för att ta emot filen.", status_code=507
        )
    cfg.tmp_dir.mkdir(parents=True, exist_ok=True)
    return cfg.tmp_dir / safe_filename("upload", ext + ".part")


def _too_large(settings: AppSettings) -> AppError:
    return AppError(
        ErrorCode.MEDIA_TOO_LARGE, f"Filen är större än {settings.max_input_file_mb} MB.", status_code=413
    )


def _finalize(
    tmp: Path, original_name: str, ext: str, digest: str, size: int, cfg: Config, settings: AppSettings
) -> StoredMedia:
    if size == 0:
        raise AppError(ErrorCode.INVALID_INPUT, "Filen är tom.")
    try:
        info = probe(tmp)  # content-based validation, not the filename
    except Exception as e:
        raise AppError(ErrorCode.CORRUPT_MEDIA, "Filen är inte en giltig mediefil.") from e
    if info.duration <= 0 or not info.has_video:
        raise AppError(ErrorCode.CORRUPT_MEDIA, "Filen saknar videospår.")
    if info.duration > settings.max_input_duration_minutes * 60:
        raise AppError(
            ErrorCode.MEDIA_TOO_LONG, f"Filen är längre än {settings.max_input_duration_minutes} minuter."
        )
    cfg.media_dir.mkdir(parents=True, exist_ok=True)
    final = cfg.media_dir / safe_filename(Path(original_name).stem, ext)
    os.replace(tmp, final)
    invalidate_cache()
    return StoredMedia(str(final.relative_to(cfg.data_dir)), digest, size, info)


def store_stream(
    chunks: Iterable[bytes], original_name: str, cfg: Config, settings: AppSettings
) -> StoredMedia:
    ext = _check_ext(original_name)
    tmp = _prepare(cfg, settings, ext)
    h, size, cap = hashlib.sha256(), 0, settings.max_input_file_mb * 1024 * 1024
    try:
        with tmp.open("wb") as fh:
            for chunk in chunks:
                size += len(chunk)
                if size > cap:
                    raise _too_large(settings)
                h.update(chunk)
                fh.write(chunk)
        return _finalize(tmp, original_name, ext, h.hexdigest(), size, cfg, settings)
    finally:
        tmp.unlink(missing_ok=True)


async def store_async(
    chunks: AsyncIterator[bytes], original_name: str, cfg: Config, settings: AppSettings
) -> StoredMedia:
    """Stream a request body straight to disk (no in-memory buffering of large files)."""
    ext = _check_ext(original_name)
    tmp = _prepare(cfg, settings, ext)
    h, size, cap = hashlib.sha256(), 0, settings.max_input_file_mb * 1024 * 1024
    try:
        with tmp.open("wb") as fh:
            async for chunk in chunks:
                size += len(chunk)
                if size > cap:
                    raise _too_large(settings)
                h.update(chunk)
                fh.write(chunk)
        return await run_in_threadpool(_finalize, tmp, original_name, ext, h.hexdigest(), size, cfg, settings)
    finally:
        tmp.unlink(missing_ok=True)


def sha256_path(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
