"""Background worker process: lane threads, persistent scheduler, heartbeats, graceful shutdown.

Run with:  python -m app.workers.main
"""

from __future__ import annotations

import logging
import os
import random
import signal
import socket
import threading
import time
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update

from app.config import Config, get_config
from app.core.errors import ErrorCode, JobError
from app.db.base import utcnow
from app.db.migrate import upgrade_to_head
from app.db.models import CandidateVideo, Channel, Job, JobStatus, JobType, Source, WorkerHeartbeat
from app.db.session import init_engine, new_session, session_scope
from app.logging_config import setup_logging
from app.services import agent, pipeline, retention, workspace
from app.services.discovery.adapters import LocalFolderAdapter, RssAdapter, YouTubeApiAdapter
from app.services.discovery.cc_finder import YouTubeCcSearchAdapter
from app.services.publishing import quota, uploader
from app.services.publishing import service as publishing
from app.services.publishing.deps import PublishDeps, make_deps
from app.services.queue import service as q
from app.services.queue.states import DISK_HEAVY_TYPES, Priority
from app.services.rendering import ffmpeg
from app.services.rendering.ffmpeg import Cancelled
from app.services.settings_service import AppSettings, load_settings
from app.services.storage.manager import cleanup_stale_temp, storage_status

log = logging.getLogger("clipforge.worker")
FAIRNESS_EVERY = 5
# Lanes that share the max_concurrent_jobs limit. Other lanes keep their own single thread.
CAPPED_LANES = frozenset({"download", "heavy"})


