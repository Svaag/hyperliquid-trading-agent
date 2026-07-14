from __future__ import annotations

from decimal import Decimal

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.feature_store import FeatureStore, derive_world_model_features
from hyperliquid_trading_agent.app.hip4.schemas import NormalizedOutcomeBook, PriceLevel
from hyperliquid_trading_agent.app.main import create_app
from hyperliquid_trading_agent.app.newswire.bus import InProcessNewswireBus
from hyperliquid_trading_agent.app.newswire.consumers.agent_feed import AgentNewsConsumer
from hyperliquid_trading_agent.app.newswire.normalize import normalize
from hyperliquid_trading_agent.app.newswire.schemas import RawNewsItem
from hyperliquid_trading_agent.app.world_model.adapters import PolymarketAdapter, _kalshi_signal, _polymarket_signals
from hyperliquid_trading_agent.app.world_model.routes import register_world_model_routes
from hyperliquid_trading_agent.app.world_model.service import WorldModelService, prediction_signal_from_hip4_book
from hyperliquid_trading_agent.app.world_model.streams import (
    WorldModelStreamAdapter,
    WorldModelStreamService,
    _polymarket_subscriptions,
    _polymarket_ws_signals,
)


def _raw_news(**kwargs) -> RawNewsItem:
    data = {"source": "coindesk", "transport": "rss", "headline": "BTC ETF inflow surge", "symbols": ["BTC"]}
    data.update(kwargs)
    return RawNewsItem(**data)


def test_world_model_builds_beliefs_and_tracks_contradictions():
    async def run():
        service = WorldModelService(settings=Settings())
        bullish = normalize(_raw_news(external_id="bull", headline="BTC ETF inflow surge", symbols=["BTC"]), symbols_universe=["BTC"], received_at_ms=1_000)
        bearish = normalize(_raw_news(external_id="bear", headline="BTC regulatory pressure intensifies", symbols=["BTC"]), symbols_universe=["BTC"], received_at_ms=2_000)
        assert bullish is not None and bearish is not None
        bullish.sentiment = "bullish"
        bullish.importance_score = 80
        bullish.confidence = 0.8
        bearish.sentiment = "bearish"
        bearish.importance_score = 75
        bearish.confidence = 0.75
        await service.observe_newswire_event(bullish)
        await service.observe_newswire_event(bearish)
        snapshot = service.snapshot(symbols=["BTC"])
        return snapshot, service.wiki_block(symbols=["BTC"])

    snapshot, block = anyio.run(run)

    assert len(snapshot.top_beliefs) == 2
    assert any(item.contradicts_belief_ids for item in snapshot.top_beliefs)
    assert snapshot.narrative_clusters[0].conflict_score > 0
    assert "Market world model" in block


def test_hip4_book_becomes_prediction_market_signal():
    settings = Settings(hip4_scan_max_book_staleness_ms=60_000, autonomy_core_universe="BTC,ETH,HYPE")
    book = NormalizedOutcomeBook(
        coin="#320",
        outcome_id=32,
        side=0,
        bids=[PriceLevel(px=Decimal("0.61"), sz=Decimal("100"))],
        asks=[PriceLevel(px=Decimal("0.64"), sz=Decimal("100"))],
        as_of_ms=1_000,
        source="fixture",
    )

    signal = prediction_signal_from_hip4_book(book, question={"question_id": 3, "name": "Will BTC close above 100k?"}, outcome={"name": "YES"}, settings=settings, now=2_000)

    assert signal is not None
    assert signal.venue == "hip4"
    assert signal.implied_probability == 0.625
    assert signal.symbols == ["BTC"]
    assert signal.status == "open"


def test_world_model_snapshot_derives_engine_features():
    async def run():
        service = WorldModelService(settings=Settings())
        event = normalize(_raw_news(external_id="wmfeat", headline="BTC breaks higher on ETF inflows", symbols=["BTC"]), symbols_universe=["BTC"], received_at_ms=1_000)
        assert event is not None
        event.sentiment = "bullish"
        event.importance_score = 90
        event.confidence = 0.85
        await service.observe_newswire_event(event)
        store = FeatureStore()
        features = await store.features_for_world_model_snapshot(asset="BTC", snapshot=service.snapshot(symbols=["BTC"]))
        return {feature.feature_name: feature for feature in features}

    features = anyio.run(run)

    assert "narrative_pressure" in features
    assert "belief_salience" in features
    assert features["narrative_pressure"].scalar_value is not None
    assert features["narrative_pressure"].scalar_value > 0
    assert features["narrative_pressure"].metadata["execution_authority"] == "none"


