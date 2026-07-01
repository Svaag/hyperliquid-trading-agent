from __future__ import annotations

import asyncio
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
    ):
        self.consumer_name = consumer_name
        self.repository = repository
        self.callbacks = callbacks
        self.poll_seconds = max(0.1, float(poll_seconds))
        self.batch_size = max(1, int(batch_size))
        self.running = False
        self.processed = 0
        self.error_count = 0
        self.last_event_id: str | None = None
        self.last_error: str | None = None
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
        rows = await self.repository.list_newswire_events_after(
            last_event_ts_ms=int(offset.get("last_event_ts_ms") or 0),
            last_event_id=offset.get("last_event_id"),
            limit=self.batch_size,
        )
        count = 0
        for row in rows:
            event = NewswireEvent.model_validate(row)
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
            except Exception as exc:  # pragma: no cover - worker safety net
                self.error_count += 1
                self.last_error = type(exc).__name__
                log.warning("stored_newswire_pump_event_failed", consumer=self.consumer_name, event_id=event.event_id, error=type(exc).__name__)
                break
        return count

    async def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict[str, Any]:
        return {
            "consumer_name": self.consumer_name,
            "running": self.running,
            "processed": self.processed,
            "last_event_id": self.last_event_id,
            "error_count": self.error_count,
            "last_error": self.last_error,
        }
