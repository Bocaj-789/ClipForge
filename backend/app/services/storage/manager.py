"""Disk budget accounting and cleanup of stale temporary files."""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from app.services.settings_service import AppSettings

_cache: dict[str, tuple[float, int]] = {}


def dir_size(path: Path, *, max_age: float = 30.0) -> int:
    key = str(path)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < max_age:
        return hit[1]
    total = 0
    if path.exists():
        for root, _dirs, files in os.walk(path, followlinks=False):
            for f in files:
                try:
                    total += (Path(root) / f).lstat().st_size
                except OSError:
                    continue
    _cache[key] = (time.monotonic(), total)
    return total


def invalidate_cache() -> None:
    _cache.clear()


@dataclass
class StorageStatus:
    used_bytes: int
    budget_bytes: int
    used_percent: float
    disk_free_bytes: int
    disk_total_bytes: int
    level: str  # ok | warn | critical
    reason: str | None
    breakdown: dict[str, int]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def storage_status(data_dir: Path, settings: AppSettings, *, max_age: float = 30.0) -> StorageStatus:
    breakdown = {
        name: dir_size(data_dir / name, max_age=max_age)
        for name in ("media", "clips", "thumbnails", "captions", "tmp", "models", "logs", "backups")
    }
    db_bytes = sum(p.stat().st_size for p in data_dir.glob("clipforge.sqlite3*") if p.is_file())
    breakdown["database"] = db_bytes
    used = sum(breakdown.values())
    budget = int(settings.storage_budget_gb * 1024**3)
    du = shutil.disk_usage(data_dir)
    pct = 100.0 * used / budget if budget else 100.0
    level, reason = "ok", None
    min_free = int(settings.min_free_disk_gb * 1024**3)
    if du.free < min_free:
        level, reason = "critical", "För lite ledigt diskutrymme på datorn."
    elif pct >= settings.storage_critical_percent:
        level, reason = "critical", "ClipForges lagringsbudget är nästan full."
    elif pct >= settings.storage_warn_percent:
        level, reason = "warn", "ClipForges lagringsbudget börjar ta slut."
    return StorageStatus(used, budget, round(pct, 1), du.free, du.total, level, reason, breakdown)


def has_free_space(data_dir: Path, settings: AppSettings, needed_bytes: int) -> bool:
    du = shutil.disk_usage(data_dir)
    return du.free - needed_bytes > settings.min_free_disk_gb * 1024**3


def newest_mtime(path: Path) -> float:
    """Latest modification time inside a tree, so a directory with files still being written is fresh."""
    latest = path.lstat().st_mtime
    if path.is_dir() and not path.is_symlink():
        for root, _dirs, files in path.walk():
            for f in files:
                try:
                    latest = max(latest, (root / f).lstat().st_mtime)
                except OSError:
                    continue
    return latest


def cleanup_stale_temp(tmp_dir: Path, *, older_than_seconds: float = 6 * 3600) -> int:
    """Remove leftovers from interrupted downloads/renders. Only touches the tmp dir."""
    if not tmp_dir.exists():
        return 0
    removed = 0
    cutoff = time.time() - older_than_seconds
    for p in tmp_dir.iterdir():
        try:
            if newest_mtime(p) < cutoff:
                if p.is_dir() and not p.is_symlink():
                    shutil.rmtree(p)
                else:
                    p.unlink()
                removed += 1
        except OSError:
            continue
    invalidate_cache()
    return removed
