"""Non-venue adapters: deterministic replay (tests) and a synthetic demo source.

`ReplayAdapter` feeds a fixed list of pre-built events through the exact same
boundary the live venues use, so the whole pipeline (dedupe -> store -> bus ->
aggregator -> API/SSE) can be exercised with no network and no Postgres.

`SyntheticDemoAdapter` generates plausible-but-fake events for local screenshots
only. It is OFF by default and labels everything ``derived`` /
``liquidation_pressure`` so demo data can never be mistaken for confirmed
liquidations on the public surface.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from collections.abc import AsyncIterator, Iterable

from hyperliquid_trading_agent.app.liquidations.adapters.base import LiquidationAdapter
from hyperliquid_trading_agent.app.liquidations.models import EventType, LiquidationEvent, SourceIntegrity
from hyperliquid_trading_agent.app.metrics import LIQUIDATION_ADAPTER_UP


class ReplayAdapter(LiquidationAdapter):
    venue = "other"
    source = "replay"
    source_integrity = SourceIntegrity.DERIVED

    def __init__(self, events: Iterable[LiquidationEvent], *, delay_s: float = 0.0, loop: bool = False) -> None:
        super().__init__()
        self._events = list(events)
        self._delay = delay_s
        self._loop = loop

    async def _connect_and_stream(self) -> AsyncIterator[LiquidationEvent]:
        for event in self._events:
            if self._stop.is_set():
                return
            yield event
            if self._delay:
                await asyncio.sleep(self._delay)

    async def run(self) -> AsyncIterator[LiquidationEvent]:
        # Finite by default — don't use the base reconnect loop (which would
        # replay the fixed list forever).
        while not self._stop.is_set():
            self._health.connected = True
            LIQUIDATION_ADAPTER_UP.labels(adapter=self.source).set(1)
            async for event in self._connect_and_stream():
                self._health.last_event_ms = event.received_at_ms
                self._health.events_total += 1
                yield event
            self._health.connected = False
            LIQUIDATION_ADAPTER_UP.labels(adapter=self.source).set(0)
            if not self._loop:
                return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1.0)
            except TimeoutError:
                pass


_DEMO_PRICES = {"BTC": 61000.0, "ETH": 3050.0, "SOL": 152.0, "HYPE": 34.0, "DOGE": 0.21, "XRP": 2.15}
# (venue, source_integrity, event_type, weight) — previews each venue's real
# liquidation-visibility model so the demo is a faithful badge/taxonomy preview.
_DEMO_SOURCES: tuple[tuple[str, SourceIntegrity, EventType, int], ...] = (
    ("lighter", SourceIntegrity.CONFIRMED, EventType.LIQUIDATION, 7),
    ("lighter", SourceIntegrity.CONFIRMED, EventType.DELEVERAGE, 1),
    ("aster", SourceIntegrity.SNAPSHOT_THROTTLED, EventType.LIQUIDATION, 5),
    ("hyperliquid", SourceIntegrity.DERIVED, EventType.LIQUIDATION_PRESSURE, 5),
    ("hyperliquid", SourceIntegrity.ACCOUNT_PRIVATE, EventType.LIQUIDATION, 2),
)


class SyntheticDemoAdapter(LiquidationAdapter):
    """Local-only fake feed. Never enable on a public deployment.

    Emits each venue's real ``source_integrity`` so the demo accurately previews
    the badge taxonomy and the confirmed-vs-pressure honesty split.
    """

    venue = "other"
    source = "synthetic_demo"
    source_integrity = SourceIntegrity.DERIVED

    def __init__(self, *, rate_per_s: float = 4.0, seed: int | None = None) -> None:
        super().__init__()
        self._interval = 1.0 / max(rate_per_s, 0.1)
        self._rng = random.Random(seed)
        self._weights = [w for *_, w in _DEMO_SOURCES]

    async def _connect_and_stream(self) -> AsyncIterator[LiquidationEvent]:
        while not self._stop.is_set():
            now_ms = int(time.time() * 1000)
            venue, integrity, event_type, _ = self._rng.choices(_DEMO_SOURCES, weights=self._weights, k=1)[0]
            symbol = self._rng.choice(list(_DEMO_PRICES))
            side = self._rng.choice(("long", "short"))
            price = _DEMO_PRICES[symbol] * self._rng.uniform(0.98, 1.02)
            # Heavy-tailed notional so the "biggest single" panel has something to show.
            notional = math.exp(self._rng.uniform(math.log(500), math.log(400_000)))
            size = notional / price
            is_hl_user = integrity == SourceIntegrity.ACCOUNT_PRIVATE
            yield LiquidationEvent(
                venue=venue,  # type: ignore[arg-type]
                source=self.source,
                source_integrity=integrity,
                event_type=event_type,
                symbol=symbol,
                liquidated_side=side,  # type: ignore[arg-type]
                price=_as_decimal(round(price, 4)),
                size_base=_as_decimal(round(size, 6)),
                timestamp_ms=now_ms,
                received_at_ms=now_ms,
                method=("backstop" if event_type == EventType.BACKSTOP else "market") if venue == "hyperliquid" else None,
                liquidated_user=(f"0x{self._rng.randrange(16**16):016x}" if is_hl_user else None),
                confidence=_as_decimal(0.25 if event_type == EventType.LIQUIDATION_PRESSURE else 1.0),
                raw={"demo": True, "nonce": self._rng.random()},
            )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                pass


def _as_decimal(value: float):
    from decimal import Decimal

    return Decimal(str(value))
