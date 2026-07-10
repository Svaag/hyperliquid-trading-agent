from __future__ import annotations

import anyio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.db.models import (
    NewswireDecisionRow,
    NewswireEvalRow,
    NewswirePolicyVersionRow,
    NewswirePublishLedgerRow,
    NewswireRewardRow,
)
from hyperliquid_trading_agent.app.db.repository import Repository
from hyperliquid_trading_agent.app.engine.feature_store import derive_features
from hyperliquid_trading_agent.app.engine.newswire_bridge import newswire_event_to_engine_event
from hyperliquid_trading_agent.app.main import create_app
from hyperliquid_trading_agent.app.newswire.adapters.alpaca_ws import AlpacaNewsAdapter
from hyperliquid_trading_agent.app.newswire.adapters.base import NewswireAdapter, RawEmit
from hyperliquid_trading_agent.app.newswire.bus import InProcessNewswireBus, QueueSubscriber
from hyperliquid_trading_agent.app.newswire.classify import classify_event_type, classify_urgency, source_score
from hyperliquid_trading_agent.app.newswire.consumers.discord_news import DiscordNewsPublisher
from hyperliquid_trading_agent.app.newswire.format import (
    format_news_digest_message,
    format_news_event_message,
)
from hyperliquid_trading_agent.app.newswire.keyword_matcher import score_importance_details
from hyperliquid_trading_agent.app.newswire.normalize import normalize
from hyperliquid_trading_agent.app.newswire.policy import DeterministicNewswirePolicy, EngineAction, NewswireAction
from hyperliquid_trading_agent.app.newswire.reward import build_reward
from hyperliquid_trading_agent.app.newswire.riskgate import HaltStateGate
from hyperliquid_trading_agent.app.newswire.schemas import NewswireEvent, NewswireFilter, RawNewsItem
from hyperliquid_trading_agent.app.newswire.service import NewswireService

UNIVERSE = ["BTC", "ETH", "HYPE", "AAPL", "NVDA"]


def _raw(**kwargs) -> RawNewsItem:
    base = {"source": "rss", "transport": "rss", "headline": "Generic headline"}
    base.update(kwargs)
    return RawNewsItem(**base)


def _event(**kwargs) -> NewswireEvent:
    event = normalize(_raw(**kwargs), symbols_universe=UNIVERSE, received_at_ms=1_000)
    assert event is not None
    return event


# --- deterministic pipeline --------------------------------------------------


def test_normalize_classifies_filing_and_tags_symbols():
    event = normalize(
        _raw(source="sec_edgar", external_id="acc-1", headline="NVDA files 8-K with SEC on guidance"),
        symbols_universe=UNIVERSE,
        received_at_ms=2_000,
    )
    assert event is not None
    assert event.event_type == "sec_filing"
    assert event.asset_class == "equity"
    assert "NVDA" in event.symbols
    assert event.source_score == 1.0
    assert event.event_id.startswith("nw_")
    assert event.tradability.allow_auto_trade is False


def test_normalize_returns_none_for_empty_item():
    assert normalize(_raw(headline="", body=""), symbols_universe=UNIVERSE) is None


def test_event_id_is_stable_for_updates():
    created = _raw(source="alpaca", external_id="42", headline="Headline v1")
    updated = _raw(source="alpaca", external_id="42", headline="Headline v2 corrected", action="updated")
    assert normalize(created, symbols_universe=UNIVERSE).event_id == normalize(updated, symbols_universe=UNIVERSE).event_id


def test_classify_event_type_and_urgency_and_source_score():
    assert classify_event_type("federal_reserve", "FOMC raises interest rate") == "macro"
    assert classify_event_type("rss", "Acme to acquire Beta in all-cash deal") == "mna"
    assert classify_urgency("nasdaq_halts", "rss", "halt", 50.0, "trading halt on AAPL") == "breaking"
    assert classify_urgency("rss", "rss", "headline", 10.0, "minor update") == "background"
    assert source_score("sec_edgar") == 1.0
    assert source_score("x_cashtag") == 0.4


def test_to_news_event_bridge_preserves_core_fields():
    event = _event(source="coindesk", headline="ETH rallies on inflows", symbols=["ETH"])
    legacy = event.to_news_event()
    assert legacy.id == event.event_id
    assert legacy.title == event.headline
    assert legacy.assets == event.symbols
    assert legacy.metadata["event_type"] == event.event_type


