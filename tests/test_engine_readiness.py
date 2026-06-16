from __future__ import annotations

import time
from typing import Any

import anyio
from fastapi.testclient import TestClient

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.readiness import build_paper_readiness_scorecard
from hyperliquid_trading_agent.app.main import create_app


class FakeReadinessService:
    def __init__(self, *, now_ms: int, run_count: int = 20, last_error: str | None = None):
        self.now_ms = now_ms
        self.run_count = run_count
        self.last_error = last_error

    def status(self) -> dict[str, Any]:
        return {"run_count": self.run_count, "last_run_at_ms": self.now_ms, "last_error": self.last_error}


class FakeReadinessRepository:
    enabled = True

    def __init__(self, *, now_ms: int, paper_leak: bool = False, missing_data: bool = False, risk_rejects: int = 0):
        self.now_ms = now_ms
        anchor = now_ms - 2 * 60 * 60 * 1000
        old = now_ms - 30 * 60 * 1000
        self.candidates = [
            {"candidate_id": "cand_1", "strategy_id": "directional_momentum", "asset": "BTC", "status": "new", "side": "long", "created_at_ms": old},
            {"candidate_id": "cand_2", "strategy_id": "microstructure_ofi", "asset": "BTC", "status": "new", "side": "short", "created_at_ms": now_ms - 1000},
        ]
        self.evs = [
            {"estimate_id": "ev_1", "candidate_id": "cand_1", "net_ev_bps": 12, "risk_adjusted_utility": 0.4, "uncertainty": 0.1, "calibration_bucket": "medium", "created_at_ms": old},
            {"estimate_id": "ev_2", "candidate_id": "cand_2", "net_ev_bps": 9, "risk_adjusted_utility": 0.3, "uncertainty": 0.2, "calibration_bucket": "medium", "created_at_ms": now_ms - 1000},
        ]
        self.allocations = [
            {"allocation_id": "alloc_1", "candidate_id": "cand_1", "status": "allocate", "allocated_notional_usd": 1000, "created_at_ms": old},
            {"allocation_id": "alloc_2", "candidate_id": "cand_2", "status": "allocate", "allocated_notional_usd": 900, "created_at_ms": now_ms - 1000},
            {"allocation_id": "alloc_3", "candidate_id": "cand_1", "status": "skip", "allocated_notional_usd": 0, "created_at_ms": old},
            {"allocation_id": "alloc_4", "candidate_id": "cand_2", "status": "skip", "allocated_notional_usd": 0, "created_at_ms": now_ms - 1000},
        ]
        self.intents = [
            {"intent_id": "intent_0", "parent_candidate_id": "cand_0", "strategy_id": "directional_momentum", "execution_mode": "shadow", "created_at_ms": anchor},
            {"intent_id": "intent_1", "parent_candidate_id": "cand_1", "strategy_id": "directional_momentum", "execution_mode": "shadow", "created_at_ms": old},
        ]
        if paper_leak:
            self.intents.append({"intent_id": "intent_paper", "parent_candidate_id": "cand_2", "strategy_id": "microstructure_ofi", "execution_mode": "paper", "created_at_ms": now_ms - 1000})
        self.reports = [{"report_id": "er_1", "intent_id": "intent_1", "execution_mode": "shadow", "status": "accepted", "slippage_bps": 0, "fees_usd": 0, "created_at_ms": old}]
        self.positions: list[dict[str, Any]] = []
        self.pnl: list[dict[str, Any]] = []
        self.risk_rejects = [
            {"decision_id": f"risk_{idx}", "decision": "reject", "violations": ["stale_market_data"], "created_at_ms": now_ms - 1000}
            for idx in range(risk_rejects)
        ]
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
        return self.risk_rejects[: kwargs.get("limit", 100)]

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

    async def list_replay_results(self, **kwargs):
        return []


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
