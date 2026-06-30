from __future__ import annotations

import anyio

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.diversity import PortfolioDiversityController
from hyperliquid_trading_agent.app.engine.schemas import AllocationDecision, AlphaCandidate


class FakeDiversityRepo:
    enabled = True

    def __init__(self, allocations: list[dict]):
        self.allocations = allocations
        self.events: list[dict] = []

    async def list_allocation_decisions(self, limit: int = 5000):
        return self.allocations[:limit]

    async def record_allocation_diversity_event(self, event: dict):
        self.events.append(event)


def _candidate(strategy_id: str = "s1", family: str = "f1", asset: str = "BTC") -> AlphaCandidate:
    return AlphaCandidate(
        candidate_id=f"cand_{asset}_{strategy_id}",
        strategy_id=strategy_id,
        strategy_version="1.0.0",
        strategy_family=family,
        asset=asset,
        asset_class="crypto",
        venue="hyperliquid",
        side="long",
        horizon="15m",
        proposed_entry=100,
        stop=97,
        targets=[106],
        thesis="test",
        invalidation_conditions=["stop"],
        feature_snapshot_id="fs_1",
        regime_snapshot_id="reg_1",
        raw_alpha_score=80,
        confidence=0.6,
        created_at_ms=1_000,
        expires_at_ms=2_000,
    )


def _allocation(candidate: AlphaCandidate, notional: float = 100.0) -> AllocationDecision:
    return AllocationDecision(
        allocation_id=f"alloc_{candidate.candidate_id}",
        candidate_id=candidate.candidate_id,
        status="allocate",
        allocated_size=notional / candidate.proposed_entry,
        allocated_notional_usd=notional,
        risk_usd=1,
        created_at_ms=1_000,
        metadata={
            "strategy_id": candidate.strategy_id,
            "strategy_version": candidate.strategy_version,
            "strategy_family": candidate.strategy_family,
            "asset": candidate.asset,
            "venue": candidate.venue,
        },
    )


def _row(strategy: str, family: str, asset: str, notional: float, ts: int) -> dict:
    return {
        "allocation_id": f"alloc_{strategy}_{asset}_{ts}",
        "candidate_id": f"cand_{strategy}_{asset}_{ts}",
        "status": "allocate",
        "allocated_notional_usd": notional,
        "created_at_ms": ts,
        "metadata": {"strategy_id": strategy, "strategy_family": family, "asset": asset, "venue": "hyperliquid"},
    }


def test_diversity_controller_allows_until_window_has_min_samples():
    candidate = _candidate("new_strategy", "new_family")
    allocation = _allocation(candidate, notional=100)
    controller = PortfolioDiversityController(Settings(environment="test", engine_diversity_min_window_samples=10))
    repo = FakeDiversityRepo([])

    async def run():
        result = await controller.apply(candidate, allocation, current_loop_allocations=[], repository=repo, timestamp_ms=1_000)
        assert result.status == "allocate"
        assert result.metadata["diversity"]["decision"] == "allow"
        assert repo.events[0]["decision"] == "allow"

    anyio.run(run)


def test_diversity_controller_hard_caps_strategy_family_and_symbol_strategy():
    now = 10_000_000
    rows = [_row("s1", "f1", "BTC", 100, now - idx) for idx in range(7)]
    rows += [_row("s2", "f1", "ETH", 100, now - 100 - idx) for idx in range(3)]
    candidate = _candidate("s1", "f1", "BTC")
    allocation = _allocation(candidate, notional=100)
    controller = PortfolioDiversityController(Settings(environment="test", engine_diversity_min_window_samples=10))
    repo = FakeDiversityRepo(rows)

    async def run():
        result = await controller.apply(candidate, allocation, current_loop_allocations=[], repository=repo, timestamp_ms=now)
        assert result.status == "skip"
        assert "strategy_hard_share_exceeded" in result.reason_codes
        assert "family_hard_share_exceeded" in result.reason_codes
        assert "symbol_strategy_hard_share_exceeded" in result.reason_codes
        assert repo.events[0]["decision"] == "throttle"

    anyio.run(run)


def test_diversity_controller_target_throttles_at_45_pct():
    now = 10_000_000
    rows = [_row("s1", "f1", "BTC", 100, now - idx) for idx in range(5)]
    rows += [_row("s2", "f2", "ETH", 100, now - 100 - idx) for idx in range(6)]
    candidate = _candidate("s1", "f1", "ETH")
    allocation = _allocation(candidate, notional=10)
    controller = PortfolioDiversityController(Settings(environment="test", engine_diversity_min_window_samples=10))
    repo = FakeDiversityRepo(rows)

    async def run():
        result = await controller.apply(candidate, allocation, current_loop_allocations=[], repository=repo, timestamp_ms=now)
        assert result.status == "skip"
        assert "strategy_target_share_throttle" in result.reason_codes

    anyio.run(run)
