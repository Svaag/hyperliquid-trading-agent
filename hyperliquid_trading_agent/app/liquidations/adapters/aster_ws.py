"""Aster perpetuals — public forced-liquidation stream (``snapshot_throttled``).

Binance-compatible all-market ``!forceOrder@arr`` stream. **Honesty note:** Aster
only pushes the *latest* liquidation order per symbol within each ~1000ms window,
so this stream coalesces/drops under bursty liquidation cascades — hence the
``snapshot_throttled`` integrity grade, never ``confirmed``.

A forced-order's side is the side of the *closing* order: ``SELL`` closes (=
liquidates) a long, ``BUY`` liquidates a short.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.liquidations.adapters._ws import dec, to_ms, ws_json_messages
from hyperliquid_trading_agent.app.liquidations.adapters.base import LiquidationAdapter
from hyperliquid_trading_agent.app.liquidations.models import EventType, LiquidationEvent, SourceIntegrity

_QUOTES = ("USDT", "USDC", "BUSD", "FDUSD", "USD")


def normalize_symbol(symbol: str) -> str:
    s = symbol.upper()
    for quote in _QUOTES:
        if s.endswith(quote) and len(s) > len(quote):
            return s[: -len(quote)]
    return s


def parse_force_order(message: dict[str, Any]) -> LiquidationEvent | None:
    """Map an Aster forceOrder frame to a `LiquidationEvent` (None if not one)."""
    data = message.get("data", message)  # unwrap combined-stream envelope if present
    if not isinstance(data, dict) or data.get("e") != "forceOrder":
        return None
    o = data.get("o") or {}
    side_raw = str(o.get("S", "")).upper()
    liquidated_side = "long" if side_raw == "SELL" else "short" if side_raw == "BUY" else "unknown"
    size = dec(o.get("z")) or dec(o.get("q"))  # accumulated filled, else original qty
    avg_price = dec(o.get("ap"))
    if avg_price == 0:
        avg_price = None
    ts = to_ms(o.get("T") or data.get("E"))
    return LiquidationEvent(
        venue="aster",
        source="aster_ws",
        source_integrity=SourceIntegrity.SNAPSHOT_THROTTLED,
        event_type=EventType.LIQUIDATION,
        symbol=normalize_symbol(str(o.get("s", ""))),
        venue_market_id=str(o.get("s", "")) or None,
        liquidated_side=liquidated_side,  # type: ignore[arg-type]
        raw_side=side_raw or None,
        price=dec(o.get("p")),
        avg_price=avg_price,
        size_base=size,
        timestamp_ms=ts or int(time.time() * 1000),
        received_at_ms=int(time.time() * 1000),
        raw=data,
    )


class AsterAdapter(LiquidationAdapter):
    venue = "aster"
    source = "aster_ws"
    source_integrity = SourceIntegrity.SNAPSHOT_THROTTLED

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._url = settings.aster_ws_url.rstrip("/") + "/ws/!forceOrder@arr"

    async def _connect_and_stream(self) -> AsyncIterator[LiquidationEvent]:
        async for message in ws_json_messages(self._url):
            event = parse_force_order(message)
            if event is not None:
                yield event
