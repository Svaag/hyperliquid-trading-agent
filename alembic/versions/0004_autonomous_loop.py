from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004_autonomous_loop"
down_revision = "0003_position_tracking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "autonomy_events",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("event_type", sa.String(length=96), nullable=False),
        sa.Column("actor", sa.String(length=128), server_default=""),
        sa.Column("symbol", sa.String(length=64)),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_autonomy_events_event_type_created_at", "autonomy_events", ["event_type", "created_at"])
    op.create_index("ix_autonomy_events_symbol_created_at", "autonomy_events", ["symbol", "created_at"])

    op.create_table(
        "market_assets",
        sa.Column("symbol", sa.String(length=64), primary_key=True),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("dex", sa.String(length=64)),
        sa.Column("sz_decimals", sa.Integer()),
        sa.Column("max_leverage", sa.Integer()),
        sa.Column("day_volume_usd", sa.Float()),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "market_observations",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("timestamp_ms", sa.BigInteger(), nullable=False),
        sa.Column("mid", sa.Float()),
        sa.Column("mark", sa.Float()),
        sa.Column("oracle", sa.Float()),
        sa.Column("funding_hourly", sa.Float()),
        sa.Column("open_interest", sa.Float()),
        sa.Column("day_volume_usd", sa.Float()),
        sa.Column("features_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_market_observations_symbol_timestamp", "market_observations", ["symbol", "timestamp_ms"])
    op.create_index("ix_market_observations_timestamp_ms", "market_observations", ["timestamp_ms"])

    op.create_table(
        "market_levels",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("strength", sa.Float(), nullable=False),
        sa.Column("timeframe", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("first_seen_ms", sa.BigInteger(), nullable=False),
        sa.Column("last_seen_ms", sa.BigInteger(), nullable=False),
        sa.Column("expires_at_ms", sa.BigInteger()),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_market_levels_symbol", "market_levels", ["symbol"])
    op.create_index("ix_market_levels_kind", "market_levels", ["kind"])
    op.create_index("ix_market_levels_symbol_kind_price", "market_levels", ["symbol", "kind", "price"])

    op.create_table(
        "news_events",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=255), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("url", sa.Text()),
        sa.Column("author_id", sa.String(length=64)),
        sa.Column("created_at_ms", sa.BigInteger()),
        sa.Column("observed_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("importance_score", sa.Float(), nullable=False),
        sa.Column("sentiment", sa.String(length=32), nullable=False),
        sa.Column("assets_json", sa.JSON(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_news_events_observed_at_ms", "news_events", ["observed_at_ms"])
    op.create_index("ix_news_events_provider", "news_events", ["provider"])

    op.create_table(
        "trade_signals",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("signal_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("expires_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("entry_px", sa.Float(), nullable=False),
        sa.Column("stop_px", sa.Float(), nullable=False),
        sa.Column("take_profit_px", sa.Float()),
        sa.Column("thesis", sa.Text(), nullable=False),
        sa.Column("invalidation", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("feature_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("risk_plan_json", sa.JSON(), nullable=False),
        sa.Column("model_insight_json", sa.JSON()),
        sa.Column("discord_channel_id", sa.String(length=64)),
        sa.Column("discord_message_id", sa.String(length=64)),
        sa.Column("approved_by_discord_user_id", sa.String(length=64)),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("rejected_by_discord_user_id", sa.String(length=64)),
        sa.Column("rejected_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_trade_signals_symbol", "trade_signals", ["symbol"])
    op.create_index("ix_trade_signals_status", "trade_signals", ["status"])
    op.create_index("ix_trade_signals_created_at_ms", "trade_signals", ["created_at_ms"])

    op.create_table(
        "paper_portfolios",
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
    op.create_index("uq_paper_portfolios_name", "paper_portfolios", ["name"], unique=True)

    op.create_table(
        "paper_orders",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("portfolio_id", sa.String(length=64), sa.ForeignKey("paper_portfolios.id"), nullable=False),
        sa.Column("signal_id", sa.String(length=64), sa.ForeignKey("trade_signals.id")),
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
    op.create_index("ix_paper_orders_symbol", "paper_orders", ["symbol"])
    op.create_index("ix_paper_orders_status", "paper_orders", ["status"])
    op.create_index("ix_paper_orders_signal_id", "paper_orders", ["signal_id"])

    op.create_table(
        "paper_fills",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("order_id", sa.String(length=64), sa.ForeignKey("paper_orders.id"), nullable=False),
        sa.Column("portfolio_id", sa.String(length=64), sa.ForeignKey("paper_portfolios.id"), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("fee_usd", sa.Float(), nullable=False),
        sa.Column("slippage_usd", sa.Float(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_paper_fills_symbol", "paper_fills", ["symbol"])

    op.create_table(
        "paper_positions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("portfolio_id", sa.String(length=64), sa.ForeignKey("paper_portfolios.id"), nullable=False),
        sa.Column("signal_id", sa.String(length=64), sa.ForeignKey("trade_signals.id")),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("avg_entry_px", sa.Float(), nullable=False),
        sa.Column("mark_px", sa.Float()),
        sa.Column("stop_px", sa.Float(), nullable=False),
        sa.Column("take_profit_px", sa.Float()),
        sa.Column("realized_pnl_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("unrealized_pnl_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
    )
    op.create_index("ix_paper_positions_symbol", "paper_positions", ["symbol"])
    op.create_index("ix_paper_positions_status", "paper_positions", ["status"])

    op.create_table(
        "portfolio_snapshots",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("portfolio_id", sa.String(length=64), sa.ForeignKey("paper_portfolios.id"), nullable=False),
        sa.Column("timestamp_ms", sa.BigInteger(), nullable=False),
        sa.Column("cash_usd", sa.Float(), nullable=False),
        sa.Column("equity_usd", sa.Float(), nullable=False),
        sa.Column("gross_exposure_usd", sa.Float(), nullable=False),
        sa.Column("net_exposure_usd", sa.Float(), nullable=False),
        sa.Column("realized_pnl_usd", sa.Float(), nullable=False),
        sa.Column("unrealized_pnl_usd", sa.Float(), nullable=False),
        sa.Column("total_pnl_usd", sa.Float(), nullable=False),
        sa.Column("drawdown_pct", sa.Float(), nullable=False),
        sa.Column("sharpe", sa.Float()),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_portfolio_snapshots_portfolio_timestamp", "portfolio_snapshots", ["portfolio_id", "timestamp_ms"])


def downgrade() -> None:
    op.drop_index("ix_portfolio_snapshots_portfolio_timestamp", table_name="portfolio_snapshots")
    op.drop_table("portfolio_snapshots")
    op.drop_index("ix_paper_positions_status", table_name="paper_positions")
    op.drop_index("ix_paper_positions_symbol", table_name="paper_positions")
    op.drop_table("paper_positions")
    op.drop_index("ix_paper_fills_symbol", table_name="paper_fills")
    op.drop_table("paper_fills")
    op.drop_index("ix_paper_orders_signal_id", table_name="paper_orders")
    op.drop_index("ix_paper_orders_status", table_name="paper_orders")
    op.drop_index("ix_paper_orders_symbol", table_name="paper_orders")
    op.drop_table("paper_orders")
    op.drop_index("uq_paper_portfolios_name", table_name="paper_portfolios")
    op.drop_table("paper_portfolios")
    op.drop_index("ix_trade_signals_created_at_ms", table_name="trade_signals")
    op.drop_index("ix_trade_signals_status", table_name="trade_signals")
    op.drop_index("ix_trade_signals_symbol", table_name="trade_signals")
    op.drop_table("trade_signals")
    op.drop_index("ix_news_events_provider", table_name="news_events")
    op.drop_index("ix_news_events_observed_at_ms", table_name="news_events")
    op.drop_table("news_events")
    op.drop_index("ix_market_levels_symbol_kind_price", table_name="market_levels")
    op.drop_index("ix_market_levels_kind", table_name="market_levels")
    op.drop_index("ix_market_levels_symbol", table_name="market_levels")
    op.drop_table("market_levels")
    op.drop_index("ix_market_observations_timestamp_ms", table_name="market_observations")
    op.drop_index("ix_market_observations_symbol_timestamp", table_name="market_observations")
    op.drop_table("market_observations")
    op.drop_table("market_assets")
    op.drop_index("ix_autonomy_events_symbol_created_at", table_name="autonomy_events")
    op.drop_index("ix_autonomy_events_event_type_created_at", table_name="autonomy_events")
    op.drop_table("autonomy_events")
