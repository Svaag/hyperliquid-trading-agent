"""Add Newswire policy loop tables.

Revision ID: 0023_newswire_policy_loop
Revises: 0022_service_runtime_boundaries
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0023_newswire_policy_loop"
down_revision = "0022_service_runtime_boundaries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "newswire_decisions",
        sa.Column("decision_id", sa.String(length=64), primary_key=True),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=96), nullable=False),
        sa.Column("policy_type", sa.String(length=32), nullable=False, server_default="static"),
        sa.Column("raw_event_hash", sa.String(length=64), nullable=False),
        sa.Column("cluster_id", sa.String(length=96)),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False, server_default="unknown"),
        sa.Column("symbols_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("asset_class", sa.String(length=16), nullable=False),
        sa.Column("features_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("scores_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("newswire_action", sa.String(length=32), nullable=False),
        sa.Column("engine_action", sa.String(length=32), nullable=False),
        sa.Column("market_impact_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("quality_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("relevance_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("novelty_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("urgency_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("source_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("direction_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("direction_confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("risk_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("reasons_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("penalties_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_newswire_decisions_event", "newswire_decisions", ["event_id"])
    op.create_index("ix_newswire_decisions_policy", "newswire_decisions", ["policy_version"])
    op.create_index("ix_newswire_decisions_created_at_ms", "newswire_decisions", ["created_at_ms"])

    op.create_table(
        "newswire_evals",
        sa.Column("eval_id", sa.String(length=64), primary_key=True),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("decision_id", sa.String(length=64)),
        sa.Column("policy_version", sa.String(length=96)),
        sa.Column("evaluator_type", sa.String(length=32), nullable=False),
        sa.Column("evaluator_id", sa.String(length=128)),
        sa.Column("label_type", sa.String(length=64), nullable=False),
        sa.Column("label_value_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1"),
        sa.Column("reason", sa.String(length=128)),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_newswire_evals_event", "newswire_evals", ["event_id"])
    op.create_index("ix_newswire_evals_decision", "newswire_evals", ["decision_id"])
    op.create_index("ix_newswire_evals_created_at_ms", "newswire_evals", ["created_at_ms"])

    op.create_table(
        "newswire_rewards",
        sa.Column("reward_id", sa.String(length=64), primary_key=True),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("decision_id", sa.String(length=64)),
        sa.Column("policy_version", sa.String(length=96), nullable=False),
        sa.Column("total_reward", sa.Float(), nullable=False, server_default="0"),
        sa.Column("reward_components_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("labels_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("reasons_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_newswire_rewards_event", "newswire_rewards", ["event_id"])
    op.create_index("ix_newswire_rewards_policy", "newswire_rewards", ["policy_version"])
    op.create_index("ix_newswire_rewards_created_at_ms", "newswire_rewards", ["created_at_ms"])

    op.create_table(
        "newswire_source_reputation",
        sa.Column("reputation_id", sa.String(length=96), primary_key=True),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False, server_default="unknown"),
        sa.Column("event_type", sa.String(length=32), nullable=False, server_default="all"),
        sa.Column("learned_reputation", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("false_positive_rate", sa.Float(), nullable=False, server_default="0"),
        sa.Column("duplicate_rate", sa.Float(), nullable=False, server_default="0"),
        sa.Column("correction_rate", sa.Float(), nullable=False, server_default="0"),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_newswire_source_reputation_source", "newswire_source_reputation", ["source_id", "event_type"])

    op.create_table(
        "newswire_policy_versions",
        sa.Column("policy_version", sa.String(length=96), primary_key=True),
        sa.Column("policy_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("params_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("model_uri", sa.Text()),
        sa.Column("replay_metrics_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("canary_metrics_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("promoted_at_ms", sa.BigInteger()),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_newswire_policy_versions_status", "newswire_policy_versions", ["status"])
    op.create_index("ix_newswire_policy_versions_created_at_ms", "newswire_policy_versions", ["created_at_ms"])


def downgrade() -> None:
    op.drop_index("ix_newswire_policy_versions_created_at_ms", table_name="newswire_policy_versions")
    op.drop_index("ix_newswire_policy_versions_status", table_name="newswire_policy_versions")
    op.drop_table("newswire_policy_versions")
    op.drop_index("ix_newswire_source_reputation_source", table_name="newswire_source_reputation")
    op.drop_table("newswire_source_reputation")
    op.drop_index("ix_newswire_rewards_created_at_ms", table_name="newswire_rewards")
    op.drop_index("ix_newswire_rewards_policy", table_name="newswire_rewards")
    op.drop_index("ix_newswire_rewards_event", table_name="newswire_rewards")
    op.drop_table("newswire_rewards")
    op.drop_index("ix_newswire_evals_created_at_ms", table_name="newswire_evals")
    op.drop_index("ix_newswire_evals_decision", table_name="newswire_evals")
    op.drop_index("ix_newswire_evals_event", table_name="newswire_evals")
    op.drop_table("newswire_evals")
    op.drop_index("ix_newswire_decisions_created_at_ms", table_name="newswire_decisions")
    op.drop_index("ix_newswire_decisions_policy", table_name="newswire_decisions")
    op.drop_index("ix_newswire_decisions_event", table_name="newswire_decisions")
    op.drop_table("newswire_decisions")
