from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import anyio
from fastapi.testclient import TestClient

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.newswire_replay import NewswireEngineReplayService
from hyperliquid_trading_agent.app.main import create_app
from hyperliquid_trading_agent.app.newswire.assessment import NewswireAssessor, SelectiveAssessmentReviewer
from hyperliquid_trading_agent.app.newswire.calibration import build_calibration_report
from hyperliquid_trading_agent.app.newswire.normalize import normalize
from hyperliquid_trading_agent.app.newswire.observability import (
    build_engine_newsfeed_health,
    build_newswire_soak_readiness,
)
from hyperliquid_trading_agent.app.newswire.schemas import (
    NewswireAssessment,
    NewswireEvent,
    NewswireStory,
    NewswireStoryRevision,
    RawNewsItem,
)
from hyperliquid_trading_agent.app.newswire.service import NewswireService
from hyperliquid_trading_agent.app.newswire.watchlist import EntityMatch


def _settings(**updates: Any) -> Settings:
    values = {
        "environment": "test",
        "newswire_enabled": True,
        "newswire_model_classify_enabled": False,
        "engine_enabled": True,
        "engine_newsfeed_enabled": True,
        "_env_file": None,
        **updates,
    }
    return Settings(**values)


def _assessment(
    *,
    story_id: str = "nws_test",
    version: str = "newswire_assessment_v2.1",
    feed_action: str = "high",
    engine_action: str = "risk_only",
) -> NewswireAssessment:
    return NewswireAssessment(
        assessment_version=version,
        decision_id=f"nwd_{story_id}",
        story_id=story_id,
        watch_priority="core",
        audience_scope="watched_asset",
        matched_symbols=["BTC"],
        relevance_score=90,
        impact_score=90,
        urgency_score=100,
        source_quality_score=90,
        novelty_score=100,
        priority_score=90,
        direction="unknown",
        risk_severity=0.9,
        feed_action=feed_action,  # type: ignore[arg-type]
        engine_action=engine_action,  # type: ignore[arg-type]
        assessed_at_ms=_now_ms(),
    )


def _story(*, assessment: NewswireAssessment | None = None, now_ms: int | None = None) -> NewswireStory:
    now = now_ms or _now_ms()
    assessment = assessment or _assessment()
    return NewswireStory(
        story_id=assessment.story_id,
        canonical_event_id="nw_test",
        headline="Bitcoin exchange outage suspends withdrawals",
        body="A major exchange reported a critical outage.",
        source="coindesk",
        provider="coindesk",
        sources=["coindesk"],
        providers=["coindesk"],
        member_event_ids=["nw_test"],
        symbols=["BTC"],
        topics=["exchange_risk"],
        asset_class="crypto",
        event_type="exchange_status",
        urgency="breaking",
        source_score=0.9,
        confidence=0.9,
        published_at_ms=now - 60_000,
        first_seen_at_ms=now - 60_000,
        last_updated_at_ms=now - 60_000,
        assessment=assessment,
        metadata={"newswire_routing_mode": "active", "last_update_type": "created"},
    )


def test_trusted_nasdaq_bare_ticker_is_extracted_and_unwatched_halt_is_capped() -> None:
    now = _now_ms()
    raw = RawNewsItem(
        source="nasdaq_halts",
        provider="nasdaq_halts",
        transport="rss",
        external_id="halt-mlec",
        headline="Trading halt on MLEC",
        published_at_ms=now,
    )
    event = normalize(raw, symbols_universe=["BTC"], received_at_ms=now)
    assert event is not None
    assert event.symbols == ["MLEC"]
    assert event.metadata["trusted_source_symbols"] == ["MLEC"]

    async def run() -> NewswireEvent | None:
        return await NewswireService(settings=_settings(newswire_watchlist="BTC"))._ingest(raw)

    assessed = anyio.run(run)
    assert assessed is not None and assessed.assessment is not None
    assert assessed.assessment.assessment_version == "newswire_assessment_v2.1"
    assert assessed.assessment.audience_scope == "unwatched_single_name"
    assert assessed.assessment.feed_action == "watch"
    assert assessed.assessment.engine_action == "ledger_only"
    assert "unwatched_single_name_cap" in assessed.assessment.penalty_codes
    assert assessed.assessment.symbol_match_reasons["MLEC"] == ["trusted_source_symbol"]


