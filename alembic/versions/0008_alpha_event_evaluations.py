"""Add alpha event evaluations and signal metadata.

Revision ID: 0008_alpha_event_evaluations
Revises: 0007_tradfi
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0008_alpha_event_evaluations"
down_revision = "0007_tradfi"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trade_signals", sa.Column("asset_class", sa.String(length=32), nullable=False, server_default="crypto")
    )
    op.add_column(
        "trade_signals", sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'"))
    )

    op.create_table(
        "alpha_event_evaluations",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("event_source", sa.String(length=64), nullable=False, server_default="unknown"),
        sa.Column("provider", sa.String(length=64), nullable=False, server_default="unknown"),
        sa.Column("event_type", sa.String(length=64), nullable=False, server_default="headline"),
        sa.Column("asset_class", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False, server_default="neutral"),
        sa.Column("sentiment", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="open"),
        sa.Column("terminal_outcome", sa.String(length=64), nullable=False, server_default="open"),
        sa.Column("received_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("completed_at_ms", sa.BigInteger()),
        sa.Column("headline", sa.Text(), nullable=False, server_default=""),
        sa.Column("url", sa.Text()),
        sa.Column("importance_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("source_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("urgency", sa.String(length=32), nullable=False, server_default="normal"),
        sa.Column("freshness", sa.String(length=32), nullable=False, server_default="fresh"),
        sa.Column("market_regime", sa.String(length=64), nullable=False, server_default="unknown"),
        sa.Column("reference_price", sa.Float()),
        sa.Column("reference_price_at_ms", sa.BigInteger()),
        sa.Column("latest_price", sa.Float()),
        sa.Column("latest_price_at_ms", sa.BigInteger()),
        sa.Column("max_favorable_price", sa.Float()),
        sa.Column("max_adverse_price", sa.Float()),
        sa.Column("max_favorable_bps", sa.Float()),
        sa.Column("max_adverse_bps", sa.Float()),
        sa.Column("max_abs_move_bps", sa.Float()),
        sa.Column("realized_or_marked_bps", sa.Float()),
        sa.Column("linked_signal_ids_json", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "uq_alpha_event_evaluations_event_symbol", "alpha_event_evaluations", ["event_id", "symbol"], unique=True
    )
    op.create_index("ix_alpha_event_evaluations_status_symbol", "alpha_event_evaluations", ["status", "symbol"])
    op.create_index("ix_alpha_event_evaluations_source_type", "alpha_event_evaluations", ["event_source", "event_type"])
    op.create_index("ix_alpha_event_evaluations_received_at_ms", "alpha_event_evaluations", ["received_at_ms"])

    op.create_table(
        "alpha_event_evaluation_marks",
        sa.Column("id", sa.String(length=96), primary_key=True),
        sa.Column("evaluation_id", sa.String(length=64), sa.ForeignKey("alpha_event_evaluations.id"), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("asset_class", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column("horizon", sa.String(length=32), nullable=False),
        sa.Column("due_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("marked_at_ms", sa.BigInteger()),
        sa.Column("price", sa.Float()),
        sa.Column("direction_adjusted_return_bps", sa.Float()),
        sa.Column("abs_move_bps", sa.Float()),
        sa.Column("max_favorable_bps_until_mark", sa.Float()),
        sa.Column("max_adverse_bps_until_mark", sa.Float()),
        sa.Column("max_abs_move_bps_until_mark", sa.Float()),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "uq_alpha_event_eval_marks_event_symbol_horizon",
        "alpha_event_evaluation_marks",
        ["event_id", "symbol", "horizon"],
        unique=True,
    )
    op.create_index("ix_alpha_event_eval_marks_eval", "alpha_event_evaluation_marks", ["evaluation_id"])
    op.create_index("ix_alpha_event_eval_marks_due_status", "alpha_event_evaluation_marks", ["status", "due_at_ms"])
    op.create_index("ix_alpha_event_eval_marks_symbol_due", "alpha_event_evaluation_marks", ["symbol", "due_at_ms"])


def downgrade() -> None:
    op.drop_index("ix_alpha_event_eval_marks_symbol_due", table_name="alpha_event_evaluation_marks")
    op.drop_index("ix_alpha_event_eval_marks_due_status", table_name="alpha_event_evaluation_marks")
    op.drop_index("ix_alpha_event_eval_marks_eval", table_name="alpha_event_evaluation_marks")
    op.drop_index("uq_alpha_event_eval_marks_event_symbol_horizon", table_name="alpha_event_evaluation_marks")
    op.drop_table("alpha_event_evaluation_marks")

    op.drop_index("ix_alpha_event_evaluations_received_at_ms", table_name="alpha_event_evaluations")
    op.drop_index("ix_alpha_event_evaluations_source_type", table_name="alpha_event_evaluations")
    op.drop_index("ix_alpha_event_evaluations_status_symbol", table_name="alpha_event_evaluations")
    op.drop_index("uq_alpha_event_evaluations_event_symbol", table_name="alpha_event_evaluations")
    op.drop_table("alpha_event_evaluations")

    op.drop_column("trade_signals", "metadata_json")
    op.drop_column("trade_signals", "asset_class")
