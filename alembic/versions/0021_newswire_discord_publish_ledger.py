"""Add Newswire Discord publish ledger.

Revision ID: 0021_newswire_publish_ledger
Revises: 0020_candidate_outcome_spine
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0021_newswire_publish_ledger"
down_revision = "0020_candidate_outcome_spine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "newswire_publish_ledger",
        sa.Column("publish_id", sa.String(length=96), primary_key=True),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("destination", sa.String(length=32), nullable=False, server_default="discord"),
        sa.Column("channel_id", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("discord_message_id", sa.String(length=64)),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_attempt_ms", sa.BigInteger(), nullable=False),
        sa.Column("last_attempt_ms", sa.BigInteger(), nullable=False),
        sa.Column("posted_at_ms", sa.BigInteger()),
        sa.Column("last_error", sa.Text()),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("destination", "channel_id", "event_id", name="uq_newswire_publish_destination_channel_event"),
    )
    op.create_index("ix_newswire_publish_ledger_channel_status", "newswire_publish_ledger", ["channel_id", "status"])
    op.create_index("ix_newswire_publish_ledger_event", "newswire_publish_ledger", ["event_id"])


def downgrade() -> None:
    op.drop_index("ix_newswire_publish_ledger_event", table_name="newswire_publish_ledger")
    op.drop_index("ix_newswire_publish_ledger_channel_status", table_name="newswire_publish_ledger")
    op.drop_table("newswire_publish_ledger")
