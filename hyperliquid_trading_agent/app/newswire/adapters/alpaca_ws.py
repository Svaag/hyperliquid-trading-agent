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


class AlpacaNewsAdapter(NewswireAdapter):
    """Alpaca News WebSocket (free, Benzinga-sourced) — the near-instant catalyst layer.

    Protocol: connect -> {action:auth} -> on authenticated {action:subscribe,news:[...]}.
    News frames arrive as ``{"T":"n", ...}``. Disconnects raise so the service supervisor
    reconnects with backoff.
    """

    name = "alpaca"

    def __init__(self, *, ws_url: str, api_key: str, api_secret: str, symbols: list[str]):
        self.ws_url = ws_url
        self.api_key = api_key
        self.api_secret = api_secret
        self.symbols = symbols or ["*"]
        self._stop = asyncio.Event()
        self._authenticated = False

    async def run(self, emit: RawEmit) -> None:
        self._authenticated = False
        async with websockets.connect(self.ws_url, ping_interval=20) as ws:
            await ws.send(json.dumps({"action": "auth", "key": self.api_key, "secret": self.api_secret}))
            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                except TimeoutError:
                    continue
                if isinstance(raw, bytes):
                    raw = raw.decode()
                messages = json.loads(raw)
                if not isinstance(messages, list):
                    messages = [messages]
                for message in messages:
                    await self._handle(ws, message, emit)

    async def stop(self) -> None:
        self._stop.set()

    async def _handle(self, ws: Any, message: dict[str, Any], emit: RawEmit) -> None:
        kind = message.get("T")
        if kind == "success" and message.get("msg") == "authenticated":
            self._authenticated = True
            await ws.send(json.dumps({"action": "subscribe", "news": self.symbols}))
            log.info("alpaca_news_subscribed", symbols=self.symbols)
        elif kind == "error":
            raise RuntimeError(f"alpaca_news_error:{message.get('msg')}")
        elif kind == "n":
            await emit(self._to_raw(message))

    def _to_raw(self, message: dict[str, Any]) -> RawNewsItem:
        symbols = [str(symbol).upper() for symbol in (message.get("symbols") or [])]
        return RawNewsItem(
            source="alpaca",
            provider=str(message.get("source") or "benzinga"),
            transport="websocket",
            external_id=str(message.get("id")) if message.get("id") is not None else None,
            headline=str(message.get("headline") or ""),
            body=str(message.get("summary") or ""),
            url=message.get("url"),
            author=message.get("author"),
            published_at_ms=created_at_ms_from(message.get("created_at")),
            symbols=symbols,
            raw=message,
        )

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "authenticated": self._authenticated, "symbols": self.symbols}
