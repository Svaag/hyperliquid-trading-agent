"""Add replay results for candidate diff validation.

Revision ID: 0010_replay_results
Revises: 0009_governance_authority
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0010_replay_results"
down_revision = "0009_governance_authority"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "replay_results",
        sa.Column("replay_id", sa.String(length=64), primary_key=True),
        sa.Column("proposal_id", sa.String(length=64)),
        sa.Column("decision_id", sa.String(length=96)),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("baseline_metrics_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("candidate_metrics_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("diffs_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("caveats_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_replay_results_proposal", "replay_results", ["proposal_id"])
    op.create_index("ix_replay_results_decision", "replay_results", ["decision_id"])
    op.create_index("ix_replay_results_created_at_ms", "replay_results", ["created_at_ms"])


def downgrade() -> None:
    op.drop_index("ix_replay_results_created_at_ms", table_name="replay_results")
    op.drop_index("ix_replay_results_decision", table_name="replay_results")
    op.drop_index("ix_replay_results_proposal", table_name="replay_results")
    op.drop_table("replay_results")
