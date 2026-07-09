from __future__ import annotations

import time
from typing import Any

import anyio

from hyperliquid_trading_agent.app.liquidations.signals import StoreBackedLiquidationSignalBridge


class FakeLiquidationStore:
    def __init__(self, *, last_update_ms: int | None, signals: dict[int, dict[str, Any]] | None = None):
        self.last_update_ms = last_update_ms
        self.signals = signals or {}
        self.calls: list[dict[str, Any]] = []

    async def latest_adapter_update_ms(self) -> int | None:
        return self.last_update_ms

    async def window_signal(self, *, symbol: str, window_ms: int, now_ms: int, venue: str = "all") -> dict[str, Any]:
        self.calls.append({"symbol": symbol, "window_ms": window_ms, "venue": venue})
        return self.signals.get(
            window_ms,
            {"long_usd": 0.0, "short_usd": 0.0, "max_single_usd": 0.0, "confidence": 0.0, "source_mix": {}, "event_count": 0},
        )


def test_store_backed_bridge_emits_aggregator_compatible_named_signals():
    now_ms = int(time.time() * 1000)
    store = FakeLiquidationStore(
        last_update_ms=now_ms - 5_000,
        signals={
            60_000: {"long_usd": 10_000.0, "short_usd": 4_000.0, "max_single_usd": 8_000.0, "confidence": 1.0, "source_mix": {"confirmed": 2}, "event_count": 2},
            300_000: {"long_usd": 50_000.0, "short_usd": 20_000.0, "max_single_usd": 30_000.0, "confidence": 0.8, "source_mix": {"confirmed": 3, "derived": 1}, "event_count": 4},
        },
    )
    bridge = StoreBackedLiquidationSignalBridge(store)

    payload = anyio.run(bridge.named_signals, "btc")

    assert payload is not None
    assert payload["symbol"] == "BTC"
    assert payload["liq_notional_1m"] == 14_000.0
    assert payload["liq_notional_5m"] == 70_000.0
    assert payload["long_vs_short_liq_imbalance_5m"] == 30_000.0
    assert payload["largest_single_liq_5m"] == 30_000.0
    assert payload["confirmed_only_liq_score_5m"] == 0.8
    assert payload["source_mix_5m"] == {"confirmed": 3, "derived": 1}
    assert payload["event_count_5m"] == 4
    assert [call["window_ms"] for call in store.calls] == [60_000, 300_000]


def test_store_backed_bridge_returns_none_when_feed_is_stale_or_absent():
    now_ms = int(time.time() * 1000)

    stale = StoreBackedLiquidationSignalBridge(FakeLiquidationStore(last_update_ms=now_ms - 3_600_000))
    assert anyio.run(stale.named_signals, "BTC") is None

    absent = StoreBackedLiquidationSignalBridge(FakeLiquidationStore(last_update_ms=None))
    assert anyio.run(absent.named_signals, "BTC") is None
