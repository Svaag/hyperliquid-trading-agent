"""Persist operational incident lifecycle state.

Revision ID: 0031_operational_incidents
Revises: 0030_canonical_market_universe
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0031_operational_incidents"
down_revision = "0030_canonical_market_universe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operational_incidents",
        sa.Column("incident_key", sa.String(length=255), primary_key=True),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("alert_type", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("severity", sa.String(length=16), nullable=False, server_default="warning"),
        sa.Column("opened_at_ms", sa.BigInteger()),
        sa.Column("last_seen_at_ms", sa.BigInteger()),
        sa.Column("resolved_at_ms", sa.BigInteger()),
        sa.Column("last_notified_at_ms", sa.BigInteger()),
        sa.Column("last_sample_id", sa.String(length=128)),
        sa.Column("bad_sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("good_sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("details_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_operational_incidents_source_state",
        "operational_incidents",
        ["source_type", "state"],
    )
    op.create_index(
        "ix_operational_incidents_alert_type",
        "operational_incidents",
        ["alert_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_operational_incidents_alert_type", table_name="operational_incidents")
    op.drop_index("ix_operational_incidents_source_state", table_name="operational_incidents")
    op.drop_table("operational_incidents")
