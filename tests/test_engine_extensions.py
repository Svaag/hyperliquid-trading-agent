from __future__ import annotations

import time
from typing import Any

import anyio
from fastapi.testclient import TestClient

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.pnl_loop import EnginePnLAttributionLoopService
from hyperliquid_trading_agent.app.engine.replay_compare import EngineReplayComparisonService, stable_hash
from hyperliquid_trading_agent.app.engine.schemas import AllocationDecision, AlphaCandidate
from hyperliquid_trading_agent.app.engine.throttles import StrategyThrottleController
from hyperliquid_trading_agent.app.main import create_app


class ReplayRepo:
    enabled = True

    def __init__(self, now_ms: int):
        self.now_ms = now_ms
        self.recorded: dict[str, Any] | None = None
        self.outcomes: list[dict[str, Any]] = []
        self.replay_links: list[dict[str, Any]] = []

    async def list_alpha_candidates(self, **kwargs):
        return [
            {"candidate_id": "cand_1", "strategy_id": "directional_momentum", "asset": "BTC", "created_at_ms": self.now_ms - 1000},
            {"candidate_id": "cand_2", "strategy_id": "microstructure_ofi", "asset": "BTC", "created_at_ms": self.now_ms - 1000},
        ]

    async def list_ev_estimates(self, **kwargs):
        return [
            {"candidate_id": "cand_1", "net_ev_bps": 8, "risk_adjusted_utility": 0.25, "created_at_ms": self.now_ms - 1000},
            {"candidate_id": "cand_2", "net_ev_bps": 15, "risk_adjusted_utility": 0.5, "created_at_ms": self.now_ms - 1000},
        ]

    async def list_allocation_decisions(self, **kwargs):
        return [
            {
                "allocation_id": "alloc_1",
                "candidate_id": "cand_1",
                "status": "allocate",
                "allocated_notional_usd": 1000,
                "created_at_ms": self.now_ms - 1000,
            },
            {
                "allocation_id": "alloc_2",
                "candidate_id": "cand_2",
                "status": "allocate",
                "allocated_notional_usd": 1000,
                "created_at_ms": self.now_ms - 1000,
            },
        ]

    async def list_execution_reports(self, **kwargs):
        return [{"execution_mode": "shadow", "slippage_bps": 0, "fees_usd": 0, "created_at_ms": self.now_ms - 1000}]

    async def list_risk_gateway_decisions(self, **kwargs):
        return []

    async def list_pnl_attribution(self, **kwargs):
        return []

    async def list_candidate_outcome_attributions(self, **kwargs):
        return self.outcomes[: kwargs.get("limit", 100)]

    async def record_replay_result_link(self, link):
        self.replay_links.append(link)
        return link["link_id"]

    async def record_replay_result(self, item):
        self.recorded = item
        return item["replay_id"]


def test_engine_replay_compare_persists_immutable_artifact():
    now_ms = int(time.time() * 1000)
    repo = ReplayRepo(now_ms)
    service = EngineReplayComparisonService(repository=repo, settings=Settings(environment="test", engine_replay_min_sample_candidates=1, engine_replay_min_shadow_intents=1))

    async def run():
        return await service.compare_variant(
            baseline_config={"engine_min_net_ev_bps": 8, "engine_min_risk_adjusted_utility": 0.25},
            candidate_config={"engine_min_net_ev_bps": 12, "engine_min_risk_adjusted_utility": 0.25},
            window_start_ms=now_ms - 60_000,
            window_end_ms=now_ms,
            universe=["BTC"],
            variant_id="tighten_ev_thresholds_v1",
        )

    artifact = anyio.run(run)

    assert repo.recorded == artifact
    assert artifact["proposal_id"] == "engine:tighten_ev_thresholds_v1"
    assert artifact["metadata"]["artifact_type"] == "engine_shadow_comparison"
    assert artifact["metadata"]["exchange_actions"] == []
    assert artifact["status"] in {"passed", "advisory_pass", "failed"}
    assert artifact["candidate_metrics"]["outcome_attribution_count"] == 0
    assert stable_hash({"a": 1}) == stable_hash({"a": 1})


