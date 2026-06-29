"""Durable, append-only persistence for the liquidation subsystem.

Mirrors the repo's `Repository` contract: a ``None`` sessionmaker means the store
is disabled and every method degrades to a no-op / empty result, so the whole
pipeline (adapters -> bus -> aggregator -> SSE) runs without Postgres in tests
and local dev. Inserts are idempotent on ``event_id`` so reconnect/snapshot
re-deliveries collapse to one row.

Self-contained (owns its own queries rather than extending the 4k-line
Repository) so the subsystem lifts cleanly into a standalone service later.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, Integer, and_, case, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hyperliquid_trading_agent.app.db.models import LiquidationAdapterStateRecord, LiquidationEventRecord
from hyperliquid_trading_agent.app.liquidations import dedupe
from hyperliquid_trading_agent.app.liquidations.models import EXECUTION_EVENT_TYPES, LiquidationEvent
from hyperliquid_trading_agent.app.logging import get_logger

log = get_logger(__name__)

_NUMERIC_FIELDS = ("price", "avg_price", "mark_price", "bankruptcy_price", "size_base", "notional_usd", "confidence")
_TOP_SYMBOLS = 25
_EXECUTION_EVENT_VALUES = tuple(str(t) for t in EXECUTION_EVENT_TYPES)


def _f(value: Decimal | float | None) -> float | None:
    return float(value) if value is not None else None


def encode_cursor(timestamp_ms: int, event_id: str) -> str:
    """Opaque keyset cursor over the ``(timestamp_ms DESC, event_id DESC)`` order."""
    return base64.urlsafe_b64encode(f"{timestamp_ms}:{event_id}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[int, str] | None:
    """Inverse of :func:`encode_cursor`; ``None`` if the token is malformed."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts_str, event_id = raw.split(":", 1)
        return int(ts_str), event_id
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None


