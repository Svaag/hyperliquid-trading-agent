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
    assert row["venue"] == "unknown"
    assert row["outcome_window"] == "unknown"
    assert row["realized_pnl_usd"] == 25.0
    assert repo.upserted


class FakeOutcomePerformanceRepo(FakePerformanceRepo):
    async def list_candidate_outcome_attributions(self, **kwargs):
        return [
            {
                "attribution_id": "coa_1",
                "candidate_id": "cand_1",
                "strategy_id": "microstructure_ofi_v2",
                "strategy_version": "2.0.0",
                "strategy_family": "microstructure_orderflow",
                "asset": "BTC",
                "venue": "hyperliquid",
                "side": "long",
                "candidate_horizon": "5m",
                "regime_snapshot_id": "reg_1",
                "feature_snapshot_id": "fs_1",
                "allocation_id": "alloc_1",
                "outcome_window": "5m",
                "window_start_ms": 1_000,
                "window_end_ms": 1_500,
                "entry_px": 100,
                "mark_px": 101,
                "net_return_bps": 95.0,
                "realized_r": 0.95,
                "mae_bps": -5.0,
                "fees_bps": 1.0,
                "slippage_bps": 4.0,
                "risk_decision": "allow",
                "council_decision": "allow_shadow",
                "allocation_status": "allocate",
                "terminal_state": "matured",
                "created_at_ms": 1_000,
                "updated_at_ms": 1_500,
                "metadata": {"regime_label": "orderflow=buy_pressure", "allocated_notional_usd": 1000},
            },
            {
                "attribution_id": "coa_2",
                "candidate_id": "cand_2",
                "strategy_id": "microstructure_ofi_v2",
                "strategy_version": "2.0.0",
                "strategy_family": "microstructure_orderflow",
                "asset": "BTC",
                "venue": "hyperliquid",
                "side": "long",
                "candidate_horizon": "5m",
                "regime_snapshot_id": "reg_1",
                "feature_snapshot_id": "fs_2",
                "allocation_id": "alloc_2",
                "outcome_window": "5m",
                "window_start_ms": 1_000,
                "window_end_ms": 1_600,
                "entry_px": 100,
                "mark_px": 99,
                "net_return_bps": -105.0,
                "realized_r": -1.05,
                "mae_bps": -110.0,
                "fees_bps": 1.0,
                "slippage_bps": 4.0,
                "risk_decision": "reject",
                "council_decision": "reject",
                "allocation_status": "risk_rejected",
                "terminal_state": "matured",
                "created_at_ms": 1_000,
                "updated_at_ms": 1_600,
                "metadata": {"regime_label": "orderflow=buy_pressure", "allocated_notional_usd": 0},
            },
        ]

    async def list_portfolio_concentration_events(self, **kwargs):
        return [{"event_id": "pce_1", "strategy_id": "microstructure_ofi_v2", "asset": "BTC", "venue": "hyperliquid", "created_at_ms": 1_200}]


def test_refresh_strategy_regime_performance_uses_candidate_outcome_attributions():
    repo = FakeOutcomePerformanceRepo()

    async def run():
        return await refresh_strategy_regime_performance(repo, window_start_ms=0, window_end_ms=2_000)

    rows = anyio.run(run)

    assert len(rows) == 1
    row = rows[0]
    assert row["strategy_id"] == "microstructure_ofi_v2"
    assert row["venue"] == "hyperliquid"
    assert row["outcome_window"] == "5m"
    assert row["candidate_count"] == 2
    assert row["allocation_count"] == 1
    assert row["risk_reject_count"] == 1
    assert row["council_veto_count"] == 1
    assert row["concentration_event_count"] == 1
    assert row["avg_net_return_bps"] == -5.0
    assert row["realized_pnl_usd"] == 9.5
