"""Per-job temporary directories.

Every job that needs scratch space (downloads, extracted audio, render intermediates) works in
``<data>/tmp/job-<job id>``. The directory is created on demand and removed when the job ends,
whatever the outcome. A transient download failure keeps its directory so the retry can resume
partial files; orphans from crashes are swept by :func:`cleanup_orphans`.
"""

from __future__ import annotations

import logging
import re
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import Config
from app.db.models import Job, JobStatus, JobType
from app.services.storage.manager import invalidate_cache

log = logging.getLogger("clipforge.workspace")

PREFIX = "job-"
_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")


def job_dir(cfg: Config, job_id: str) -> Path:
    if not _ID.match(job_id):
        raise ValueError("invalid job id")
    return cfg.tmp_dir / f"{PREFIX}{job_id}"


def remove(cfg: Config, job_id: str) -> None:
    shutil.rmtree(job_dir(cfg, job_id), ignore_errors=True)
    invalidate_cache()


@contextmanager
def job_workspace(
    cfg: Config,
    job_id: str,
    *,
    fresh: bool = False,
    keep_if: Callable[[BaseException], bool] | None = None,
) -> Iterator[Path]:
    """Create the job's scratch directory and remove it afterwards.

    ``fresh=True`` discards leftovers from an earlier attempt first. ``keep_if`` may veto the
    removal for a given exception (a retryable download keeps its partial files for the resume).
    """
    path = job_dir(cfg, job_id)
    if fresh:
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    except BaseException as e:
        if keep_if is None or not keep_if(e):
            shutil.rmtree(path, ignore_errors=True)
            invalidate_cache()
        raise
    else:
        shutil.rmtree(path, ignore_errors=True)
        invalidate_cache()


def release(cfg: Config, job: Job) -> None:
    """Called by the worker after a job attempt. Keeps only a retrying download's partial files."""
    if job.status == JobStatus.RETRY_WAIT and job.job_type == JobType.ACQUIRE:
        return
    remove(cfg, job.id)


def cleanup_orphans(db: Session, cfg: Config) -> int:
    """Delete job directories whose job is gone or no longer needs scratch space."""
    if not cfg.tmp_dir.exists():
        return 0
    removed = 0
    for p in cfg.tmp_dir.iterdir():
        if not (p.is_dir() and not p.is_symlink() and p.name.startswith(PREFIX)):
            continue
        job_id = p.name.removeprefix(PREFIX)
        job = db.get(Job, job_id)
        keep = job is not None and (
            job.status == JobStatus.RUNNING
            or (job.status in (JobStatus.PENDING, JobStatus.RETRY_WAIT) and job.job_type == JobType.ACQUIRE)
        )
        if keep:
            continue
        shutil.rmtree(p, ignore_errors=True)
        removed += 1
    if removed:
        invalidate_cache()
        log.info("removed %s orphaned job directories", removed)
    return removed
