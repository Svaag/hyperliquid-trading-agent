from __future__ import annotations

import pytest

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.schemas import AlphaCandidate
from hyperliquid_trading_agent.app.engine.throttles import StrategyThrottleController


class FakeAllocationRepo:
    enabled = True

    def __init__(self, allocations: list[dict]):
        self.allocations = allocations

    async def list_allocation_decisions(self, limit: int = 1000):
        return self.allocations[:limit]


def _candidate(strategy_id: str = "support_resistance_reversion_v2") -> AlphaCandidate:
    return AlphaCandidate(
        candidate_id=f"cand_{strategy_id}",
        strategy_id=strategy_id,
        asset="BTC",
        venue="hyperliquid",
        side="long",
        horizon="15m",
        proposed_entry=100.0,
        stop=99.0,
        targets=[102.0],
        thesis="test",
        invalidation_conditions=["invalid below stop"],
        feature_snapshot_id="fs_test",
        regime_snapshot_id="reg_test",
        raw_alpha_score=80,
        confidence=0.8,
        created_at_ms=1_000,
        expires_at_ms=61_000,
    )


def _allocation(strategy_id: str, created_at_ms: int, status: str = "allocate") -> dict:
    return {
        "allocation_id": f"alloc_{strategy_id}_{created_at_ms}",
        "candidate_id": f"cand_{strategy_id}_{created_at_ms}",
        "status": status,
        "created_at_ms": created_at_ms,
        "metadata": {"strategy_id": strategy_id},
    }


@pytest.mark.asyncio
async def test_strategy_throttle_ignores_stale_historical_dominance() -> None:
    now_ms = 10 * 60 * 60 * 1000
    settings = Settings(
        environment="test",
        engine_strategy_throttle_lookback_hours=1,
        engine_strategy_max_allocation_share_pct=55,
        engine_strategy_max_allocations_per_loop=3,
    )
    stale_allocations = [_allocation("support_resistance_reversion_v2", 1_000 + idx) for idx in range(50)]
    throttles = StrategyThrottleController(settings)

    allowed, reasons, metadata = await throttles.allow_allocation(
        _candidate("support_resistance_reversion_v2"),
        current_loop_allocations=[],
        repository=FakeAllocationRepo(stale_allocations),
        timestamp_ms=now_ms,
    )

    assert allowed is True
    assert reasons == []
    assert metadata["recent_total_allocations"] == 0
    assert throttles.status()["reason_counts"] == {}


@pytest.mark.asyncio
async def test_strategy_throttle_blocks_recent_dominance_with_minimum_samples() -> None:
    now_ms = 10 * 60 * 60 * 1000
    settings = Settings(
        environment="test",
        engine_strategy_throttle_lookback_hours=24,
        engine_strategy_max_allocation_share_pct=55,
        engine_strategy_max_allocations_per_loop=3,
    )
    recent_allocations = [_allocation("support_resistance_reversion_v2", now_ms - 1_000 - idx) for idx in range(8)]
    throttles = StrategyThrottleController(settings)

    allowed, reasons, metadata = await throttles.allow_allocation(
        _candidate("support_resistance_reversion_v2"),
        current_loop_allocations=[],
        repository=FakeAllocationRepo(recent_allocations),
        timestamp_ms=now_ms,
    )

    assert allowed is False
    assert reasons == ["strategy_throttle"]
    assert metadata["throttle_reason"] == "recent_allocation_share"
    assert metadata["recent_allocation_share_pct"] == 100.0
    status = throttles.status()
    assert status["reason_counts"]["recent_allocation_share"] == 1
    assert status["last_recent_share_pct"]["support_resistance_reversion_v2"] == 100.0
