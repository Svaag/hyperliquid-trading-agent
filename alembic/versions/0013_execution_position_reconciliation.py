"""Add paper/shadow execution, position thesis, reconciliation, and attribution.

Revision ID: 0013_exec_pos_recon
Revises: 0012_candidate_ev_alloc
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0013_exec_pos_recon"
down_revision = "0012_candidate_ev_alloc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "order_intents",
        sa.Column("intent_id", sa.String(length=96), primary_key=True),
        sa.Column("parent_candidate_id", sa.String(length=96), nullable=False),
        sa.Column("portfolio_decision_id", sa.String(length=96), nullable=False),
        sa.Column("asset", sa.String(length=64), nullable=False),
        sa.Column("asset_class", sa.String(length=32), nullable=False, server_default="crypto"),
        sa.Column("venue", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("order_type", sa.String(length=32), nullable=False),
        sa.Column("time_in_force", sa.String(length=32), nullable=False),
        sa.Column("target_size", sa.Float(), nullable=False),
        sa.Column("target_notional_usd", sa.Float(), nullable=False),
        sa.Column("max_slippage_bps", sa.Float(), nullable=False),
        sa.Column("price_limit", sa.Float()),
        sa.Column("reduce_only", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("post_only", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deadline_ts_ms", sa.BigInteger(), nullable=False),
        sa.Column("strategy_id", sa.String(length=96), nullable=False),
        sa.Column("model_version_id", sa.String(length=128), nullable=False),
        sa.Column("config_version_id", sa.String(length=128), nullable=False),
        sa.Column("risk_budget_id", sa.String(length=128), nullable=False),
        sa.Column("execution_mode", sa.String(length=32), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_order_intents_candidate", "order_intents", ["parent_candidate_id"])
    op.create_index("ix_order_intents_created", "order_intents", ["created_at_ms"])
    op.create_index("ix_order_intents_mode", "order_intents", ["execution_mode"])

    op.create_table(
        "execution_reports",
        sa.Column("report_id", sa.String(length=96), primary_key=True),
        sa.Column("intent_id", sa.String(length=96), nullable=False),
        sa.Column("execution_mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("requested_size", sa.Float(), nullable=False),
        sa.Column("filled_size", sa.Float(), nullable=False, server_default="0"),
        sa.Column("avg_fill_px", sa.Float()),
        sa.Column("fees_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("slippage_bps", sa.Float(), nullable=False, server_default="0"),
        sa.Column("market_impact_bps", sa.Float()),
        sa.Column("adapter", sa.String(length=32), nullable=False),
        sa.Column("assumptions_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_execution_reports_intent", "execution_reports", ["intent_id"])
    op.create_index("ix_execution_reports_created", "execution_reports", ["created_at_ms"])
    op.create_index("ix_execution_reports_mode_status", "execution_reports", ["execution_mode", "status"])

    op.create_table(
        "position_theses",
        sa.Column("position_id", sa.String(length=96), primary_key=True),
        sa.Column("entry_candidate_id", sa.String(length=96), nullable=False),
        sa.Column("strategy_id", sa.String(length=96), nullable=False),
        sa.Column("asset", sa.String(length=64), nullable=False),
        sa.Column("asset_class", sa.String(length=32), nullable=False, server_default="crypto"),
        sa.Column("venue", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("entry_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("expected_horizon", sa.String(length=32), nullable=False),
        sa.Column("stop", sa.Float(), nullable=False),
        sa.Column("targets_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("invalidation_rules_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("thesis_features_at_entry_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("current_thesis_score", sa.Float(), nullable=False, server_default="1"),
        sa.Column("degradation_reasons_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("position_state", sa.String(length=32), nullable=False, server_default="proposed"),
        sa.Column("execution_report_ids_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("opened_at_ms", sa.BigInteger()),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("closed_at_ms", sa.BigInteger()),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_position_theses_asset_state", "position_theses", ["asset", "position_state"])
    op.create_index("ix_position_theses_candidate", "position_theses", ["entry_candidate_id"])

    op.create_table(
        "reconciliation_runs",
        sa.Column("reconciliation_id", sa.String(length=96), primary_key=True),
        sa.Column("execution_mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expected_positions_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("observed_positions_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("mismatches_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("started_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("completed_at_ms", sa.BigInteger()),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_reconciliation_runs_started", "reconciliation_runs", ["started_at_ms"])

    op.create_table(
        "pnl_attribution_records",
        sa.Column("attribution_id", sa.String(length=96), primary_key=True),
        sa.Column("position_id", sa.String(length=96)),
        sa.Column("candidate_id", sa.String(length=96)),
        sa.Column("strategy_id", sa.String(length=96), nullable=False),
        sa.Column("asset", sa.String(length=64), nullable=False),
        sa.Column("window_start_ms", sa.BigInteger(), nullable=False),
        sa.Column("window_end_ms", sa.BigInteger(), nullable=False),
        sa.Column("alpha_pnl_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("timing_pnl_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("execution_pnl_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("fees_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("funding_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("residual_pnl_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("total_pnl_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("metrics_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_pnl_attribution_asset_window", "pnl_attribution_records", ["asset", "window_start_ms", "window_end_ms"])
    op.create_index("ix_pnl_attribution_strategy", "pnl_attribution_records", ["strategy_id"])

    op.create_table(
        "kill_switch_events",
        sa.Column("event_id", sa.String(length=96), primary_key=True),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("triggered_by", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("affected_assets_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("affected_strategies_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("block_new_orders", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("cancel_open_orders", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("freeze_config_changes", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("expires_at_ms", sa.BigInteger()),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_kill_switch_events_scope_action", "kill_switch_events", ["scope", "action"])
    op.create_index("ix_kill_switch_events_created", "kill_switch_events", ["created_at_ms"])


def downgrade() -> None:
    op.drop_index("ix_kill_switch_events_created", table_name="kill_switch_events")
    op.drop_index("ix_kill_switch_events_scope_action", table_name="kill_switch_events")
    op.drop_table("kill_switch_events")
    op.drop_index("ix_pnl_attribution_strategy", table_name="pnl_attribution_records")
    op.drop_index("ix_pnl_attribution_asset_window", table_name="pnl_attribution_records")
    op.drop_table("pnl_attribution_records")
    op.drop_index("ix_reconciliation_runs_started", table_name="reconciliation_runs")
    op.drop_table("reconciliation_runs")
    op.drop_index("ix_position_theses_candidate", table_name="position_theses")
    op.drop_index("ix_position_theses_asset_state", table_name="position_theses")
    op.drop_table("position_theses")
    op.drop_index("ix_execution_reports_mode_status", table_name="execution_reports")
    op.drop_index("ix_execution_reports_created", table_name="execution_reports")
    op.drop_index("ix_execution_reports_intent", table_name="execution_reports")
    op.drop_table("execution_reports")
    op.drop_index("ix_order_intents_mode", table_name="order_intents")
    op.drop_index("ix_order_intents_created", table_name="order_intents")
    op.drop_index("ix_order_intents_candidate", table_name="order_intents")
    op.drop_table("order_intents")
