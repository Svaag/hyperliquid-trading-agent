"""Add engine strategy, council, diversity, and learning tables.

Revision ID: 0019_strategy_regime_council
Revises: 0018_liquidations
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0019_strategy_regime_council"
down_revision = "0018_liquidations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategy_specs",
        sa.Column("strategy_id", sa.String(length=96), primary_key=True),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("family", sa.String(length=96), nullable=False),
        sa.Column("supported_assets_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("supported_venues_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("supported_horizons_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("required_features_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("valid_regimes_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("max_candidates_per_run", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("max_allocation_share_pct", sa.Float(), nullable=False, server_default="45.0"),
        sa.Column("cooldown_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("min_confidence", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("min_ev_bps", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("risk_tags_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("counts_for_breadth", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_strategy_specs_family", "strategy_specs", ["family"])
    op.create_index("ix_strategy_specs_enabled", "strategy_specs", ["enabled"])

    op.create_table(
        "strategy_regime_performance",
        sa.Column("performance_id", sa.String(length=128), primary_key=True),
        sa.Column("strategy_id", sa.String(length=96), nullable=False),
        sa.Column("strategy_version", sa.String(length=64), nullable=False, server_default="unknown"),
        sa.Column("strategy_family", sa.String(length=96), nullable=False, server_default="unknown"),
        sa.Column("regime_label", sa.String(length=255), nullable=False),
        sa.Column("asset", sa.String(length=64), nullable=False, server_default="GLOBAL"),
        sa.Column("window_start_ms", sa.BigInteger(), nullable=False),
        sa.Column("window_end_ms", sa.BigInteger(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("allocation_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("win_rate_pct", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("avg_net_ev_bps", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("realized_pnl_usd", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("score", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_strategy_regime_performance_strategy", "strategy_regime_performance", ["strategy_id"])
    op.create_index("ix_strategy_regime_performance_regime", "strategy_regime_performance", ["regime_label"])
    op.create_index("ix_strategy_regime_performance_window", "strategy_regime_performance", ["window_end_ms"])

    op.create_table(
        "allocation_diversity_events",
        sa.Column("event_id", sa.String(length=128), primary_key=True),
        sa.Column("candidate_id", sa.String(length=96), nullable=False),
        sa.Column("allocation_id", sa.String(length=96), nullable=False),
        sa.Column("strategy_id", sa.String(length=96), nullable=False),
        sa.Column("strategy_version", sa.String(length=64), nullable=False, server_default="unknown"),
        sa.Column("strategy_family", sa.String(length=96), nullable=False, server_default="unknown"),
        sa.Column("asset", sa.String(length=64), nullable=False),
        sa.Column("venue", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("reason_codes_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_allocation_diversity_events_candidate", "allocation_diversity_events", ["candidate_id"])
    op.create_index("ix_allocation_diversity_events_strategy", "allocation_diversity_events", ["strategy_id"])
    op.create_index("ix_allocation_diversity_events_created", "allocation_diversity_events", ["created_at_ms"])
    op.create_index("ix_allocation_diversity_events_decision", "allocation_diversity_events", ["decision"])

    op.create_table(
        "candidate_trade_packets",
        sa.Column("packet_id", sa.String(length=128), primary_key=True),
        sa.Column("candidate_id", sa.String(length=96), nullable=False),
        sa.Column("strategy_id", sa.String(length=96), nullable=False),
        sa.Column("strategy_version", sa.String(length=64), nullable=False, server_default="unknown"),
        sa.Column("strategy_family", sa.String(length=96), nullable=False, server_default="unknown"),
        sa.Column("asset", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("horizon", sa.String(length=32), nullable=False),
        sa.Column("feature_snapshot_id", sa.String(length=96), nullable=False),
        sa.Column("regime_snapshot_id", sa.String(length=96), nullable=False),
        sa.Column("packet_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_candidate_trade_packets_candidate", "candidate_trade_packets", ["candidate_id"])
    op.create_index("ix_candidate_trade_packets_strategy", "candidate_trade_packets", ["strategy_id"])
    op.create_index("ix_candidate_trade_packets_created", "candidate_trade_packets", ["created_at_ms"])

    op.create_table(
        "council_reviews",
        sa.Column("review_id", sa.String(length=128), primary_key=True),
        sa.Column("packet_id", sa.String(length=128), nullable=False),
        sa.Column("candidate_id", sa.String(length=96), nullable=False),
        sa.Column("strategy_id", sa.String(length=96), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("vetoes_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("warnings_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("required_evidence_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("regime_fit_score", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("strategy_regime_score", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("portfolio_impact_score", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_council_reviews_candidate", "council_reviews", ["candidate_id"])
    op.create_index("ix_council_reviews_strategy", "council_reviews", ["strategy_id"])
    op.create_index("ix_council_reviews_decision", "council_reviews", ["decision"])
    op.create_index("ix_council_reviews_created", "council_reviews", ["created_at_ms"])

    op.create_table(
        "council_votes",
        sa.Column("vote_id", sa.String(length=128), primary_key=True),
        sa.Column("review_id", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("vetoes_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("warnings_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("required_evidence_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("scores_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_council_votes_review", "council_votes", ["review_id"])
    op.create_index("ix_council_votes_role", "council_votes", ["role"])
    op.create_index("ix_council_votes_created", "council_votes", ["created_at_ms"])

    op.create_table(
        "bandit_policy_snapshots",
        sa.Column("policy_id", sa.String(length=128), primary_key=True),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="report_only"),
        sa.Column("trained_window_start_ms", sa.BigInteger(), nullable=False),
        sa.Column("trained_window_end_ms", sa.BigInteger(), nullable=False),
        sa.Column("context_features_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("arms_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("policy_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_bandit_policy_snapshots_status", "bandit_policy_snapshots", ["status"])
    op.create_index("ix_bandit_policy_snapshots_created", "bandit_policy_snapshots", ["created_at_ms"])

    op.create_table(
        "bandit_recommendations",
        sa.Column("recommendation_id", sa.String(length=128), primary_key=True),
        sa.Column("policy_id", sa.String(length=128), nullable=False),
        sa.Column("strategy_id", sa.String(length=96), nullable=False),
        sa.Column("asset", sa.String(length=64), nullable=False, server_default="GLOBAL"),
        sa.Column("regime_label", sa.String(length=255), nullable=False, server_default="unknown"),
        sa.Column("recommendation", sa.Text(), nullable=False, server_default=""),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("expected_score_delta", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("auto_apply_allowed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_bandit_recommendations_policy", "bandit_recommendations", ["policy_id"])
    op.create_index("ix_bandit_recommendations_strategy", "bandit_recommendations", ["strategy_id"])
    op.create_index("ix_bandit_recommendations_created", "bandit_recommendations", ["created_at_ms"])


def downgrade() -> None:
    op.drop_index("ix_bandit_recommendations_created", table_name="bandit_recommendations")
    op.drop_index("ix_bandit_recommendations_strategy", table_name="bandit_recommendations")
    op.drop_index("ix_bandit_recommendations_policy", table_name="bandit_recommendations")
    op.drop_table("bandit_recommendations")
    op.drop_index("ix_bandit_policy_snapshots_created", table_name="bandit_policy_snapshots")
    op.drop_index("ix_bandit_policy_snapshots_status", table_name="bandit_policy_snapshots")
    op.drop_table("bandit_policy_snapshots")
    op.drop_index("ix_council_votes_created", table_name="council_votes")
    op.drop_index("ix_council_votes_role", table_name="council_votes")
    op.drop_index("ix_council_votes_review", table_name="council_votes")
    op.drop_table("council_votes")
    op.drop_index("ix_council_reviews_created", table_name="council_reviews")
    op.drop_index("ix_council_reviews_decision", table_name="council_reviews")
    op.drop_index("ix_council_reviews_strategy", table_name="council_reviews")
    op.drop_index("ix_council_reviews_candidate", table_name="council_reviews")
    op.drop_table("council_reviews")
    op.drop_index("ix_candidate_trade_packets_created", table_name="candidate_trade_packets")
    op.drop_index("ix_candidate_trade_packets_strategy", table_name="candidate_trade_packets")
    op.drop_index("ix_candidate_trade_packets_candidate", table_name="candidate_trade_packets")
    op.drop_table("candidate_trade_packets")
    op.drop_index("ix_allocation_diversity_events_decision", table_name="allocation_diversity_events")
    op.drop_index("ix_allocation_diversity_events_created", table_name="allocation_diversity_events")
    op.drop_index("ix_allocation_diversity_events_strategy", table_name="allocation_diversity_events")
    op.drop_index("ix_allocation_diversity_events_candidate", table_name="allocation_diversity_events")
    op.drop_table("allocation_diversity_events")
    op.drop_index("ix_strategy_regime_performance_window", table_name="strategy_regime_performance")
    op.drop_index("ix_strategy_regime_performance_regime", table_name="strategy_regime_performance")
    op.drop_index("ix_strategy_regime_performance_strategy", table_name="strategy_regime_performance")
    op.drop_table("strategy_regime_performance")
    op.drop_index("ix_strategy_specs_enabled", table_name="strategy_specs")
    op.drop_index("ix_strategy_specs_family", table_name="strategy_specs")
    op.drop_table("strategy_specs")
