from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import websockets

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.metrics import HL_WS_MESSAGES, HL_WS_RECONNECTS

log = get_logger(__name__)

MessageCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass(frozen=True)
class SubscriptionSpec:
    type: str
    coin: str | None = None
    user: str | None = None
    interval: str | None = None

    def payload(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": self.type}
        if self.coin:
            data["coin"] = self.coin
        if self.user:
            data["user"] = self.user
        if self.interval:
            data["interval"] = self.interval
        return data

    def identifier(self) -> str:
        subscription_type = self.type
        if subscription_type == "allMids":
            return "allMids"
        if subscription_type in {"l2Book", "trades", "bbo", "activeAssetCtx"}:
            return f"{subscription_type}:{(self.coin or '').lower()}"
        if subscription_type == "candle":
            return f"candle:{(self.coin or '').lower()},{self.interval or ''}"
        if subscription_type in {"userEvents", "orderUpdates"}:
            return subscription_type
        if self.user:
            return f"{subscription_type}:{self.user.lower()}"
        return subscription_type


@dataclass
class WebSocketCache:
    all_mids: dict[str, str] = field(default_factory=dict)
    active_asset_ctxs: dict[str, dict[str, Any]] = field(default_factory=dict)
    updated_at_ms: int = 0


class HyperliquidWebSocketWorker:
    """Dynamic Hyperliquid WebSocket fan-out worker.

    The worker is lazy: if no static or dynamic subscriptions exist, it waits
    without opening a socket. This keeps default runtime overhead near zero, and
    lets the position tracker subscribe to one low-volume `allMids` stream when
    active trackers exist.
    """

    def __init__(self, settings: Settings, subscriptions: list[SubscriptionSpec] | None = None):
        self.settings = settings
        self.static_subscriptions = subscriptions if subscriptions is not None else ([SubscriptionSpec("allMids")] if settings.hyperliquid_ws_enabled else [])
        self.cache = WebSocketCache()
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._callbacks: dict[str, tuple[SubscriptionSpec, MessageCallback]] = {}
        self._subscribed_identifiers: set[str] = set()
        self._ws: Any | None = None
        self._connected = False
        self._last_message_at_ms: int | None = None
        self._reconnect_count = 0

    async def start(self) -> None:
        while not self._stop.is_set():
            await self._wait_for_desired_subscriptions()
            if self._stop.is_set():
                break
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - external websocket behavior
                self._reconnect_count += 1
                HL_WS_RECONNECTS.inc()
                log.warning("hyperliquid_ws_reconnect", error=type(exc).__name__)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=5)
                except TimeoutError:
                    continue

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # pragma: no cover - external websocket cleanup
                pass

    async def subscribe(self, spec: SubscriptionSpec, callback: MessageCallback) -> str:
        subscription_id = uuid4().hex
        async with self._lock:
            self._callbacks[subscription_id] = (spec, callback)
            ws = self._ws
            should_send = ws is not None and spec.identifier() not in self._subscribed_identifiers
        self._wake.set()
        if should_send:
            await self._send_subscribe(spec)
        return subscription_id

    async def unsubscribe(self, subscription_id: str) -> None:
        async with self._lock:
            item = self._callbacks.pop(subscription_id, None)
            if item is None:
                return
            spec = item[0]
            identifier = spec.identifier()
            should_unsubscribe = self._ws is not None and identifier not in self._desired_specs_unlocked()
            no_desired = not self._desired_specs_unlocked()
        if should_unsubscribe:
            await self._send_unsubscribe(spec)
        if no_desired and self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # pragma: no cover
                pass
        self._wake.set()

    def status(self) -> dict[str, Any]:
        desired = self._desired_specs_unlocked_no_lock()
        return {
            "connected": self._connected,
            "subscriptions": sorted(desired.keys()),
            "callback_count": len(self._callbacks),
            "static_subscription_count": len(self.static_subscriptions),
            "updated_at_ms": self.cache.updated_at_ms,
            "last_message_at_ms": self._last_message_at_ms,
            "reconnect_count": self._reconnect_count,
        }

    async def _wait_for_desired_subscriptions(self) -> None:
        while not self._stop.is_set():
            async with self._lock:
                if self._desired_specs_unlocked():
                    return
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1)
            except TimeoutError:
                continue

    async def _run_once(self) -> None:
        async with websockets.connect(self.settings.hyperliquid_ws_url, ping_interval=None) as ws:
            self._ws = ws
            self._connected = True
            self._subscribed_identifiers = set()
            async with self._lock:
                specs = list(self._desired_specs_unlocked().values())
            for spec in specs:
                await self._send_subscribe(spec)
            last_rx = time.monotonic()
            try:
                while not self._stop.is_set():
                    async with self._lock:
                        if not self._desired_specs_unlocked():
                            break
                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=30)
                        last_rx = time.monotonic()
                    except TimeoutError:
                        if time.monotonic() - last_rx > 45:
                            await ws.send(json.dumps({"method": "ping"}))
                        continue
                    if isinstance(message, bytes):
                        message = message.decode()
                    if message == "Websocket connection established.":
                        continue
                    await self._handle_message(json.loads(message))
            finally:
                self._connected = False
                self._ws = None
                self._subscribed_identifiers = set()

    async def _send_subscribe(self, spec: SubscriptionSpec) -> None:
        ws = self._ws
        if ws is None:
            return
        await ws.send(json.dumps({"method": "subscribe", "subscription": spec.payload()}))
        self._subscribed_identifiers.add(spec.identifier())

    async def _send_unsubscribe(self, spec: SubscriptionSpec) -> None:
        ws = self._ws
        if ws is None:
            return
        await ws.send(json.dumps({"method": "unsubscribe", "subscription": spec.payload()}))
        self._subscribed_identifiers.discard(spec.identifier())

    async def _handle_message(self, message: dict[str, Any]) -> None:
        channel = message.get("channel")
        data = message.get("data")
        HL_WS_MESSAGES.labels(channel=str(channel or "unknown")).inc()
        now_ms = int(time.time() * 1000)
        self.cache.updated_at_ms = now_ms
        self._last_message_at_ms = now_ms
        if channel == "allMids" and isinstance(data, dict):
            mids = data.get("mids", data)
            if isinstance(mids, dict):
                self.cache.all_mids.update({str(key): str(value) for key, value in mids.items()})
        elif channel in {"activeAssetCtx", "activeSpotAssetCtx"} and isinstance(data, dict):
            coin = data.get("coin") or data.get("ctx", {}).get("coin")
            if coin:
                self.cache.active_asset_ctxs[str(coin)] = data

        identifier = _message_identifier(message)
        if identifier is None:
            return
        async with self._lock:
            callbacks = [callback for spec, callback in self._callbacks.values() if spec.identifier() == identifier]
        for callback in callbacks:
            try:
                result = callback(message)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # pragma: no cover - callback isolation
                log.warning("hyperliquid_ws_callback_failed", channel=channel, error=type(exc).__name__)

    def _desired_specs_unlocked(self) -> dict[str, SubscriptionSpec]:
        desired = {spec.identifier(): spec for spec in self.static_subscriptions}
        for spec, _callback in self._callbacks.values():
            desired[spec.identifier()] = spec
        return desired

    def _desired_specs_unlocked_no_lock(self) -> dict[str, SubscriptionSpec]:
        desired = {spec.identifier(): spec for spec in self.static_subscriptions}
        for spec, _callback in self._callbacks.values():
            desired[spec.identifier()] = spec
        return desired


def _message_identifier(message: dict[str, Any]) -> str | None:
    channel = message.get("channel")
    data = message.get("data")
    if channel == "allMids":
        return "allMids"
    if channel in {"l2Book", "bbo", "activeAssetCtx", "activeSpotAssetCtx"} and isinstance(data, dict):
        subscription_channel = "activeAssetCtx" if channel == "activeSpotAssetCtx" else channel
        return f"{subscription_channel}:{str(data.get('coin', '')).lower()}"
    if channel == "trades" and isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            return f"trades:{str(first.get('coin', '')).lower()}"
    if channel == "candle" and isinstance(data, dict):
        return f"candle:{str(data.get('s', '')).lower()},{data.get('i', '')}"
    if channel in {"user", "orderUpdates"}:
        return "userEvents" if channel == "user" else "orderUpdates"
    if channel in {"userFills", "userFundings", "userNonFundingLedgerUpdates", "webData2"} and isinstance(data, dict):
        return f"{channel}:{str(data.get('user', '')).lower()}"
    return None
