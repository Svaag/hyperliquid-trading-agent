from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002_high_stakes_decisions"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "decision_runs",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("route", sa.JSON(), nullable=False),
        sa.Column("selected_roles", sa.JSON(), nullable=False),
        sa.Column("context_snapshot", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("round_count", sa.Integer(), nullable=False),
        sa.Column("final_summary", sa.Text(), nullable=False),
        sa.Column("proposal_id", sa.String(length=64)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "decision_role_outputs",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("run_id", sa.String(length=64), sa.ForeignKey("decision_runs.id"), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("round_index", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(length=255)),
        sa.Column("provider", sa.String(length=64)),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("output_json", sa.JSON(), nullable=False),
        sa.Column("raw_content", sa.Text(), nullable=False),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "decision_state_snapshots",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("run_id", sa.String(length=64), sa.ForeignKey("decision_runs.id"), nullable=False),
        sa.Column("round_index", sa.Integer(), nullable=False),
        sa.Column("node", sa.String(length=128), nullable=False),
        sa.Column("state_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "trade_proposals",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("run_id", sa.String(length=64), sa.ForeignKey("decision_runs.id")),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("coin", sa.String(length=64)),
        sa.Column("side", sa.String(length=16)),
        sa.Column("proposal_json", sa.JSON(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    for table in ["trade_proposals", "decision_state_snapshots", "decision_role_outputs", "decision_runs"]:
        op.drop_table(table)
