"""In-process async pub/sub fan-out for normalized liquidation events.

The supervisor publishes every persisted `LiquidationEvent` here; the store,
rolling aggregator, SSE/WebSocket browser feeds, and the observe-only agent
bridge all consume from it. This is deliberately a thin in-process bus today —
the same interface (publish + subscribe-iterator) maps cleanly onto a
Redis/NATS/Kafka topic when the subsystem is extracted into its own service.

Backpressure favors ingest: a slow subscriber (e.g. a stalled browser) drops its
*oldest* queued events rather than blocking producers or other subscribers.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from hyperliquid_trading_agent.app.liquidations.models import LiquidationEvent
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.metrics import LIQUIDATION_BUS_DROPPED, LIQUIDATION_BUS_SUBSCRIBERS

log = get_logger(__name__)


class LiquidationBus:
    def __init__(self, subscriber_maxsize: int = 2000) -> None:
        self._subscriber_maxsize = subscriber_maxsize
        self._queues: set[asyncio.Queue[LiquidationEvent]] = set()

    async def publish(self, event: LiquidationEvent) -> None:
        for queue in list(self._queues):
            self._offer(queue, event)

    def _offer(self, queue: asyncio.Queue[LiquidationEvent], event: LiquidationEvent) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop the oldest to keep the freshest tape moving for a slow client.
            try:
                queue.get_nowait()
                queue.task_done()
            except asyncio.QueueEmpty:  # pragma: no cover - racy but harmless
                pass
            LIQUIDATION_BUS_DROPPED.labels(reason="slow_subscriber").inc()
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - extreme backpressure
                LIQUIDATION_BUS_DROPPED.labels(reason="overflow").inc()

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[AsyncIterator[LiquidationEvent]]:
        """Yield an async iterator of events for the lifetime of the context."""
        queue: asyncio.Queue[LiquidationEvent] = asyncio.Queue(maxsize=self._subscriber_maxsize)
        self._queues.add(queue)
        LIQUIDATION_BUS_SUBSCRIBERS.set(len(self._queues))
        try:
            yield self._iterate(queue)
        finally:
            self._queues.discard(queue)
            LIQUIDATION_BUS_SUBSCRIBERS.set(len(self._queues))

    async def _iterate(self, queue: asyncio.Queue[LiquidationEvent]) -> AsyncIterator[LiquidationEvent]:
        while True:
            event = await queue.get()
            try:
                yield event
            finally:
                queue.task_done()

    @property
    def subscriber_count(self) -> int:
        return len(self._queues)
