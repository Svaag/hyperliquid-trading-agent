"""Vendor-agnostic TradFi data provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime
from typing import Any

from hyperliquid_trading_agent.app.tradfi.schemas import (
    Bar,
    CalendarEvent,
    CorporateAction,
    OptionContract,
    OptionsChain,
    StockQuote,
    StockSnapshot,
    StockTrade,
)


class TradFiProvider(ABC):
    """Abstract interface for TradFi market data.

    Each implementation wraps a specific vendor SDK (Alpaca, IBKR, Polygon, etc.).
    The ``TradFiClient`` facade holds one or more providers and exposes a single
    TTL-cached interface to the rest of the application.
    """

    name: str = "base"

    # --- Quotes & Trades --------------------------------------------------------

    @abstractmethod
    async def get_latest_quote(self, symbol: str) -> StockQuote | None: ...

    @abstractmethod
    async def get_latest_trade(self, symbol: str) -> StockTrade | None: ...

    # --- Snapshots --------------------------------------------------------------

    @abstractmethod
    async def get_snapshots(self, symbols: list[str]) -> dict[str, StockSnapshot]: ...

    # --- Bars -------------------------------------------------------------------

    @abstractmethod
    async def get_bars(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        limit: int | None = None,
    ) -> list[Bar]: ...

    # --- Corporate Actions ------------------------------------------------------

    @abstractmethod
    async def get_corporate_actions(
        self,
        symbols: list[str],
        start: date | None = None,
        end: date | None = None,
        limit: int | None = None,
    ) -> dict[str, list[CorporateAction]]: ...

    # --- Options ----------------------------------------------------------------

    @abstractmethod
    async def get_options_chain(
        self,
        underlying: str,
        expiration: date | None = None,
        strike_min: float | None = None,
        strike_max: float | None = None,
    ) -> OptionsChain: ...

    @abstractmethod
    async def get_option_snapshot(self, option_symbol: str) -> OptionContract | None: ...

    # --- Calendar ---------------------------------------------------------------

    @abstractmethod
    async def get_calendar(
        self,
        start: date,
        end: date,
        event_types: list[str] | None = None,
    ) -> list[CalendarEvent]: ...

    # --- Lifecycle --------------------------------------------------------------

    async def start(self) -> None:
        """Optional startup hook (e.g., pre-warm connections)."""
        return None

    async def close(self) -> None:
        """Optional cleanup hook."""
        return None

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "healthy": True}