class Worker:
    def __init__(
        self,
        cfg: Config,
        *,
        lanes: tuple[str, ...] = ("heavy", "download", "io", "light"),
        publish_deps: PublishDeps | None = None,
    ) -> None:
        self.cfg = cfg
        self.publish_deps = publish_deps or make_deps(cfg)
        self.worker_id = f"{socket.gethostname()}-{os.getpid()}"
        self.lanes = lanes
        self.shutdown = threading.Event()
        self.current: dict[str, str] = {}
        self.progress_state: dict[str, dict[str, Any]] = {}
        self.degraded: dict[str, str] = {}
        self._claims: dict[str, int] = {}
        self._lock = threading.Lock()
        self._slots: threading.BoundedSemaphore | None = None  # set in start() from max_concurrent_jobs

    # --------------------------------------------------------- factories
    def discovery_factory(self, src: Source) -> Any:
        s = self._settings()
        if src.kind == "rss":
            return RssAdapter(s.allowed_direct_media_hosts)
        if src.kind == "youtube_channel":
            key = self.cfg.youtube_api_key.get_secret_value() if self.cfg.youtube_api_key else None
            return YouTubeApiAdapter(key)
        if src.kind == "youtube_cc_search":
            return self._cc_adapter()
        if src.kind == "local_folder":
            return LocalFolderAdapter(self.cfg)
        raise JobError(ErrorCode.INVALID_INPUT, "Källtypen stöds inte.", transient=False, block=True)

    def _cc_adapter(self) -> YouTubeCcSearchAdapter:
        key = self.cfg.youtube_api_key.get_secret_value() if self.cfg.youtube_api_key else None
        with session_scope() as db:
            s = load_settings(db)
            backlog = (
                db.scalar(
                    select(func.count())
                    .select_from(CandidateVideo)
                    .where(CandidateVideo.status.in_(["discovered", "queued", "analyzing"]))
                )
                or 0
            )
            return YouTubeCcSearchAdapter(
                key,
                search_budget=quota.remaining(db, s, quota.SEARCH),
                unit_budget=quota.remaining(db, s, quota.UNITS),
                backlog=backlog,
                max_input_minutes=s.max_input_duration_minutes,
            )

    def _settings(self) -> AppSettings:
        with session_scope() as db:
            return load_settings(db)

    # --------------------------------------------------------- health
    def check_health(self) -> None:
        s = self._settings()
        deg: dict[str, str] = {}
        ok, reason = ffmpeg.available()
        if not ok:
            deg["ffmpeg"] = reason or "ffmpeg saknas"
        st = pipeline.default_engine(self.cfg, s).status()
        if not st["available"]:
            deg["transcription"] = str(st["reason"])
        if s.llm_provider == "ollama":
            _p, note = pipeline.default_provider(self.cfg, s)
            if note:
                deg["llm"] = note
        if s.youtube_media_provider != "off":
            for prov in pipeline.default_providers(self.cfg, s):
                ok_p, why = prov.available()
                if not ok_p:
                    deg["media_provider"] = why or f"{prov.name} är inte tillgänglig."
        storage = storage_status(self.cfg.data_dir, s)
        if storage.level == "critical":
            deg["storage"] = f"{storage.reason} Nya hämtningar och renderingar är pausade."
        with session_scope() as db:
            connected = db.scalar(
                select(func.count()).select_from(Channel).where(Channel.token_reference.is_not(None))
            )
        if connected:
            store_ok, store_why = self.publish_deps.store.available()
            if not store_ok:
                deg["token_store"] = store_why or "Säker tokenlagring saknas; uppladdningar kan inte köras."
        with self._lock:
            self.degraded = deg

    def heartbeat(self) -> None:
        with session_scope() as db:
            hb = db.get(WorkerHeartbeat, self.worker_id)
            if hb is None:
                hb = WorkerHeartbeat(worker_id=self.worker_id)
                db.add(hb)
            hb.last_seen_at = utcnow()
            hb.stopped_at = None
            with self._lock:
                hb.current_job_ids = list(self.current.values())
                hb.degraded_json = dict(self.degraded)

    # --------------------------------------------------------- scheduler
    def schedule_due_sources(self) -> int:
        n = 0
        with session_scope() as db:
            s = load_settings(db)
            if not s.discovery_enabled:
                return 0
            now = utcnow()
            due = db.scalars(
                select(Source).where(
                    Source.enabled.is_(True),
                    Source.paused.is_(False),
                    Source.kind != "manual_url",
                    (Source.next_check_at.is_(None)) | (Source.next_check_at <= now),
                )
            ).all()
            day_ago = now - timedelta(days=1)
            discovered_today = (
                db.scalar(
                    select(func.count())
                    .select_from(Job)
                    .where(Job.job_type == JobType.DISCOVER, Job.created_at >= day_ago)
                )
                or 0
            )
            for src in due:
                if src.kind not in s.allowed_source_kinds or discovered_today >= s.max_discoveries_per_day:
                    continue
                interval = max(15, src.polling_interval_minutes)
                # Back off sources that keep failing; jitter avoids bursts.
                factor = min(16, 2 ** min(src.consecutive_failures, 4))
                jitter = random.uniform(0, interval * 0.1)  # noqa: S311
                src.next_check_at = now + timedelta(minutes=interval * factor + jitter)
                pri = Priority.CREATOR if src.kind in ("youtube_channel", "rss") else Priority.DISCOVERY
                q.enqueue(
                    db,
                    JobType.DISCOVER,
                    priority=pri,
                    source_id=src.id,
                    idempotency_key=f"discover:{src.id}:{now.strftime('%Y%m%d%H%M')}",
                )
                discovered_today += 1
                n += 1
        return n

    def refresh_publications(self) -> int:
        """Read back what YouTube reports for videos that are still processing or scheduled."""
        with session_scope() as db:
            pubs = uploader.due_for_refresh(db)
            if not pubs:
                return 0
            return uploader.refresh_availability(db, self.publish_deps, load_settings(db), pubs)

    def maintenance_loop(self) -> None:
        last_health = 0.0
        last_sched = 0.0
        last_cleanup = 0.0
        last_refresh = time.monotonic()  # first status refresh 10 minutes after start
        last_retention = 0.0
        while not self.shutdown.is_set():
            t = time.monotonic()
            try:
                if t - last_health > 60:
                    self.check_health()
                    last_health = t
                self.heartbeat()
                with session_scope() as db:
                    q.recover_expired_leases(db)
                    publishing.reconcile(db)
                    workspace.cleanup_orphans(db, self.cfg)
                    ctl = agent.get_control(db)
                    if ctl.desired_state == "draining" and not self.current:
                        agent.set_desired(db, "stopped", reason="Tömning klar.", actor="system")
                if t - last_sched > 30:
                    self.schedule_due_sources()
                    last_sched = t
                if t - last_cleanup > 3600:
                    cleanup_stale_temp(self.cfg.tmp_dir)
                    last_cleanup = t
                if t - last_refresh > 600:
                    self.refresh_publications()
                    last_refresh = t
                if t - last_retention > 3600:
                    with session_scope() as db:
                        retention.sweep(
                            db,
                            self.cfg,
                            source_media_hours=load_settings(db).source_media_retention_hours,
                        )
                    last_retention = t
            except Exception:
                log.exception("maintenance loop error")
            self.shutdown.wait(10)

    # --------------------------------------------------------- lanes
    def _may_claim(self) -> bool:
        with session_scope() as db:
            ctl = agent.get_control(db)
            return ctl.desired_state == "running" and not ctl.maintenance_mode

    def _take_slot(self) -> bool:
        """Wait for one of the shared job slots. Returns False once shutdown has been requested."""
        assert self._slots is not None
        while not self.shutdown.is_set():
            if self._slots.acquire(timeout=1):
                return True
        return False

    def lane_loop(self, lane: str) -> None:
        idle = 1.0
        capped = lane in CAPPED_LANES
        while not self.shutdown.is_set():
            if capped and not self._take_slot():
                break
            try:
                job = self.run_once(lane)
            except Exception:
                log.exception("lane %s crashed; continuing", lane)
                job = None
            finally:
                if capped and self._slots is not None:
                    self._slots.release()
            if job:
                idle = 0.2
            else:
                idle = min(5.0, idle * 1.5)
            self.shutdown.wait(idle)

    def run_once(self, lane: str) -> str | None:
        if not self._may_claim():
            return None
        with self._lock:
            exclude = set(DISK_HEAVY_TYPES) if "storage" in self.degraded else set()
        n = self._claims.get(lane, 0) + 1
        self._claims[lane] = n
        db = new_session()
        try:
            job = q.claim(
                db,
                lane=lane,
                worker_id=self.worker_id,
                lease_seconds=self.cfg.worker_lease_seconds,
                fairness=(n % FAIRNESS_EVERY == 0),
                exclude_types=exclude,
            )
            if job is None:
                return None
            with self._lock:
                self.current[lane] = job.id
            self.execute(db, job)
            return job.id
        finally:
            with self._lock:
                self.current.pop(lane, None)
            db.close()

    def execute(self, db: Any, job: Job) -> None:
        settings = load_settings(db)
        # Progress/lease state is kept in memory and flushed by the lease thread. Writing from the
        # handler thread could deadlock against the handler's own open SQLite write transaction.
        state: dict[str, Any] = {"progress": 0.0, "stage": None, "cancel": False}
        with self._lock:
            self.progress_state[job.id] = state

        def progress(p: float, stage: str | None) -> None:
            state["progress"] = p
            if stage:
                state["stage"] = stage

        def cancelled() -> bool:
            return self.shutdown.is_set() or bool(state["cancel"])

        ctx = pipeline.JobContext(
            db=db,
            job=job,
            cfg=self.cfg,
            settings=settings,
            progress=progress,
            cancelled=cancelled,
            discovery_factory=self.discovery_factory,
            publish_deps=self.publish_deps,
        )
        handler = pipeline.HANDLERS.get(job.job_type)
        log.info(
            "job %s %s started (attempt %s)",
            job.id,
            job.job_type,
            job.attempt_count,
            extra={"job_id": job.id},
        )
        try:
            if handler is None:
                raise JobError(ErrorCode.INTERNAL, f"Ingen hanterare för {job.job_type}.", transient=False)
            out = handler(ctx)
            if ctx.notes:
                out = {**out, "notes": ctx.notes}
            q.complete(db, job, out)
            db.commit()
            log.info("job %s succeeded", job.id, extra={"job_id": job.id})
        except Cancelled:
            db.rollback()
            job = db.get(Job, job.id, populate_existing=True)
            if self.shutdown.is_set() and not job.cancel_requested:
                q.defer(db, job, until=utcnow(), reason="Avbrutet vid avstängning; återupptas.")
            else:
                q.mark_cancelled(db, job)
            db.commit()
        except pipeline.Defer as d:
            db.rollback()
            job = db.get(Job, job.id, populate_existing=True)
            q.defer(db, job, until=d.until, reason=d.reason)
            db.commit()
        except Exception as e:
            db.rollback()
            if not isinstance(e, JobError):
                log.exception("job %s raised", job.id, extra={"job_id": job.id})
            else:
                log.warning("job %s: %s (%s)", job.id, e.safe_message, e.code, extra={"job_id": job.id})
            job = db.get(Job, job.id, populate_existing=True)
            q.fail(db, job, e)
            if job.status in (JobStatus.FAILED, JobStatus.BLOCKED, JobStatus.AWAITING_MEDIA):
                pipeline.on_job_failed(db, job)
            db.commit()
        finally:
            with self._lock:
                self.progress_state.pop(job.id, None)
            try:
                workspace.release(self.cfg, job)
            except Exception:
                log.exception("could not clean temp directory for job %s", job.id)

    def lease_loop(self) -> None:
        """Extend leases, persist progress and pick up cancel requests for running jobs."""
        while not self.shutdown.is_set():
            with self._lock:
                items = list(self.progress_state.items())
            for job_id, st in items:
                try:
                    with session_scope() as db:
                        st["cancel"] = q.heartbeat(
                            db,
                            job_id,
                            self.worker_id,
                            lease_seconds=self.cfg.worker_lease_seconds,
                            progress=float(st["progress"] or 0),
                            stage=st["stage"],
                        )  # type: ignore[arg-type]
                except Exception:
                    log.exception("lease update failed for %s", job_id)
            self.shutdown.wait(2)

    # --------------------------------------------------------- lifecycle
    def run(self) -> None:
        with session_scope() as db:
            recovered = q.recover_expired_leases(db)
            # Our own previous incarnation's jobs (same host/pid reuse is rare) are recovered via lease.
            agent.get_control(db)
        cleanup_stale_temp(self.cfg.tmp_dir, older_than_seconds=3600)
        with session_scope() as db:
            workspace.cleanup_orphans(db, self.cfg)
        log.info("worker %s starting; recovered %s stale jobs", self.worker_id, recovered)
        # Downloads moved to their own lane. Move waiting downloads queued before that change too.
        with session_scope() as db:
            db.execute(
                update(Job)
                .where(Job.job_type == JobType.ACQUIRE, Job.lane != "download")
                .values(lane="download")
            )
        self.check_health()
        self.heartbeat()
        threads = [
            threading.Thread(target=self.maintenance_loop, name="maintenance", daemon=True),
            threading.Thread(target=self.lease_loop, name="lease", daemon=True),
        ]
        s = self._settings()
        self._slots = threading.BoundedSemaphore(s.max_concurrent_jobs)
        for lane in self.lanes:
            count = {
                "heavy": s.heavy_concurrency,
                "download": s.download_concurrency,
            }.get(lane, 1)
            for i in range(count):
                threads.append(
                    threading.Thread(target=self.lane_loop, args=(lane,), name=f"{lane}-{i}", daemon=True)
                )
        for t in threads:
            t.start()
        self.shutdown.wait()
        log.info("shutdown requested; waiting for jobs to reach a checkpoint")
        for t in threads:
            t.join(timeout=60)
        with session_scope() as db:
            hb = db.get(WorkerHeartbeat, self.worker_id)
            if hb:
                hb.stopped_at = utcnow()
        log.info("worker stopped")


def main() -> None:
    cfg = get_config()
    cfg.ensure_dirs()
    setup_logging(cfg.logs_dir, "worker")
    upgrade_to_head(cfg.database_url)
    init_engine(cfg.database_url)
    w = Worker(cfg)

    def _stop(signum: int, _frame: object) -> None:
        log.info("signal %s received", signum)
        w.shutdown.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    w.run()


if __name__ == "__main__":
    main()
