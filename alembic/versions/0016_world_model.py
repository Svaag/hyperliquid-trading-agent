"""Add market world-model tables.

Revision ID: 0016_world_model
Revises: 0015_hip4_outcomes
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0016_world_model"
down_revision = "0015_hip4_outcomes"
branch_labels = None
depends_on = None


def _json_default(value: str) -> sa.TextClause:
    return sa.text(f"'{value}'")


def upgrade() -> None:
    op.create_table(
        "world_events",
        sa.Column("event_id", sa.String(length=128), primary_key=True),
        sa.Column("source_type", sa.String(length=64), nullable=False, server_default=sa.text("'unknown'")),
        sa.Column("source", sa.String(length=128), nullable=False, server_default=sa.text("'unknown'")),
        sa.Column("provider", sa.String(length=128), nullable=False, server_default=sa.text("'unknown'")),
        sa.Column("event_type", sa.String(length=128), nullable=False, server_default=sa.text("'unknown'")),
        sa.Column("asset_class", sa.String(length=64), nullable=False, server_default=sa.text("'unknown'")),
        sa.Column("symbols_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("topics_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("title", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("body", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("url", sa.Text()),
        sa.Column("event_ts_ms", sa.BigInteger()),
        sa.Column("received_ts_ms", sa.BigInteger(), nullable=False),
        sa.Column("computed_ts_ms", sa.BigInteger(), nullable=False),
        sa.Column("importance_score", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("sentiment", sa.String(length=32), nullable=False, server_default=sa.text("'unknown'")),
        sa.Column("confidence", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("source_score", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("quality_score", sa.Float(), nullable=False, server_default=sa.text("1")),
        sa.Column("staleness_ms", sa.BigInteger()),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_world_events_source_type", "world_events", ["source_type"])
    op.create_index("ix_world_events_received", "world_events", ["received_ts_ms"])

    op.create_table(
        "market_beliefs",
        sa.Column("belief_id", sa.String(length=128), primary_key=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("symbols_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("topics_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("direction", sa.String(length=32), nullable=False, server_default=sa.text("'unknown'")),
        sa.Column("probability", sa.Float()),
        sa.Column("confidence", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("salience", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("evidence_event_ids_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("contradicts_belief_ids_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'active'")),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("expires_at_ms", sa.BigInteger()),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_market_beliefs_kind_status", "market_beliefs", ["kind", "status"])
    op.create_index("ix_market_beliefs_subject", "market_beliefs", ["subject"])
    op.create_index("ix_market_beliefs_updated", "market_beliefs", ["updated_at_ms"])

    op.create_table(
        "narrative_clusters",
        sa.Column("cluster_id", sa.String(length=128), primary_key=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("symbols_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("topics_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("belief_ids_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("event_ids_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("pressure_score", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("consensus_score", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("conflict_score", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_narrative_clusters_updated", "narrative_clusters", ["updated_at_ms"])
    op.create_index("ix_narrative_clusters_pressure", "narrative_clusters", ["pressure_score"])

    op.create_table(
        "prediction_market_signals",
        sa.Column("signal_id", sa.String(length=128), primary_key=True),
        sa.Column("venue", sa.String(length=64), nullable=False),
        sa.Column("market_id", sa.String(length=128), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("outcome_id", sa.String(length=128)),
        sa.Column("outcome_name", sa.String(length=255), nullable=False, server_default=sa.text("''")),
        sa.Column("symbols_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("topics_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("implied_probability", sa.Float()),
        sa.Column("probability_delta", sa.Float()),
        sa.Column("best_bid", sa.Float()),
        sa.Column("best_ask", sa.Float()),
        sa.Column("liquidity_usd", sa.Float()),
        sa.Column("volume_usd", sa.Float()),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'unknown'")),
        sa.Column("source_event_ids_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("as_of_ms", sa.BigInteger(), nullable=False),
        sa.Column("staleness_ms", sa.BigInteger()),
        sa.Column("confidence", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_prediction_market_signals_venue_market", "prediction_market_signals", ["venue", "market_id"])
    op.create_index("ix_prediction_market_signals_as_of", "prediction_market_signals", ["as_of_ms"])
    op.create_index("ix_prediction_market_signals_status", "prediction_market_signals", ["status"])

    op.create_table(
        "source_credibility",
        sa.Column("source_key", sa.String(length=255), primary_key=True),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=False, server_default=sa.text("'unknown'")),
        sa.Column("score", sa.Float(), nullable=False, server_default=sa.text("0.5")),
        sa.Column("observations", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("confirmations", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("contradictions", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("notes_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "world_memory_atoms",
        sa.Column("memory_id", sa.String(length=128), primary_key=True),
        sa.Column("memory_type", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("symbols_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("topics_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("source_event_ids_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("source_belief_ids_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("confidence", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("salience", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("last_reinforced_at_ms", sa.BigInteger()),
        sa.Column("expires_at_ms", sa.BigInteger()),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_world_memory_atoms_type", "world_memory_atoms", ["memory_type"])
    op.create_index("ix_world_memory_atoms_subject", "world_memory_atoms", ["subject"])
    op.create_index("ix_world_memory_atoms_reinforced", "world_memory_atoms", ["last_reinforced_at_ms"])

    op.create_table(
        "world_model_snapshots",
        sa.Column("snapshot_id", sa.String(length=128), primary_key=True),
        sa.Column("as_of_ms", sa.BigInteger(), nullable=False),
        sa.Column("symbols_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("topics_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("summary", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("top_beliefs_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("narrative_clusters_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("prediction_market_signals_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("source_credibility_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("memory_atoms_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("quality_flags_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_world_model_snapshots_as_of", "world_model_snapshots", ["as_of_ms"])


def downgrade() -> None:
    op.drop_index("ix_world_model_snapshots_as_of", table_name="world_model_snapshots")
    op.drop_table("world_model_snapshots")
    op.drop_index("ix_world_memory_atoms_reinforced", table_name="world_memory_atoms")
    op.drop_index("ix_world_memory_atoms_subject", table_name="world_memory_atoms")
    op.drop_index("ix_world_memory_atoms_type", table_name="world_memory_atoms")
    op.drop_table("world_memory_atoms")
    op.drop_table("source_credibility")
    op.drop_index("ix_prediction_market_signals_status", table_name="prediction_market_signals")
    op.drop_index("ix_prediction_market_signals_as_of", table_name="prediction_market_signals")
    op.drop_index("ix_prediction_market_signals_venue_market", table_name="prediction_market_signals")
    op.drop_table("prediction_market_signals")
    op.drop_index("ix_narrative_clusters_pressure", table_name="narrative_clusters")
    op.drop_index("ix_narrative_clusters_updated", table_name="narrative_clusters")
    op.drop_table("narrative_clusters")
    op.drop_index("ix_market_beliefs_updated", table_name="market_beliefs")
    op.drop_index("ix_market_beliefs_subject", table_name="market_beliefs")
    op.drop_index("ix_market_beliefs_kind_status", table_name="market_beliefs")
    op.drop_table("market_beliefs")
    op.drop_index("ix_world_events_received", table_name="world_events")
    op.drop_index("ix_world_events_source_type", table_name="world_events")
    op.drop_table("world_events")
