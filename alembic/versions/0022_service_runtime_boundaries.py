"""Add service-role runtime boundary tables.

Revision ID: 0022_service_runtime_boundaries
Revises: 0021_newswire_publish_ledger
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0022_service_runtime_boundaries"
down_revision = "0021_newswire_publish_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "service_heartbeats",
        sa.Column("service_role", sa.String(length=64), nullable=False),
        sa.Column("instance_id", sa.String(length=96), nullable=False),
        sa.Column("hostname", sa.String(length=255)),
        sa.Column("pid", sa.Integer()),
        sa.Column("version", sa.String(length=64)),
        sa.Column("started_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="starting"),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("service_role", "instance_id"),
    )
    op.create_index("ix_service_heartbeats_role_updated", "service_heartbeats", ["service_role", "updated_at_ms"])
    op.create_index("ix_service_heartbeats_status_updated", "service_heartbeats", ["status", "updated_at_ms"])

    op.create_table(
        "consumer_offsets",
        sa.Column("consumer_name", sa.String(length=128), primary_key=True),
        sa.Column("source_table", sa.String(length=128), nullable=False),
        sa.Column("last_event_id", sa.String(length=128)),
        sa.Column("last_event_ts_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "worker_commands",
        sa.Column("command_id", sa.String(length=96), primary_key=True),
        sa.Column("target_role", sa.String(length=64), nullable=False),
        sa.Column("command_type", sa.String(length=96), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("idempotency_key", sa.String(length=255)),
        sa.Column("requested_by", sa.String(length=128)),
        sa.Column("requested_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("claimed_by", sa.String(length=128)),
        sa.Column("claimed_at_ms", sa.BigInteger()),
        sa.Column("completed_at_ms", sa.BigInteger()),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("result_json", sa.JSON()),
        sa.Column("last_error", sa.Text()),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_worker_commands_role_status_requested", "worker_commands", ["target_role", "status", "requested_at_ms"])
    op.create_index("uq_worker_commands_idempotency_key", "worker_commands", ["idempotency_key"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_worker_commands_idempotency_key", table_name="worker_commands")
    op.drop_index("ix_worker_commands_role_status_requested", table_name="worker_commands")
    op.drop_table("worker_commands")
    op.drop_table("consumer_offsets")
    op.drop_index("ix_service_heartbeats_status_updated", table_name="service_heartbeats")
    op.drop_index("ix_service_heartbeats_role_updated", table_name="service_heartbeats")
    op.drop_table("service_heartbeats")
