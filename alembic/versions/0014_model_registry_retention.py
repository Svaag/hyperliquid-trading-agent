"""Add engine model registry and retention audit tables.

Revision ID: 0014_model_registry_retention
Revises: 0013_exec_pos_recon
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0014_model_registry_retention"
down_revision = "0013_exec_pos_recon"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_versions",
        sa.Column("model_version_id", sa.String(length=128), primary_key=True),
        sa.Column("model_type", sa.String(length=96), nullable=False),
        sa.Column("artifact_uri", sa.Text(), nullable=False),
        sa.Column("training_data_hash", sa.String(length=128), nullable=False),
        sa.Column("feature_schema_hash", sa.String(length=128), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("approved_by", sa.String(length=128)),
        sa.Column("approved_at_ms", sa.BigInteger()),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_model_versions_status", "model_versions", ["status"])
    op.create_index("ix_model_versions_model_type", "model_versions", ["model_type"])

    op.create_table(
        "model_training_runs",
        sa.Column("training_run_id", sa.String(length=128), primary_key=True),
        sa.Column("model_version_id", sa.String(length=128)),
        sa.Column("model_type", sa.String(length=96), nullable=False),
        sa.Column("dataset_start_ms", sa.BigInteger(), nullable=False),
        sa.Column("dataset_end_ms", sa.BigInteger(), nullable=False),
        sa.Column("training_data_hash", sa.String(length=128), nullable=False),
        sa.Column("feature_schema_hash", sa.String(length=128), nullable=False),
        sa.Column("code_version", sa.String(length=64)),
        sa.Column("metrics_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("artifact_uri", sa.Text()),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("completed_at_ms", sa.BigInteger()),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_model_training_runs_created", "model_training_runs", ["created_at_ms"])

    op.create_table(
        "feature_schema_versions",
        sa.Column("feature_schema_version_id", sa.String(length=128), primary_key=True),
        sa.Column("schema_hash", sa.String(length=128), nullable=False),
        sa.Column("feature_names_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("feature_definitions_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "retention_runs",
        sa.Column("retention_run_id", sa.String(length=96), primary_key=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("completed_at_ms", sa.BigInteger()),
        sa.Column("deleted_counts_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("rollup_counts_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("caveats_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_retention_runs_started", "retention_runs", ["started_at_ms"])


def downgrade() -> None:
    op.drop_index("ix_retention_runs_started", table_name="retention_runs")
    op.drop_table("retention_runs")
    op.drop_table("feature_schema_versions")
    op.drop_index("ix_model_training_runs_created", table_name="model_training_runs")
    op.drop_table("model_training_runs")
    op.drop_index("ix_model_versions_model_type", table_name="model_versions")
    op.drop_index("ix_model_versions_status", table_name="model_versions")
    op.drop_table("model_versions")
