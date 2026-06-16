"""Add engine event ledger and feature store.

Revision ID: 0011_engine_event_feature_store
Revises: 0010_replay_results
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0011_engine_event_feature_store"
down_revision = "0010_replay_results"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "normalized_events",
        sa.Column("event_id", sa.String(length=96), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("event_type", sa.String(length=96), nullable=False),
        sa.Column("asset_class", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column("symbols_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("source", sa.String(length=96), nullable=False),
        sa.Column("provider", sa.String(length=96), nullable=False),
        sa.Column("event_ts_ms", sa.BigInteger()),
        sa.Column("received_ts_ms", sa.BigInteger(), nullable=False),
        sa.Column("computed_ts_ms", sa.BigInteger(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("quality_score", sa.Float(), nullable=False, server_default="1"),
        sa.Column("staleness_ms", sa.BigInteger()),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_normalized_events_received_at_ms", "normalized_events", ["received_ts_ms"])
    op.create_index("ix_normalized_events_event_type_received", "normalized_events", ["event_type", "received_ts_ms"])
    op.create_index("ix_normalized_events_asset_class_received", "normalized_events", ["asset_class", "received_ts_ms"])

    op.create_table(
        "feature_values",
        sa.Column("feature_id", sa.String(length=96), primary_key=True),
        sa.Column("asset", sa.String(length=64), nullable=False),
        sa.Column("feature_group", sa.String(length=64), nullable=False),
        sa.Column("feature_name", sa.String(length=128), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("scalar_value", sa.Float()),
        sa.Column("event_ts_ms", sa.BigInteger()),
        sa.Column("received_ts_ms", sa.BigInteger(), nullable=False),
        sa.Column("computed_ts_ms", sa.BigInteger(), nullable=False),
        sa.Column("source_event_id", sa.String(length=96)),
        sa.Column("source", sa.String(length=96), nullable=False),
        sa.Column("version", sa.String(length=96), nullable=False),
        sa.Column("quality_score", sa.Float(), nullable=False, server_default="1"),
        sa.Column("staleness_ms", sa.BigInteger()),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_feature_values_asset_feature_computed", "feature_values", ["asset", "feature_name", "computed_ts_ms"])
    op.create_index("ix_feature_values_source_event", "feature_values", ["source_event_id"])
    op.create_index("ix_feature_values_group_computed", "feature_values", ["feature_group", "computed_ts_ms"])

    op.create_table(
        "feature_rollups",
        sa.Column("rollup_id", sa.String(length=96), primary_key=True),
        sa.Column("asset", sa.String(length=64), nullable=False),
        sa.Column("feature_group", sa.String(length=64), nullable=False),
        sa.Column("feature_name", sa.String(length=128), nullable=False),
        sa.Column("interval", sa.String(length=16), nullable=False),
        sa.Column("window_start_ms", sa.BigInteger(), nullable=False),
        sa.Column("window_end_ms", sa.BigInteger(), nullable=False),
        sa.Column("min_value", sa.Float()),
        sa.Column("max_value", sa.Float()),
        sa.Column("avg_value", sa.Float()),
        sa.Column("last_value", sa.Float()),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quality_avg", sa.Float()),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_feature_rollups_asset_feature_window", "feature_rollups", ["asset", "feature_name", "window_start_ms"])
    op.create_index("ix_feature_rollups_interval_window", "feature_rollups", ["interval", "window_start_ms"])

    op.create_table(
        "regime_snapshots",
        sa.Column("regime_snapshot_id", sa.String(length=96), primary_key=True),
        sa.Column("primary_asset", sa.String(length=64), nullable=False, server_default="GLOBAL"),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("as_of_ms", sa.BigInteger(), nullable=False),
        sa.Column("vector_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("permissions_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("feature_refs_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("quality_flags_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_regime_snapshots_created_at_ms", "regime_snapshots", ["created_at_ms"])
    op.create_index("ix_regime_snapshots_primary_asset_created", "regime_snapshots", ["primary_asset", "created_at_ms"])


def downgrade() -> None:
    op.drop_index("ix_regime_snapshots_primary_asset_created", table_name="regime_snapshots")
    op.drop_index("ix_regime_snapshots_created_at_ms", table_name="regime_snapshots")
    op.drop_table("regime_snapshots")
    op.drop_index("ix_feature_rollups_interval_window", table_name="feature_rollups")
    op.drop_index("ix_feature_rollups_asset_feature_window", table_name="feature_rollups")
    op.drop_table("feature_rollups")
    op.drop_index("ix_feature_values_group_computed", table_name="feature_values")
    op.drop_index("ix_feature_values_source_event", table_name="feature_values")
    op.drop_index("ix_feature_values_asset_feature_computed", table_name="feature_values")
    op.drop_table("feature_values")
    op.drop_index("ix_normalized_events_asset_class_received", table_name="normalized_events")
    op.drop_index("ix_normalized_events_event_type_received", table_name="normalized_events")
    op.drop_index("ix_normalized_events_received_at_ms", table_name="normalized_events")
    op.drop_table("normalized_events")
