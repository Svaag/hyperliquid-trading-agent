from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from hyperliquid_trading_agent import __version__
from hyperliquid_trading_agent.app.agent.high_stakes.context import HighStakesContextBuilder
from hyperliquid_trading_agent.app.agent.high_stakes.graph import HighStakesDebateGraph
from hyperliquid_trading_agent.app.agent.high_stakes.roles import HighStakesRoleRunner
from hyperliquid_trading_agent.app.agent.high_stakes.schemas import TradeProposalRequest, TradeProposalResponse
from hyperliquid_trading_agent.app.agent.model_gateway import ModelGateway
from hyperliquid_trading_agent.app.agent.runner import AgentContext, TradingAgentRunner
from hyperliquid_trading_agent.app.agent.tools import AgentTools
from hyperliquid_trading_agent.app.config import Settings, load_settings
from hyperliquid_trading_agent.app.db.repository import Repository
from hyperliquid_trading_agent.app.db.session import create_engine, create_sessionmaker
from hyperliquid_trading_agent.app.discord_bot import DiscordTradingBot
from hyperliquid_trading_agent.app.hyperliquid.client import HyperliquidClient
from hyperliquid_trading_agent.app.hyperliquid.sdk_info_client import SDKInfoClient
from hyperliquid_trading_agent.app.hyperliquid.ws_worker import HyperliquidWebSocketWorker
from hyperliquid_trading_agent.app.logging import configure_logging, get_logger
from hyperliquid_trading_agent.app.metrics import SERVICE_INFO, UP
from hyperliquid_trading_agent.app.news.service import NewsService
from hyperliquid_trading_agent.app.tracking.alerts import DiscordAlertSink
from hyperliquid_trading_agent.app.tracking.service import PositionTrackingService

log = get_logger(__name__)


class AskRequest(BaseModel):
    prompt: str


