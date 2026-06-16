"""Add engine candidates, EV, allocation, and debate records.

Revision ID: 0012_candidate_ev_alloc
Revises: 0011_engine_event_feature_store
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0012_candidate_ev_alloc"
down_revision = "0011_engine_event_feature_store"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alpha_candidates",
        sa.Column("candidate_id", sa.String(length=96), primary_key=True),
        sa.Column("strategy_id", sa.String(length=96), nullable=False),
        sa.Column("asset", sa.String(length=64), nullable=False),
        sa.Column("asset_class", sa.String(length=32), nullable=False, server_default="crypto"),
        sa.Column("venue", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("horizon", sa.String(length=32), nullable=False),
        sa.Column("proposed_entry", sa.Float(), nullable=False),
        sa.Column("stop", sa.Float(), nullable=False),
        sa.Column("targets_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("thesis", sa.Text(), nullable=False, server_default=""),
        sa.Column("invalidation_conditions_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("feature_snapshot_id", sa.String(length=96), nullable=False),
        sa.Column("regime_snapshot_id", sa.String(length=96), nullable=False),
        sa.Column("source_event_ids_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("raw_alpha_score", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="new"),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("expires_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_alpha_candidates_status_created", "alpha_candidates", ["status", "created_at_ms"])
    op.create_index("ix_alpha_candidates_asset_status", "alpha_candidates", ["asset", "status"])
    op.create_index("ix_alpha_candidates_strategy_created", "alpha_candidates", ["strategy_id", "created_at_ms"])

    op.create_table(
        "candidate_book_snapshots",
        sa.Column("candidate_book_id", sa.String(length=96), primary_key=True),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("as_of_ms", sa.BigInteger(), nullable=False),
        sa.Column("candidate_ids_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("ranked_candidate_ids_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("rejected_candidate_ids_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("portfolio_state_ref", sa.String(length=128)),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_candidate_book_snapshots_created", "candidate_book_snapshots", ["created_at_ms"])

    op.create_table(
        "ev_estimates",
        sa.Column("estimate_id", sa.String(length=96), primary_key=True),
        sa.Column("candidate_id", sa.String(length=96), nullable=False),
        sa.Column("model_version_id", sa.String(length=128), nullable=False),
        sa.Column("p_target", sa.Float(), nullable=False),
        sa.Column("p_stop", sa.Float(), nullable=False),
        sa.Column("p_timeout", sa.Float(), nullable=False),
        sa.Column("expected_favorable_bps", sa.Float(), nullable=False),
        sa.Column("expected_adverse_bps", sa.Float(), nullable=False),
        sa.Column("expected_holding_ms", sa.BigInteger(), nullable=False),
        sa.Column("expected_fee_bps", sa.Float(), nullable=False),
        sa.Column("expected_spread_cost_bps", sa.Float(), nullable=False),
        sa.Column("expected_slippage_bps", sa.Float(), nullable=False),
        sa.Column("expected_market_impact_bps", sa.Float(), nullable=False),
        sa.Column("expected_funding_cost_bps", sa.Float(), nullable=False),
        sa.Column("tail_loss_bps", sa.Float(), nullable=False),
        sa.Column("net_ev_bps", sa.Float(), nullable=False),
        sa.Column("risk_adjusted_utility", sa.Float(), nullable=False),
        sa.Column("uncertainty", sa.Float(), nullable=False),
        sa.Column("calibration_bucket", sa.String(length=128), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_ev_estimates_candidate", "ev_estimates", ["candidate_id"])
    op.create_index("ix_ev_estimates_created_at_ms", "ev_estimates", ["created_at_ms"])

    op.create_table(
        "allocation_decisions",
        sa.Column("allocation_id", sa.String(length=96), primary_key=True),
        sa.Column("candidate_id", sa.String(length=96), nullable=False),
        sa.Column("candidate_book_id", sa.String(length=96)),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("allocated_size", sa.Float(), nullable=False, server_default="0"),
        sa.Column("allocated_notional_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("risk_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("max_size_multiplier", sa.Float(), nullable=False, server_default="1"),
        sa.Column("opportunity_cost_rank", sa.Integer()),
        sa.Column("constraints_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("reason_codes_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_allocation_decisions_candidate", "allocation_decisions", ["candidate_id"])
    op.create_index("ix_allocation_decisions_created", "allocation_decisions", ["created_at_ms"])

    op.create_table(
        "evidence_packs",
        sa.Column("evidence_pack_id", sa.String(length=96), primary_key=True),
        sa.Column("candidate_id", sa.String(length=96), nullable=False),
        sa.Column("strategy_id", sa.String(length=96), nullable=False),
        sa.Column("asset", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("horizon", sa.String(length=32), nullable=False),
        sa.Column("feature_snapshot_id", sa.String(length=96), nullable=False),
        sa.Column("pack_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_evidence_packs_candidate", "evidence_packs", ["candidate_id"])

    op.create_table(
        "debate_decisions",
        sa.Column("debate_decision_id", sa.String(length=96), primary_key=True),
        sa.Column("evidence_pack_id", sa.String(length=96), nullable=False),
        sa.Column("candidate_id", sa.String(length=96), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("confidence_adjustment", sa.Float(), nullable=False),
        sa.Column("max_size_multiplier", sa.Float(), nullable=False),
        sa.Column("reason_codes_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("required_invalidation_checks_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("audit_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("role_outputs_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("judge_model", sa.String(length=255)),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_debate_decisions_candidate", "debate_decisions", ["candidate_id"])
    op.create_index("ix_debate_decisions_evidence_pack", "debate_decisions", ["evidence_pack_id"])
    op.create_index("ix_debate_decisions_created", "debate_decisions", ["created_at_ms"])


def downgrade() -> None:
    op.drop_index("ix_debate_decisions_created", table_name="debate_decisions")
    op.drop_index("ix_debate_decisions_evidence_pack", table_name="debate_decisions")
    op.drop_index("ix_debate_decisions_candidate", table_name="debate_decisions")
    op.drop_table("debate_decisions")
    op.drop_index("ix_evidence_packs_candidate", table_name="evidence_packs")
    op.drop_table("evidence_packs")
    op.drop_index("ix_allocation_decisions_created", table_name="allocation_decisions")
    op.drop_index("ix_allocation_decisions_candidate", table_name="allocation_decisions")
    op.drop_table("allocation_decisions")
    op.drop_index("ix_ev_estimates_created_at_ms", table_name="ev_estimates")
    op.drop_index("ix_ev_estimates_candidate", table_name="ev_estimates")
    op.drop_table("ev_estimates")
    op.drop_index("ix_candidate_book_snapshots_created", table_name="candidate_book_snapshots")
    op.drop_table("candidate_book_snapshots")
    op.drop_index("ix_alpha_candidates_strategy_created", table_name="alpha_candidates")
    op.drop_index("ix_alpha_candidates_asset_status", table_name="alpha_candidates")
    op.drop_index("ix_alpha_candidates_status_created", table_name="alpha_candidates")
    op.drop_table("alpha_candidates")
