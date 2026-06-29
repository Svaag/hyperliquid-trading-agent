from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import websockets

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.world_model.adapters import (
    _json_list,
    _safe_float,
    _stable_id,
    _symbols_from_text,
    _topics_from_text,
    _with_probability_delta,
)
from hyperliquid_trading_agent.app.world_model.reducer import now_ms
from hyperliquid_trading_agent.app.world_model.schemas import PredictionMarketSignal
from hyperliquid_trading_agent.app.world_model.service import WorldModelService

log = get_logger(__name__)


@dataclass
class StreamStatus:
    name: str
    enabled: bool
    connected: bool = False
    last_message_at_ms: int | None = None
    reconnect_count: int = 0
    error_count: int = 0
    gap_repairs: int = 0
    subscribed_markets: int = 0
    subscriptions: list[str] = field(default_factory=list)
    last_error: str | None = None
    last_event: dict[str, Any] | None = None

    def as_dict(self, *, stale_after_ms: int) -> dict[str, Any]:
        last_message = self.last_message_at_ms
        stale = self.connected and (last_message is None or now_ms() - last_message > stale_after_ms)
        return {
            "name": self.name,
            "enabled": self.enabled,
            "connected": self.connected,
            "stale": stale,
            "last_message_at_ms": last_message,
            "reconnect_count": self.reconnect_count,
            "error_count": self.error_count,
            "gap_repairs": self.gap_repairs,
            "subscribed_markets": self.subscribed_markets,
            "subscriptions": self.subscriptions[:50],
            "last_error": self.last_error,
            "last_event": self.last_event,
            "execution_authority": "none",
        }


class WorldModelStreamAdapter:
    name = "base"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.status = StreamStatus(name=self.name, enabled=self.enabled)
        self._stop = asyncio.Event()

    @property
    def enabled(self) -> bool:
        return False

    async def run(self, service: WorldModelService) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        self._stop.set()

    def status_dict(self) -> dict[str, Any]:
        return self.status.as_dict(stale_after_ms=max(1, self.settings.world_model_stream_stale_after_seconds) * 1000)


class WorldModelStreamService:
    def __init__(self, *, settings: Settings, world_model_service: WorldModelService, adapters: list[WorldModelStreamAdapter] | None = None):
        self.settings = settings
        self.world_model_service = world_model_service
        self.adapters: dict[str, WorldModelStreamAdapter] = {
            adapter.name: adapter for adapter in (adapters if adapters is not None else [PolymarketWebSocketAdapter(settings)])
        }
        self.running = False
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        if not self.settings.world_model_streams_enabled or self.running:
            return
        self.running = True
        for adapter in self.adapters.values():
            adapter.status.enabled = adapter.enabled
            if adapter.enabled:
                self._tasks.append(asyncio.create_task(self._supervise(adapter), name=f"world-model-stream-{adapter.name}"))
        log.info("world_model_streams_started", adapters=[name for name, adapter in self.adapters.items() if adapter.enabled])

    async def stop(self) -> None:
        self.running = False
        for adapter in self.adapters.values():
            try:
                await adapter.stop()
            except Exception:  # pragma: no cover - cleanup best-effort
                pass
        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []

    async def _supervise(self, adapter: WorldModelStreamAdapter) -> None:
        backoff = 5
        while self.running:
            adapter.status.connected = False
            try:
                await adapter.run(self.world_model_service)
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - external stream behavior
                adapter.status.connected = False
                adapter.status.error_count += 1
                adapter.status.reconnect_count += 1
                adapter.status.last_error = type(exc).__name__
                log.warning("world_model_stream_restart", adapter=adapter.name, error=type(exc).__name__)
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
                backoff = min(max(5, self.settings.world_model_stream_reconnect_max_seconds), backoff * 2)
            else:
                backoff = 5
        adapter.status.connected = False

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.world_model_streams_enabled,
            "running": self.running,
            "streams": [adapter.status_dict() for adapter in self.adapters.values()],
            "execution_authority": "none",
        }


