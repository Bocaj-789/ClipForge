"""Multi-channel publishing: jobs, resumable uploads, retries, partial success, duplicates, quota, privacy.

All Google traffic goes to ``FakeGoogle`` through ``httpx.MockTransport``. These tests prove that
ClipForge speaks the documented protocol correctly and recovers from the failures it simulates;
they do not prove anything about Google's live behaviour (see docs/YOUTUBE_SETUP.md, live check).
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.base import utcnow
from app.db.models import Channel, ClipStatus, Job, JobStatus, JobType, Publication, RenderedClip
from app.db.session import session_scope
from app.services.publishing import quota, uploader
from app.services.queue import service as q
from app.services.rendering.ffmpeg import Cancelled
from app.services.settings_service import load_settings
from tests.fake_google import FakeGoogle, error_response
from tests.publishing_helpers import make_channel, make_clip, make_due, run_io, worker_for


# ---------------------------------------------------------------- helpers
class Env:
    def __init__(self, client, h, google, deps, cfg, channels: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
        self.client, self.h, self.google, self.deps, self.cfg = client, h, google, deps, cfg
        self.channels = channels  # name -> id
        self.worker = worker_for(cfg, deps)

    @property
    def ids(self) -> list[str]:
        return list(self.channels.values())

    def clip(self, **kw) -> str:  # type: ignore[no-untyped-def]
        return make_clip(self.cfg, **kw)

    def publish(self, rid: str, channel_ids: list[str] | None = None, **body):  # type: ignore[no-untyped-def]
        r = self.client.post(
            f"/api/v1/clips/{rid}/publish",
            json={"channel_ids": channel_ids or self.ids, **body},
            headers=self.h,
        )
        assert r.status_code == 200, r.text
        return r.json()

    def run(self) -> int:
        return run_io(self.worker)

    def pub(self, channel: str) -> dict:
        items = self.client.get("/api/v1/publications").json()["items"]
        return next(p for p in items if p["channel_id"] == self.channels[channel])

    def settings(self, **changes) -> None:  # type: ignore[no-untyped-def]
        r = self.client.patch("/api/v1/settings", json={"changes": changes, "confirm": "JA"}, headers=self.h)
        assert r.status_code == 200, r.text


@pytest.fixture
def env(pauthed, google: FakeGoogle, pdeps, pub_cfg) -> Env:  # type: ignore[no-untyped-def]
    client, h = pauthed
    channels = {
        "A": make_channel(pdeps, google, "Kanal A", "UC_A"),
        "B": make_channel(pdeps, google, "Kanal B", "UC_B"),
        "C": make_channel(pdeps, google, "Kanal C", "UC_C"),
    }
    return Env(client, h, google, pdeps, pub_cfg, channels)


def _jobs() -> list[Job]:
    with session_scope() as db:
        rows = list(db.scalars(select(Job).where(Job.job_type == JobType.PUBLISH).order_by(Job.created_at)))
        for r in rows:
            db.expunge(r)
        return rows


def _make_allowed_public(env: Env, name: str) -> None:
    with session_scope() as db:
        db.get(Channel, env.channels[name]).allow_public = True


# ---------------------------------------------------------------- jobs per destination
def test_one_publication_and_one_job_per_selected_channel(env: Env) -> None:
    rid = env.clip()
    res = env.publish(rid, [env.channels["A"], env.channels["B"]])
    assert res["created"] == 2 and {r["outcome"] for r in res["results"]} == {"created"}
    jobs = _jobs()
    assert len(jobs) == 2 and {j.channel_id for j in jobs} == {env.channels["A"], env.channels["B"]}
    assert all(j.job_type == JobType.PUBLISH and j.lane == "io" for j in jobs)
    assert len({j.idempotency_key for j in jobs}) == 2
    assert env.google.insert_calls == 0  # nothing leaves the machine until the worker runs the jobs


def test_selected_channel_list_is_deduplicated(env: Env) -> None:
    rid = env.clip()
    res = env.publish(rid, [env.channels["A"], env.channels["A"]])
    assert res["created"] == 1 and len(_jobs()) == 1


# ---------------------------------------------------------------- the real upload
def test_uploads_the_actual_rendered_file_with_all_metadata(env: Env) -> None:
    rid = env.clip(size=700_000, title="Räksmörgåsen <b>vinner</b>")
    env.publish(rid, [env.channels["A"]])
    assert env.run() == 1

    p = env.pub("A")
    assert p["status"] == "uploaded" and p["upload_succeeded"] is True
    vid = p["external_video_id"]
    assert p["external_url"] == f"https://www.youtube.com/watch?v={vid}"
    # Byte-for-byte the rendered MP4, sent in several resumable chunks.
    with session_scope() as db:
        assert env.google.video_sha[vid] == db.get(RenderedClip, rid).checksum
    (session,) = env.google.sessions.values()
    assert session.put_calls == 3 and session.size == 700_000
    # Metadata sent to videos.insert.
    snippet, status = env.google.videos[vid]["snippet"], env.google.videos[vid]["status"]
    assert snippet["title"].startswith("Räksmörgåsen") and "<" not in snippet["title"]
    assert snippet["categoryId"] == "22" and snippet["defaultLanguage"] == "sv"
    assert "shorts" in snippet["tags"] and "test" in snippet["tags"]
    assert "En beskrivning." in snippet["description"]
    assert status["privacyStatus"] == "private" and status["selfDeclaredMadeForKids"] is False
    assert "publishAt" not in status
    with session_scope() as db:
        assert db.get(RenderedClip, rid).clip.status == ClipStatus.PUBLISHED
        assert db.get(Channel, env.channels["A"]).last_upload_at is not None


def test_upload_success_is_not_the_same_as_being_public(env: Env) -> None:
    _make_allowed_public(env, "A")
    rid = env.clip()
    env.publish(rid, [env.channels["A"]], visibility="public")
    env.run()
    p = env.pub("A")
    # YouTube still processes the file: uploaded, but not public yet.
    assert p["status"] == "uploaded" and p["upload_succeeded"] and p["is_public"] is False
    assert p["availability"] == "processing" and p["visibility_requested"] == "public"
    env.google.processed = True
    r = env.client.post(f"/api/v1/publications/{p['id']}/refresh", headers=env.h)
    assert r.status_code == 200 and r.json()["availability"] == "public" and r.json()["is_public"] is True


def test_private_default_and_unlisted(env: Env) -> None:
    rid = env.clip()
    env.publish(rid, [env.channels["A"]])
    env.publish(rid, [env.channels["B"]], visibility="unlisted")
    env.run()
    env.google.processed = True
    for name, expected in (("A", "private"), ("B", "unlisted")):
        p = env.pub(name)
        r = env.client.post(f"/api/v1/publications/{p['id']}/refresh", headers=env.h)
        assert r.json()["availability"] == expected and r.json()["is_public"] is False
    assert {v["status"]["privacyStatus"] for v in env.google.videos.values()} == {"private", "unlisted"}


def test_public_is_refused_without_the_channel_opt_in(env: Env) -> None:
    rid = env.clip()
    res = env.publish(rid, [env.channels["A"]], visibility="public")
    assert res["created"] == 0 and res["results"][0]["outcome"] == "blocked"
    assert any("Offentlig publicering" in r for r in res["results"][0]["reasons"])
    assert _jobs() == [] and env.google.insert_calls == 0
    # A schedule is a future public release, so it needs the same opt-in.
    when = (utcnow() + timedelta(days=2)).isoformat()
    res = env.publish(rid, [env.channels["A"]], schedule_at=when)
    assert res["created"] == 0


def test_scheduled_release_sets_publish_at_on_a_private_upload(env: Env) -> None:
    _make_allowed_public(env, "A")
    rid = env.clip()
    when = utcnow() + timedelta(days=2)
    res = env.publish(rid, [env.channels["A"]], schedule_at=when.isoformat())
    assert res["created"] == 1
    env.run()
    status = next(iter(env.google.videos.values()))["status"]
    assert status["privacyStatus"] == "private" and status["publishAt"].endswith("Z")
    env.google.processed = True
    p = env.pub("A")
    r = env.client.post(f"/api/v1/publications/{p['id']}/refresh", headers=env.h)
    assert r.json()["availability"] == "scheduled" and r.json()["is_public"] is False


def test_schedule_in_the_past_or_for_unlisted_is_refused(env: Env) -> None:
    _make_allowed_public(env, "A")
    rid = env.clip()
    soon = (utcnow() + timedelta(minutes=2)).isoformat()
    res = env.publish(rid, [env.channels["A"]], schedule_at=soon)
    assert res["created"] == 0 and any("15 minuter" in r for r in res["results"][0]["reasons"])
    res = env.publish(
        rid,
        [env.channels["A"]],
        schedule_at=(utcnow() + timedelta(days=1)).isoformat(),
        visibility="unlisted",
    )
    assert res["created"] == 0


def test_youtube_forcing_private_is_reported(env: Env) -> None:
    _make_allowed_public(env, "A")
    env.google.next_privacy_override = "private"
    rid = env.clip()
    env.publish(rid, [env.channels["A"]], visibility="public")
    env.run()
    env.google.processed = True
    p = env.pub("A")
    r = env.client.post(f"/api/v1/publications/{p['id']}/refresh", headers=env.h).json()
    assert r["availability"] == "private" and r["notice"] and "privat" in r["notice"]


# ---------------------------------------------------------------- gates
@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"review": "needs_review"}, "inte godkänt"),
        ({"review": "rejected"}, "avvisat"),
        ({"rights": "unknown"}, "Rättigheterna"),
    ],
)
def test_manual_publish_gates(env: Env, kwargs: dict, needle: str) -> None:
    rid = env.clip(**kwargs)
    res = env.publish(rid, [env.channels["A"]])
    assert res["created"] == 0 and any(needle in r for r in res["results"][0]["reasons"])
    assert _jobs() == []


def test_missing_file_and_disconnected_channel_are_refused(env: Env) -> None:
    rid = env.clip()
    with session_scope() as db:
        (env.cfg.data_dir / db.get(RenderedClip, rid).file_path).unlink()
    assert env.publish(rid, [env.channels["A"]])["created"] == 0
    rid2 = env.clip()
    with session_scope() as db:
        db.get(Channel, env.channels["B"]).token_reference = None
    res = env.publish(rid2, [env.channels["B"]])
    assert res["created"] == 0 and any("inte ansluten" in r for r in res["results"][0]["reasons"])


def test_gates_are_checked_again_when_the_upload_starts(env: Env) -> None:
    rid = env.clip()
    env.publish(rid, [env.channels["A"], env.channels["B"]])
    with session_scope() as db:
        db.get(RenderedClip, rid).review_status = "rejected"  # rejected after queueing
    env.run()
    assert env.google.insert_calls == 0
    assert {env.pub(n)["status"] for n in ("A", "B")} == {"blocked"}


# ---------------------------------------------------------------- partial success and recovery
def test_one_channel_failing_does_not_block_the_others_and_is_recoverable(env: Env) -> None:
    env.google.fault_for("UC_B", "start", error_response(403, "forbidden", "Channel suspended"))
    rid = env.clip()
    env.publish(rid)
    env.run()
    assert env.pub("A")["status"] == "uploaded" and env.pub("C")["status"] == "uploaded"
    b = env.pub("B")
    assert b["status"] == "blocked" and "Channel suspended" in b["safe_error_message"]
    assert b["upload_succeeded"] is False and b["job"]["status"] == "blocked"
    assert len(env.google.uploads_for("UC_A")) == 1 and len(env.google.uploads_for("UC_C")) == 1
    inserts_before = env.google.insert_calls

    # Recover only the failed channel; the two that succeeded are not uploaded again.
    r = env.client.post(f"/api/v1/publications/{b['id']}/retry", json={}, headers=env.h)
    assert r.status_code == 200 and r.json()["status"] == "queued"
    env.run()
    assert env.pub("B")["status"] == "uploaded"
    assert env.google.insert_calls == inserts_before + 1
    for ch in ("UC_A", "UC_B", "UC_C"):
        assert len(env.google.uploads_for(ch)) == 1


def test_publishing_again_after_partial_failure_only_touches_the_failed_channel(env: Env) -> None:
    env.google.fault_for("UC_B", "start", error_response(400, "invalidTitle", "Bad title"))
    rid = env.clip()
    env.publish(rid)
    env.run()
    assert env.pub("B")["status"] == "failed"
    res = env.publish(rid)  # the user presses "publish" for the same three channels again
    outcomes = {r["channel_name"]: r["outcome"] for r in res["results"]}
    assert outcomes == {"Kanal A": "already_uploaded", "Kanal B": "retried", "Kanal C": "already_uploaded"}
    assert all(r["url"] for r in res["results"] if r["outcome"] == "already_uploaded")
    env.run()
    assert env.pub("B")["status"] == "uploaded"
    assert [len(env.google.uploads_for(c)) for c in ("UC_A", "UC_B", "UC_C")] == [1, 1, 1]


def test_invalid_metadata_fails_permanently_without_retries(env: Env) -> None:
    env.google.fault("start", error_response(400, "invalidTags", "Bad tags"))
    rid = env.clip()
    env.publish(rid, [env.channels["A"]])
    env.run()
    p = env.pub("A")
    assert p["status"] == "failed" and "invalidTags" in p["safe_error_message"]
    assert p["job"]["status"] == "failed" and p["job"]["attempt_count"] == 1


# ---------------------------------------------------------------- duplicates
def test_publishing_twice_before_the_upload_runs_creates_one_job(env: Env) -> None:
    rid = env.clip()
    env.publish(rid, [env.channels["A"]])
    again = env.publish(rid, [env.channels["A"]])
    assert again["created"] == 0 and again["results"][0]["outcome"] == "in_progress"
    assert len(_jobs()) == 1
    env.run()
    assert env.google.insert_calls == 1


def test_an_uploaded_clip_is_never_uploaded_again(env: Env) -> None:
    rid = env.clip()
    env.publish(rid, [env.channels["A"]])
    env.run()
    again = env.publish(rid, [env.channels["A"]])
    assert again["results"][0]["outcome"] == "already_uploaded"
    pid = env.pub("A")["id"]
    assert env.client.post(f"/api/v1/publications/{pid}/retry", json={}, headers=env.h).status_code == 409
    assert env.client.delete(f"/api/v1/publications/{pid}", headers=env.h).status_code == 409
    assert env.run() == 0 and env.google.insert_calls == 1


def test_database_forbids_two_publications_for_the_same_clip_and_channel(env: Env) -> None:
    rid = env.clip()
    with session_scope() as db:
        db.add(Publication(rendered_clip_id=rid, channel_id=env.channels["A"]))
    with pytest.raises(IntegrityError), session_scope() as db:
        db.add(Publication(rendered_clip_id=rid, channel_id=env.channels["A"]))


def test_rerunning_a_finished_job_does_not_upload_a_second_time(env: Env) -> None:
    """Simulates a crash after YouTube accepted the file but before the job was marked done."""
    rid = env.clip()
    env.publish(rid, [env.channels["A"]])
    env.run()
    (job,) = _jobs()
    r = env.client.post(
        f"/api/v1/jobs/{job.id}/requeue", headers=env.h
    )  # a second job for the same publication
    assert r.status_code == 200
    env.run()
    assert env.google.insert_calls == 1 and len(env.google.uploads_for("UC_A")) == 1
    assert env.pub("A")["status"] == "uploaded"


def test_deleting_a_failed_record_frees_the_pair(env: Env) -> None:
    env.google.fault("start", error_response(400, "invalidTitle"))
    rid = env.clip()
    env.publish(rid, [env.channels["A"]])
    env.run()
    pid = env.pub("A")["id"]
    assert env.client.delete(f"/api/v1/publications/{pid}", headers=env.h).status_code == 200
    assert env.publish(rid, [env.channels["A"]])["results"][0]["outcome"] == "created"


# ---------------------------------------------------------------- retries and backoff
def test_transient_errors_retry_with_growing_backoff_then_succeed(env: Env) -> None:
    env.google.fault("start", error_response(503, "backendError"), error_response(500, "internalError"))
    rid = env.clip()
    env.publish(rid, [env.channels["A"]])
    delays = []
    for attempt in (1, 2):
        env.run()
        job = _jobs()[0]
        assert job.status == JobStatus.RETRY_WAIT and job.attempt_count == attempt
        delays.append((job.next_attempt_at - utcnow()).total_seconds())
        p = env.pub("A")
        assert p["status"] == "queued" and p["safe_error_message"] and p["upload_succeeded"] is False
        make_due()
    assert 14 <= delays[0] <= 31 and 29 <= delays[1] <= 61  # exponential with jitter
    env.run()
    job = _jobs()[0]
    assert job.status == JobStatus.SUCCEEDED and job.attempt_count == 3
    assert env.pub("A")["status"] == "uploaded" and len(env.google.uploads_for("UC_A")) == 1


def test_retries_are_bounded_and_end_in_failed(env: Env) -> None:
    env.google.fault("start", *[error_response(503, "backendError") for _ in range(10)])
    rid = env.clip()
    env.publish(rid, [env.channels["A"]])
    for _ in range(8):
        env.run()
        make_due()
    job = _jobs()[0]
    assert job.status == JobStatus.FAILED and job.attempt_count == job.max_attempts == 5
    assert env.pub("A")["status"] == "failed"
    assert len(env.google.uploads_for("UC_A")) == 0


def test_a_chunk_failure_resumes_from_what_youtube_has(env: Env) -> None:
    env.google.fault("put", error_response(503, "backendError"))  # first chunk fails once
    rid = env.clip()
    env.publish(rid, [env.channels["A"]])
    env.run()
    assert env.pub("A")["status"] == "uploaded"
    assert env.google.insert_calls == 1  # same session, no new upload
    assert env.deps.sleeps and env.deps.sleeps[0] >= 1  # backed off before resuming
    with session_scope() as db:
        assert env.google.video_sha[env.pub("A")["external_video_id"]] == db.get(RenderedClip, rid).checksum


def test_a_network_timeout_mid_upload_resumes_the_same_session(env: Env) -> None:
    env.google.fault("put", httpx.ReadTimeout("slow"), httpx.ConnectError("down"))
    rid = env.clip()
    env.publish(rid, [env.channels["A"]])
    env.run()
    assert env.pub("A")["status"] == "uploaded" and env.google.insert_calls == 1


def _arm_lost_final_answer(env: Env) -> None:
    """On the 2nd PUT (last chunk of a 2-chunk file) Google applies the bytes but the answer is lost."""
    state = {"n": 0}
    orig = env.google._put  # noqa: SLF001

    def put(acc, request, query):  # type: ignore[no-untyped-def]
        state["n"] += 1
        if state["n"] == 2:
            env.google.fault("put_apply_then_fail", httpx.ReadTimeout("lost answer"))
        return orig(acc, request, query)

    env.google._put = put  # type: ignore[method-assign]  # noqa: SLF001


def test_an_unconfirmed_final_chunk_is_not_uploaded_twice(env: Env) -> None:
    """YouTube got the whole file but the answer was lost: ask the session instead of re-uploading."""
    _arm_lost_final_answer(env)
    rid = env.clip(size=500_000)  # two chunks of 256 KiB
    env.publish(rid, [env.channels["A"]])
    env.run()
    assert env.pub("A")["status"] == "uploaded"
    assert env.google.insert_calls == 1 and len(env.google.uploads_for("UC_A")) == 1
    assert env.deps.sleeps  # it backed off, then asked Google what it had


def _lose_final_answer(env: Env) -> str:
    env.deps.chunk_retries = 0  # surface the failure to the job instead of retrying in-process
    _arm_lost_final_answer(env)
    rid = env.clip(size=500_000)
    env.publish(rid, [env.channels["A"]])
    env.run()
    with session_scope() as db:
        pub = db.scalars(select(Publication)).one()
        assert pub.final_chunk_sent and pub.upload_session_uri and pub.external_video_id is None
    assert env.pub("A")["status"] == "queued" and _jobs()[0].status == JobStatus.RETRY_WAIT
    return rid


def test_job_retry_after_lost_final_answer_adopts_the_finished_session(env: Env) -> None:
    _lose_final_answer(env)
    make_due()
    env.run()
    assert env.pub("A")["status"] == "uploaded"
    assert env.google.insert_calls == 1 and len(env.google.uploads_for("UC_A")) == 1


def test_expired_session_with_finished_upload_is_found_in_recent_uploads(env: Env) -> None:
    _lose_final_answer(env)
    env.google.sessions.clear()  # session expired, but the video does exist on the channel
    make_due()
    env.run()
    assert env.pub("A")["status"] == "uploaded"
    assert env.google.insert_calls == 1 and len(env.google.uploads_for("UC_A")) == 1


def test_unknown_outcome_stops_instead_of_guessing_and_can_be_overridden(env: Env) -> None:
    _lose_final_answer(env)
    # Session gone AND no matching video on the channel: ClipForge cannot know what happened.
    env.google.sessions.clear()
    env.google.videos.clear()
    env.google.video_channel.clear()
    make_due()
    env.run()
    p = env.pub("A")
    assert p["status"] == "blocked" and p["error_code"] == "upload_outcome_unknown"
    assert env.google.insert_calls == 1  # no second upload was started
    # A plain retry is refused; only an explicit "start over" re-uploads.
    assert env.client.post(f"/api/v1/publications/{p['id']}/retry", json={}, headers=env.h).status_code == 409
    r = env.client.post(f"/api/v1/publications/{p['id']}/retry", json={"start_over": True}, headers=env.h)
    assert r.status_code == 200
    env.run()
    assert env.pub("A")["status"] == "uploaded" and env.google.insert_calls == 2


def test_cancel_between_chunks_keeps_the_session_and_resume_sends_only_the_rest(env: Env) -> None:
    rid = env.clip(size=700_000)  # three chunks
    env.publish(rid, [env.channels["A"]])
    calls = {"n": 0}

    def cancelled() -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # let the first chunk through, then cancel

    with session_scope() as db:
        job = db.scalars(select(Job)).one()
        with pytest.raises(Cancelled):
            uploader.run_publish(
                db, job, load_settings(db), env.deps, env.cfg, progress=lambda *_: None, cancelled=cancelled
            )
        pub = db.scalars(select(Publication)).one()
        assert pub.bytes_uploaded == 256 * 1024 and pub.upload_session_uri and pub.status == "uploading"
    with session_scope() as db:
        job = db.scalars(select(Job)).one()
        out = uploader.run_publish(
            db, job, load_settings(db), env.deps, env.cfg, progress=lambda *_: None, cancelled=lambda: False
        )
        assert out["video_id"]
    (session,) = env.google.sessions.values()
    assert env.google.insert_calls == 1 and session.put_calls == 3  # 1 + 2 remaining, not 3 + 3


def test_cancelling_a_queued_upload_and_retrying_it(env: Env) -> None:
    rid = env.clip()
    env.publish(rid, [env.channels["A"]])
    pid = env.pub("A")["id"]
    r = env.client.post(f"/api/v1/publications/{pid}/cancel", headers=env.h)
    assert r.json()["status"] == "cancelled" and r.json()["job"]["status"] == "cancelled"
    assert env.run() == 0 and env.google.insert_calls == 0
    assert env.client.post(f"/api/v1/publications/{pid}/retry", json={}, headers=env.h).status_code == 200
    env.run()
    assert env.pub("A")["status"] == "uploaded"


def test_generic_job_endpoints_keep_publications_in_sync(env: Env) -> None:
    rid = env.clip()
    env.publish(rid, [env.channels["A"]])
    (job,) = _jobs()
    env.client.post(f"/api/v1/jobs/{job.id}/cancel", headers=env.h)
    assert env.pub("A")["status"] == "cancelled"
    env.client.post(f"/api/v1/jobs/{job.id}/retry", headers=env.h)
    assert env.pub("A")["status"] == "queued"


def test_crashed_worker_resumes_the_persisted_session(env: Env) -> None:
    rid = env.clip(size=700_000)
    env.publish(rid, [env.channels["A"]])
    # The first attempt dies after one chunk, leaving the job 'running' with an expired lease.
    calls = {"n": 0}

    def cancelled() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    with session_scope() as db:
        job = db.scalars(select(Job)).one()
        job.status, job.worker_id, job.attempt_count = JobStatus.RUNNING, "dead-worker", 1
        job.lease_expires_at = utcnow() - timedelta(minutes=1)
        db.flush()
        with pytest.raises(Cancelled):
            uploader.run_publish(
                db, job, load_settings(db), env.deps, env.cfg, progress=lambda *_: None, cancelled=cancelled
            )
    from app.services.publishing import service as publishing

    with session_scope() as db:
        assert q.recover_expired_leases(db) == 1
        publishing.reconcile(db)
    assert env.pub("A")["status"] == "queued"
    make_due()
    env.run()
    assert env.pub("A")["status"] == "uploaded" and env.google.insert_calls == 1


# ---------------------------------------------------------------- quota, rate limits, auth
def test_google_quota_exceeded_defers_to_the_pacific_midnight_without_using_an_attempt(env: Env) -> None:
    env.google.fault_for("UC_A", "start", error_response(403, "quotaExceeded", "quota"))
    rid = env.clip()
    env.publish(rid, [env.channels["A"], env.channels["B"]])
    env.run()
    assert env.pub("B")["status"] == "uploaded"  # another channel is unaffected
    a = env.pub("A")
    assert a["status"] == "queued" and a["error_code"] == "api_quota_exceeded"
    job = next(j for j in _jobs() if j.channel_id == env.channels["A"])
    assert job.status == JobStatus.RETRY_WAIT and job.attempt_count == 0
    expected = quota.next_reset() + timedelta(minutes=5)
    assert abs((job.next_attempt_at - expected).total_seconds()) < 120
    # After the reset it goes through.
    make_due()
    env.run()
    assert env.pub("A")["status"] == "uploaded"


def test_local_insert_limit_stops_uploads_before_calling_google(env: Env) -> None:
    env.settings(youtube_daily_insert_limit=1)
    rid = env.clip()
    env.publish(rid)
    env.run()
    uploaded = [n for n in "ABC" if env.pub(n)["status"] == "uploaded"]
    assert len(uploaded) == 1 and env.google.insert_calls == 1
    waiting = [n for n in "ABC" if n not in uploaded]
    assert all(
        env.pub(n)["status"] == "queued" and env.pub(n)["error_code"] == "api_quota_exceeded" for n in waiting
    )
    status = env.client.get("/api/v1/publishing/status").json()["quota"]
    assert status["insert"] == {"used": 1, "limit": 1, "remaining": 0}


def test_quota_ledger_counts_calls(env: Env) -> None:
    rid = env.clip()
    env.publish(rid, [env.channels["A"]])
    env.run()
    with session_scope() as db:
        s = load_settings(db)
        assert quota.used(db, quota.INSERT) == 1
        assert quota.used(db, quota.UNITS) >= 1  # the post-upload status check
        assert quota.remaining(db, s, quota.INSERT) == 99


def test_pacific_reset_is_dst_safe() -> None:
    from datetime import UTC, datetime

    assert quota.next_reset(datetime(2026, 7, 1, 12, 0, tzinfo=UTC)) == datetime(2026, 7, 2, 7, 0, tzinfo=UTC)
    assert quota.next_reset(datetime(2026, 12, 1, 12, 0, tzinfo=UTC)) == datetime(
        2026, 12, 2, 8, 0, tzinfo=UTC
    )


def test_revoked_token_blocks_only_that_channel_and_asks_for_reconnect(env: Env) -> None:
    env.google.accounts["UC_B"].revoked = True
    with session_scope() as db:
        db.get(Channel, env.channels["B"]).auto_publish_enabled = True
    rid = env.clip()
    env.publish(rid)
    env.run()
    assert env.pub("A")["status"] == "uploaded" and env.pub("C")["status"] == "uploaded"
    b = env.pub("B")
    assert b["status"] == "blocked" and b["error_code"] == "api_auth_expired"
    ch = next(c for c in env.client.get("/api/v1/channels").json()["items"] if c["id"] == env.channels["B"])
    assert ch["connection_state"] == "needs_reauth" and ch["oauth_connected"] is False
    assert ch["auto_publish_enabled"] is False and ch["connection_error"]
    # And the UI cannot queue more uploads for it until it is reconnected.
    res = env.publish(env.clip(), [env.channels["B"]])
    assert res["created"] == 0 and any("anslutning" in r for r in res["results"][0]["reasons"])


def test_expired_access_token_is_refreshed_transparently(env: Env) -> None:
    rid = env.clip()
    env.publish(rid, [env.channels["A"]])
    env.google.access.clear()  # nothing cached is valid any more
    env.deps._tokens["x"] = ("stale", 0.0)  # noqa: SLF001 - irrelevant entry must not matter
    env.run()
    assert env.pub("A")["status"] == "uploaded"


def test_rate_limit_is_retried_honouring_retry_after(env: Env) -> None:
    env.google.fault("start", error_response(429, "rateLimitExceeded", "slow down", **{"Retry-After": "600"}))
    rid = env.clip()
    env.publish(rid, [env.channels["A"]])
    env.run()
    job = _jobs()[0]
    assert job.status == JobStatus.RETRY_WAIT
    assert (job.next_attempt_at - utcnow()).total_seconds() >= 590


def test_upload_limit_of_the_channel_waits_hours_not_seconds(env: Env) -> None:
    env.google.fault("start", error_response(400, "uploadLimitExceeded", "limit"))
    rid = env.clip()
    env.publish(rid, [env.channels["A"]])
    env.run()
    job = _jobs()[0]
    assert job.status == JobStatus.RETRY_WAIT
    assert (job.next_attempt_at - utcnow()).total_seconds() > 5 * 3600


# ---------------------------------------------------------------- daily limits (automatic only)
def test_manual_publishing_is_not_throttled_by_daily_caps_but_automatic_is(env: Env) -> None:
    from app.services.publishing import service as publishing

    with session_scope() as db:
        ch = db.get(Channel, env.channels["A"])
        ch.max_uploads_per_day = 1
        ch.min_spacing_minutes = 240
        ch.auto_publish_enabled = True
        s = load_settings(db)
        s.auto_publish_global_enabled = True
    # Two manual uploads to the same channel on the same day are the user's explicit choice.
    r1, r2 = env.clip(), env.clip()
    assert env.publish(r1, [env.channels["A"]])["created"] == 1
    assert env.publish(r2, [env.channels["A"]])["created"] == 1
    env.run()
    # Automation, however, now sees the channel's daily cap exhausted.
    r3 = env.clip()
    with session_scope() as db:
        s = load_settings(db)
        s.auto_publish_global_enabled = True
        from app.services.settings_service import save_settings

        save_settings(db, s)
    with session_scope() as db:
        out = publishing.auto_publish_after_render(db, env.cfg, load_settings(db), db.get(RenderedClip, r3))
    assert [o.outcome for o in out] == ["blocked"]
    assert any("dagliga" in r for r in out[0].reasons)


# ---------------------------------------------------------------- automatic publishing
def _enable_auto(env: Env, names: str = "AB") -> None:
    from app.services.settings_service import save_settings

    with session_scope() as db:
        s = load_settings(db)
        s.auto_publish_global_enabled = True
        save_settings(db, s)
        for n in names:
            db.get(Channel, env.channels[n]).auto_publish_enabled = True


def _auto(env: Env, rid: str):  # type: ignore[no-untyped-def]
    from app.services.publishing import service as publishing

    with session_scope() as db:
        return publishing.auto_publish_after_render(db, env.cfg, load_settings(db), db.get(RenderedClip, rid))


def test_automatic_publishing_is_off_by_default(env: Env) -> None:
    with session_scope() as db:
        db.get(Channel, env.channels["A"]).auto_publish_enabled = True  # channel on, global switch off
    assert _auto(env, env.clip()) == [] and _jobs() == []


def test_automatic_publishing_follows_the_channel_selection_and_stays_private(env: Env) -> None:
    _enable_auto(env, "AB")
    out = _auto(env, env.clip(review="needs_review"))  # auto does not wait for manual approval
    assert sorted(o.channel_name for o in out) == ["Kanal A", "Kanal B"]  # channel C did not opt in
    assert {o.outcome for o in out} == {"created"}
    env.run()
    assert len(env.google.videos) == 2
    assert {v["status"]["privacyStatus"] for v in env.google.videos.values()} == {"private"}
    assert env.pub("A")["automatic"] is True and env.pub("A")["status"] == "uploaded"


def test_automatic_publishing_respects_score_warnings_rights_and_daily_cap(env: Env) -> None:
    _enable_auto(env, "A")
    low = _auto(env, env.clip(review="needs_review", score=20.0))
    assert low[0].outcome == "blocked" and any("poäng" in r for r in low[0].reasons)
    # Render warnings do not stop a private upload...
    with session_scope() as db:
        db.get(Channel, env.channels["A"]).max_uploads_per_day = 99
    warned_private = _auto(env, env.clip(review="needs_review", warnings=["låg upplösning"]))
    assert warned_private[0].outcome == "created"
    # ...but they do stop one that would become public.
    with session_scope() as db:
        ch = db.get(Channel, env.channels["A"])
        ch.allow_public, ch.default_visibility = True, "public"
        ch.schedule_json = {"windows": [["00:00", "23:59"]]}
    warned_public = _auto(env, env.clip(review="needs_review", warnings=["låg upplösning"]))
    assert warned_public[0].outcome == "blocked" and any("varningar" in r for r in warned_public[0].reasons)
    with session_scope() as db:
        ch = db.get(Channel, env.channels["A"])
        ch.allow_public, ch.default_visibility = False, "private"
    rights = _auto(env, env.clip(review="needs_review", rights="unknown"))
    assert rights[0].outcome == "blocked" and any("Rättigheterna" in r for r in rights[0].reasons)
    with session_scope() as db:
        db.get(Channel, env.channels["A"]).max_uploads_per_day = 2  # the warned clip above used one slot
    assert _auto(env, env.clip())[0].outcome == "created"
    capped = _auto(env, env.clip())
    assert capped[0].outcome == "blocked" and any("dagliga" in r for r in capped[0].reasons)
    assert len(_jobs()) == 2


def test_automatic_public_upload_is_scheduled_inside_the_channels_window(env: Env) -> None:
    _enable_auto(env, "A")
    tomorrow = (utcnow() + timedelta(days=1)).astimezone().weekday()
    with session_scope() as db:
        ch = db.get(Channel, env.channels["A"])
        ch.allow_public = True
        ch.default_visibility = "public"
        ch.schedule_json = {"days": [tomorrow], "windows": [["10:00", "11:00"]]}
        ch.timezone = "UTC"
    out = _auto(env, env.clip(review="needs_review"))
    assert out[0].outcome == "created"
    env.run()
    status = next(iter(env.google.videos.values()))["status"]
    # Never published instantly outside the window: uploaded privately with a release time.
    assert status["privacyStatus"] == "private" and "publishAt" in status


def test_automatic_public_requires_the_explicit_opt_in(env: Env) -> None:
    _enable_auto(env, "A")
    with session_scope() as db:
        ch = db.get(Channel, env.channels["A"])
        ch.default_visibility = "public"  # bypassing the API validation on purpose
        ch.allow_public = False
    out = _auto(env, env.clip(review="needs_review"))
    assert out[0].outcome == "blocked" and any("Offentlig publicering" in r for r in out[0].reasons)
    assert _jobs() == [] and env.google.insert_calls == 0


# ---------------------------------------------------------------- status refresh and history
def test_worker_refresh_updates_availability_and_detects_deleted_videos(env: Env) -> None:
    rid = env.clip()
    env.publish(rid, [env.channels["A"], env.channels["B"]])
    env.run()
    env.google.processed = True
    with session_scope() as db:
        for p in db.scalars(select(Publication)):
            p.last_checked_at = utcnow() - timedelta(hours=1)
    gone = env.pub("B")["external_video_id"]
    del env.google.videos[gone]
    assert env.worker.refresh_publications() == 2
    assert env.pub("A")["availability"] == "private"
    b = env.pub("B")
    assert b["availability"] == "missing" and b["upload_succeeded"] is True
    # A video that no longer exists can be forgotten, which frees the pair for a fresh upload.
    assert env.client.delete(f"/api/v1/publications/{b['id']}", headers=env.h).status_code == 200


def test_refresh_reports_google_errors(env: Env) -> None:
    rid = env.clip()
    env.publish(rid, [env.channels["A"]])
    env.run()
    env.google.fault("videos", error_response(500, "backendError", "down"))
    pid = env.pub("A")["id"]
    r = env.client.post(f"/api/v1/publications/{pid}/refresh", headers=env.h)
    assert r.status_code == 400 and "YouTube" in r.json()["error"]["message"]


def test_per_channel_history_lists_status_and_url(env: Env) -> None:
    env.google.fault_for("UC_B", "start", error_response(403, "forbidden", "Nope"))
    rid = env.clip()
    env.publish(rid, [env.channels["A"], env.channels["B"]])
    env.run()
    a = env.client.get(f"/api/v1/channels/{env.channels['A']}/publications").json()["items"]
    b = env.client.get(f"/api/v1/channels/{env.channels['B']}/publications").json()["items"]
    assert (
        a[0]["external_url"].startswith("https://www.youtube.com/watch?v=") and a[0]["status"] == "uploaded"
    )
    assert b[0]["external_url"] is None and b[0]["status"] == "blocked"
    clip_pubs = env.client.get(f"/api/v1/clips/{rid}/publications").json()["items"]
    assert {p["channel_name"] for p in clip_pubs} == {"Kanal A", "Kanal B"}
    filtered = env.client.get("/api/v1/publications", params={"status": "blocked"}).json()
    assert filtered["total"] == 1 and filtered["counts"]["uploaded"] == 1


def test_channel_with_upload_history_cannot_be_deleted(env: Env) -> None:
    rid = env.clip()
    env.publish(rid, [env.channels["A"]])
    env.run()
    env.client.post(f"/api/v1/channels/{env.channels['A']}/disconnect", headers=env.h)
    assert env.client.delete(f"/api/v1/channels/{env.channels['A']}", headers=env.h).status_code == 409


def test_publishing_status_endpoint(env: Env) -> None:
    body = env.client.get("/api/v1/publishing/status").json()
    assert body["client_configured"] is True
    assert body["redirect_uri"] == "http://127.0.0.1:8000/api/v1/oauth/youtube/callback"
    assert body["connected_channels"] == 3 and body["token_store"]["available"] is True
    assert body["quota"]["insert"]["limit"] == 100 and body["quota"]["units"]["limit"] == 10000
