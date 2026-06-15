from __future__ import annotations

import asyncio
from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.metrics import (
    NEWSWIRE_ADAPTER_RECONNECTS,
    NEWSWIRE_ADAPTER_UP,
    NEWSWIRE_BUS_DROPPED,
    NEWSWIRE_EVENTS,
)
from hyperliquid_trading_agent.app.newswire.adapters.base import NewswireAdapter
from hyperliquid_trading_agent.app.newswire.adapters.rss import RssAdapter
from hyperliquid_trading_agent.app.newswire.bus import InProcessNewswireBus, NewswireBus
from hyperliquid_trading_agent.app.newswire.normalize import normalize, now_ms
from hyperliquid_trading_agent.app.newswire.riskgate import HaltStateGate
from hyperliquid_trading_agent.app.newswire.schemas import NewswireEvent, NewswireFilter, RawNewsItem

log = get_logger(__name__)


class NewswireService:
    """Free-standing ingestion gateway: supervises adapters, normalizes + scores + gates
    deterministically, then publishes canonical events to the bus and persists them."""

    def __init__(self, *, settings: Settings, repository: Any | None = None, bus: NewswireBus | None = None):
        self.settings = settings
        self.repository = repository
        self.bus: NewswireBus = bus or InProcessNewswireBus()
        self.halt_gate = HaltStateGate()
        self.adapters: list[NewswireAdapter] = []
        self.running = False
        self._tasks: list[asyncio.Task] = []
        self._by_id: dict[str, NewswireEvent] = {}
        self._symbols_universe = settings.newswire_symbols_universe
        self.last_event_at_ms: int | None = None
        self.last_event_per_source: dict[str, int] = {}
        self.adapter_errors = 0

    def build_adapters(self) -> list[NewswireAdapter]:
        adapters: list[NewswireAdapter] = []
        settings = self.settings
        if settings.newswire_rss_feed_urls:
            adapters.append(RssAdapter(settings.newswire_rss_feed_urls, poll_seconds=settings.newswire_rss_poll_seconds))
        if settings.alpaca_news_enabled and settings.alpaca_api_key and settings.alpaca_api_secret:
            from hyperliquid_trading_agent.app.newswire.adapters.alpaca_ws import AlpacaNewsAdapter

            adapters.append(
                AlpacaNewsAdapter(
                    ws_url=settings.alpaca_news_ws_url,
                    api_key=settings.alpaca_api_key,
                    api_secret=settings.alpaca_api_secret,
                    symbols=settings.alpaca_news_symbol_list,
                )
            )
        if settings.trading_economics_enabled and settings.trading_economics_api_key:
            from hyperliquid_trading_agent.app.newswire.adapters.trading_economics_ws import TradingEconomicsAdapter

            adapters.append(TradingEconomicsAdapter(ws_url=settings.trading_economics_ws_url, api_key=settings.trading_economics_api_key))
        if settings.x_newswire_enabled and settings.x_bearer_token:
            from hyperliquid_trading_agent.app.newswire.adapters.x_curated import XCuratedAdapter

            adapters.append(XCuratedAdapter(settings=settings))
        return adapters

    async def start(self) -> None:
        if not self.settings.newswire_enabled or self.running:
            return
        self.running = True
        self.adapters = self.build_adapters()
        for adapter in self.adapters:
            self._tasks.append(asyncio.create_task(self._supervise(adapter), name=f"newswire-{adapter.name}"))
        log.info("newswire_started", adapters=[a.name for a in self.adapters])

    async def stop(self) -> None:
        self.running = False
        for adapter in self.adapters:
            try:
                await adapter.stop()
            except Exception:  # pragma: no cover - adapter cleanup best-effort
                pass
        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []

    async def _supervise(self, adapter: NewswireAdapter) -> None:
        backoff = 5
        while self.running:
            NEWSWIRE_ADAPTER_UP.labels(adapter=adapter.name).set(1)
            try:
                await adapter.run(self._ingest)
                break  # clean return = stop requested
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - external source behavior
                self.adapter_errors += 1
                NEWSWIRE_ADAPTER_UP.labels(adapter=adapter.name).set(0)
                NEWSWIRE_ADAPTER_RECONNECTS.labels(adapter=adapter.name).inc()
                log.warning("newswire_adapter_restart", adapter=adapter.name, error=type(exc).__name__)
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
                backoff = min(60, backoff * 2)
            else:
                backoff = 5
        NEWSWIRE_ADAPTER_UP.labels(adapter=adapter.name).set(0)

    async def _ingest(self, raw: RawNewsItem) -> NewswireEvent | None:
        event = normalize(raw, symbols_universe=self._symbols_universe, received_at_ms=now_ms())
        if event is None:
            return None
        if event.action == "created" and event.event_id in self._by_id:
            NEWSWIRE_BUS_DROPPED.labels(reason="duplicate").inc()
            return None
        event = self.halt_gate.apply(event)
        self._index(event)
        NEWSWIRE_EVENTS.labels(provider=event.provider).inc()
        if self.repository is not None and getattr(self.repository, "enabled", False):
            await self.repository.record_newswire_event(event.model_dump(mode="json"))
        await self.bus.publish(event)
        return event

    def _index(self, event: NewswireEvent) -> None:
        self._by_id.pop(event.event_id, None)  # move-to-end on update
        self._by_id[event.event_id] = event
        cap = max(1, self.settings.newswire_max_events_buffer)
        while len(self._by_id) > cap:
            oldest = next(iter(self._by_id))
            self._by_id.pop(oldest, None)
        self.last_event_at_ms = event.received_at_ms
        self.last_event_per_source[event.source] = event.received_at_ms

    # --- query surface for the HTTP gateway ---------------------------------

    def get_event(self, event_id: str) -> NewswireEvent | None:
        return self._by_id.get(event_id)

    def list_events(self, *, filter: NewswireFilter | None = None, limit: int = 100) -> list[NewswireEvent]:
        events = list(reversed(self._by_id.values()))
        if filter is not None:
            events = [event for event in events if filter.matches(event)]
        return events[:limit]

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.newswire_enabled,
            "running": self.running,
            "adapters": [adapter.status() for adapter in self.adapters],
            "adapter_errors": self.adapter_errors,
            "buffered_events": len(self._by_id),
            "last_event_at_ms": self.last_event_at_ms,
            "last_event_per_source": self.last_event_per_source,
            "halted_symbols": self.halt_gate.halted_symbols(),
            "bus": self.bus.status(),
        }
