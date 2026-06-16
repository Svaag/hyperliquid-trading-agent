"""Alpaca Markets TradFi provider — wraps alpaca-py data clients + REST for corp actions."""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime
from typing import Any

from alpaca.data import (
    CorporateActionsRequest,
    OptionChainRequest,
    OptionSnapshotRequest,
    StockBarsRequest,
    StockLatestQuoteRequest,
    StockLatestTradeRequest,
    StockSnapshotRequest,
)
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.historical.corporate_actions import CorporateActionsClient
from alpaca.data.models import Snapshot
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetCalendarRequest

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

_TIMEFRAME_MAP: dict[str, TimeFrame] = {
    "1Min": TimeFrame.Minute,
    "5Min": TimeFrame(5, TimeFrame.Minute.unit),
    "15Min": TimeFrame(15, TimeFrame.Minute.unit),
    "30Min": TimeFrame(30, TimeFrame.Minute.unit),
    "1Hour": TimeFrame.Hour,
    "2Hour": TimeFrame(2, TimeFrame.Hour.unit),
    "4Hour": TimeFrame(4, TimeFrame.Hour.unit),
    "1Day": TimeFrame.Day,
    "1Week": TimeFrame.Week,
    "1Month": TimeFrame.Month,
}

# OCC symbol pattern: <root><YY><MM><DD><C|P><strike*1000 with leading zeros>
_OCC_RE = re.compile(r"^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d+)$")


def _parse_occ(occ: str) -> tuple[str, date, str, float] | None:
    """Parse an OCC option symbol into (root, expiration, type, strike)."""
    m = _OCC_RE.match(occ.upper().replace(" ", ""))
    if not m:
        return None
    root, yy, mm, dd, opt, strike_str = m.groups()
    year = 2000 + int(yy)
    expiration = date(year, int(mm), int(dd))
    option_type = "call" if opt == "C" else "put"
    strike = float(strike_str) / 1000.0
    return root, expiration, option_type, strike


def _to_timeframe(tf: str) -> TimeFrame:
    cached = _TIMEFRAME_MAP.get(tf)
    if cached is not None:
        return cached
    _TIMEFRAME_MAP[tf] = TimeFrame.Day
    return TimeFrame.Day


