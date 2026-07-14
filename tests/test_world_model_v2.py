from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.db.models import Base
from hyperliquid_trading_agent.app.engine.feature_store import FeatureStore
from hyperliquid_trading_agent.app.world_model.routes import register_world_model_routes
from hyperliquid_trading_agent.app.world_model.schemas import PredictionMarketSignal
from hyperliquid_trading_agent.app.world_model.streams import PolymarketSubscription, _polymarket_ws_signals
from hyperliquid_trading_agent.app.world_model.v2_reducer import (
    canonical_hypothesis,
    compute_macro_states,
    conditional_prediction_impacts,
    map_prediction_market,
)
from hyperliquid_trading_agent.app.world_model.v2_schemas import (
    MacroObservationV2,
    PredictionQuoteV2,
)
from hyperliquid_trading_agent.app.world_model.v2_service import WorldModelV2Service


def _observation(index: int, *, available_at_ms: int | None = None) -> MacroObservationV2:
    return MacroObservationV2(
        observation_id=f"cpi-{index}", series_id="CUSR0000SA0", factor_id="inflation",
        period=f"2025-{index + 1:02d}", value=100.0 + index, units="index", frequency="monthly",
        vintage="cutover", event_at_ms=1_000 + index, available_at_ms=available_at_ms or 2_000 + index,
        source="bls",
    )


def test_v2_tables_are_additive_and_leave_v1_and_paper_tables_present() -> None:
    tables = set(Base.metadata.tables)
    assert {"world_events", "market_beliefs", "prediction_market_signals"} <= tables
    assert {"prediction_market_positions", "prediction_market_fills"} <= tables
    assert {
        "world_model_v2_evidence", "world_model_v2_macro_observations", "world_model_v2_prediction_quotes",
        "world_model_v2_hypotheses", "world_model_v2_asset_impacts", "world_model_v2_snapshots",
    } <= tables


def test_macro_state_never_uses_observations_not_yet_available() -> None:
    observations = [_observation(index) for index in range(6)]
    observations.append(_observation(9, available_at_ms=99_000))
    states = compute_macro_states(observations, as_of_ms=10_000)
    inflation = next(item for item in states if item.factor_id == "inflation")
    assert "cpi-9" not in inflation.source_observation_ids
    assert inflation.freshness_ms == 10_000 - 2_005
    assert inflation.coverage == pytest.approx(0.25)


def test_surprise_requires_a_separately_sourced_forecast() -> None:
    with pytest.raises(ValidationError):
        MacroObservationV2(**{**_observation(0).model_dump(), "surprise": 1.0})


def test_prediction_relevance_rejects_sports_and_maps_macro() -> None:
    sports = map_prediction_market(venue="polymarket", market_id="sports", question="Will Spain win the 2026 FIFA World Cup?", liquidity_usd=1_000_000)
    macro = map_prediction_market(venue="polymarket", market_id="fed", question="Will the Fed cut interest rates in September?", liquidity_usd=1_000_000)
    assert sports.admission_status == "rejected"
    assert macro.admission_status == "admitted"
    assert "policy_stance" in macro.factor_ids


def test_binary_market_produces_one_canonical_yes_hypothesis() -> None:
    market = map_prediction_market(venue="polymarket", market_id="fed", question="Will the Fed cut interest rates?", liquidity_usd=100_000)
    quotes = [
        PredictionQuoteV2(quote_key="p:fed:yes", market_key=market.market_key, venue="polymarket", market_id="fed", outcome_id="yes", outcome_name="Yes", probability=0.4, best_bid=0.39, best_ask=0.41, spread=0.02, provider_at_ms=10_000, observed_at_ms=10_000),
        PredictionQuoteV2(quote_key="p:fed:no", market_key=market.market_key, venue="polymarket", market_id="fed", outcome_id="no", outcome_name="No", probability=0.6, best_bid=0.59, best_ask=0.61, spread=0.02, provider_at_ms=10_000, observed_at_ms=10_000),
    ]
    hypothesis = canonical_hypothesis(market, quotes, now_ms=11_000)
    assert hypothesis is not None
    assert hypothesis.yes_probability == pytest.approx(0.4)
    assert hypothesis.outcome_probabilities == {}
    assert not hasattr(hypothesis, "direction")


def test_prediction_asset_effect_is_conditional_on_yes_scenario_semantics() -> None:
    market = map_prediction_market(venue="polymarket", market_id="fed-cut", question="Will the Fed cut interest rates?", liquidity_usd=100_000)
    quotes = [
        PredictionQuoteV2(quote_key="p:cut:yes", market_key=market.market_key, venue="polymarket", market_id="fed-cut", outcome_id="yes", outcome_name="Yes", probability=0.7, best_bid=0.69, best_ask=0.71, spread=0.02, provider_at_ms=10_000, observed_at_ms=10_000),
        PredictionQuoteV2(quote_key="p:cut:no", market_key=market.market_key, venue="polymarket", market_id="fed-cut", outcome_id="no", outcome_name="No", probability=0.3, best_bid=0.29, best_ask=0.31, spread=0.02, provider_at_ms=10_000, observed_at_ms=10_000),
    ]
    hypothesis = canonical_hypothesis(market, quotes, now_ms=11_000)
    assert hypothesis is not None
    impacts = conditional_prediction_impacts(hypothesis)
    btc_policy = next(item for item in impacts if item.instrument_id == "BTC" and item.factor_id == "policy_stance")
    dxy_policy = next(item for item in impacts if item.instrument_id == "DXY" and item.factor_id == "policy_stance")
    assert btc_policy.mode == "conditional" and btc_policy.direction == "supportive"
    assert dxy_policy.direction == "adverse"


