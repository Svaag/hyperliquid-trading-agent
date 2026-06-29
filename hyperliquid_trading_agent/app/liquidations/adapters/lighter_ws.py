"""Lighter (zkLighter) perpetuals — verifiable liquidation trades (``confirmed``).

Lighter is the first DEX to expose *verifiable* matching/liquidations. The public
``trade/{market_index}`` channel carries a ``liquidation_trades`` array plus a
per-trade ``type`` of ``liquidation`` / ``deleverage`` / ``market-settlement`` —
an exact, confirmed source. We subscribe to every market (index→symbol resolved
from REST) and emit only the non-``trade`` events.

Side assumption: the liquidated account is the taker, so ``is_maker_ask`` (maker
on the ask) implies the taker is buying to close a **short**; otherwise a
**long**. Flagged here because it needs confirmation against live data; full
provenance is kept in ``raw`` for audit.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.liquidations.adapters._ws import dec, to_ms, ws_json_messages
from hyperliquid_trading_agent.app.liquidations.adapters.base import LiquidationAdapter
from hyperliquid_trading_agent.app.liquidations.models import EventType, LiquidationEvent, SourceIntegrity
from hyperliquid_trading_agent.app.logging import get_logger

log = get_logger(__name__)

_TYPE_TO_EVENT = {
    "liquidation": EventType.LIQUIDATION,
    "deleverage": EventType.DELEVERAGE,
    "market-settlement": EventType.MARKET_SETTLEMENT,
    "market_settlement": EventType.MARKET_SETTLEMENT,
}


def _market_index_from_channel(channel: Any) -> int | None:
    # "trade:0" / "trade/0"
    if not isinstance(channel, str):
        return None
    for sep in (":", "/"):
        if sep in channel:
            tail = channel.rsplit(sep, 1)[1]
            return int(tail) if tail.isdigit() else None
    return None


def parse_trade(trade: dict[str, Any], *, market_index: int, symbol: str, force_liquidation: bool) -> LiquidationEvent | None:
    """Map one Lighter trade object to a `LiquidationEvent` (None for normal trades)."""
    raw_type = str(trade.get("type", "")).lower()
    if raw_type in ("", "trade") and not force_liquidation:
        return None
    event_type = _TYPE_TO_EVENT.get(raw_type, EventType.LIQUIDATION if force_liquidation else None)
    if event_type is None:
        return None
    is_maker_ask = bool(trade.get("is_maker_ask"))
    liquidated_side = "short" if is_maker_ask else "long"
    return LiquidationEvent(
        venue="lighter",
        source="lighter_ws",
        source_integrity=SourceIntegrity.CONFIRMED,
        event_type=event_type,
        symbol=symbol,
        venue_market_id=str(market_index),
        liquidated_side=liquidated_side,  # type: ignore[arg-type]
        raw_side="maker_ask" if is_maker_ask else "maker_bid",
        price=dec(trade.get("price")),
        size_base=dec(trade.get("size")),
        notional_usd=dec(trade.get("usd_amount")),
        timestamp_ms=to_ms(trade.get("timestamp")) or int(time.time() * 1000),
        received_at_ms=int(time.time() * 1000),
        trade_id=str(trade.get("trade_id_str") or trade.get("trade_id") or "") or None,
        raw=trade,
    )


def iter_liquidations(message: dict[str, Any], symbols: dict[int, str]) -> list[LiquidationEvent]:
    market_index = _market_index_from_channel(message.get("channel"))
    if market_index is None:
        return []
    symbol = symbols.get(market_index, f"MKT{market_index}")
    events: list[LiquidationEvent] = []
    for trade in message.get("liquidation_trades") or []:
        if isinstance(trade, dict):
            event = parse_trade(trade, market_index=market_index, symbol=symbol, force_liquidation=True)
            if event is not None:
                events.append(event)
    for trade in message.get("trades") or []:
        if isinstance(trade, dict) and str(trade.get("type", "")).lower() not in ("", "trade"):
            event = parse_trade(trade, market_index=market_index, symbol=symbol, force_liquidation=False)
            if event is not None:
                events.append(event)
    return events


def _extract_market_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("order_books", "orderBooks", "markets", "order_book_details", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


async def load_markets(url: str) -> dict[int, str]:
    """Fetch market_index -> symbol from the Lighter REST API (best-effort)."""
    out: dict[int, str] = {}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("lighter_markets_fetch_failed", error=type(exc).__name__)
        return out
    for item in _extract_market_items(data):
        idx = item.get("market_id", item.get("market_index", item.get("index")))
        symbol = item.get("symbol") or item.get("name")
        if idx is not None and symbol:
            try:
                out[int(idx)] = str(symbol).upper()
            except (TypeError, ValueError):
                continue
    return out


class LighterAdapter(LiquidationAdapter):
    venue = "lighter"
    source = "lighter_ws"
    source_integrity = SourceIntegrity.CONFIRMED

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._url = settings.lighter_ws_url
        self._markets_url = settings.lighter_markets_url
        self._max_markets = settings.lighter_max_markets
        self._symbols: dict[int, str] = {}

    async def _connect_and_stream(self) -> AsyncIterator[LiquidationEvent]:
        self._symbols = await load_markets(self._markets_url)
        indices = sorted(self._symbols) if self._symbols else list(range(self._max_markets))

        async def _subscribe(ws: Any) -> None:
            import json

            for idx in indices:
                await ws.send(json.dumps({"type": "subscribe", "channel": f"trade/{idx}"}))

        async for message in ws_json_messages(self._url, on_open=_subscribe):
            for event in iter_liquidations(message, self._symbols):
                yield event
