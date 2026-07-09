"""Observe-only signal bridge for the trading agent.

The agent never touches raw venue sockets — it asks this bridge for normalized,
read-only `LiquidationSignal`s and scalar features. By contract these may inform
annotations, alerts, and paper-trading correlations only; they must never be
used to loosen risk, raise leverage, or change sizing. Any defensive action
(tighten/halt) must still route through the existing `RiskGateway`.
"""

from __future__ import annotations

import time
from typing import Any

from hyperliquid_trading_agent.app.liquidations.aggregator import WINDOWS_MS
from hyperliquid_trading_agent.app.liquidations.models import LiquidationSignal
from hyperliquid_trading_agent.app.liquidations.service import LiquidationService


class LiquidationSignalBridge:
    """Read-only facade over the live aggregator. No control-plane methods."""

    def __init__(self, service: LiquidationService) -> None:
        self._service = service

    def signal(self, symbol: str, *, venue: str = "all", window: str = "5m") -> LiquidationSignal:
        window_ms = WINDOWS_MS.get(window, WINDOWS_MS["5m"])
        return self._service.aggregator.signal(_now_ms(), venue=venue, symbol=symbol, window_ms=window_ms)

    def named_signals(self, symbol: str, *, venue: str = "all") -> dict[str, Any]:
        """Flat scalar features keyed for easy consumption by the agent."""
        now = _now_ms()
        agg = self._service.aggregator
        s1 = agg.signal(now, venue=venue, symbol=symbol, window_ms=WINDOWS_MS["1m"])
        s5 = agg.signal(now, venue=venue, symbol=symbol, window_ms=WINDOWS_MS["5m"])
        return {
            "symbol": symbol.upper(),
            "venue": venue,
            "as_of_ms": now,
            "liq_notional_1m": float(s1.long_liq_notional_usd + s1.short_liq_notional_usd),
            "liq_notional_5m": float(s5.long_liq_notional_usd + s5.short_liq_notional_usd),
            "long_vs_short_liq_imbalance_5m": float(s5.net_liq_imbalance_usd),
            "largest_single_liq_5m": float(s5.max_single_liq_usd),
            "confirmed_only_liq_score_5m": float(s5.confidence),
            "source_mix_5m": s5.source_mix,
            "event_count_5m": s5.event_count,
        }


class StoreBackedLiquidationSignalBridge:
    """Read-only DB-backed bridge for processes without the live aggregator.

    Computes the same 1m/5m execution-only aggregates from persisted
    ``liquidation_events`` (written by the liquidations service role). Returns
    ``None`` when the feed looks dead so consumers never mistake an outage for
    a genuinely quiet market.
    """

    def __init__(self, store: Any, *, max_feed_age_ms: int = 600_000) -> None:
        self._store = store
        self.max_feed_age_ms = max(60_000, int(max_feed_age_ms))

    async def named_signals(self, symbol: str, *, venue: str = "all") -> dict[str, Any] | None:
        now = _now_ms()
        last_update_ms = await self._store.latest_adapter_update_ms()
        if not last_update_ms or now - int(last_update_ms) > self.max_feed_age_ms:
            return None
        s1 = await self._store.window_signal(symbol=symbol, venue=venue, window_ms=WINDOWS_MS["1m"], now_ms=now)
        s5 = await self._store.window_signal(symbol=symbol, venue=venue, window_ms=WINDOWS_MS["5m"], now_ms=now)
        return {
            "symbol": symbol.upper(),
            "venue": venue,
            "as_of_ms": now,
            "liq_notional_1m": float(s1["long_usd"] + s1["short_usd"]),
            "liq_notional_5m": float(s5["long_usd"] + s5["short_usd"]),
            "long_vs_short_liq_imbalance_5m": float(s5["long_usd"] - s5["short_usd"]),
            "largest_single_liq_5m": float(s5["max_single_usd"]),
            "confirmed_only_liq_score_5m": float(s5["confidence"]),
            "source_mix_5m": s5["source_mix"],
            "event_count_5m": s5["event_count"],
        }


def _now_ms() -> int:
    return int(time.time() * 1000)
