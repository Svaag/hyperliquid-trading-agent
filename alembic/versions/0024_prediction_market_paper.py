"""Add prediction-market paper trading tables.

Revision ID: 0024_prediction_market_paper
Revises: 0023_newswire_policy_loop
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0024_prediction_market_paper"
down_revision = "0023_newswire_policy_loop"
branch_labels = None
depends_on = None


def _json_default(value: str) -> sa.TextClause:
    return sa.text(f"'{value}'")


def upgrade() -> None:
    op.create_table(
        "prediction_market_paper_accounts",
        sa.Column("account_id", sa.String(length=64), primary_key=True),
        sa.Column("discord_guild_id", sa.String(length=64), nullable=False),
        sa.Column("discord_user_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("initial_cash_usd", sa.Float(), nullable=False),
        sa.Column("cash_usd", sa.Float(), nullable=False),
        sa.Column("realized_pnl_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("discord_guild_id", "discord_user_id", name="uq_prediction_market_accounts_guild_user"),
    )
    op.create_index("ix_prediction_market_accounts_guild", "prediction_market_paper_accounts", ["discord_guild_id"])

    op.create_table(
        "prediction_market_bet_drafts",
        sa.Column("draft_id", sa.String(length=64), primary_key=True),
        sa.Column("account_id", sa.String(length=64), sa.ForeignKey("prediction_market_paper_accounts.account_id"), nullable=False),
        sa.Column("discord_guild_id", sa.String(length=64), nullable=False),
        sa.Column("discord_user_id", sa.String(length=64), nullable=False),
        sa.Column("venue", sa.String(length=64), nullable=False),
        sa.Column("market_id", sa.String(length=128), nullable=False),
        sa.Column("outcome_id", sa.String(length=128)),
        sa.Column("outcome_name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("stake_usd", sa.Float(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("shares", sa.Float(), nullable=False),
        sa.Column("quote_signal_id", sa.String(length=128)),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="new"),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("expires_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("confirmed_at_ms", sa.BigInteger()),
        sa.Column("cancelled_at_ms", sa.BigInteger()),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_prediction_market_drafts_account_status", "prediction_market_bet_drafts", ["account_id", "status"])
    op.create_index("ix_prediction_market_drafts_guild_user", "prediction_market_bet_drafts", ["discord_guild_id", "discord_user_id"])
    op.create_index("ix_prediction_market_drafts_market", "prediction_market_bet_drafts", ["venue", "market_id", "outcome_id"])

    op.create_table(
        "prediction_market_positions",
        sa.Column("position_id", sa.String(length=64), primary_key=True),
        sa.Column("account_id", sa.String(length=64), sa.ForeignKey("prediction_market_paper_accounts.account_id"), nullable=False),
        sa.Column("discord_guild_id", sa.String(length=64), nullable=False),
        sa.Column("discord_user_id", sa.String(length=64), nullable=False),
        sa.Column("draft_id", sa.String(length=64)),
        sa.Column("venue", sa.String(length=64), nullable=False),
        sa.Column("market_id", sa.String(length=128), nullable=False),
        sa.Column("outcome_id", sa.String(length=128)),
        sa.Column("outcome_name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="open"),
        sa.Column("shares", sa.Float(), nullable=False),
        sa.Column("avg_entry_price", sa.Float(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("mark_price", sa.Float()),
        sa.Column("current_value_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("realized_pnl_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("unrealized_pnl_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("opened_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("closed_at_ms", sa.BigInteger()),
        sa.Column("settled_at_ms", sa.BigInteger()),
        sa.Column("result", sa.String(length=32), nullable=False, server_default="open"),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_prediction_market_positions_account_status", "prediction_market_positions", ["account_id", "status"])
    op.create_index("ix_prediction_market_positions_guild_user", "prediction_market_positions", ["discord_guild_id", "discord_user_id"])
    op.create_index("ix_prediction_market_positions_market", "prediction_market_positions", ["venue", "market_id", "outcome_id"])
    op.create_index("ix_prediction_market_positions_status", "prediction_market_positions", ["status"])

    op.create_table(
        "prediction_market_fills",
        sa.Column("fill_id", sa.String(length=64), primary_key=True),
        sa.Column("account_id", sa.String(length=64), sa.ForeignKey("prediction_market_paper_accounts.account_id"), nullable=False),
        sa.Column("position_id", sa.String(length=64)),
        sa.Column("draft_id", sa.String(length=64)),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("venue", sa.String(length=64), nullable=False),
        sa.Column("market_id", sa.String(length=128), nullable=False),
        sa.Column("outcome_id", sa.String(length=128)),
        sa.Column("shares", sa.Float(), nullable=False, server_default="0"),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("cash_delta_usd", sa.Float(), nullable=False),
        sa.Column("realized_pnl_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_prediction_market_fills_account", "prediction_market_fills", ["account_id"])
    op.create_index("ix_prediction_market_fills_position", "prediction_market_fills", ["position_id"])
    op.create_index("ix_prediction_market_fills_created", "prediction_market_fills", ["created_at_ms"])

    op.create_table(
        "prediction_market_settlements",
        sa.Column("settlement_id", sa.String(length=64), primary_key=True),
        sa.Column("venue", sa.String(length=64), nullable=False),
        sa.Column("market_id", sa.String(length=128), nullable=False),
        sa.Column("outcome_id", sa.String(length=128)),
        sa.Column("settlement_fraction", sa.Float(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("applied_by", sa.String(length=128), nullable=False, server_default="system"),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_prediction_market_settlements_market", "prediction_market_settlements", ["venue", "market_id", "outcome_id"])
    op.create_index("ix_prediction_market_settlements_created", "prediction_market_settlements", ["created_at_ms"])


def downgrade() -> None:
    op.drop_index("ix_prediction_market_settlements_created", table_name="prediction_market_settlements")
    op.drop_index("ix_prediction_market_settlements_market", table_name="prediction_market_settlements")
    op.drop_table("prediction_market_settlements")
    op.drop_index("ix_prediction_market_fills_created", table_name="prediction_market_fills")
    op.drop_index("ix_prediction_market_fills_position", table_name="prediction_market_fills")
    op.drop_index("ix_prediction_market_fills_account", table_name="prediction_market_fills")
    op.drop_table("prediction_market_fills")
    op.drop_index("ix_prediction_market_positions_status", table_name="prediction_market_positions")
    op.drop_index("ix_prediction_market_positions_market", table_name="prediction_market_positions")
    op.drop_index("ix_prediction_market_positions_guild_user", table_name="prediction_market_positions")
    op.drop_index("ix_prediction_market_positions_account_status", table_name="prediction_market_positions")
    op.drop_table("prediction_market_positions")
    op.drop_index("ix_prediction_market_drafts_market", table_name="prediction_market_bet_drafts")
    op.drop_index("ix_prediction_market_drafts_guild_user", table_name="prediction_market_bet_drafts")
    op.drop_index("ix_prediction_market_drafts_account_status", table_name="prediction_market_bet_drafts")
    op.drop_table("prediction_market_bet_drafts")
    op.drop_index("ix_prediction_market_accounts_guild", table_name="prediction_market_paper_accounts")
    op.drop_table("prediction_market_paper_accounts")
