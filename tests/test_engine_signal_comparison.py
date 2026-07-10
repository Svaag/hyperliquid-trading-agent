from __future__ import annotations

import time

import anyio

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.signal_comparison import build_signal_path_comparison


class _ComparisonRepository:
    def __init__(self) -> None:
        now = int(time.time() * 1000)
        self.evaluations = [
            {
                "signal_id": "legacy_long",
                "symbol": "BTC",
                "side": "long",
                "signal_type": "momentum",
                "created_at_ms": now - 60_000,
                "terminal_outcome": "take_profit",
                "realized_or_marked_r": 1.5,
                "marks": [
                    {
                        "status": "completed",
                        "marked_at_ms": now - 30_000,
                        "direction_adjusted_return_bps": 25.0,
                    }
                ],
            },
            {
                "signal_id": "legacy_short",
                "symbol": "ETH",
                "side": "short",
                "signal_type": "reversion",
                "created_at_ms": now - 120_000,
                "terminal_outcome": "stop_hit",
                "realized_or_marked_r": -1.0,
                "marks": [],
            },
        ]
        self.candidates = [
            {
                "candidate_id": "candidate_long",
                "asset": "BTC",
                "side": "long",
                "strategy_id": "directional_momentum_v2",
                "created_at_ms": now - 50_000,
            },
            {
                "candidate_id": "candidate_other",
                "asset": "SOL",
                "side": "short",
                "strategy_id": "support_resistance_reversion_v2",
                "created_at_ms": now - 80_000,
            },
        ]
        self.outcomes = [
            {
                "candidate_id": "candidate_long",
                "terminal_state": "marked",
                "window_end_ms": now - 20_000,
                "net_return_bps": 18.0,
                "realized_r": 0.8,
            },
            {
                "candidate_id": "candidate_other",
                "terminal_state": "marked",
                "window_end_ms": now - 10_000,
                "net_return_bps": -4.0,
                "realized_r": -0.2,
            },
        ]
        self.pnl = [{"window_end_ms": now - 5_000, "total_pnl_usd": 12.5}]
        self.proposals = [{"created_at_ms": now - 40_000, "status": "proposed"}]
        self.replays = [
            {
                "replay_id": "ereplay_test",
                "proposal_id": "engine:test",
                "status": "advisory_pass",
                "created_at_ms": now,
                "metadata": {
                    "artifact_type": "engine_shadow_comparison",
                    "verdict": "baseline_equivalence",
                    "promotion_decision": "eligible_for_review",
                },
            }
        ]

    async def list_signal_evaluations(self, **kwargs):
        return self.evaluations

    async def list_alpha_candidates(self, **kwargs):
        return self.candidates

    async def list_candidate_outcome_attributions(self, **kwargs):
        return self.outcomes

    async def list_pnl_attribution(self, **kwargs):
        return self.pnl

    async def list_engine_operator_proposals(self, **kwargs):
        return self.proposals

    async def list_replay_results(self, **kwargs):
        return self.replays


def test_signal_path_comparison_maps_performance_overlap_and_safety() -> None:
    async def run():
        return await build_signal_path_comparison(
            _ComparisonRepository(),
            settings=Settings(
                _env_file=None,
                autonomy_signals_run_with_engine_enabled=True,
                engine_operator_proposals_enabled=True,
            ),
        )

    report = anyio.run(run)

    assert report["legacy"]["evaluation_count"] == 2
    assert report["legacy"]["hit_rate_pct"] == 50.0
    assert report["engine"]["matured_candidate_outcome_count"] == 2
    assert report["engine"]["hit_rate_pct"] == 50.0
    assert report["engine"]["shadow_total_pnl_usd"] == 12.5
    assert report["engine"]["latest_replay"]["status"] == "advisory_pass"
    assert report["overlap"]["matched_legacy_signal_count"] == 1
    assert report["overlap"]["matches"][0]["candidate_id"] == "candidate_long"
    assert report["path_mapping"]["legacy"]["configured_with_engine"] is True
    assert report["safety"] == {
        "execution_authority": "none",
        "engine_operator_proposals_acknowledgment_only": True,
        "paper_order_created_by_report": False,
        "live_order_created_by_report": False,
    }
