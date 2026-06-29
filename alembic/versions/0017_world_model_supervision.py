"""Add world-model supervision and calibration tables.

Revision ID: 0017_world_model_supervision
Revises: 0016_world_model
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0017_world_model_supervision"
down_revision = "0016_world_model"
branch_labels = None
depends_on = None


def _json_default(value: str) -> sa.TextClause:
    return sa.text(f"'{value}'")


def upgrade() -> None:
    op.create_table(
        "world_model_annotations",
        sa.Column("annotation_id", sa.String(length=128), primary_key=True),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("actor_id", sa.String(length=128)),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_world_model_annotations_target", "world_model_annotations", ["target_type", "target_id"])
    op.create_index("ix_world_model_annotations_created", "world_model_annotations", ["created_at_ms"])

    op.create_table(
        "world_model_outcomes",
        sa.Column("outcome_id", sa.String(length=128), primary_key=True),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("outcome", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=64)),
        sa.Column("horizon", sa.String(length=64)),
        sa.Column("realized_value", sa.Float()),
        sa.Column("confidence_delta", sa.Float(), nullable=False, server_default=sa.text("0.05")),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_world_model_outcomes_target", "world_model_outcomes", ["target_type", "target_id"])
    op.create_index("ix_world_model_outcomes_created", "world_model_outcomes", ["created_at_ms"])

    op.create_table(
        "prediction_market_calibrations",
        sa.Column("calibration_id", sa.String(length=128), primary_key=True),
        sa.Column("signal_id", sa.String(length=128), nullable=False),
        sa.Column("venue", sa.String(length=64), nullable=False),
        sa.Column("market_id", sa.String(length=128), nullable=False),
        sa.Column("implied_probability", sa.Float()),
        sa.Column("realized_outcome", sa.Float()),
        sa.Column("brier_score", sa.Float()),
        sa.Column("settled_at_ms", sa.BigInteger()),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_prediction_market_calibrations_signal", "prediction_market_calibrations", ["signal_id"])
    op.create_index("ix_prediction_market_calibrations_venue_market", "prediction_market_calibrations", ["venue", "market_id"])


def downgrade() -> None:
    op.drop_index("ix_prediction_market_calibrations_venue_market", table_name="prediction_market_calibrations")
    op.drop_index("ix_prediction_market_calibrations_signal", table_name="prediction_market_calibrations")
    op.drop_table("prediction_market_calibrations")
    op.drop_index("ix_world_model_outcomes_created", table_name="world_model_outcomes")
    op.drop_index("ix_world_model_outcomes_target", table_name="world_model_outcomes")
    op.drop_table("world_model_outcomes")
    op.drop_index("ix_world_model_annotations_created", table_name="world_model_annotations")
    op.drop_index("ix_world_model_annotations_target", table_name="world_model_annotations")
    op.drop_table("world_model_annotations")