def test_watched_trusted_halt_can_break_and_broad_crypto_shock_routes_risk() -> None:
    now = _now_ms()

    async def run() -> tuple[NewswireEvent | None, NewswireEvent | None]:
        service = NewswireService(settings=_settings(newswire_watchlist="NVDA"))
        halt = await service._ingest(
            RawNewsItem(
                source="nasdaq_halts",
                provider="nasdaq_halts",
                transport="rss",
                external_id="halt-nvda",
                headline="Trading halt on NVDA",
                published_at_ms=now,
            )
        )
        broad = await service._ingest(
            RawNewsItem(
                source="coindesk",
                provider="coindesk",
                transport="rss",
                external_id="crypto-outage",
                headline="Major crypto exchange outage suspends withdrawals",
                published_at_ms=now,
                asset_class="crypto",
                event_type="exchange_status",
            )
        )
        return halt, broad

    halt, broad = anyio.run(run)
    assert halt is not None and halt.assessment is not None
    assert halt.assessment.audience_scope == "watched_asset"
    assert halt.assessment.feed_action == "breaking"
    assert broad is not None and broad.assessment is not None
    assert broad.assessment.audience_scope == "broad_market"
    assert broad.assessment.engine_action == "risk_only"


def test_model_review_eligibility_is_fresh_boundary_only() -> None:
    settings = _settings(newswire_model_classify_enabled=True)
    assessor = NewswireAssessor(settings)
    now = _now_ms()
    event = NewswireEvent(
        event_id="nw_boundary",
        source="coindesk",
        provider="coindesk",
        transport="rss",
        received_at_ms=now,
        published_at_ms=now,
        headline="Bitcoin market update",
        symbols=["BTC"],
        asset_class="crypto",
        source_score=0.7,
    )
    watched = EntityMatch(symbols=["BTC"], reasons={"BTC": ["provider_symbol"]}, topics=[], watch_priority="core")
    assert assessor.model_review_eligibility(event, watched, 52.5, audience_scope="watched_asset") == (
        True,
        "boundary_50",
    )
    assert assessor.model_review_eligibility(event, watched, 53.1, audience_scope="watched_asset")[0] is False
    assert assessor.model_review_eligibility(
        event.model_copy(update={"freshness": "stale"}), watched, 50, audience_scope="watched_asset"
    ) == (False, "stale")
    assert assessor.model_review_eligibility(
        event.model_copy(update={"metadata": {"newswire_startup_backlog": True}}),
        watched,
        50,
        audience_scope="watched_asset",
    ) == (False, "startup_backlog")
    equity = event.model_copy(update={"asset_class": "equity", "symbols": ["MLEC"]})
    unwatched = EntityMatch(
        symbols=["MLEC"], reasons={"MLEC": ["trusted_source_symbol"]}, topics=[], watch_priority="unwatched"
    )
    assert assessor.model_review_eligibility(
        equity, unwatched, 50, audience_scope="unwatched_single_name"
    ) == (False, "unwatched_equity")