def test_engine_replay_groups_candidate_outcomes_by_strategy_regime_asset_venue_window():
    now_ms = int(time.time() * 1000)
    repo = ReplayRepo(now_ms)
    repo.outcomes = [
        {
            "attribution_id": "coa_1",
            "candidate_id": "cand_2",
            "strategy_id": "microstructure_ofi",
            "strategy_version": "1.0.0",
            "strategy_family": "microstructure_orderflow",
            "asset": "BTC",
            "venue": "hyperliquid",
            "candidate_horizon": "5m",
            "regime_snapshot_id": "reg_1",
            "outcome_window": "5m",
            "window_end_ms": now_ms - 500,
            "net_return_bps": 25,
            "realized_r": 0.5,
            "risk_decision": "allow",
            "council_decision": "allow_shadow",
            "metadata": {"regime_label": "orderflow=buy_pressure"},
        }
    ]
    service = EngineReplayComparisonService(repository=repo, settings=Settings(environment="test", engine_readiness_max_strategy_allocation_share_pct=100, engine_replay_min_sample_candidates=1, engine_replay_min_shadow_intents=1))

    async def run():
        return await service.compare_variant(
            baseline_config={"current": True},
            candidate_config={"current": True},
            window_start_ms=now_ms - 60_000,
            window_end_ms=now_ms,
            universe=["BTC"],
            variant_id="outcome_groups_v1",
        )

    artifact = anyio.run(run)

    groups = artifact["candidate_metrics"]["strategy_regime_outcome_groups"]
    assert "microstructure_ofi|orderflow=buy_pressure|BTC|hyperliquid|5m" in groups
    assert groups["microstructure_ofi|orderflow=buy_pressure|BTC|hyperliquid|5m"]["avg_net_return_bps"] == 25
    assert repo.replay_links


def _candidate(cid: str, strategy: str, score: float) -> AlphaCandidate:
    return AlphaCandidate(
        candidate_id=cid,
        strategy_id=strategy,
        asset="BTC",
        venue="hyperliquid",
        side="long",
        horizon="1h",
        proposed_entry=100,
        stop=95,
        targets=[110],
        thesis="test",
        invalidation_conditions=["test invalidation"],
        feature_snapshot_id="fs_1",
        regime_snapshot_id="reg_1",
        raw_alpha_score=score,
        confidence=0.5,
        created_at_ms=1,
        expires_at_ms=999,
    )


def test_engine_replay_compare_marks_safe_inconclusive_as_advisory_pass():
    now_ms = int(time.time() * 1000)
    repo = ReplayRepo(now_ms)
    service = EngineReplayComparisonService(repository=repo, settings=Settings(environment="test", engine_readiness_max_strategy_allocation_share_pct=100, engine_replay_min_sample_candidates=1, engine_replay_min_shadow_intents=1))

    async def run():
        return await service.compare_variant(
            baseline_config={"current": True},
            candidate_config={"current": True},
            window_start_ms=now_ms - 60_000,
            window_end_ms=now_ms,
            universe=["BTC"],
            variant_id="same_config_v1",
        )

    artifact = anyio.run(run)

    assert artifact["status"] == "advisory_pass"
    assert artifact["metadata"]["verdict"] == "baseline_equivalence"
    assert artifact["metadata"]["promotion_decision"] == "eligible_for_review"


def test_engine_replay_compare_reports_insufficient_data_for_empty_window():
    now_ms = int(time.time() * 1000)
    repo = ReplayRepo(now_ms)
    service = EngineReplayComparisonService(repository=repo, settings=Settings(environment="test", engine_replay_min_sample_candidates=1, engine_replay_min_shadow_intents=1))

    async def run():
        # Window entirely before any repo data exists.
        return await service.compare_variant(
            baseline_config={"current": True},
            candidate_config={"current": True},
            window_start_ms=now_ms - 7_200_000,
            window_end_ms=now_ms - 3_600_000,
            universe=["BTC"],
            variant_id="empty_window_v1",
        )

    artifact = anyio.run(run)

    assert artifact["status"] == "insufficient_data"
    assert artifact["metadata"]["verdict"] == "insufficient_data"
    assert artifact["metadata"]["promotion_decision"] == "do_not_promote"


