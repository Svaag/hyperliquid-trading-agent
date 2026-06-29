"""In-memory rolling aggregates over the liquidation stream.

Fed one event at a time from the bus; answers `/api/summary` (windowed notional
by venue/symbol/side + integrity mix, plus a bucketed series for the shape
chart) and the observe-only `LiquidationSignal` for the agent. Confirmed
executions only — inferred ``liquidation_pressure`` is tracked separately so it
never inflates the "how much was liquidated" headline.

Single-threaded (asyncio): all methods are synchronous and never await between
mutations, so no locking is needed. State is ephemeral — a restart re-warms from
the live stream within one window; durable history lives in the store.
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal
from typing import Any

from hyperliquid_trading_agent.app.liquidations.models import LiquidationEvent, LiquidationSignal

WINDOWS_MS: dict[str, int] = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}
_MAX_WINDOW_MS = max(WINDOWS_MS.values())
_TOP_SYMBOLS = 10


class _Entry:
    __slots__ = ("integrity", "is_execution", "notional", "side", "symbol", "ts", "venue")

    def __init__(self, event: LiquidationEvent) -> None:
        self.ts = event.timestamp_ms
        self.venue = str(event.venue)
        self.symbol = event.symbol
        self.side = event.liquidated_side
        self.integrity = str(event.source_integrity)
        self.notional = float(event.notional_usd) if event.notional_usd is not None else 0.0
        self.is_execution = event.is_execution


class RollingAggregator:
    def __init__(self, max_window_ms: int = _MAX_WINDOW_MS, max_entries: int = 200_000) -> None:
        self._max_window_ms = max_window_ms
        self._entries: deque[_Entry] = deque(maxlen=max_entries)

    def record(self, event: LiquidationEvent) -> None:
        self._entries.append(_Entry(event))

    def _evict(self, now_ms: int) -> None:
        cutoff = now_ms - self._max_window_ms
        while self._entries and self._entries[0].ts < cutoff:
            self._entries.popleft()

    def _window_entries(self, now_ms: int, window_ms: int, *, executions_only: bool = True) -> list[_Entry]:
        cutoff = now_ms - window_ms
        return [e for e in self._entries if e.ts >= cutoff and (e.is_execution or not executions_only)]

    def summary(self, now_ms: int) -> dict[str, Any]:
        self._evict(now_ms)
        windows: dict[str, Any] = {}
        for name, window_ms in WINDOWS_MS.items():
            entries = self._window_entries(now_ms, window_ms)
            windows[name] = _window_stats(entries)
        return {
            "as_of_ms": now_ms,
            "windows": windows,
            "series": self.series(now_ms, window_ms=WINDOWS_MS["1h"], bucket_ms=60_000),
        }

    def series(self, now_ms: int, *, window_ms: int, bucket_ms: int) -> list[dict[str, Any]]:
        """Time-bucketed long/short/total notional for the shape chart."""
        cutoff = now_ms - window_ms
        n_buckets = max(1, window_ms // bucket_ms)
        buckets: list[dict[str, Any]] = [
            {"t": cutoff + i * bucket_ms, "long": 0.0, "short": 0.0, "total": 0.0, "count": 0} for i in range(n_buckets)
        ]
        for e in self._entries:
            if e.ts < cutoff or not e.is_execution:
                continue
            idx = min(n_buckets - 1, (e.ts - cutoff) // bucket_ms)
            b = buckets[idx]
            if e.side == "long":
                b["long"] += e.notional
            elif e.side == "short":
                b["short"] += e.notional
            b["total"] += e.notional
            b["count"] += 1
        return buckets

    def signal(self, now_ms: int, *, venue: str, symbol: str, window_ms: int) -> LiquidationSignal:
        """Observe-only signal for the agent — never used to loosen risk."""
        entries = [
            e
            for e in self._window_entries(now_ms, window_ms)
            if (venue == "all" or e.venue == venue) and e.symbol == symbol.upper()
        ]
        long_usd = sum(e.notional for e in entries if e.side == "long")
        short_usd = sum(e.notional for e in entries if e.side == "short")
        max_single = max((e.notional for e in entries), default=0.0)
        source_mix: dict[str, int] = {}
        confirmed_notional = 0.0
        total_notional = 0.0
        for e in entries:
            source_mix[e.integrity] = source_mix.get(e.integrity, 0) + 1
            total_notional += e.notional
            if e.integrity in ("confirmed", "verifiable"):
                confirmed_notional += e.notional
        confidence = Decimal(str(confirmed_notional / total_notional)) if total_notional > 0 else Decimal("0")
        return LiquidationSignal(
            venue=venue,
            symbol=symbol.upper(),
            window_ms=window_ms,
            long_liq_notional_usd=Decimal(str(long_usd)),
            short_liq_notional_usd=Decimal(str(short_usd)),
            net_liq_imbalance_usd=Decimal(str(long_usd - short_usd)),
            max_single_liq_usd=Decimal(str(max_single)),
            event_count=len(entries),
            source_mix=source_mix,
            confidence=confidence,
            as_of_ms=now_ms,
        )


def _window_stats(entries: list[_Entry]) -> dict[str, Any]:
    total = long_usd = short_usd = 0.0
    by_venue: dict[str, float] = {}
    by_symbol: dict[str, float] = {}
    integrity_mix: dict[str, int] = {}
    max_single: dict[str, Any] | None = None
    for e in entries:
        total += e.notional
        if e.side == "long":
            long_usd += e.notional
        elif e.side == "short":
            short_usd += e.notional
        by_venue[e.venue] = by_venue.get(e.venue, 0.0) + e.notional
        by_symbol[e.symbol] = by_symbol.get(e.symbol, 0.0) + e.notional
        integrity_mix[e.integrity] = integrity_mix.get(e.integrity, 0) + 1
        if max_single is None or e.notional > max_single["notional_usd"]:
            max_single = {"venue": e.venue, "symbol": e.symbol, "side": e.side, "notional_usd": e.notional}
    top_symbols = dict(sorted(by_symbol.items(), key=lambda kv: kv[1], reverse=True)[:_TOP_SYMBOLS])
    return {
        "count": len(entries),
        "total_notional_usd": total,
        "long_notional_usd": long_usd,
        "short_notional_usd": short_usd,
        "by_venue": by_venue,
        "by_symbol": top_symbols,
        "integrity_mix": integrity_mix,
        "max_single": max_single,
    }