def test_newswire_consumer_feeds_world_model_without_autonomy():
    async def run():
        settings = Settings(environment="test", newswire_enabled=True, newswire_agent_min_importance=0)
        bus = InProcessNewswireBus()
        service = WorldModelService(settings=settings)
        consumer = AgentNewsConsumer(settings=settings, bus=bus, autonomy_service=None, world_model_service=service)
        await consumer.start()
        event = normalize(_raw_news(external_id="wm-only", headline="BTC ETF inflow surge", symbols=["BTC"]), symbols_universe=["BTC"], received_at_ms=1_000)
        assert event is not None
        event.sentiment = "bullish"
        event.importance_score = 85
        event.confidence = 0.8
        await bus.publish(event)
        await consumer.stop()
        return service.snapshot(symbols=["BTC"])

    snapshot = anyio.run(run)

    assert snapshot.top_beliefs
    assert snapshot.top_beliefs[0].symbols == ["BTC"]


def test_world_model_feature_boundary_forbids_execution_authority():
    with pytest.raises(ValueError, match="advisory-only"):
        derive_world_model_features(
            asset="BTC",
            snapshot={
                "snapshot_id": "wm_bad",
                "as_of_ms": 1_000,
                "metadata": {"execution_authority": "orders"},
                "top_beliefs": [],
                "narrative_clusters": [],
                "prediction_market_signals": [],
            },
        )

    with pytest.raises(ValueError, match="exchange_actions"):
        derive_world_model_features(
            asset="BTC",
            snapshot={
                "snapshot_id": "wm_bad_actions",
                "as_of_ms": 1_000,
                "metadata": {"execution_authority": "none"},
                "exchange_actions": [{"side": "buy"}],
            },
        )


def test_world_model_dashboard_routes_render_graph_tree():
    async def seed() -> WorldModelService:
        settings = Settings(environment="test", hip4_scan_max_book_staleness_ms=60_000, autonomy_core_universe="BTC,ETH,HYPE")
        service = WorldModelService(settings=settings)
        event = normalize(
            _raw_news(external_id="dash", headline="BTC rallies as ETF flows accelerate", symbols=["BTC"]),
            symbols_universe=["BTC"],
            received_at_ms=1_000,
        )
        assert event is not None
        event.sentiment = "bullish"
        event.importance_score = 85
        event.confidence = 0.82
        await service.observe_newswire_event(event)
        book = NormalizedOutcomeBook(
            coin="#320",
            outcome_id=32,
            side=0,
            bids=[PriceLevel(px=Decimal("0.61"), sz=Decimal("100"))],
            asks=[PriceLevel(px=Decimal("0.64"), sz=Decimal("100"))],
            as_of_ms=1_000,
            source="fixture",
        )
        signal = prediction_signal_from_hip4_book(book, question={"question_id": 3, "name": "Will BTC close above 100k?"}, outcome={"name": "YES"}, settings=settings, now=2_000)
        assert signal is not None
        await service.observe_prediction_market_signal(signal)
        return service

    app = FastAPI()
    app.state.world_model_service = anyio.run(seed)
    register_world_model_routes(app, Settings(environment="test"), lambda settings, authorization: None)
    client = TestClient(app)

    html = client.get("/world-model/dashboard")
    data = client.get("/world-model/dashboard/data", params={"symbol": "BTC", "limit": 50})

    assert html.status_code == 200
    assert "World Model Dashboard" in html.text
    assert "world-model-graph" in html.text
    assert "timeSlider" in html.text
    assert "Annotation Queue" in html.text
    assert data.status_code == 200
    body = data.json()
    assert body["summary"]["beliefs"] >= 2
    assert any(node["type"] == "belief" for node in body["graph"]["nodes"])
    assert any(node["type"] == "prediction_market" for node in body["graph"]["nodes"])
    assert any(edge["type"] == "evidence" for edge in body["graph"]["edges"])

    belief_id = body["beliefs"]["items"][0]["belief_id"]
    annotated = client.post(
        "/world-model/annotations",
        json={"target_type": "belief", "target_id": belief_id, "action": "pinned", "note": "watch", "actor_id": "operator"},
    )
    assert annotated.status_code == 200
    assert annotated.json()["item"]["metadata"]["execution_authority"] == "none"

    contradiction_view = client.get("/world-model/dashboard/data", params={"symbol": "BTC", "mode": "contradictions"}).json()
    assert contradiction_view["filters"]["mode"] == "contradictions"
    assert contradiction_view["summary"]["annotations"] == 1
    assert any((node.get("data") or {}).get("annotations") for node in contradiction_view["graph"]["nodes"])

    signal_id = body["prediction_markets"]["items"][0]["signal_id"]
    outcome = client.post(
        "/world-model/outcomes",
        json={"target_type": "prediction_signal", "target_id": signal_id, "outcome": "yes", "symbol": "BTC", "realized_value": 1.0},
    )
    assert outcome.status_code == 200
    calibration = client.get("/world-model/prediction-calibration", params={"signal_id": signal_id}).json()
    assert calibration["items"][0]["brier_score"] is not None

    nearest = client.get("/world-model/snapshots/nearest", params={"as_of_ms": 2_000, "symbol": "BTC"})
    replay = client.get("/world-model/replay", params={"start_ms": 0, "end_ms": 10_000, "symbol": "BTC"})
    assert nearest.status_code == 200
    assert replay.status_code == 200
    assert "events" in replay.json()


