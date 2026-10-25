"""channel release spacing

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-25 21:00:00

Adds ``channels.release_spacing_minutes`` (nullable). Plain ``ADD COLUMN``, no batch rebuild, so no
dependent rows can be lost.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("channels", sa.Column("release_spacing_minutes", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.execute("ALTER TABLE channels DROP COLUMN release_spacing_minutes")