def test_discord_embed_payloads_are_embed_first_and_decode_entities():
    event = _event(source="coindesk", headline="ETH rallies &#39;hard&#39; on inflows", symbols=["ETH"])
    event.importance_score = 82.0
    event.urgency = "breaking"
    single = format_news_event_message(event)
    assert single["content"] == ""
    assert "ETH rallies 'hard'" in single["fallback_content"]
    assert "ETH rallies 'hard'" in single["embeds"][0]["title"]
    assert "footer" not in single["embeds"][0]
    assert single["embeds"][0]["color"] == 0xE74C3C

    digest = format_news_digest_message([event], max_items=10)
    assert digest["content"] == ""
    assert "Newswire digest" in digest["embeds"][0]["title"]
    assert "ETH rallies 'hard'" in digest["embeds"][0]["description"]
    assert "footer" not in digest["embeds"][0]


def test_keyword_matcher_uses_exact_terms_and_penalizes_low_value_posts():
    securitize = score_importance_details("Securitize expands tokenized treasury product", "", "", {})
    assert "sec" not in securitize.keyword_hits
    assert securitize.score == 15.0

    historical = score_importance_details(
        "If You Invested $1000 In Dogecoin 5 Years Ago",
        "Dogecoin has a market cap of $75 billion today.",
        "",
        {},
    )
    assert historical.score < 25.0
    assert "historical_investment_article" in historical.penalties
    assert "material_size_language:body" not in historical.reasons

    material = score_importance_details("Fund buys $2 billion BTC after ETF inflows", "", "", {})
    assert material.score >= 35.0
    assert "material_size_language:title" in material.reasons


def test_policy_decision_reward_penalizes_bad_engine_false_positive():
    event = _event(source="coindesk", headline="Breaking: ETH ETF approved with major inflows", symbols=["ETH"])
    event.importance_score = 90.0
    event.urgency = "breaking"
    event.sentiment = "bullish"
    event.confidence = 0.9
    event.source_score = 0.85
    decision = DeterministicNewswirePolicy(Settings(newswire_policy_shadow_only=False)).score(event)
    assert decision.newswire_action in {NewswireAction.HIGH, NewswireAction.BREAKING}
    assert decision.engine_action in {EngineAction.DIRECTIONAL_FEATURE, EngineAction.RISK_ONLY}

    reward = build_reward(
        decision,
        [
            {"label_type": "quality", "label_value": False, "confidence": 1.0},
            {"label_type": "tradable", "label_value": False, "confidence": 1.0},
            {"label_type": "direction_correct", "label_value": False, "confidence": 1.0},
        ],
    )
    assert reward.total_reward < -8.0
    assert "sent_low_quality_event_to_engine" in reward.reasons


def test_policy_decision_summary_adds_feedback_components():
    event = _event(source="coindesk", headline="ETH ETF inflows rise", symbols=["ETH"])
    event.metadata["newswire_policy_decision"] = {
        "decision_id": "nwd_1",
        "policy_version": "newswire_baseline_v1",
        "shadow_only": True,
        "newswire_action": "high",
        "engine_action": "risk_only",
        "quality_score": 72.0,
        "market_impact_score": 80.0,
    }
    payload = format_news_event_message(event)
    field_names = {field["name"] for field in payload["embeds"][0]["fields"]}
    assert {"Policy", "Quality", "Impact"} <= field_names
    assert any(item["custom_id"].startswith(f"nwfb:{event.event_id}:") for item in payload["components"])


def test_active_policy_decision_drives_engine_news_features():
    settings = Settings(newswire_policy_shadow_only=False)
    event = _event(source="coindesk", headline="ETH exchange outage increases risk", symbols=["ETH"])
    event.source_score = 0.85
    event.confidence = 0.8
    event.metadata["newswire_policy_decision"] = {
        "decision_id": "nwd_1",
        "policy_version": "newswire_policy_test",
        "shadow_only": False,
        "newswire_action": "high",
        "engine_action": "risk_only",
        "market_impact_score": 80.0,
        "quality_score": 72.0,
        "relevance_score": 85.0,
        "novelty_score": 75.0,
        "urgency_score": 55.0,
        "source_score": 0.85,
        "confidence": 0.8,
        "direction_score": 1.0,
        "direction_confidence": 0.9,
        "risk_score": 0.75,
    }
    normalized = newswire_event_to_engine_event(event, settings=settings)
    assert normalized is not None
    features = derive_features(normalized)
    by_name = {feature.feature_name: feature for feature in features}
    assert by_name["catalyst_pressure"].scalar_value == 0.0
    assert by_name["event_risk_pressure"].scalar_value and by_name["event_risk_pressure"].scalar_value > 0
    assert by_name["source_consensus_score"].metadata["policy_version"] == "newswire_policy_test"


