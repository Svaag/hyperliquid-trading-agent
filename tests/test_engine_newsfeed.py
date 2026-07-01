from __future__ import annotations

import anyio

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.alpha.wave1a import (
    FundingCarryStrategy,
    LiquidationCascadeStrategy,
    LiquidationMeanRevertStrategy,
    MicrostructureOFIV2Strategy,
)
from hyperliquid_trading_agent.app.engine.alpha.wave1c import NewsImpulseStrategy
from hyperliquid_trading_agent.app.engine.event_ledger import EventLedger
from hyperliquid_trading_agent.app.engine.feature_store import FeatureStore
from hyperliquid_trading_agent.app.engine.newswire_bridge import EngineNewsConsumer, newswire_event_to_engine_event
from hyperliquid_trading_agent.app.engine.regime import RegimeEngine
from hyperliquid_trading_agent.app.engine.schemas import FeatureValue, RegimeVector
from hyperliquid_trading_agent.app.engine.strategy_selector import ConservativeStrategySelector
from hyperliquid_trading_agent.app.newswire.bus import InProcessNewswireBus
from hyperliquid_trading_agent.app.newswire.schemas import NewswireEvent


class _EngineHarness:
    def __init__(self) -> None:
        self.ledger = EventLedger()
        self.feature_store = FeatureStore()


def _news_event(**overrides) -> NewswireEvent:
    data = {
        "event_id": "nw_test_btc",
        "source": "coindesk",
        "provider": "coindesk",
        "transport": "rss",
        "received_at_ms": 1_000,
        "published_at_ms": 900,
        "headline": "BTC ETF inflow surge",
        "body": "Large inflows support crypto risk appetite.",
        "symbols": ["BTC"],
        "asset_class": "crypto",
        "event_type": "headline",
        "urgency": "normal",
        "importance_score": 80.0,
        "sentiment": "bullish",
        "freshness": "breaking",
        "confidence": 0.9,
        "source_score": 0.8,
    }
    data.update(overrides)
    return NewswireEvent(**data)


def _feature(name: str, value: float, *, ts: int, asset: str = "BTC") -> FeatureValue:
    return FeatureValue(
        feature_id=f"feat_{asset}_{name}_{ts}_{value}",
        asset=asset,
        feature_group="test",
        feature_name=name,
        value={name: value},
        scalar_value=value,
        received_ts_ms=ts,
        computed_ts_ms=ts,
        source="test",
        version="test_v1",
        metadata={"newswire_event_id": f"nw_{name}_{ts}"} if name in {"catalyst_pressure", "event_risk_pressure"} else {},
    )


def _core_features(*, as_of_ms: int, asset: str = "BTC") -> list[FeatureValue]:
    return [
        *[_feature("mid", value, ts=as_of_ms - (5 - idx) * 60_000, asset=asset) for idx, value in enumerate([100, 101, 102, 103, 104])],
        _feature("spread_bps", 3.0, ts=as_of_ms, asset=asset),
        _feature("top_depth_usd", 500_000, ts=as_of_ms, asset=asset),
        _feature("top_imbalance", 0.35, ts=as_of_ms, asset=asset),
        *[_feature("funding_hourly", value, ts=as_of_ms - (5 - idx) * 60_000, asset=asset) for idx, value in enumerate([0, 0, 0, 0, 0])],
        *[_feature("open_interest", value, ts=as_of_ms - (5 - idx) * 60_000, asset=asset) for idx, value in enumerate([100, 101, 102, 103, 104])],
    ]


def _regime(*, news_risk_tier: str, news_pressure: float) -> RegimeVector:
    return RegimeVector(
        regime_snapshot_id=f"reg_{news_risk_tier}",
        primary_asset="BTC",
        created_at_ms=1_000,
        as_of_ms=1_000,
        trend_state="range",
        trend_confidence=0.5,
        realized_vol_percentile=0.4,
        liquidity_state="deep",
        spread_state="tight",
        volatility_state="normal",
        funding_state="neutral",
        oi_state="flat",
        liquidation_state="long_flush",
        orderflow_state="buy_pressure",
        news_state="catalyst" if news_risk_tier != "no_event" else "no_event",
        correlation_state="normal",
        session_state="us",
        feature_coverage_pct=100.0,
        regime_label=f"liquidation=long_flush|orderflow=buy_pressure|funding=neutral|news={news_risk_tier}",
        news_catalyst_pressure=news_pressure,
        regime_stability_score=0.7,
        derived_labels={"news_risk_tier": news_risk_tier},
    )


