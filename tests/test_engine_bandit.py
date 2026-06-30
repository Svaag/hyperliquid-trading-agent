from __future__ import annotations

import anyio

from hyperliquid_trading_agent.app.engine.bandit import OfflineContextualBanditReporter


class FakeBanditRepo:
    def __init__(self):
        self.policy = None
        self.recommendations: list[dict] = []

    async def list_strategy_regime_performance(self, **kwargs):
        return [
            {"strategy_id": "microstructure_ofi_v2", "strategy_family": "microstructure_orderflow", "asset": "BTC", "regime_label": "orderflow=buy_pressure", "score": 76, "candidate_count": 12, "window_end_ms": 1_000},
            {"strategy_id": "funding_carry_v1", "strategy_family": "funding_basis", "asset": "ETH", "regime_label": "funding=positive_extreme", "score": 30, "candidate_count": 8, "window_end_ms": 1_000},
        ]

    async def list_strategy_specs(self, **kwargs):
        return [
            {"strategy_id": "microstructure_ofi_v2", "family": "microstructure_orderflow", "enabled": True, "counts_for_breadth": True},
            {"strategy_id": "funding_carry_v1", "family": "funding_basis", "enabled": True, "counts_for_breadth": True},
            {"strategy_id": "legacy_signal_adapter_v1", "family": "legacy_bridge", "enabled": True, "counts_for_breadth": False},
        ]

    async def upsert_bandit_policy_snapshot(self, snapshot: dict):
        self.policy = snapshot
        return snapshot["policy_id"]

    async def record_bandit_recommendation(self, recommendation: dict):
        self.recommendations.append(recommendation)
        return recommendation["recommendation_id"]


def test_offline_bandit_reporter_writes_report_only_recommendations():
    repo = FakeBanditRepo()

    async def run():
        return await OfflineContextualBanditReporter(repo).run(window_start_ms=0, window_end_ms=2_000)

    result = anyio.run(run)

    assert result["report_only"] is True
    assert result["auto_apply_allowed"] is False
    assert repo.policy["status"] == "report_only"
    assert repo.policy["metadata"]["auto_apply_allowed"] is False
    assert {item["strategy_id"] for item in repo.recommendations} == {"microstructure_ofi_v2", "funding_carry_v1"}
    assert all(item["auto_apply_allowed"] is False for item in repo.recommendations)
    assert any(item["expected_score_delta"] < 0 for item in repo.recommendations)