class LiquidationStore:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession] | None):
        self.sessionmaker = sessionmaker

    @property
    def enabled(self) -> bool:
        return self.sessionmaker is not None

    async def persist(self, event: LiquidationEvent) -> bool:
        """Insert one event; returns True if stored, False if a duplicate/no-op."""
        if self.sessionmaker is None:
            return False
        row: dict[str, Any] = {
            "event_id": event.event_id,
            "venue": str(event.venue),
            "source": event.source,
            "source_integrity": str(event.source_integrity),
            "event_type": str(event.event_type),
            "symbol": event.symbol,
            "venue_market_id": event.venue_market_id,
            "liquidated_side": event.liquidated_side,
            "raw_side": event.raw_side,
            "price": _f(event.price),
            "avg_price": _f(event.avg_price),
            "mark_price": _f(event.mark_price),
            "bankruptcy_price": _f(event.bankruptcy_price),
            "size_base": _f(event.size_base),
            "notional_usd": _f(event.notional_usd),
            "timestamp_ms": event.timestamp_ms,
            "received_at_ms": event.received_at_ms,
            "block_height": event.block_height,
            "tx_hash": event.tx_hash,
            "log_index": event.log_index,
            "trade_id": event.trade_id,
            "liquidation_id": event.liquidation_id,
            "liquidated_user": event.liquidated_user,
            "liquidator": event.liquidator,
            "method": event.method,
            "confidence": _f(event.confidence),
            "raw_json": event.raw,
        }
        try:
            async with self.sessionmaker() as session:
                stmt = pg_insert(LiquidationEventRecord).values(**row).on_conflict_do_nothing(index_elements=["event_id"])
                result = await session.execute(stmt)
                await session.commit()
                return bool(getattr(result, "rowcount", 0))
        except Exception as exc:  # pragma: no cover - persistence must not break ingest
            log.warning("liquidation_persist_failed", venue=str(event.venue), error=type(exc).__name__)
            return False

    async def recent(
        self,
        *,
        limit: int = 100,
        venue: str | None = None,
        symbol: str | None = None,
        min_notional: float | None = None,
        source_integrity: str | None = None,
    ) -> list[dict[str, Any]]:
        """Most-recent persisted events (public projection)."""
        if self.sessionmaker is None:
            return []
        limit = max(1, min(limit, 1000))
        stmt = select(LiquidationEventRecord).order_by(LiquidationEventRecord.timestamp_ms.desc()).limit(limit)
        if venue:
            stmt = stmt.where(LiquidationEventRecord.venue == venue)
        if symbol:
            stmt = stmt.where(LiquidationEventRecord.symbol == symbol.upper())
        if source_integrity:
            stmt = stmt.where(LiquidationEventRecord.source_integrity == source_integrity)
        if min_notional is not None:
            stmt = stmt.where(LiquidationEventRecord.notional_usd >= min_notional)
        try:
            async with self.sessionmaker() as session:
                rows = (await session.execute(stmt)).scalars().all()
                return [_record_to_dict(row, public=True) for row in rows]
        except Exception as exc:  # pragma: no cover
            log.warning("liquidation_recent_failed", error=type(exc).__name__)
            return []

    async def get_event(self, event_id: str) -> dict[str, Any] | None:
        """Full row including raw payload — admin/audit only."""
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                row = await session.get(LiquidationEventRecord, event_id)
                return _record_to_dict(row, public=False) if row is not None else None
        except Exception as exc:  # pragma: no cover
            log.warning("liquidation_get_event_failed", error=type(exc).__name__)
            return None

    async def query(
        self,
        *,
        limit: int = 200,
        cursor: str | None = None,
        venue: str | None = None,
        symbol: str | None = None,
        source_integrity: str | None = None,
        event_type: str | None = None,
        side: str | None = None,
        min_notional: float | None = None,
        since_ms: int | None = None,
        until_ms: int | None = None,
    ) -> dict[str, Any]:
        """Durable, keyset-paginated history (public projection).

        Ordered ``timestamp_ms DESC, event_id DESC``; ``next_cursor`` is non-null
        only when a full page was returned (i.e. more rows may exist).
        """
        if self.sessionmaker is None:
            return {"items": [], "next_cursor": None}
        limit = max(1, min(limit, 1000))
        stmt = select(LiquidationEventRecord).where(
            *_filter_conditions(
                venue=venue,
                symbol=symbol,
                source_integrity=source_integrity,
                event_type=event_type,
                side=side,
                min_notional=min_notional,
                since_ms=since_ms,
                until_ms=until_ms,
            )
        )
        if cursor:
            decoded = decode_cursor(cursor)
            if decoded is not None:
                ts, eid = decoded
                stmt = stmt.where(
                    or_(
                        LiquidationEventRecord.timestamp_ms < ts,
                        and_(LiquidationEventRecord.timestamp_ms == ts, LiquidationEventRecord.event_id < eid),
                    )
                )
        stmt = stmt.order_by(
            LiquidationEventRecord.timestamp_ms.desc(), LiquidationEventRecord.event_id.desc()
        ).limit(limit)
        try:
            async with self.sessionmaker() as session:
                rows = list((await session.execute(stmt)).scalars().all())
        except Exception as exc:  # pragma: no cover - read must not raise to the API
            log.warning("liquidation_query_failed", error=type(exc).__name__)
            return {"items": [], "next_cursor": None}
        next_cursor = encode_cursor(rows[-1].timestamp_ms, rows[-1].event_id) if len(rows) == limit else None
        return {"items": [_record_to_dict(r, public=True) for r in rows], "next_cursor": next_cursor}

    async def stream_query(
        self,
        *,
        max_rows: int,
        venue: str | None = None,
        symbol: str | None = None,
        source_integrity: str | None = None,
        event_type: str | None = None,
        side: str | None = None,
        min_notional: float | None = None,
        since_ms: int | None = None,
        until_ms: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Server-side cursored stream of the public projection, for bulk export.

        Yields at most ``max_rows`` rows newest-first without buffering the whole
        result set (backed by ``stream_scalars``). No-op generator when disabled.
        """
        if self.sessionmaker is None:
            return
        stmt = (
            select(LiquidationEventRecord)
            .where(
                *_filter_conditions(
                    venue=venue,
                    symbol=symbol,
                    source_integrity=source_integrity,
                    event_type=event_type,
                    side=side,
                    min_notional=min_notional,
                    since_ms=since_ms,
                    until_ms=until_ms,
                )
            )
            .order_by(LiquidationEventRecord.timestamp_ms.desc(), LiquidationEventRecord.event_id.desc())
        )
        async with self.sessionmaker() as session:
            result = await session.stream_scalars(stmt)
            emitted = 0
            async for row in result:
                yield _record_to_dict(row, public=True)
                emitted += 1
                if emitted >= max_rows:
                    break

    async def stats(
        self,
        *,
        since_ms: int,
        until_ms: int,
        bucket_ms: int,
        venue: str | None = None,
        symbol: str | None = None,
        source_integrity: str | None = None,
        side: str | None = None,
        min_notional: float | None = None,
    ) -> dict[str, Any]:
        """Durable, bucketed aggregates over an arbitrary range — executions only.

        Inferred ``liquidation_pressure`` is always excluded so the historical
        "how much was liquidated" series can never be inflated by derived flow.
        """
        empty: dict[str, Any] = {
            "since_ms": since_ms,
            "until_ms": until_ms,
            "bucket_ms": bucket_ms,
            "count": 0,
            "total_notional_usd": 0.0,
            "series": [],
            "by_venue": {},
            "by_symbol": {},
            "by_integrity": {},
        }
        if self.sessionmaker is None:
            return empty
        notional = func.coalesce(LiquidationEventRecord.notional_usd, 0.0)
        side_col = LiquidationEventRecord.liquidated_side
        conditions = [
            LiquidationEventRecord.timestamp_ms >= since_ms,
            LiquidationEventRecord.timestamp_ms < until_ms,
            LiquidationEventRecord.event_type.in_(_EXECUTION_EVENT_VALUES),
            *_filter_conditions(
                venue=venue, symbol=symbol, source_integrity=source_integrity, side=side, min_notional=min_notional
            ),
        ]
        # Integer bucket index from range start; integer/integer division floors on
        # both Postgres and SQLite, so the same SQL serves prod and tests.
        bucket = ((LiquidationEventRecord.timestamp_ms - since_ms) / bucket_ms).cast(Integer).label("bucket")
        series_stmt = (
            select(
                bucket,
                func.count().label("count"),
                func.coalesce(func.sum(notional), 0.0).label("total"),
                func.coalesce(func.sum(case((side_col == "long", notional), else_=0.0)), 0.0).label("long"),
                func.coalesce(func.sum(case((side_col == "short", notional), else_=0.0)), 0.0).label("short"),
            )
            .where(*conditions)
            .group_by(bucket)
            .order_by(bucket)
        )
        venue_stmt = (
            select(LiquidationEventRecord.venue, func.coalesce(func.sum(notional), 0.0))
            .where(*conditions)
            .group_by(LiquidationEventRecord.venue)
        )
        symbol_stmt = (
            select(LiquidationEventRecord.symbol, func.coalesce(func.sum(notional), 0.0).label("n"))
            .where(*conditions)
            .group_by(LiquidationEventRecord.symbol)
            .order_by(func.coalesce(func.sum(notional), 0.0).desc())
            .limit(_TOP_SYMBOLS)
        )
        integrity_stmt = (
            select(LiquidationEventRecord.source_integrity, func.count())
            .where(*conditions)
            .group_by(LiquidationEventRecord.source_integrity)
        )
        try:
            async with self.sessionmaker() as session:
                series_rows = (await session.execute(series_stmt)).all()
                venue_rows = (await session.execute(venue_stmt)).all()
                symbol_rows = (await session.execute(symbol_stmt)).all()
                integrity_rows = (await session.execute(integrity_stmt)).all()
        except Exception as exc:  # pragma: no cover - read must not raise to the API
            log.warning("liquidation_stats_failed", error=type(exc).__name__)
            return empty
        series = [
            {
                "t": since_ms + int(b) * bucket_ms,
                "count": int(count),
                "total": float(total),
                "long": float(long_usd),
                "short": float(short_usd),
            }
            for (b, count, total, long_usd, short_usd) in series_rows
        ]
        return {
            "since_ms": since_ms,
            "until_ms": until_ms,
            "bucket_ms": bucket_ms,
            "count": sum(row["count"] for row in series),
            "total_notional_usd": sum(row["total"] for row in series),
            "series": series,
            "by_venue": {venue_name: float(total) for venue_name, total in venue_rows},
            "by_symbol": {sym: float(total) for sym, total in symbol_rows},
            "by_integrity": {integrity: int(count) for integrity, count in integrity_rows},
        }

    async def upsert_adapter_state(
        self,
        adapter_name: str,
        *,
        status: str,
        updated_at_ms: int,
        last_event_ms: int | None = None,
        last_cursor: str | None = None,
        error: str | None = None,
    ) -> None:
        if self.sessionmaker is None:
            return
        values = {
            "adapter_name": adapter_name,
            "status": status,
            "updated_at_ms": updated_at_ms,
            "last_event_ms": last_event_ms,
            "last_cursor": last_cursor,
            "error": error,
        }
        update_cols = {k: v for k, v in values.items() if k != "adapter_name"}
        try:
            async with self.sessionmaker() as session:
                stmt = (
                    pg_insert(LiquidationAdapterStateRecord)
                    .values(**values)
                    .on_conflict_do_update(index_elements=["adapter_name"], set_=update_cols)
                )
                await session.execute(stmt)
                await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("liquidation_adapter_state_failed", adapter=adapter_name, error=type(exc).__name__)


def _filter_conditions(
    *,
    venue: str | None = None,
    symbol: str | None = None,
    source_integrity: str | None = None,
    event_type: str | None = None,
    side: str | None = None,
    min_notional: float | None = None,
    since_ms: int | None = None,
    until_ms: int | None = None,
) -> list[ColumnElement[bool]]:
    """Shared WHERE-clause set for the history/export/stats reads."""
    conditions: list[ColumnElement[bool]] = []
    if venue:
        conditions.append(LiquidationEventRecord.venue == venue)
    if symbol:
        conditions.append(LiquidationEventRecord.symbol == symbol.upper())
    if source_integrity:
        conditions.append(LiquidationEventRecord.source_integrity == source_integrity)
    if event_type:
        conditions.append(LiquidationEventRecord.event_type == event_type)
    if side:
        conditions.append(LiquidationEventRecord.liquidated_side == side)
    if min_notional is not None:
        conditions.append(LiquidationEventRecord.notional_usd >= min_notional)
    if since_ms is not None:
        conditions.append(LiquidationEventRecord.timestamp_ms >= since_ms)
    if until_ms is not None:
        conditions.append(LiquidationEventRecord.timestamp_ms < until_ms)
    return conditions


def _record_to_dict(record: LiquidationEventRecord, *, public: bool) -> dict[str, Any]:
    data: dict[str, Any] = {
        "event_id": record.event_id,
        "venue": record.venue,
        "source": record.source,
        "source_integrity": record.source_integrity,
        "event_type": record.event_type,
        "symbol": record.symbol,
        "venue_market_id": record.venue_market_id,
        "liquidated_side": record.liquidated_side,
        "raw_side": record.raw_side,
        "price": record.price,
        "avg_price": record.avg_price,
        "mark_price": record.mark_price,
        "bankruptcy_price": record.bankruptcy_price,
        "size_base": record.size_base,
        "notional_usd": record.notional_usd,
        "timestamp_ms": record.timestamp_ms,
        "received_at_ms": record.received_at_ms,
        "block_height": record.block_height,
        "tx_hash": record.tx_hash,
        "log_index": record.log_index,
        "trade_id": record.trade_id,
        "liquidation_id": record.liquidation_id,
        "method": record.method,
        "confidence": record.confidence,
    }
    if public:
        data["liquidated_user"] = dedupe.redact_address(record.liquidated_user)
        data["liquidator"] = dedupe.redact_address(record.liquidator)
    else:
        data["liquidated_user"] = record.liquidated_user
        data["liquidator"] = record.liquidator
        data["raw"] = record.raw_json
    return data
