from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0006_newswire"
down_revision = "0005_signal_evaluation_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "newswire_events",
        sa.Column("event_id", sa.String(length=64), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("transport", sa.String(length=16), nullable=False),
        sa.Column("received_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("published_at_ms", sa.BigInteger()),
        sa.Column("updated_at_ms", sa.BigInteger()),
        sa.Column("action", sa.String(length=16), nullable=False, server_default="created"),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("url", sa.Text()),
        sa.Column("author", sa.String(length=128)),
        sa.Column("symbols_json", sa.JSON(), nullable=False),
        sa.Column("asset_class", sa.String(length=16), nullable=False, server_default="unknown"),
        sa.Column("event_type", sa.String(length=32), nullable=False, server_default="headline"),
        sa.Column("urgency", sa.String(length=16), nullable=False, server_default="normal"),
        sa.Column("importance_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("sentiment", sa.String(length=16), nullable=False, server_default="unknown"),
        sa.Column("freshness", sa.String(length=16), nullable=False, server_default="fresh"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("source_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("tradability_json", sa.JSON(), nullable=False),
        sa.Column("enrichment_json", sa.JSON()),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_newswire_events_received_at_ms", "newswire_events", ["received_at_ms"])
    op.create_index("ix_newswire_events_source", "newswire_events", ["source"])
    op.create_index("ix_newswire_events_event_type", "newswire_events", ["event_type"])
    op.create_index("ix_newswire_events_asset_class", "newswire_events", ["asset_class"])


def downgrade() -> None:
    op.drop_index("ix_newswire_events_asset_class", table_name="newswire_events")
    op.drop_index("ix_newswire_events_event_type", table_name="newswire_events")
    op.drop_index("ix_newswire_events_source", table_name="newswire_events")
    op.drop_index("ix_newswire_events_received_at_ms", table_name="newswire_events")
    op.drop_table("newswire_events")
