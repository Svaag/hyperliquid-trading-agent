from __future__ import annotations

import asyncio
import time
from collections import Counter
from typing import Any

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.newswire.assessment import ASSESSMENT_VERSION
from hyperliquid_trading_agent.app.newswire.calibration import build_calibration_report
from hyperliquid_trading_agent.app.newswire.classify import SOURCE_SCORES
from hyperliquid_trading_agent.app.newswire.feedback import build_newswire_feedback_summary
from hyperliquid_trading_agent.app.newswire.learning import train_contextual_bandit_policy
from hyperliquid_trading_agent.app.newswire.observability import (
    build_engine_newsfeed_health,
    build_newswire_soak_readiness,
)
from hyperliquid_trading_agent.app.newswire.policy import NewsEval
from hyperliquid_trading_agent.app.newswire.reward import build_reward
from hyperliquid_trading_agent.app.newswire.schemas import (
    NewswireEvent,
    NewswireFilter,
    NewswireStory,
    NewswireStoryRevision,
)

log = get_logger(__name__)

router = APIRouter()


class NewswireDiscordTestRequest(BaseModel):
    channel_id: str | None = None
    dry_run: bool = False


class NewswireEvalRequest(BaseModel):
    event_id: str
    decision_id: str | None = None
    policy_version: str | None = None
    evaluator_type: str = "human"
    evaluator_id: str | None = None
    label_type: str
    label_value: Any
    confidence: float = 1.0
    reason: str | None = None
    notes: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class NewswireRewardBuildRequest(BaseModel):
    event_id: str | None = None
    policy_version: str | None = None
    limit: int = 250


class NewswirePolicyTrainRequest(BaseModel):
    min_rows: int | None = None
    limit: int = 5000


class NewswireReclassifyRequest(BaseModel):
    start_ms: int = Field(default=0, ge=0)
    end_ms: int | None = Field(default=None, ge=1)
    symbols: list[str] = Field(default_factory=list, max_length=100)
    source: str | None = Field(default=None, max_length=64)
    limit: int = Field(default=500, ge=1, le=5000)
    dry_run: bool = True


class NewswireReplayRequest(BaseModel):
    start_ms: int | None = Field(default=None, ge=1)
    end_ms: int | None = Field(default=None, ge=1)
    window_hours: int = Field(default=24, ge=1, le=24 * 30)
    symbols: list[str] = Field(default_factory=list, max_length=100)
    source: str | None = Field(default=None, max_length=64)
    min_importance: float = Field(default=0.0, ge=0.0, le=100.0)
    limit: int = Field(default=1000, ge=1, le=5000)
    dry_run: bool = True


def register_newswire_routes(app: FastAPI) -> None:
    app.include_router(router)


def _auth(settings: Settings, authorization: str | None) -> None:
    # Lazy import avoids a circular import at module load (main imports this module).
    from hyperliquid_trading_agent.app.main import _require_agent_api

    _require_agent_api(settings, authorization)


def _ws_authorized(settings: Settings, token: str | None) -> bool:
    if settings.agent_api_bearer_token:
        return token == settings.agent_api_bearer_token
    return settings.environment.lower() in {"dev", "test", "local"}


