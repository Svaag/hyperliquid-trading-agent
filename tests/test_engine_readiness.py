from __future__ import annotations

import time
from typing import Any

import anyio
import pytest
from fastapi.testclient import TestClient

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.paper_signoff import build_paper_signoff_preflight
from hyperliquid_trading_agent.app.engine.readiness import build_paper_readiness_scorecard
from hyperliquid_trading_agent.app.engine.runtime import resolve_engine_runtime
from hyperliquid_trading_agent.app.main import create_app


class FakeReadinessService:
    def __init__(self, *, now_ms: int, run_count: int = 20, last_error: str | None = None):
        self.now_ms = now_ms
        self.run_count = run_count
        self.last_error = last_error

    def status(self) -> dict[str, Any]:
        return {"run_count": self.run_count, "last_run_at_ms": self.now_ms, "last_error": self.last_error}


class PassiveReadinessService:
    def status(self) -> dict[str, Any]:
        return {"enabled": False, "run_count": 0, "last_run_at_ms": None, "last_error": None}


class FakeReadinessRepository:
    enabled = True

    def __init__(self, *, now_ms: int, paper_leak: bool = False, missing_data: bool = False, risk_rejects: int = 0):
        self.now_ms = now_ms
        anchor = now_ms - 2 * 60 * 60 * 1000
        old = now_ms - 30 * 60 * 1000
        self.candidates = [
            {"candidate_id": "cand_1", "strategy_id": "directional_momentum_v2", "asset": "BTC", "status": "new", "side": "long", "regime_snapshot_id": "reg_1", "created_at_ms": old, "metadata": {"strategy_version": "2.0.0", "strategy_family": "trend_following", "feature_coverage_pct": 100.0, "counts_for_breadth": True, "regime_label": "trend=bull"}},
            {"candidate_id": "cand_2", "strategy_id": "microstructure_ofi_v2", "asset": "ETH", "status": "new", "side": "short", "regime_snapshot_id": "reg_2", "created_at_ms": now_ms - 1000, "metadata": {"strategy_version": "2.0.0", "strategy_family": "microstructure_orderflow", "feature_coverage_pct": 100.0, "counts_for_breadth": True, "regime_label": "orderflow=sell_pressure"}},
        ]
        self.evs = [
            {"estimate_id": "ev_1", "candidate_id": "cand_1", "net_ev_bps": 12, "risk_adjusted_utility": 0.4, "uncertainty": 0.1, "calibration_bucket": "medium", "created_at_ms": old},
            {"estimate_id": "ev_2", "candidate_id": "cand_2", "net_ev_bps": 9, "risk_adjusted_utility": 0.3, "uncertainty": 0.2, "calibration_bucket": "medium", "created_at_ms": now_ms - 1000},
        ]
        self.allocations = [
            {"allocation_id": "alloc_1", "candidate_id": "cand_1", "status": "allocate", "allocated_notional_usd": 1000, "created_at_ms": old, "metadata": {"strategy_id": "directional_momentum_v2", "strategy_family": "trend_following", "asset": "BTC"}},
            {"allocation_id": "alloc_2", "candidate_id": "cand_2", "status": "allocate", "allocated_notional_usd": 900, "created_at_ms": now_ms - 1000, "metadata": {"strategy_id": "microstructure_ofi_v2", "strategy_family": "microstructure_orderflow", "asset": "ETH"}},
            {"allocation_id": "alloc_3", "candidate_id": "cand_1", "status": "skip", "allocated_notional_usd": 0, "created_at_ms": old},
            {"allocation_id": "alloc_4", "candidate_id": "cand_2", "status": "skip", "allocated_notional_usd": 0, "created_at_ms": now_ms - 1000},
        ]
        self.intents = [
            {"intent_id": "intent_0", "parent_candidate_id": "cand_0", "strategy_id": "directional_momentum_v2", "execution_mode": "shadow", "created_at_ms": anchor},
            {"intent_id": "intent_1", "parent_candidate_id": "cand_1", "strategy_id": "directional_momentum_v2", "execution_mode": "shadow", "created_at_ms": old},
        ]
        if paper_leak:
            self.intents.append({"intent_id": "intent_paper", "parent_candidate_id": "cand_2", "strategy_id": "microstructure_ofi", "execution_mode": "paper", "created_at_ms": now_ms - 1000})
        self.reports = [{"report_id": "er_1", "intent_id": "intent_1", "execution_mode": "shadow", "status": "accepted", "slippage_bps": 0, "fees_usd": 0, "created_at_ms": old}]
        self.positions: list[dict[str, Any]] = []
        self.pnl: list[dict[str, Any]] = []
        self.risk_decisions = [
            {"decision_id": "risk_allow_0", "intent_id": "intent_0", "decision": "allow", "violations": [], "created_at_ms": anchor},
            {"decision_id": "risk_allow_1", "intent_id": "intent_1", "decision": "allow", "violations": [], "created_at_ms": old},
        ]
        self.risk_decisions.extend(
            {"decision_id": f"risk_{idx}", "intent_id": f"intent_reject_{idx}", "decision": "reject", "violations": ["stale_market_data"], "created_at_ms": now_ms - 1000}
            for idx in range(risk_rejects)
        )
        self.council_reviews = [
            {"review_id": "council_1", "candidate_id": "cand_1", "strategy_id": "directional_momentum_v2", "decision": "allow_shadow", "created_at_ms": old},
            {"review_id": "council_2", "candidate_id": "cand_2", "strategy_id": "microstructure_ofi_v2", "decision": "allow_shadow", "created_at_ms": now_ms - 1000},
        ]
        self.candidate_evidence_links = [
            {"link_id": "cel_1", "candidate_id": "cand_1", "strategy_id": "directional_momentum_v2", "risk_decision_id": "risk_pre_1", "council_review_id": "council_1", "outcome_window_ids": ["coa_1"], "created_at_ms": old, "metadata": {"council_decision": "allow_shadow"}},
            {"link_id": "cel_2", "candidate_id": "cand_2", "strategy_id": "microstructure_ofi_v2", "risk_decision_id": "risk_pre_2", "council_review_id": "council_2", "outcome_window_ids": ["coa_2"], "created_at_ms": now_ms - 1000, "metadata": {"council_decision": "allow_shadow"}},
        ]
        self.candidate_outcomes = [
            {"attribution_id": "coa_1", "candidate_id": "cand_1", "strategy_id": "directional_momentum_v2", "strategy_family": "trend_following", "asset": "BTC", "venue": "hyperliquid", "regime_snapshot_id": "reg_1", "outcome_window": "5m", "net_return_bps": 20, "terminal_state": "matured", "created_at_ms": old, "window_end_ms": old, "metadata": {"regime_label": "trend=bull"}},
            {"attribution_id": "coa_2", "candidate_id": "cand_2", "strategy_id": "microstructure_ofi_v2", "strategy_family": "microstructure_orderflow", "asset": "ETH", "venue": "hyperliquid", "regime_snapshot_id": "reg_2", "outcome_window": "5m", "net_return_bps": 10, "terminal_state": "matured", "created_at_ms": old, "window_end_ms": old, "metadata": {"regime_label": "orderflow=sell_pressure"}},
        ]
        self.portfolio_concentration_events: list[dict[str, Any]] = []
        self.strategy_regime_performance = [
            {"performance_id": "perf_1", "strategy_id": "directional_momentum_v2", "strategy_family": "trend_following", "regime_label": "trend=bull", "candidate_count": 2, "score": 60, "created_at_ms": old, "window_end_ms": old},
            {"performance_id": "perf_2", "strategy_id": "microstructure_ofi_v2", "strategy_family": "microstructure_orderflow", "regime_label": "orderflow=sell_pressure", "candidate_count": 2, "score": 60, "created_at_ms": old, "window_end_ms": old},
        ]
        self.replay_results = [
            {"replay_id": "ereplay_1", "proposal_id": "engine:test", "status": "passed", "candidate_metrics": {"candidate_count": 2}, "created_at_ms": now_ms - 1000, "metadata": {"artifact_type": "engine_shadow_comparison", "data_window": {"start_ms": now_ms - 60 * 60 * 1000, "end_ms": now_ms}, "verdict": "candidate_better"}}
        ]
        self.heartbeats: list[dict[str, Any]] = []
        self.missing_data = missing_data

    async def list_alpha_candidates(self, **kwargs):
        return self.candidates[: kwargs.get("limit", 100)]

    async def list_ev_estimates(self, **kwargs):
        return self.evs[: kwargs.get("limit", 100)]

    async def list_allocation_decisions(self, **kwargs):
        return self.allocations[: kwargs.get("limit", 100)]

    async def list_order_intents(self, **kwargs):
        items = self.intents
        if kwargs.get("execution_mode"):
            items = [item for item in items if item.get("execution_mode") == kwargs["execution_mode"]]
        return items[: kwargs.get("limit", 100)]

    async def list_execution_reports(self, **kwargs):
        return self.reports[: kwargs.get("limit", 100)]

    async def list_position_theses(self, **kwargs):
        return self.positions[: kwargs.get("limit", 100)]

    async def list_risk_gateway_decisions(self, **kwargs):
        items = self.risk_decisions
        if kwargs.get("decision"):
            items = [item for item in items if item.get("decision") == kwargs["decision"]]
        return items[: kwargs.get("limit", 100)]

    async def list_pnl_attribution(self, **kwargs):
        return self.pnl[: kwargs.get("limit", 100)]

    async def list_feature_values(self, **kwargs):
        if self.missing_data:
            return []
        return [{"feature_id": "feat_1", "asset": kwargs.get("asset"), "computed_ts_ms": self.now_ms - 1000}]

    async def latest_regime_snapshot(self, **kwargs):
        if self.missing_data:
            return None
        return {"regime_snapshot_id": "reg_1", "primary_asset": kwargs.get("primary_asset"), "as_of_ms": self.now_ms - 1000}

    async def list_council_reviews(self, **kwargs):
        return self.council_reviews[: kwargs.get("limit", 100)]

    async def list_candidate_evidence_links(self, **kwargs):
        return self.candidate_evidence_links[: kwargs.get("limit", 100)]

    async def list_candidate_outcome_attributions(self, **kwargs):
        return self.candidate_outcomes[: kwargs.get("limit", 100)]

    async def list_portfolio_concentration_events(self, **kwargs):
        return self.portfolio_concentration_events[: kwargs.get("limit", 100)]

    async def list_strategy_regime_performance(self, **kwargs):
        return self.strategy_regime_performance[: kwargs.get("limit", 100)]

    async def list_replay_results(self, **kwargs):
        return self.replay_results[: kwargs.get("limit", 100)]

    async def list_service_heartbeats(self, **kwargs):
        items = self.heartbeats
        if kwargs.get("service_role"):
            items = [item for item in items if item.get("service_role") == kwargs["service_role"]]
        return items[: kwargs.get("limit", 100)]


