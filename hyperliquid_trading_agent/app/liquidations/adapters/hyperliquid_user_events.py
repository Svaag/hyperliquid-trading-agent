"""Hyperliquid account-scoped exact liquidations (``account_private``).

Subscribes to ``userFills`` for a configured set of addresses (own accounts,
known whales, and the HLP liquidator vault). A fill carrying a ``liquidation``
object is an exact liquidation for that account — ``method`` ``"backstop"`` marks
the HLP-vault backstop path; otherwise a normal book liquidation. Exact, but only
for watched accounts, hence ``account_private`` (the global confirmed path is the
gRPC ``StreamFills`` adapter, stubbed for now).

The initial ``userFills`` snapshot is skipped so old fills aren't re-surfaced as
fresh tape on every reconnect; live incremental fills are processed.
"""

from __future__ import annotations

import time
from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.liquidations.adapters._hyperliquid_base import HyperliquidWsAdapter
from hyperliquid_trading_agent.app.liquidations.adapters._ws import dec, to_ms
from hyperliquid_trading_agent.app.liquidations.models import EventType, LiquidationEvent, SourceIntegrity

_DIR_TO_SIDE = {"close long": "long", "close short": "short"}


def parse_fill(fill: dict[str, Any]) -> LiquidationEvent | None:
    liquidation = fill.get("liquidation")
    if not isinstance(liquidation, dict):
        return None
    method = str(liquidation.get("method", "")).lower()
    event_type = EventType.BACKSTOP if method == "backstop" else EventType.LIQUIDATION
    direction = str(fill.get("dir", "")).lower()
    side = _DIR_TO_SIDE.get(direction, "unknown")
    return LiquidationEvent(
        venue="hyperliquid",
        source="hyperliquid_user_events",
        source_integrity=SourceIntegrity.ACCOUNT_PRIVATE,
        event_type=event_type,
        symbol=str(fill.get("coin", "")).upper(),
        liquidated_side=side,  # type: ignore[arg-type]
        raw_side=direction or None,
        price=dec(fill.get("px")),
        size_base=dec(fill.get("sz")),
        mark_price=dec(liquidation.get("markPx")),
        timestamp_ms=to_ms(fill.get("time")) or int(time.time() * 1000),
        received_at_ms=int(time.time() * 1000),
        trade_id=str(fill.get("tid") or fill.get("hash") or "") or None,
        liquidated_user=str(liquidation.get("liquidatedUser") or "") or None,
        method=method or None,
        raw=fill,
    )


class HyperliquidUserEventsAdapter(HyperliquidWsAdapter):
    source = "hyperliquid_user_events"
    source_integrity = SourceIntegrity.ACCOUNT_PRIVATE

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._addresses = settings.hl_watch_address_list

    def _subscriptions(self) -> list[dict[str, Any]]:
        return [{"type": "userFills", "user": address} for address in self._addresses]

    def _decode(self, message: dict[str, Any]) -> list[LiquidationEvent]:
        if message.get("channel") != "userFills":
            return []
        data = message.get("data")
        if not isinstance(data, dict) or data.get("isSnapshot"):
            return []  # skip the historical snapshot; only live increments
        events: list[LiquidationEvent] = []
        for fill in data.get("fills") or []:
            if isinstance(fill, dict):
                event = parse_fill(fill)
                if event is not None:
                    events.append(event)
        return events
