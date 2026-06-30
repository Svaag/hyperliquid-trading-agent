"""Supervisor that turns adapters into the live, persisted, queryable stream.

For each adapter it runs ``adapter.run()`` as a task and pipes every event
through one path: in-memory dedupe -> rolling aggregator + recent tape -> bus
fan-out -> durable store. The bus feeds the SSE/WebSocket browser tape and the
observe-only agent bridge; the in-memory tape + aggregator answer the public API
even when Postgres is absent (so the subsystem is fully exercisable offline).
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.liquidations.adapters.base import LiquidationAdapter
from hyperliquid_trading_agent.app.liquidations.adapters.replay import SyntheticDemoAdapter
from hyperliquid_trading_agent.app.liquidations.aggregator import WINDOWS_MS, RollingAggregator
from hyperliquid_trading_agent.app.liquidations.bus import LiquidationBus
from hyperliquid_trading_agent.app.liquidations.models import LiquidationEvent
from hyperliquid_trading_agent.app.liquidations.reconcile import reconcile
from hyperliquid_trading_agent.app.liquidations.store import LiquidationStore
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.metrics import (
    LIQUIDATION_DEDUPED,
    LIQUIDATION_EVENTS,
    LIQUIDATION_NOTIONAL,
    LIQUIDATION_RECONCILE_CONFIRMED_COVERAGE,
    LIQUIDATION_RECONCILE_MATCH_RATE,
    LIQUIDATION_RECONCILE_NOTIONAL_DELTA,
)

log = get_logger(__name__)


class LiquidationService:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession] | None,
        *,
        adapters: list[LiquidationAdapter] | None = None,
    ) -> None:
        self.settings = settings
        self.store = LiquidationStore(sessionmaker)
        self.bus = LiquidationBus()
        self.aggregator = RollingAggregator()
        self.adapters: list[LiquidationAdapter] = adapters if adapters is not None else self._build_adapters(settings)
        self._recent: deque[LiquidationEvent] = deque(maxlen=max(100, settings.liquidations_recent_buffer))
        self._seen: set[str] = set()
        self._seen_order: deque[str] = deque(maxlen=max(1000, settings.liquidations_recent_buffer * 4))
        self._tasks: list[asyncio.Task[None]] = []
        self._running = False

    def _build_adapters(self, settings: Settings) -> list[LiquidationAdapter]:
        adapters: list[LiquidationAdapter] = []
        # Venue adapters register behind their own flags (lazy import so a disabled
        # venue never pulls its deps). The HL gRPC confirmed source stays a Phase-2
        # opt-in stub and is not auto-registered.
        if settings.liquidations_aster_enabled:
            from hyperliquid_trading_agent.app.liquidations.adapters.aster_ws import AsterAdapter

            adapters.append(AsterAdapter(settings))
        if settings.liquidations_lighter_enabled:
            from hyperliquid_trading_agent.app.liquidations.adapters.lighter_ws import LighterAdapter

            adapters.append(LighterAdapter(settings))
        if settings.liquidations_hl_public_enabled:
            from hyperliquid_trading_agent.app.liquidations.adapters.hyperliquid_public_ws import (
                HyperliquidPublicAdapter,
            )

            adapters.append(HyperliquidPublicAdapter(settings))
        if settings.liquidations_hl_user_enabled:
            from hyperliquid_trading_agent.app.liquidations.adapters.hyperliquid_user_events import (
                HyperliquidUserEventsAdapter,
            )

            adapters.append(HyperliquidUserEventsAdapter(settings))
        if settings.liquidations_hl_grpc_enabled:
            from hyperliquid_trading_agent.app.liquidations.adapters.hyperliquid_grpc import HyperliquidGrpcAdapter

            adapters.append(HyperliquidGrpcAdapter(settings))
        if settings.liquidations_demo_enabled:
            adapters.append(SyntheticDemoAdapter())
        return adapters

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        for adapter in self.adapters:
            self._tasks.append(asyncio.create_task(self._pump(adapter), name=f"liq-adapter-{adapter.source}"))
        log.info("liquidation_service_started", adapters=[a.source for a in self.adapters])

    async def _pump(self, adapter: LiquidationAdapter) -> None:
        try:
            async for event in adapter.run():
                await self._handle(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - adapter.run already guards reconnect
            log.warning("liquidation_pump_failed", adapter=adapter.source, error=type(exc).__name__)

    async def _handle(self, event: LiquidationEvent) -> None:
        if event.event_id in self._seen:
            LIQUIDATION_DEDUPED.labels(venue=str(event.venue), reason="duplicate").inc()
            return
        self._seen.add(event.event_id)
        if len(self._seen_order) == self._seen_order.maxlen:
            self._seen.discard(self._seen_order[0])
        self._seen_order.append(event.event_id)

        self.aggregator.record(event)
        self._recent.append(event)
        LIQUIDATION_EVENTS.labels(
            venue=str(event.venue), source_integrity=str(event.source_integrity), event_type=str(event.event_type)
        ).inc()
        if event.notional_usd is not None and event.is_execution:
            LIQUIDATION_NOTIONAL.labels(
                venue=str(event.venue), source_integrity=str(event.source_integrity), side=event.liquidated_side
            ).inc(float(event.notional_usd))

        await self.bus.publish(event)
        await self.store.persist(event)
        await self.store.upsert_adapter_state(
            event.source, status="streaming", updated_at_ms=event.received_at_ms, last_event_ms=event.timestamp_ms
        )

    async def stop(self) -> None:
        self._running = False
        for adapter in self.adapters:
            adapter.stop()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        log.info("liquidation_service_stopped")

    # ------------------------------------------------------------------ reads

    def recent(
        self,
        *,
        limit: int = 100,
        venue: str | None = None,
        symbol: str | None = None,
        min_notional: float | None = None,
        source_integrity: str | None = None,
    ) -> list[dict[str, Any]]:
        """Newest-first tape from the in-memory buffer (public projection)."""
        limit = max(1, min(limit, 1000))
        symbol_u = symbol.upper() if symbol else None
        out: list[dict[str, Any]] = []
        for event in reversed(self._recent):
            if venue and str(event.venue) != venue:
                continue
            if symbol_u and event.symbol != symbol_u:
                continue
            if source_integrity and str(event.source_integrity) != source_integrity:
                continue
            if min_notional is not None and (event.notional_usd is None or float(event.notional_usd) < min_notional):
                continue
            out.append(event.public_view())
            if len(out) >= limit:
                break
        return out

    def summary(self, now_ms: int) -> dict[str, Any]:
        return self.aggregator.summary(now_ms)

    def venues(self, now_ms: int) -> list[dict[str, Any]]:
        """Per-adapter health + integrity badge data."""
        stale_after_ms = 120_000
        out: list[dict[str, Any]] = []
        for adapter in self.adapters:
            health = adapter.health()
            last = health.get("last_event_ms")
            health["stale"] = bool(last is not None and now_ms - int(last) > stale_after_ms)
            out.append(health)
        return out

    async def get_event(self, event_id: str) -> dict[str, Any] | None:
        stored = await self.store.get_event(event_id)
        if stored is not None:
            return stored
        for event in self._recent:
            if event.event_id == event_id:
                return event.model_dump(mode="json")
        return None

    def reconcile_report(self, now_ms: int) -> dict[str, Any]:
        """Derived-vs-confirmed reconciliation over the live HL tape + gauge sync.

        Pull-based: gauges refresh when the report is computed. Honest by default —
        with no confirmed source the report shows ``confirmed_coverage: 0.0`` and
        ``confirmed_source: "not_configured"``.
        """
        report = reconcile(
            list(self._recent),
            bucket_ms=self.settings.liquidations_reconcile_bucket_ms,
            window_ms=self.settings.liquidations_reconcile_window_ms,
            now_ms=now_ms,
            confirmed_source=self._confirmed_source_label(),
        )
        LIQUIDATION_RECONCILE_CONFIRMED_COVERAGE.set(report["confirmed_coverage"])
        for symbol, stats in report["by_symbol"].items():
            derived = stats["derived_buckets"]
            match_rate = stats["matched_buckets"] / derived if derived else 0.0
            LIQUIDATION_RECONCILE_MATCH_RATE.labels(symbol=symbol).set(match_rate)
            LIQUIDATION_RECONCILE_NOTIONAL_DELTA.labels(symbol=symbol).set(
                stats["confirmed_notional_usd"] - stats["derived_notional_usd"]
            )
        return report

    def _confirmed_source_label(self) -> str:
        settings = self.settings
        if settings.liquidations_hl_grpc_enabled and settings.hl_grpc_endpoint:
            return settings.hl_grpc_provider or "grpc"
        if settings.liquidations_hl_user_enabled and settings.hl_watch_address_list:
            return "account_private"
        return "not_configured"

    def subscribe(self) -> Any:
        return self.bus.subscribe()

    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "store_enabled": self.store.enabled,
            "adapters": [a.source for a in self.adapters],
            "recent_buffered": len(self._recent),
            "bus_subscribers": self.bus.subscriber_count,
            "windows_ms": WINDOWS_MS,
        }