# --- bus ---------------------------------------------------------------------


def test_bus_fanout_respects_filters():
    async def run():
        bus = InProcessNewswireBus()
        crypto: list[NewswireEvent] = []
        equity: list[NewswireEvent] = []
        await bus.subscribe(lambda e: crypto.append(e), filter=NewswireFilter(asset_classes=["crypto"]))
        await bus.subscribe(lambda e: equity.append(e), filter=NewswireFilter(asset_classes=["equity"]))
        await bus.publish(_event(source="coindesk", headline="BTC pumps", symbols=["BTC"]))
        await bus.publish(_event(source="sec_edgar", external_id="x", headline="AAPL files 10-Q"))
        return crypto, equity

    crypto, equity = anyio.run(run)
    assert [e.asset_class for e in crypto] == ["crypto"]
    assert [e.asset_class for e in equity] == ["equity"]


def test_queue_subscriber_receives_published_events():
    async def run():
        bus = InProcessNewswireBus()
        async with QueueSubscriber(bus, filter=NewswireFilter(min_importance=0)) as sub:
            await bus.publish(_event(headline="Something happened"))
            return await sub.get()

    event = anyio.run(run)
    assert event.headline == "Something happened"


# --- halt gate ---------------------------------------------------------------


def test_halt_gate_marks_and_clears_symbols():
    gate = HaltStateGate()
    halt = _event(source="nasdaq_halts", external_id="h1", headline="Trading halt on AAPL", symbols=["AAPL"])
    gate.apply(halt)
    follow_up = _event(source="rss", external_id="f1", headline="AAPL rumor mill", symbols=["AAPL"])
    gated = gate.apply(follow_up)
    assert gated.tradability.halted_symbols == ["AAPL"]
    assert gated.tradability.halt_state_checked is True
    assert gated.tradability.allow_auto_trade is False

    resume = _event(source="nasdaq_halts", external_id="h2", headline="Trading resumes for AAPL", symbols=["AAPL"])
    gate.apply(resume)
    assert "AAPL" not in gate.halted_symbols()


# --- service -----------------------------------------------------------------


def test_service_ingest_persistence_diagnostics_and_dropped_counts():
    class Repo:
        enabled = True

        def __init__(self) -> None:
            self.events: dict[str, dict] = {}

        async def record_newswire_event(self, event: dict) -> str:
            self.events[event["event_id"]] = event
            return event["event_id"]

    async def run():
        repo = Repo()
        service = NewswireService(settings=Settings(newswire_enabled=True), repository=repo)
        raw = _raw(source="alpaca", external_id="100", headline="BTC breaks out", symbols=["BTC"])
        first = await service._ingest(raw)
        second = await service._ingest(raw)
        return service, repo, first, second

    service, repo, first, second = anyio.run(run)
    assert first is not None
    assert second is None
    assert len(repo.events) == 1
    status = service.status()
    assert status["persisted_event_count"] == 1
    assert status["persistence_errors"] == 0
    assert status["dropped_events_by_reason"] == {"duplicate": 1}


def test_service_ingest_persists_policy_decision():
    class Repo:
        enabled = True

        def __init__(self) -> None:
            self.events: dict[str, dict] = {}
            self.decisions: dict[str, dict] = {}

        async def record_newswire_event(self, event: dict) -> str:
            self.events[event["event_id"]] = event
            return event["event_id"]

        async def record_newswire_decision(self, decision: dict) -> str:
            self.decisions[decision["decision_id"]] = decision
            return decision["decision_id"]

    async def run():
        repo = Repo()
        service = NewswireService(settings=Settings(newswire_enabled=True), repository=repo)
        event = await service._ingest(_raw(source="coindesk", external_id="d1", headline="ETH ETF inflows rise", symbols=["ETH"]))
        return service, repo, event

    service, repo, event = anyio.run(run)
    assert event is not None
    assert repo.decisions
    assert event.metadata["newswire_policy_decision"]["policy_version"] == "newswire_assessment_v2"
    assert service.status()["persisted_decision_count"] == 1