def test_engine_replay_compare_still_fails_genuinely_worse_variant():
    now_ms = int(time.time() * 1000)
    repo = ReplayRepo(now_ms)
    service = EngineReplayComparisonService(repository=repo, settings=Settings(environment="test", engine_readiness_max_strategy_allocation_share_pct=100, engine_replay_min_sample_candidates=1, engine_replay_min_shadow_intents=1))

    async def run():
        return await service.compare_variant(
            baseline_config={"current": True},
            candidate_config={"engine_min_net_ev_bps": 1000, "engine_min_risk_adjusted_utility": 0.25},
            window_start_ms=now_ms - 60_000,
            window_end_ms=now_ms,
            universe=["BTC"],
            variant_id="worse_variant_v1",
        )

    artifact = anyio.run(run)

    assert artifact["status"] == "failed"
    assert artifact["metadata"]["verdict"] == "candidate_worse"
    assert artifact["metadata"]["promotion_decision"] == "do_not_promote"


def test_strategy_throttle_filters_candidates_and_blocks_loop_allocations():
    settings = Settings(environment="test", engine_strategy_max_candidates_per_loop=1, engine_strategy_max_allocations_per_loop=1)
    controller = StrategyThrottleController(settings)
    candidates = [_candidate("cand_1", "s1", 0.9), _candidate("cand_2", "s1", 0.5)]

    class Repo:
        enabled = False

        async def list_allocation_decisions(self, **kwargs):
            return []

        async def list_alpha_candidates(self, **kwargs):
            return []

    async def run():
        kept, events = await controller.filter_candidates(candidates, repository=Repo(), timestamp_ms=1)
        allocation = AllocationDecision(
            allocation_id="alloc_1",
            candidate_id="cand_1",
            status="allocate",
            allocated_size=1,
            allocated_notional_usd=100,
            risk_usd=1,
            reason_codes=[],
            created_at_ms=1,
            metadata={"strategy_id": "s1"},
        )
        allowed, reasons, metadata = await controller.allow_allocation(candidates[0], current_loop_allocations=[allocation], repository=Repo(), timestamp_ms=2)
        return kept, events, allowed, reasons, metadata

    kept, events, allowed, reasons, metadata = anyio.run(run)

    assert [item.candidate_id for item in kept] == ["cand_1"]
    assert events[0]["candidate_id"] == "cand_2"
    assert allowed is False
    assert reasons == ["strategy_throttle"]
    assert metadata["throttle_reason"] == "max_allocations_per_loop"


class PnLRepo:
    def __init__(self, now_ms: int):
        self.now_ms = now_ms
        self.pnl: list[dict[str, Any]] = []
        self.positions = [
            {
                "position_id": "pos_1",
                "entry_candidate_id": "cand_1",
                "strategy_id": "directional_momentum",
                "asset": "BTC",
                "side": "long",
                "stop": 90,
                "targets": [110],
                "position_state": "open",
                "execution_report_ids": ["er_1"],
                "opened_at_ms": now_ms - 60_000,
                "updated_at_ms": now_ms - 60_000,
                "degradation_reasons": [],
            }
        ]

    async def list_position_theses(self, **kwargs):
        return self.positions

    async def list_execution_reports(self, **kwargs):
        return [{"report_id": "er_1", "filled_size": 1, "requested_size": 1, "avg_fill_px": 100, "fees_usd": 0.1, "slippage_bps": 1, "created_at_ms": self.now_ms - 60_000}]

    async def list_pnl_attribution(self, **kwargs):
        return self.pnl

    async def record_pnl_attribution(self, item):
        self.pnl.append(item)
        return item["attribution_id"]

    async def record_position_thesis(self, item):
        self.positions[0] = item
        return item["position_id"]


class Mids:
    async def all_mids(self):
        return {"BTC": "111"}


