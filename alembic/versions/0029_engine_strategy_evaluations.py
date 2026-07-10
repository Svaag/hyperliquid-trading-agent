"""Add append-only engine strategy activation evaluations.

Revision ID: 0029_engine_strategy_evaluations
Revises: 0028_wave_supervisor_runs
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0029_engine_strategy_evaluations"
down_revision = "0028_wave_supervisor_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "engine_strategy_evaluations",
        sa.Column("evaluation_id", sa.String(length=128), primary_key=True),
        sa.Column("engine_run_id", sa.String(length=96), nullable=False),
        sa.Column("evaluated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("asset", sa.String(length=64), nullable=False),
        sa.Column("venue", sa.String(length=64), nullable=False, server_default="hyperliquid"),
        sa.Column("strategy_id", sa.String(length=96), nullable=False),
        sa.Column("strategy_version", sa.String(length=64), nullable=False, server_default="unknown"),
        sa.Column("strategy_family", sa.String(length=96), nullable=False, server_default="unknown"),
        sa.Column("catalog_mode", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column("activation_scope", sa.String(length=32), nullable=False, server_default="paper_shadow"),
        sa.Column("paper_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("counts_for_breadth", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("selection_status", sa.String(length=32), nullable=False),
        sa.Column("selection_reason", sa.String(length=96)),
        sa.Column("regime_snapshot_id", sa.String(length=96)),
        sa.Column("regime_label", sa.String(length=255), nullable=False, server_default="unknown"),
        sa.Column("news_risk_tier", sa.String(length=32), nullable=False, server_default="no_event"),
        sa.Column("required_feature_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("present_feature_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fresh_feature_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("feature_coverage_pct", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("fresh_feature_coverage_pct", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("missing_features_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("stale_features_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("feature_ages_ms_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("generation_attempted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("generation_outcome", sa.String(length=32), nullable=False),
        sa.Column("trigger_fired", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("candidate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("candidate_ids_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("reason_codes_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("diagnostics_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_engine_strategy_evaluations_run", "engine_strategy_evaluations", ["engine_run_id"])
    op.create_index(
        "ix_engine_strategy_evaluations_strategy_time",
        "engine_strategy_evaluations",
        ["strategy_id", "evaluated_at_ms"],
    )
    op.create_index(
        "ix_engine_strategy_evaluations_asset_strategy_time",
        "engine_strategy_evaluations",
        ["asset", "strategy_id", "evaluated_at_ms"],
    )
    op.create_index(
        "ix_engine_strategy_evaluations_outcome_time",
        "engine_strategy_evaluations",
        ["generation_outcome", "evaluated_at_ms"],
    )


def downgrade() -> None:
    op.drop_index("ix_engine_strategy_evaluations_outcome_time", table_name="engine_strategy_evaluations")
    op.drop_index("ix_engine_strategy_evaluations_asset_strategy_time", table_name="engine_strategy_evaluations")
    op.drop_index("ix_engine_strategy_evaluations_strategy_time", table_name="engine_strategy_evaluations")
    op.drop_index("ix_engine_strategy_evaluations_run", table_name="engine_strategy_evaluations")
    op.drop_table("engine_strategy_evaluations")