def _build_filter(symbol: str | None, asset_class: str | None, event_type: str | None, source: str | None, min_importance: float) -> NewswireFilter:
    try:
        return NewswireFilter(
            symbols=[symbol] if symbol else [],
            asset_classes=[asset_class] if asset_class else [],  # type: ignore[list-item]
            event_types=[event_type] if event_type else [],  # type: ignore[list-item]
            sources=[source] if source else [],
            min_importance=min_importance,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/newswire/events")
async def list_newswire_events(
    request: Request,
    symbol: str | None = None,
    asset_class: str | None = None,
    event_type: str | None = None,
    source: str | None = None,
    min_importance: float = 0.0,
    limit: int = 100,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _auth(request.app.state.settings, authorization)
    flt = _build_filter(symbol, asset_class, event_type, source, min_importance)
    try:
        rows = await request.app.state.repository.list_newswire_events(limit=max(1, min(limit, 500)))
        events = [NewswireEvent.model_validate(row) for row in rows]
    except Exception:
        service = getattr(request.app.state, "newswire_service", None)
        events = service.list_events(limit=max(1, min(limit, 500))) if service is not None else []
    events = [event for event in events if flt.matches(event)]
    return {"items": [event.model_dump(mode="json") for event in events], "count": len(events)}


@router.get("/newswire/events/{event_id}")
async def get_newswire_event(request: Request, event_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _auth(request.app.state.settings, authorization)
    try:
        row = await request.app.state.repository.get_newswire_event(event_id)
        if row is not None:
            return NewswireEvent.model_validate(row).model_dump(mode="json")
    except Exception:
        pass
    service = getattr(request.app.state, "newswire_service", None)
    event = service.get_event(event_id) if service is not None else None
    if event is None:
        raise HTTPException(status_code=404, detail="newswire event not found")
    return event.model_dump(mode="json")


@router.get("/newswire/feed")
async def list_newswire_feed(
    request: Request,
    symbol: str | None = None,
    topic: str | None = None,
    status: str | None = None,
    feed_action: str | None = None,
    audience_scope: str | None = None,
    min_priority: float = 0.0,
    include_dropped: bool = False,
    limit: int = 100,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Canonical product feed: one current row per clustered story."""
    _auth(request.app.state.settings, authorization)
    bounded_limit = max(1, min(limit, 500))
    fetch_limit = min(2000, max(bounded_limit, bounded_limit * 4))
    try:
        rows = await request.app.state.repository.list_newswire_stories(
            status=status,
            symbol=symbol,
            feed_action=feed_action,
            limit=fetch_limit,
        )
    except Exception:
        service = getattr(request.app.state, "newswire_service", None)
        rows = (
            [story.model_dump(mode="json") for story in service.list_stories(limit=fetch_limit)]
            if service is not None and callable(getattr(service, "list_stories", None))
            else []
        )
    if not rows:
        service = getattr(request.app.state, "newswire_service", None)
        if service is not None and callable(getattr(service, "list_stories", None)):
            rows = [story.model_dump(mode="json") for story in service.list_stories(limit=fetch_limit)]
    stories: list[NewswireStory] = []
    for row in rows:
        try:
            story = NewswireStory.model_validate(row)
        except ValidationError:
            continue
        assessment = story.assessment
        if not include_dropped and feed_action != "drop" and (assessment is None or assessment.feed_action == "drop"):
            continue
        if assessment is not None and assessment.priority_score < min_priority:
            continue
        if audience_scope and (assessment is None or assessment.audience_scope != audience_scope):
            continue
        if topic and topic.lower() not in {item.lower() for item in story.topics}:
            continue
        stories.append(story)
        if len(stories) >= bounded_limit:
            break
    return {
        "items": [story.model_dump(mode="json") for story in stories],
        "count": len(stories),
        "view": "canonical_stories",
        "assessment_version": ASSESSMENT_VERSION,
    }


@router.get("/newswire/stories/{story_id}")
async def get_newswire_story(
    request: Request,
    story_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _auth(request.app.state.settings, authorization)
    try:
        row = await request.app.state.repository.get_newswire_story(story_id)
    except Exception:
        service = getattr(request.app.state, "newswire_service", None)
        local = service.get_story(story_id) if service is not None and callable(getattr(service, "get_story", None)) else None
        row = local.model_dump(mode="json") if local is not None else None
    if row is None:
        service = getattr(request.app.state, "newswire_service", None)
        local = service.get_story(story_id) if service is not None and callable(getattr(service, "get_story", None)) else None
        row = local.model_dump(mode="json") if local is not None else None
    if row is None:
        raise HTTPException(status_code=404, detail="newswire story not found")
    try:
        story = NewswireStory.model_validate(row)
    except ValidationError as exc:
        raise HTTPException(status_code=500, detail=f"invalid persisted newswire story: {exc}") from None
    try:
        revisions = await request.app.state.repository.list_newswire_story_revisions(story_id=story_id, limit=100)
    except Exception:
        revisions = []
    return {
        **story.model_dump(mode="json"),
        "revisions": revisions,
        "revision_count": len(revisions),
    }


@router.get("/newswire/risk-state")
async def get_newswire_risk_state(
    request: Request,
    scope: str | None = None,
    include_transitions: bool = True,
    limit: int = 100,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _auth(request.app.state.settings, authorization)
    bounded_limit = max(1, min(limit, 500))
    try:
        states = await request.app.state.repository.list_newswire_risk_states(scope=scope, limit=bounded_limit)
        transitions = (
            await request.app.state.repository.list_newswire_risk_transitions(scope=scope, limit=bounded_limit)
            if include_transitions
            else []
        )
    except Exception:
        consumer = getattr(request.app.state, "engine_news_consumer", None)
        state_machine = getattr(consumer, "risk_state", None)
        local_states = list(getattr(state_machine, "states", {}).values()) if state_machine is not None else []
        if scope:
            local_states = [item for item in local_states if item.scope == scope.upper()]
        states = [item.model_dump(mode="json") for item in local_states[:bounded_limit]]
        transitions = []
    return {
        "items": states,
        "count": len(states),
        "transitions": transitions,
        "transition_count": len(transitions),
    }


@router.get("/newswire/status")
async def newswire_status(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _auth(request.app.state.settings, authorization)
    repo = request.app.state.repository
    try:
        latest = await repo.list_newswire_events(limit=1)
        latest_stories = await repo.list_newswire_stories(limit=500)
        risk_states = await repo.list_newswire_risk_states(limit=100)
        newswire_workers = await repo.list_service_heartbeats(service_role="newswire", limit=10)
        publisher_workers = await repo.list_service_heartbeats(service_role="discord_publisher", limit=10)
        trader_workers = await repo.list_service_heartbeats(service_role="trader", limit=10)
        engine_offset = await repo.get_consumer_offset(
            "trader:engine_newswire",
            source_table="newswire_story_revisions",
        )
    except Exception:
        service = getattr(request.app.state, "newswire_service", None)
        status = service.status() if service is not None else {"enabled": request.app.state.settings.newswire_enabled, "running": False}
        publisher = getattr(request.app.state, "newswire_discord", None)
        if publisher is not None and callable(getattr(publisher, "status_async", None)):
            status["discord_publisher"] = await publisher.status_async()
        return status
    action_counts = Counter(
        str((story.get("assessment") or {}).get("feed_action") or "unassessed") for story in latest_stories
    )
    engine_action_counts = Counter(
        str((story.get("assessment") or {}).get("engine_action") or "unassessed") for story in latest_stories
    )
    trader = next((item for item in trader_workers if item.get("status") == "running"), None)
    raw_trader_metadata = trader.get("metadata") if isinstance(trader, dict) else None
    trader_metadata = dict(raw_trader_metadata) if isinstance(raw_trader_metadata, dict) else {}
    raw_newsfeed_runtime = trader_metadata.get("engine_newsfeed")
    newsfeed_runtime = dict(raw_newsfeed_runtime) if isinstance(raw_newsfeed_runtime, dict) else {}
    newsfeed_health = build_engine_newsfeed_health(
        request.app.state.settings,
        newsfeed_runtime,
        engine_offset,
        newswire_active=bool(latest_stories and any(item.get("status") == "running" for item in newswire_workers)),
        latest_source_at_ms=int(latest_stories[0].get("last_updated_at_ms") or 0) if latest_stories else None,
    )
    worker_running = any(item.get("status") == "running" for item in newswire_workers)
    local_service = getattr(request.app.state, "newswire_service", None)
    local_status = local_service.status() if local_service is not None else {}
    local_running = bool(local_status.get("running"))
    result = {
        "enabled": bool(worker_running or request.app.state.settings.newswire_enabled),
        "running": bool(worker_running or local_running),
        "configured_for_api_role": request.app.state.settings.newswire_enabled,
        "owner_role": "newswire",
        "runtime_source": "newswire_heartbeat" if worker_running else "local_service",
        "latest_event": latest[0] if latest else None,
        "latest_story": latest_stories[0] if latest_stories else None,
        "story_sample_count": len(latest_stories),
        "feed_action_counts": dict(action_counts),
        "engine_action_counts": dict(engine_action_counts),
        "risk_states": risk_states,
        "workers": newswire_workers,
        "discord_publisher_workers": publisher_workers,
        "engine_newsfeed": {
            "runtime": newsfeed_runtime,
            "offset": engine_offset,
            "health": newsfeed_health,
        },
    }
    channel_id = request.app.state.settings.newswire_news_channel_id
    if channel_id and callable(getattr(repo, "newswire_delivery_status", None)):
        result["discord_delivery"] = await repo.newswire_delivery_status(channel_id)
    return result


@router.get("/newswire/readiness")
async def newswire_readiness(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _auth(request.app.state.settings, authorization)
    return await build_newswire_soak_readiness(
        request.app.state.repository,
        request.app.state.settings,
    )


@router.get("/newswire/calibration")
async def newswire_calibration(
    request: Request,
    limit: int = 2000,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _auth(request.app.state.settings, authorization)
    bounded_limit = max(1, min(5000, limit))
    try:
        rows = await request.app.state.repository.list_newswire_stories(limit=bounded_limit)
    except Exception:
        service = getattr(request.app.state, "newswire_service", None)
        rows = service.list_stories(limit=bounded_limit) if service is not None else []
    return {
        **build_calibration_report(rows),
        "generated_at_ms": _now_ms(),
        "query_limit": bounded_limit,
    }


@router.get("/newswire/feedback-summary")
async def newswire_feedback_summary(
    request: Request,
    cohort_start_ms: int | None = None,
    as_of_ms: int | None = None,
    source: str | None = None,
    score_bucket: str | None = None,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _auth(request.app.state.settings, authorization)
    start_ms = cohort_start_ms
    if start_ms is None:
        try:
            heartbeats = await request.app.state.repository.list_service_heartbeats(
                service_role="discord_publisher",
                limit=5,
            )
        except Exception:
            heartbeats = []
        current = next((item for item in heartbeats if item.get("status") == "running"), None)
        start_ms = int((current or {}).get("started_at_ms") or 0)
    return await build_newswire_feedback_summary(
        request.app.state.repository,
        cohort_start_ms=start_ms,
        as_of_ms=as_of_ms,
        source=source,
        score_bucket=score_bucket,
    )


@router.post("/newswire/reclassify")
async def newswire_reclassify(
    body: NewswireReclassifyRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _auth(request.app.state.settings, authorization)
    payload = body.model_dump(mode="json", exclude_none=True)
    try:
        command = await request.app.state.repository.enqueue_worker_command(
            target_role="newswire",
            command_type="newswire_reclassify",
            payload=payload,
            requested_by="api",
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"newswire command queue unavailable: {type(exc).__name__}") from None
    command_id = str(command.get("command_id") or "")
    return {
        "accepted": True,
        "command_id": command_id,
        "status_url": f"/commands/{command_id}",
        "target_role": "newswire",
        "command_type": "newswire_reclassify",
        "status": command.get("status"),
        "dry_run": body.dry_run,
        "execution_authority": "none",
        "publishes_to_live_bus": False,
    }


@router.post("/newswire/replay")
async def newswire_replay(
    body: NewswireReplayRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _auth(request.app.state.settings, authorization)
    if body.start_ms is not None and body.end_ms is not None and body.start_ms >= body.end_ms:
        raise HTTPException(status_code=422, detail="start_ms must be before end_ms")
    payload = body.model_dump(mode="json", exclude_none=True)
    try:
        command = await request.app.state.repository.enqueue_worker_command(
            target_role="trader",
            command_type="engine_newswire_replay",
            payload=payload,
            requested_by="api",
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"trader command queue unavailable: {type(exc).__name__}") from None
    command_id = str(command.get("command_id") or "")
    return {
        "accepted": True,
        "command_id": command_id,
        "status_url": f"/commands/{command_id}",
        "target_role": "trader",
        "command_type": "engine_newswire_replay",
        "status": command.get("status"),
        "dry_run": body.dry_run,
        "report_only": True,
        "execution_authority": "none",
        "live_consumer_offset_mutation": False,
    }


@router.post("/newswire/discord/test")
async def newswire_discord_test(
    body: NewswireDiscordTestRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _auth(request.app.state.settings, authorization)
    try:
        command = await request.app.state.repository.enqueue_worker_command(
            target_role="discord_publisher",
            command_type="discord_test",
            payload=body.model_dump(mode="json"),
            requested_by="api",
        )
        command_id = str(command.get("command_id") or "")
        return {"accepted": True, "command_id": command_id, "status_url": f"/commands/{command_id}", "target_role": "discord_publisher", "command_type": "discord_test", "status": command.get("status")}
    except Exception:
        publisher = getattr(request.app.state, "newswire_discord", None)
        if publisher is None or not callable(getattr(publisher, "send_test_message", None)):
            raise HTTPException(status_code=503, detail="newswire discord publisher is not configured")
        return await publisher.send_test_message(channel_id=body.channel_id, dry_run=body.dry_run)


@router.get("/newswire/decisions")
async def list_newswire_decisions(
    request: Request,
    event_id: str | None = None,
    policy_version: str | None = None,
    limit: int = 100,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _auth(request.app.state.settings, authorization)
    items = await request.app.state.repository.list_newswire_decisions(event_id=event_id, policy_version=policy_version, limit=max(1, min(limit, 1000)))
    return {"items": items, "count": len(items)}


@router.post("/newswire/evals")
async def record_newswire_eval(
    body: NewswireEvalRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _auth(request.app.state.settings, authorization)
    payload = body.model_dump(mode="json")
    payload["created_at_ms"] = _now_ms()
    try:
        eval_record = NewsEval(**payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    eval_id = await request.app.state.repository.record_newswire_eval(eval_record.model_dump(mode="json", exclude_none=True))
    return {"accepted": bool(eval_id), "eval_id": eval_id}


@router.get("/newswire/evals")
async def list_newswire_evals(
    request: Request,
    event_id: str | None = None,
    decision_id: str | None = None,
    limit: int = 100,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _auth(request.app.state.settings, authorization)
    items = await request.app.state.repository.list_newswire_evals(event_id=event_id, decision_id=decision_id, limit=max(1, min(limit, 1000)))
    return {"items": items, "count": len(items)}


@router.get("/newswire/rewards")
async def list_newswire_rewards(
    request: Request,
    event_id: str | None = None,
    policy_version: str | None = None,
    limit: int = 100,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _auth(request.app.state.settings, authorization)
    items = await request.app.state.repository.list_newswire_rewards(event_id=event_id, policy_version=policy_version, limit=max(1, min(limit, 1000)))
    return {"items": items, "count": len(items)}


@router.post("/newswire/rewards/build")
async def build_newswire_rewards(
    body: NewswireRewardBuildRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _auth(request.app.state.settings, authorization)
    repo = request.app.state.repository
    decisions = await repo.list_newswire_decisions(event_id=body.event_id, policy_version=body.policy_version, limit=max(1, min(body.limit, 5000)))
    rewards: list[dict[str, Any]] = []
    skipped = 0
    for decision in decisions:
        evals = await repo.list_newswire_evals(decision_id=decision.get("decision_id"), limit=100)
        if not evals:
            evals = await repo.list_newswire_evals(event_id=decision.get("event_id"), limit=100)
        if not evals:
            skipped += 1
            continue
        reward = build_reward(decision, evals)
        data = reward.model_dump(mode="json")
        await repo.record_newswire_reward(data)
        rewards.append(data)
    return {"built": len(rewards), "skipped": skipped, "items": rewards}


@router.get("/newswire/policies")
async def list_newswire_policies(
    request: Request,
    status: str | None = None,
    limit: int = 100,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _auth(request.app.state.settings, authorization)
    items = await request.app.state.repository.list_newswire_policy_versions(status=status, limit=max(1, min(limit, 1000)))
    return {"items": items, "count": len(items)}


@router.post("/newswire/policies/train")
async def train_newswire_policy(
    body: NewswirePolicyTrainRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _auth(request.app.state.settings, authorization)
    repo = request.app.state.repository
    limit = max(1, min(body.limit, 20_000))
    decisions = await repo.list_newswire_decisions(limit=limit)
    rewards = await repo.list_newswire_rewards(limit=limit)
    min_rows = body.min_rows if body.min_rows is not None else int(request.app.state.settings.newswire_policy_min_reward_rows)
    candidate = train_contextual_bandit_policy(decisions=decisions, rewards=rewards, min_rows=max(1, min_rows))
    data = candidate.model_dump(mode="json")
    await repo.upsert_newswire_policy_version(data)
    return data


@router.post("/newswire/policies/{policy_version}/promote")
async def promote_newswire_policy(
    policy_version: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _auth(request.app.state.settings, authorization)
    repo = request.app.state.repository
    policies = await repo.list_newswire_policy_versions(limit=1000)
    selected = next((item for item in policies if item.get("policy_version") == policy_version), None)
    if selected is None:
        raise HTTPException(status_code=404, detail="newswire policy version not found")
    replay_metrics = selected.get("replay_metrics") if isinstance(selected.get("replay_metrics"), dict) else {}
    if not bool(replay_metrics.get("ready")):
        raise HTTPException(status_code=409, detail="newswire policy replay guardrails have not passed")
    ok = await repo.promote_newswire_policy_version(policy_version, now_ms=_now_ms())
    if not ok:
        raise HTTPException(status_code=404, detail="newswire policy version not found")
    return {"promoted": True, "policy_version": policy_version}


@router.get("/newswire/sources")
async def newswire_sources(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    _auth(settings, authorization)
    sources = {
        "rss": {"enabled": bool(settings.newswire_rss_feed_urls), "feeds": len(settings.newswire_rss_feed_urls), "transport": "rss"},
        "alpaca": {"enabled": settings.alpaca_news_enabled, "transport": "websocket"},
        "trading_economics": {"enabled": settings.trading_economics_enabled, "transport": "websocket"},
        "x_curated": {"enabled": settings.x_newswire_enabled, "transport": "poll"},
    }
    return {"sources": sources, "source_scores": SOURCE_SCORES}


@router.websocket("/newswire/stream")
async def newswire_stream(websocket: WebSocket) -> None:
    settings: Settings = websocket.app.state.settings
    if not _ws_authorized(settings, websocket.query_params.get("token")):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    flt = await _read_filter_frame(websocket)
    last_ts = 0
    last_id: str | None = None
    try:
        while True:
            rows = await websocket.app.state.repository.list_newswire_story_revisions_after(
                last_event_ts_ms=last_ts,
                last_event_id=last_id,
                limit=100,
            )
            for row in rows:
                revision = NewswireStoryRevision.model_validate(row)
                event = revision.story.to_event(update_type=revision.update_type)
                last_ts = int(revision.emitted_at_ms)
                last_id = revision.revision_id
                if flt is None or flt.matches(event):
                    await websocket.send_json(event.model_dump(mode="json"))
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        return
    except Exception as exc:  # pragma: no cover - websocket runtime behavior
        log.warning("newswire_stream_failed", error=type(exc).__name__)
        await websocket.close(code=1011)


async def _read_filter_frame(websocket: WebSocket) -> NewswireFilter | None:
    """Optional first frame: {"filter": {...}}. Times out fast so a silent client streams all."""
    try:
        message = await asyncio.wait_for(websocket.receive_json(), timeout=2.0)
    except (TimeoutError, WebSocketDisconnect, ValueError):
        return None
    raw = message.get("filter") if isinstance(message, dict) else None
    if not isinstance(raw, dict):
        return None
    try:
        return NewswireFilter(**raw)
    except ValidationError:
        return None


def _now_ms() -> int:
    return int(time.time() * 1000)
