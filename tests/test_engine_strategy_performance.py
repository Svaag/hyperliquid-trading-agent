from __future__ import annotations

import anyio

from hyperliquid_trading_agent.app.engine.strategy_performance import refresh_strategy_regime_performance


class FakePerformanceRepo:
    def __init__(self):
        self.upserted: list[dict] = []

    async def list_alpha_candidates(self, **kwargs):
        return [
            {
                "candidate_id": "cand_1",
                "strategy_id": "microstructure_ofi_v2",
                "asset": "BTC",
                "created_at_ms": 1_000,
                "regime_snapshot_id": "reg_1",
                "metadata": {"strategy_version": "2.0.0", "strategy_family": "microstructure_orderflow", "regime_label": "orderflow=buy_pressure"},
            },
            {
                "candidate_id": "cand_2",
                "strategy_id": "legacy_signal_adapter_v1",
                "asset": "BTC",
                "created_at_ms": 1_100,
                "regime_snapshot_id": "reg_1",
                "metadata": {"strategy_version": "1.0.0", "strategy_family": "legacy_bridge", "regime_label": "orderflow=buy_pressure"},
            },
        ]

    async def list_allocation_decisions(self, **kwargs):
        return [
            {"allocation_id": "alloc_1", "candidate_id": "cand_1", "status": "allocate", "allocated_notional_usd": 100, "created_at_ms": 1_200},
            {"allocation_id": "alloc_2", "candidate_id": "cand_2", "status": "skip", "allocated_notional_usd": 0, "created_at_ms": 1_200},
        ]

    async def list_ev_estimates(self, **kwargs):
        return [
            {"estimate_id": "ev_1", "candidate_id": "cand_1", "net_ev_bps": 18.0, "created_at_ms": 1_200},
            {"estimate_id": "ev_2", "candidate_id": "cand_2", "net_ev_bps": 4.0, "created_at_ms": 1_200},
        ]

    async def list_pnl_attribution(self, **kwargs):
        return [{"strategy_id": "microstructure_ofi_v2", "asset": "BTC", "total_pnl_usd": 25.0}]

    async def upsert_strategy_regime_performance(self, row: dict):
        self.upserted.append(row)


def test_refresh_strategy_regime_performance_builds_scorecards():
    repo = FakePerformanceRepo()

    async def run():
        return await refresh_strategy_regime_performance(repo, window_start_ms=0, window_end_ms=2_000)

    rows = anyio.run(run)

    assert len(rows) == 2
    row = next(item for item in rows if item["strategy_id"] == "microstructure_ofi_v2")
    assert row["strategy_family"] == "microstructure_orderflow"
    assert row["candidate_count"] == 1
    assert row["allocation_count"] == 1
    assert row["avg_net_ev_bps"] == 18.0
    assert row["realized_pnl_usd"] == 25.0
    assert repo.upserted
