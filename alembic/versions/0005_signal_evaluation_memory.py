from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0005_signal_evaluation_memory"
down_revision = "0004_autonomous_loop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signal_evaluations",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("signal_id", sa.String(length=64), sa.ForeignKey("trade_signals.id"), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("signal_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("completed_at_ms", sa.BigInteger()),
        sa.Column("entry_px", sa.Float(), nullable=False),
        sa.Column("stop_px", sa.Float(), nullable=False),
        sa.Column("take_profit_px", sa.Float()),
        sa.Column("signal_score", sa.Float(), nullable=False),
        sa.Column("signal_confidence", sa.Float(), nullable=False),
        sa.Column("signal_status_at_eval_start", sa.String(length=32), nullable=False),
        sa.Column("first_price", sa.Float()),
        sa.Column("latest_price", sa.Float()),
        sa.Column("latest_price_at_ms", sa.BigInteger()),
        sa.Column("max_favorable_price", sa.Float()),
        sa.Column("max_adverse_price", sa.Float()),
        sa.Column("max_favorable_bps", sa.Float()),
        sa.Column("max_adverse_bps", sa.Float()),
        sa.Column("max_favorable_r", sa.Float()),
        sa.Column("max_adverse_r", sa.Float()),
        sa.Column("stop_hit", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("stop_hit_at_ms", sa.BigInteger()),
        sa.Column("take_profit_hit", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("take_profit_hit_at_ms", sa.BigInteger()),
        sa.Column("terminal_outcome", sa.String(length=64), nullable=False),
        sa.Column("realized_or_marked_r", sa.Float()),
        sa.Column("opportunity_cost_r", sa.Float()),
        sa.Column("approved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("rejected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("paper_ordered", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("paper_position_id", sa.String(length=64)),
        sa.Column("feature_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("evidence_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("market_regime", sa.String(length=64), nullable=False, server_default="unknown"),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_index("uq_signal_evaluations_signal_id", "signal_evaluations", ["signal_id"], unique=True)
    op.create_index("ix_signal_evaluations_status_symbol", "signal_evaluations", ["status", "symbol"])
    op.create_index("ix_signal_evaluations_symbol_created_at_ms", "signal_evaluations", ["symbol", "created_at_ms"])

    op.create_table(
        "signal_evaluation_marks",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("evaluation_id", sa.String(length=64), sa.ForeignKey("signal_evaluations.id"), nullable=False),
        sa.Column("signal_id", sa.String(length=64), sa.ForeignKey("trade_signals.id"), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("horizon", sa.String(length=32), nullable=False),
        sa.Column("due_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("marked_at_ms", sa.BigInteger()),
        sa.Column("price", sa.Float()),
        sa.Column("direction_adjusted_return_bps", sa.Float()),
        sa.Column("r_multiple", sa.Float()),
        sa.Column("mfe_bps_until_mark", sa.Float()),
        sa.Column("mae_bps_until_mark", sa.Float()),
        sa.Column("mfe_r_until_mark", sa.Float()),
        sa.Column("mae_r_until_mark", sa.Float()),
        sa.Column("stop_hit_before_mark", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("take_profit_hit_before_mark", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("uq_signal_evaluation_marks_signal_horizon", "signal_evaluation_marks", ["signal_id", "horizon"], unique=True)
    op.create_index("ix_signal_evaluation_marks_eval", "signal_evaluation_marks", ["evaluation_id"])
    op.create_index("ix_signal_evaluation_marks_due_status", "signal_evaluation_marks", ["status", "due_at_ms"])
    op.create_index("ix_signal_evaluation_marks_symbol_due", "signal_evaluation_marks", ["symbol", "due_at_ms"])

    op.create_table(
        "memory_observations",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=64)),
        sa.Column("symbol", sa.String(length=64)),
        sa.Column("signal_type", sa.String(length=64)),
        sa.Column("market_regime", sa.String(length=64)),
        sa.Column("observation", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_memory_observations_source", "memory_observations", ["source_type", "source_id"])
    op.create_index("ix_memory_observations_role_symbol", "memory_observations", ["role", "symbol"])
    op.create_index("ix_memory_observations_created_at_ms", "memory_observations", ["created_at_ms"])

    op.create_table(
        "candidate_lessons",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("lesson_type", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=64)),
        sa.Column("scope_json", sa.JSON(), nullable=False),
        sa.Column("claim", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("source_observation_ids_json", sa.JSON(), nullable=False),
        sa.Column("source_run_ids_json", sa.JSON(), nullable=False),
        sa.Column("source_signal_ids_json", sa.JSON(), nullable=False),
        sa.Column("sample_size", sa.Integer(), nullable=False),
        sa.Column("counterexamples_json", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("expected_future_behavior_change", sa.Text(), nullable=False),
        sa.Column("strategy_affecting", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("risk_affecting", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("execution_affecting", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("capital_allocation_affecting", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("expires_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_candidate_lessons_status_expires", "candidate_lessons", ["status", "expires_at_ms"])
    op.create_index("ix_candidate_lessons_role_type", "candidate_lessons", ["role", "lesson_type"])

    for table_name in ("shadow_role_lessons", "role_lessons"):
        op.create_table(
            table_name,
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("role", sa.String(length=64), nullable=False),
            sa.Column("lesson_type", sa.String(length=64), nullable=False),
            sa.Column("scope_json", sa.JSON(), nullable=False),
            sa.Column("claim", sa.Text(), nullable=False),
            sa.Column("instruction", sa.Text(), nullable=False),
            sa.Column("evidence_json", sa.JSON(), nullable=False),
            sa.Column("source_candidate_id", sa.String(length=64)),
            sa.Column("source_run_ids_json", sa.JSON(), nullable=False),
            sa.Column("source_signal_ids_json", sa.JSON(), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("sample_size", sa.Integer(), nullable=False),
            sa.Column("counterexamples_json", sa.JSON(), nullable=False),
            sa.Column("validation_status", sa.String(length=64), nullable=False),
            sa.Column("strategy_affecting", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("risk_affecting", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("execution_affecting", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("capital_allocation_affecting", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
            sa.Column("activated_at_ms", sa.BigInteger()),
            sa.Column("expires_at_ms", sa.BigInteger(), nullable=False),
            sa.Column("last_revalidated_at_ms", sa.BigInteger()),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index(f"ix_{table_name}_role_status", table_name, ["role", "validation_status"])
        op.create_index(f"ix_{table_name}_expires", table_name, ["expires_at_ms"])

    op.create_table(
        "operator_output_lessons",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("scope_json", sa.JSON(), nullable=False),
        sa.Column("issue_or_pattern", sa.Text(), nullable=False),
        sa.Column("preferred_behavior", sa.Text(), nullable=False),
        sa.Column("bad_examples_json", sa.JSON(), nullable=False),
        sa.Column("good_examples_json", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("sample_size", sa.Integer(), nullable=False),
        sa.Column("validation_status", sa.String(length=64), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("expires_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_operator_output_lessons_status", "operator_output_lessons", ["validation_status"])
    op.create_index("ix_operator_output_lessons_expires", "operator_output_lessons", ["expires_at_ms"])

    op.create_table(
        "operator_feedback",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=128)),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("rating", sa.String(length=32), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_operator_feedback_target", "operator_feedback", ["target_type", "target_id"])
    op.create_index("ix_operator_feedback_created_at_ms", "operator_feedback", ["created_at_ms"])

    op.create_table(
        "tuning_proposals",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("proposal_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("affected_scope_json", sa.JSON(), nullable=False),
        sa.Column("current_behavior_json", sa.JSON(), nullable=False),
        sa.Column("proposed_diff_json", sa.JSON(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("source_lesson_ids_json", sa.JSON(), nullable=False),
        sa.Column("source_signal_ids_json", sa.JSON(), nullable=False),
        sa.Column("expected_impact", sa.Text(), nullable=False),
        sa.Column("risk_assessment", sa.Text(), nullable=False),
        sa.Column("blast_radius", sa.String(length=32), nullable=False),
        sa.Column("rollback_plan", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("sample_size", sa.Integer(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("expires_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("evaluation_window", sa.String(length=64), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_tuning_proposals_status_expires", "tuning_proposals", ["status", "expires_at_ms"])
    op.create_index("ix_tuning_proposals_type", "tuning_proposals", ["proposal_type"])

    op.create_table(
        "token_capital_snapshots",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("timestamp_ms", sa.BigInteger(), nullable=False),
        sa.Column("window", sa.String(length=32), nullable=False),
        sa.Column("total_score", sa.Float(), nullable=False),
        sa.Column("risk_adjusted_performance_score", sa.Float(), nullable=False),
        sa.Column("signal_quality_score", sa.Float(), nullable=False),
        sa.Column("memory_compounding_score", sa.Float(), nullable=False),
        sa.Column("risk_discipline_score", sa.Float(), nullable=False),
        sa.Column("operator_communication_score", sa.Float(), nullable=False),
        sa.Column("reliability_score", sa.Float(), nullable=False),
        sa.Column("hard_gate_penalties_json", sa.JSON(), nullable=False),
        sa.Column("component_details_json", sa.JSON(), nullable=False),
        sa.Column("created_from_report_id", sa.String(length=64)),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_token_capital_snapshots_window_timestamp", "token_capital_snapshots", ["window", "timestamp_ms"])

    for table_name, key_name in (("daily_reports", "report_date"), ("weekly_reports", "week_key")):
        op.create_table(
            table_name,
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column(key_name, sa.String(length=32), nullable=False),
            sa.Column("period_start_ms", sa.BigInteger(), nullable=False),
            sa.Column("period_end_ms", sa.BigInteger(), nullable=False),
            sa.Column("generated_at_ms", sa.BigInteger(), nullable=False),
            sa.Column("token_capital_score", sa.Float()),
            sa.Column("summary", sa.Text(), nullable=False),
            sa.Column("report_json", sa.JSON(), nullable=False),
            sa.Column("discord_channel_id", sa.String(length=64)),
            sa.Column("discord_message_id", sa.String(length=64)),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index(f"uq_{table_name}_{key_name}", table_name, [key_name], unique=True)
        op.create_index(f"ix_{table_name}_period", table_name, ["period_start_ms", "period_end_ms"])


def downgrade() -> None:
    for table_name, key_name in (("weekly_reports", "week_key"), ("daily_reports", "report_date")):
        op.drop_index(f"ix_{table_name}_period", table_name=table_name)
        op.drop_index(f"uq_{table_name}_{key_name}", table_name=table_name)
        op.drop_table(table_name)

    op.drop_index("ix_token_capital_snapshots_window_timestamp", table_name="token_capital_snapshots")
    op.drop_table("token_capital_snapshots")

    op.drop_index("ix_tuning_proposals_type", table_name="tuning_proposals")
    op.drop_index("ix_tuning_proposals_status_expires", table_name="tuning_proposals")
    op.drop_table("tuning_proposals")

    op.drop_index("ix_operator_feedback_created_at_ms", table_name="operator_feedback")
    op.drop_index("ix_operator_feedback_target", table_name="operator_feedback")
    op.drop_table("operator_feedback")

    op.drop_index("ix_operator_output_lessons_expires", table_name="operator_output_lessons")
    op.drop_index("ix_operator_output_lessons_status", table_name="operator_output_lessons")
    op.drop_table("operator_output_lessons")

    for table_name in ("role_lessons", "shadow_role_lessons"):
        op.drop_index(f"ix_{table_name}_expires", table_name=table_name)
        op.drop_index(f"ix_{table_name}_role_status", table_name=table_name)
        op.drop_table(table_name)

    op.drop_index("ix_candidate_lessons_role_type", table_name="candidate_lessons")
    op.drop_index("ix_candidate_lessons_status_expires", table_name="candidate_lessons")
    op.drop_table("candidate_lessons")

    op.drop_index("ix_memory_observations_created_at_ms", table_name="memory_observations")
    op.drop_index("ix_memory_observations_role_symbol", table_name="memory_observations")
    op.drop_index("ix_memory_observations_source", table_name="memory_observations")
    op.drop_table("memory_observations")

    op.drop_index("ix_signal_evaluation_marks_symbol_due", table_name="signal_evaluation_marks")
    op.drop_index("ix_signal_evaluation_marks_due_status", table_name="signal_evaluation_marks")
    op.drop_index("ix_signal_evaluation_marks_eval", table_name="signal_evaluation_marks")
    op.drop_index("uq_signal_evaluation_marks_signal_horizon", table_name="signal_evaluation_marks")
    op.drop_table("signal_evaluation_marks")

    op.drop_index("ix_signal_evaluations_symbol_created_at_ms", table_name="signal_evaluations")
    op.drop_index("ix_signal_evaluations_status_symbol", table_name="signal_evaluations")
    op.drop_index("uq_signal_evaluations_signal_id", table_name="signal_evaluations")
    op.drop_table("signal_evaluations")
