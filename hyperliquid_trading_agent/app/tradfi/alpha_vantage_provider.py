"""Alpha Vantage TradFi provider with MCP-first and REST fallback transports."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

import httpx

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

AlphaVantageTransport = Literal["auto", "mcp", "rest"]

_INTRADAY_INTERVALS: dict[str, str] = {
    "1Min": "1min",
    "5Min": "5min",
    "15Min": "15min",
    "30Min": "30min",
    "1Hour": "60min",
}

_FUNCTION_BY_TIMEFRAME: dict[str, str] = {
    "1Day": "TIME_SERIES_DAILY",
    "1Week": "TIME_SERIES_WEEKLY",
    "1Month": "TIME_SERIES_MONTHLY",
}

_TIME_SERIES_KEY_BY_FUNCTION: dict[str, str] = {
    "TIME_SERIES_DAILY": "Time Series (Daily)",
    "TIME_SERIES_WEEKLY": "Weekly Time Series",
    "TIME_SERIES_MONTHLY": "Monthly Time Series",
}


class AlphaVantageProviderError(RuntimeError):
    """Raised for Alpha Vantage payloads that are not usable market data."""


class AlphaVantageTradFiProvider(TradFiProvider):
    """TradFiProvider implementation backed by Alpha Vantage.

    The provider attempts the official Alpha Vantage MCP endpoint first when
    configured, then falls back to the REST API. REST stays in place for
    deterministic tests and for runtime resilience if MCP tool names drift.
    """

    name = "alpha_vantage"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://www.alphavantage.co/query",
        mcp_url: str = "https://mcp.alphavantage.co/mcp",
        mcp_auth_header: str = "Authorization",
        mcp_auth_scheme: str = "Bearer",
        transport: AlphaVantageTransport = "auto",
        timeout_seconds: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.mcp_url = mcp_url
        self.mcp_auth_header = mcp_auth_header
        self.mcp_auth_scheme = mcp_auth_scheme
        self.transport = transport
        self.timeout_seconds = timeout_seconds
        self._owns_http = http_client is None
        self.http = http_client or httpx.AsyncClient(timeout=timeout_seconds)
        self._mcp_unavailable = False

    async def close(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    async def get_latest_quote(self, symbol: str) -> StockQuote | None:
        try:
            data = await self._query_rest({"function": "GLOBAL_QUOTE", "symbol": symbol.upper()})
            quote = data.get("Global Quote") if isinstance(data, dict) else None
            if not isinstance(quote, dict):
                return None
            price = _float_or_none(quote.get("05. price"))
            return StockQuote(
                symbol=symbol.upper(),
                bid_price=price,
                ask_price=price,
                timestamp=_parse_timestamp(quote.get("07. latest trading day")),
            )
        except Exception as exc:
            log.warning("alpha_vantage_get_quote_failed", symbol=symbol, error=type(exc).__name__)
            return None

    async def get_latest_trade(self, symbol: str) -> StockTrade | None:
        try:
            data = await self._query_rest({"function": "GLOBAL_QUOTE", "symbol": symbol.upper()})
            quote = data.get("Global Quote") if isinstance(data, dict) else None
            if not isinstance(quote, dict):
                return None
            price = _float_or_none(quote.get("05. price"))
            volume = int(_float_or_none(quote.get("06. volume")) or 0)
            if price is None:
                return None
            return StockTrade(
                symbol=symbol.upper(),
                price=price,
                size=volume,
                timestamp=_parse_timestamp(quote.get("07. latest trading day")),
                exchange="alpha_vantage",
            )
        except Exception as exc:
            log.warning("alpha_vantage_get_trade_failed", symbol=symbol, error=type(exc).__name__)
            return None

    async def get_snapshots(self, symbols: list[str]) -> dict[str, StockSnapshot]:
        result: dict[str, StockSnapshot] = {}
        for symbol in sorted({item.upper() for item in symbols if item.strip()}):
            quote, bars = await asyncio.gather(self.get_latest_quote(symbol), self.get_bars(symbol, "1Day", _days_ago(7), datetime.now(UTC), limit=2))
            latest_bar = bars[-1] if bars else None
            previous = bars[-2].close if len(bars) >= 2 else None
            change_pct = None
            if latest_bar is not None and previous is not None and previous != 0:
                change_pct = (latest_bar.close - previous) / previous * 100
            if quote is not None or latest_bar is not None:
                result[symbol] = StockSnapshot(
                    symbol=symbol,
                    latest_quote=quote,
                    latest_trade=None,
                    daily_bar=latest_bar,
                    previous_close=previous,
                    change_pct=change_pct,
                )
        return result

    async def get_bars(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        limit: int | None = None,
    ) -> list[Bar]:
        function, params = _time_series_request(symbol.upper(), timeframe)
        bars: list[Bar] = []
        if self.transport in {"auto", "mcp"} and not self._mcp_unavailable:
            try:
                payload = await self._query_mcp(function, params)
                bars = _parse_time_series_payload(symbol.upper(), timeframe, function, payload)
            except Exception as exc:
                log.debug("alpha_vantage_mcp_bars_failed", symbol=symbol, timeframe=timeframe, error=type(exc).__name__)
                self._mcp_unavailable = True
                if self.transport == "mcp":
                    return []
        if not bars and self.transport in {"auto", "rest"}:
            try:
                payload = await self._query_rest({"function": function, **params})
                bars = _parse_time_series_payload(symbol.upper(), timeframe, function, payload)
            except Exception as exc:
                log.warning("alpha_vantage_rest_bars_failed", symbol=symbol, timeframe=timeframe, error=type(exc).__name__)
                return []
        filtered = [bar for bar in bars if _in_range(bar.timestamp, start, end)]
        if limit is not None:
            filtered = filtered[-max(0, limit) :]
        return filtered

    async def get_corporate_actions(
        self,
        symbols: list[str],
        start: date | None = None,
        end: date | None = None,
        limit: int | None = None,
    ) -> dict[str, list[CorporateAction]]:
        return {}

    async def get_options_chain(
        self,
        underlying: str,
        expiration: date | None = None,
        strike_min: float | None = None,
        strike_max: float | None = None,
    ) -> OptionsChain:
        return OptionsChain(underlying=underlying.upper())

    async def get_option_snapshot(self, option_symbol: str) -> OptionContract | None:
        return None

    async def get_calendar(
        self,
        start: date,
        end: date,
        event_types: list[str] | None = None,
    ) -> list[CalendarEvent]:
        return []

    async def get_asset_metadata(self, symbols: list[str]) -> dict[str, TradFiAsset]:
        result: dict[str, TradFiAsset] = {}
        for symbol in sorted({item.upper() for item in symbols if item.strip()}):
            try:
                payload = await self._query_rest({"function": "SYMBOL_SEARCH", "keywords": symbol})
            except Exception as exc:
                log.debug("alpha_vantage_symbol_search_failed", symbol=symbol, error=type(exc).__name__)
                continue
            matches = payload.get("bestMatches") if isinstance(payload, dict) else None
            if not isinstance(matches, list):
                continue
            selected = _select_symbol_match(symbol, matches)
            if selected is None:
                continue
            asset_type = str(selected.get("3. type") or "equity").lower()
            result[symbol] = TradFiAsset(
                symbol=str(selected.get("1. symbol") or symbol).upper(),
                name=str(selected.get("2. name") or ""),
                exchange=str(selected.get("4. region") or ""),
                asset_class="etf" if "etf" in asset_type else "us_equity",
                status="active",
                tradable=True,
            )
        return result

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "transport": self.transport,
            "mcp_configured": bool(self.mcp_url),
            "mcp_unavailable": self._mcp_unavailable,
            "healthy": True,
        }

    async def _query_rest(self, params: dict[str, Any]) -> dict[str, Any]:
        response = await self.http.get(self.base_url, params={**params, "apikey": self.api_key})
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise AlphaVantageProviderError("non_object_payload")
        _raise_for_alpha_vantage_error(data)
        return data

    async def _query_mcp(self, function: str, params: dict[str, Any]) -> dict[str, Any]:
        headers = _mcp_headers(self.api_key, self.mcp_auth_header, self.mcp_auth_scheme)
        arguments = {"function": function, **params, "apikey": self.api_key}
        tool_names = _candidate_mcp_tool_names(function)
        try:
            import mcp as mcp_module

            client_cls = getattr(mcp_module, "Client", None)
            if client_cls is not None:
                async with client_cls(self.mcp_url, headers=headers) as client:
                    tools = await client.list_tools()
                    tool_name = _select_mcp_tool(function, tool_names, tools)
                    result = await client.call_tool(tool_name, arguments)
                    return _mcp_result_to_json(result)
        except ImportError:
            pass
        except TypeError:
            pass

        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(self.mcp_url, headers=headers, timeout=self.timeout_seconds) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                tool_name = _select_mcp_tool(function, tool_names, tools.tools if hasattr(tools, "tools") else tools)
                result = await session.call_tool(tool_name, arguments)
                return _mcp_result_to_json(result)


def _time_series_request(symbol: str, timeframe: str) -> tuple[str, dict[str, Any]]:
    if timeframe in _INTRADAY_INTERVALS:
        return (
            "TIME_SERIES_INTRADAY",
            {
                "symbol": symbol,
                "interval": _INTRADAY_INTERVALS[timeframe],
                "outputsize": "compact",
                "adjusted": "true",
                "extended_hours": "true",
            },
        )
    function = _FUNCTION_BY_TIMEFRAME.get(timeframe, "TIME_SERIES_DAILY")
    params: dict[str, Any] = {"symbol": symbol}
    if function == "TIME_SERIES_DAILY":
        params["outputsize"] = "compact"
    return function, params


def _parse_time_series_payload(symbol: str, timeframe: str, function: str, payload: Mapping[str, Any]) -> list[Bar]:
    _raise_for_alpha_vantage_error(payload)
    key = _time_series_key(function, payload)
    rows = payload.get(key)
    if not isinstance(rows, dict):
        return []
    bars: list[Bar] = []
    for raw_timestamp, raw_bar in rows.items():
        if not isinstance(raw_bar, dict):
            continue
        timestamp = _parse_timestamp(raw_timestamp)
        if timestamp is None:
            continue
        open_px = _float_or_none(raw_bar.get("1. open"))
        high = _float_or_none(raw_bar.get("2. high"))
        low = _float_or_none(raw_bar.get("3. low"))
        close = _float_or_none(raw_bar.get("4. close"))
        volume = _float_or_none(raw_bar.get("5. volume"))
        if open_px is None or high is None or low is None or close is None or volume is None:
            continue
        bars.append(
            Bar(
                symbol=symbol,
                timestamp=timestamp,
                open=open_px,
                high=high,
                low=low,
                close=close,
                volume=volume,
                timeframe=timeframe,
            )
        )
    return sorted(bars, key=lambda item: item.timestamp)


def _time_series_key(function: str, payload: Mapping[str, Any]) -> str:
    if function == "TIME_SERIES_INTRADAY":
        for key in payload:
            if str(key).startswith("Time Series"):
                return str(key)
        return "Time Series (60min)"
    return _TIME_SERIES_KEY_BY_FUNCTION.get(function, "Time Series (Daily)")


def _raise_for_alpha_vantage_error(payload: Mapping[str, Any]) -> None:
    for key in ("Error Message", "Note", "Information"):
        value = payload.get(key)
        if value:
            raise AlphaVantageProviderError(str(value)[:240])


def _select_symbol_match(symbol: str, matches: list[Any]) -> dict[str, Any] | None:
    symbol_upper = symbol.upper()
    normalized = symbol_upper.replace(".", "-")
    candidates = [item for item in matches if isinstance(item, dict)]
    exact = [item for item in candidates if str(item.get("1. symbol") or "").upper() in {symbol_upper, normalized}]
    return (exact or candidates)[:1][0] if candidates else None


def _candidate_mcp_tool_names(function: str) -> list[str]:
    lowered = function.lower()
    return [
        function,
        lowered,
        f"get_{lowered}",
        f"alpha_vantage_{lowered}",
        "query",
        "alpha_vantage_query",
    ]


def _select_mcp_tool(function: str, candidates: list[str], tools: Any) -> str:
    tool_items = list(tools or [])
    by_name: dict[str, Any] = {}
    for tool in tool_items:
        name = str(getattr(tool, "name", "") or (tool.get("name") if isinstance(tool, dict) else ""))
        if name:
            by_name[name] = tool
    for candidate in candidates:
        if candidate in by_name:
            return candidate
    function_tokens = function.lower().split("_")
    for name, tool in by_name.items():
        description = str(getattr(tool, "description", "") or (tool.get("description") if isinstance(tool, dict) else "")).lower()
        haystack = f"{name.lower()} {description}"
        if all(token in haystack for token in function_tokens):
            return name
    raise AlphaVantageProviderError("mcp_tool_not_found")


def _mcp_result_to_json(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        _raise_for_alpha_vantage_error(result)
        return result
    content = getattr(result, "content", None) or []
    for item in content:
        text = getattr(item, "text", None)
        if text:
            data = json.loads(text)
            if isinstance(data, dict):
                _raise_for_alpha_vantage_error(data)
                return data
    structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        _raise_for_alpha_vantage_error(structured)
        return structured
    raise AlphaVantageProviderError("mcp_result_not_json")


def _mcp_headers(api_key: str, header_name: str, scheme: str) -> dict[str, str]:
    if not header_name:
        return {}
    value = f"{scheme} {api_key}".strip() if scheme else api_key
    return {header_name: value}


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _in_range(timestamp: datetime, start: datetime, end: datetime) -> bool:
    ts = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=UTC)
    start_ts = start if start.tzinfo else start.replace(tzinfo=UTC)
    end_ts = end if end.tzinfo else end.replace(tzinfo=UTC)
    return start_ts <= ts <= end_ts


def _days_ago(days: int) -> datetime:
    return datetime.now(UTC).replace(microsecond=0) - timedelta(days=days)
