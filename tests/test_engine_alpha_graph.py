from __future__ import annotations

import anyio

from hyperliquid_trading_agent.app.engine.alpha_graph import build_strategy_regime_alpha_graph


class FakeAlphaGraphRepo:
    async def list_strategy_specs(self, **kwargs):
        return [
            {"strategy_id": "microstructure_ofi_v2", "version": "2.0.0", "family": "microstructure_orderflow", "enabled": True, "counts_for_breadth": True},
            {"strategy_id": "regime_defensive_flat_v1", "version": "1.0.0", "family": "defensive_control", "enabled": True, "counts_for_breadth": False},
        ]

    async def list_alpha_candidates(self, **kwargs):
        return [
            {
                "candidate_id": "cand_1",
                "strategy_id": "microstructure_ofi_v2",
                "strategy_family": "microstructure_orderflow",
                "asset": "BTC",
                "venue": "hyperliquid",
                "horizon": "5m",
                "side": "long",
                "regime_snapshot_id": "reg_1",
                "feature_snapshot_id": "fs_1",
                "metadata": {"regime_label": "orderflow=buy_pressure", "feature_coverage_pct": 100.0},
            }
        ]

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
                "regime_snapshot_id": "reg_1",
                "outcome_window": "5m",
                "terminal_state": "matured",
                "net_return_bps": 15.0,
                "realized_r": 0.5,
                "risk_decision": "allow",
                "council_decision": "allow_shadow",
                "allocation_status": "allocate",
                "metadata": {"regime_label": "orderflow=buy_pressure"},
            },
            {
                "attribution_id": "coa_2",
                "candidate_id": "cand_2",
                "strategy_id": "funding_carry_v1",
                "strategy_version": "1.0.0",
                "strategy_family": "funding_basis",
                "asset": "ETH",
                "venue": "hyperliquid",
                "regime_snapshot_id": "reg_2",
                "outcome_window": "1h",
                "terminal_state": "matured",
                "net_return_bps": -12.0,
                "realized_r": -0.4,
                "risk_decision": "reject",
                "council_decision": "reject",
                "allocation_status": "risk_rejected",
                "metadata": {"regime_label": "funding=positive_extreme"},
            },
        ]

    async def list_strategy_regime_performance(self, **kwargs):
        return [
            {
                "performance_id": "perf_1",
                "strategy_id": "microstructure_ofi_v2",
                "strategy_version": "2.0.0",
                "strategy_family": "microstructure_orderflow",
                "regime_label": "orderflow=buy_pressure",
                "asset": "BTC",
                "venue": "hyperliquid",
                "outcome_window": "5m",
                "candidate_count": 8,
                "allocation_count": 3,
                "score": 70,
                "avg_net_return_bps": 15,
                "avg_realized_r": 0.5,
            },
            {
                "performance_id": "perf_2",
                "strategy_id": "funding_carry_v1",
                "strategy_version": "1.0.0",
                "strategy_family": "funding_basis",
                "regime_label": "funding=positive_extreme",
                "asset": "ETH",
                "venue": "hyperliquid",
                "outcome_window": "1h",
                "candidate_count": 2,
                "allocation_count": 0,
                "score": 90,
                "avg_net_return_bps": 20,
                "avg_realized_r": 0.2,
            },
        ]

    async def list_council_reviews(self, **kwargs):
        return [{"review_id": "council_1", "candidate_id": "cand_2", "strategy_id": "funding_carry_v1", "decision": "reject"}]

    async def list_risk_gateway_decisions(self, **kwargs):
        return [{"decision_id": "risk_1", "intent_id": "intent_1", "decision": "reject", "metadata": {"strategy_id": "funding_carry_v1"}}]

    async def list_replay_result_links(self, **kwargs):
        return [{"link_id": "rrl_1", "replay_id": "replay_1", "strategy_id": "funding_carry_v1", "regime_snapshot_id": "reg_2", "metadata": {"replay_status": "failed"}}]

    async def list_regime_snapshots(self, **kwargs):
        return [{"regime_snapshot_id": "reg_1", "primary_asset": "BTC", "vector_json": {"regime_label": "orderflow=buy_pressure"}}]


def test_strategy_regime_alpha_graph_projects_evidence_edges():
    async def run():
        return await build_strategy_regime_alpha_graph(FakeAlphaGraphRepo())

    graph = anyio.run(run)

    edge_types = {edge["type"] for edge in graph["edges"]}
    node_types = {node["type"] for node in graph["nodes"]}

    assert graph["read_only"] is True
    assert "strategy" in node_types
    assert "market_regime" in node_types
    assert "worked_in" in edge_types
    assert "needs_more_evidence_in" in edge_types
    assert "risk_rejected_in" in edge_types
    assert "council_vetoed_in" in edge_types
    assert "replay_failed_in" in edge_types
    assert "overfit_warning_in" in edge_types
    assert graph["safety"] == {"config_mutation": False, "order_mutation": False, "risk_bypass": False, "council_bypass": False}
