from __future__ import annotations

import time
from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.hip4.orderbook import parse_l2_book
from hyperliquid_trading_agent.app.hip4.schemas import NormalizedOutcomeBook
from hyperliquid_trading_agent.app.hyperliquid.ws_worker import SubscriptionSpec


class Hip4WsSubscriptionManager:
    def __init__(self, *, settings: Settings, ws_worker: Any | None = None, hip4_client: Any | None = None):
        self.settings = settings
        self.ws_worker = ws_worker
        self.hip4_client = hip4_client
        self.books: dict[str, NormalizedOutcomeBook] = {}
        self._subscription_ids: dict[str, str] = {}
        self._desired_coins: list[str] = []
        self.last_resnapshot_at_ms: int | None = None
        self.last_reconnect_seen: int = 0

    async def update_hot_subscriptions(self, coins: list[str]) -> None:
        capped = _dedupe(coins)[: self.settings.hip4_ws_max_subscriptions]
        self._desired_coins = capped
        if self.ws_worker is None or not self.settings.hip4_ws_enabled:
            return
        for coin in capped:
            if coin in self._subscription_ids:
                continue
            sub_id = await self.ws_worker.subscribe(SubscriptionSpec("l2Book", coin=coin), self._on_l2_book)
            self._subscription_ids[coin] = sub_id
        for coin in list(self._subscription_ids):
            if coin not in capped:
                await self.ws_worker.unsubscribe(self._subscription_ids.pop(coin))

    async def stop(self) -> None:
        if self.ws_worker is None:
            return
        for sub_id in list(self._subscription_ids.values()):
            await self.ws_worker.unsubscribe(sub_id)
        self._subscription_ids.clear()

    async def resnapshot(self) -> None:
        if self.hip4_client is None:
            return
        for coin in self._desired_coins:
            try:
                payload = await self.hip4_client.l2_book(coin)
            except Exception:
                continue
            self.books[coin] = parse_l2_book(coin, payload, source="rest", as_of_ms=int(time.time() * 1000))
        self.last_resnapshot_at_ms = int(time.time() * 1000)

    async def maybe_resnapshot_after_reconnect(self) -> None:
        if self.ws_worker is None or not self.settings.hip4_ws_resnapshot_on_reconnect:
            return
        status = self.ws_worker.status()
        reconnect_count = int(status.get("reconnect_count") or 0)
        if reconnect_count > self.last_reconnect_seen:
            self.last_reconnect_seen = reconnect_count
            await self.resnapshot()

    def mark_stale(self, *, now_ms: int | None = None) -> None:
        now = now_ms or int(time.time() * 1000)
        for coin, book in list(self.books.items()):
            if now - int(book.as_of_ms) > self.settings.hip4_scan_max_book_staleness_ms:
                self.books[coin] = book.model_copy(update={"stale": True})

    def status(self) -> dict[str, Any]:
        return {
            "desired_subscription_count": len(self._desired_coins),
            "active_subscription_count": len(self._subscription_ids),
            "book_count": len(self.books),
            "last_resnapshot_at_ms": self.last_resnapshot_at_ms,
            "subscription_budget": self.settings.hip4_ws_max_subscriptions,
        }

    async def _on_l2_book(self, message: dict[str, Any]) -> None:
        data = message.get("data")
        if not isinstance(data, dict):
            return
        coin = str(data.get("coin") or "")
        if not coin:
            return
        self.books[coin] = parse_l2_book(coin, data, source="ws", as_of_ms=int(time.time() * 1000))


def prioritize_hot_coins(*, allowlisted: list[str], active: list[str], liquid: list[str], remaining: list[str], budget: int) -> list[str]:
    return _dedupe([*allowlisted, *active, *liquid, *remaining])[:budget]


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out
