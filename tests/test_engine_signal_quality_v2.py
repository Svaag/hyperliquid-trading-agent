from __future__ import annotations

import anyio

from hyperliquid_trading_agent.app.engine.news_risk_counterfactual import run_news_risk_counterfactual
from hyperliquid_trading_agent.app.engine.signal_quality import build_signal_quality_report


class _QualityRepository:
    enabled = True

    def __init__(self) -> None:
        self.as_of = 10_000_000
        self.persisted: list[dict] = []
        self.outcomes = [
            self._outcome("cand_a", "5m", "alloc_a", "reg_a5", 10.0, 18.0, self.as_of - 300_000),
            self._outcome("cand_a", "15m", "alloc_a", "reg_a15", -2.0, 6.0, self.as_of - 200_000),
            self._outcome("cand_b", "5m", "alloc_b", "reg_b", -20.0, -12.0, self.as_of - 100_000),
            {
                **self._outcome("cand_missing", "5m", "alloc_m", "reg_m", 0.0, 0.0, self.as_of - 50_000),
                "terminal_state": "missing_mark",
                "mark_px": None,
                "metadata": {},
            },
            {
                **self._outcome("cand_late", "5m", "alloc_l", "reg_l", 5.0, 13.0, self.as_of - 40_000),
                "quality_flags": ["late_mark"],
            },
        ]

    def _outcome(self, candidate_id, window, allocation_id, regime_id, net, gross, window_end):
        start = window_end - {"5m": 300_000, "15m": 900_000}[window]
        return {
            "attribution_id": f"{candidate_id}_{window}",
            "candidate_id": candidate_id,
            "strategy_id": "microstructure_ofi_v2",
            "strategy_family": "microstructure_orderflow",
            "asset": "BTC",
            "side": "long",
            "candidate_horizon": "5m",
            "regime_snapshot_id": regime_id,
            "allocation_id": allocation_id,
            "outcome_window": window,
            "window_start_ms": start,
            "window_end_ms": window_end,
            "gross_return_bps": gross,
            "fees_bps": 4.5,
            "slippage_bps": 4.0,
            "funding_bps": 0.0,
            "net_return_bps": net,
            "realized_r": net / 20.0,
            "mfe_bps": max(gross, 0.0),
            "mae_bps": min(gross, 0.0),
            "terminal_state": "matured",
            "quality_flags": [],
            "updated_at_ms": self.as_of,
            "metadata": {
                "mark_source": "feature_store_mid",
                "expected_spread_cost_bps": 1.5,
                "expected_slippage_bps": 2.0,
                "expected_market_impact_bps": 0.5,
            },
        }

    async def list_candidate_outcome_attributions(self, **kwargs):
        offset = int(kwargs.get("offset") or 0)
        limit = int(kwargs.get("limit") or len(self.outcomes))
        return self.outcomes[offset : offset + limit]

    async def list_regime_snapshots_by_ids(self, ids):
        starts = {row["regime_snapshot_id"]: row["window_start_ms"] for row in self.outcomes}
        return [
            {
                "regime_snapshot_id": regime_id,
                "as_of_ms": starts[regime_id],
                "vector": {
                    "regime_label": "range",
                    "as_of_ms": starts[regime_id],
                    "news_risk_mode": "risk_off" if regime_id.startswith("reg_a") else "shock",
                    "metadata": {
                        "observed_news_risk_mode": "risk_off" if regime_id.startswith("reg_a") else "shock"
                    },
                },
            }
            for regime_id in ids
            if regime_id in starts
        ]

    async def list_allocation_decisions_by_ids(self, ids):
        rows = {
            "alloc_a": {
                "allocation_id": "alloc_a",
                "metadata": {
                    "news_risk_overlay": {
                        "would_block": False,
                        "would_size_multiplier": 0.5,
                    }
                },
            },
            "alloc_b": {
                "allocation_id": "alloc_b",
                "metadata": {
                    "news_risk_overlay": {
                        "would_block": True,
                        "would_size_multiplier": 1.0,
                    }
                },
            },
        }
        return [rows[item] for item in ids if item in rows]

    async def record_replay_result(self, artifact):
        self.persisted.append(artifact)
        return artifact["replay_id"]


def test_signal_quality_never_pools_horizons_and_excludes_non_strict_marks() -> None:
    repo = _QualityRepository()

    report = anyio.run(
        lambda: build_signal_quality_report(repo, window_hours=1, as_of_ms=repo.as_of)
    )

    assert report["grain"] == "candidate_id_x_outcome_window"
    assert report["data_quality"]["usable_rows"] == 3
    assert report["data_quality"]["missing_mark"] == 1
    assert report["data_quality"]["fallback_or_late_marks"] == 1
    assert {item["outcome_window"] for item in report["overall_by_outcome_window"]} == {"5m", "15m"}
    five_minute = next(item for item in report["overall_by_outcome_window"] if item["outcome_window"] == "5m")
    fifteen_minute = next(item for item in report["overall_by_outcome_window"] if item["outcome_window"] == "15m")
    assert five_minute["n"] == 2
    assert fifteen_minute["n"] == 1
    assert report["legacy_mixed_latest_endpoint"]["readiness_eligible"] is False


def test_newswire_counterfactual_uses_persisted_shadow_overlay_and_is_research_only() -> None:
    repo = _QualityRepository()

    artifact = anyio.run(
        lambda: run_news_risk_counterfactual(repo, window_hours=1, as_of_ms=repo.as_of)
    )

    assert artifact["metadata"]["artifact_type"] == "engine_news_risk_counterfactual"
    assert artifact["metadata"]["readiness_eligible"] is False
    assert artifact["metadata"]["side_effects"]["order_intents"] == 0
    assert artifact["candidate_metrics"]["blocked_count"] == 1
    assert artifact["candidate_metrics"]["halved_count"] == 2
    assert artifact["metadata"]["safety_decision"]["recommendation"] == "keep_overlay_report_only"
    assert repo.persisted == [artifact]