@dataclass(frozen=True)
class PolymarketSubscription:
    asset_id: str
    market_id: str
    question: str
    outcome_name: str
    symbols: list[str]
    topics: list[str]
    liquidity_usd: float | None = None
    volume_usd: float | None = None


class PolymarketWebSocketAdapter(WorldModelStreamAdapter):
    name = "polymarket_ws"

    @property
    def enabled(self) -> bool:
        return self.settings.world_model_streams_enabled and self.settings.world_model_polymarket_ws_enabled

    async def run(self, service: WorldModelService) -> None:
        subscriptions = await self._discover_subscriptions()
        if not subscriptions:
            self.status.subscribed_markets = 0
            self.status.subscriptions = []
            raise RuntimeError("polymarket_no_subscriptions")
        by_asset = {item.asset_id: item for item in subscriptions}
        self.status.subscribed_markets = len({item.market_id for item in subscriptions})
        self.status.subscriptions = [item.asset_id for item in subscriptions]
        async with websockets.connect(self.settings.world_model_polymarket_ws_url, ping_interval=None) as ws:
            self.status.connected = True
            await ws.send(json.dumps({"type": "market", "assets_ids": list(by_asset.keys())}))
            heartbeat = asyncio.create_task(self._heartbeat(ws), name="polymarket-ws-ping")
            try:
                while not self._stop.is_set():
                    raw = await ws.recv()
                    await self._handle_message(raw, by_asset=by_asset, service=service)
            finally:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
                self.status.connected = False

    async def _heartbeat(self, ws: Any) -> None:
        interval = max(1, self.settings.world_model_polymarket_ws_ping_seconds)
        while not self._stop.is_set():
            await asyncio.sleep(interval)
            await ws.send("PING")

    async def _discover_subscriptions(self) -> list[PolymarketSubscription]:
        url = self.settings.world_model_polymarket_base_url.rstrip("/") + "/markets"
        params = {"active": "true", "closed": "false", "limit": self.settings.world_model_adapter_max_items}
        async with httpx.AsyncClient(timeout=self.settings.world_model_adapter_timeout_seconds) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
        markets = data.get("markets", data) if isinstance(data, dict) else data
        out: list[PolymarketSubscription] = []
        for market in list(markets or [])[: self.settings.world_model_adapter_max_items]:
            out.extend(_polymarket_subscriptions(market, self.settings))
        return out

    async def _handle_message(self, raw: Any, *, by_asset: dict[str, PolymarketSubscription], service: WorldModelService) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode()
        if raw in {"PONG", "PING"}:
            return
        try:
            message = json.loads(str(raw))
        except json.JSONDecodeError:
            return
        messages = message if isinstance(message, list) else [message]
        for item in messages:
            for signal in _polymarket_ws_signals(item, by_asset=by_asset, now=now_ms()):
                signal = _with_probability_delta(service, signal)
                await service.observe_prediction_market_signal(signal)
                self.status.last_message_at_ms = now_ms()
                self.status.last_error = None
                self.status.last_event = {
                    "signal_id": signal.signal_id,
                    "market_id": signal.market_id,
                    "venue": signal.venue,
                    "probability": signal.implied_probability,
                    "status": signal.status,
                }


def _polymarket_subscriptions(market: dict[str, Any], settings: Settings) -> list[PolymarketSubscription]:
    market_id = str(market.get("id") or market.get("conditionId") or market.get("slug") or _stable_id("pm_poly_market", market))
    question = str(market.get("question") or market.get("title") or market.get("slug") or "")
    outcomes = [str(item) for item in (_json_list(market.get("outcomes")) or ["YES"])]
    token_ids = [str(item) for item in (_json_list(market.get("clobTokenIds")) or _json_list(market.get("clob_token_ids")) or []) if item]
    liquidity = _safe_float(market.get("liquidity") or market.get("liquidityNum"))
    volume = _safe_float(market.get("volume") or market.get("volumeNum"))
    symbols = _symbols_from_text(question, settings)
    topics = ["prediction_market", "polymarket", *_topics_from_text(question)]
    out = []
    for idx, asset_id in enumerate(token_ids):
        outcome = outcomes[idx] if idx < len(outcomes) else asset_id
        out.append(
            PolymarketSubscription(
                asset_id=asset_id,
                market_id=market_id,
                question=question,
                outcome_name=str(outcome),
                symbols=symbols,
                topics=topics,
                liquidity_usd=liquidity,
                volume_usd=volume,
            )
        )
    return out


