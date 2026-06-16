"""Add TradFi / equity paper trading tables.

Revision ID: 0007_tradfi
Revises: 0006_newswire
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0007_tradfi"
down_revision = "0006_newswire"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- equity_paper_portfolios ---
    op.create_table(
        "equity_paper_portfolios",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("initial_equity_usd", sa.Float(), nullable=False),
        sa.Column("cash_usd", sa.Float(), nullable=False),
        sa.Column("realized_pnl_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_index("uq_equity_portfolios_name", "equity_paper_portfolios", ["name"], unique=True)

    # --- equity_paper_orders ---
    op.create_table(
        "equity_paper_orders",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("portfolio_id", sa.String(length=64), sa.ForeignKey("equity_paper_portfolios.id"), nullable=False),
        sa.Column("signal_id", sa.String(length=64), nullable=True),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("order_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("requested_px", sa.Float()),
        sa.Column("filled_px", sa.Float()),
        sa.Column("stop_px", sa.Float()),
        sa.Column("take_profit_px", sa.Float()),
        sa.Column("fee_bps", sa.Float(), nullable=False),
        sa.Column("slippage_bps", sa.Float(), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_equity_orders_symbol", "equity_paper_orders", ["symbol"])
    op.create_index("ix_equity_orders_status", "equity_paper_orders", ["status"])
    op.create_index("ix_equity_orders_signal_id", "equity_paper_orders", ["signal_id"])

    # --- equity_paper_fills ---
    op.create_table(
        "equity_paper_fills",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("order_id", sa.String(length=64), sa.ForeignKey("equity_paper_orders.id"), nullable=False),
        sa.Column("portfolio_id", sa.String(length=64), sa.ForeignKey("equity_paper_portfolios.id"), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("fee_usd", sa.Float(), nullable=False),
        sa.Column("slippage_usd", sa.Float(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_equity_fills_symbol", "equity_paper_fills", ["symbol"])

    # --- equity_paper_positions ---
    op.create_table(
        "equity_paper_positions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("portfolio_id", sa.String(length=64), sa.ForeignKey("equity_paper_portfolios.id"), nullable=False),
        sa.Column("signal_id", sa.String(length=64), nullable=True),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("avg_entry_px", sa.Float(), nullable=False),
        sa.Column("mark_px", sa.Float()),
        sa.Column("stop_px", sa.Float()),
        sa.Column("take_profit_px", sa.Float()),
        sa.Column("realized_pnl_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("unrealized_pnl_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
    )
    op.create_index("ix_equity_positions_symbol", "equity_paper_positions", ["symbol"])
    op.create_index("ix_equity_positions_status", "equity_paper_positions", ["status"])

    # --- equity_portfolio_snapshots ---
    op.create_table(
        "equity_portfolio_snapshots",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("portfolio_id", sa.String(length=64), sa.ForeignKey("equity_paper_portfolios.id"), nullable=False),
        sa.Column("timestamp_ms", sa.BigInteger(), nullable=False),
        sa.Column("cash_usd", sa.Float(), nullable=False),
        sa.Column("equity_usd", sa.Float(), nullable=False),
        sa.Column("gross_exposure_usd", sa.Float(), nullable=False),
        sa.Column("net_exposure_usd", sa.Float(), nullable=False),
        sa.Column("realized_pnl_usd", sa.Float(), nullable=False),
        sa.Column("unrealized_pnl_usd", sa.Float(), nullable=False),
        sa.Column("total_pnl_usd", sa.Float(), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_equity_snapshots_portfolio_ts", "equity_portfolio_snapshots", ["portfolio_id", "timestamp_ms"])

    # --- equity_options_flow_events ---
    op.create_table(
        "equity_options_flow_events",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("flow_type", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column("volume_oi_ratio", sa.Float(), nullable=False, server_default="0"),
        sa.Column("premium_estimate", sa.Float(), nullable=False, server_default="0"),
        sa.Column("is_sweep", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("cluster_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("urgency_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("contract_json", sa.JSON()),
        sa.Column("enrichment_json", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_equity_flow_symbol_detected", "equity_options_flow_events", ["symbol", "detected_at"])


def downgrade() -> None:
    op.drop_table("equity_options_flow_events")
    op.drop_index("ix_equity_snapshots_portfolio_ts", table_name="equity_portfolio_snapshots")
    op.drop_table("equity_portfolio_snapshots")
    op.drop_index("ix_equity_positions_status", table_name="equity_paper_positions")
    op.drop_index("ix_equity_positions_symbol", table_name="equity_paper_positions")
    op.drop_table("equity_paper_positions")
    op.drop_index("ix_equity_fills_symbol", table_name="equity_paper_fills")
    op.drop_table("equity_paper_fills")
    op.drop_index("ix_equity_orders_signal_id", table_name="equity_paper_orders")
    op.drop_index("ix_equity_orders_status", table_name="equity_paper_orders")
    op.drop_index("ix_equity_orders_symbol", table_name="equity_paper_orders")
    op.drop_table("equity_paper_orders")
    op.drop_index("uq_equity_portfolios_name", table_name="equity_paper_portfolios")
    op.drop_table("equity_paper_portfolios")
