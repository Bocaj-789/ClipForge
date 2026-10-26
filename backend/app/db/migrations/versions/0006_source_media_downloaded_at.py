"""source media download time

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-26 12:00:00

Adds ``candidate_videos.media_downloaded_at``, used to remove downloaded source videos after the
configured number of hours.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.base

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "candidate_videos", sa.Column("media_downloaded_at", app.db.base.UTCDateTime(), nullable=True)
    )


def downgrade() -> None:
    op.execute("ALTER TABLE candidate_videos DROP COLUMN media_downloaded_at")
