"""Add HIP-4 outcome paper/shadow tables.

Revision ID: 0015_hip4_outcomes
Revises: 0014_model_registry_retention
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0015_hip4_outcomes"
down_revision = "0014_model_registry_retention"
branch_labels = None
depends_on = None


def _json_default(value: str) -> sa.TextClause:
    return sa.text(f"'{value}'")


def upgrade() -> None:
    op.create_table(
        "hip4_capability_probes",
        sa.Column("probe_id", sa.String(length=96), primary_key=True),
        sa.Column("network", sa.String(length=32), nullable=False),
        sa.Column("probed_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("outcome_meta_available", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("outcome_meta_error", sa.String(length=128)),
        sa.Column("outcome_meta_schema_hash", sa.String(length=128)),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("degraded_reasons_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_hip4_capability_probes_network_created", "hip4_capability_probes", ["network", "probed_at_ms"])
    op.create_index("ix_hip4_capability_probes_schema_hash", "hip4_capability_probes", ["outcome_meta_schema_hash"])

    op.create_table(
        "hip4_raw_payloads",
        sa.Column("payload_id", sa.String(length=96), primary_key=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("network", sa.String(length=32), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("schema_hash", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("observed_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_hip4_raw_payloads_source_network", "hip4_raw_payloads", ["source", "network"])
    op.create_index("ix_hip4_raw_payloads_observed", "hip4_raw_payloads", ["observed_at_ms"])
    op.create_index("ix_hip4_raw_payloads_schema_hash", "hip4_raw_payloads", ["schema_hash"])

    op.create_table(
        "hip4_outcome_specs",
        sa.Column("outcome_id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("quote_token", sa.String(length=64)),
        sa.Column("side0_name", sa.String(length=64), nullable=False, server_default=sa.text("'YES'")),
        sa.Column("side1_name", sa.String(length=64), nullable=False, server_default=sa.text("'NO'")),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'open'")),
        sa.Column("settle_fraction", sa.String(length=96)),
        sa.Column("settlement_details", sa.Text()),
        sa.Column("raw_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("as_of_ms", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_hip4_outcome_specs_outcome", "hip4_outcome_specs", ["outcome_id"])
    op.create_index("ix_hip4_outcome_specs_status", "hip4_outcome_specs", ["status"])
    op.create_index("ix_hip4_outcome_specs_as_of", "hip4_outcome_specs", ["as_of_ms"])

    op.create_table(
        "hip4_question_specs",
        sa.Column("question_id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("fallback_outcome_id", sa.Integer()),
        sa.Column("named_outcome_ids_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("settled_named_outcome_ids_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("outcome_ids_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'open'")),
        sa.Column("raw_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("as_of_ms", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_hip4_question_specs_question", "hip4_question_specs", ["question_id"])
    op.create_index("ix_hip4_question_specs_status", "hip4_question_specs", ["status"])
    op.create_index("ix_hip4_question_specs_as_of", "hip4_question_specs", ["as_of_ms"])

    op.create_table(
        "hip4_market_snapshots",
        sa.Column("snapshot_id", sa.String(length=96), primary_key=True),
        sa.Column("question_id", sa.Integer()),
        sa.Column("outcome_id", sa.Integer()),
        sa.Column("coin", sa.String(length=32), nullable=False),
        sa.Column("side", sa.Integer(), nullable=False),
        sa.Column("as_of_ms", sa.BigInteger(), nullable=False),
        sa.Column("best_bid", sa.String(length=96)),
        sa.Column("best_ask", sa.String(length=96)),
        sa.Column("raw_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_hip4_market_snapshots_question", "hip4_market_snapshots", ["question_id"])
    op.create_index("ix_hip4_market_snapshots_outcome", "hip4_market_snapshots", ["outcome_id"])
    op.create_index("ix_hip4_market_snapshots_as_of", "hip4_market_snapshots", ["as_of_ms"])

    op.create_table(
        "hip4_edge_candidates",
        sa.Column("candidate_id", sa.String(length=96), primary_key=True),
        sa.Column("strategy_type", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("question_id", sa.Integer()),
        sa.Column("outcome_ids_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("as_of_ms", sa.BigInteger(), nullable=False),
        sa.Column("size", sa.String(length=96), nullable=False),
        sa.Column("gross_cost_or_proceeds", sa.String(length=96), nullable=False),
        sa.Column("expected_net_edge_usd", sa.String(length=96), nullable=False),
        sa.Column("expected_net_edge_bps", sa.String(length=96), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("candidate_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_hip4_edge_candidates_candidate", "hip4_edge_candidates", ["candidate_id"])
    op.create_index("ix_hip4_edge_candidates_question", "hip4_edge_candidates", ["question_id"])
    op.create_index("ix_hip4_edge_candidates_status", "hip4_edge_candidates", ["status"])
    op.create_index("ix_hip4_edge_candidates_as_of", "hip4_edge_candidates", ["as_of_ms"])

    op.create_table(
        "hip4_paper_portfolios",
        sa.Column("portfolio_id", sa.String(length=96), primary_key=True),
        sa.Column("quote_token", sa.String(length=64), nullable=False),
        sa.Column("cash", sa.String(length=96), nullable=False),
        sa.Column("realized_pnl", sa.String(length=96), nullable=False),
        sa.Column("unrealized_pnl", sa.String(length=96), nullable=False),
        sa.Column("settlement_pnl", sa.String(length=96), nullable=False),
        sa.Column("modeled_fees", sa.String(length=96), nullable=False),
        sa.Column("daily_notional", sa.String(length=96), nullable=False),
        sa.Column("balances_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "hip4_paper_positions",
        sa.Column("position_id", sa.String(length=96), primary_key=True),
        sa.Column("portfolio_id", sa.String(length=96), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("balance", sa.String(length=96), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_hip4_paper_positions_token", "hip4_paper_positions", ["token"])

    op.create_table(
        "hip4_paper_actions",
        sa.Column("action_id", sa.String(length=96), primary_key=True),
        sa.Column("candidate_id", sa.String(length=96)),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("amount", sa.String(length=96), nullable=False),
        sa.Column("price", sa.String(length=96)),
        sa.Column("action_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_hip4_paper_actions_candidate", "hip4_paper_actions", ["candidate_id"])
    op.create_index("ix_hip4_paper_actions_action_type", "hip4_paper_actions", ["action_type"])
    op.create_index("ix_hip4_paper_actions_created", "hip4_paper_actions", ["created_at_ms"])

    op.create_table(
        "hip4_paper_fills",
        sa.Column("fill_id", sa.String(length=96), primary_key=True),
        sa.Column("candidate_id", sa.String(length=96), nullable=False),
        sa.Column("coin", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("size", sa.String(length=96), nullable=False),
        sa.Column("price", sa.String(length=96), nullable=False),
        sa.Column("notional", sa.String(length=96), nullable=False),
        sa.Column("fee", sa.String(length=96), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_hip4_paper_fills_candidate", "hip4_paper_fills", ["candidate_id"])
    op.create_index("ix_hip4_paper_fills_created", "hip4_paper_fills", ["created_at_ms"])

    op.create_table(
        "hip4_reconciliation_runs",
        sa.Column("run_id", sa.String(length=96), primary_key=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("discrepancies_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("result_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_hip4_reconciliation_runs_status", "hip4_reconciliation_runs", ["status"])
    op.create_index("ix_hip4_reconciliation_runs_created", "hip4_reconciliation_runs", ["created_at_ms"])

    op.create_table(
        "hip4_settlements",
        sa.Column("settlement_id", sa.String(length=96), primary_key=True),
        sa.Column("outcome_id", sa.Integer(), nullable=False),
        sa.Column("settle_fraction", sa.String(length=96)),
        sa.Column("details", sa.Text()),
        sa.Column("raw_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("as_of_ms", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_hip4_settlements_outcome", "hip4_settlements", ["outcome_id"])
    op.create_index("ix_hip4_settlements_as_of", "hip4_settlements", ["as_of_ms"])


def downgrade() -> None:
    op.drop_index("ix_hip4_settlements_as_of", table_name="hip4_settlements")
    op.drop_index("ix_hip4_settlements_outcome", table_name="hip4_settlements")
    op.drop_table("hip4_settlements")
    op.drop_index("ix_hip4_reconciliation_runs_created", table_name="hip4_reconciliation_runs")
    op.drop_index("ix_hip4_reconciliation_runs_status", table_name="hip4_reconciliation_runs")
    op.drop_table("hip4_reconciliation_runs")
    op.drop_index("ix_hip4_paper_fills_created", table_name="hip4_paper_fills")
    op.drop_index("ix_hip4_paper_fills_candidate", table_name="hip4_paper_fills")
    op.drop_table("hip4_paper_fills")
    op.drop_index("ix_hip4_paper_actions_created", table_name="hip4_paper_actions")
    op.drop_index("ix_hip4_paper_actions_action_type", table_name="hip4_paper_actions")
    op.drop_index("ix_hip4_paper_actions_candidate", table_name="hip4_paper_actions")
    op.drop_table("hip4_paper_actions")
    op.drop_index("ix_hip4_paper_positions_token", table_name="hip4_paper_positions")
    op.drop_table("hip4_paper_positions")
    op.drop_table("hip4_paper_portfolios")
    op.drop_index("ix_hip4_edge_candidates_as_of", table_name="hip4_edge_candidates")
    op.drop_index("ix_hip4_edge_candidates_status", table_name="hip4_edge_candidates")
    op.drop_index("ix_hip4_edge_candidates_question", table_name="hip4_edge_candidates")
    op.drop_index("ix_hip4_edge_candidates_candidate", table_name="hip4_edge_candidates")
    op.drop_table("hip4_edge_candidates")
    op.drop_index("ix_hip4_market_snapshots_as_of", table_name="hip4_market_snapshots")
    op.drop_index("ix_hip4_market_snapshots_outcome", table_name="hip4_market_snapshots")
    op.drop_index("ix_hip4_market_snapshots_question", table_name="hip4_market_snapshots")
    op.drop_table("hip4_market_snapshots")
    op.drop_index("ix_hip4_question_specs_as_of", table_name="hip4_question_specs")
    op.drop_index("ix_hip4_question_specs_status", table_name="hip4_question_specs")
    op.drop_index("ix_hip4_question_specs_question", table_name="hip4_question_specs")
    op.drop_table("hip4_question_specs")
    op.drop_index("ix_hip4_outcome_specs_as_of", table_name="hip4_outcome_specs")
    op.drop_index("ix_hip4_outcome_specs_status", table_name="hip4_outcome_specs")
    op.drop_index("ix_hip4_outcome_specs_outcome", table_name="hip4_outcome_specs")
    op.drop_table("hip4_outcome_specs")
    op.drop_index("ix_hip4_raw_payloads_schema_hash", table_name="hip4_raw_payloads")
    op.drop_index("ix_hip4_raw_payloads_observed", table_name="hip4_raw_payloads")
    op.drop_index("ix_hip4_raw_payloads_source_network", table_name="hip4_raw_payloads")
    op.drop_table("hip4_raw_payloads")
    op.drop_index("ix_hip4_capability_probes_schema_hash", table_name="hip4_capability_probes")
    op.drop_index("ix_hip4_capability_probes_network_created", table_name="hip4_capability_probes")
    op.drop_table("hip4_capability_probes")
