from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command

from app.db.migrate import _alembic_cfg, upgrade_to_head  # noqa: PLC2701


def test_0002_migrates_existing_channels_and_publications_with_safe_defaults(tmp_path: Path) -> None:
    db_file = tmp_path / "old.sqlite3"
    url = f"sqlite:///{db_file}"
    command.upgrade(_alembic_cfg(url), "0001")
    con = sqlite3.connect(db_file)
    con.execute(
        "INSERT INTO channels (id, platform, display_name, enabled, paused, auto_publish_enabled,"
        " max_uploads_per_day, min_spacing_minutes, timezone, schedule_json, category_rules_json,"
        " title_template, description_template, default_language, default_visibility, render_profile,"
        " caption_style, min_clip_seconds, max_clip_seconds, late_policy, created_at, updated_at)"
        " VALUES ('c1','youtube','Gammal kanal',1,0,0,1,240,'Europe/Stockholm','{}','{}','{title}',"
        " '{description}','sv','private','shorts_standard','bold',20,60,'next_slot',"
        " '2026-01-01 00:00:00','2026-01-01 00:00:00')"
    )
    con.execute(
        "INSERT INTO publications (id, rendered_clip_id, channel_id, status, visibility, request_id,"
        " metadata_json, created_at, updated_at) VALUES ('p1','r1','c1','queued','private','req1','{}',"
        " '2026-01-01 00:00:00','2026-01-01 00:00:00')"
    )
    con.commit()
    con.close()

    upgrade_to_head(url)

    con = sqlite3.connect(db_file)
    ch = con.execute(
        "SELECT connection_state, allow_public, made_for_kids, notify_subscribers, category_id,"
        " auto_publish_min_score, granted_scopes_json, default_visibility FROM channels"
    ).fetchone()
    assert ch == ("disconnected", 0, 0, 0, "22", 60.0, "[]", "private")  # nothing becomes public by migration
    pub = con.execute(
        "SELECT availability, automatic, bytes_uploaded, final_chunk_sent, tags_json FROM publications"
    ).fetchone()
    assert pub == ("unknown", 0, 0, 0, "[]")
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"oauth_states", "quota_usage"} <= tables
    con.close()

    command.downgrade(_alembic_cfg(url), "0001")  # reversible
    con = sqlite3.connect(db_file)
    assert "oauth_states" not in {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    assert con.execute("SELECT display_name FROM channels").fetchone() == ("Gammal kanal",)
    con.close()
