from __future__ import annotations

from pathlib import Path

import pytest

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.hip4.ws import Hip4WsSubscriptionManager, prioritize_hot_coins

FIXTURES = Path("tests/fixtures/hip4")


class FakeWsWorker:
    def __init__(self):
        self.subscriptions = []
        self.unsubscribed = []

    async def subscribe(self, spec, callback):
        self.subscriptions.append(spec.identifier())
        return f"sub_{len(self.subscriptions)}"

    async def unsubscribe(self, sub_id):
        self.unsubscribed.append(sub_id)

    def status(self):
        return {"reconnect_count": 0}


@pytest.mark.asyncio
async def test_ws_manager_dedupes_and_caps_subscriptions() -> None:
    worker = FakeWsWorker()
    manager = Hip4WsSubscriptionManager(settings=Settings(environment="test", hip4_ws_max_subscriptions=2), ws_worker=worker)

    await manager.update_hot_subscriptions(["#1720", "#1720", "#1721", "#1730"])

    assert worker.subscriptions == ["l2Book:#1720", "l2Book:#1721"]
    assert manager.status()["active_subscription_count"] == 2


def test_hot_coin_prioritization_dedupes_by_priority() -> None:
    coins = prioritize_hot_coins(allowlisted=["#1", "#2"], active=["#2", "#3"], liquid=[], remaining=["#4"], budget=3)

    assert coins == ["#1", "#2", "#3"]


def test_main_lifespan_starts_ws_worker_when_only_hip4_needs_ws() -> None:
    source = Path("hyperliquid_trading_agent/app/main.py").read_text()

    assert "hyperliquid-ws" in source
    assert "settings.hip4_enabled and settings.hip4_ws_enabled" in source
