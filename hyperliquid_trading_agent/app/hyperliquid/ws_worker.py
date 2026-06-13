from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import websockets

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class SubscriptionSpec:
    type: str
    coin: str | None = None
    user: str | None = None

    def payload(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": self.type}
        if self.coin:
            data["coin"] = self.coin
        if self.user:
            data["user"] = self.user
        return data


@dataclass
class WebSocketCache:
    all_mids: dict[str, str] = field(default_factory=dict)
    active_asset_ctxs: dict[str, dict[str, Any]] = field(default_factory=dict)
    updated_at_ms: int = 0


class HyperliquidWebSocketWorker:
    """Optional WebSocket cache worker.

    Disabled by default for tonight's REST-first MVP. When enabled, it maintains
    a best-effort in-memory cache for low-latency future paths and reconnects on
    server-side disconnects as recommended by Hyperliquid docs.
    """

    def __init__(self, settings: Settings, subscriptions: list[SubscriptionSpec] | None = None):
        self.settings = settings
        self.enabled = settings.hyperliquid_ws_enabled
        self.subscriptions = subscriptions or [SubscriptionSpec("allMids")]
        self.cache = WebSocketCache()
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if not self.enabled:
            return
        while not self._stop.is_set():
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - external websocket behavior
                log.warning("hyperliquid_ws_reconnect", error=type(exc).__name__)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=5)
                except TimeoutError:
                    continue

    async def stop(self) -> None:
        self._stop.set()

    async def _run_once(self) -> None:
        async with websockets.connect(self.settings.hyperliquid_ws_url, ping_interval=None) as ws:
            for spec in self.subscriptions:
                await ws.send(json.dumps({"method": "subscribe", "subscription": spec.payload()}))
            last_rx = time.monotonic()
            while not self._stop.is_set():
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=30)
                    last_rx = time.monotonic()
                except TimeoutError:
                    if time.monotonic() - last_rx > 45:
                        await ws.send(json.dumps({"method": "ping"}))
                    continue
                self._handle_message(json.loads(message))

    def _handle_message(self, message: dict[str, Any]) -> None:
        channel = message.get("channel")
        data = message.get("data")
        self.cache.updated_at_ms = int(time.time() * 1000)
        if channel == "allMids" and isinstance(data, dict):
            mids = data.get("mids", data)
            if isinstance(mids, dict):
                self.cache.all_mids.update({str(key): str(value) for key, value in mids.items()})
        elif channel == "activeAssetCtx" and isinstance(data, dict):
            coin = data.get("coin") or data.get("ctx", {}).get("coin")
            if coin:
                self.cache.active_asset_ctxs[str(coin)] = data
