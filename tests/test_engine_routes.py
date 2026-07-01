from __future__ import annotations

from fastapi.testclient import TestClient

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.main import create_app


class FakeEngineRepository:
    enabled = True

    async def list_normalized_events(self, **kwargs):
        return [{"event_id": "evt_1", "event_type": kwargs.get("event_type") or "all_mids"}]

    async def get_normalized_event(self, event_id):
        return {"event_id": event_id, "event_type": "all_mids"} if event_id == "evt_1" else None

    async def list_feature_values(self, **kwargs):
        return [{"feature_id": "feat_1", "asset": kwargs.get("asset"), "feature_name": kwargs.get("feature_name") or "mid"}]

    async def latest_regime_snapshot(self, primary_asset=None):
        return {"regime_snapshot_id": "reg_1", "primary_asset": primary_asset or "GLOBAL"}

    async def list_regime_snapshots(self, **kwargs):
        return [{"regime_snapshot_id": "reg_1", "primary_asset": kwargs.get("primary_asset") or "GLOBAL", "created_at_ms": 1_000}]

    async def list_alpha_candidates(self, **kwargs):
        return [{"candidate_id": "cand_1", "status": kwargs.get("status") or "new"}]

    async def get_alpha_candidate(self, candidate_id):
        return {"candidate_id": candidate_id} if candidate_id == "cand_1" else None

    async def latest_candidate_book_snapshot(self):
        return {"candidate_book_id": "book_1"}

    async def list_ev_estimates(self, **kwargs):
        return [{"estimate_id": "ev_1"}]

    async def list_allocation_decisions(self, **kwargs):
        return [{"allocation_id": "alloc_1"}]

    async def list_strategy_specs(self, **kwargs):
        return [{"strategy_id": "microstructure_ofi_v2", "family": kwargs.get("family") or "microstructure_orderflow", "enabled": True, "counts_for_breadth": True, "metadata": {}}]

    async def get_strategy_spec(self, strategy_id):
        return {"strategy_id": strategy_id, "family": "microstructure_orderflow"} if strategy_id == "microstructure_ofi_v2" else None

    async def list_strategy_regime_performance(self, **kwargs):
        return [{"performance_id": "perf_1", "strategy_id": kwargs.get("strategy_id") or "microstructure_ofi_v2"}]

    async def list_candidate_trade_packets(self, **kwargs):
        return [{"packet_id": "packet_1"}]

    async def list_candidate_evidence_links(self, **kwargs):
        return [{"link_id": "cel_1"}]

    async def list_candidate_outcome_attributions(self, **kwargs):
        return [{"attribution_id": "coa_1", "outcome_window": kwargs.get("outcome_window") or "5m"}]

    async def list_council_reviews(self, **kwargs):
        return [{"review_id": "council_1"}]

    async def list_allocation_diversity_events(self, **kwargs):
        return [{"event_id": "div_1"}]

    async def list_portfolio_concentration_events(self, **kwargs):
        return [{"event_id": "pce_1"}]

    async def list_replay_result_links(self, **kwargs):
        return [{"link_id": "rrl_1"}]

    async def list_bandit_recommendations(self, **kwargs):
        return [{"recommendation_id": "bandit_1", "auto_apply_allowed": False}]

    async def upsert_bandit_policy_snapshot(self, snapshot):
        return snapshot["policy_id"]

    async def record_bandit_recommendation(self, recommendation):
        return recommendation["recommendation_id"]

    async def get_evidence_pack(self, evidence_pack_id):
        return {"evidence_pack_id": evidence_pack_id} if evidence_pack_id == "ep_1" else None

    async def list_debate_decisions(self, **kwargs):
        return [{"debate_decision_id": "dd_1"}]

    async def list_order_intents(self, **kwargs):
        return [{"intent_id": "intent_1"}]

    async def list_execution_reports(self, **kwargs):
        return [{"report_id": "er_1"}]

    async def list_position_theses(self, **kwargs):
        return [{"position_id": "pos_1"}]

    async def list_reconciliation_runs(self, **kwargs):
        return [{"reconciliation_id": "recon_1"}]

    async def list_model_versions(self, **kwargs):
        return [{"model_version_id": "model_1"}]

    async def list_risk_gateway_decisions(self, **kwargs):
        return [{"decision_id": "risk_1", "decision": "reject", "violations": ["stale_market_data"]}]

    async def list_pnl_attribution(self, **kwargs):
        return [{"attribution_id": "pnl_1", "strategy_id": "directional_momentum", "total_pnl_usd": 1.2}]

    async def list_retention_runs(self, **kwargs):
        return [{"retention_run_id": "ret_1"}]


