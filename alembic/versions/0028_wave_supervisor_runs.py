"""Persist Wave Supervisor runs.

Revision ID: 0028_wave_supervisor_runs
Revises: 0027_operator_outbox
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0028_wave_supervisor_runs"
down_revision = "0027_operator_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wave_supervisor_runs",
        sa.Column("run_id", sa.String(length=96), primary_key=True),
        sa.Column("owner_role", sa.String(length=64), nullable=False, server_default="scheduler"),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("classification_state", sa.String(length=64)),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("completed_at_ms", sa.BigInteger()),
        sa.Column("duration_ms", sa.BigInteger()),
        sa.Column("result_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("last_error", sa.Text()),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_wave_supervisor_runs_status_created",
        "wave_supervisor_runs",
        ["status", "created_at_ms"],
    )
    op.create_index(
        "ix_wave_supervisor_runs_state_created",
        "wave_supervisor_runs",
        ["classification_state", "created_at_ms"],
    )


def downgrade() -> None:
    op.drop_index("ix_wave_supervisor_runs_state_created", table_name="wave_supervisor_runs")
    op.drop_index("ix_wave_supervisor_runs_status_created", table_name="wave_supervisor_runs")
    op.drop_table("wave_supervisor_runs")
