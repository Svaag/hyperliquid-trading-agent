"""Add isolated world-model v2 stores.

Revision ID: 0033_world_model_v2
Revises: 0032_repair_newswire_reasons
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0033_world_model_v2"
down_revision = "0032_repair_newswire_reasons"
branch_labels = None
depends_on = None


def _base(name: str, key: str, length: int = 200) -> list[sa.Column]:
    return [
        sa.Column(key, sa.String(length=length), primary_key=True),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "world_model_v2_evidence",
        sa.Column("evidence_id", sa.String(160), primary_key=True),
        sa.Column("source_type", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(128), nullable=False),
        sa.Column("available_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("admission_status", sa.String(32), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_wm_v2_evidence_available", "world_model_v2_evidence", ["available_at_ms"])
    op.create_index("ix_wm_v2_evidence_admission", "world_model_v2_evidence", ["admission_status"])
    op.create_table(
        "world_model_v2_macro_observations",
        sa.Column("observation_id", sa.String(180), primary_key=True),
        sa.Column("series_id", sa.String(96), nullable=False),
        sa.Column("factor_id", sa.String(64), nullable=False),
        sa.Column("period", sa.String(64), nullable=False),
        sa.Column("vintage", sa.String(64), nullable=False),
        sa.Column("available_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("series_id", "period", "vintage", name="uq_wm_v2_macro_vintage"),
    )
    op.create_index("ix_wm_v2_macro_series_available", "world_model_v2_macro_observations", ["series_id", "available_at_ms"])
    op.create_table(
        "world_model_v2_macro_states",
        sa.Column("factor_id", sa.String(64), primary_key=True),
        sa.Column("as_of_ms", sa.BigInteger(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_wm_v2_macro_state_as_of", "world_model_v2_macro_states", ["as_of_ms"])
    op.create_table(
        "world_model_v2_prediction_markets",
        sa.Column("market_key", sa.String(200), primary_key=True),
        sa.Column("venue", sa.String(64), nullable=False),
        sa.Column("market_id", sa.String(160), nullable=False),
        sa.Column("admission_status", sa.String(32), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("venue", "market_id", name="uq_wm_v2_prediction_market"),
    )
    op.create_index("ix_wm_v2_prediction_admission", "world_model_v2_prediction_markets", ["admission_status"])
    op.create_table(
        "world_model_v2_prediction_quotes",
        sa.Column("quote_key", sa.String(240), primary_key=True),
        sa.Column("market_key", sa.String(200), nullable=False),
        sa.Column("outcome_id", sa.String(160), nullable=False),
        sa.Column("observed_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_wm_v2_prediction_quote_observed", "world_model_v2_prediction_quotes", ["observed_at_ms"])
    op.create_table(
        "world_model_v2_prediction_quote_history",
        sa.Column("history_id", sa.String(255), primary_key=True),
        sa.Column("quote_key", sa.String(240), nullable=False),
        sa.Column("observed_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_wm_v2_quote_history_key_time", "world_model_v2_prediction_quote_history", ["quote_key", "observed_at_ms"])
    op.create_table(
        "world_model_v2_prediction_quote_rollups",
        sa.Column("rollup_id", sa.String(255), primary_key=True),
        sa.Column("quote_key", sa.String(240), nullable=False),
        sa.Column("bucket_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("quote_key", "bucket_at_ms", name="uq_wm_v2_quote_rollup_hour"),
    )
    op.create_index("ix_wm_v2_quote_rollup_key_bucket", "world_model_v2_prediction_quote_rollups", ["quote_key", "bucket_at_ms"])
    op.create_table(
        "world_model_v2_hypotheses",
        sa.Column("hypothesis_id", sa.String(200), primary_key=True),
        sa.Column("market_key", sa.String(200), nullable=False),
        sa.Column("as_of_ms", sa.BigInteger(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_wm_v2_hypothesis_as_of", "world_model_v2_hypotheses", ["as_of_ms"])
    op.create_table(
        "world_model_v2_asset_impacts",
        sa.Column("impact_id", sa.String(240), primary_key=True),
        sa.Column("instrument_id", sa.String(96), nullable=False),
        sa.Column("factor_id", sa.String(64), nullable=False),
        sa.Column("as_of_ms", sa.BigInteger(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_wm_v2_impact_instrument_as_of", "world_model_v2_asset_impacts", ["instrument_id", "as_of_ms"])
    op.create_table(
        "world_model_v2_snapshots",
        sa.Column("snapshot_id", sa.String(160), primary_key=True),
        sa.Column("as_of_ms", sa.BigInteger(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_wm_v2_snapshot_as_of", "world_model_v2_snapshots", ["as_of_ms"])
    op.create_table(
        "world_model_v2_supervision",
        sa.Column("supervision_id", sa.String(160), primary_key=True),
        sa.Column("target_type", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(200), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_wm_v2_supervision_target", "world_model_v2_supervision", ["target_type", "target_id"])


def downgrade() -> None:
    for table, indexes in reversed([
        ("world_model_v2_evidence", ["ix_wm_v2_evidence_available", "ix_wm_v2_evidence_admission"]),
        ("world_model_v2_macro_observations", ["ix_wm_v2_macro_series_available"]),
        ("world_model_v2_macro_states", ["ix_wm_v2_macro_state_as_of"]),
        ("world_model_v2_prediction_markets", ["ix_wm_v2_prediction_admission"]),
        ("world_model_v2_prediction_quotes", ["ix_wm_v2_prediction_quote_observed"]),
        ("world_model_v2_prediction_quote_history", ["ix_wm_v2_quote_history_key_time"]),
        ("world_model_v2_prediction_quote_rollups", ["ix_wm_v2_quote_rollup_key_bucket"]),
        ("world_model_v2_hypotheses", ["ix_wm_v2_hypothesis_as_of"]),
        ("world_model_v2_asset_impacts", ["ix_wm_v2_impact_instrument_as_of"]),
        ("world_model_v2_snapshots", ["ix_wm_v2_snapshot_as_of"]),
        ("world_model_v2_supervision", ["ix_wm_v2_supervision_target"]),
    ]):
        for index in indexes:
            op.drop_index(index, table_name=table)
        op.drop_table(table)
