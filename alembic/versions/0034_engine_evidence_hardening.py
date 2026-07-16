"""Harden strategy promotion, EV, execution-cost, and outcome evidence.

Revision ID: 0034_engine_evidence_hardening
Revises: 0033_world_model_v2
"""

from __future__ import annotations

import json
import time

import sqlalchemy as sa

from alembic import op

revision = "0034_engine_evidence_hardening"
down_revision = "0033_world_model_v2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategy_version_policies",
        sa.Column("strategy_version_key", sa.String(192), primary_key=True),
        sa.Column("strategy_id", sa.String(96), nullable=False),
        sa.Column("strategy_version", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("reason_codes_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("effective_from_ms", sa.BigInteger(), nullable=False),
        sa.Column("effective_until_ms", sa.BigInteger()),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_strategy_version_policies_strategy",
        "strategy_version_policies",
        ["strategy_id", "strategy_version"],
    )
    op.create_index("ix_strategy_version_policies_state", "strategy_version_policies", ["state"])

    op.add_column("ev_estimates", sa.Column("gross_ev_bps", sa.Float(), nullable=False, server_default="0"))
    op.add_column("ev_estimates", sa.Column("execution_cost_quote_id", sa.String(128)))
    op.add_column(
        "allocation_decisions",
        sa.Column("allocation_scope", sa.String(32), nullable=False, server_default="unknown"),
    )

    op.create_table(
        "execution_cost_quotes",
        sa.Column("quote_id", sa.String(128), primary_key=True),
        sa.Column("candidate_id", sa.String(96), nullable=False),
        sa.Column("venue_id", sa.String(96), nullable=False),
        sa.Column("instrument_id", sa.String(96), nullable=False),
        sa.Column("side", sa.String(16), nullable=False),
        sa.Column("requested_size", sa.Float(), nullable=False),
        sa.Column("requested_notional_usd", sa.Float(), nullable=False),
        sa.Column("reference_price", sa.Float(), nullable=False),
        sa.Column("simulated_fill_size", sa.Float(), nullable=False, server_default="0"),
        sa.Column("simulated_avg_fill_px", sa.Float()),
        sa.Column("fee_bps", sa.Float(), nullable=False, server_default="0"),
        sa.Column("spread_cost_bps", sa.Float(), nullable=False, server_default="0"),
        sa.Column("slippage_bps", sa.Float(), nullable=False, server_default="0"),
        sa.Column("market_impact_bps", sa.Float(), nullable=False, server_default="0"),
        sa.Column("latency_slippage_bps", sa.Float(), nullable=False, server_default="0"),
        sa.Column("total_execution_cost_bps", sa.Float(), nullable=False, server_default="0"),
        sa.Column("cost_quality", sa.String(32), nullable=False),
        sa.Column("book_snapshot_id", sa.String(128)),
        sa.Column("fee_schedule_id", sa.String(128)),
        sa.Column("simulation_model_version", sa.String(64), nullable=False),
        sa.Column("book_as_of_ms", sa.BigInteger()),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("reason_codes_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_execution_cost_quotes_candidate", "execution_cost_quotes", ["candidate_id"])
    op.create_index(
        "ix_execution_cost_quotes_venue_created",
        "execution_cost_quotes",
        ["venue_id", "created_at_ms"],
    )
    op.create_index("ix_execution_cost_quotes_quality", "execution_cost_quotes", ["cost_quality"])

    for column in (
        sa.Column("execution_adjusted_return_bps", sa.Float()),
        sa.Column("execution_cost_quote_id", sa.String(128)),
        sa.Column("execution_report_id", sa.String(128)),
        sa.Column("execution_cost_quality", sa.String(32), nullable=False, server_default="unavailable"),
    ):
        op.add_column("candidate_outcome_attributions", column)

    for column in (
        sa.Column("execution_cost_quote_id", sa.String(128)),
        sa.Column("fee_bps", sa.Float(), nullable=False, server_default="0"),
        sa.Column("spread_cost_bps", sa.Float(), nullable=False, server_default="0"),
        sa.Column("latency_slippage_bps", sa.Float(), nullable=False, server_default="0"),
        sa.Column("total_execution_cost_bps", sa.Float(), nullable=False, server_default="0"),
        sa.Column("book_snapshot_id", sa.String(128)),
        sa.Column("fee_schedule_id", sa.String(128)),
        sa.Column("simulation_model_version", sa.String(64)),
        sa.Column("cost_quality", sa.String(32), nullable=False, server_default="unavailable"),
    ):
        op.add_column("execution_reports", column)

    # Freeze every implementation that existed when this migration was applied.
    # Later, previously unseen versions are admitted as research_only by runtime.
    specs = sa.table(
        "strategy_specs",
        sa.column("strategy_id", sa.String),
        sa.column("version", sa.String),
    )
    policies = sa.table(
        "strategy_version_policies",
        sa.column("strategy_version_key", sa.String),
        sa.column("strategy_id", sa.String),
        sa.column("strategy_version", sa.String),
        sa.column("state", sa.String),
        sa.column("reason_codes_json", sa.JSON),
        sa.column("effective_from_ms", sa.BigInteger),
        sa.column("created_at_ms", sa.BigInteger),
        sa.column("updated_at_ms", sa.BigInteger),
        sa.column("metadata_json", sa.JSON),
    )
    now = int(time.time() * 1000)
    freeze_existing = sa.select(
        specs.c.strategy_id + sa.literal("@") + specs.c.version,
        specs.c.strategy_id,
        specs.c.version,
        sa.literal("frozen"),
        sa.cast(
            sa.literal(
                json.dumps(
                    [
                        "current_strategy_version_frozen",
                        "negative_strict_cohort_review_2026_07_16",
                    ]
                )
            ),
            sa.JSON(),
        ),
        sa.literal(now),
        sa.literal(now),
        sa.literal(now),
        sa.cast(sa.literal(json.dumps({"migration": revision})), sa.JSON()),
    )
    op.execute(
        policies.insert().from_select(
            [
                "strategy_version_key",
                "strategy_id",
                "strategy_version",
                "state",
                "reason_codes_json",
                "effective_from_ms",
                "created_at_ms",
                "updated_at_ms",
                "metadata_json",
            ],
            freeze_existing,
        )
    )


def downgrade() -> None:
    for name in (
        "cost_quality",
        "simulation_model_version",
        "fee_schedule_id",
        "book_snapshot_id",
        "total_execution_cost_bps",
        "latency_slippage_bps",
        "spread_cost_bps",
        "fee_bps",
        "execution_cost_quote_id",
    ):
        op.drop_column("execution_reports", name)
    for name in (
        "execution_cost_quality",
        "execution_report_id",
        "execution_cost_quote_id",
        "execution_adjusted_return_bps",
    ):
        op.drop_column("candidate_outcome_attributions", name)
    op.drop_index("ix_execution_cost_quotes_quality", table_name="execution_cost_quotes")
    op.drop_index("ix_execution_cost_quotes_venue_created", table_name="execution_cost_quotes")
    op.drop_index("ix_execution_cost_quotes_candidate", table_name="execution_cost_quotes")
    op.drop_table("execution_cost_quotes")
    op.drop_column("allocation_decisions", "allocation_scope")
    op.drop_column("ev_estimates", "execution_cost_quote_id")
    op.drop_column("ev_estimates", "gross_ev_bps")
    op.drop_index("ix_strategy_version_policies_state", table_name="strategy_version_policies")
    op.drop_index("ix_strategy_version_policies_strategy", table_name="strategy_version_policies")
    op.drop_table("strategy_version_policies")
