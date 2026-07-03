from __future__ import annotations

import anyio

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.alpha.wave1a import RegimeDefensiveFlatStrategy
from hyperliquid_trading_agent.app.engine.schemas import AlphaCandidate, EVEstimate, FeatureSnapshot, RegimeVector
from hyperliquid_trading_agent.app.engine.service import InstitutionalEngineService
from hyperliquid_trading_agent.app.engine.strategy_registry import WAVE_1A_NUCLEUS_IDS
from hyperliquid_trading_agent.app.governance.risk_gateway import RiskGateway


class FakeHyperliquidEngine:
    def __init__(self):
        self.l2_calls = 0

    async def all_mids(self):
        return {"BTC": "104"}

    async def l2_book(self, symbol):
        self.l2_calls += 1
        return {"levels": [[[103.9, 1000]], [[104.1, 900]]]}


class FakeEngineHardeningRepo:
    enabled = True

    def __init__(self):
        self.specs: list[dict] = []
        self.risk_decisions: list[dict] = []

    async def upsert_strategy_spec(self, spec: dict):
        self.specs.append(spec)
        return spec["strategy_id"]

    async def record_risk_gateway_decision(self, decision: dict):
        self.risk_decisions.append(decision)
        return decision["decision_id"]


def _regime(**overrides) -> RegimeVector:
    data = dict(
        regime_snapshot_id="reg_flat",
        primary_asset="BTC",
        created_at_ms=1_000,
        as_of_ms=1_000,
        trend_state="range",
        trend_confidence=0.3,
        liquidity_state="normal",
        spread_state="tight",
        volatility_state="extreme",
        funding_state="neutral",
        oi_state="flat",
        liquidation_state="calm",
        orderflow_state="balanced",
        news_state="no_event",
        correlation_state="normal",
        session_state="us",
        feature_coverage_pct=100.0,
        regime_label="volatility=extreme",
        regime_stability_score=0.75,
    )
    data.update(overrides)
    return RegimeVector(**data)


def test_institutional_engine_run_once_is_paper_shadow_only():
    settings = Settings(
        engine_enabled=True,
        engine_min_net_ev_bps=-100,
        engine_min_risk_adjusted_utility=-100,
        autonomy_core_universe="BTC",
        autonomy_max_hot_l2_assets=1,
        engine_debate_priority_min=0,
        engine_paper_enabled=True,
        engine_shadow_enabled=True,
    )
    risk_gateway = RiskGateway(settings=settings)
    service = InstitutionalEngineService(settings=settings, repository=None, hyperliquid=FakeHyperliquidEngine(), risk_gateway=risk_gateway)

    async def run():
        # First pass seeds only one price, so run twice to build trend history.
        await service.run_once(symbols=["BTC"])
        return await service.run_once(symbols=["BTC"])

    result = anyio.run(run)

    assert result["candidates"] >= 0
    assert service.status()["run_count"] == 2
    assert service.status()["last_error"] is None


def test_engine_persists_all_strategy_specs_for_reporting():
    repo = FakeEngineHardeningRepo()
    settings = Settings(environment="test")
    service = InstitutionalEngineService(settings=settings, repository=repo, hyperliquid=FakeHyperliquidEngine(), risk_gateway=RiskGateway(settings=settings, repository=repo))

    async def run():
        return await service.persist_strategy_specs()

    count = anyio.run(run)
    ids = {spec["strategy_id"] for spec in repo.specs}

    assert count == len(repo.specs)
    assert WAVE_1A_NUCLEUS_IDS <= ids
    assert "microstructure_absorption_v1" in ids
    assert service.status()["strategy_specs_persisted"] is True


