"""Repair Newswire symbol reasons corrupted by legacy secret redaction.

Revision ID: 0032_repair_newswire_reasons
Revises: 0031_operational_incidents
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import sqlalchemy as sa

from alembic import op

revision = "0032_repair_newswire_reasons"
down_revision = "0031_operational_incidents"
branch_labels = None
depends_on = None

_QUALITY_FLAG = "legacy_symbol_reason_redaction_repaired"
_REPAIRED_REASON = "legacy_redaction_repaired"


def upgrade() -> None:
    bind = op.get_bind()
    _repair_story_rows(bind)
    _repair_revision_rows(bind)
    _repair_event_rows(bind)


def downgrade() -> None:
    # The legacy redactor destroyed the original reason values, so the repair is
    # intentionally irreversible. No schema objects are created by this revision.
    pass


def _repair_story_rows(bind: Any) -> None:
    table = sa.table(
        "newswire_stories",
        sa.column("story_id", sa.String()),
        sa.column("assessment_json", sa.JSON()),
        sa.column("metadata_json", sa.JSON()),
    )
    rows = list(
        bind.execute(
            sa.select(table.c.story_id, table.c.assessment_json, table.c.metadata_json)
        ).mappings()
    )
    for row in rows:
        assessment, changed = _repair_assessment(row["assessment_json"])
        if not changed:
            continue
        bind.execute(
            table.update()
            .where(table.c.story_id == row["story_id"])
            .values(
                assessment_json=assessment,
                metadata_json=_add_quality_flag(row["metadata_json"]),
            )
        )


def _repair_revision_rows(bind: Any) -> None:
    table = sa.table(
        "newswire_story_revisions",
        sa.column("revision_id", sa.String()),
        sa.column("story_json", sa.JSON()),
    )
    rows = list(bind.execute(sa.select(table.c.revision_id, table.c.story_json)).mappings())
    for row in rows:
        story = deepcopy(row["story_json"])
        if not isinstance(story, dict):
            continue
        assessment, changed = _repair_assessment(story.get("assessment"))
        if not changed:
            continue
        story["assessment"] = assessment
        story["metadata"] = _add_quality_flag(story.get("metadata"))
        bind.execute(
            table.update()
            .where(table.c.revision_id == row["revision_id"])
            .values(story_json=story)
        )


def _repair_event_rows(bind: Any) -> None:
    table = sa.table(
        "newswire_events",
        sa.column("event_id", sa.String()),
        sa.column("assessment_json", sa.JSON()),
        sa.column("metadata_json", sa.JSON()),
    )
    rows = list(
        bind.execute(
            sa.select(table.c.event_id, table.c.assessment_json, table.c.metadata_json)
        ).mappings()
    )
    for row in rows:
        assessment, changed = _repair_assessment(row["assessment_json"])
        if not changed:
            continue
        bind.execute(
            table.update()
            .where(table.c.event_id == row["event_id"])
            .values(
                assessment_json=assessment,
                metadata_json=_add_quality_flag(row["metadata_json"]),
            )
        )


def _repair_assessment(value: Any) -> tuple[Any, bool]:
    if not isinstance(value, dict):
        return value, False
    assessment = deepcopy(value)
    reasons = assessment.get("symbol_match_reasons")
    if not isinstance(reasons, dict):
        return assessment, False
    changed = False
    repaired_reasons: dict[str, Any] = {}
    for symbol, symbol_reasons in reasons.items():
        if symbol_reasons == "[REDACTED]":
            repaired_reasons[str(symbol)] = [_REPAIRED_REASON]
            changed = True
            continue
        if isinstance(symbol_reasons, list) and "[REDACTED]" in symbol_reasons:
            repaired_reasons[str(symbol)] = [
                _REPAIRED_REASON if item == "[REDACTED]" else item
                for item in symbol_reasons
            ]
            changed = True
            continue
        repaired_reasons[str(symbol)] = symbol_reasons
    if changed:
        assessment["symbol_match_reasons"] = repaired_reasons
    return assessment, changed


def _add_quality_flag(value: Any) -> dict[str, Any]:
    metadata = deepcopy(value) if isinstance(value, dict) else {}
    flags = metadata.get("data_quality_flags")
    clean_flags = list(flags) if isinstance(flags, list) else []
    if _QUALITY_FLAG not in clean_flags:
        clean_flags.append(_QUALITY_FLAG)
    metadata["data_quality_flags"] = clean_flags
    return metadata
