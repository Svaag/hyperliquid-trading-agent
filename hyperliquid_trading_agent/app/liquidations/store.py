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

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hyperliquid_trading_agent.app.db.models import LiquidationAdapterStateRecord, LiquidationEventRecord
from hyperliquid_trading_agent.app.liquidations import dedupe
from hyperliquid_trading_agent.app.liquidations.models import LiquidationEvent
from hyperliquid_trading_agent.app.logging import get_logger

log = get_logger(__name__)

_NUMERIC_FIELDS = ("price", "avg_price", "mark_price", "bankruptcy_price", "size_base", "notional_usd", "confidence")


def _f(value: Decimal | float | None) -> float | None:
    return float(value) if value is not None else None


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