def test_current_polymarket_price_changes_and_book_contracts() -> None:
    subscriptions = {
        "yes-token": PolymarketSubscription(asset_id="yes-token", market_id="fed", question="Will the Fed cut rates?", outcome_name="Yes", symbols=["BTC"], topics=["macro"], liquidity_usd=100_000),
    }
    changed = _polymarket_ws_signals({"event_type": "price_change", "timestamp": "10000", "price_changes": [{"asset_id": "yes-token", "price": "0.42", "best_bid": "0.41", "best_ask": "0.43"}]}, by_asset=subscriptions, now=1)
    book = _polymarket_ws_signals({"event_type": "book", "asset_id": "yes-token", "timestamp": "10001", "bids": [{"price": "0.40"}, {"price": "0.41"}], "asks": [{"price": "0.44"}, {"price": "0.43"}]}, by_asset=subscriptions, now=1)
    assert changed[0].implied_probability == pytest.approx(0.42)
    assert changed[0].as_of_ms == 10_000_000
    assert book[0].best_bid == pytest.approx(0.41)
    assert book[0].best_ask == pytest.approx(0.43)


@pytest.mark.asyncio
async def test_service_never_creates_yes_no_duplicate_beliefs() -> None:
    settings = Settings(environment="test", world_model_v2_enabled=True)
    service = WorldModelV2Service(settings=settings)
    timestamp = int(__import__("time").time() * 1000)
    for outcome, probability, bid, ask in (("Yes", 0.4, 0.39, 0.41), ("No", 0.6, 0.59, 0.61)):
        await service.observe_prediction_market_signal(PredictionMarketSignal(
            signal_id=f"fed-{outcome}", venue="polymarket", market_id="fed", question="Will the Fed cut interest rates?",
            outcome_id=outcome.lower(), outcome_name=outcome, implied_probability=probability,
            best_bid=bid, best_ask=ask, liquidity_usd=100_000, status="open", as_of_ms=timestamp, confidence=0.9,
        ))
    assert len(service.hypotheses) == 1
    assert next(iter(service.hypotheses.values())).yes_probability == pytest.approx(0.4)
    await service.observe_prediction_market_signal(PredictionMarketSignal(
        signal_id="fed-settled", venue="polymarket", market_id="fed", question="Will the Fed cut interest rates?",
        outcome_id="yes", outcome_name="Yes", implied_probability=1.0, best_bid=1.0, best_ask=1.0,
        liquidity_usd=100_000, status="settled", as_of_ms=timestamp + 1, confidence=1.0,
    ))
    assert service.hypotheses == {}


@pytest.mark.asyncio
async def test_shadow_features_are_persisted_but_never_enter_active_latest() -> None:
    class Repo:
        def __init__(self) -> None:
            self.items: list[dict] = []

        async def record_feature_value(self, item: dict) -> None:
            self.items.append(item)

    repo = Repo()
    store = FeatureStore(repo)
    snapshot = SimpleNamespace(model_dump=lambda **_: {
        "snapshot_id": "wm2-1", "as_of_ms": 10_000,
        "asset_impacts": [{"impact_id": "i1", "instrument_id": "BTC", "factor_id": "liquidity", "horizon": "swing", "direction": "supportive", "mode": "current", "strength": 0.7, "mapping_version": "v1"}],
        "forecasts": [], "metadata": {"shadow_only": True, "execution_authority": "none"},
    })
    features = await store.features_for_world_model_v2_shadow(asset="BTC", snapshot=snapshot)
    assert features and repo.items
    assert store.snapshot(asset="BTC").features == {}
    assert all(item["metadata"]["shadow_only"] for item in repo.items)


def test_v2_api_cutover_exposes_typed_views_without_raw_quote_beliefs() -> None:
    settings = Settings(environment="test", world_model_v2_enabled=True)
    service = WorldModelV2Service(settings=settings)
    app = FastAPI()
    app.state.world_model_service = service
    register_world_model_routes(app, settings, lambda *_: None)
    with TestClient(app) as client:
        status = client.get("/world-model/status").json()
        beliefs = client.get("/world-model/beliefs").json()["items"]
        macro = client.get("/world-model/macro-state").json()
        dashboard = client.get("/world-model/dashboard/data").json()
        dashboard_html = client.get("/world-model/dashboard").text
        dashboard_js = client.get("/world-model/dashboard/app.js")
    assert status["version"] == 2 and status["shadow_only"] is True
    assert all(item["assertion_type"] in {"macro_state", "asset_impact"} for item in beliefs)
    assert macro["count"] == len(service.states)
    assert "macro_state" in dashboard and "asset_impacts" in dashboard and "quality" in dashboard
    assert '<script src="/world-model/dashboard/app.js" defer></script>' in dashboard_html
    assert "function render(data)" in dashboard_js.text
    assert "function render(data)" not in dashboard_html
