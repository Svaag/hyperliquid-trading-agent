from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0003_position_tracking"
down_revision = "0002_high_stakes_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "position_trackers",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("proposal_id", sa.String(length=64), sa.ForeignKey("trade_proposals.id")),
        sa.Column("run_id", sa.String(length=64), sa.ForeignKey("decision_runs.id")),
        sa.Column("source", sa.String(length=64), nullable=False, server_default="auto_high_stakes"),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("coin", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("entry_px", sa.Float(), nullable=False),
        sa.Column("stop_px", sa.Float(), nullable=False),
        sa.Column("take_profit_px", sa.Float()),
        sa.Column("current_px", sa.Float()),
        sa.Column("last_px", sa.Float()),
        sa.Column("last_price_at_ms", sa.BigInteger()),
        sa.Column("price_source", sa.String(length=32), nullable=False, server_default="allMids"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("discord_guild_id", sa.String(length=32)),
        sa.Column("discord_channel_id", sa.String(length=32)),
        sa.Column("discord_thread_id", sa.String(length=32)),
        sa.Column("discord_user_id", sa.String(length=32)),
        sa.Column("plan_json", sa.JSON(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_position_trackers_status_coin", "position_trackers", ["status", "coin"])
    op.create_index("ix_position_trackers_discord_thread", "position_trackers", ["discord_thread_id"])
    op.create_index("ix_position_trackers_proposal_id", "position_trackers", ["proposal_id"])

    op.create_table(
        "tracked_levels",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tracker_id", sa.String(length=64), sa.ForeignKey("position_trackers.id"), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("terminal", sa.Boolean(), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("armed", sa.Boolean(), nullable=False),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rearm_band_bps", sa.Float(), nullable=False, server_default="10"),
        sa.Column("last_triggered_at", sa.DateTime(timezone=True)),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_tracked_levels_tracker_id", "tracked_levels", ["tracker_id"])
    op.create_index("ix_tracked_levels_kind", "tracked_levels", ["kind"])

    op.create_table(
        "tracking_events",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tracker_id", sa.String(length=64), sa.ForeignKey("position_trackers.id"), nullable=False),
        sa.Column("level_id", sa.String(length=64), sa.ForeignKey("tracked_levels.id")),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("coin", sa.String(length=64), nullable=False),
        sa.Column("price", sa.Float()),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("alert_destination", sa.String(length=64)),
        sa.Column("alert_status", sa.String(length=32)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_tracking_events_tracker_id_created_at", "tracking_events", ["tracker_id", "created_at"])
    op.create_index("ix_tracking_events_event_type", "tracking_events", ["event_type"])


def downgrade() -> None:
    op.drop_index("ix_tracking_events_event_type", table_name="tracking_events")
    op.drop_index("ix_tracking_events_tracker_id_created_at", table_name="tracking_events")
    op.drop_table("tracking_events")
    op.drop_index("ix_tracked_levels_kind", table_name="tracked_levels")
    op.drop_index("ix_tracked_levels_tracker_id", table_name="tracked_levels")
    op.drop_table("tracked_levels")
    op.drop_index("ix_position_trackers_proposal_id", table_name="position_trackers")
    op.drop_index("ix_position_trackers_discord_thread", table_name="position_trackers")
    op.drop_index("ix_position_trackers_status_coin", table_name="position_trackers")
    op.drop_table("position_trackers")