def _polymarket_ws_signals(message: dict[str, Any], *, by_asset: dict[str, PolymarketSubscription], now: int) -> list[PredictionMarketSignal]:
    if not isinstance(message, dict):
        return []
    raw_type = str(message.get("event_type") or message.get("type") or "").lower()
    if raw_type == "market_resolved":
        return _polymarket_resolved_signals(message, by_asset=by_asset, now=now)
    changes = message.get("changes") if isinstance(message.get("changes"), list) else [message]
    out: list[PredictionMarketSignal] = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        asset_id = str(change.get("asset_id") or change.get("assetId") or message.get("asset_id") or message.get("assetId") or "")
        subscription = by_asset.get(asset_id)
        if subscription is None:
            continue
        probability = _first_float(change, message, keys=["price", "best_bid", "bestBid", "bid", "mid"])
        best_bid = _first_float(change, message, keys=["best_bid", "bestBid", "bid"])
        best_ask = _first_float(change, message, keys=["best_ask", "bestAsk", "ask"])
        if probability is None and best_bid is not None and best_ask is not None:
            probability = max(0.0, min(1.0, (best_bid + best_ask) / 2.0))
        out.append(_signal_from_subscription(subscription, probability=probability, best_bid=best_bid, best_ask=best_ask, status="open", now=now, raw=message))
    return out


def _polymarket_resolved_signals(message: dict[str, Any], *, by_asset: dict[str, PolymarketSubscription], now: int) -> list[PredictionMarketSignal]:
    asset_id = str(message.get("asset_id") or message.get("assetId") or message.get("token_id") or message.get("tokenId") or "")
    subscriptions = [by_asset[asset_id]] if asset_id in by_asset else list(by_asset.values())
    winning = str(message.get("winning_asset_id") or message.get("winningAssetId") or "")
    out = []
    for subscription in subscriptions:
        probability = 1.0 if winning and subscription.asset_id == winning else 0.0 if winning else None
        out.append(_signal_from_subscription(subscription, probability=probability, best_bid=None, best_ask=None, status="settled", now=now, raw=message))
    return out


def _signal_from_subscription(
    subscription: PolymarketSubscription,
    *,
    probability: float | None,
    best_bid: float | None,
    best_ask: float | None,
    status: str,
    now: int,
    raw: dict[str, Any],
) -> PredictionMarketSignal:
    return PredictionMarketSignal(
        signal_id=_stable_id("pm_poly", subscription.market_id, subscription.outcome_name),
        venue="polymarket",
        market_id=subscription.market_id,
        question=subscription.question,
        outcome_id=subscription.asset_id,
        outcome_name=subscription.outcome_name,
        symbols=subscription.symbols,
        topics=subscription.topics,
        implied_probability=probability,
        best_bid=best_bid,
        best_ask=best_ask,
        liquidity_usd=subscription.liquidity_usd,
        volume_usd=subscription.volume_usd,
        status=status,  # type: ignore[arg-type]
        as_of_ms=now,
        confidence=0.75 if probability is not None else 0.45,
        metadata={
            "source": "polymarket_websocket",
            "source_id": f"polymarket:{subscription.market_id}:{subscription.asset_id}",
            "adapter": "polymarket_ws",
            "raw_event": raw,
            "paper_only": True,
            "execution_authority": "none",
        },
    )


def _first_float(*containers: dict[str, Any], keys: list[str]) -> float | None:
    for container in containers:
        for key in keys:
            value = _safe_float(container.get(key))
            if value is not None:
                return max(0.0, min(1.0, value))
    return None