def test_model_reviewer_uses_one_call_no_repair_and_bounded_queue() -> None:
    class Gateway:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        def configured_attempts(self) -> list[Any]:
            return [SimpleNamespace(model="openai:test", missing_reason=None)]

        async def complete_with_chain(self, _prompt: str, _system: str, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            self.started.set()
            await self.release.wait()
            return SimpleNamespace(content='{"impact_band":"material","confidence":0.9}')

    async def run() -> tuple[list[tuple[Any, str]], Gateway, dict[str, Any]]:
        gateway = Gateway()
        reviewer = SelectiveAssessmentReviewer(
            _settings(
                newswire_model_classify_enabled=True,
                newswire_model_classify_queue_size=1,
                agent_model_chain="openai:test,openai:fallback",
            ),
            gateway,
        )
        base = _story().to_event()
        first = asyncio.create_task(reviewer.review(base, _assessment()))
        await gateway.started.wait()
        second = asyncio.create_task(
            reviewer.review(base.model_copy(update={"event_id": "nw_second", "headline": "Second"}), _assessment())
        )
        await asyncio.sleep(0)
        third = await reviewer.review(
            base.model_copy(update={"event_id": "nw_third", "headline": "Third"}), _assessment()
        )
        gateway.release.set()
        results = [await first, await second, third]
        status = reviewer.status()
        await reviewer.stop()
        return results, gateway, status

    results, gateway, status = anyio.run(run)
    assert [state for _, state in results] == ["applied", "applied", "fallback"]
    assert len(gateway.calls) == 2
    assert all(call["model_chain"] == ["openai:test"] for call in gateway.calls)
    assert status["queue_dropped"] == 1
    assert status["attempt_policy"] == "one_model_call_no_repair"


def test_calibration_and_reclassification_are_auditable_without_live_publish() -> None:
    old = _assessment(version="newswire_assessment_v2", feed_action="breaking")
    story = _story(assessment=old)
    report = build_calibration_report([story.model_dump(mode="json")])
    assert report["dimensions"]["audience_scope"]["watched_asset"]["count"] == 1
    assert report["threshold_inclusion"]["80"]["included"] == 1

    class Repo:
        enabled = True

        def __init__(self) -> None:
            self.upserts: list[dict[str, Any]] = []
            self.decisions: list[dict[str, Any]] = []

        async def list_newswire_stories(self, **_kwargs: Any) -> list[dict[str, Any]]:
            return [story.model_dump(mode="json")]

        async def upsert_newswire_story(self, value: dict[str, Any]) -> str:
            self.upserts.append(value)
            return str(value["story_id"])

        async def record_newswire_decision(self, value: dict[str, Any]) -> str:
            self.decisions.append(value)
            return str(value["decision_id"])

    async def run() -> tuple[dict[str, Any], Repo, list[NewswireEvent]]:
        repo = Repo()
        service = NewswireService(settings=_settings(), repository=repo)
        published: list[NewswireEvent] = []
        await service.bus.subscribe(lambda event: published.append(event))
        result = await service.reclassify_stories({"dry_run": False, "limit": 10})
        return result, repo, published

    result, repo, published = anyio.run(run)
    assert result["assessment_version"] == "newswire_assessment_v2.1"
    assert result["applied"] == 1
    assert result["published_to_live_bus"] is False
    assert result["live_consumer_offset_mutated"] is False
    assert repo.upserts[0]["assessment"]["assessment_version"] == "newswire_assessment_v2.1"
    assert repo.decisions
    assert published == []


def test_engine_newswire_replay_preserves_timestamps_and_live_offset() -> None:
    now = _now_ms()
    story = _story(now_ms=now)
    revision = NewswireStoryRevision(
        revision_id="nwsr_test_1",
        story_id=story.story_id,
        revision=1,
        update_type="created",
        emitted_at_ms=now - 30_000,
        story=story,
    )

    class Repo:
        def __init__(self) -> None:
            self.offset = {
                "consumer_name": "trader:engine_newswire",
                "source_table": "newswire_story_revisions",
                "last_event_id": "nwsr_live",
                "last_event_ts_ms": now - 10_000,
                "updated_at_ms": now - 10_000,
            }

        async def get_consumer_offset(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return dict(self.offset)

        async def list_newswire_story_revisions(self, **_kwargs: Any) -> list[dict[str, Any]]:
            return [revision.model_dump(mode="json")]

    class Ledger:
        def __init__(self) -> None:
            self.events: list[Any] = []

        async def record(self, event: Any) -> Any:
            self.events.append(event)
            return event

    class Features:
        def __init__(self) -> None:
            self.events: list[Any] = []

        async def features_for_event(self, event: Any) -> list[int]:
            self.events.append(event)
            return [1, 2, 3]

    async def run() -> tuple[dict[str, Any], Repo, Ledger, Features]:
        repo, ledger, features = Repo(), Ledger(), Features()
        result = await NewswireEngineReplayService(
            settings=_settings(engine_news_min_source_score=0.5),
            repository=repo,
            replay_ledger=ledger,
            replay_feature_store=features,
        ).run(
            {
                "start_ms": now - 3_600_000,
                "end_ms": now,
                "symbols": ["BTC"],
                "dry_run": False,
            }
        )
        return result, repo, ledger, features

    result, repo, ledger, features = anyio.run(run)
    assert result["events_recorded"] == 1
    assert result["features_created"] == 3
    assert result["live_offset_before"] == result["live_offset_after"] == repo.offset
    assert result["live_offset_write_performed"] is False
    assert result["order_intents_created"] == result["execution_reports_created"] == 0
    assert result["isolated_from_live_in_memory_state"] is True
    assert ledger.events[0].computed_ts_ms == ledger.events[0].received_ts_ms == story.last_updated_at_ms
    assert ledger.events[0].metadata["replay"] is True
    assert ledger.events[0].metadata["execution_authority"] == "none"
    assert features.events == ledger.events


def test_newsfeed_health_and_soak_readiness_are_machine_readable_and_time_based() -> None:
    now = _now_ms()
    runtime = {
        "consumer": {
            "running": True,
            "received_events": 10,
            "recorded_events": 8,
            "features_created": 12,
            "skipped_events": 2,
            "skip_reasons": {"policy_ledger_only": 2},
            "error_count": 0,
        },
        "pump": {"running": True, "processed": 10, "error_count": 0, "invalid_rows_skipped": 0},
    }
    offset = {
        "last_event_id": "nwsr_latest",
        "last_event_ts_ms": now - 1_000,
        "updated_at_ms": now - 1_000,
    }
    health = build_engine_newsfeed_health(
        _settings(), runtime, offset, newswire_active=True, generated_at_ms=now
    )
    assert health["status"] == "healthy"
    assert health["counters"]["features_created"] == 12
    stale = build_engine_newsfeed_health(
        _settings(newswire_engine_offset_stale_seconds=60),
        runtime,
        {**offset, "last_event_ts_ms": now - 10 * 60_000, "updated_at_ms": now - 10 * 60_000},
        newswire_active=True,
        latest_source_at_ms=now - 1_000,
        generated_at_ms=now,
    )
    assert stale["status"] == "degraded"
    assert "live_offset_stale" in {item["code"] for item in stale["reasons"]}
    api_role_health = build_engine_newsfeed_health(
        _settings(engine_enabled=False),
        runtime,
        offset,
        newswire_active=True,
        generated_at_ms=now,
    )
    assert api_role_health["enabled"] is True
    assert api_role_health["configured_for_local_role"] is False
    assert api_role_health["runtime_detected"] is True
    assert api_role_health["status"] == "healthy"

    recovered_runtime = {
        **runtime,
        "pump": {
            **runtime["pump"],
            "error_count": 5,
            "invalid_rows_skipped": 5,
            "consecutive_error_count": 0,
            "last_success_at_ms": now - 1_000,
            "last_error_at_ms": now - 2_000,
            "last_invalid_row_at_ms": now - 2 * 60 * 60_000,
        },
    }
    recovered = build_engine_newsfeed_health(
        _settings(), recovered_runtime, offset, newswire_active=True, generated_at_ms=now
    )
    assert recovered["status"] == "healthy"
    assert recovered["counters"]["pump_errors"] == 5

    active_error_runtime = {
        **recovered_runtime,
        "pump": {**recovered_runtime["pump"], "consecutive_error_count": 2},
    }
    active_error = build_engine_newsfeed_health(
        _settings(), active_error_runtime, offset, newswire_active=True, generated_at_ms=now
    )
    assert active_error["status"] == "degraded"
    assert "pump_errors" in {item["code"] for item in active_error["reasons"]}

    recent_invalid_runtime = {
        **recovered_runtime,
        "pump": {**recovered_runtime["pump"], "last_invalid_row_at_ms": now - 1_000},
    }
    recent_invalid = build_engine_newsfeed_health(
        _settings(), recent_invalid_runtime, offset, newswire_active=True, generated_at_ms=now
    )
    assert recent_invalid["status"] == "warning"
    assert "invalid_rows_skipped" in {item["code"] for item in recent_invalid["reasons"]}

    class Repo:
        def __init__(self, started_at_ms: int) -> None:
            self.started_at_ms = started_at_ms

        async def list_service_heartbeats(self, **_kwargs: Any) -> list[dict[str, Any]]:
            return [
                {
                    "service_role": "newswire",
                    "status": "running",
                    "started_at_ms": self.started_at_ms,
                    "metadata": {"newswire": {"running": True}},
                },
                {
                    "service_role": "trader",
                    "status": "running",
                    "started_at_ms": self.started_at_ms,
                    "metadata": {"engine_newsfeed": runtime},
                },
            ]

        async def get_consumer_offset(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return offset

        async def list_newswire_stories(self, **_kwargs: Any) -> list[dict[str, Any]]:
            return [{"story_id": "nws_test"}]

        async def list_newswire_events(self, **_kwargs: Any) -> list[dict[str, Any]]:
            return [{"event_id": "nw_test", "received_at_ms": now - 1_000}]

        async def list_newswire_story_revisions(self, **_kwargs: Any) -> list[dict[str, Any]]:
            return [{"revision_id": "nwsr_latest", "emitted_at_ms": now - 1_000}]

        async def list_normalized_events(self, **_kwargs: Any) -> list[dict[str, Any]]:
            return [{"event_id": "evt_test", "received_ts_ms": now - 1_000, "metadata": {}}]

        async def list_feature_values(self, **_kwargs: Any) -> list[dict[str, Any]]:
            return [
                {
                    "feature_id": "fv_test",
                    "feature_group": "news",
                    "computed_ts_ms": now - 1_000,
                    "metadata": {},
                }
            ]

        async def list_order_intents(self, **_kwargs: Any) -> list[dict[str, Any]]:
            return []

        async def list_execution_reports(self, **_kwargs: Any) -> list[dict[str, Any]]:
            return []

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        passed = await build_newswire_soak_readiness(
            Repo(now - 25 * 3_600_000), _settings(), generated_at_ms=now
        )
        restarted = await build_newswire_soak_readiness(
            Repo(now - 60 * 60_000), _settings(), generated_at_ms=now
        )
        return passed, restarted

    passed, restarted = anyio.run(run)
    assert passed["ready"] is True
    assert passed["criteria"]["no_paper_or_live_execution_side_effects"] is True
    assert restarted["ready"] is False
    assert restarted["remaining_ms"] > 0
    assert "continuous_window_complete" in restarted["blockers"]
    assert restarted["restart_resets_continuous_window"] is True


def test_newswire_operator_endpoints_expose_calibration_and_queue_owned_commands() -> None:
    story = _story()

    class Repo:
        def __init__(self) -> None:
            self.commands: list[dict[str, Any]] = []

        async def list_newswire_stories(self, **_kwargs: Any) -> list[dict[str, Any]]:
            return [story.model_dump(mode="json")]

        async def enqueue_worker_command(self, **kwargs: Any) -> dict[str, Any]:
            command = {"command_id": f"cmd_{len(self.commands) + 1}", "status": "pending", **kwargs}
            self.commands.append(command)
            return command

    settings = Settings(
        environment="test",
        database_url="sqlite+aiosqlite://",
        newswire_enabled=False,
        engine_enabled=False,
        position_tracking_enabled=False,
        autonomy_enabled=False,
        _env_file=None,
    )
    app = create_app(settings)
    repo = Repo()
    with TestClient(app) as client:
        app.state.repository = repo
        calibration = client.get("/newswire/calibration")
        reclassify = client.post("/newswire/reclassify", json={"dry_run": True})
        replay = client.post("/newswire/replay", json={"window_hours": 1, "dry_run": True})

    assert calibration.status_code == 200
    assert calibration.json()["assessment_version"] == "newswire_assessment_v2.1"
    assert reclassify.json()["target_role"] == "newswire"
    assert reclassify.json()["publishes_to_live_bus"] is False
    assert replay.json()["target_role"] == "trader"
    assert replay.json()["live_consumer_offset_mutation"] is False
    assert [item["command_type"] for item in repo.commands] == [
        "newswire_reclassify",
        "engine_newswire_replay",
    ]


def test_newswire_status_uses_worker_runtime_when_api_role_is_disabled() -> None:
    runtime = {
        "consumer": {"running": True, "received_events": 4, "recorded_events": 1, "features_created": 8},
        "pump": {"running": True, "processed": 4, "error_count": 0},
    }

    class Repo:
        enabled = True

        async def list_newswire_events(self, **_kwargs: Any) -> list[dict[str, Any]]:
            return [{"event_id": "nw_1", "received_at_ms": 10}]

        async def list_newswire_stories(self, **_kwargs: Any) -> list[dict[str, Any]]:
            return [{"story_id": "nws_1", "last_updated_at_ms": 10, "assessment": {}}]

        async def list_newswire_risk_states(self, **_kwargs: Any) -> list[dict[str, Any]]:
            return []

        async def list_service_heartbeats(self, service_role: str, **_kwargs: Any) -> list[dict[str, Any]]:
            if service_role == "newswire":
                return [{"service_role": service_role, "status": "running", "metadata": {}}]
            if service_role == "discord_publisher":
                return [{"service_role": service_role, "status": "running", "metadata": {}}]
            if service_role == "trader":
                return [{"service_role": service_role, "status": "running", "metadata": {"engine_newsfeed": runtime}}]
            return []

        async def get_consumer_offset(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"last_event_id": "nwsr_1", "last_event_ts_ms": 10, "updated_at_ms": _now_ms()}

    settings = _settings(newswire_enabled=False, engine_enabled=False)
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.repository = Repo()
        status = client.get("/newswire/status").json()

    assert status["enabled"] is True
    assert status["running"] is True
    assert status["configured_for_api_role"] is False
    assert status["owner_role"] == "newswire"
    assert status["runtime_source"] == "newswire_heartbeat"
    assert status["engine_newsfeed"]["health"]["enabled"] is True


def _now_ms() -> int:
    return int(time.time() * 1000)
