"""source config

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-25 20:00:00

Adds ``sources.config_json`` for kind-specific options (used by the Creative Commons finder). Plain
``ADD COLUMN`` (no batch rebuild) so no dependent rows can be lost.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("config_json", sa.JSON(), nullable=False, server_default="{}"))


def downgrade() -> None:
    op.execute("ALTER TABLE sources DROP COLUMN config_json")
