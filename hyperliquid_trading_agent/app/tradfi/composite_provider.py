"""Composite TradFi provider with ordered fallback semantics."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.tradfi.base import TradFiProvider
from hyperliquid_trading_agent.app.tradfi.schemas import (
    Bar,
    CalendarEvent,
    CorporateAction,
    OptionContract,
    OptionsChain,
    StockQuote,
    StockSnapshot,
    StockTrade,
    TradFiAsset,
)

log = get_logger(__name__)


class CompositeTradFiProvider(TradFiProvider):
    """Try providers in order and return the first useful response."""

    name = "composite"

    def __init__(self, providers: list[TradFiProvider]):
        self.providers = providers

    async def get_latest_quote(self, symbol: str) -> StockQuote | None:
        for provider in self.providers:
            try:
                quote = await provider.get_latest_quote(symbol)
            except Exception as exc:
                log.debug("tradfi_provider_quote_failed", provider=provider.name, symbol=symbol, error=type(exc).__name__)
                continue
            if quote is not None:
                return quote
        return None

    async def get_latest_trade(self, symbol: str) -> StockTrade | None:
        for provider in self.providers:
            try:
                trade = await provider.get_latest_trade(symbol)
            except Exception as exc:
                log.debug("tradfi_provider_trade_failed", provider=provider.name, symbol=symbol, error=type(exc).__name__)
                continue
            if trade is not None:
                return trade
        return None

    async def get_snapshots(self, symbols: list[str]) -> dict[str, StockSnapshot]:
        remaining = {symbol.upper() for symbol in symbols if symbol.strip()}
        result: dict[str, StockSnapshot] = {}
        for provider in self.providers:
            if not remaining:
                break
            try:
                snapshots = await provider.get_snapshots(sorted(remaining))
            except Exception as exc:
                log.debug("tradfi_provider_snapshots_failed", provider=provider.name, error=type(exc).__name__)
                continue
            for symbol, snapshot in snapshots.items():
                symbol_upper = symbol.upper()
                if symbol_upper in remaining:
                    result[symbol_upper] = snapshot
                    remaining.discard(symbol_upper)
        return result

    async def get_bars(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        limit: int | None = None,
    ) -> list[Bar]:
        for provider in self.providers:
            try:
                bars = await provider.get_bars(symbol, timeframe, start, end, limit)
            except Exception as exc:
                log.debug("tradfi_provider_bars_failed", provider=provider.name, symbol=symbol, timeframe=timeframe, error=type(exc).__name__)
                continue
            if bars:
                return bars
        return []

    async def get_corporate_actions(
        self,
        symbols: list[str],
        start: date | None = None,
        end: date | None = None,
        limit: int | None = None,
    ) -> dict[str, list[CorporateAction]]:
        result: dict[str, list[CorporateAction]] = {}
        for provider in self.providers:
            try:
                actions = await provider.get_corporate_actions(symbols, start, end, limit)
            except Exception as exc:
                log.debug("tradfi_provider_corp_failed", provider=provider.name, error=type(exc).__name__)
                continue
            for symbol, items in actions.items():
                if items:
                    result.setdefault(symbol.upper(), []).extend(items)
        return result

    async def get_options_chain(
        self,
        underlying: str,
        expiration: date | None = None,
        strike_min: float | None = None,
        strike_max: float | None = None,
    ) -> OptionsChain:
        fallback = OptionsChain(underlying=underlying.upper())
        for provider in self.providers:
            try:
                chain = await provider.get_options_chain(underlying, expiration, strike_min, strike_max)
            except Exception as exc:
                log.debug("tradfi_provider_chain_failed", provider=provider.name, underlying=underlying, error=type(exc).__name__)
                continue
            if chain.contracts or chain.underlying_price is not None:
                return chain
            fallback = chain
        return fallback

    async def get_option_snapshot(self, option_symbol: str) -> OptionContract | None:
        for provider in self.providers:
            try:
                snapshot = await provider.get_option_snapshot(option_symbol)
            except Exception as exc:
                log.debug("tradfi_provider_option_failed", provider=provider.name, symbol=option_symbol, error=type(exc).__name__)
                continue
            if snapshot is not None:
                return snapshot
        return None

    async def get_calendar(
        self,
        start: date,
        end: date,
        event_types: list[str] | None = None,
    ) -> list[CalendarEvent]:
        result: list[CalendarEvent] = []
        for provider in self.providers:
            try:
                result.extend(await provider.get_calendar(start, end, event_types))
            except Exception as exc:
                log.debug("tradfi_provider_calendar_failed", provider=provider.name, error=type(exc).__name__)
        return result

    async def get_asset_metadata(self, symbols: list[str]) -> dict[str, TradFiAsset]:
        remaining = {symbol.upper() for symbol in symbols if symbol.strip()}
        result: dict[str, TradFiAsset] = {}
        for provider in self.providers:
            if not remaining:
                break
            try:
                metadata = await provider.get_asset_metadata(sorted(remaining))
            except Exception as exc:
                log.debug("tradfi_provider_asset_metadata_failed", provider=provider.name, error=type(exc).__name__)
                continue
            for symbol, asset in metadata.items():
                symbol_upper = symbol.upper()
                if symbol_upper in remaining:
                    result[symbol_upper] = asset
                    remaining.discard(symbol_upper)
        return result

    async def start(self) -> None:
        for provider in self.providers:
            await provider.start()

    async def close(self) -> None:
        for provider in self.providers:
            await provider.close()

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "providers": [provider.status() for provider in self.providers],
            "healthy": bool(self.providers),
        }
