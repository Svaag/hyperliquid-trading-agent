"""TradFiClient — facade over one or more TradFiProviders with TTL cache + rate guard."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.tradfi.base import TradFiProvider
from hyperliquid_trading_agent.app.tradfi.schemas import (
    BAR_TIMEFRAMES,
    Bar,
    CalendarEvent,
    CorporateAction,
    OptionContract,
    OptionsChain,
    StockQuote,
    StockSnapshot,
    StockTrade,
)

log = get_logger(__name__)

# Minimum interval between requests to the same provider (ms).
_RATE_GUARD_MS = 200


class TradFiClient:
    """Cached, rate-guarded facade over a TradFi provider.

    Mirrors the pattern used by ``HyperliquidClient``: TTL cache per method,
    process-local rate guard, and transparent error handling.
    """

    def __init__(
        self,
        provider: TradFiProvider,
        *,
        cache_ttl_quote_seconds: int = 5,
        cache_ttl_snapshot_seconds: int = 10,
        cache_ttl_bars_seconds: int = 30,
        cache_ttl_chain_seconds: int = 30,
        cache_ttl_corp_seconds: int = 300,
        cache_ttl_calendar_seconds: int = 3600,
    ):
        self._provider = provider
        self._ttls: dict[str, int] = {
            "quote": cache_ttl_quote_seconds,
            "trade": cache_ttl_quote_seconds,
            "snapshot": cache_ttl_snapshot_seconds,
            "bars": cache_ttl_bars_seconds,
            "chain": cache_ttl_chain_seconds,
            "option_snap": cache_ttl_quote_seconds,
            "corp": cache_ttl_corp_seconds,
            "calendar": cache_ttl_calendar_seconds,
        }
        self._cache: dict[str, tuple[float, Any]] = {}
        self._last_request_ms: float = 0
        self._lock = asyncio.Lock()

    @property
    def provider(self) -> TradFiProvider:
        return self._provider

    async def start(self) -> None:
        await self._provider.start()

    async def close(self) -> None:
        await self._provider.close()

    # --- Quotes & Trades --------------------------------------------------------

    async def get_latest_quote(self, symbol: str) -> StockQuote | None:
        return await self._cached(f"quote:{symbol.upper()}", "quote", lambda: self._provider.get_latest_quote(symbol))

    async def get_latest_trade(self, symbol: str) -> StockTrade | None:
        return await self._cached(f"trade:{symbol.upper()}", "trade", lambda: self._provider.get_latest_trade(symbol))

    # --- Snapshots --------------------------------------------------------------

    async def get_snapshots(self, symbols: list[str]) -> dict[str, StockSnapshot]:
        key = f"snapshot:{','.join(sorted(s.upper() for s in symbols))}"
        return await self._cached(key, "snapshot", lambda: self._provider.get_snapshots(symbols)) or {}

    # --- Bars -------------------------------------------------------------------

    async def get_bars(
        self,
        symbol: str,
        timeframe: str = "1d",
        lookback_hours: int = 24,
        limit: int | None = None,
    ) -> list[Bar]:
        tf = BAR_TIMEFRAMES.get(timeframe, timeframe)
        end = datetime.now(UTC)
        start = end - timedelta(hours=lookback_hours)
        ttl_bucket = int(time.time() // max(1, self._ttls.get("bars", 30)))
        key = f"bars:{symbol.upper()}:{tf}:{lookback_hours}:{limit}:{ttl_bucket}"
        return await self._cached(key, "bars", lambda: self._provider.get_bars(symbol.upper(), tf, start, end, limit)) or []

    # --- Corporate Actions ------------------------------------------------------

    async def get_corporate_actions(
        self,
        symbols: list[str],
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, list[CorporateAction]]:
        syms = sorted(s.upper() for s in symbols)
        key = f"corp:{','.join(syms)}:{start}:{end}"
        return await self._cached(key, "corp", lambda: self._provider.get_corporate_actions(symbols, start, end)) or {}

    # --- Options ----------------------------------------------------------------

    async def get_options_chain(
        self,
        underlying: str,
        expiration: date | None = None,
        strike_min: float | None = None,
        strike_max: float | None = None,
    ) -> OptionsChain:
        key = f"chain:{underlying.upper()}:{expiration}:{strike_min}:{strike_max}"
        return await self._cached(key, "chain", lambda: self._provider.get_options_chain(underlying, expiration, strike_min, strike_max)) or OptionsChain(underlying=underlying.upper())

    async def get_option_snapshot(self, option_symbol: str) -> OptionContract | None:
        return await self._cached(f"opt_snap:{option_symbol.upper()}", "option_snap", lambda: self._provider.get_option_snapshot(option_symbol))

    # --- Calendar ---------------------------------------------------------------

    async def get_calendar(self, start: date, end: date, event_types: list[str] | None = None) -> list[CalendarEvent]:
        key = f"calendar:{start}:{end}:{','.join(event_types or [])}"
        return await self._cached(key, "calendar", lambda: self._provider.get_calendar(start, end, event_types)) or []

    # --- Caching + rate guard ---------------------------------------------------

    async def _rate_guard(self) -> None:
        now = int(time.time() * 1000)
        wait = self._last_request_ms + _RATE_GUARD_MS - now
        if wait > 0:
            await asyncio.sleep(wait / 1000)
        self._last_request_ms = int(time.time() * 1000)

    async def _cached(self, key: str, cache_bucket: str, fetcher: Any) -> Any:
        """Return cached value if fresh, otherwise fetch with rate guard."""
        ttl = self._ttls.get(cache_bucket, 30)
        now = time.time()
        if key in self._cache:
            cached_at, value = self._cache[key]
            if now - cached_at < ttl:
                return value
        async with self._lock:
            # Recheck under lock
            if key in self._cache:
                cached_at, value = self._cache[key]
                if now - cached_at < ttl:
                    return value
            await self._rate_guard()
            try:
                result = await fetcher()
            except Exception as exc:
                log.warning("tradfi_cache_fetch_failed", key=key, error=type(exc).__name__)
                # Return stale cache if available
                if key in self._cache:
                    _, stale = self._cache[key]
                    return stale
                raise
            self._cache[key] = (time.time(), result)
            # Prune old entries
            if len(self._cache) > 500:
                cutoff = now - max(self._ttls.values()) * 2
                self._cache = {k: v for k, v in self._cache.items() if v[0] > cutoff}
            return result

    def status(self) -> dict[str, Any]:
        return {
            "provider": self._provider.status(),
            "cache_entries": len(self._cache),
        }