def test_service_ingest_dedupes_and_publishes():
    async def run():
        service = NewswireService(settings=Settings(newswire_enabled=True))
        seen: list[NewswireEvent] = []
        await service.bus.subscribe(lambda e: seen.append(e))
        raw = _raw(source="alpaca", external_id="100", headline="BTC breaks out", symbols=["BTC"])
        first = await service._ingest(raw)
        second = await service._ingest(raw)
        return first, second, seen

    first, second, seen = anyio.run(run)
    assert first is not None
    assert second is None  # duplicate dropped
    assert len(seen) == 1
    assert seen[0].symbols == ["BTC"]


# --- alpaca adapter parsing --------------------------------------------------


def test_alpaca_frame_parses_into_raw_item():
    adapter = AlpacaNewsAdapter(ws_url="wss://x", api_key="k", api_secret="s", symbols=["*"])
    frame = {
        "T": "n",
        "id": 555,
        "headline": "Company X reports record earnings",
        "summary": "Beats estimates",
        "author": "Benzinga",
        "created_at": "2026-06-15T12:00:00Z",
        "url": "https://example.com/n/555",
        "symbols": ["nvda"],
        "source": "benzinga",
    }
    raw = adapter._to_raw(frame)
    assert raw.external_id == "555"
    assert raw.symbols == ["NVDA"]
    assert raw.transport == "websocket"
    event = normalize(raw, symbols_universe=UNIVERSE)
    assert event is not None
    assert event.event_type == "earnings"


# --- discord publisher -------------------------------------------------------


class _FakeSink:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, list[dict] | None]] = []

    async def send(self, channel_id: str, content: str, embeds: list[dict] | None = None) -> str | None:
        self.sent.append((channel_id, content, embeds))
        return "msg-1"


def test_discord_publisher_role_can_run_without_owning_news_ingestion():
    settings = Settings(
        environment="test",
        service_role="discord_publisher",
        newswire_enabled=False,
        discord_publisher_enabled=True,
        newswire_discord_enabled=True,
        newswire_news_channel_id="999",
        discord_bot_token="not-used",
        engine_enabled=False,
        autonomy_enabled=False,
        _env_file=None,
    )
    publisher = DiscordNewsPublisher(settings=settings, bus=InProcessNewswireBus(), alert_sink=_FakeSink())

    assert publisher.enabled is True


def test_discord_publisher_posts_breaking_immediately_and_batches_rest():
    async def run():
        settings = Settings(
            newswire_news_channel_id="999",
            newswire_send_min_interval_ms=0,
            newswire_breaking_min_importance=80,
            newswire_news_min_importance=0,
        )
        sink = _FakeSink()
        publisher = DiscordNewsPublisher(settings=settings, bus=InProcessNewswireBus(), alert_sink=sink)
        breaking = _event(source="nasdaq_halts", external_id="b", headline="Trading halt on NVDA", symbols=["NVDA"])
        normal = _event(source="coindesk", external_id="n", headline="ETH slowly grinds higher", symbols=["ETH"])
        normal.urgency = "normal"
        normal.importance_score = 30.0
        await publisher._on_event(breaking)
        await publisher._on_event(normal)
        before_flush = list(sink.sent)
        await publisher._flush()
        return before_flush, sink.sent

    before_flush, after_flush = anyio.run(run)
    assert len(before_flush) == 1  # only the breaking event posted immediately
    assert before_flush[0][1] == ""
    assert before_flush[0][2]  # embed payload included
    assert len(after_flush) == 2  # digest flushed the buffered normal event
    assert after_flush[1][1] == ""
    assert "digest" in after_flush[1][2][0]["title"].lower()


def test_discord_publisher_skips_startup_backlog():
    async def run():
        settings = Settings(
            newswire_news_channel_id="999",
            newswire_send_min_interval_ms=0,
            newswire_news_min_importance=0,
            newswire_discord_startup_grace_seconds=60,
        )
        sink = _FakeSink()
        publisher = DiscordNewsPublisher(settings=settings, bus=InProcessNewswireBus(), alert_sink=sink)
        publisher._started_at_ms = 1_000_000
        event = _event(source="coindesk", external_id="old", headline="BTC headline", symbols=["BTC"])
        event.importance_score = 90.0
        event.published_at_ms = 800_000
        event.freshness = "fresh"
        await publisher._on_event(event)
        return sink.sent, publisher.status()

    sent, status = anyio.run(run)
    assert sent == []
    assert status["skip_counts"]["startup_backlog"] == 1


