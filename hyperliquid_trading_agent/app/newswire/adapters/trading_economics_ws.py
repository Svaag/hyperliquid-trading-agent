from __future__ import annotations

import asyncio
import json
from typing import Any

import websockets

from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.newswire.adapters.base import NewswireAdapter, RawEmit
from hyperliquid_trading_agent.app.newswire.normalize import created_at_ms_from
from hyperliquid_trading_agent.app.newswire.schemas import RawNewsItem

log = get_logger(__name__)


class TradingEconomicsAdapter(NewswireAdapter):
    """Trading Economics calendar WebSocket — scheduled macro releases (CPI, FOMC, NFP...).

    Defensive about the exact payload shape; emits one macro event per calendar update.
    """

    name = "trading_economics"

    def __init__(self, *, ws_url: str, api_key: str):
        self.ws_url = ws_url
        self.api_key = api_key
        self._stop = asyncio.Event()

    def _connect_url(self) -> str:
        sep = "&" if "?" in self.ws_url else "?"
        return f"{self.ws_url}{sep}client={self.api_key}" if self.api_key else self.ws_url

    async def run(self, emit: RawEmit) -> None:
        async with websockets.connect(self._connect_url(), ping_interval=20) as ws:
            await ws.send(json.dumps({"topic": "subscribe", "to": "calendar"}))
            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                except TimeoutError:
                    continue
                if isinstance(raw, bytes):
                    raw = raw.decode()
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                item = self._to_raw(message)
                if item is not None:
                    await emit(item)

    async def stop(self) -> None:
        self._stop.set()

    def _to_raw(self, message: dict[str, Any]) -> RawNewsItem | None:
        if not isinstance(message, dict) or not message.get("event"):
            return None
        country = str(message.get("country") or "")
        event = str(message.get("event") or "")
        actual = message.get("actual")
        forecast = message.get("forecast")
        previous = message.get("previous")
        headline = f"{country} {event}".strip()
        if actual is not None:
            headline += f": actual {actual}"
            if forecast is not None:
                headline += f" vs forecast {forecast}"
            if previous is not None:
                headline += f" (prev {previous})"
        external_id = str(message.get("CalendarId") or message.get("calendarId") or f"{country}:{event}:{message.get('date')}")
        return RawNewsItem(
            source="trading_economics",
            provider="trading_economics",
            transport="websocket",
            external_id=external_id,
            headline=headline,
            body=str(message.get("category") or ""),
            published_at_ms=created_at_ms_from(message.get("date")),
            asset_class="macro",
            event_type="macro",
            raw=message,
        )
