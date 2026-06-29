"""Shared Hyperliquid websocket plumbing for the public + account adapters.

Both HL adapters speak the same raw protocol (subscribe messages + app-level
``{"method":"ping"}`` keepalive, since HL closes idle sockets after 60s), and
differ only in their subscriptions and how they decode a frame. Using raw
websockets keeps them uniformly testable (decode is a pure function) and
consistent with the Aster/Lighter adapters; the repo already builds on
``hyperliquid-python-sdk`` elsewhere.
"""

from __future__ import annotations

import json
from abc import abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.liquidations.adapters._ws import ws_json_messages
from hyperliquid_trading_agent.app.liquidations.adapters.base import LiquidationAdapter
from hyperliquid_trading_agent.app.liquidations.models import LiquidationEvent


class HyperliquidWsAdapter(LiquidationAdapter):
    venue = "hyperliquid"

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._url = settings.hyperliquid_ws_url

    @abstractmethod
    def _subscriptions(self) -> list[dict[str, Any]]:
        """The ``subscription`` payloads to send on connect."""
        raise NotImplementedError

    @abstractmethod
    def _decode(self, message: dict[str, Any]) -> list[LiquidationEvent]:
        """Decode one frame into zero or more events."""
        raise NotImplementedError

    async def _connect_and_stream(self) -> AsyncIterator[LiquidationEvent]:
        subscriptions = self._subscriptions()

        async def _open(ws: Any) -> None:
            for sub in subscriptions:
                await ws.send(json.dumps({"method": "subscribe", "subscription": sub}))

        async for message in ws_json_messages(
            self._url, on_open=_open, ping_payload={"method": "ping"}, recv_timeout=70.0
        ):
            for event in self._decode(message):
                yield event