def test_pnl_loop_marks_and_closes_target_hit_position():
    now_ms = int(time.time() * 1000)
    repo = PnLRepo(now_ms)
    service = EnginePnLAttributionLoopService(settings=Settings(environment="test"), repository=repo, hyperliquid=Mids())

    result = anyio.run(service.run_once)

    assert result["records_created"] == 1
    assert repo.pnl[0]["total_pnl_usd"] > 0
    assert repo.positions[0]["position_state"] == "closed"
    assert "target_hit" in repo.positions[0]["degradation_reasons"]


def test_unified_dashboard_routes_registered():
    app = create_app(Settings(environment="test", engine_enabled=True, engine_execution_modes="shadow", engine_readiness_min_candidates=1, engine_readiness_min_shadow_intents=0, engine_readiness_min_runs=0))
    now_ms = int(time.time() * 1000)
    from tests.test_engine_readiness import FakeReadinessRepository, FakeReadinessService

    repo = FakeReadinessRepository(now_ms=now_ms)
    async def list_candidate_config_diffs(**kwargs):
        return []

    async def list_regime_snapshots(**kwargs):
        asset = kwargs.get("primary_asset") or "BTC"
        return [
            {
                "regime_snapshot_id": f"reg_{asset}",
                "primary_asset": asset,
                "created_at_ms": now_ms,
                "as_of_ms": now_ms,
                "vector": {
                    "regime_snapshot_id": f"reg_{asset}",
                    "primary_asset": asset,
                    "created_at_ms": now_ms,
                    "as_of_ms": now_ms,
                    "regime_label": "trend=bull|news=catalyst",
                    "trend_state": "bull",
                    "volatility_state": "normal",
                    "news_state": "catalyst",
                    "news_catalyst_pressure": 0.4,
                    "regime_stability_score": 0.7,
                    "feature_coverage_pct": 100,
                    "derived_labels": {"news_risk_tier": "catalyst", "news_direction": "bullish"},
                },
            }
        ]

    repo.list_candidate_config_diffs = list_candidate_config_diffs
    repo.list_regime_snapshots = list_regime_snapshots
    app.state.repository = repo
    app.state.engine_service = FakeReadinessService(now_ms=now_ms)
    client = TestClient(app)

    root = client.get("/")
    assert root.status_code == 200
    assert "Hyperliquid Trading Agent" in root.text
    assert "/world-model/dashboard" in root.text
    assert "/dashboard" in root.text
    dashboard = client.get("/dashboard")
    assert "Trading Agent Dashboard" in dashboard.text
    assert "Loading bounded dashboard snapshot" in dashboard.text
    data = client.get("/dashboard/data").json()
    assert "engine" in data
    assert "readiness" in data["engine"]
    assert data["engine"]["readiness"]["detail_endpoint"] == "/engine/readiness"
    assert "checks" not in data["engine"]["readiness"]
    assert "reports" not in data["engine"]["readiness"]
    assert data["engine"]["candidate_funnel"]["sample_limit"] == 1_000
    assert data["engine"]["signal_quality"]["data_quality"]["sample_limit"] == 5_000
    assert data["engine"]["regime"]["latest_by_asset"]["BTC"]["news_risk_tier"] == "catalyst"

def test_pnl_loop_closes_aged_position_even_without_execution_report():
    now_ms = int(time.time() * 1000)
    repo = PnLRepo(now_ms)
    repo.positions = [
        {
            "position_id": "pos_era1",
            "entry_candidate_id": "cand_old",
            "strategy_id": "support_resistance_reversion_v2",
            "asset": "BTC",
            "side": "long",
            "stop": 90,
            "targets": [110],
            "position_state": "approved",
            "execution_report_ids": ["er_gone"],
            "opened_at_ms": now_ms - 49 * 60 * 60 * 1000,
            "updated_at_ms": now_ms - 49 * 60 * 60 * 1000,
            "degradation_reasons": [],
        }
    ]
    service = EnginePnLAttributionLoopService(settings=Settings(environment="test"), repository=repo, hyperliquid=Mids())

    result = anyio.run(service.run_once)

    assert result["positions_closed"] == 1
    assert result["records_created"] == 0
    assert repo.positions[0]["position_state"] == "closed"
    assert "max_age" in repo.positions[0]["degradation_reasons"]
    assert "missing_execution_report" in repo.positions[0]["degradation_reasons"]
