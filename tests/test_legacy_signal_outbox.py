from __future__ import annotations

import time
from typing import Any

import anyio

from hyperliquid_trading_agent.app.autonomy.schemas import TradeSignal
from hyperliquid_trading_agent.app.autonomy.service import AutonomousTradingLoopService
from hyperliquid_trading_agent.app.config import Settings


class _OutboxRepository:
    enabled = True

    def __init__(self) -> None:
        self.notifications: list[dict[str, Any]] = []

    async def enqueue_operational_notification(self, **kwargs: Any) -> str:
        self.notifications.append(kwargs)
        return "opn_legacy"


def _signal() -> TradeSignal:
    now = int(time.time() * 1000)
    return TradeSignal(
        id="legacy_signal_1",
        symbol="BTC",
        side="long",
        signal_type="momentum",
        score=80,
        confidence=0.75,
        created_at_ms=now,
        expires_at_ms=now + 30 * 60_000,
        entry=100,
        stop=98,
        take_profit=104,
        invalidation="momentum fails",
        thesis="Legacy comparison path",
        risk_plan={"rr": 2.0},
    )


def test_legacy_signal_uses_operational_outbox_when_worker_has_no_discord_client() -> None:
    repo = _OutboxRepository()
    service = AutonomousTradingLoopService(
        settings=Settings(
            _env_file=None,
            autonomy_alert_channel_id="alerts",
            autonomy_signals_run_with_engine_enabled=True,
        ),
        repository=repo,  # type: ignore[arg-type]
        hyperliquid=None,
        news=None,
        alert_sink=None,
    )

    async def run():
        return await service._deliver_signal_alert(_signal(), category="legacy_trade_signal")

    delivery_ref, transport = anyio.run(run)

    assert delivery_ref == "opn_legacy"
    assert transport == "operational_outbox"
    assert len(repo.notifications) == 1
    notification = repo.notifications[0]
    assert notification["category"] == "legacy_trade_signal"
    assert notification["payload"]["metadata"]["paper_only"] is True
    assert "Legacy AI Trading Signal" in notification["payload"]["content"]
    assert notification["payload"]["metadata"]["live_execution_allowed"] is False
