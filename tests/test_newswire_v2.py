from __future__ import annotations

import time
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import anyio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy import Column, MetaData, String, Table, create_engine, inspect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.db.models import (
    NewswireDeliveryRow,
    NewswireRiskStateRecord,
    NewswireRiskTransitionRecord,
    NewswireStoryRevisionRow,
    NewswireStoryRow,
)
from hyperliquid_trading_agent.app.db.repository import Repository
from hyperliquid_trading_agent.app.engine.alpha.news_event import NewsEventAlphaStrategy
from hyperliquid_trading_agent.app.engine.feature_store import FeatureStore
from hyperliquid_trading_agent.app.engine.news_risk import NewsRiskStateMachine
from hyperliquid_trading_agent.app.engine.regime import RegimeEngine
from hyperliquid_trading_agent.app.engine.schemas import FeatureSnapshot, RegimeVector, StrategyPermissions
from hyperliquid_trading_agent.app.engine.strategy_registry import create_default_strategy_registry
from hyperliquid_trading_agent.app.main import create_app
from hyperliquid_trading_agent.app.newswire.normalize import normalize
from hyperliquid_trading_agent.app.newswire.schemas import NewswireAssessment, NewswireEvent, RawNewsItem
from hyperliquid_trading_agent.app.newswire.service import NewswireService
from hyperliquid_trading_agent.app.newswire.watchlist import DynamicNewswireWatchSet, resolve_entities
from hyperliquid_trading_agent.app.world_model.service import WorldModelService


def _settings(**updates) -> Settings:
    values = {
        "environment": "test",
        "newswire_enabled": True,
        "newswire_model_classify_enabled": False,
        "_env_file": None,
        **updates,
    }
    return Settings(**values)


def _raw(**updates) -> RawNewsItem:
    data = {
        "source": "coindesk",
        "provider": "coindesk",
        "transport": "rss",
        "headline": "Bitcoin daily market recap",
    }
    data.update(updates)
    return RawNewsItem(**data)


def _assessment(*, story_id: str, engine_action: str = "risk_only") -> NewswireAssessment:
    now = int(time.time() * 1000)
    return NewswireAssessment(
        decision_id=f"decision_{story_id}",
        story_id=story_id,
        watch_priority="core",
        matched_symbols=["BTC"],
        relevance_score=100,
        impact_score=95,
        urgency_score=100,
        source_quality_score=100,
        novelty_score=100,
        priority_score=98,
        direction="unknown",
        direction_confidence=0,
        risk_bias=0,
        risk_severity=1,
        feed_action="breaking",
        engine_action=engine_action,  # type: ignore[arg-type]
        assessed_at_ms=now,
    )


def _risk_event(*, action: str = "created") -> NewswireEvent:
    now = int(time.time() * 1000)
    assessment = _assessment(story_id="nws_exchange_outage", engine_action="ignore" if action == "removed" else "risk_only")
    return NewswireEvent(
        event_id=f"nw_exchange_outage_{action}",
        schema_version=2,
        source="nasdaq_halts",
        provider="official",
        transport="rss",
        received_at_ms=now,
        published_at_ms=now,
        action=action,  # type: ignore[arg-type]
        headline="Bitcoin exchange trading halted after critical outage",
        symbols=["BTC"],
        asset_class="crypto",
        event_type="exchange_status",
        urgency="breaking",
        importance_score=98,
        sentiment="unknown",
        freshness="breaking",
        confidence=1,
        source_score=1,
        story_id="nws_exchange_outage",
        story_revision=2 if action == "removed" else 1,
        assessment=assessment,
        metadata={
            "story_id": "nws_exchange_outage",
            "story_sources": ["nasdaq_halts"],
            "newswire_policy_decision": {
                "newswire_action": assessment.feed_action,
                "engine_action": assessment.engine_action,
                "shadow_only": False,
            },
        },
    )