def test_world_model_dashboard_seed_and_time_travel_smoke():
    settings = Settings(environment="test", world_model_dev_seed_enabled=True)
    app = FastAPI()
    app.state.world_model_service = WorldModelService(settings=settings)
    register_world_model_routes(app, settings, lambda settings, authorization: None)
    client = TestClient(app)

    seeded = client.post("/world-model/dev/seed", json={"symbol": "BTC", "topic": "macro"})
    assert seeded.status_code == 200
    assert seeded.json()["execution_authority"] == "none"

    data = client.get("/world-model/dashboard/data", params={"symbol": "BTC", "topic": "macro", "mode": "prediction_consensus"})
    assert data.status_code == 200
    body = data.json()
    assert body["summary"]["events"] >= 2
    assert body["summary"]["prediction_market_signals"] >= 1
    assert body["summary"]["annotations"] >= 1
    assert body["summary"]["outcomes"] >= 2
    assert body["prediction_calibration"]["items"][0]["brier_score"] is not None

    as_of_ms = body["snapshot"]["as_of_ms"]
    historical = client.get("/world-model/dashboard/data", params={"symbol": "BTC", "topic": "macro", "as_of_ms": as_of_ms})
    replay = client.get("/world-model/replay", params={"start_ms": as_of_ms - 60_000, "end_ms": as_of_ms + 60_000, "symbol": "BTC", "topic": "macro"})
    assert historical.status_code == 200
    assert historical.json()["filters"]["as_of_ms"] == as_of_ms
    assert replay.status_code == 200
    assert replay.json()["snapshots"]


def test_world_model_lists_fall_back_to_reducer_when_repository_fails():
    class FailingRepository:
        enabled = True

        async def list_world_events(self, **kwargs):
            raise RuntimeError("db unavailable")

        async def list_market_beliefs(self, **kwargs):
            raise RuntimeError("db unavailable")

        async def upsert_world_model_snapshot(self, snapshot):
            raise RuntimeError("db unavailable")

    async def run():
        service = WorldModelService(settings=Settings(environment="test"), repository=FailingRepository())
        event = normalize(_raw_news(external_id="fallback", headline="BTC breaks higher", symbols=["BTC"]), symbols_universe=["BTC"], received_at_ms=1_000)
        assert event is not None
        event.sentiment = "bullish"
        event.importance_score = 80
        await service.observe_newswire_event(event)
        await service.persist_snapshot(service.snapshot(symbols=["BTC"]))
        events = await service.list_events(symbol="BTC")
        beliefs = await service.list_beliefs(symbol="BTC")
        await service.list_events(symbol="BTC")
        await service.list_beliefs(symbol="BTC")
        return service.status(), events, beliefs

    status, events, beliefs = anyio.run(run)

    assert status["error_count"] == 0
    assert status["repository_error_count"] == 1
    assert status["repository_available"] is False
    assert events[0]["symbols"] == ["BTC"]
    assert beliefs[0]["symbols"] == ["BTC"]