def test_engine_settings_defaults_are_shadow_only():
    settings = Settings(environment="test", _env_file=None)

    assert settings.engine_shadow_enabled is True
    assert settings.engine_paper_enabled is False
    assert settings.engine_live_enabled is False
    assert settings.engine_wave2_enabled is False
    assert settings.engine_execution_mode_list == ["shadow"]


def test_engine_wave2_flag_is_deferred_until_wave1_evidence_is_reliable():
    with pytest.raises(ValueError, match="ENGINE_WAVE2_ENABLED"):
        Settings(environment="test", engine_wave2_enabled=True)


def test_shadow_full_alpha_catalog_mode_requires_shadow_only_runtime():
    settings = Settings(environment="test", engine_alpha_catalog_mode="SHADOW_FULL_CATALOG", _env_file=None)
    assert settings.engine_alpha_catalog_mode == "shadow_full_catalog"

    with pytest.raises(ValueError, match="ENGINE_ALPHA_CATALOG_MODE=shadow_full_catalog requires"):
        Settings(environment="test", engine_alpha_catalog_mode="shadow_full_catalog", engine_paper_enabled=True, _env_file=None)
    with pytest.raises(ValueError, match="ENGINE_ALPHA_CATALOG_MODE=shadow_full_catalog requires"):
        Settings(environment="test", engine_alpha_catalog_mode="shadow_full_catalog", engine_execution_modes="paper,shadow", _env_file=None)
    with pytest.raises(ValueError, match="ENGINE_ALPHA_CATALOG_MODE=shadow_full_catalog requires"):
        Settings(environment="test", engine_alpha_catalog_mode="shadow_full_catalog", engine_shadow_enabled=False, _env_file=None)


