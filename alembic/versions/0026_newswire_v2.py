"""Newswire V2 canonical stories, durable delivery, and risk state.

Revision ID: 0026_newswire_v2
Revises: 0025_engine_ops_hardening
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0026_newswire_v2"
down_revision = "0025_engine_ops_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("newswire_events", sa.Column("story_id", sa.String(length=64)))
    op.add_column("newswire_events", sa.Column("story_revision", sa.Integer()))
    op.add_column("newswire_events", sa.Column("topics_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
    op.add_column("newswire_events", sa.Column("assessment_json", sa.JSON()))
    op.create_index("ix_newswire_events_story", "newswire_events", ["story_id", "story_revision"])

    op.create_table(
        "newswire_stories",
        sa.Column("story_id", sa.String(length=64), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("canonical_event_id", sa.String(length=64), nullable=False),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("url", sa.Text()),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("sources_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("providers_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("member_event_ids_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("symbols_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("topics_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("asset_class", sa.String(length=16), nullable=False, server_default="unknown"),
        sa.Column("event_type", sa.String(length=32), nullable=False, server_default="headline"),
        sa.Column("urgency", sa.String(length=16), nullable=False, server_default="normal"),
        sa.Column("sentiment", sa.String(length=16), nullable=False, server_default="unknown"),
        sa.Column("source_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("published_at_ms", sa.BigInteger()),
        sa.Column("first_seen_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("last_updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("source_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("independent_source_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("assessment_json", sa.JSON()),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_newswire_stories_updated", "newswire_stories", ["last_updated_at_ms"])
    op.create_index("ix_newswire_stories_status_updated", "newswire_stories", ["status", "last_updated_at_ms"])
    op.create_index("ix_newswire_stories_canonical_event", "newswire_stories", ["canonical_event_id"])

    op.create_table(
        "newswire_story_revisions",
        sa.Column("revision_id", sa.String(length=64), primary_key=True),
        sa.Column("story_id", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("update_type", sa.String(length=16), nullable=False),
        sa.Column("emitted_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("story_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("story_id", "revision", name="uq_newswire_story_revision"),
    )
    op.create_index("ix_newswire_story_revisions_emitted", "newswire_story_revisions", ["emitted_at_ms", "revision_id"])
    op.create_index("ix_newswire_story_revisions_story", "newswire_story_revisions", ["story_id", "revision"])

    op.create_table(
        "newswire_deliveries",
        sa.Column("delivery_id", sa.String(length=96), primary_key=True),
        sa.Column("destination", sa.String(length=32), nullable=False, server_default="discord"),
        sa.Column("channel_id", sa.String(length=64), nullable=False),
        sa.Column("story_id", sa.String(length=64), nullable=False),
        sa.Column("story_revision", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("scheduled_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("next_attempt_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("discord_message_id", sa.String(length=64)),
        sa.Column("posted_at_ms", sa.BigInteger()),
        sa.Column("last_error", sa.Text()),
        sa.Column("skip_reason", sa.String(length=64)),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("destination", "channel_id", "story_id", "story_revision", name="uq_newswire_delivery_story_revision"),
    )
    op.create_index("ix_newswire_deliveries_due", "newswire_deliveries", ["destination", "status", "next_attempt_at_ms"])
    op.create_index("ix_newswire_deliveries_story", "newswire_deliveries", ["story_id", "story_revision"])

    op.create_table(
        "newswire_risk_states",
        sa.Column("scope", sa.String(length=64), primary_key=True),
        sa.Column("mode", sa.String(length=24), nullable=False, server_default="neutral"),
        sa.Column("signed_pressure", sa.Float(), nullable=False, server_default="0"),
        sa.Column("risk_pressure", sa.Float(), nullable=False, server_default="0"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("evidence_story_ids_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("entered_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("expires_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("assessment_version", sa.String(length=64), nullable=False),
        sa.Column("transition_reason", sa.String(length=128), nullable=False, server_default="initialized"),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_newswire_risk_states_mode_updated", "newswire_risk_states", ["mode", "updated_at_ms"])

    op.create_table(
        "newswire_risk_transitions",
        sa.Column("transition_id", sa.String(length=96), primary_key=True),
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("from_mode", sa.String(length=24), nullable=False),
        sa.Column("to_mode", sa.String(length=24), nullable=False),
        sa.Column("signed_pressure", sa.Float(), nullable=False),
        sa.Column("risk_pressure", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence_story_ids_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("reason", sa.String(length=128), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_newswire_risk_transitions_scope_created", "newswire_risk_transitions", ["scope", "created_at_ms"])
    op.create_index("ix_newswire_risk_transitions_mode_created", "newswire_risk_transitions", ["to_mode", "created_at_ms"])


def downgrade() -> None:
    op.drop_index("ix_newswire_risk_transitions_mode_created", table_name="newswire_risk_transitions")
    op.drop_index("ix_newswire_risk_transitions_scope_created", table_name="newswire_risk_transitions")
    op.drop_table("newswire_risk_transitions")
    op.drop_index("ix_newswire_risk_states_mode_updated", table_name="newswire_risk_states")
    op.drop_table("newswire_risk_states")
    op.drop_index("ix_newswire_deliveries_story", table_name="newswire_deliveries")
    op.drop_index("ix_newswire_deliveries_due", table_name="newswire_deliveries")
    op.drop_table("newswire_deliveries")
    op.drop_index("ix_newswire_story_revisions_story", table_name="newswire_story_revisions")
    op.drop_index("ix_newswire_story_revisions_emitted", table_name="newswire_story_revisions")
    op.drop_table("newswire_story_revisions")
    op.drop_index("ix_newswire_stories_canonical_event", table_name="newswire_stories")
    op.drop_index("ix_newswire_stories_status_updated", table_name="newswire_stories")
    op.drop_index("ix_newswire_stories_updated", table_name="newswire_stories")
    op.drop_table("newswire_stories")
    op.drop_index("ix_newswire_events_story", table_name="newswire_events")
    op.drop_column("newswire_events", "assessment_json")
    op.drop_column("newswire_events", "topics_json")
    op.drop_column("newswire_events", "story_revision")
    op.drop_column("newswire_events", "story_id")