def test_watched_routine_story_is_routed_without_legacy_threshold() -> None:
    async def run():
        service = NewswireService(settings=_settings())
        seen: list[NewswireEvent] = []
        await service.bus.subscribe(lambda event: seen.append(event))
        raw_event = await service._ingest(_raw(external_id="watched-1"))
        return raw_event, seen, service.list_stories()

    raw_event, seen, stories = anyio.run(run)

    assert raw_event is not None
    assert raw_event.metadata["legacy_importance_score"] < 60
    assert raw_event.assessment is not None
    assert raw_event.assessment.watch_priority == "core"
    assert raw_event.assessment.feed_action == "standard"
    assert raw_event.assessment.reason_codes
    assert seen[0].story_id == stories[0].story_id


def test_same_story_clusters_independent_sources_into_revisions() -> None:
    async def run():
        service = NewswireService(settings=_settings())
        await service._ingest(_raw(source="coindesk", external_id="one", headline="Bitcoin ETF approved after regulator vote"))
        await service._ingest(
            _raw(
                source="cointelegraph",
                provider="cointelegraph",
                external_id="two",
                headline="Bitcoin ETF approved after regulator vote",
            )
        )
        return service.list_stories()

    stories = anyio.run(run)

    assert len(stories) == 1
    assert stories[0].revision == 2
    assert stories[0].independent_source_count == 2
    assert stories[0].metadata["last_update_type"] == "confirmed"
    assert len(stories[0].member_event_ids) == 2


def test_same_source_duplicate_is_audited_without_new_story_revision() -> None:
    async def run():
        service = NewswireService(settings=_settings())
        seen: list[NewswireEvent] = []
        await service.bus.subscribe(lambda event: seen.append(event))
        await service._ingest(_raw(external_id="duplicate-one"))
        duplicate = await service._ingest(_raw(external_id="duplicate-two"))
        return service, seen, duplicate

    service, seen, duplicate = anyio.run(run)

    assert duplicate is not None
    assert len(seen) == 1
    assert service.list_stories()[0].revision == 1
    assert service.status()["dropped_events_by_reason"]["story_duplicate"] == 1


def test_short_ticker_matching_is_boundary_and_case_safe() -> None:
    settings = _settings(newswire_watchlist="ONE,BTC")
    watch_set = DynamicNewswireWatchSet(settings).snapshot
    event = normalize(
        _raw(headline="A company takes one step toward a routine product launch"),
        symbols_universe=settings.newswire_symbols_universe,
        received_at_ms=int(time.time() * 1000),
    )
    assert event is not None

    match = resolve_entities(event, watch_set)

    assert "ONE" not in match.symbols


def test_news_risk_shock_is_shadow_observable_and_retraction_clears_it() -> None:
    async def run():
        settings = _settings(engine_news_risk_overlay_mode="shadow")
        store = FeatureStore()
        machine = NewsRiskStateMachine(settings)
        states = await machine.observe(_risk_event(), feature_store=store)
        features = await store.latest(asset="BTC", limit=100)
        shadow = RegimeEngine(news_risk_overlay_mode="shadow").compute(features, primary_asset="BTC")
        active = RegimeEngine(news_risk_overlay_mode="active").compute(features, primary_asset="BTC")
        cleared = await machine.retract(_risk_event(action="removed"), feature_store=store)
        return states, shadow, active, cleared

    states, shadow, active, cleared = anyio.run(run)

    assert states[0].mode == "shock"
    assert shadow.news_risk_mode == "neutral"
    assert shadow.metadata["observed_news_risk_mode"] == "shock"
    assert active.news_risk_mode == "shock"
    assert active.permissions.momentum_allowed is False
    assert cleared[0].mode == "neutral"
    assert cleared[0].transition_reason == "source_story_retracted"


def test_news_alpha_runtime_mode_is_independent_and_shadow_only_by_default() -> None:
    off = create_default_strategy_registry(news_event_alpha_mode="off")
    shadow = create_default_strategy_registry(news_event_alpha_mode="shadow")
    paper = create_default_strategy_registry(news_event_alpha_mode="paper")

    assert off.get("news_event_alpha_v2") is None
    assert shadow.get("news_event_alpha_v2") is not None
    assert shadow.require_spec("news_event_alpha_v2").metadata["activation_scope"] == "shadow_only"
    assert paper.get("news_event_alpha_v2") is not None
    assert paper.require_spec("news_event_alpha_v2").metadata.get("activation_scope") != "shadow_only"


