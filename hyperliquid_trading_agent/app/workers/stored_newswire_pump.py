from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from hyperliquid_trading_agent.app.db.repository import Repository
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.newswire.schemas import NewswireEvent

log = get_logger(__name__)

NewswireHandler = Callable[[NewswireEvent], Awaitable[None] | None]


class StoredNewswirePump:
    """Poll persisted Newswire events and deliver them to one durable consumer."""

    def __init__(
        self,
        *,
        consumer_name: str,
        repository: Repository,
        callbacks: list[NewswireHandler],
        poll_seconds: float = 1.0,
        batch_size: int = 100,
        bootstrap_from_latest: bool = False,
        bootstrap_metadata: dict[str, Any] | None = None,
    ):
        self.consumer_name = consumer_name
        self.repository = repository
        self.callbacks = callbacks
        self.poll_seconds = max(0.1, float(poll_seconds))
        self.batch_size = max(1, int(batch_size))
        self.bootstrap_from_latest = bool(bootstrap_from_latest)
        self.bootstrap_metadata = bootstrap_metadata or {}
        self.running = False
        self.processed = 0
        self.error_count = 0
        self.consecutive_error_count = 0
        self.invalid_rows_skipped = 0
        self.bootstrapped_from_latest = False
        self.last_event_id: str | None = None
        self.last_error: str | None = None
        self.last_error_at_ms: int | None = None
        self.last_success_at_ms: int | None = None
        self.last_invalid_row_at_ms: int | None = None
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        self.running = True
        try:
            while not self._stop.is_set():
                processed = await self.run_once()
                if processed == 0:
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
                    except TimeoutError:
                        continue
        finally:
            self.running = False

    async def run_once(self) -> int:
        offset = await self.repository.get_consumer_offset(self.consumer_name, source_table="newswire_events")
        if await self._maybe_bootstrap_from_latest(offset):
            return 0
        rows = await self.repository.list_newswire_events_after(
            last_event_ts_ms=int(offset.get("last_event_ts_ms") or 0),
            last_event_id=offset.get("last_event_id"),
            limit=self.batch_size,
        )
        count = 0
        for row in rows:
            try:
                event = NewswireEvent.model_validate(row)
            except Exception as exc:
                await self._skip_invalid_row(row, exc)
                count += 1
                continue
            try:
                for callback in self.callbacks:
                    result = callback(event)
                    if result is not None:
                        await result
                await self.repository.update_consumer_offset(
                    self.consumer_name,
                    source_table="newswire_events",
                    last_event_id=event.event_id,
                    last_event_ts_ms=int(event.received_at_ms),
                    metadata={"last_headline": event.headline, "source": event.source},
                )
                self.processed += 1
                count += 1
                self.last_event_id = event.event_id
                self._record_success()
            except Exception as exc:  # pragma: no cover - worker safety net
                self.error_count += 1
                self.consecutive_error_count += 1
                self.last_error = type(exc).__name__
                self.last_error_at_ms = _now_ms()
                log.warning("stored_newswire_pump_event_failed", consumer=self.consumer_name, event_id=event.event_id, error=type(exc).__name__)
                break
        return count

    async def _maybe_bootstrap_from_latest(self, offset: dict[str, Any]) -> bool:
        if not self.bootstrap_from_latest or self.bootstrapped_from_latest:
            return False
        has_offset = bool(offset.get("last_event_id")) or int(offset.get("last_event_ts_ms") or 0) > 0 or int(offset.get("updated_at_ms") or 0) > 0
        if has_offset:
            self.bootstrapped_from_latest = True
            return False
        latest = await self.repository.list_newswire_events(limit=1)
        if not latest:
            return False
        row = latest[0]
        event_id = str(row.get("event_id") or "")
        received_at_ms = int(row.get("received_at_ms") or 0)
        if not event_id or received_at_ms <= 0:
            return False
        metadata = {
            **self.bootstrap_metadata,
            "bootstrap_from_latest": True,
            "reason": "avoid_historical_news_regime_pollution",
            "last_headline": row.get("headline"),
            "source": row.get("source"),
        }
        await self.repository.update_consumer_offset(
            self.consumer_name,
            source_table="newswire_events",
            last_event_id=event_id,
            last_event_ts_ms=received_at_ms,
            metadata=metadata,
        )
        self.bootstrapped_from_latest = True
        self.last_event_id = event_id
        return True

    async def _skip_invalid_row(self, row: dict[str, Any], exc: Exception) -> None:
        event_id = str(row.get("event_id") or "")
        received_at_ms = int(row.get("received_at_ms") or 0)
        self.error_count += 1
        self.invalid_rows_skipped += 1
        self.last_error = type(exc).__name__
        self.last_invalid_row_at_ms = _now_ms()
        if event_id and received_at_ms > 0:
            await self.repository.update_consumer_offset(
                self.consumer_name,
                source_table="newswire_events",
                last_event_id=event_id,
                last_event_ts_ms=received_at_ms,
                metadata={
                    "invalid_row_skipped": True,
                    "error": type(exc).__name__,
                    "last_headline": row.get("headline"),
                    "source": row.get("source"),
                },
            )
            self.last_event_id = event_id
        log.warning("stored_newswire_pump_invalid_row", consumer=self.consumer_name, event_id=event_id or None, error=type(exc).__name__)

    async def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict[str, Any]:
        return {
            "consumer_name": self.consumer_name,
            "running": self.running,
            "processed": self.processed,
            "last_event_id": self.last_event_id,
            "error_count": self.error_count,
            "consecutive_error_count": self.consecutive_error_count,
            "invalid_rows_skipped": self.invalid_rows_skipped,
            "bootstrap_from_latest": self.bootstrap_from_latest,
            "bootstrapped_from_latest": self.bootstrapped_from_latest,
            "last_error": self.last_error,
            "last_error_at_ms": self.last_error_at_ms,
            "last_success_at_ms": self.last_success_at_ms,
            "last_invalid_row_at_ms": self.last_invalid_row_at_ms,
        }

    def _record_success(self) -> None:
        self.consecutive_error_count = 0
        self.last_success_at_ms = _now_ms()
        self.last_error = None


def _now_ms() -> int:
    return int(time.time() * 1000)