def test_engine_readonly_routes_are_registered_and_auth_protected_in_dev():
    app = create_app(Settings(environment="test", engine_execution_modes="paper,shadow"))
    app.state.repository = FakeEngineRepository()
    client = TestClient(app)

    assert client.get("/engine/status").json()["execution_modes"] == ["paper", "shadow"]
    assert client.get("/engine/events").json()[0]["event_id"] == "evt_1"
    assert client.get("/engine/events/evt_1").json()["event_id"] == "evt_1"
    assert client.get("/engine/features", params={"asset": "BTC"}).json()[0]["asset"] == "BTC"
    assert client.get("/engine/regime/latest").json()["regime_snapshot_id"] == "reg_1"
    assert client.get("/engine/regime/history", params={"primary_asset": "BTC"}).json()[0]["primary_asset"] == "BTC"
    assert client.get("/engine/candidates").json()[0]["candidate_id"] == "cand_1"
    assert client.get("/engine/candidates/cand_1").json()["candidate_id"] == "cand_1"
    assert client.get("/engine/candidate-book/latest").json()["candidate_book_id"] == "book_1"
    assert client.get("/engine/ev-estimates").json()[0]["estimate_id"] == "ev_1"
    assert client.get("/engine/allocations").json()[0]["allocation_id"] == "alloc_1"
    assert client.get("/engine/strategies").json()[0]["strategy_id"] == "microstructure_ofi_v2"
    catalog = client.get("/engine/strategy-catalog").json()
    assert catalog["total_specs"] == 1
    assert catalog["runtime_enabled"] == 1
    assert catalog["families"][0]["family"] == "microstructure_orderflow"
    assert client.get("/engine/strategies/microstructure_ofi_v2").json()["strategy_id"] == "microstructure_ofi_v2"
    assert client.get("/engine/strategy-regime-performance").json()[0]["performance_id"] == "perf_1"
    assert client.get("/engine/strategy-regime-performance/microstructure_ofi_v2").json()[0]["strategy_id"] == "microstructure_ofi_v2"
    assert client.post("/engine/strategy-regime-performance/refresh", json={"window_hours": 24}).json()["report_only"] is True
    assert client.get("/engine/candidate-trade-packets").json()[0]["packet_id"] == "packet_1"
    assert client.get("/engine/candidate-evidence-links").json()[0]["link_id"] == "cel_1"
    assert client.get("/engine/candidate-outcome-attributions").json()[0]["attribution_id"] == "coa_1"
    assert client.get("/engine/council-reviews").json()[0]["review_id"] == "council_1"
    assert client.get("/engine/diversity-events").json()[0]["event_id"] == "div_1"
    assert client.get("/engine/portfolio-concentration-events").json()[0]["event_id"] == "pce_1"
    assert client.get("/engine/replay-result-links").json()[0]["link_id"] == "rrl_1"
    assert client.get("/engine/bandit-recommendations").json()[0]["recommendation_id"] == "bandit_1"
    assert client.get("/engine/alpha-graph").json()["graph_id"] == "strategy_regime_alpha_graph_v1"
    assert client.post("/engine/bandit-recommendations/run", json={"window_hours": 24}).json()["auto_apply_allowed"] is False
    assert client.get("/engine/evidence-packs/ep_1").json()["evidence_pack_id"] == "ep_1"
    assert client.get("/engine/debate-decisions").json()[0]["debate_decision_id"] == "dd_1"
    assert client.get("/engine/order-intents").json()[0]["intent_id"] == "intent_1"
    assert client.get("/engine/execution-reports").json()[0]["report_id"] == "er_1"
    assert client.get("/engine/positions").json()[0]["position_id"] == "pos_1"
    assert client.get("/engine/reconciliation").json()[0]["reconciliation_id"] == "recon_1"
    assert client.get("/engine/model-versions").json()[0]["model_version_id"] == "model_1"
    assert client.get("/engine/risk-rejects").json()[0]["decision_id"] == "risk_1"
    assert client.get("/engine/pnl-attribution").json()[0]["attribution_id"] == "pnl_1"
    report = client.get("/engine/validation-report").json()
    assert report["summary"]["risk_reject_count"] == 1
    assert "by_strategy" in report
    dashboard = client.get("/engine/dashboard")
    assert dashboard.status_code == 200
    assert "Engine Validation Dashboard" in dashboard.text
    assert client.get("/engine/retention").json()[0]["retention_run_id"] == "ret_1"


def test_engine_routes_require_token_outside_dev():
    app = create_app(Settings(environment="prod", agent_api_bearer_token="secret", engine_enabled=False, autonomy_enabled=False, hip4_enabled=False, orchestration_wave_supervisor_enabled=False, tradfi_enabled=False, _env_file=None))
    app.state.repository = FakeEngineRepository()
    client = TestClient(app)

    assert client.get("/engine/status").status_code == 401
    assert client.get("/engine/status", headers={"Authorization": "Bearer secret"}).status_code == 200