def test_news_alpha_requires_fresh_story_and_market_confirmation() -> None:
    now = int(time.time() * 1000)
    strategy = NewsEventAlphaStrategy()
    strategy.configure(_settings(engine_news_alpha_mode="shadow"))
    regime = RegimeVector(
        regime_snapshot_id="reg_news",
        primary_asset="BTC",
        created_at_ms=now,
        as_of_ms=now,
        news_state="catalyst",
        news_catalyst_pressure=0.8,
        regime_stability_score=0.8,
        permissions=StrategyPermissions(news_event_allowed=True),
    )
    features = {
        "mid": 100.0,
        "mid_return_5m_bps": 30.0,
        "top_imbalance": 0.2,
        "spread_bps": 5.0,
        "news_story_impact": 0.9,
        "news_direction_confidence": 0.9,
        "news_source_quality": 0.95,
        "news_independent_source_count": 1.0,
        "news_story_context": {
            "story_id": "nws_directional",
            "story_member_event_ids": ["nw_1"],
            "direction_score": 1.0,
            "engine_action": "directional_feature",
            "received_at_ms": now - 60_000,
        },
    }
    fresh = FeatureSnapshot(snapshot_id="fs_fresh", asset="BTC", as_of_ms=now, features=features)
    unconfirmed = fresh.model_copy(
        update={"snapshot_id": "fs_unconfirmed", "features": {**features, "mid_return_5m_bps": 0.0}}
    )
    stale_context = {**features["news_story_context"], "received_at_ms": now - 31 * 60_000}
    stale = fresh.model_copy(
        update={"snapshot_id": "fs_stale", "features": {**features, "news_story_context": stale_context}}
    )

    candidates = strategy.generate(fresh, regime, timestamp_ms=now)

    assert len(candidates) == 1
    assert candidates[0].side == "long"
    assert candidates[0].metadata["news_alpha_mode"] == "shadow"
    assert strategy.generate(unconfirmed, regime, timestamp_ms=now) == []
    assert strategy.generate(stale, regime, timestamp_ms=now) == []


def test_world_model_supersedes_belief_when_story_is_retracted() -> None:
    async def run():
        service = WorldModelService(settings=_settings())
        created = _risk_event()
        beliefs = await service.observe_newswire_event(created)
        retracted = await service.observe_newswire_event(_risk_event(action="removed"))
        return service, beliefs, retracted

    service, beliefs, retracted = anyio.run(run)

    assert beliefs and beliefs[0].status == "active"
    assert retracted and retracted[0].status == "superseded"
    assert all(item.status != "active" for item in service.reducer.beliefs.values())


