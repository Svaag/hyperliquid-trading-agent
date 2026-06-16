"""Add governance audit, memory policy, risk, and review tables.

Revision ID: 0009_governance_authority
Revises: 0008_alpha_event_evaluations
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0009_governance_authority"
down_revision = "0008_alpha_event_evaluations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "config_versions",
        sa.Column("id", sa.String(length=96), primary_key=True),
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("version_hash", sa.String(length=128), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("code_version", sa.String(length=64)),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_config_versions_scope_active", "config_versions", ["scope", "active"])
    op.create_index("ix_config_versions_hash", "config_versions", ["version_hash"])

    op.create_table(
        "prompt_versions",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("prompt_name", sa.String(length=128), nullable=False),
        sa.Column("version_hash", sa.String(length=128), nullable=False),
        sa.Column("content_hash", sa.String(length=128), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("code_version", sa.String(length=64)),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_prompt_versions_name_active", "prompt_versions", ["prompt_name", "active"])
    op.create_index("ix_prompt_versions_hash", "prompt_versions", ["version_hash"])

    op.create_table(
        "decision_contexts",
        sa.Column("id", sa.String(length=96), primary_key=True),
        sa.Column("source_type", sa.String(length=64), nullable=False, server_default="unknown"),
        sa.Column("source_id", sa.String(length=96)),
        sa.Column("run_id", sa.String(length=64)),
        sa.Column("config_version_id", sa.String(length=96), nullable=False),
        sa.Column("risk_config_version_id", sa.String(length=96), nullable=False),
        sa.Column("model_route_version_id", sa.String(length=96)),
        sa.Column("prompt_version_ids_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("injected_memory_ids_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("market_snapshot_refs_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("data_freshness_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("code_version", sa.String(length=64)),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("context_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_decision_contexts_source", "decision_contexts", ["source_type", "source_id"])
    op.create_index("ix_decision_contexts_run_id", "decision_contexts", ["run_id"])
    op.create_index("ix_decision_contexts_created_at_ms", "decision_contexts", ["created_at_ms"])

    op.create_table(
        "risk_gateway_decisions",
        sa.Column("decision_id", sa.String(length=64), primary_key=True),
        sa.Column("intent_id", sa.String(length=96), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("violations_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("limits_snapshot_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("market_snapshot_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("portfolio_snapshot_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("config_version_id", sa.String(length=96)),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_risk_gateway_decisions_intent", "risk_gateway_decisions", ["intent_id"])
    op.create_index("ix_risk_gateway_decisions_created_at_ms", "risk_gateway_decisions", ["created_at_ms"])

    op.create_table(
        "memory_injection_events",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("run_id", sa.String(length=64)),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("context_type", sa.String(length=64), nullable=False),
        sa.Column("memory_ids_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("blocked_memory_ids_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("policy_decision_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_memory_injection_events_run_role", "memory_injection_events", ["run_id", "role"])
    op.create_index("ix_memory_injection_events_created_at_ms", "memory_injection_events", ["created_at_ms"])

    for table_name in ("shadow_role_lessons", "role_lessons"):
        op.add_column(table_name, sa.Column("memory_status", sa.String(length=64), nullable=False, server_default="validated_advisory"))
        op.add_column(table_name, sa.Column("allowed_contexts_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
        op.add_column(table_name, sa.Column("forbidden_contexts_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
        op.add_column(table_name, sa.Column("promotion_history_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
        op.add_column(table_name, sa.Column("rollback_target", sa.String(length=128)))

    op.create_table(
        "candidate_config_diffs",
        sa.Column("proposal_id", sa.String(length=64), primary_key=True),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("scope_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("change_type", sa.String(length=64), nullable=False),
        sa.Column("current_value_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("proposed_value_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("expected_effect", sa.Text(), nullable=False, server_default=""),
        sa.Column("known_risks_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("validation_required_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("risk_direction", sa.String(length=32), nullable=False),
        sa.Column("requires_human_approval", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("auto_apply_allowed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_candidate_config_diffs_status_created", "candidate_config_diffs", ["status", "created_at_ms"])
    op.create_index("ix_candidate_config_diffs_strategy", "candidate_config_diffs", ["strategy_id"])

    op.create_table(
        "shadow_comparisons",
        sa.Column("comparison_id", sa.String(length=64), primary_key=True),
        sa.Column("proposal_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("baseline_metrics_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("candidate_metrics_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("metric_deltas_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("recommendation", sa.String(length=64), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_shadow_comparisons_proposal", "shadow_comparisons", ["proposal_id"])
    op.create_index("ix_shadow_comparisons_created_at_ms", "shadow_comparisons", ["created_at_ms"])

    op.create_table(
        "rollback_plans",
        sa.Column("rollback_plan_id", sa.String(length=64), primary_key=True),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=96), nullable=False),
        sa.Column("previous_version_id", sa.String(length=128), nullable=False),
        sa.Column("rollback_steps_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("verification_steps_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("owner", sa.String(length=128), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "review_packets",
        sa.Column("review_packet_id", sa.String(length=64), primary_key=True),
        sa.Column("proposal_id", sa.String(length=64), nullable=False),
        sa.Column("evidence_links_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("affected_strategies_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("affected_symbols_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("affected_venues_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("risk_direction", sa.String(length=32), nullable=False),
        sa.Column("expected_effect", sa.Text(), nullable=False, server_default=""),
        sa.Column("known_risks_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("replay_results_json", sa.JSON()),
        sa.Column("shadow_results_json", sa.JSON()),
        sa.Column("reviewer_findings_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("approval_requirements_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("rollback_plan_id", sa.String(length=64), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_review_packets_proposal", "review_packets", ["proposal_id"])

    op.create_table(
        "promotion_decisions",
        sa.Column("decision_id", sa.String(length=64), primary_key=True),
        sa.Column("proposal_id", sa.String(length=64), nullable=False),
        sa.Column("reviewer", sa.String(length=128), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("evidence_reviewed_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("tests_reviewed_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("proposer_actor", sa.String(length=128), nullable=False),
        sa.Column("approver_actor", sa.String(length=128), nullable=False),
        sa.Column("change_control_id", sa.String(length=128), nullable=False),
        sa.Column("approved_contexts_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("rollback_plan_id", sa.String(length=64), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_promotion_decisions_proposal", "promotion_decisions", ["proposal_id"])

    for column in (
        sa.Column("strategy_id", sa.String(length=64), nullable=False, server_default="autonomy_v1"),
        sa.Column("change_type", sa.String(length=64), nullable=False, server_default="proposal"),
        sa.Column("risk_direction", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column("requires_human_approval", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("validation_required_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("known_risks_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("review_packet_id", sa.String(length=64)),
        sa.Column("candidate_diff_status", sa.String(length=32), nullable=False, server_default="proposed"),
    ):
        op.add_column("tuning_proposals", column)


def downgrade() -> None:
    for column_name in (
        "candidate_diff_status",
        "review_packet_id",
        "known_risks_json",
        "validation_required_json",
        "requires_human_approval",
        "risk_direction",
        "change_type",
        "strategy_id",
    ):
        op.drop_column("tuning_proposals", column_name)

    op.drop_index("ix_promotion_decisions_proposal", table_name="promotion_decisions")
    op.drop_table("promotion_decisions")
    op.drop_index("ix_review_packets_proposal", table_name="review_packets")
    op.drop_table("review_packets")
    op.drop_table("rollback_plans")
    op.drop_index("ix_shadow_comparisons_created_at_ms", table_name="shadow_comparisons")
    op.drop_index("ix_shadow_comparisons_proposal", table_name="shadow_comparisons")
    op.drop_table("shadow_comparisons")
    op.drop_index("ix_candidate_config_diffs_strategy", table_name="candidate_config_diffs")
    op.drop_index("ix_candidate_config_diffs_status_created", table_name="candidate_config_diffs")
    op.drop_table("candidate_config_diffs")

    for table_name in ("role_lessons", "shadow_role_lessons"):
        op.drop_column(table_name, "rollback_target")
        op.drop_column(table_name, "promotion_history_json")
        op.drop_column(table_name, "forbidden_contexts_json")
        op.drop_column(table_name, "allowed_contexts_json")
        op.drop_column(table_name, "memory_status")

    op.drop_index("ix_memory_injection_events_created_at_ms", table_name="memory_injection_events")
    op.drop_index("ix_memory_injection_events_run_role", table_name="memory_injection_events")
    op.drop_table("memory_injection_events")
    op.drop_index("ix_risk_gateway_decisions_created_at_ms", table_name="risk_gateway_decisions")
    op.drop_index("ix_risk_gateway_decisions_intent", table_name="risk_gateway_decisions")
    op.drop_table("risk_gateway_decisions")
    op.drop_index("ix_decision_contexts_created_at_ms", table_name="decision_contexts")
    op.drop_index("ix_decision_contexts_run_id", table_name="decision_contexts")
    op.drop_index("ix_decision_contexts_source", table_name="decision_contexts")
    op.drop_table("decision_contexts")
    op.drop_index("ix_prompt_versions_hash", table_name="prompt_versions")
    op.drop_index("ix_prompt_versions_name_active", table_name="prompt_versions")
    op.drop_table("prompt_versions")
    op.drop_index("ix_config_versions_hash", table_name="config_versions")
    op.drop_index("ix_config_versions_scope_active", table_name="config_versions")
    op.drop_table("config_versions")
