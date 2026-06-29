"""Add liquidation flow monitor tables.

Revision ID: 0018_liquidations
Revises: 0017_world_model_supervision
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0018_liquidations"
down_revision = "0017_world_model_supervision"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "liquidation_events",
        sa.Column("event_id", sa.String(length=200), primary_key=True),
        sa.Column("venue", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_integrity", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("venue_market_id", sa.String(length=64)),
        sa.Column("liquidated_side", sa.String(length=16), nullable=False, server_default="unknown"),
        sa.Column("raw_side", sa.String(length=32)),
        sa.Column("price", sa.Float()),
        sa.Column("avg_price", sa.Float()),
        sa.Column("mark_price", sa.Float()),
        sa.Column("bankruptcy_price", sa.Float()),
        sa.Column("size_base", sa.Float()),
        sa.Column("notional_usd", sa.Float()),
        sa.Column("timestamp_ms", sa.BigInteger(), nullable=False),
        sa.Column("received_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("block_height", sa.BigInteger()),
        sa.Column("tx_hash", sa.String(length=128)),
        sa.Column("log_index", sa.Integer()),
        sa.Column("trade_id", sa.String(length=128)),
        sa.Column("liquidation_id", sa.String(length=128)),
        sa.Column("liquidated_user", sa.String(length=128)),
        sa.Column("liquidator", sa.String(length=128)),
        sa.Column("method", sa.String(length=32)),
        sa.Column("confidence", sa.Float(), nullable=False, server_default=sa.text("1.0")),
        sa.Column("raw_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_liquidation_events_ts", "liquidation_events", ["timestamp_ms"])
    op.create_index("ix_liquidation_events_venue_symbol_ts", "liquidation_events", ["venue", "symbol", "timestamp_ms"])
    op.create_index("ix_liquidation_events_integrity_ts", "liquidation_events", ["source_integrity", "timestamp_ms"])

    op.create_table(
        "liquidation_adapter_state",
        sa.Column("adapter_name", sa.String(length=64), primary_key=True),
        sa.Column("last_cursor", sa.String(length=255)),
        sa.Column("last_event_ms", sa.BigInteger()),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="init"),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("liquidation_adapter_state")
    op.drop_index("ix_liquidation_events_integrity_ts", table_name="liquidation_events")
    op.drop_index("ix_liquidation_events_venue_symbol_ts", table_name="liquidation_events")
    op.drop_index("ix_liquidation_events_ts", table_name="liquidation_events")
    op.drop_table("liquidation_events")