class AskResponse(BaseModel):
    content: str
    refused: bool = False
    fallback_used: bool = False
    model_used: str | None = None
    tool_count: int = 0
    decision_run_id: str | None = None
    proposal_id: str | None = None
    high_stakes: bool = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    UP.set(1)
    SERVICE_INFO.info({"version": __version__, "environment": settings.environment})

    engine = create_engine(settings)
    sessionmaker = create_sessionmaker(engine)
    repository = Repository(sessionmaker)
    hyperliquid = HyperliquidClient(settings=settings)
    sdk_info = None if settings.high_stakes_info_provider == "rest_only" else SDKInfoClient(settings=settings)
    news = NewsService(settings=settings, repository=repository)
    tools = AgentTools(hyperliquid=hyperliquid, news=news, repository=repository)
    model_gateway = ModelGateway(settings=settings)
    high_stakes_context = HighStakesContextBuilder(tools=tools, settings=settings, sdk_info=sdk_info)
    high_stakes_roles = HighStakesRoleRunner(model_gateway=model_gateway, settings=settings)
    ws_worker = HyperliquidWebSocketWorker(settings=settings)
    tracking_service = PositionTrackingService(settings=settings, repository=repository, ws_worker=ws_worker)
    high_stakes_graph = HighStakesDebateGraph(
        settings=settings,
        context_builder=high_stakes_context,
        role_runner=high_stakes_roles,
        repository=repository,
        tracking_service=tracking_service,
    )
    runner = TradingAgentRunner(
        tools=tools,
        model_gateway=model_gateway,
        repository=repository,
        settings=settings,
        high_stakes_graph=high_stakes_graph,
    )
    bot = DiscordTradingBot(settings=settings, runner=runner, tracking_service=tracking_service)
    tracking_service.alert_sink = DiscordAlertSink(bot)

    app.state.engine = engine
    app.state.repository = repository
    app.state.hyperliquid = hyperliquid
    app.state.news = news
    app.state.sdk_info = sdk_info
    app.state.agent_runner = runner
    app.state.high_stakes_graph = high_stakes_graph
    app.state.discord_bot = bot
    app.state.ws_worker = ws_worker
    app.state.tracking_service = tracking_service

    bot_task: asyncio.Task | None = None
    ws_task: asyncio.Task | None = None
    tracking_task: asyncio.Task | None = None
    if settings.hyperliquid_ws_enabled or settings.position_tracking_enabled:
        ws_task = asyncio.create_task(ws_worker.start(), name="hyperliquid-ws")
        log.info("hyperliquid_ws_task_started")
    if settings.position_tracking_enabled:
        tracking_task = asyncio.create_task(tracking_service.start(), name="position-tracking")
        log.info("position_tracking_task_started")
    if settings.discord_bot_token:
        bot_task = asyncio.create_task(bot.start(), name="discord-bot")
        log.info("discord_bot_task_started")
    else:
        log.info("discord_bot_disabled", reason="DISCORD_BOT_TOKEN-not-set")
    try:
        yield
    finally:
        UP.set(0)
        await bot.stop()
        await tracking_service.stop()
        await ws_worker.stop()
        if sdk_info is not None:
            await sdk_info.close()
        await hyperliquid.close()
        await engine.dispose()
        for task in [bot_task, tracking_task, ws_task]:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level)
    app = FastAPI(title="Hyperliquid Trading Agent", version=__version__, lifespan=lifespan)
    app.state.settings = settings

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "service": settings.service_name, "version": __version__}

    @app.get("/ready")
    async def ready() -> dict[str, Any]:
        ready_checks: dict[str, Any] = {"discord_enabled": bool(settings.discord_bot_token)}
        try:
            await app.state.hyperliquid.all_mids()
            ready_checks["hyperliquid"] = "ok"
        except Exception as exc:
            ready_checks["hyperliquid"] = f"degraded:{type(exc).__name__}"
        if settings.position_tracking_enabled:
            tracking_status = app.state.tracking_service.status()
            ws_status = app.state.ws_worker.status()
            last_message_at = ws_status.get("last_message_at_ms")
            stale = tracking_status.get("active_count", 0) > 0 and (not last_message_at or int(time.time() * 1000) - int(last_message_at) > 120_000)
            ready_checks["position_tracking"] = "degraded:websocket_stale" if stale else "ok"
        return {"status": "ready", "checks": ready_checks}

    @app.get("/health/config")
    async def config_health() -> dict[str, Any]:
        gateway = ModelGateway(settings)
        attempts = gateway.configured_attempts()
        return {
            "environment": settings.environment,
            "hyperliquid_network": settings.hyperliquid_network,
            "hyperliquid_exchange_enabled": settings.hyperliquid_exchange_enabled,
            "hyperliquid_ws_enabled": settings.hyperliquid_ws_enabled,
            "models": [{"model": item.model, "provider": item.provider, "missing": item.missing_reason} for item in attempts],
            "position_tracking": _tracking_config_status(app),
            "high_stakes": {
                "enabled": settings.high_stakes_debate_enabled,
                "activation_policy": settings.high_stakes_activation_policy,
                "prompt_style": settings.high_stakes_prompt_style,
                "info_provider": settings.high_stakes_info_provider,
                "max_rounds": settings.high_stakes_max_rounds,
                "timeout_seconds": settings.high_stakes_timeout_seconds,
                "max_coins": settings.high_stakes_max_coins,
                "max_data_escalations": settings.high_stakes_max_data_escalations,
                "account_allowlist_count": len(settings.account_allowlist),
                "smart_money_watchlist_count": len(settings.smart_money_addresses),
                "model_contract": settings.debate_model_contract(),
                "roles": {
                    role: [
                        {"model": item.model, "provider": item.provider, "missing": item.missing_reason}
                        for item in gateway.configured_attempts_for_chain(settings.role_model_chain(role))
                    ]
                    for role in settings.debate_role_names
                },
            },
            "news_providers": {
                "rss_count": len(settings.rss_feed_urls),
                "tavily": bool(settings.tavily_api_key),
                "serpapi": bool(settings.serpapi_api_key),
                "newsapi": bool(settings.newsapi_api_key),
                "perplexity": bool(settings.perplexity_api_key),
                "x": bool(settings.x_bearer_token),
            },
        }

    @app.post("/ask", response_model=AskResponse)
    async def ask(request: AskRequest) -> AskResponse:
        runner: TradingAgentRunner = app.state.agent_runner
        response = await runner.answer(request.prompt, context=AgentContext(source="api"))
        return AskResponse(
            content=response.content,
            refused=response.refused,
            fallback_used=response.fallback_used,
            model_used=response.model_used,
            tool_count=len(response.tool_results),
            decision_run_id=response.decision_run_id,
            proposal_id=response.proposal_id,
            high_stakes=response.high_stakes,
        )

    @app.post("/trade/proposals", response_model=TradeProposalResponse)
    async def create_trade_proposal(request: TradeProposalRequest, authorization: str | None = Header(default=None)) -> TradeProposalResponse:
        _require_agent_api(settings, authorization)
        if not settings.high_stakes_debate_enabled:
            raise HTTPException(status_code=409, detail="high-stakes debate is disabled")
        graph: HighStakesDebateGraph = app.state.high_stakes_graph
        forced = request.model_copy(update={"force_debate": True, "dry_run": True})
        return await graph.run(forced, agent_context={"source": "api", "actor": "api"})

    @app.get("/trade/proposals/{proposal_id}")
    async def get_trade_proposal(proposal_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        repository: Repository = app.state.repository
        proposal = await repository.get_trade_proposal(proposal_id)
        if proposal is None:
            raise HTTPException(status_code=404, detail="trade proposal not found")
        return proposal

    @app.get("/tracking/positions")
    async def list_tracking_positions(
        status: str | None = None,
        coin: str | None = None,
        discord_thread_id: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        repository: Repository = app.state.repository
        items = await repository.list_position_trackers(status=status, coin=coin, discord_thread_id=discord_thread_id)
        return {"items": items, "count": len(items)}

    @app.get("/tracking/positions/{tracker_id}")
    async def get_tracking_position(tracker_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        repository: Repository = app.state.repository
        tracker = await repository.get_position_tracker(tracker_id)
        if tracker is None:
            raise HTTPException(status_code=404, detail="tracker not found")
        return tracker

    @app.get("/tracking/positions/{tracker_id}/events")
    async def get_tracking_events(tracker_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        repository: Repository = app.state.repository
        tracker = await repository.get_position_tracker(tracker_id)
        if tracker is None:
            raise HTTPException(status_code=404, detail="tracker not found")
        events = await repository.list_tracking_events(tracker_id)
        return {"items": events, "count": len(events)}

    @app.post("/tracking/positions/{tracker_id}/pause")
    async def pause_tracking_position(tracker_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        return await _set_tracker_status(app, settings, tracker_id, "paused", authorization)

    @app.post("/tracking/positions/{tracker_id}/resume")
    async def resume_tracking_position(tracker_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        return await _set_tracker_status(app, settings, tracker_id, "active", authorization)

    @app.post("/tracking/positions/{tracker_id}/stop")
    async def stop_tracking_position(tracker_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        return await _set_tracker_status(app, settings, tracker_id, "stopped", authorization)

    @app.get("/metrics")
    async def metrics(authorization: str | None = Header(default=None)):
        if settings.metrics_bearer_token:
            expected = f"Bearer {settings.metrics_bearer_token}"
            if authorization != expected:
                raise HTTPException(status_code=401, detail="metrics token required")
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


def _tracking_config_status(app: FastAPI) -> dict[str, Any]:
    settings: Settings = app.state.settings
    tracking_service = getattr(app.state, "tracking_service", None)
    ws_worker = getattr(app.state, "ws_worker", None)
    service_status = tracking_service.status() if tracking_service is not None else {}
    return {
        "enabled": settings.position_tracking_enabled,
        "auto_arm": settings.position_tracking_auto_arm,
        "price_source": settings.position_tracking_price_source,
        "default_ttl_hours": settings.position_tracking_default_ttl_hours,
        "rearm_band_bps": settings.position_tracking_rearm_band_bps,
        "reload_seconds": settings.position_tracking_reload_seconds,
        "max_active": settings.position_tracking_max_active,
        "service": service_status,
        "ws_status": ws_worker.status() if ws_worker is not None else {},
    }


async def _set_tracker_status(app: FastAPI, settings: Settings, tracker_id: str, status: str, authorization: str | None) -> dict[str, Any]:
    _require_agent_api(settings, authorization)
    repository: Repository = app.state.repository
    tracker = await repository.get_position_tracker(tracker_id)
    if tracker is None:
        raise HTTPException(status_code=404, detail="tracker not found")
    await repository.set_position_tracker_status(tracker_id, status, reason="api")
    tracking_service: PositionTrackingService = app.state.tracking_service
    await tracking_service.reload_active_trackers()
    updated = await repository.get_position_tracker(tracker_id)
    return {"status": status, "tracker": updated}


def _require_agent_api(settings: Settings, authorization: str | None) -> None:
    if settings.agent_api_bearer_token:
        expected = f"Bearer {settings.agent_api_bearer_token}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="agent API token required")
        return
    if settings.environment.lower() not in {"dev", "test", "local"}:
        raise HTTPException(status_code=503, detail="AGENT_API_BEARER_TOKEN must be set outside dev/test/local")


def main() -> None:
    settings = load_settings()
    uvicorn.run("hyperliquid_trading_agent.app.main:create_app", factory=True, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
