"""Programmatic Alembic helpers used at startup, by scripts and by tests."""

from __future__ import annotations

from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from app.config import REPO_ROOT
from app.db.session import make_engine


def _alembic_cfg(url: str) -> AlembicConfig:
    cfg = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    cfg.attributes["url"] = url
    cfg.attributes["configure_logger"] = False
    return cfg


def upgrade_to_head(url: str) -> None:
    command.upgrade(_alembic_cfg(url), "head")


def migration_status(url: str) -> dict[str, str | bool | None]:
    cfg = _alembic_cfg(url)
    head = ScriptDirectory.from_config(cfg).get_current_head()
    engine = make_engine(url)
    try:
        with engine.connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()
    return {"current": current, "head": head, "up_to_date": current == head}