def test_flat_candidates_receive_explicit_no_trade_risk_evidence():
    repo = FakeEngineHardeningRepo()
    settings = Settings(environment="test", engine_shadow_enabled=True, engine_paper_enabled=False)
    service = InstitutionalEngineService(settings=settings, repository=repo, hyperliquid=FakeHyperliquidEngine(), risk_gateway=RiskGateway(settings=settings, repository=repo))
    candidate = RegimeDefensiveFlatStrategy().generate(FeatureSnapshot(snapshot_id="fs_flat", asset="BTC", as_of_ms=1_000, features={"mid": 100.0}), _regime(), timestamp_ms=10_000)[0]
    ev = EVEstimate(
        estimate_id="ev_flat",
        candidate_id=candidate.candidate_id,
        model_version_id="deterministic_fallback_v1",
        p_target=0.3,
        p_stop=0.3,
        p_timeout=0.4,
        expected_favorable_bps=0,
        expected_adverse_bps=1,
        expected_holding_ms=60_000,
        expected_fee_bps=0,
        expected_spread_cost_bps=0,
        expected_slippage_bps=0,
        expected_market_impact_bps=0,
        expected_funding_cost_bps=0,
        tail_loss_bps=1,
        net_ev_bps=0,
        risk_adjusted_utility=0,
        uncertainty=0.1,
        calibration_bucket="flat",
        created_at_ms=10_000,
    )

    async def run():
        return await service._candidate_risk_precheck(candidate, ev, snapshot_features={"spread_bps": 3.0}, timestamp_ms=10_000)

    decision = anyio.run(run)

    assert decision.allowed is True
    assert decision.intent_id == f"no_trade_{candidate.candidate_id}"
    assert decision.metadata["candidate_level_no_trade"] is True
    assert decision.metadata["execution_authority"] == "none"
    assert repo.risk_decisions[0]["decision_id"] == decision.decision_id


def test_candidate_risk_precheck_uses_fresh_decision_market_timestamps():
    repo = FakeEngineHardeningRepo()
    hyperliquid = FakeHyperliquidEngine()
    settings = Settings(
        environment="test",
        engine_shadow_enabled=True,
        engine_paper_enabled=False,
        engine_min_net_ev_bps=-100,
    )
    service = InstitutionalEngineService(settings=settings, repository=repo, hyperliquid=hyperliquid, risk_gateway=RiskGateway(settings=settings, repository=repo))
    candidate = AlphaCandidate(
        candidate_id="cand_fresh_ts",
        strategy_id="microstructure_ofi_v2",
        strategy_version="test",
        strategy_family="microstructure_orderflow",
        asset="BTC",
        asset_class="crypto",
        venue="hyperliquid",
        side="long",
        horizon="5m",
        proposed_entry=104.0,
        stop=102.0,
        targets=[108.0],
        thesis="Fresh timestamp regression candidate.",
        invalidation_conditions=["Order flow reverses"],
        feature_snapshot_id="fs_test",
        regime_snapshot_id="reg_test",
        raw_alpha_score=70.0,
        confidence=0.7,
        created_at_ms=10_000,
        expires_at_ms=70_000,
    )
    ev = EVEstimate(
        estimate_id="ev_fresh_ts",
        candidate_id=candidate.candidate_id,
        model_version_id="deterministic_fallback_v1",
        p_target=0.5,
        p_stop=0.2,
        p_timeout=0.3,
        expected_favorable_bps=20,
        expected_adverse_bps=10,
        expected_holding_ms=60_000,
        expected_fee_bps=0,
        expected_spread_cost_bps=0,
        expected_slippage_bps=0,
        expected_market_impact_bps=0,
        expected_funding_cost_bps=0,
        tail_loss_bps=10,
        net_ev_bps=8,
        risk_adjusted_utility=8,
        uncertainty=0.1,
        calibration_bucket="test",
        created_at_ms=10_000,
    )

    async def run():
        return await service._candidate_risk_precheck(candidate, ev, snapshot_features={"spread_bps": 3.0}, timestamp_ms=10_000)

    decision = anyio.run(run)
    violation_codes = {item["code"] for item in decision.violations}

    assert decision.allowed is True
    assert violation_codes.isdisjoint({"expired_intent", "stale_price", "stale_orderbook"})
    assert decision.market_snapshot["freshness_source"] == "hyperliquid_l2_book"
    assert decision.market_snapshot["last_orderbook_at_ms"] > 10_000
    assert hyperliquid.l2_calls == 1
