"""Durable operational notifications and engine operator proposals.

Revision ID: 0027_operator_outbox
Revises: 0026_newswire_v2
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0027_operator_outbox"
down_revision = "0026_newswire_v2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operational_notification_outbox",
        sa.Column("notification_id", sa.String(length=96), primary_key=True),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False, unique=True),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("source_type", sa.String(length=64)),
        sa.Column("source_id", sa.String(length=128)),
        sa.Column("destination", sa.String(length=32), nullable=False, server_default="discord"),
        sa.Column("channel_id", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False, server_default="info"),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("scheduled_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("next_attempt_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("lease_expires_at_ms", sa.BigInteger()),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("discord_message_id", sa.String(length=64)),
        sa.Column("sent_at_ms", sa.BigInteger()),
        sa.Column("last_error", sa.Text()),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_operational_notifications_due",
        "operational_notification_outbox",
        ["destination", "status", "next_attempt_at_ms"],
    )
    op.create_index(
        "ix_operational_notifications_category_created",
        "operational_notification_outbox",
        ["category", "scheduled_at_ms"],
    )
    op.create_index(
        "ix_operational_notifications_source",
        "operational_notification_outbox",
        ["source_type", "source_id"],
    )

    op.create_table(
        "engine_operator_proposals",
        sa.Column("proposal_id", sa.String(length=96), primary_key=True),
        sa.Column("candidate_id", sa.String(length=96), nullable=False),
        sa.Column("packet_id", sa.String(length=128)),
        sa.Column("council_review_id", sa.String(length=128)),
        sa.Column("strategy_id", sa.String(length=96), nullable=False),
        sa.Column("asset", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="proposed"),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("net_ev_bps", sa.Float(), nullable=False),
        sa.Column("risk_adjusted_utility", sa.Float(), nullable=False),
        sa.Column("feature_coverage_pct", sa.Float(), nullable=False),
        sa.Column("allocated_notional_usd", sa.Float(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("expires_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("acknowledged_by", sa.String(length=128)),
        sa.Column("acknowledged_at_ms", sa.BigInteger()),
        sa.Column("rejected_by", sa.String(length=128)),
        sa.Column("rejected_at_ms", sa.BigInteger()),
        sa.Column("rejection_reason", sa.Text()),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("candidate_id", name="uq_engine_operator_proposals_candidate"),
    )
    op.create_index(
        "ix_engine_operator_proposals_status_created",
        "engine_operator_proposals",
        ["status", "created_at_ms"],
    )
    op.create_index(
        "ix_engine_operator_proposals_asset_created",
        "engine_operator_proposals",
        ["asset", "created_at_ms"],
    )
    op.create_index(
        "ix_engine_operator_proposals_strategy_created",
        "engine_operator_proposals",
        ["strategy_id", "created_at_ms"],
    )


def downgrade() -> None:
    op.drop_index("ix_engine_operator_proposals_strategy_created", table_name="engine_operator_proposals")
    op.drop_index("ix_engine_operator_proposals_asset_created", table_name="engine_operator_proposals")
    op.drop_index("ix_engine_operator_proposals_status_created", table_name="engine_operator_proposals")
    op.drop_table("engine_operator_proposals")
    op.drop_index("ix_operational_notifications_source", table_name="operational_notification_outbox")
    op.drop_index("ix_operational_notifications_category_created", table_name="operational_notification_outbox")
    op.drop_index("ix_operational_notifications_due", table_name="operational_notification_outbox")
    op.drop_table("operational_notification_outbox")
