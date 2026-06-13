from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_threads",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("discord_guild_id", sa.String(length=32)),
        sa.Column("discord_channel_id", sa.String(length=32)),
        sa.Column("discord_thread_id", sa.String(length=32)),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("thread_id", sa.String(length=64), sa.ForeignKey("conversation_threads.id"), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("discord_user_id", sa.String(length=32)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("output_json", sa.JSON(), nullable=False),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "cache_items",
        sa.Column("key", sa.String(length=255), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "news_items",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("source", sa.String(length=255), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "paper_trade_ideas",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("discord_user_id", sa.String(length=32)),
        sa.Column("coin", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("thesis", sa.Text(), nullable=False),
        sa.Column("plan", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "paper_trade_snapshots",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("idea_id", sa.String(length=64), sa.ForeignKey("paper_trade_ideas.id"), nullable=False),
        sa.Column("market_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    for table in [
        "paper_trade_snapshots",
        "paper_trade_ideas",
        "news_items",
        "cache_items",
        "tool_calls",
        "audit_events",
        "conversation_messages",
        "conversation_threads",
    ]:
        op.drop_table(table)
