"""clip review decision time

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-25 22:00:00

Adds ``rendered_clips.review_decided_at``, used to remove declined clips two days later.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.base

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("rendered_clips", sa.Column("review_decided_at", app.db.base.UTCDateTime(), nullable=True))


def downgrade() -> None:
    op.execute("ALTER TABLE rendered_clips DROP COLUMN review_decided_at")