def test_discord_feedback_component_records_human_eval():
    class Repo:
        def __init__(self) -> None:
            self.evals: list[dict] = []

        async def list_newswire_decisions(self, *, event_id: str, limit: int = 1) -> list[dict]:
            return [{"decision_id": "nwd_1", "policy_version": "newswire_baseline_v1", "event_id": event_id}]

        async def record_newswire_eval(self, eval_record: dict) -> str:
            self.evals.append(eval_record)
            return "nwe_1"

    async def run():
        repo = Repo()
        publisher = DiscordNewsPublisher(settings=Settings(newswire_news_channel_id="999"), bus=InProcessNewswireBus(), alert_sink=_FakeSink(), repository=repo)
        response = await publisher.handle_feedback_component("nwfb:nw_1:quality:noise", "user-1", "msg-1")
        return response, repo.evals

    response, evals = anyio.run(run)
    assert response == "Feedback recorded: quality=False."
    assert evals[0]["event_id"] == "nw_1"
    assert evals[0]["decision_id"] == "nwd_1"
    assert evals[0]["label_type"] == "quality"
    assert evals[0]["label_value"] is False
    assert evals[0]["metadata"]["discord_message_id"] == "msg-1"


# --- gateway -----------------------------------------------------------------


def test_newswire_gateway_lists_filters_and_404s():
    settings = Settings(environment="test", newswire_enabled=False, position_tracking_enabled=False, autonomy_enabled=False)
    app = create_app(settings)
    with TestClient(app) as client:
        service: NewswireService = app.state.newswire_service
        service._index(_event(source="coindesk", external_id="g1", headline="BTC surges", symbols=["BTC"]))
        service._index(_event(source="sec_edgar", external_id="g2", headline="AAPL files 8-K"))

        all_events = client.get("/newswire/events")
        assert all_events.status_code == 200
        assert all_events.json()["count"] == 2

        btc = client.get("/newswire/events", params={"symbol": "BTC"})
        assert btc.json()["count"] == 1
        assert btc.json()["items"][0]["symbols"] == ["BTC"]

        status = client.get("/newswire/status")
        assert status.status_code == 200
        assert status.json()["buffered_events"] == 2

        assert client.get("/newswire/events/does-not-exist").status_code == 404


