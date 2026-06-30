"""Hyperliquid public trades → derived liquidation *pressure* (``derived``).

Hyperliquid exposes no public all-liquidations feed, and a public trade cannot be
proven to be a liquidation. So this adapter is deliberately honest: it watches the
public ``trades`` feed for a few coins and emits ``liquidation_pressure`` /
``derived`` events only for large aggressive sweeps (>= a notional threshold) —
a forced-flow proxy, never a confirmed liquidation.

A large taker *sell* (HL side ``"A"``) is downward pressure → pressure on longs;
a large taker *buy* (``"B"``) → pressure on shorts.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.liquidations.adapters._hyperliquid_base import HyperliquidWsAdapter
from hyperliquid_trading_agent.app.liquidations.adapters._ws import dec, to_ms
from hyperliquid_trading_agent.app.liquidations.models import EventType, LiquidationEvent, SourceIntegrity


def parse_trade(trade: dict[str, Any], *, min_notional: float) -> LiquidationEvent | None:
    price = dec(trade.get("px"))
    size = dec(trade.get("sz"))
    if price is None or size is None:
        return None
    if float(price) * float(size) < min_notional:
        return None
    side = str(trade.get("side", ""))  # "A" = taker sell, "B" = taker buy
    liquidated_side = "long" if side == "A" else "short" if side == "B" else "unknown"
    return LiquidationEvent(
        venue="hyperliquid",
        source="hyperliquid_public_ws",
        source_integrity=SourceIntegrity.DERIVED,
        event_type=EventType.LIQUIDATION_PRESSURE,
        symbol=str(trade.get("coin", "")).upper(),
        liquidated_side=liquidated_side,  # type: ignore[arg-type]
        raw_side=side or None,
        price=price,
        size_base=size,
        timestamp_ms=to_ms(trade.get("time")) or int(time.time() * 1000),
        received_at_ms=int(time.time() * 1000),
        trade_id=str(trade.get("tid") or trade.get("hash") or "") or None,
        confidence=Decimal("0.3"),
        raw=trade,
    )


class HyperliquidPublicAdapter(HyperliquidWsAdapter):
    source = "hyperliquid_public_ws"
    source_integrity = SourceIntegrity.DERIVED

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._coins = settings.hl_public_coin_list
        self._min_notional = settings.hl_pressure_min_notional_usd

    def _subscriptions(self) -> list[dict[str, Any]]:
        return [{"type": "trades", "coin": coin} for coin in self._coins]

    def _decode(self, message: dict[str, Any]) -> list[LiquidationEvent]:
        if message.get("channel") != "trades":
            return []
        data = message.get("data")
        if not isinstance(data, list):
            return []
        events: list[LiquidationEvent] = []
        for trade in data:
            if isinstance(trade, dict):
                event = parse_trade(trade, min_notional=self._min_notional)
                if event is not None:
                    events.append(event)
        return events
