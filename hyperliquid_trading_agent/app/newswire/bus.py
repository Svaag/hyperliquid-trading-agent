from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from uuid import uuid4

from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.metrics import NEWSWIRE_BUS_DROPPED, NEWSWIRE_BUS_PUBLISHED
from hyperliquid_trading_agent.app.newswire.schemas import NewswireEvent, NewswireFilter

log = get_logger(__name__)

NewswireCallback = Callable[[NewswireEvent], Awaitable[None] | None]


class NewswireBus(Protocol):
    """Transport-agnostic pub/sub contract.

    The in-process implementation fans out via asyncio. Swapping to Redis/NATS later
    means re-implementing this Protocol; subscribers are unaffected.
    """

    async def publish(self, event: NewswireEvent) -> None: ...

    async def subscribe(self, callback: NewswireCallback, *, filter: NewswireFilter | None = None) -> str: ...

    async def unsubscribe(self, subscription_id: str) -> None: ...

    def status(self) -> dict[str, Any]: ...


class InProcessNewswireBus:
    """asyncio fan-out bus with per-subscriber filtering and callback isolation."""

    def __init__(self) -> None:
        self._subscribers: dict[str, tuple[NewswireFilter | None, NewswireCallback]] = {}
        self._lock = asyncio.Lock()
        self._published = 0
        self._last_event_id: str | None = None

    async def publish(self, event: NewswireEvent) -> None:
        self._published += 1
        self._last_event_id = event.event_id
        NEWSWIRE_BUS_PUBLISHED.labels(source=event.source, event_type=event.event_type).inc()
        async with self._lock:
            targets = [(flt, cb) for flt, cb in self._subscribers.values()]
        for flt, callback in targets:
            if flt is not None and not flt.matches(event):
                continue
            try:
                result = callback(event)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # pragma: no cover - subscriber isolation
                log.warning("newswire_subscriber_failed", error=type(exc).__name__)

    async def subscribe(self, callback: NewswireCallback, *, filter: NewswireFilter | None = None) -> str:
        subscription_id = uuid4().hex
        async with self._lock:
            self._subscribers[subscription_id] = (filter, callback)
        return subscription_id

    async def unsubscribe(self, subscription_id: str) -> None:
        async with self._lock:
            self._subscribers.pop(subscription_id, None)

    def status(self) -> dict[str, Any]:
        return {
            "subscriber_count": len(self._subscribers),
            "published_total": self._published,
            "last_event_id": self._last_event_id,
        }


class QueueSubscriber:
    """Bounded-queue bus subscription for remote/slow consumers (e.g. the WS endpoint).

    Decouples a slow client from ingestion: when the queue is full, events are dropped
    (and counted) rather than blocking the publisher.
    """

    def __init__(self, bus: NewswireBus, *, filter: NewswireFilter | None = None, maxsize: int = 256) -> None:
        self.bus = bus
        self.filter = filter
        self.queue: asyncio.Queue[NewswireEvent] = asyncio.Queue(maxsize=maxsize)
        self._subscription_id: str | None = None

    async def __aenter__(self) -> QueueSubscriber:
        self._subscription_id = await self.bus.subscribe(self._on_event, filter=self.filter)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._subscription_id is not None:
            await self.bus.unsubscribe(self._subscription_id)
            self._subscription_id = None

    def _on_event(self, event: NewswireEvent) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            NEWSWIRE_BUS_DROPPED.labels(reason="slow_subscriber").inc()

    async def get(self) -> NewswireEvent:
        return await self.queue.get()