def test_newswire_discord_test_endpoint_dry_run():
    settings = Settings(
        environment="test",
        newswire_enabled=False,
        newswire_news_channel_id="999",
        discord_bot_token="not-used",
        position_tracking_enabled=False,
        autonomy_enabled=False,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post("/newswire/discord/test", json={"dry_run": True})

    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is True
    assert body["channel_id"] == "999"
    assert body["payload"]["content"] == ""
    assert "Newswire Discord test" in body["payload"]["fallback_content"]
    assert "footer" not in body["payload"]["embeds"][0]


def test_world_model_live_exposes_newswire_discord_without_full_bot():
    settings = Settings(
        environment="test",
        runtime_profile="world_model_live",
        newswire_enabled=False,
        newswire_discord_enabled=True,
        newswire_news_channel_id="999",
        discord_bot_token="not-used",
        world_model_streams_enabled=False,
        position_tracking_enabled=True,
        autonomy_enabled=True,
        tradfi_enabled=True,
        engine_enabled=True,
        hip4_enabled=True,
        hip4_scan_enabled=True,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        ready = client.get("/ready").json()
        health = client.get("/health/config").json()

    assert ready["checks"]["discord_enabled"] is False
    assert health["newswire"]["discord_enabled"] is True
    assert health["newswire"]["discord_publisher"]["channel_configured"] is True
    assert health["newswire"]["discord_publisher"]["discord"]["configured"] is True


# --- repository ledger -------------------------------------------------------


def test_repository_newswire_publish_ledger_claims_and_status():
    async def run():
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as conn:
            await conn.run_sync(NewswirePublishLedgerRow.__table__.create)
        repo = Repository(async_sessionmaker(engine, expire_on_commit=False))
        first = await repo.claim_newswire_publish("nw_1", "999", "breaking", 1_000)
        duplicate_pending = await repo.claim_newswire_publish("nw_1", "999", "breaking", 2_000)
        await repo.mark_newswire_publish_failed(["nw_1"], "999", "boom", 3_000)
        retry_failed = await repo.claim_newswire_publish("nw_1", "999", "digest", 4_000)
        await repo.mark_newswire_publish_posted(["nw_1"], "999", "msg-1", 5_000)
        duplicate_posted = await repo.claim_newswire_publish("nw_1", "999", "breaking", 6_000)
        status = await repo.newswire_publish_status("999")
        await engine.dispose()
        return first, duplicate_pending, retry_failed, duplicate_posted, status

    first, duplicate_pending, retry_failed, duplicate_posted, status = anyio.run(run)
    assert first is True
    assert duplicate_pending is False
    assert retry_failed is True
    assert duplicate_posted is False
    assert status["counts"] == {"posted": 1}
    assert status["last_event_id"] == "nw_1"


def test_repository_newswire_policy_loop_records():
    async def run():
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as conn:
            await conn.run_sync(NewswireDecisionRow.__table__.create)
            await conn.run_sync(NewswireEvalRow.__table__.create)
            await conn.run_sync(NewswireRewardRow.__table__.create)
            await conn.run_sync(NewswirePolicyVersionRow.__table__.create)
        repo = Repository(async_sessionmaker(engine, expire_on_commit=False))
        decision = {
            "decision_id": "nwd_1",
            "event_id": "nw_1",
            "policy_version": "newswire_baseline_v1",
            "policy_type": "static",
            "raw_event_hash": "hash",
            "source": "coindesk",
            "provider": "rss",
            "symbols": ["ETH"],
            "event_type": "headline",
            "asset_class": "crypto",
            "newswire_action": "high",
            "engine_action": "risk_only",
            "market_impact_score": 80.0,
            "quality_score": 70.0,
            "relevance_score": 85.0,
            "novelty_score": 75.0,
            "urgency_score": 55.0,
            "source_score": 0.85,
            "confidence": 0.8,
            "direction_score": 0.0,
            "direction_confidence": 0.0,
            "risk_score": 0.7,
            "created_at_ms": 1_000,
        }
        await repo.record_newswire_decision(decision)
        await repo.record_newswire_eval({"event_id": "nw_1", "decision_id": "nwd_1", "label_type": "quality", "label_value": True, "created_at_ms": 2_000})
        reward = build_reward(decision, await repo.list_newswire_evals(decision_id="nwd_1"))
        await repo.record_newswire_reward(reward.model_dump(mode="json"))
        await repo.upsert_newswire_policy_version(
            {
                "policy_version": "newswire_bandit_test",
                "policy_type": "bandit",
                "status": "candidate",
                "params": {"learner": "contextual_bandit_v1"},
                "created_at_ms": 3_000,
            }
        )
        promoted = await repo.promote_newswire_policy_version("newswire_bandit_test", now_ms=4_000)
        rows = await repo.list_newswire_decisions(event_id="nw_1")
        evals = await repo.list_newswire_evals(event_id="nw_1")
        rewards = await repo.list_newswire_rewards(event_id="nw_1")
        policies = await repo.list_newswire_policy_versions(status="promoted")
        await engine.dispose()
        return promoted, rows, evals, rewards, policies

    promoted, rows, evals, rewards, policies = anyio.run(run)
    assert promoted is True
    assert rows[0]["decision_id"] == "nwd_1"
    assert evals[0]["label_value"] is True
    assert rewards[0]["total_reward"] > 0
    assert policies[0]["policy_version"] == "newswire_bandit_test"


# --- service supervisor lifecycle --------------------------------------------


class _OneShotAdapter(NewswireAdapter):
    name = "oneshot"

    async def run(self, emit: RawEmit) -> None:
        await emit(_raw(source="alpaca", external_id=" os1", headline="ETH breakout confirmed", symbols=["ETH"]))


def test_service_start_supervises_adapter_and_publishes(monkeypatch):
    async def run():
        service = NewswireService(settings=Settings(newswire_enabled=True))
        monkeypatch.setattr(service, "build_adapters", lambda: [_OneShotAdapter()])
        collected: list[NewswireEvent] = []
        await service.bus.subscribe(lambda e: collected.append(e))
        await service.start()
        await anyio.sleep(0.05)
        await service.stop()
        return collected

    collected = anyio.run(run)
    assert len(collected) == 1
    assert collected[0].symbols == ["ETH"]
