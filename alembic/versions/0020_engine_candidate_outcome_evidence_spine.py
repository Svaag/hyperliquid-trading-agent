"""Add engine candidate evidence spine and outcome attribution.

Revision ID: 0020_engine_candidate_outcome_evidence_spine
Revises: 0019_engine_strategy_regime_council_learning
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0020_engine_candidate_outcome_evidence_spine"
down_revision = "0019_engine_strategy_regime_council_learning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("strategy_regime_performance", sa.Column("venue", sa.String(length=64), nullable=False, server_default="unknown"))
    op.add_column("strategy_regime_performance", sa.Column("outcome_window", sa.String(length=16), nullable=False, server_default="unknown"))
    op.add_column("strategy_regime_performance", sa.Column("risk_reject_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("strategy_regime_performance", sa.Column("council_veto_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("strategy_regime_performance", sa.Column("concentration_event_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("strategy_regime_performance", sa.Column("avg_net_return_bps", sa.Float(), nullable=False, server_default="0.0"))
    op.add_column("strategy_regime_performance", sa.Column("avg_realized_r", sa.Float(), nullable=False, server_default="0.0"))
    op.add_column("strategy_regime_performance", sa.Column("avg_drawdown_bps", sa.Float(), nullable=False, server_default="0.0"))
    op.add_column("strategy_regime_performance", sa.Column("avg_fees_bps", sa.Float(), nullable=False, server_default="0.0"))
    op.add_column("strategy_regime_performance", sa.Column("avg_slippage_bps", sa.Float(), nullable=False, server_default="0.0"))
    op.create_index(
        "ix_strategy_regime_performance_group",
        "strategy_regime_performance",
        ["strategy_id", "regime_label", "asset", "venue", "outcome_window"],
    )

    op.create_table(
        "candidate_evidence_links",
        sa.Column("link_id", sa.String(length=128), primary_key=True),
        sa.Column("candidate_id", sa.String(length=96), nullable=False),
        sa.Column("strategy_id", sa.String(length=96), nullable=False),
        sa.Column("strategy_version", sa.String(length=64), nullable=False, server_default="unknown"),
        sa.Column("strategy_family", sa.String(length=96), nullable=False, server_default="unknown"),
        sa.Column("asset", sa.String(length=64), nullable=False),
        sa.Column("venue", sa.String(length=64), nullable=False, server_default="hyperliquid"),
        sa.Column("horizon", sa.String(length=32), nullable=False),
        sa.Column("regime_snapshot_id", sa.String(length=96), nullable=False),
        sa.Column("feature_snapshot_id", sa.String(length=96), nullable=False),
        sa.Column("risk_decision_id", sa.String(length=96), nullable=True),
        sa.Column("council_review_id", sa.String(length=128), nullable=True),
        sa.Column("replay_context_id", sa.String(length=128), nullable=True),
        sa.Column("allocation_id", sa.String(length=96), nullable=True),
        sa.Column("packet_id", sa.String(length=128), nullable=True),
        sa.Column("outcome_window_ids_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_candidate_evidence_links_candidate", "candidate_evidence_links", ["candidate_id"])
    op.create_index("ix_candidate_evidence_links_strategy", "candidate_evidence_links", ["strategy_id"])
    op.create_index("ix_candidate_evidence_links_created", "candidate_evidence_links", ["created_at_ms"])

    op.create_table(
        "candidate_outcome_attributions",
        sa.Column("attribution_id", sa.String(length=128), primary_key=True),
        sa.Column("candidate_id", sa.String(length=96), nullable=False),
        sa.Column("strategy_id", sa.String(length=96), nullable=False),
        sa.Column("strategy_version", sa.String(length=64), nullable=False, server_default="unknown"),
        sa.Column("strategy_family", sa.String(length=96), nullable=False, server_default="unknown"),
        sa.Column("asset", sa.String(length=64), nullable=False),
        sa.Column("venue", sa.String(length=64), nullable=False, server_default="hyperliquid"),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("candidate_horizon", sa.String(length=32), nullable=False),
        sa.Column("regime_snapshot_id", sa.String(length=96), nullable=False),
        sa.Column("feature_snapshot_id", sa.String(length=96), nullable=False),
        sa.Column("risk_decision_id", sa.String(length=96), nullable=True),
        sa.Column("council_review_id", sa.String(length=128), nullable=True),
        sa.Column("replay_context_id", sa.String(length=128), nullable=True),
        sa.Column("allocation_id", sa.String(length=96), nullable=True),
        sa.Column("outcome_window", sa.String(length=16), nullable=False),
        sa.Column("window_start_ms", sa.BigInteger(), nullable=False),
        sa.Column("window_end_ms", sa.BigInteger(), nullable=False),
        sa.Column("entry_px", sa.Float(), nullable=False),
        sa.Column("mark_px", sa.Float(), nullable=True),
        sa.Column("gross_return_bps", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("fees_bps", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("slippage_bps", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("funding_bps", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("net_return_bps", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("realized_r", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("mfe_bps", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("mae_bps", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("risk_decision", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column("council_decision", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column("allocation_status", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column("terminal_state", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("quality_flags_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_candidate_outcome_attributions_candidate", "candidate_outcome_attributions", ["candidate_id"])
    op.create_index("ix_candidate_outcome_attributions_strategy", "candidate_outcome_attributions", ["strategy_id"])
    op.create_index("ix_candidate_outcome_attributions_window", "candidate_outcome_attributions", ["outcome_window", "window_end_ms"])
    op.create_index("ix_candidate_outcome_attributions_group", "candidate_outcome_attributions", ["strategy_id", "asset", "venue", "outcome_window"])
    op.create_index("ix_candidate_outcome_attributions_terminal", "candidate_outcome_attributions", ["terminal_state"])

    op.create_table(
        "replay_result_links",
        sa.Column("link_id", sa.String(length=128), primary_key=True),
        sa.Column("replay_id", sa.String(length=128), nullable=False),
        sa.Column("candidate_id", sa.String(length=96), nullable=True),
        sa.Column("strategy_id", sa.String(length=96), nullable=False, server_default="unknown"),
        sa.Column("strategy_version", sa.String(length=64), nullable=False, server_default="unknown"),
        sa.Column("strategy_family", sa.String(length=96), nullable=False, server_default="unknown"),
        sa.Column("asset", sa.String(length=64), nullable=False, server_default="GLOBAL"),
        sa.Column("venue", sa.String(length=64), nullable=False, server_default="unknown"),
        sa.Column("regime_snapshot_id", sa.String(length=96), nullable=True),
        sa.Column("horizon", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column("outcome_window", sa.String(length=16), nullable=False, server_default="unknown"),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_replay_result_links_replay", "replay_result_links", ["replay_id"])
    op.create_index("ix_replay_result_links_candidate", "replay_result_links", ["candidate_id"])
    op.create_index("ix_replay_result_links_strategy", "replay_result_links", ["strategy_id"])
    op.create_index("ix_replay_result_links_created", "replay_result_links", ["created_at_ms"])

    op.create_table(
        "portfolio_concentration_events",
        sa.Column("event_id", sa.String(length=128), primary_key=True),
        sa.Column("candidate_id", sa.String(length=96), nullable=False),
        sa.Column("allocation_id", sa.String(length=96), nullable=True),
        sa.Column("strategy_id", sa.String(length=96), nullable=False),
        sa.Column("strategy_version", sa.String(length=64), nullable=False, server_default="unknown"),
        sa.Column("strategy_family", sa.String(length=96), nullable=False, server_default="unknown"),
        sa.Column("asset", sa.String(length=64), nullable=False),
        sa.Column("venue", sa.String(length=64), nullable=False, server_default="hyperliquid"),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("reason_codes_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("strategy_share_pct", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("family_share_pct", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("symbol_strategy_share_pct", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_portfolio_concentration_events_candidate", "portfolio_concentration_events", ["candidate_id"])
    op.create_index("ix_portfolio_concentration_events_strategy", "portfolio_concentration_events", ["strategy_id"])
    op.create_index("ix_portfolio_concentration_events_created", "portfolio_concentration_events", ["created_at_ms"])
    op.create_index("ix_portfolio_concentration_events_decision", "portfolio_concentration_events", ["decision"])


def downgrade() -> None:
    op.drop_index("ix_portfolio_concentration_events_decision", table_name="portfolio_concentration_events")
    op.drop_index("ix_portfolio_concentration_events_created", table_name="portfolio_concentration_events")
    op.drop_index("ix_portfolio_concentration_events_strategy", table_name="portfolio_concentration_events")
    op.drop_index("ix_portfolio_concentration_events_candidate", table_name="portfolio_concentration_events")
    op.drop_table("portfolio_concentration_events")

    op.drop_index("ix_replay_result_links_created", table_name="replay_result_links")
    op.drop_index("ix_replay_result_links_strategy", table_name="replay_result_links")
    op.drop_index("ix_replay_result_links_candidate", table_name="replay_result_links")
    op.drop_index("ix_replay_result_links_replay", table_name="replay_result_links")
    op.drop_table("replay_result_links")

    op.drop_index("ix_candidate_outcome_attributions_terminal", table_name="candidate_outcome_attributions")
    op.drop_index("ix_candidate_outcome_attributions_group", table_name="candidate_outcome_attributions")
    op.drop_index("ix_candidate_outcome_attributions_window", table_name="candidate_outcome_attributions")
    op.drop_index("ix_candidate_outcome_attributions_strategy", table_name="candidate_outcome_attributions")
    op.drop_index("ix_candidate_outcome_attributions_candidate", table_name="candidate_outcome_attributions")
    op.drop_table("candidate_outcome_attributions")

    op.drop_index("ix_candidate_evidence_links_created", table_name="candidate_evidence_links")
    op.drop_index("ix_candidate_evidence_links_strategy", table_name="candidate_evidence_links")
    op.drop_index("ix_candidate_evidence_links_candidate", table_name="candidate_evidence_links")
    op.drop_table("candidate_evidence_links")

    op.drop_index("ix_strategy_regime_performance_group", table_name="strategy_regime_performance")
    op.drop_column("strategy_regime_performance", "avg_slippage_bps")
    op.drop_column("strategy_regime_performance", "avg_fees_bps")
    op.drop_column("strategy_regime_performance", "avg_drawdown_bps")
    op.drop_column("strategy_regime_performance", "avg_realized_r")
    op.drop_column("strategy_regime_performance", "avg_net_return_bps")
    op.drop_column("strategy_regime_performance", "concentration_event_count")
    op.drop_column("strategy_regime_performance", "council_veto_count")
    op.drop_column("strategy_regime_performance", "risk_reject_count")
    op.drop_column("strategy_regime_performance", "outcome_window")
    op.drop_column("strategy_regime_performance", "venue")
