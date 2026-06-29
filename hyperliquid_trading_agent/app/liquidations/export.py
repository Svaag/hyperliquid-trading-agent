"""Stable serializers for the public export API (CSV / NDJSON).

Pure functions over the **public projection** dict (the same redacted shape the
JSON API returns — counterparties hashed, ``raw`` dropped). The column set is
fixed and explicit so the CSV schema is stable across releases and a missing key
serializes to an empty cell rather than shifting columns. No DB, no I/O — these
are unit-testable in isolation and shared by the streaming export route.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

# Fixed, analyst-friendly column order. Counterparties are the redacted values;
# ``raw`` and un-hashed identities are intentionally absent from every export.
EXPORT_COLUMNS: tuple[str, ...] = (
    "event_id",
    "timestamp_ms",
    "received_at_ms",
    "venue",
    "source",
    "source_integrity",
    "event_type",
    "symbol",
    "venue_market_id",
    "liquidated_side",
    "raw_side",
    "price",
    "avg_price",
    "mark_price",
    "bankruptcy_price",
    "size_base",
    "notional_usd",
    "block_height",
    "tx_hash",
    "log_index",
    "trade_id",
    "liquidation_id",
    "liquidated_user",
    "liquidator",
    "method",
    "confidence",
)

CSV_MEDIA_TYPE = "text/csv"
NDJSON_MEDIA_TYPE = "application/x-ndjson"


def _csv_line(values: list[Any]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow(values)
    return buf.getvalue()


def format_csv_header() -> str:
    return _csv_line(list(EXPORT_COLUMNS))


def format_csv_row(row: dict[str, Any]) -> str:
    return _csv_line([_cell(row.get(col)) for col in EXPORT_COLUMNS])


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def format_ndjson_row(row: dict[str, Any]) -> str:
    return json.dumps(row, separators=(",", ":")) + "\n"


def to_csv(rows: list[dict[str, Any]]) -> str:
    """Whole-table CSV (header + rows) — convenience for tests / small results."""
    return format_csv_header() + "".join(format_csv_row(row) for row in rows)
