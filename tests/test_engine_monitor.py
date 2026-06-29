from __future__ import annotations

import time

import anyio

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.monitor import EngineValidationMonitorService, format_engine_validation_digest


class FakeSink:
    def __init__(self):
        self.messages: list[str] = []

    async def send(self, channel_id: str, content: str) -> str | None:
        self.messages.append(content)
        return "msg_1"


class FakeEngineService:
    def __init__(self, status):
        self._status = status

    def status(self):
        return self._status


class FakeMonitorRepository:
    def __init__(self):
        self.now = int(time.time() * 1000)

    async def list_alpha_candidates(self, **kwargs):
        return [
            {"candidate_id": "cand_1", "strategy_id": "directional_momentum", "asset": "BTC", "status": "new", "side": "long"},
            {"candidate_id": "cand_2", "strategy_id": "microstructure_ofi", "asset": "ETH", "status": "new", "side": "short"},
        ]

    async def list_ev_estimates(self, **kwargs):
        return [
            {
                "estimate_id": "ev_1",
                "candidate_id": "cand_1",
                "net_ev_bps": 12,
                "risk_adjusted_utility": 0.5,
                "uncertainty": 0.2,
                "calibration_bucket": "medium_confidence",
            }
        ]

    async def list_allocation_decisions(self, **kwargs):
        return [{"allocation_id": "alloc_1", "candidate_id": "cand_1", "status": "allocate"}]

    async def list_order_intents(self, **kwargs):
        if kwargs.get("execution_mode") == "paper":
            return [{"intent_id": "intent_paper", "strategy_id": "directional_momentum", "execution_mode": "paper"}]
        return [{"intent_id": "intent_1", "strategy_id": "directional_momentum", "execution_mode": "shadow"}]

    async def list_execution_reports(self, **kwargs):
        return [{"report_id": "er_1", "intent_id": "intent_1", "execution_mode": "shadow", "status": "filled", "slippage_bps": 2, "fees_usd": 1.5}]

    async def list_position_theses(self, **kwargs):
        return []

    async def list_risk_gateway_decisions(self, **kwargs):
        return [
            {"decision_id": f"risk_{idx}", "decision": "reject", "violations": ["stale_market_data"], "created_at_ms": self.now}
            for idx in range(6)
        ]

    async def list_pnl_attribution(self, **kwargs):
        return []

    async def list_feature_values(self, **kwargs):
        asset = kwargs.get("asset")
        if asset == "BTC":
            return [{"feature_id": "feat_1", "asset": asset, "computed_ts_ms": self.now}]
        return []

    async def latest_regime_snapshot(self, **kwargs):
        if kwargs.get("primary_asset") == "BTC":
            return {"regime_snapshot_id": "reg_1", "primary_asset": "BTC", "as_of_ms": self.now}
        return None


def test_engine_validation_digest_formats_summary():
    settings = Settings(engine_enabled=True, engine_paper_enabled=False, engine_execution_modes="shadow")
    message = format_engine_validation_digest(
        {
            "summary": {"candidate_count": 1, "ev_estimate_count": 1, "allocated_count": 1, "allocation_count": 1, "allocation_rate_pct": 100, "shadow_intent_count": 1, "paper_intent_count": 0, "execution_report_count": 1, "risk_reject_count": 0, "open_position_count": 0},
            "execution_simulations": {"avg_slippage_bps": 2, "fees_usd": 1},
            "by_strategy": {"directional_momentum": {"candidate_count": 1, "allocated_count": 1, "shadow_intent_count": 1, "avg_net_ev_bps": 12, "total_pnl_usd": 0}},
            "ev_calibration": {"bucket_summary": {"medium": {"count": 1, "avg_net_ev_bps": 12, "avg_uncertainty": 0.2, "realized_sample_count": 0}}},
        },
        [],
        settings=settings,
        service_status={"run_count": 3, "last_error": None, "last_run_at_ms": 123},
    )

    assert "Engine validation digest" in message
    assert "No alert conditions" in message
    assert "directional_momentum" in message


def test_engine_validation_monitor_detects_bad_shadow_conditions_and_posts_digest():
    settings = Settings(
        engine_enabled=True,
        engine_paper_enabled=False,
        engine_shadow_enabled=True,
        engine_execution_modes="shadow",
        autonomy_core_universe="BTC,ETH",
        autonomy_alert_channel_id="alerts",
        engine_validation_risk_reject_spike_count=5,
        engine_validation_missing_data_seconds=300,
    )
    repo = FakeMonitorRepository()
    sink = FakeSink()
    service = EngineValidationMonitorService(
        settings=settings,
        repository=repo,
        engine_service=FakeEngineService({"run_count": 1, "last_error": None, "last_run_at_ms": repo.now}),
        alert_sink=sink,
    )

    async def run():
        result = await service.run_once(post=True)
        return result

    result = anyio.run(run)
    alert_types = {item["type"] for item in result["alerts"]}

    assert "paper_intent_in_shadow_only" in alert_types
    assert "risk_rejects_spike" in alert_types
    assert "missing_feature_or_regime_data" in alert_types
    assert sink.messages
    assert service.status()["digest_count"] == 1