class AlpacaTradFiProvider(TradFiProvider):
    """TradFiProvider implementation using the Alpaca Data API v2.

    Requires ``ALPACA_API_KEY`` and ``ALPACA_API_SECRET``.
    Uses the free ``IEX`` feed by default for stocks.
    Corporate actions and calendar use the REST API directly (not in alpaca-py data client).
    """

    name = "alpaca"

    def __init__(self, *, api_key: str, api_secret: str, feed: DataFeed = DataFeed.IEX):
        self.api_key = api_key
        self.api_secret = api_secret
        self.feed = feed
        self._stock: StockHistoricalDataClient | None = None
        self._options: OptionHistoricalDataClient | None = None
        self._corporate: CorporateActionsClient | None = None
        self._trading: TradingClient | None = None

    def _ensure_stock(self) -> StockHistoricalDataClient:
        if self._stock is None:
            self._stock = StockHistoricalDataClient(api_key=self.api_key, secret_key=self.api_secret)
        return self._stock

    def _ensure_options(self) -> OptionHistoricalDataClient:
        if self._options is None:
            self._options = OptionHistoricalDataClient(api_key=self.api_key, secret_key=self.api_secret)
        return self._options

    def _ensure_corporate(self) -> CorporateActionsClient:
        if self._corporate is None:
            self._corporate = CorporateActionsClient(api_key=self.api_key, secret_key=self.api_secret)
        return self._corporate

    def _ensure_trading(self) -> TradingClient:
        if self._trading is None:
            # Paper=True keeps this client in paper mode. We only use market calendar.
            self._trading = TradingClient(api_key=self.api_key, secret_key=self.api_secret, paper=True)
        return self._trading

    # --- Quotes & Trades --------------------------------------------------------

    async def get_latest_quote(self, symbol: str) -> StockQuote | None:
        try:
            client = self._ensure_stock()
            request = StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=self.feed)
            response = await asyncio.to_thread(client.get_stock_latest_quote, request)
            return self._quote_to_model(symbol.upper(), response.get(symbol.upper()))
        except Exception as exc:
            log.warning("alpaca_get_latest_quote_failed", symbol=symbol, error=type(exc).__name__)
            return None

    async def get_latest_trade(self, symbol: str) -> StockTrade | None:
        try:
            client = self._ensure_stock()
            request = StockLatestTradeRequest(symbol_or_symbols=symbol, feed=self.feed)
            response = await asyncio.to_thread(client.get_stock_latest_trade, request)
            return self._trade_to_model(symbol.upper(), response.get(symbol.upper()))
        except Exception as exc:
            log.warning("alpaca_get_latest_trade_failed", symbol=symbol, error=type(exc).__name__)
            return None

    # --- Snapshots --------------------------------------------------------------

    async def get_snapshots(self, symbols: list[str]) -> dict[str, StockSnapshot]:
        if not symbols:
            return {}
        try:
            client = self._ensure_stock()
            request = StockSnapshotRequest(symbol_or_symbols=symbols, feed=self.feed)
            response = await asyncio.to_thread(client.get_stock_snapshot, request)
            result: dict[str, StockSnapshot] = {}
            for sym, snap in (response or {}).items():
                if snap is not None:
                    result[sym.upper()] = self._snapshot_to_model(sym.upper(), snap)
            return result
        except Exception as exc:
            log.warning("alpaca_get_snapshots_failed", symbols=symbols, error=type(exc).__name__)
            return {}

    # --- Bars -------------------------------------------------------------------

    async def get_bars(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        limit: int | None = None,
    ) -> list[Bar]:
        try:
            client = self._ensure_stock()
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=_to_timeframe(timeframe),
                start=start,
                end=end,
                limit=limit,
                adjustment=Adjustment.ALL,
                feed=self.feed,
            )
            response = await asyncio.to_thread(client.get_stock_bars, request)
            bars = response.get(symbol.upper(), [])
            return [
                Bar(
                    symbol=symbol.upper(),
                    timestamp=b.timestamp,
                    open=float(b.open),
                    high=float(b.high),
                    low=float(b.low),
                    close=float(b.close),
                    volume=float(b.volume),
                    trade_count=b.trade_count,
                    vwap=float(b.vwap) if b.vwap else None,
                    timeframe=timeframe,
                )
                for b in bars
            ]
        except Exception as exc:
            log.warning("alpaca_get_bars_failed", symbol=symbol, timeframe=timeframe, error=type(exc).__name__)
            return []

    # --- Corporate Actions ------------------------------------------------------

    async def get_corporate_actions(
        self,
        symbols: list[str],
        start: date | None = None,
        end: date | None = None,
        limit: int | None = 1000,
    ) -> dict[str, list[CorporateAction]]:
        if not symbols:
            return {}
        try:
            client = self._ensure_corporate()
            request = CorporateActionsRequest(
                symbols=[s.upper() for s in symbols],
                start=start,
                end=end,
                limit=limit or 1000,
            )
            response = await asyncio.to_thread(client.get_corporate_actions, request)
            result: dict[str, list[CorporateAction]] = {}
            data = getattr(response, "data", {}) or {}
            for action_type, items in data.items():
                for item in items or []:
                    payload = item.model_dump(mode="python") if callable(getattr(item, "model_dump", None)) else dict(item)
                    sym = str(payload.get("symbol", "")).upper()
                    action = CorporateAction(
                        id=str(payload.get("id", "")),
                        symbol=sym,
                        action_type=str(payload.get("corporate_action_type") or action_type or "unknown").lower(),
                        declaration_date=_parse_date(payload.get("declaration_date") or payload.get("process_date")),
                        ex_date=_parse_date(payload.get("ex_date")),
                        record_date=_parse_date(payload.get("record_date")),
                        payable_date=_parse_date(payload.get("payable_date")),
                        description=str(payload.get("description", "")),
                        old_rate=_float_or_none(payload.get("old_rate")),
                        new_rate=_float_or_none(payload.get("new_rate")),
                        dividend_rate=_float_or_none(payload.get("rate")),
                        dividend_type=str(payload.get("dividend_type", "")).lower() if payload.get("dividend_type") else None,
                    )
                    result.setdefault(sym, []).append(action)
            return result
        except Exception as exc:
            log.warning("alpaca_get_corp_actions_failed", symbols=symbols, error=type(exc).__name__)
            return {}

    # --- Options ----------------------------------------------------------------

    async def get_options_chain(
        self,
        underlying: str,
        expiration: date | None = None,
        strike_min: float | None = None,
        strike_max: float | None = None,
    ) -> OptionsChain:
        try:
            client = self._ensure_options()
            params: dict[str, Any] = {"underlying_symbol": underlying.upper()}
            if expiration:
                params["expiration_date"] = expiration
            if strike_min is not None:
                params["strike_price_gte"] = strike_min
            if strike_max is not None:
                params["strike_price_lte"] = strike_max
            request = OptionChainRequest(**params)
            stock_snaps = await self.get_snapshots([underlying.upper()])
            stock_snap = stock_snaps.get(underlying.upper())
            under_price = _snapshot_price(stock_snap)
            response = await asyncio.to_thread(client.get_option_chain, request)
            contracts: list[OptionContract] = []
            for occ_symbol, snap in (response or {}).items():
                if snap is None:
                    continue
                parsed = _parse_occ(str(occ_symbol))
                if parsed is None:
                    continue
                root, exp, opt_type, strike = parsed
                contract = self._option_snapshot_to_contract(occ_symbol, root, exp, opt_type, strike, snap)
                contracts.append(contract)
            return OptionsChain(
                underlying=underlying.upper(),
                underlying_price=under_price,
                expiration_date=expiration,
                contracts=sorted(contracts, key=lambda c: (c.expiration_date, c.strike_price)),
            )
        except Exception as exc:
            log.warning("alpaca_get_options_chain_failed", underlying=underlying, error=type(exc).__name__)
            return OptionsChain(underlying=underlying.upper())

    async def get_option_snapshot(self, option_symbol: str) -> OptionContract | None:
        try:
            client = self._ensure_options()
            request = OptionSnapshotRequest(symbol_or_symbols=option_symbol)
            response = await asyncio.to_thread(client.get_option_snapshot, request)
            snap = response.get(option_symbol.upper()) or (next(iter(response.values())) if response else None)
            if snap is None:
                return None
            parsed = _parse_occ(option_symbol)
            root, exp, opt_type, strike = parsed if parsed else ("?", date.today(), "call", 0.0)
            return self._option_snapshot_to_contract(option_symbol.upper(), root, exp, opt_type, strike, snap)
        except Exception as exc:
            log.warning("alpaca_get_option_snapshot_failed", symbol=option_symbol, error=type(exc).__name__)
            return None

    def _option_snapshot_to_contract(
        self,
        occ_symbol: str,
        root: str,
        expiration: date,
        option_type: str,
        strike: float,
        snap: Any,
    ) -> OptionContract:
        return OptionContract(
            symbol=str(occ_symbol),
            underlying=root,
            strike_price=strike,
            expiration_date=expiration,
            option_type=option_type,  # type: ignore[arg-type]
            bid=float(snap.latest_quote.bid_price) if snap.latest_quote and snap.latest_quote.bid_price else None,
            ask=float(snap.latest_quote.ask_price) if snap.latest_quote and snap.latest_quote.ask_price else None,
            last_price=float(snap.latest_trade.price) if snap.latest_trade and snap.latest_trade.price else None,
            last_size=int(snap.latest_trade.size) if snap.latest_trade and snap.latest_trade.size else None,
            volume=None,  # not in OptionsSnapshot
            open_interest=None,  # not in OptionsSnapshot
            implied_volatility=float(snap.implied_volatility) if snap.implied_volatility else None,
            delta=float(snap.greeks.delta) if snap.greeks and snap.greeks.delta else None,
            gamma=float(snap.greeks.gamma) if snap.greeks and snap.greeks.gamma else None,
            theta=float(snap.greeks.theta) if snap.greeks and snap.greeks.theta else None,
            vega=float(snap.greeks.vega) if snap.greeks and snap.greeks.vega else None,
            rho=float(snap.greeks.rho) if snap.greeks and snap.greeks.rho else None,
        )

    # --- Calendar ---------------------------------------------------------------

    async def get_calendar(
        self,
        start: date,
        end: date,
        event_types: list[str] | None = None,
    ) -> list[CalendarEvent]:
        try:
            client = self._ensure_trading()
            request = GetCalendarRequest(start=start, end=end)
            days = await asyncio.to_thread(client.get_calendar, request)
            events: list[CalendarEvent] = []
            for day in days or []:
                day_date = _parse_date(getattr(day, "date", None))
                if day_date is None:
                    continue
                open_time = getattr(day, "open", "")
                close_time = getattr(day, "close", "")
                events.append(
                    CalendarEvent(
                        date=day_date,
                        event_type="trading_day",
                        description=f"Market open: {open_time} - {close_time}",
                    )
                )
            return events
        except Exception as exc:
            log.warning("alpaca_get_calendar_failed", error=type(exc).__name__)
            return []

    # --- Asset metadata --------------------------------------------------------

    async def get_asset_metadata(self, symbols: list[str]) -> dict[str, TradFiAsset]:
        if not symbols:
            return {}
        client = self._ensure_trading()
        result: dict[str, TradFiAsset] = {}
        for symbol in sorted({s.upper() for s in symbols if s.strip()}):
            try:
                asset = await asyncio.to_thread(client.get_asset, symbol)
            except Exception as exc:
                log.debug("alpaca_get_asset_metadata_miss", symbol=symbol, error=type(exc).__name__)
                continue
            result[symbol] = TradFiAsset(
                symbol=str(getattr(asset, "symbol", symbol)).upper(),
                name=str(getattr(asset, "name", "") or ""),
                exchange=str(getattr(asset, "exchange", "") or ""),
                asset_class=str(getattr(asset, "asset_class", "") or ""),
                status=str(getattr(asset, "status", "") or ""),
                tradable=bool(getattr(asset, "tradable", False)),
                marginable=bool(getattr(asset, "marginable", False)),
                shortable=bool(getattr(asset, "shortable", False)),
                easy_to_borrow=bool(getattr(asset, "easy_to_borrow", False)),
            )
        return result

    # --- Internal converters ----------------------------------------------------

    @staticmethod
    def _quote_to_model(symbol: str, quote_data: Any) -> StockQuote | None:
        if quote_data is None:
            return None
        return StockQuote(
            symbol=symbol,
            ask_price=float(quote_data.ask_price) if quote_data.ask_price else None,
            ask_size=float(quote_data.ask_size) if quote_data.ask_size else None,
            bid_price=float(quote_data.bid_price) if quote_data.bid_price else None,
            bid_size=float(quote_data.bid_size) if quote_data.bid_size else None,
            timestamp=quote_data.timestamp,
            conditions=list(quote_data.conditions) if quote_data.conditions else [],
            tape=str(quote_data.tape) if getattr(quote_data, "tape", None) else "",
        )

    @staticmethod
    def _trade_to_model(symbol: str, trade_data: Any) -> StockTrade | None:
        if trade_data is None:
            return None
        return StockTrade(
            symbol=symbol,
            price=float(trade_data.price),
            size=int(trade_data.size),
            timestamp=trade_data.timestamp,
            exchange=str(trade_data.exchange) if getattr(trade_data, "exchange", None) else None,
            conditions=list(trade_data.conditions) if getattr(trade_data, "conditions", None) else [],
            tape=str(trade_data.tape) if getattr(trade_data, "tape", None) else "",
        )

    @staticmethod
    def _snapshot_to_model(symbol: str, snap: Snapshot) -> StockSnapshot:
        quote = AlpacaTradFiProvider._quote_to_model(symbol, snap.latest_quote)
        trade = AlpacaTradFiProvider._trade_to_model(symbol, snap.latest_trade)
        bar = None
        if snap.daily_bar:
            bar = Bar(
                symbol=symbol,
                timestamp=snap.daily_bar.timestamp,
                open=float(snap.daily_bar.open),
                high=float(snap.daily_bar.high),
                low=float(snap.daily_bar.low),
                close=float(snap.daily_bar.close),
                volume=float(snap.daily_bar.volume),
                trade_count=snap.daily_bar.trade_count,
                vwap=float(snap.daily_bar.vwap) if snap.daily_bar.vwap else None,
                timeframe="1Day",
            )
        change_pct = None
        if snap.daily_bar and snap.previous_daily_bar:
            prev_close = float(snap.previous_daily_bar.close)
            if prev_close and prev_close != 0:
                change_pct = (float(snap.daily_bar.close) - prev_close) / prev_close * 100
        return StockSnapshot(
            symbol=symbol,
            latest_quote=quote,
            latest_trade=trade,
            daily_bar=bar,
            previous_close=float(snap.previous_daily_bar.close) if snap.previous_daily_bar else None,
            change_pct=change_pct,
        )

    # --- Lifecycle --------------------------------------------------------------

    async def close(self) -> None:
        self._stock = None
        self._options = None
        self._corporate = None
        self._trading = None

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "feed": str(self.feed), "healthy": True}


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _snapshot_price(snapshot: StockSnapshot | None) -> float | None:
    if snapshot is None:
        return None
    if snapshot.latest_trade is not None and snapshot.latest_trade.price > 0:
        return snapshot.latest_trade.price
    if snapshot.daily_bar is not None and snapshot.daily_bar.close > 0:
        return snapshot.daily_bar.close
    quote = snapshot.latest_quote
    if quote is not None and quote.bid_price and quote.ask_price:
        return (quote.bid_price + quote.ask_price) / 2
    return None


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None