def test_engine_news_consumer_runs_without_process_owned_news_ingestion() -> None:
    async def run():
        settings = Settings(environment="test", newswire_enabled=False, engine_enabled=True, engine_newsfeed_enabled=True, engine_news_min_importance=35, engine_news_min_source_score=0.4, _env_file=None)
        bus = InProcessNewswireBus()
        engine = _EngineHarness()
        consumer = EngineNewsConsumer(settings=settings, bus=bus, engine_service=engine)
        await consumer.start()
        await bus.publish(_news_event(importance_score=35.0))
        await consumer.stop()
        return consumer.status(), await engine.ledger.list(event_type="newswire"), {feature.feature_name: feature for feature in await engine.feature_store.latest(asset="BTC", limit=10)}

    status, events, features = anyio.run(run)

    assert status["recorded_events"] == 1
    assert events[0].event_id == "evt_nw_test_btc"
    assert "catalyst_pressure" in features
    assert "event_risk_pressure" in features
    assert "source_consensus_score" in features


def test_newswire_consumer_records_engine_event_and_derives_news_features() -> None:
    async def run():
        settings = Settings(environment="test", newswire_enabled=True, engine_enabled=True, engine_newsfeed_enabled=True, engine_news_min_importance=35, engine_news_min_source_score=0.4)
        bus = InProcessNewswireBus()
        engine = _EngineHarness()
        consumer = EngineNewsConsumer(settings=settings, bus=bus, engine_service=engine)
        await consumer.start()
        await bus.publish(_news_event())
        await consumer.stop()
        events = await engine.ledger.list(event_type="newswire")
        features = await engine.feature_store.latest(asset="BTC", limit=10)
        return consumer.status(), events, {feature.feature_name: feature for feature in features}

    status, events, features = anyio.run(run)

    assert status["recorded_events"] == 1
    assert events[0].event_id == "evt_nw_test_btc"
    assert "catalyst_pressure" in features
    assert "event_risk_pressure" in features
    assert features["catalyst_pressure"].scalar_value is not None
    assert features["catalyst_pressure"].scalar_value > 0
    assert features["event_risk_pressure"].scalar_value == features["catalyst_pressure"].scalar_value
    assert features["event_risk_pressure"].metadata["newswire_event_id"] == "nw_test_btc"


def test_engine_news_consumer_filters_below_importance_threshold() -> None:
    async def run():
        settings = Settings(environment="test", newswire_enabled=False, engine_enabled=True, engine_newsfeed_enabled=True, engine_news_min_importance=35, engine_news_min_source_score=0.4, _env_file=None)
        bus = InProcessNewswireBus()
        engine = _EngineHarness()
        consumer = EngineNewsConsumer(settings=settings, bus=bus, engine_service=engine)
        await consumer.start()
        await bus.publish(_news_event(event_id="nw_low", importance_score=34.9))
        await consumer.stop()
        return consumer.status(), await engine.ledger.list(event_type="newswire")

    status, events = anyio.run(run)

    assert status["received_events"] == 0
    assert events == []


def test_engine_news_consumer_records_event_but_skips_features_for_low_source_score() -> None:
    async def run():
        settings = Settings(environment="test", newswire_enabled=False, engine_enabled=True, engine_newsfeed_enabled=True, engine_news_min_source_score=0.4, _env_file=None)
        bus = InProcessNewswireBus()
        engine = _EngineHarness()
        consumer = EngineNewsConsumer(settings=settings, bus=bus, engine_service=engine)
        await consumer.start()
        await bus.publish(_news_event(event_id="nw_low_source", importance_score=90.0, source_score=0.2))
        await consumer.stop()
        return consumer.status(), await engine.ledger.list(event_type="newswire"), await engine.feature_store.latest(asset="BTC", limit=10)

    status, events, features = anyio.run(run)

    assert len(events) == 1
    assert status["skip_reasons"]["source_score_below_minimum"] == 1
    assert features == []