def test_world_model_live_adapter_normalizers_are_advisory_only():
    settings = Settings(environment="test", autonomy_core_universe="BTC,ETH,HYPE")
    poly = _polymarket_signals(
        {
            "id": "m1",
            "question": "Will Bitcoin close above 100k?",
            "outcomes": '["Yes","No"]',
            "outcomePrices": '["0.62","0.38"]',
            "liquidity": "10000",
            "bestBid": "0.60",
            "bestAsk": "0.64",
        },
        settings,
    )
    kalshi = _kalshi_signal({"ticker": "KXBTC", "title": "Will BTC close above 100k?", "yes_bid": 61, "yes_ask": 64}, settings)

    assert poly[0].venue == "polymarket"
    assert poly[0].metadata["execution_authority"] == "none"
    assert poly[0].best_bid == pytest.approx(0.60)
    assert poly[0].best_ask == pytest.approx(0.64)
    assert poly[1].best_bid == pytest.approx(0.36)
    assert poly[1].best_ask == pytest.approx(0.40)
    assert kalshi is not None
    assert kalshi.venue == "kalshi"
    assert kalshi.implied_probability == 0.625
    assert kalshi.metadata["paper_only"] is True
    assert poly[0].signal_id == _polymarket_signals(
        {
            "id": "m1",
            "question": "Will Bitcoin close above 100k?",
            "outcomes": '["Yes","No"]',
            "outcomePrices": '["0.63","0.37"]',
        },
        settings,
    )[0].signal_id
    assert "raw_market" in poly[0].metadata
    assert kalshi.signal_id == _kalshi_signal({"ticker": "KXBTC", "title": "Will BTC close above 100k?", "yes_bid": 62, "yes_ask": 65}, settings).signal_id


def test_world_model_adapter_cadence_and_probability_delta():
    async def run():
        settings = Settings(
            environment="test",
            world_model_adapters_enabled=True,
            world_model_polymarket_enabled=True,
            world_model_adapter_poll_interval_seconds=60,
        )
        service = WorldModelService(settings=settings)
        adapter = PolymarketAdapter(settings)
        calls = 0

        async def fake_poll(world_model_service):
            nonlocal calls
            calls += 1
            signal = _polymarket_signals(
                {
                    "id": "m1",
                    "question": "Will Bitcoin close above 100k?",
                    "outcomes": '["Yes","No"]',
                    "outcomePrices": '["0.62","0.38"]' if calls == 1 else '["0.67","0.33"]',
                },
                settings,
            )[0]
            if signal.signal_id in world_model_service.reducer.prediction_signals:
                previous = world_model_service.reducer.prediction_signals[signal.signal_id]
                signal = signal.model_copy(update={"probability_delta": signal.implied_probability - previous.implied_probability})
            await world_model_service.observe_prediction_market_signal(signal)
            return {"events": 0, "prediction_signals": 1, "duplicates_skipped": 0}

        adapter.poll = fake_poll  # type: ignore[method-assign]
        first = await adapter.run_poll(service)
        skipped = await adapter.run_poll(service)
        forced = await adapter.run_poll(service, force=True)
        signal = next(iter(service.reducer.prediction_signals.values()))
        return first, skipped, forced, signal, calls

    first, skipped, forced, signal, calls = anyio.run(run)

    assert first["counts"]["prediction_signals"] == 1
    assert skipped["skipped"] is True
    assert skipped["reason"] == "poll_interval"
    assert forced["counts"]["prediction_signals"] == 1
    assert calls == 2
    assert signal.probability_delta == pytest.approx(0.05)
    assert signal.metadata["execution_authority"] == "none"