def test_repository_persists_story_revision_delivery_and_risk_state() -> None:
    async def run():
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        tables = (
            NewswireStoryRow,
            NewswireStoryRevisionRow,
            NewswireDeliveryRow,
            NewswireRiskStateRecord,
            NewswireRiskTransitionRecord,
        )
        async with engine.begin() as connection:
            for table in tables:
                await connection.run_sync(table.__table__.create)
        repo = Repository(async_sessionmaker(engine, expire_on_commit=False))

        service = NewswireService(settings=_settings())
        await service._ingest(_raw(external_id="persisted"))
        story = service.list_stories()[0]
        revision = service.story_clusterer.revision(story, "created")
        await repo.upsert_newswire_story(story.model_dump(mode="json"))
        await repo.record_newswire_story_revision(revision.model_dump(mode="json"))
        first_delivery = await repo.schedule_newswire_delivery(
            story_id=story.story_id,
            story_revision=story.revision,
            channel_id="999",
            mode="digest",
            scheduled_at_ms=1_000,
            payload={"event": story.to_event().model_dump(mode="json")},
        )
        duplicate_delivery = await repo.schedule_newswire_delivery(
            story_id=story.story_id,
            story_revision=story.revision,
            channel_id="999",
            mode="digest",
            scheduled_at_ms=1_000,
            payload={"event": story.to_event().model_dump(mode="json")},
        )
        await repo.upsert_newswire_risk_state(
            {
                "scope": "BTC",
                "mode": "risk_off",
                "signed_pressure": -0.7,
                "risk_pressure": 0.8,
                "confidence": 0.9,
                "evidence_story_ids": [story.story_id],
                "entered_at_ms": 1_000,
                "updated_at_ms": 1_000,
                "expires_at_ms": 2_000,
                "assessment_version": "newswire_assessment_v2.1",
                "transition_reason": "test",
            }
        )
        due = await repo.claim_due_newswire_deliveries(channel_id="999", now_ms=1_000)
        duplicate_claim = await repo.claim_due_newswire_deliveries(channel_id="999", now_ms=1_000)
        await repo.mark_newswire_deliveries_posted(
            [str(due[0]["delivery_id"])],
            message_id="msg-1",
            now_ms=1_100,
        )
        result = {
            "story": await repo.get_newswire_story(story.story_id),
            "revisions": await repo.list_newswire_story_revisions(story_id=story.story_id),
            "due": due,
            "duplicate_claim": duplicate_claim,
            "delivery_status": await repo.newswire_delivery_status("999"),
            "was_delivered": await repo.was_newswire_story_delivered(story.story_id, "999"),
            "risk": await repo.list_newswire_risk_states(scope="BTC"),
            "delivery_ids": (first_delivery, duplicate_delivery),
        }
        await engine.dispose()
        return result

    result = anyio.run(run)

    assert result["story"]["assessment"]["assessment_version"] == "newswire_assessment_v2.1"
    assert len(result["revisions"]) == 1
    assert len(result["due"]) == 1
    assert result["duplicate_claim"] == []
    assert result["delivery_ids"][0] == result["delivery_ids"][1]
    assert result["delivery_status"]["counts"] == {"posted": 1}
    assert result["was_delivered"] is True
    assert result["risk"][0]["mode"] == "risk_off"


def test_first_class_feed_and_story_api_use_canonical_story_view() -> None:
    settings = _settings(
        newswire_enabled=False,
        position_tracking_enabled=False,
        autonomy_enabled=False,
        database_url="sqlite+aiosqlite://",
    )
    app = create_app(settings)

    with TestClient(app) as client:
        service: NewswireService = app.state.newswire_service

        async def ingest() -> None:
            await service._ingest(_raw(external_id="api-story"))

        anyio.run(ingest)
        feed = client.get("/newswire/feed")
        story_id = feed.json()["items"][0]["story_id"]
        detail = client.get(f"/newswire/stories/{story_id}")
        risk = client.get("/newswire/risk-state")

    assert feed.status_code == 200
    assert feed.json()["view"] == "canonical_stories"
    assert feed.json()["items"][0]["assessment"]["feed_action"] == "standard"
    assert detail.status_code == 200
    assert detail.json()["story_id"] == story_id
    assert risk.status_code == 200
    assert risk.json()["items"] == []


def test_newswire_v2_migration_creates_canonical_tables_and_event_columns() -> None:
    engine = create_engine("sqlite://")
    metadata = MetaData()
    Table("newswire_events", metadata, Column("event_id", String(64), primary_key=True))
    metadata.create_all(engine)
    spec = spec_from_file_location("migration_0026_newswire_v2", Path("alembic/versions/0026_newswire_v2.py"))
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)

    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    event_columns = {column["name"] for column in inspector.get_columns("newswire_events")}

    assert {
        "newswire_stories",
        "newswire_story_revisions",
        "newswire_deliveries",
        "newswire_risk_states",
        "newswire_risk_transitions",
    } <= tables
    assert {"story_id", "story_revision", "topics_json", "assessment_json"} <= event_columns
