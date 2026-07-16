"""Repair Newswire symbol reasons corrupted by legacy secret redaction.

Revision ID: 0032_repair_newswire_reasons
Revises: 0031_operational_incidents
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0032_repair_newswire_reasons"
down_revision = "0031_operational_incidents"
branch_labels = None
depends_on = None

_QUALITY_FLAG = "legacy_symbol_reason_redaction_repaired"
_REPAIRED_REASON = "legacy_redaction_repaired"


def upgrade() -> None:
    """Repair affected JSON documents with offline-renderable set-based SQL."""

    _repair_assessment_column(
        table_name="newswire_stories",
        id_column="story_id",
        assessment_column="assessment_json",
        metadata_column="metadata_json",
    )
    _repair_story_revisions()
    _repair_assessment_column(
        table_name="newswire_events",
        id_column="event_id",
        assessment_column="assessment_json",
        metadata_column="metadata_json",
    )


def downgrade() -> None:
    # The legacy redactor destroyed the original reason values, so the repair is
    # intentionally irreversible. No schema objects are created by this revision.
    pass


def _repair_assessment_column(
    *,
    table_name: str,
    id_column: str,
    assessment_column: str,
    metadata_column: str,
) -> None:
    assessment = f"source.{assessment_column}::jsonb"
    metadata = f"source.{metadata_column}::jsonb"
    op.execute(
        sa.text(
            f"""
            WITH repaired AS (
                SELECT
                    source.{id_column} AS row_id,
                    {_repaired_assessment_sql(assessment)} AS assessment_json,
                    {_quality_metadata_sql(metadata)} AS metadata_json
                FROM {table_name} AS source
                WHERE {_contains_redacted_reason_sql(assessment)}
            )
            UPDATE {table_name} AS target SET
                {assessment_column}=repaired.assessment_json::json,
                {metadata_column}=repaired.metadata_json::json
            FROM repaired
            WHERE target.{id_column}=repaired.row_id
            """
        )
    )


def _repair_story_revisions() -> None:
    story = "source.story_json::jsonb"
    assessment = f"({story} -> 'assessment')"
    metadata = f"({story} -> 'metadata')"
    repaired_story = (
        "jsonb_set("
        f"jsonb_set({story}, '{{assessment}}', {_repaired_assessment_sql(assessment)}, false), "
        f"'{{metadata}}', {_quality_metadata_sql(metadata)}, true)"
    )
    op.execute(
        sa.text(
            f"""
            WITH repaired AS (
                SELECT
                    source.revision_id,
                    {repaired_story} AS story_json
                FROM newswire_story_revisions AS source
                WHERE jsonb_typeof({story})='object'
                  AND {_contains_redacted_reason_sql(assessment)}
            )
            UPDATE newswire_story_revisions AS target SET
                story_json=repaired.story_json::json
            FROM repaired
            WHERE target.revision_id=repaired.revision_id
            """
        )
    )


def _contains_redacted_reason_sql(assessment: str) -> str:
    return f"""
        jsonb_typeof({assessment})='object'
        AND jsonb_typeof({assessment} -> 'symbol_match_reasons')='object'
        AND EXISTS (
            SELECT 1
            FROM jsonb_each(
                CASE
                    WHEN jsonb_typeof({assessment})='object'
                         AND jsonb_typeof({assessment} -> 'symbol_match_reasons')='object'
                        THEN {assessment} -> 'symbol_match_reasons'
                    ELSE '{{}}'::jsonb
                END
            ) AS reason(symbol, value)
            WHERE reason.value='"[REDACTED]"'::jsonb
               OR (
                    jsonb_typeof(reason.value)='array'
                    AND reason.value @> '["[REDACTED]"]'::jsonb
               )
        )
    """


def _repaired_assessment_sql(assessment: str) -> str:
    return f"""
        jsonb_set(
            {assessment},
            '{{symbol_match_reasons}}',
            (
                SELECT jsonb_object_agg(
                    reason.symbol,
                    CASE
                        WHEN reason.value='"[REDACTED]"'::jsonb
                            THEN jsonb_build_array('{_REPAIRED_REASON}'::text)
                        WHEN jsonb_typeof(reason.value)='array'
                             AND reason.value @> '["[REDACTED]"]'::jsonb
                            THEN (
                                SELECT jsonb_agg(
                                    CASE
                                        WHEN item.value='"[REDACTED]"'::jsonb
                                            THEN to_jsonb('{_REPAIRED_REASON}'::text)
                                        ELSE item.value
                                    END
                                    ORDER BY item.ordinality
                                )
                                FROM jsonb_array_elements(reason.value)
                                    WITH ORDINALITY AS item(value, ordinality)
                            )
                        ELSE reason.value
                    END
                )
                FROM jsonb_each({assessment} -> 'symbol_match_reasons') AS reason(symbol, value)
            ),
            false
        )
    """


def _quality_metadata_sql(metadata: str) -> str:
    clean_metadata = f"CASE WHEN jsonb_typeof({metadata})='object' THEN {metadata} ELSE '{{}}'::jsonb END"
    flags = f"({clean_metadata} -> 'data_quality_flags')"
    repaired_flags = f"""
        CASE
            WHEN jsonb_typeof({flags})='array' THEN
                CASE
                    WHEN {flags} @> jsonb_build_array('{_QUALITY_FLAG}'::text)
                        THEN {flags}
                    ELSE {flags} || jsonb_build_array('{_QUALITY_FLAG}'::text)
                END
            ELSE jsonb_build_array('{_QUALITY_FLAG}'::text)
        END
    """
    return f"jsonb_set({clean_metadata}, '{{data_quality_flags}}', {repaired_flags}, true)"