def test_polymarket_ws_message_normalizes_advisory_signal():
    settings = Settings(environment="test", autonomy_core_universe="BTC,ETH,HYPE")
    subscriptions = _polymarket_subscriptions(
        {
            "id": "m1",
            "question": "Will Bitcoin close above 100k?",
            "outcomes": '["Yes","No"]',
            "clobTokenIds": '["asset_yes","asset_no"]',
            "liquidity": "10000",
            "volume": "50000",
        },
        settings,
    )
    by_asset = {item.asset_id: item for item in subscriptions}

    signals = _polymarket_ws_signals(
        {
            "event_type": "price_change",
            "changes": [{"asset_id": "asset_yes", "price": "0.66", "best_bid": "0.65", "best_ask": "0.67"}],
        },
        by_asset=by_asset,
        now=2_000,
    )

    assert len(signals) == 1
    assert signals[0].venue == "polymarket"
    assert signals[0].signal_id == _polymarket_signals(
        {
            "id": "m1",
            "question": "Will Bitcoin close above 100k?",
            "outcomes": '["Yes","No"]',
            "outcomePrices": '["0.66","0.34"]',
        },
        settings,
    )[0].signal_id
    assert signals[0].implied_probability == 0.66
    assert signals[0].best_bid == 0.65
    assert signals[0].best_ask == 0.67
    assert signals[0].metadata["execution_authority"] == "none"
    assert signals[0].metadata["adapter"] == "polymarket_ws"


def test_world_model_stream_service_status_tracks_fake_adapter():
    class FakeStreamAdapter(WorldModelStreamAdapter):
        name = "fake"

        @property
        def enabled(self) -> bool:
            return True

        async def run(self, service: WorldModelService) -> None:
            self.status.connected = True
            self.status.last_message_at_ms = 2_000
            self.status.subscribed_markets = 1
            self.status.subscriptions = ["asset_yes"]
            self.status.last_event = {"signal_id": "pm_fake"}
            while not self._stop.is_set():
                await anyio.sleep(0.01)

    async def run():
        settings = Settings(environment="test", world_model_streams_enabled=True)
        service = WorldModelService(settings=settings)
        streams = WorldModelStreamService(settings=settings, world_model_service=service, adapters=[FakeStreamAdapter(settings)])
        await streams.start()
        await anyio.sleep(0.02)
        status = streams.status()
        await streams.stop()
        return status

    status = anyio.run(run)

    assert status["running"] is True
    assert status["streams"][0]["connected"] is True
    assert status["streams"][0]["subscribed_markets"] == 1
    assert status["streams"][0]["execution_authority"] == "none"


def test_dashboard_only_readiness_ignores_full_runtime_flags():
    app = create_app(
        Settings(
            environment="test",
            runtime_profile="dashboard_only",
            position_tracking_enabled=True,
            autonomy_enabled=True,
            tradfi_enabled=True,
            engine_enabled=True,
        )
    )

    with TestClient(app) as client:
        body = client.get("/ready").json()

    assert body["checks"]["runtime_profile"] == "dashboard_only"
    assert "world_model_repository" in body["checks"]
    assert "position_tracking" not in body["checks"]
    assert "tradfi" not in body["checks"]


def test_world_model_live_readiness_ignores_trading_runtime_flags():
    app = create_app(
        Settings(
            environment="test",
            runtime_profile="world_model_live",
            newswire_enabled=False,
            world_model_streams_enabled=False,
            position_tracking_enabled=True,
            autonomy_enabled=True,
            tradfi_enabled=True,
            engine_enabled=True,
            hip4_enabled=True,
            hip4_scan_enabled=True,
            discord_bot_token="not-used",
        )
    )

    with TestClient(app) as client:
        body = client.get("/ready").json()

    assert body["checks"]["runtime_profile"] == "world_model_live"
    assert body["checks"]["discord_enabled"] is False
    assert body["checks"]["newswire"] == "disabled"
    assert body["checks"]["world_model_streams"] == "disabled"
    assert "world_model_repository" in body["checks"]
    assert "position_tracking" not in body["checks"]
    assert "autonomy" not in body["checks"]
    assert "tradfi" not in body["checks"]
