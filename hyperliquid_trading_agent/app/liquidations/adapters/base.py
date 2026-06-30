"""The adapter boundary: every venue is consumed through this one contract.

`run()` is a long-lived async generator of normalized `LiquidationEvent`s. The
base class owns the reconnect/backoff loop and health bookkeeping (mirroring the
`HyperliquidWebSocketWorker` shape) so each concrete adapter only implements
`_connect_and_stream()` — one connection's worth of decode+normalize+yield.

A `NotConfigured` adapter (e.g. the Hyperliquid gRPC stub) raises from
`_connect_and_stream`; the supervisor logs it once and leaves the source dark
rather than crash-looping.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass

from hyperliquid_trading_agent.app.liquidations.models import LiquidationEvent, SourceIntegrity
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.metrics import (
    LIQUIDATION_ADAPTER_ERRORS,
    LIQUIDATION_ADAPTER_RECONNECTS,
    LIQUIDATION_ADAPTER_UP,
)

log = get_logger(__name__)


class NotConfigured(RuntimeError):
    """Raised by an adapter that is wired but intentionally not enabled yet."""


@dataclass
class AdapterHealth:
    connected: bool = False
    last_event_ms: int | None = None
    reconnects: int = 0
    events_total: int = 0
    error: str | None = None


class LiquidationAdapter(ABC):
    venue: str = "other"
    source: str = "adapter"
    source_integrity: SourceIntegrity = SourceIntegrity.DERIVED

    #: Max backoff between reconnect attempts (seconds).
    max_backoff_s: float = 30.0

    def __init__(self) -> None:
        self._health = AdapterHealth()
        self._stop = asyncio.Event()

    @abstractmethod
    def _connect_and_stream(self) -> AsyncIterator[LiquidationEvent]:
        """Yield events for the lifetime of a single connection.

        Returning normally (socket closed) triggers a reconnect; raising triggers
        a reconnect with backoff. Raise `NotConfigured` to stay dark.
        """
        raise NotImplementedError

    async def run(self) -> AsyncIterator[LiquidationEvent]:
        backoff = 1.0
        while not self._stop.is_set():
            self._health.connected = True
            self._health.error = None
            LIQUIDATION_ADAPTER_UP.labels(adapter=self.source).set(1)
            got_event = False
            try:
                async for event in self._connect_and_stream():
                    got_event = True
                    backoff = 1.0
                    self._health.last_event_ms = event.received_at_ms
                    self._health.events_total += 1
                    yield event
            except NotConfigured as exc:
                self._health.error = "not_configured"
                log.info("liquidation_adapter_not_configured", adapter=self.source, reason=str(exc))
                LIQUIDATION_ADAPTER_UP.labels(adapter=self.source).set(0)
                self._health.connected = False
                return  # stay dark; do not crash-loop
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - external connection behavior
                self._health.error = type(exc).__name__
                self._health.reconnects += 1
                LIQUIDATION_ADAPTER_RECONNECTS.labels(adapter=self.source).inc()
                LIQUIDATION_ADAPTER_ERRORS.labels(adapter=self.source, error=type(exc).__name__).inc()
                log.warning("liquidation_adapter_reconnect", adapter=self.source, error=type(exc).__name__)
            finally:
                self._health.connected = False
                LIQUIDATION_ADAPTER_UP.labels(adapter=self.source).set(0)
            if self._stop.is_set():
                break
            sleep_for = 0.0 if got_event else backoff
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(sleep_for, 0.01))
            except TimeoutError:
                pass
            backoff = min(backoff * 2, self.max_backoff_s)

    def stop(self) -> None:
        self._stop.set()

    def health(self) -> dict:
        return {
            "adapter": self.source,
            "venue": self.venue,
            "source_integrity": str(self.source_integrity),
            **asdict(self._health),
        }