def test_macro_news_proxies_to_all_core_symbols_by_default() -> None:
    settings = Settings(autonomy_core_universe="BTC,ETH,HYPE", engine_news_macro_min_importance=60)
    event = _news_event(event_id="nw_macro", source="federal_reserve", provider="federal_reserve", symbols=[], asset_class="macro", event_type="macro", headline="FOMC cuts rates", importance_score=85)

    normalized = newswire_event_to_engine_event(event, settings=settings)

    assert normalized is not None
    assert normalized.symbols == ["BTC", "ETH", "HYPE"]


def test_unknown_sentiment_creates_event_risk_but_not_directional_catalyst() -> None:
    async def run():
        settings = Settings(environment="test", newswire_enabled=True, engine_enabled=True, engine_newsfeed_enabled=True, engine_news_min_source_score=0.4)
        bus = InProcessNewswireBus()
        engine = _EngineHarness()
        consumer = EngineNewsConsumer(settings=settings, bus=bus, engine_service=engine)
        await consumer.start()
        await bus.publish(_news_event(event_id="nw_unknown", sentiment="unknown", importance_score=90, confidence=0.9, source_score=0.8))
        await consumer.stop()
        return {feature.feature_name: feature for feature in await engine.feature_store.latest(asset="BTC", limit=10)}

    features = anyio.run(run)

    assert features["catalyst_pressure"].scalar_value == 0.0
    assert features["event_risk_pressure"].scalar_value is not None
    assert features["event_risk_pressure"].scalar_value > 0.0


def test_regime_uses_recent_news_and_expires_stale_news() -> None:
    as_of_ms = 10_000_000
    recent = [*_core_features(as_of_ms=as_of_ms), _feature("catalyst_pressure", 0.60, ts=as_of_ms), _feature("event_risk_pressure", 0.60, ts=as_of_ms)]
    stale_ts = as_of_ms - 61 * 60_000
    stale = [*_core_features(as_of_ms=as_of_ms), _feature("catalyst_pressure", 0.90, ts=stale_ts), _feature("event_risk_pressure", 0.90, ts=stale_ts)]

    recent_regime = RegimeEngine(news_catalyst_ttl_ms=60 * 60_000).compute(recent, primary_asset="BTC", as_of_ms=as_of_ms)
    stale_regime = RegimeEngine(news_catalyst_ttl_ms=60 * 60_000).compute(stale, primary_asset="BTC", as_of_ms=as_of_ms)

    assert recent_regime.news_state == "catalyst"
    assert recent_regime.derived_labels["news_risk_tier"] == "event_risk"
    assert stale_regime.news_state == "no_event"
    assert stale_regime.derived_labels["news_risk_tier"] == "no_event"


def test_conservative_selector_suppresses_reversion_during_event_risk() -> None:
    selector = ConservativeStrategySelector()
    selection = selector.select([LiquidationCascadeStrategy(), LiquidationMeanRevertStrategy()], _regime(news_risk_tier="event_risk", news_pressure=0.6))

    assert [strategy.strategy_id for strategy in selection.strategies] == ["liquidation_cascade_v1"]
    assert any(item["strategy_id"] == "liquidation_mean_revert_v1" and item["reason"] == "news_event_risk_suppression" for item in selection.skipped)


def test_event_shock_suppresses_microstructure_and_funding() -> None:
    selector = ConservativeStrategySelector()
    selection = selector.select([MicrostructureOFIV2Strategy(), FundingCarryStrategy(), LiquidationCascadeStrategy()], _regime(news_risk_tier="event_shock", news_pressure=0.8))

    assert [strategy.strategy_id for strategy in selection.strategies] == ["liquidation_cascade_v1"]
    skipped = {item["strategy_id"]: item["reason"] for item in selection.skipped}
    assert skipped["microstructure_ofi_v2"] == "news_event_shock_suppression"
    assert skipped["funding_carry_v1"] == "news_event_shock_suppression"


def test_disabled_news_strategy_remains_disabled_under_catalyst_regime() -> None:
    strategy = NewsImpulseStrategy()
    strategy.spec = strategy.spec.model_copy(update={"enabled": False})

    selection = ConservativeStrategySelector().select([strategy], _regime(news_risk_tier="catalyst", news_pressure=0.4))

    assert selection.strategies == []
    assert selection.skipped[0]["reason"] == "strategy_disabled"
