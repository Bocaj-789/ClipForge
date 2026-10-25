"""youtube publishing

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-25 16:00:31.451178

Adds OAuth state, a local quota ledger and the columns needed for resumable uploads, per-channel
privacy opt-ins and upload history. New NOT NULL columns carry server defaults so existing rows
(channels configured before publishing existed) migrate cleanly.

Columns are added with plain ``ALTER TABLE ... ADD COLUMN`` on purpose. Alembic's batch mode
would rebuild ``channels`` (DROP TABLE + copy) and, with foreign keys enforced, SQLite's implicit
DELETE would cascade into ``jobs``/``publications`` and silently destroy those rows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.base

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _channel_columns() -> list[sa.Column]:
    return [
        sa.Column("connection_state", sa.String(length=16), nullable=False, server_default="disconnected"),
        sa.Column("connection_error", sa.Text(), nullable=True),
        sa.Column("granted_scopes_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("youtube_title", sa.String(length=200), nullable=True),
        sa.Column("youtube_handle", sa.String(length=100), nullable=True),
        sa.Column("allow_public", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("category_id", sa.String(length=8), nullable=False, server_default="22"),
        sa.Column("made_for_kids", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("notify_subscribers", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("auto_publish_min_score", sa.Float(), nullable=False, server_default="60"),
    ]


def _publication_columns() -> list[sa.Column]:
    return [
        sa.Column("title", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("tags_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("category_id", sa.String(length=8), nullable=False, server_default="22"),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("made_for_kids", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("notify_subscribers", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("automatic", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("job_id", sa.String(length=32), nullable=True),
        sa.Column("file_size_bytes", sa.Integer(), nullable=True),
        sa.Column("bytes_uploaded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("session_created_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("final_chunk_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("availability", sa.String(length=16), nullable=False, server_default="unknown"),
        sa.Column("privacy_actual", sa.String(length=16), nullable=True),
        sa.Column("youtube_upload_status", sa.String(length=32), nullable=True),
        sa.Column("rejection_reason", sa.String(length=100), nullable=True),
        sa.Column("last_checked_at", app.db.base.UTCDateTime(), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "quota_usage",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("day", sa.String(length=10), nullable=False),
        sa.Column("bucket", sa.String(length=16), nullable=False),
        sa.Column("used", sa.Integer(), nullable=False),
        sa.Column("updated_at", app.db.base.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quota_usage")),
        sa.UniqueConstraint("day", "bucket", name="uq_quota_day_bucket"),
    )
    op.create_table(
        "oauth_states",
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("browser_hash", sa.String(length=64), nullable=False),
        sa.Column("code_verifier", sa.String(length=128), nullable=False),
        sa.Column("channel_id", sa.String(length=32), nullable=True),
        sa.Column("redirect_uri", sa.String(length=300), nullable=False),
        sa.Column("created_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("expires_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("consumed_at", app.db.base.UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["channels.id"],
            name=op.f("fk_oauth_states_channel_id_channels"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("state_hash", name=op.f("pk_oauth_states")),
    )
    for col in _channel_columns():
        op.add_column("channels", col)
    for col in _publication_columns():
        op.add_column("publications", col)


def downgrade() -> None:
    # Native DROP COLUMN (SQLite >= 3.35) for the same reason upgrade avoids batch mode.
    for col in reversed(_publication_columns()):
        op.execute(f"ALTER TABLE publications DROP COLUMN {col.name}")
    for col in reversed(_channel_columns()):
        op.execute(f"ALTER TABLE channels DROP COLUMN {col.name}")
    op.drop_table("oauth_states")
    op.drop_table("quota_usage")