def readiness_settings(**overrides) -> Settings:
    defaults = dict(
        environment="test",
        engine_enabled=True,
        engine_execution_modes="shadow",
        engine_shadow_enabled=True,
        engine_paper_enabled=False,
        engine_live_enabled=False,
        autonomy_core_universe="BTC",
        engine_readiness_window_hours=1,
        engine_readiness_min_runs=1,
        engine_readiness_min_candidates=2,
        engine_readiness_min_shadow_intents=1,
        engine_readiness_min_score_to_pass=85,
        engine_validation_alert_stale_loop_seconds=300,
        engine_validation_missing_data_seconds=300,
        engine_validation_risk_reject_spike_count=5,
        engine_readiness_min_active_strategy_count_24h=2,
        engine_readiness_min_active_strategy_family_count_24h=2,
        engine_readiness_max_symbol_strategy_allocation_share_pct=60,
        engine_readiness_min_candidate_strategy_metadata_coverage_pct=100,
        engine_readiness_min_candidate_evidence_link_coverage_pct=100,
        engine_readiness_min_council_packet_coverage_pct=100,
        engine_readiness_min_candidate_risk_gateway_coverage_pct=100,
        engine_readiness_min_matured_outcome_attribution_coverage_pct=100,
        engine_readiness_min_council_review_coverage_pct=100,
        engine_readiness_min_risk_gateway_coverage_pct=100,
        engine_readiness_min_strategy_regime_evidence_coverage_pct=100,
        engine_readiness_min_strategy_regime_sample_count=1,
        engine_readiness_min_strategy_regime_score=45,
        engine_readiness_require_latest_replay=True,
        engine_readiness_min_replay_window_hours=1,
        engine_readiness_min_replay_sample_size=2,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_paper_readiness_can_pass_with_clean_shadow_sample():
    now_ms = int(time.time() * 1000)
    repo = FakeReadinessRepository(now_ms=now_ms)
    service = FakeReadinessService(now_ms=now_ms)
    settings = readiness_settings()

    async def run():
        return await build_paper_readiness_scorecard(repo, settings, service, window_hours=1, limit=100)

    scorecard = anyio.run(run)

    assert scorecard["ready_for_paper"] is True
    assert scorecard["grade"] == "pass"
    assert scorecard["score"] >= 85
    assert scorecard["hard_blocks"] == []
    assert scorecard["recommendation"] == "ready_for_paper"


def test_paper_readiness_uses_trader_engine_loop_heartbeat_when_api_service_is_passive():
    now_ms = int(time.time() * 1000)
    repo = FakeReadinessRepository(now_ms=now_ms)
    repo.heartbeats = [
        {
            "service_role": "trader",
            "instance_id": "trader-1",
            "status": "running",
            "updated_at_ms": now_ms,
            "metadata": {
                "engine_loop": {
                    "enabled": True,
                    "running": True,
                    "service": {"enabled": True, "run_count": 3, "last_run_at_ms": now_ms, "last_error": None},
                }
            },
        }
    ]
    settings = readiness_settings()

    async def run():
        return await build_paper_readiness_scorecard(repo, settings, PassiveReadinessService(), window_hours=1, limit=100)

    scorecard = anyio.run(run)
    reliability = scorecard["checks"]["engine_reliability"]

    assert scorecard["ready_for_paper"] is True
    assert reliability["run_count"] == 3
    assert reliability["runtime_source"] == "trader_heartbeat"
    assert reliability["runtime_instance_id"] == "trader-1"


def test_scheduler_resolves_engine_enabled_state_from_fresh_trader_heartbeat():
    now_ms = int(time.time() * 1000)
    repo = FakeReadinessRepository(now_ms=now_ms)
    repo.heartbeats = [
        {
            "service_role": "trader",
            "instance_id": "trader-runtime",
            "status": "running",
            "updated_at_ms": now_ms,
            "metadata": {
                "engine_loop": {
                    "enabled": True,
                    "running": True,
                    "paper_enabled": False,
                    "live_enabled": False,
                    "wave1c_enabled": True,
                    "wave2_enabled": False,
                    "service": {
                        "enabled": True,
                        "run_count": 37,
                        "last_run_at_ms": now_ms,
                    },
                }
            },
        }
    ]
    settings = readiness_settings(_env_file=None, engine_enabled=False)

    runtime = anyio.run(resolve_engine_runtime, repo, settings)

    assert runtime["enabled"] is True
    assert runtime["runtime_source"] == "trader_heartbeat"
    assert runtime["run_count"] == 37
    assert runtime["wave1c_enabled"] is True


def test_readiness_separates_shadow_research_breadth_from_paper_eligible_breadth():
    now_ms = int(time.time() * 1000)
    repo = FakeReadinessRepository(now_ms=now_ms)
    for candidate in repo.candidates:
        candidate["source_integrity"] = {"activation_scope": "shadow_only", "paper_eligible": False, "operator_promotion_required": True}
    service = FakeReadinessService(now_ms=now_ms)
    settings = readiness_settings(engine_alpha_catalog_mode="shadow_full_catalog")

    async def run():
        return await build_paper_readiness_scorecard(repo, settings, service, window_hours=1, limit=100)

    scorecard = anyio.run(run)
    diversity = scorecard["checks"]["strategy_diversity"]
    codes = {item["code"] for item in scorecard["hard_blocks"]}

    assert diversity["active_shadow_strategy_count"] == 2
    assert diversity["active_shadow_family_count"] == 2
    assert diversity["shadow_research_strategy_count"] == 2
    assert diversity["paper_eligible_active_strategy_count"] == 0
    assert diversity["paper_eligible_active_family_count"] == 0
    assert "insufficient_active_strategy_count" in codes
    assert scorecard["ready_for_paper"] is False


def test_paper_readiness_blocks_paper_leak_missing_data_and_risk_spike():
    now_ms = int(time.time() * 1000)
    repo = FakeReadinessRepository(now_ms=now_ms, paper_leak=True, missing_data=True, risk_rejects=5)
    service = FakeReadinessService(now_ms=now_ms)
    settings = readiness_settings()

    async def run():
        return await build_paper_readiness_scorecard(repo, settings, service, window_hours=1, limit=100)

    scorecard = anyio.run(run)
    codes = {item["code"] for item in scorecard["hard_blocks"]}

    assert scorecard["ready_for_paper"] is False
    assert scorecard["grade"] == "blocked"
    assert "paper_intents_in_shadow_only" in codes
    assert "missing_core_data" in codes
    assert "risk_reject_spike_critical" in codes


def test_paper_readiness_blocks_missing_replay_and_council_coverage():
    now_ms = int(time.time() * 1000)
    repo = FakeReadinessRepository(now_ms=now_ms)
    repo.replay_results = []
    repo.council_reviews = []
    service = FakeReadinessService(now_ms=now_ms)
    settings = readiness_settings()

    async def run():
        return await build_paper_readiness_scorecard(repo, settings, service, window_hours=1, limit=100)

    scorecard = anyio.run(run)
    codes = {item["code"] for item in scorecard["hard_blocks"]}

    assert "replay_comparison_missing" in codes
    assert "council_review_coverage_low" in codes
    assert scorecard["checks"]["shadow_replay"]["required"] is True


def test_paper_signoff_preflight_requires_symbol_evidence_and_no_live_mode():
    now_ms = int(time.time() * 1000)
    repo = FakeReadinessRepository(now_ms=now_ms)
    service = FakeReadinessService(now_ms=now_ms)
    settings = readiness_settings()

    async def run():
        return await build_paper_signoff_preflight(repo, settings, service, symbols=["BTC", "ETH"], window_hours=1, limit=100)

    preflight = anyio.run(run)
    assert preflight["ready_for_paper_signoff"] is True
    assert preflight["paper_only"] is True
    assert preflight["live_execution_allowed"] is False
    assert preflight["evidence_quality"]["passes_minimums"] is True

    async def run_missing_symbol():
        return await build_paper_signoff_preflight(repo, settings, service, symbols=["BTC", "HYPE"], window_hours=1, limit=100)

    missing_symbol = anyio.run(run_missing_symbol)
    assert missing_symbol["ready_for_paper_signoff"] is False
    assert missing_symbol["symbol_evidence"][1]["symbol"] == "HYPE"
    assert missing_symbol["symbol_evidence"][1]["has_shadow_evidence"] is False


def test_engine_readiness_route_is_registered():
    now_ms = int(time.time() * 1000)
    settings = readiness_settings()
    app = create_app(settings)
    app.state.repository = FakeReadinessRepository(now_ms=now_ms)
    app.state.engine_service = FakeReadinessService(now_ms=now_ms)
    client = TestClient(app)

    response = client.get("/engine/readiness", params={"window_hours": 1, "limit": 100})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ready_for_paper"] is True
    assert payload["checks"]["shadow_integrity"]["paper_intent_count"] == 0

    preflight = client.get("/engine/paper-signoff/preflight", params={"symbols": "BTC,ETH", "window_hours": 1, "limit": 100})
    assert preflight.status_code == 200
    assert preflight.json()["ready_for_paper_signoff"] is True

def test_paper_readiness_surfaces_stale_trader_heartbeat_with_true_run_count():
    now_ms = int(time.time() * 1000)
    repo = FakeReadinessRepository(now_ms=now_ms)
    stale_updated = now_ms - 600_000
    repo.heartbeats = [
        {
            "service_role": "trader",
            "instance_id": "trader-1",
            "status": "running",
            "updated_at_ms": stale_updated,
            "metadata": {
                "engine_loop": {
                    "enabled": True,
                    "running": True,
                    "service": {"enabled": True, "run_count": 756, "last_run_at_ms": stale_updated, "last_error": None},
                }
            },
        }
    ]
    settings = readiness_settings()

    async def run():
        return await build_paper_readiness_scorecard(repo, settings, PassiveReadinessService(), window_hours=1, limit=100)

    scorecard = anyio.run(run)
    reliability = scorecard["checks"]["engine_reliability"]

    assert reliability["run_count"] == 756
    assert reliability["runtime_stale"] is True
    assert reliability["runtime_age_ms"] >= 600_000
    assert "engine_loop_stale" in {block["code"] for block in scorecard["hard_blocks"]}
    assert scorecard["ready_for_paper"] is False

def test_paper_readiness_blocks_zero_sample_replay_artifact():
    now_ms = int(time.time() * 1000)
    repo = FakeReadinessRepository(now_ms=now_ms)
    repo.replay_results = [
        {
            "replay_id": "ereplay_zero",
            "proposal_id": "engine:test",
            "status": "advisory_pass",
            "candidate_metrics": {"candidate_count": 0},
            "created_at_ms": now_ms - 1000,
            "metadata": {"artifact_type": "engine_shadow_comparison", "data_window": {"start_ms": now_ms - 60 * 60 * 1000, "end_ms": now_ms}, "verdict": "baseline_equivalence"},
        }
    ]
    settings = readiness_settings()

    async def run():
        return await build_paper_readiness_scorecard(repo, settings, FakeReadinessService(now_ms=now_ms), window_hours=1, limit=100)

    scorecard = anyio.run(run)

    assert "replay_comparison_stale" in {block["code"] for block in scorecard["hard_blocks"]}
    assert scorecard["ready_for_paper"] is False
