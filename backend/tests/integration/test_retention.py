"""Removal of unneeded rendered clips: declined after two days, uploaded once YouTube confirms."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from app.config import Config
from app.db.base import utcnow
from app.db.models import RenderedClip
from app.db.session import session_scope
from app.services import retention
from tests.publishing_helpers import make_clip


def _set(rid: str, **values: object) -> None:
    with session_scope() as db:
        r = db.get(RenderedClip, rid)
        for k, v in values.items():
            setattr(r, k, v)


def _path(cfg: Config, rid: str):  # type: ignore[no-untyped-def]
    with session_scope() as db:
        return cfg.data_dir / db.get(RenderedClip, rid).file_path


def test_declined_clip_is_removed_two_days_after_it_was_declined(
    pclient: TestClient, pub_cfg: Config
) -> None:
    old = make_clip(pub_cfg, review="rejected")
    fresh = make_clip(pub_cfg, review="rejected")
    kept = make_clip(pub_cfg, review="approved")
    _set(old, review_decided_at=utcnow() - timedelta(days=3))
    _set(fresh, review_decided_at=utcnow() - timedelta(days=1))
    paths = {k: _path(pub_cfg, k) for k in (old, fresh, kept)}
    with session_scope() as db:
        counts = retention.sweep(db, pub_cfg)
    assert counts == {"declined": 1, "uploaded": 0}
    assert not paths[old].exists()
    assert paths[fresh].exists() and paths[kept].exists()
    with session_scope() as db:
        assert db.get(RenderedClip, old).deleted_at is not None
        assert db.get(RenderedClip, fresh).deleted_at is None


def test_declined_clip_without_a_date_gets_two_days_from_now(pclient: TestClient, pub_cfg: Config) -> None:
    rid = make_clip(pub_cfg, review="rejected")
    path = _path(pub_cfg, rid)
    with session_scope() as db:
        retention.sweep(db, pub_cfg)
    assert path.exists()  # declined before this rule existed: the countdown starts now
    with session_scope() as db:
        assert db.get(RenderedClip, rid).review_decided_at is not None
        assert retention.sweep(db, pub_cfg, now=utcnow() + timedelta(days=3))["declined"] == 1
    assert not path.exists()


def test_declined_clip_is_kept_while_an_upload_still_needs_it(pclient: TestClient, pub_cfg: Config) -> None:
    # Publications in flight keep the file; a clip is never deleted out from under an upload.
    from app.db.models import Channel, Publication

    rid = make_clip(pub_cfg, review="rejected")
    _set(rid, review_decided_at=utcnow() - timedelta(days=5))
    path = _path(pub_cfg, rid)
    with session_scope() as db:
        ch = Channel(display_name="Kö-kanal")
        db.add(ch)
        db.flush()
        db.add(Publication(rendered_clip_id=rid, channel_id=ch.id, status="queued"))
    with session_scope() as db:
        assert retention.sweep(db, pub_cfg)["declined"] == 0
    assert path.exists()
