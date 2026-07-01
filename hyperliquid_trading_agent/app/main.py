from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Any, cast
from uuid import uuid4

import uvicorn
from alpaca.data.enums import DataFeed
from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import HTMLResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from hyperliquid_trading_agent import __version__
from hyperliquid_trading_agent.app.agent.high_stakes.context import HighStakesContextBuilder
from hyperliquid_trading_agent.app.agent.high_stakes.graph import HighStakesDebateGraph
from hyperliquid_trading_agent.app.agent.high_stakes.roles import HighStakesRoleRunner
from hyperliquid_trading_agent.app.agent.high_stakes.schemas import TradeProposalRequest
from hyperliquid_trading_agent.app.agent.model_gateway import ModelGateway
from hyperliquid_trading_agent.app.agent.runner import TradingAgentRunner
from hyperliquid_trading_agent.app.agent.tools import AgentTools
from hyperliquid_trading_agent.app.autonomy.discord import DiscordAutonomyAlertSink
from hyperliquid_trading_agent.app.autonomy.equity_features import EquitySignalGenerator
from hyperliquid_trading_agent.app.autonomy.evaluation import SignalEvaluationService
from hyperliquid_trading_agent.app.autonomy.event_evaluation import AlphaEventEvaluationService
from hyperliquid_trading_agent.app.autonomy.memory import MemoryService
from hyperliquid_trading_agent.app.autonomy.reports import AutonomyReportService
from hyperliquid_trading_agent.app.autonomy.schemas import OperatorFeedback
from hyperliquid_trading_agent.app.autonomy.service import AutonomousTradingLoopService
from hyperliquid_trading_agent.app.autonomy.tuning import TuningProposalService
from hyperliquid_trading_agent.app.config import ServiceRole, Settings, load_settings
from hyperliquid_trading_agent.app.dashboard import register_dashboard_routes
from hyperliquid_trading_agent.app.db.repository import Repository
from hyperliquid_trading_agent.app.db.session import create_engine, create_sessionmaker
from hyperliquid_trading_agent.app.discord_bot import DiscordTradingBot
from hyperliquid_trading_agent.app.discord_publish import SendOnlyDiscordClient, SendOnlyDiscordSink
from hyperliquid_trading_agent.app.engine.monitor import EngineValidationMonitorService
from hyperliquid_trading_agent.app.engine.newswire_bridge import EngineNewsConsumer
from hyperliquid_trading_agent.app.engine.pnl_loop import EnginePnLAttributionLoopService
from hyperliquid_trading_agent.app.engine.routes import register_engine_routes
from hyperliquid_trading_agent.app.engine.service import InstitutionalEngineService
from hyperliquid_trading_agent.app.governance.decision_context import DecisionContextRecorder
from hyperliquid_trading_agent.app.governance.review import ReviewWorkflowService
from hyperliquid_trading_agent.app.governance.risk_gateway import RiskGateway
from hyperliquid_trading_agent.app.governance.routes import register_governance_routes
from hyperliquid_trading_agent.app.governance.shadow import ShadowComparisonService
from hyperliquid_trading_agent.app.hip4.routes import register_hip4_routes
from hyperliquid_trading_agent.app.hip4.service import Hip4Service
from hyperliquid_trading_agent.app.hyperliquid.client import HyperliquidClient
from hyperliquid_trading_agent.app.hyperliquid.sdk_info_client import SDKInfoClient
from hyperliquid_trading_agent.app.hyperliquid.ws_worker import HyperliquidWebSocketWorker
from hyperliquid_trading_agent.app.liquidations.routes import register_liquidation_routes
from hyperliquid_trading_agent.app.liquidations.service import LiquidationService
from hyperliquid_trading_agent.app.liquidations.signals import LiquidationSignalBridge
from hyperliquid_trading_agent.app.logging import configure_logging, get_logger
from hyperliquid_trading_agent.app.metrics import SERVICE_INFO, UP
from hyperliquid_trading_agent.app.news.service import NewsService
from hyperliquid_trading_agent.app.newswire.consumers.agent_feed import AgentNewsConsumer
from hyperliquid_trading_agent.app.newswire.consumers.discord_news import DiscordNewsPublisher
from hyperliquid_trading_agent.app.newswire.enrich import Enricher
from hyperliquid_trading_agent.app.newswire.gateway import register_newswire_routes
from hyperliquid_trading_agent.app.newswire.service import NewswireService
from hyperliquid_trading_agent.app.orchestration.routes import register_orchestration_routes
from hyperliquid_trading_agent.app.orchestration.wave_supervisor import WaveSupervisor
from hyperliquid_trading_agent.app.runtime_commands import COMMAND_REGISTRY, command_registry_json
from hyperliquid_trading_agent.app.tracking.alerts import DiscordAlertSink
from hyperliquid_trading_agent.app.tracking.service import PositionTrackingService
from hyperliquid_trading_agent.app.tradfi.alpaca_provider import AlpacaTradFiProvider
from hyperliquid_trading_agent.app.tradfi.client import TradFiClient
from hyperliquid_trading_agent.app.tradfi.options_flow import FlowEnricher, OptionsFlowDetector
from hyperliquid_trading_agent.app.tradfi.paper.simulator import EquityPaperSimulator
from hyperliquid_trading_agent.app.world_model.adapters import WorldModelAdapterService
from hyperliquid_trading_agent.app.world_model.routes import register_world_model_routes
from hyperliquid_trading_agent.app.world_model.service import WorldModelService
from hyperliquid_trading_agent.app.world_model.streams import WorldModelStreamService

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


class AutonomyActionRequest(BaseModel):
    actor: str = "api"
    reason: str = ""


class AutonomyFeedbackRequest(BaseModel):
    target_type: str = "signal"
    target_id: str
    rating: str
    note: str = ""
    actor_id: str | None = None
    metadata: dict[str, Any] = {}


class CandidatePromotionRequest(BaseModel):
    human_review_confirmed: bool = False
    reviewer: str = "api"
    change_control_id: str = ""
    approved_for_role_injection_roles: list[str] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    dashboard_only = settings.service_role == ServiceRole.API
    world_model_live = False
    restricted_runtime = True
    UP.set(1)
    SERVICE_INFO.info({"version": __version__, "environment": settings.environment})

    engine = create_engine(settings)
    sessionmaker = create_sessionmaker(engine)
    repository = Repository(sessionmaker)
    # Liquidation flow monitor: independent of the trading runtime profiles — it is
    # a public observability surface, gated only by its own feature flag.
    liquidation_service = LiquidationService(settings, sessionmaker) if settings.liquidations_enabled else None
    liquidation_signal_bridge = LiquidationSignalBridge(liquidation_service) if liquidation_service is not None else None
    decision_context_recorder = DecisionContextRecorder(settings=settings, repository=repository, code_version=__version__)
    await decision_context_recorder.snapshot_startup()
    hyperliquid = HyperliquidClient(settings=settings)
    sdk_info = None if restricted_runtime or settings.high_stakes_info_provider == "rest_only" else SDKInfoClient(settings=settings)
    news = NewsService(settings=settings, repository=repository)
    model_gateway = ModelGateway(settings=settings)
    shadow_service = ShadowComparisonService(repository=repository)
    review_service = ReviewWorkflowService(repository=repository, shadow_service=shadow_service)
    world_model_service = WorldModelService(settings=settings, repository=repository)

    tradfi_client: TradFiClient | None = None
    options_flow_detector: OptionsFlowDetector | None = None
    flow_enricher: FlowEnricher | None = None
    equity_paper: EquityPaperSimulator | None = None
    equity_signal_generator: EquitySignalGenerator | None = None
    if settings.tradfi_enabled and not restricted_runtime:
        if settings.alpaca_api_key and settings.alpaca_api_secret:
            try:
                provider = AlpacaTradFiProvider(
                    api_key=settings.alpaca_api_key,
                    api_secret=settings.alpaca_api_secret,
                    feed=DataFeed(settings.alpaca_data_feed),
                )
                tradfi_client = TradFiClient(provider)
                await tradfi_client.start()
                log.info("tradfi_client_started", provider=provider.name, feed=settings.alpaca_data_feed)
            except Exception as exc:
                log.warning("tradfi_client_start_failed", error=type(exc).__name__)
        else:
            log.warning("tradfi_disabled_missing_alpaca_keys")
    if tradfi_client is not None:
        options_flow_detector = OptionsFlowDetector(
            min_volume_oi_ratio=settings.options_flow_min_volume_oi_ratio,
            min_premium=settings.options_flow_min_premium,
        )
        if settings.options_flow_llm_enrich_enabled:
            flow_enricher = FlowEnricher(
                model_gateway=model_gateway,
                max_calls_per_hour=settings.options_flow_llm_enrich_max_calls_per_hour,
            )
        equity_paper = EquityPaperSimulator(
            initial_equity_usd=settings.autonomy_equity_paper_initial_equity_usd,
            risk_pct_per_trade=settings.autonomy_equity_paper_risk_pct_per_trade,
            max_gross_leverage=settings.autonomy_equity_paper_max_gross_leverage,
            max_single_name_exposure_pct=settings.autonomy_equity_paper_max_single_name_exposure_pct,
            taker_fee_bps=settings.autonomy_equity_paper_taker_fee_bps,
            maker_fee_bps=settings.autonomy_equity_paper_maker_fee_bps,
            default_slippage_bps=settings.autonomy_equity_paper_default_slippage_bps,
            tradfi_client=tradfi_client,
            repository=repository,
        )
        equity_signal_generator = EquitySignalGenerator(
            min_signal_score=settings.autonomy_equity_min_signal_score,
            max_signals_per_day=settings.autonomy_equity_max_signals_per_day,
            signal_ttl_minutes=settings.autonomy_equity_signal_ttl_minutes,
            flow_detector=options_flow_detector,
        )

    tools = AgentTools(
        hyperliquid=hyperliquid,
        news=news,
        repository=repository,
        tradfi=tradfi_client,
        options_flow=options_flow_detector,
    )
    memory_service = MemoryService(settings=settings, repository=repository)
    evaluation_service = SignalEvaluationService(settings=settings, repository=repository, memory_service=memory_service, world_model_service=world_model_service)
    event_evaluation_service = AlphaEventEvaluationService(settings=settings, repository=repository, memory_service=memory_service, world_model_service=world_model_service)
    tuning_service = TuningProposalService(settings=settings, repository=repository, memory_service=memory_service)
    report_service = AutonomyReportService(
        settings=settings,
        repository=repository,
        evaluation_service=evaluation_service,
        event_evaluation_service=event_evaluation_service,
        memory_service=memory_service,
        tuning_service=tuning_service,
    )
    high_stakes_context = HighStakesContextBuilder(tools=tools, settings=settings, sdk_info=sdk_info, world_model_service=world_model_service)
    high_stakes_roles = HighStakesRoleRunner(model_gateway=model_gateway, settings=settings, memory_service=memory_service, world_model_service=world_model_service)
    ws_worker = HyperliquidWebSocketWorker(settings=settings)
    tracking_service = PositionTrackingService(settings=settings, repository=repository, ws_worker=ws_worker)
    risk_gateway = RiskGateway(settings=settings, repository=repository, decision_context_recorder=decision_context_recorder)
    hip4_service = Hip4Service(settings=settings, repository=repository, hyperliquid=hyperliquid, ws_worker=ws_worker, risk_gateway=risk_gateway, world_model_service=world_model_service)
    autonomy_service = AutonomousTradingLoopService(
        settings=settings,
        repository=repository,
        hyperliquid=hyperliquid,
        news=news,
        ws_worker=ws_worker,
        model_gateway=model_gateway,
        evaluation_service=evaluation_service,
        event_evaluation_service=event_evaluation_service,
        memory_service=memory_service,
        report_service=report_service,
        tuning_service=tuning_service,
        tradfi=tradfi_client,
        equity_portfolio=equity_paper,
        equity_signal_generator=equity_signal_generator,
        options_flow=options_flow_detector,
        flow_enricher=flow_enricher,
        decision_context_recorder=decision_context_recorder,
        risk_gateway=risk_gateway,
        world_model_service=world_model_service,
    )
    engine_service = InstitutionalEngineService(
        settings=settings,
        repository=repository,
        hyperliquid=hyperliquid,
        risk_gateway=risk_gateway,
        portfolio_service=autonomy_service.portfolio,
        world_model_service=world_model_service,
        liquidation_bridge=liquidation_signal_bridge,
    )
    autonomy_service.engine_service = engine_service
    report_service.portfolio_service = autonomy_service.portfolio
    report_service.equity_portfolio_service = equity_paper
    high_stakes_graph = HighStakesDebateGraph(
        settings=settings,
        context_builder=high_stakes_context,
        role_runner=high_stakes_roles,
        repository=repository,
        tracking_service=tracking_service,
        decision_context_recorder=decision_context_recorder,
    )
    runner = TradingAgentRunner(
        tools=tools,
        model_gateway=model_gateway,
        repository=repository,
        settings=settings,
        high_stakes_graph=high_stakes_graph,
    )
    bot = DiscordTradingBot(settings=settings, runner=runner, tracking_service=tracking_service, autonomy_service=autonomy_service)
    tracking_service.alert_sink = DiscordAlertSink(bot)
    autonomy_alert_sink = DiscordAutonomyAlertSink(bot)
    hip4_service.alert_sink = autonomy_alert_sink
    autonomy_service.alert_sink = autonomy_alert_sink
    report_service.alert_sink = autonomy_alert_sink

    engine_validation_monitor = EngineValidationMonitorService(settings=settings, repository=repository, engine_service=engine_service, alert_sink=autonomy_alert_sink)
    engine_pnl_attribution = EnginePnLAttributionLoopService(settings=settings, repository=repository, hyperliquid=hyperliquid)
    newswire_service = NewswireService(settings=settings, repository=repository)
    engine_news_consumer = EngineNewsConsumer(settings=settings, bus=newswire_service.bus, engine_service=engine_service)
    newswire_enricher = Enricher(settings=settings, model_gateway=model_gateway)
    newswire_discord_client = SendOnlyDiscordClient(token=settings.discord_bot_token) if world_model_live else None
    newswire_alert_sink = SendOnlyDiscordSink(newswire_discord_client) if newswire_discord_client is not None else autonomy_alert_sink
    newswire_discord = DiscordNewsPublisher(
        settings=settings,
        bus=newswire_service.bus,
        alert_sink=newswire_alert_sink,
        enricher=newswire_enricher,
        repository=repository,
    )
    newswire_agent_consumer = AgentNewsConsumer(
        settings=settings,
        bus=newswire_service.bus,
        autonomy_service=None if world_model_live else autonomy_service,
        repository=repository,
        event_evaluation_service=None if world_model_live else event_evaluation_service,
        world_model_service=world_model_service,
    )
    world_model_adapter_service = WorldModelAdapterService(settings=settings, world_model_service=world_model_service)
    world_model_stream_service = WorldModelStreamService(settings=settings, world_model_service=world_model_service)
    wave_supervisor = WaveSupervisor(settings=settings, repository=repository, engine_service=engine_service)

    app.state.engine = engine
    app.state.repository = repository
    app.state.liquidation_service = liquidation_service
    app.state.decision_context_recorder = decision_context_recorder
    app.state.hyperliquid = hyperliquid
    app.state.news = news
    app.state.sdk_info = sdk_info
    app.state.agent_runner = runner
    app.state.high_stakes_graph = high_stakes_graph
    app.state.discord_bot = bot
    app.state.ws_worker = ws_worker
    app.state.tracking_service = tracking_service
    app.state.hip4_service = hip4_service
    app.state.autonomy_service = autonomy_service
    app.state.engine_service = engine_service
    app.state.engine_validation_monitor = engine_validation_monitor
    app.state.engine_pnl_attribution = engine_pnl_attribution
    app.state.evaluation_service = evaluation_service
    app.state.event_evaluation_service = event_evaluation_service
    app.state.memory_service = memory_service
    app.state.report_service = report_service
    app.state.tuning_service = tuning_service
    app.state.shadow_service = shadow_service
    app.state.review_service = review_service
    app.state.world_model_service = world_model_service
    app.state.world_model_adapter_service = world_model_adapter_service
    app.state.world_model_stream_service = world_model_stream_service
    app.state.newswire_service = newswire_service
    app.state.engine_news_consumer = engine_news_consumer
    app.state.newswire_discord = newswire_discord
    app.state.newswire_discord_client = newswire_discord_client
    app.state.tradfi_client = tradfi_client
    app.state.options_flow_detector = options_flow_detector
    app.state.flow_enricher = flow_enricher
    app.state.equity_paper = equity_paper
    app.state.equity_signal_generator = equity_signal_generator
    app.state.wave_supervisor = wave_supervisor

    bot_task: asyncio.Task | None = None
    newswire_discord_task: asyncio.Task | None = None
    ws_task: asyncio.Task | None = None
    tracking_task: asyncio.Task | None = None
    autonomy_task: asyncio.Task | None = None
    if not restricted_runtime and (settings.hyperliquid_ws_enabled or settings.position_tracking_enabled or settings.autonomy_enabled or (settings.hip4_enabled and settings.hip4_ws_enabled)):
        ws_task = asyncio.create_task(ws_worker.start(), name="hyperliquid-ws")
        log.info("hyperliquid_ws_task_started")
    if settings.position_tracking_enabled and not restricted_runtime:
        tracking_task = asyncio.create_task(tracking_service.start(), name="position-tracking")
        log.info("position_tracking_task_started")
    if settings.autonomy_enabled and not restricted_runtime:
        autonomy_task = asyncio.create_task(autonomy_service.start(), name="autonomy-service")
        log.info("autonomy_service_task_started")
    if not restricted_runtime:
        await hip4_service.start()
    if settings.discord_bot_token and settings.environment.lower() != "test" and not restricted_runtime:
        bot_task = asyncio.create_task(bot.start(), name="discord-bot")
        log.info("discord_bot_task_started")
    else:
        if settings.environment.lower() == "test":
            reason = "test-environment"
        elif restricted_runtime:
            reason = f"{settings.runtime_profile}-restricted-runtime"
        else:
            reason = "DISCORD_BOT_TOKEN-not-set"
        log.info("discord_bot_disabled", reason=reason)
    if not restricted_runtime:
        await engine_validation_monitor.start()
        await engine_pnl_attribution.start()
    if settings.newswire_enabled and not dashboard_only:
        # Subscribe consumers before adapters start so no early events are missed.
        if world_model_live and newswire_discord_client is not None and settings.discord_bot_token and settings.environment.lower() != "test":
            newswire_discord_task = asyncio.create_task(newswire_discord_client.start(), name="discord-news-send-only")
            ready = await newswire_discord_client.wait_until_ready(timeout=30)
            log.info("newswire_send_only_discord_started", ready=ready)
        await engine_news_consumer.start()
        await newswire_discord.start()
        await newswire_agent_consumer.start()
        await newswire_service.start()
        log.info("newswire_started")
    if settings.world_model_streams_enabled and not dashboard_only:
        await world_model_stream_service.start()
    if settings.orchestration_wave_supervisor_enabled and not restricted_runtime:
        await wave_supervisor.start()
    if liquidation_service is not None:
        await liquidation_service.start()
        log.info("liquidation_service_task_started")
    try:
        yield
    finally:
        UP.set(0)
        if liquidation_service is not None:
            await liquidation_service.stop()
        if settings.orchestration_wave_supervisor_enabled and not restricted_runtime:
            await wave_supervisor.stop()
        if not restricted_runtime:
            await bot.stop()
        if settings.world_model_streams_enabled and not dashboard_only:
            await world_model_stream_service.stop()
        if settings.newswire_enabled and not dashboard_only:
            await newswire_service.stop()
            await newswire_discord.stop()
            await newswire_agent_consumer.stop()
            await engine_news_consumer.stop()
            if newswire_discord_client is not None:
                await newswire_discord_client.stop()
        if not restricted_runtime:
            await engine_pnl_attribution.stop()
            await engine_validation_monitor.stop()
            await hip4_service.stop()
            await autonomy_service.stop()
            await tracking_service.stop()
            await ws_worker.stop()
        if sdk_info is not None:
            await sdk_info.close()
        if tradfi_client is not None:
            await tradfi_client.close()
        await hyperliquid.close()
        await engine.dispose()
        for task in [bot_task, newswire_discord_task, autonomy_task, tracking_task, ws_task]:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    if settings.service_role != ServiceRole.API:
        raise RuntimeError(f"FastAPI app may only run with SERVICE_ROLE=api, got {settings.service_role!s}")
    configure_logging(settings.log_level)
    app = FastAPI(title="Hyperliquid Trading Agent", version=__version__, lifespan=lifespan)
    app.state.settings = settings

    register_governance_routes(app, settings, _require_agent_api)
    register_dashboard_routes(app, settings, _require_agent_api)
    register_world_model_routes(app, settings, _require_agent_api)
    register_liquidation_routes(app, settings, _require_agent_api)

    @app.get("/", response_class=HTMLResponse)
    async def root() -> HTMLResponse:
        return HTMLResponse(_root_html(settings))

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "service": settings.service_name, "version": __version__}

    @app.get("/ready")
    async def ready() -> dict[str, Any]:
        ready_checks: dict[str, Any] = {
            "service_role": str(settings.service_role),
            "runtime_profile": settings.runtime_profile,
            "discord_enabled": False,
        }
        health = await app.state.world_model_service.repository_health()
        ready_checks["world_model_repository"] = "ok" if health.get("ping", {}).get("ok") else f"degraded:{health.get('ping', {}).get('error')}"
        if settings.runtime_profile == "world_model_live":
            ready_checks["newswire"] = "disabled" if not settings.newswire_enabled else "worker-owned"
            ready_checks["world_model_streams"] = "disabled" if not settings.world_model_streams_enabled else "worker-owned"
        try:
            heartbeats = await app.state.repository.list_service_heartbeats(limit=25) if app.state.repository.enabled else []
            ready_checks["worker_heartbeats"] = len(heartbeats)
        except Exception as exc:
            ready_checks["worker_heartbeats"] = f"degraded:{type(exc).__name__}"
        return {"status": "ready", "checks": ready_checks}

    @app.get("/health/config")
    async def config_health() -> dict[str, Any]:
        gateway = ModelGateway(settings)
        attempts = gateway.configured_attempts()
        return {
            "service_role": str(settings.service_role),
            "runtime_profile": settings.runtime_profile,
            "environment": settings.environment,
            "hyperliquid_network": settings.hyperliquid_network,
            "hyperliquid_exchange_enabled": settings.hyperliquid_exchange_enabled,
            "hyperliquid_ws_enabled": settings.hyperliquid_ws_enabled,
            "models": [{"model": item.model, "provider": item.provider, "missing": item.missing_reason} for item in attempts],
            "position_tracking": _tracking_config_status(app),
            "hip4": _hip4_config_status(app),
            "autonomy": _autonomy_config_status(app),
            "tradfi": _tradfi_config_status(app),
            "engine": {
                "enabled": settings.engine_enabled,
                "mode": settings.engine_mode,
                "execution_modes": settings.engine_execution_mode_list,
                "paper_enabled": settings.engine_paper_enabled,
                "shadow_enabled": settings.engine_shadow_enabled,
                "live_enabled": settings.engine_live_enabled,
                "debate_enabled": settings.engine_debate_enabled,
                "debate_priority_min": settings.engine_debate_priority_min,
                "min_net_ev_bps": settings.engine_min_net_ev_bps,
                "min_risk_adjusted_utility": settings.engine_min_risk_adjusted_utility,
            },
            "orchestration": {
                "wave_supervisor": app.state.wave_supervisor.status() if getattr(app.state, "wave_supervisor", None) is not None else {
                    "enabled": settings.orchestration_wave_supervisor_enabled,
                    "running": False,
                    "handoff_repo": settings.orchestration_wave_supervisor_handoff_repo,
                }
            },
            "world_model": app.state.world_model_service.status() if getattr(app.state, "world_model_service", None) is not None else {},
            "world_model_streams": app.state.world_model_stream_service.status() if getattr(app.state, "world_model_stream_service", None) is not None else {},
            "high_stakes": {
                "enabled": settings.high_stakes_debate_enabled,
                "activation_policy": settings.high_stakes_activation_policy,
                "prompt_style": settings.high_stakes_prompt_style,
                "info_provider": settings.high_stakes_info_provider,
                "max_rounds": settings.high_stakes_max_rounds,
                "timeout_seconds": settings.high_stakes_timeout_seconds,
                "review_concurrency": settings.high_stakes_review_concurrency,
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
            "newswire": {
                "enabled": settings.newswire_enabled,
                "discord_enabled": settings.newswire_discord_enabled,
                "news_channel_configured": settings.newswire_news_channel_configured,
                "rss_feed_count": len(settings.newswire_rss_feed_urls),
                "alpaca_news_enabled": settings.alpaca_news_enabled,
                "trading_economics_enabled": settings.trading_economics_enabled,
                "x_curated_enabled": settings.x_newswire_enabled,
                "symbols_universe": settings.newswire_symbols_universe,
                "llm_enrich_enabled": settings.newswire_llm_enrich_enabled,
                "thresholds": {
                    "news_min_importance": settings.newswire_news_min_importance,
                    "breaking_min_importance": settings.newswire_breaking_min_importance,
                    "agent_min_importance": settings.newswire_agent_min_importance,
                    "digest_interval_seconds": settings.newswire_digest_interval_seconds,
                    "discord_digest_max_items": settings.newswire_discord_digest_max_items,
                    "discord_startup_grace_seconds": settings.newswire_discord_startup_grace_seconds,
                },
                "warnings": settings.newswire_config_warnings(),
                "service": app.state.newswire_service.status() if getattr(app.state, "newswire_service", None) is not None else {},
                "discord_publisher": await app.state.newswire_discord.status_async() if getattr(app.state, "newswire_discord", None) is not None else {},
            },
        }

    @app.get("/runtime/dashboard", response_class=HTMLResponse)
    async def runtime_dashboard() -> HTMLResponse:
        return HTMLResponse(_runtime_dashboard_html())

    @app.get("/runtime/dashboard/data")
    async def runtime_dashboard_data(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        heartbeats = _annotate_heartbeats(settings, await app.state.repository.list_service_heartbeats(limit=100))
        commands = _annotate_commands(settings, await app.state.repository.list_worker_commands(limit=500))
        offsets = _annotate_offsets(await app.state.repository.list_consumer_offsets(limit=100))
        return {
            "runtime": {"service_role": str(settings.service_role), "runtime_profile": settings.runtime_profile, "environment": settings.environment},
            "heartbeats": {"items": heartbeats, "count": len(heartbeats), "stale_count": len([item for item in heartbeats if item.get("stale") is True])},
            "commands": {"items": commands[:100], "count": len(commands), "counts": _command_counts(commands)},
            "offsets": {"items": offsets, "count": len(offsets)},
            "registry": command_registry_json(),
            "command_health": _command_health(heartbeats),
        }

    @app.get("/runtime/heartbeats")
    async def runtime_heartbeats(service_role: str | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        items = _annotate_heartbeats(settings, await app.state.repository.list_service_heartbeats(service_role=service_role, limit=100))
        return {"items": items, "count": len(items), "stale_count": len([item for item in items if item.get("stale") is True])}

    @app.get("/runtime/status")
    async def runtime_status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        heartbeats = _annotate_heartbeats(settings, await app.state.repository.list_service_heartbeats(limit=100))
        return {
            "service_role": str(settings.service_role),
            "runtime_profile": settings.runtime_profile,
            "workers": heartbeats,
            "worker_count": len(heartbeats),
            "stale_worker_count": len([item for item in heartbeats if item.get("stale") is True]),
            "heartbeat_stale_seconds": settings.service_heartbeat_stale_seconds,
        }

    @app.get("/runtime/command-registry")
    async def runtime_command_registry(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        items = command_registry_json()
        return {"items": items, "count": len(items)}

    @app.get("/runtime/command-health")
    async def runtime_command_health(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        heartbeats = _annotate_heartbeats(settings, await app.state.repository.list_service_heartbeats(limit=100))
        return _command_health(heartbeats)

    @app.get("/runtime/offsets")
    async def runtime_offsets(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        items = _annotate_offsets(await app.state.repository.list_consumer_offsets(limit=100))
        return {"items": items, "count": len(items)}

    @app.get("/runtime/offsets/{consumer_name}")
    async def runtime_offset(consumer_name: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        return _annotate_offsets([await app.state.repository.get_consumer_offset(consumer_name)])[0]

    @app.get("/commands")
    async def list_commands(target_role: str | None = None, status: str | None = None, command_type: str | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        items = _annotate_commands(settings, await app.state.repository.list_worker_commands(target_role=target_role, status=status, command_type=command_type, limit=100))
        return {"items": items, "count": len(items), "counts": _command_counts(items)}

    @app.get("/commands/{command_id}")
    async def get_command(command_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        command = await app.state.repository.get_worker_command(command_id)
        if command is None:
            raise HTTPException(status_code=404, detail="command not found")
        return _annotate_commands(settings, [command])[0]

    @app.post("/commands/{command_id}/retry", status_code=202)
    async def retry_command(command_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        command = await app.state.repository.retry_worker_command(command_id, requested_by="api")
        if command is None:
            raise HTTPException(status_code=404, detail="command not found")
        return _accepted_command(command)

    @app.post("/commands/{command_id}/cancel")
    async def cancel_command(command_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        command = await app.state.repository.cancel_worker_command(command_id, cancelled_by="api")
        if command is None:
            raise HTTPException(status_code=404, detail="command not found")
        return _annotate_commands(settings, [command])[0]

    @app.post("/ask", status_code=202)
    async def ask(request: AskRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        command = await app.state.repository.enqueue_worker_command(
            target_role=ServiceRole.AGENT.value,
            command_type="ask",
            payload=request.model_dump(mode="json"),
            requested_by="api",
            idempotency_key=None,
        )
        return _accepted_command(command)

    @app.post("/trade/proposals", status_code=202)
    async def create_trade_proposal(request: TradeProposalRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        if not settings.high_stakes_debate_enabled:
            raise HTTPException(status_code=409, detail="high-stakes trade proposals are disabled")
        command = await app.state.repository.enqueue_worker_command(
            target_role=ServiceRole.AGENT.value,
            command_type="trade_proposal",
            payload=request.model_dump(mode="json"),
            requested_by="api",
            idempotency_key=None,
        )
        return _accepted_command(command)

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

    @app.post("/tracking/positions/{tracker_id}/pause", status_code=202)
    async def pause_tracking_position(tracker_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        command = await app.state.repository.enqueue_worker_command(target_role="trader", command_type="tracking_pause", payload={"tracker_id": tracker_id}, requested_by="api")
        return _accepted_command(command)

    @app.post("/tracking/positions/{tracker_id}/resume", status_code=202)
    async def resume_tracking_position(tracker_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        command = await app.state.repository.enqueue_worker_command(target_role="trader", command_type="tracking_resume", payload={"tracker_id": tracker_id}, requested_by="api")
        return _accepted_command(command)

    @app.post("/tracking/positions/{tracker_id}/stop", status_code=202)
    async def stop_tracking_position(tracker_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        command = await app.state.repository.enqueue_worker_command(target_role="trader", command_type="tracking_stop", payload={"tracker_id": tracker_id}, requested_by="api")
        return _accepted_command(command)

    @app.get("/autonomy/status")
    async def autonomy_status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        return app.state.autonomy_service.status()

    @app.post("/autonomy/pause")
    async def pause_autonomy(request: AutonomyActionRequest | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        command = await app.state.repository.enqueue_worker_command(target_role="trader", command_type="autonomy_pause", payload=(request.model_dump(mode="json") if request else {}), requested_by="api")
        return _accepted_command(command)

    @app.post("/autonomy/resume")
    async def resume_autonomy(request: AutonomyActionRequest | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        command = await app.state.repository.enqueue_worker_command(target_role="trader", command_type="autonomy_resume", payload=(request.model_dump(mode="json") if request else {}), requested_by="api")
        return _accepted_command(command)

    @app.get("/autonomy/universe")
    async def autonomy_universe(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        service = app.state.autonomy_service
        return {"items": [asset.model_dump(mode="json") for asset in service.universe], "count": len(service.universe), "resolver": service.universe_resolver.status()}

    @app.get("/autonomy/market-map")
    async def autonomy_market_map(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        return app.state.autonomy_service.reducer.snapshot().model_dump(mode="json")

    @app.get("/autonomy/market-map/{symbol}")
    async def autonomy_market_map_symbol(symbol: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        state = app.state.autonomy_service.reducer.snapshot().assets.get(symbol.upper())
        if state is None:
            raise HTTPException(status_code=404, detail="symbol not found")
        return state.model_dump(mode="json")

    @app.get("/autonomy/signals")
    async def autonomy_signals(status: str | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        items = [item.model_dump(mode="json") for item in app.state.autonomy_service.list_signals(status=status)]
        return {"items": items, "count": len(items)}

    @app.get("/autonomy/signals/{signal_id}")
    async def autonomy_signal(signal_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        signal = await app.state.autonomy_service._get_signal(signal_id)
        if signal is None:
            raise HTTPException(status_code=404, detail="signal not found")
        return signal.model_dump(mode="json")

    @app.post("/autonomy/signals/{signal_id}/approve")
    async def approve_autonomy_signal(signal_id: str, request: AutonomyActionRequest | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        command = await app.state.repository.enqueue_worker_command(target_role="trader", command_type="autonomy_signal_approve", payload={"signal_id": signal_id, **(request.model_dump(mode="json") if request else {})}, requested_by="api")
        return _accepted_command(command)

    @app.post("/autonomy/signals/{signal_id}/reject")
    async def reject_autonomy_signal(signal_id: str, request: AutonomyActionRequest | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        command = await app.state.repository.enqueue_worker_command(target_role="trader", command_type="autonomy_signal_reject", payload={"signal_id": signal_id, **(request.model_dump(mode="json") if request else {})}, requested_by="api")
        return _accepted_command(command)

    @app.post("/autonomy/signals/{signal_id}/expire")
    async def expire_autonomy_signal(signal_id: str, request: AutonomyActionRequest | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        command = await app.state.repository.enqueue_worker_command(target_role="trader", command_type="autonomy_signal_expire", payload={"signal_id": signal_id, **(request.model_dump(mode="json") if request else {})}, requested_by="api")
        return _accepted_command(command)

    @app.post("/admin/debug/seed-flip-demo")
    async def seed_flip_demo(request: AutonomyActionRequest | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        command = await app.state.repository.enqueue_worker_command(target_role="trader", command_type="admin_debug_seed_flip_demo", payload=(request.model_dump(mode="json") if request else {}), requested_by="api")
        return _accepted_command(command)

    @app.get("/autonomy/portfolio")
    async def autonomy_portfolio(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        service = app.state.autonomy_service
        return {
            "portfolio": service.portfolio.portfolio.model_dump(mode="json") if service.portfolio.portfolio else None,
            "latest_snapshot": service.portfolio.latest_snapshot().model_dump(mode="json") if service.portfolio.latest_snapshot() else None,
        }

    @app.get("/autonomy/portfolio/snapshots")
    async def autonomy_portfolio_snapshots(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        items = [item.model_dump(mode="json") for item in app.state.autonomy_service.portfolio.snapshots[-200:]]
        return {"items": items, "count": len(items)}

    @app.get("/autonomy/positions")
    async def autonomy_positions(status: str | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        positions = list(app.state.autonomy_service.portfolio.positions.values())
        if status:
            positions = [item for item in positions if item.status == status]
        return {"items": [item.model_dump(mode="json") for item in positions], "count": len(positions)}

    @app.get("/autonomy/orders")
    async def autonomy_orders(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        orders = list(app.state.autonomy_service.portfolio.orders.values())
        return {"items": [item.model_dump(mode="json") for item in orders], "count": len(orders)}

    @app.get("/autonomy/fills")
    async def autonomy_fills(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        fills = list(app.state.autonomy_service.portfolio.fills.values())
        return {"items": [item.model_dump(mode="json") for item in fills], "count": len(fills)}

    @app.get("/tradfi/status")
    async def tradfi_status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        return _tradfi_config_status(app)

    @app.get("/tradfi/quote/{symbol}")
    async def tradfi_quote(symbol: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        client = _require_tradfi_client(app)
        quote = await client.get_latest_quote(symbol)
        trade = await client.get_latest_trade(symbol)
        return {
            "symbol": symbol.upper(),
            "quote": quote.model_dump(mode="json") if quote else None,
            "latest_trade": trade.model_dump(mode="json") if trade else None,
        }

    @app.get("/tradfi/snapshots")
    async def tradfi_snapshots(symbols: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        client = _require_tradfi_client(app)
        symbol_list = [item.strip().upper() for item in symbols.split(",") if item.strip()]
        if not symbol_list:
            raise HTTPException(status_code=400, detail="symbols query parameter is required")
        snaps = await client.get_snapshots(symbol_list[:50])
        return {"items": {sym: snap.model_dump(mode="json") for sym, snap in snaps.items()}, "count": len(snaps)}

    @app.get("/tradfi/bars/{symbol}")
    async def tradfi_bars(
        symbol: str,
        timeframe: str = "1d",
        lookback_hours: int = 120,
        limit: int | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        client = _require_tradfi_client(app)
        bars = await client.get_bars(symbol, timeframe=timeframe, lookback_hours=lookback_hours, limit=limit)
        return {"symbol": symbol.upper(), "timeframe": timeframe, "items": [bar.model_dump(mode="json") for bar in bars], "count": len(bars)}

    @app.get("/tradfi/corporate-actions/{symbol}")
    async def tradfi_corporate_actions(
        symbol: str,
        start: str | None = None,
        end: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        client = _require_tradfi_client(app)
        actions = await client.get_corporate_actions([symbol], start=_parse_optional_date(start), end=_parse_optional_date(end))
        items = actions.get(symbol.upper(), [])
        return {"symbol": symbol.upper(), "items": [item.model_dump(mode="json") for item in items], "count": len(items)}

    @app.get("/tradfi/calendar")
    async def tradfi_calendar(
        start: str | None = None,
        end: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        client = _require_tradfi_client(app)
        start_date = _parse_optional_date(start) or date.today()
        end_date = _parse_optional_date(end) or (start_date + timedelta(days=30))
        events = await client.get_calendar(start_date, end_date)
        return {"items": [event.model_dump(mode="json") for event in events], "count": len(events)}

    @app.get("/tradfi/options/{symbol}/chain")
    async def tradfi_options_chain(
        symbol: str,
        expiration: str | None = None,
        strike_min: float | None = None,
        strike_max: float | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        client = _require_tradfi_client(app)
        chain = await client.get_options_chain(symbol, expiration=_parse_optional_date(expiration), strike_min=strike_min, strike_max=strike_max)
        return chain.model_dump(mode="json")

    @app.get("/tradfi/options/{symbol}/flow")
    async def tradfi_options_flow(symbol: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        client = _require_tradfi_client(app)
        detector = getattr(app.state, "options_flow_detector", None)
        if detector is None:
            raise HTTPException(status_code=409, detail="options flow detector is not configured")
        chain = await client.get_options_chain(symbol)
        events = detector.detect(chain)
        enricher = getattr(app.state, "flow_enricher", None)
        if enricher is not None:
            for event in events[:3]:
                enrichment = await enricher.maybe_enrich(event)
                if enrichment:
                    event.enrichment = enrichment
        return {
            "symbol": symbol.upper(),
            "underlying_price": chain.underlying_price,
            "contracts_scanned": len(chain.contracts),
            "items": [event.model_dump(mode="json") for event in events],
            "count": len(events),
        }

    @app.get("/autonomy/equity/signals")
    async def autonomy_equity_signals(status: str | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        items = [item.model_dump(mode="json") for item in app.state.autonomy_service.list_equity_signals(status=status)]
        return {"items": items, "count": len(items)}

    @app.post("/autonomy/equity/signals/{signal_id}/approve")
    async def approve_autonomy_equity_signal(signal_id: str, request: AutonomyActionRequest | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        command = await app.state.repository.enqueue_worker_command(target_role="trader", command_type="autonomy_equity_signal_approve", payload={"signal_id": signal_id, **(request.model_dump(mode="json") if request else {})}, requested_by="api")
        return _accepted_command(command)

    @app.post("/autonomy/equity/signals/{signal_id}/reject")
    async def reject_autonomy_equity_signal(signal_id: str, request: AutonomyActionRequest | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        command = await app.state.repository.enqueue_worker_command(target_role="trader", command_type="autonomy_equity_signal_reject", payload={"signal_id": signal_id, **(request.model_dump(mode="json") if request else {})}, requested_by="api")
        return _accepted_command(command)

    @app.get("/autonomy/equity/portfolio")
    async def autonomy_equity_portfolio(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        equity_paper = getattr(app.state.autonomy_service, "equity_portfolio", None)
        if equity_paper is None:
            raise HTTPException(status_code=409, detail="equity paper portfolio is not configured")
        latest = equity_paper.snapshots[-1] if equity_paper.snapshots else equity_paper.snapshot()
        return {"portfolio": equity_paper.portfolio.model_dump(mode="json"), "latest_snapshot": latest.model_dump(mode="json")}

    @app.get("/autonomy/equity/positions")
    async def autonomy_equity_positions(status: str | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        equity_paper = getattr(app.state.autonomy_service, "equity_portfolio", None)
        if equity_paper is None:
            raise HTTPException(status_code=409, detail="equity paper portfolio is not configured")
        positions = list(equity_paper.positions.values())
        if status:
            positions = [item for item in positions if item.status == status]
        return {"items": [item.model_dump(mode="json") for item in positions], "count": len(positions)}

    @app.get("/autonomy/equity/orders")
    async def autonomy_equity_orders(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        equity_paper = getattr(app.state.autonomy_service, "equity_portfolio", None)
        if equity_paper is None:
            raise HTTPException(status_code=409, detail="equity paper portfolio is not configured")
        orders = list(equity_paper.orders.values())
        return {"items": [item.model_dump(mode="json") for item in orders], "count": len(orders)}

    @app.get("/autonomy/equity/fills")
    async def autonomy_equity_fills(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        equity_paper = getattr(app.state.autonomy_service, "equity_portfolio", None)
        if equity_paper is None:
            raise HTTPException(status_code=409, detail="equity paper portfolio is not configured")
        fills = list(equity_paper.fills.values())
        return {"items": [item.model_dump(mode="json") for item in fills], "count": len(fills)}

    @app.get("/autonomy/news")
    async def autonomy_news(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        events = sorted(app.state.autonomy_service.news_events.values(), key=lambda item: item.observed_at_ms, reverse=True)
        return {"items": [item.model_dump(mode="json") for item in events[:200]], "count": len(events)}

    @app.get("/autonomy/evaluations/signals")
    async def autonomy_signal_evaluations(status: str | None = None, symbol: str | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        items = await app.state.evaluation_service.list_evaluations(status=status, symbol=symbol, limit=200)
        return {"items": [item.model_dump(mode="json") for item in items], "count": len(items)}

    @app.get("/autonomy/evaluations/signals/{signal_id}")
    async def autonomy_signal_evaluation(signal_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        item = await app.state.evaluation_service.get_by_signal_id(signal_id)
        if item is None:
            raise HTTPException(status_code=404, detail="signal evaluation not found")
        return item.model_dump(mode="json")

    @app.post("/autonomy/evaluations/run")
    async def autonomy_evaluations_run(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        command = await app.state.repository.enqueue_worker_command(target_role="scheduler", command_type="autonomy_evaluations_run", payload={}, requested_by="api")
        return _accepted_command(command)

    @app.post("/autonomy/evaluations/backfill")
    async def autonomy_evaluations_backfill(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        command = await app.state.repository.enqueue_worker_command(target_role="scheduler", command_type="autonomy_evaluations_backfill", payload={}, requested_by="api")
        return _accepted_command(command)

    @app.get("/autonomy/evaluations/events")
    async def autonomy_event_evaluations(status: str | None = None, symbol: str | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        items = await app.state.event_evaluation_service.list_evaluations(status=status, symbol=symbol, limit=200)
        return {"items": [item.model_dump(mode="json") for item in items], "count": len(items)}

    @app.get("/autonomy/evaluations/events/by-event/{event_id}")
    async def autonomy_event_evaluation_by_event(event_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        items = await app.state.event_evaluation_service.get_by_event_id(event_id)
        if not items:
            raise HTTPException(status_code=404, detail="event evaluation not found")
        return {"items": [item.model_dump(mode="json") for item in items], "count": len(items)}

    @app.get("/autonomy/evaluations/events/{evaluation_id}")
    async def autonomy_event_evaluation(evaluation_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        item = await app.state.event_evaluation_service.get(evaluation_id)
        if item is None:
            raise HTTPException(status_code=404, detail="event evaluation not found")
        return item.model_dump(mode="json")

    @app.post("/autonomy/evaluations/events/backfill")
    async def autonomy_event_evaluations_backfill(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        command = await app.state.repository.enqueue_worker_command(target_role="scheduler", command_type="autonomy_event_evaluations_backfill", payload={}, requested_by="api")
        return _accepted_command(command)

    @app.get("/autonomy/reports/daily")
    async def autonomy_daily_reports(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        items = await app.state.report_service.list_reports("daily", limit=30)
        return {"items": items, "count": len(items)}

    @app.get("/autonomy/reports/daily/{report_date}")
    async def autonomy_daily_report(report_date: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        item = await app.state.report_service.get_report("daily", report_date)
        if item is None:
            raise HTTPException(status_code=404, detail="daily report not found")
        return item

    @app.post("/autonomy/reports/daily/run")
    async def autonomy_daily_report_run(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        command = await app.state.repository.enqueue_worker_command(target_role="scheduler", command_type="autonomy_daily_report_run", payload={}, requested_by="api")
        return _accepted_command(command)

    @app.get("/autonomy/reports/weekly")
    async def autonomy_weekly_reports(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        items = await app.state.report_service.list_reports("weekly", limit=30)
        return {"items": items, "count": len(items)}

    @app.get("/autonomy/reports/weekly/{week_key}")
    async def autonomy_weekly_report(week_key: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        item = await app.state.report_service.get_report("weekly", week_key)
        if item is None:
            raise HTTPException(status_code=404, detail="weekly report not found")
        return item

    @app.post("/autonomy/reports/weekly/run")
    async def autonomy_weekly_report_run(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        command = await app.state.repository.enqueue_worker_command(target_role="scheduler", command_type="autonomy_weekly_report_run", payload={}, requested_by="api")
        return _accepted_command(command)

    @app.get("/autonomy/token-capital")
    async def autonomy_token_capital(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        latest = getattr(app.state.report_service, "latest_token_capital", None)
        if latest is not None:
            return latest.model_dump(mode="json")
        history = await app.state.report_service.token_capital_history(limit=1)
        if history:
            return history[0]
        fallback = app.state.report_service.scorer.compute(
            window="daily",
            timestamp_ms=int(time.time() * 1000),
            evaluations=[],
            portfolio_snapshot=None,
            event_evaluations=[],
            memory_counts={},
            feedback_items=[],
            reliability={"no_persisted_report": True},
        )
        return fallback.model_dump(mode="json")

    @app.get("/autonomy/token-capital/history")
    async def autonomy_token_capital_history(window: str | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        items = await app.state.report_service.token_capital_history(window=window, limit=100)
        return {"items": items, "count": len(items)}

    @app.get("/autonomy/memory/observations")
    async def autonomy_memory_observations(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        items = await app.state.memory_service.list_observations(limit=200)
        return {"items": items, "count": len(items)}

    @app.get("/autonomy/memory/candidates")
    async def autonomy_memory_candidates(status: str | None = None, role: str | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        items = await app.state.memory_service.list_candidates(status=status, role=role, limit=200)
        return {"items": items, "count": len(items)}

    @app.get("/autonomy/memory/shadow")
    async def autonomy_memory_shadow(role: str | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        items = await app.state.memory_service.list_lessons(role=role, status="shadow", include_shadow=True, limit=200)
        return {"items": items, "count": len(items)}

    @app.get("/autonomy/memory/lessons")
    async def autonomy_memory_lessons(role: str | None = None, status: str | None = "active", authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        items = await app.state.memory_service.list_lessons(role=role, status=status, include_shadow=False, limit=200)
        return {"items": items, "count": len(items)}

    @app.get("/autonomy/memory/lessons/{lesson_id}")
    async def autonomy_memory_lesson(lesson_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        item = await app.state.memory_service.get_lesson(lesson_id)
        if item is None:
            raise HTTPException(status_code=404, detail="lesson not found")
        return item

    @app.post("/autonomy/memory/lessons/{lesson_id}/archive")
    async def autonomy_memory_lesson_archive(lesson_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        await app.state.memory_service.archive_lesson(lesson_id)
        return {"status": "archived", "lesson_id": lesson_id}

    @app.post("/autonomy/memory/candidates/{candidate_id}/reject")
    async def autonomy_memory_candidate_reject(candidate_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        await app.state.memory_service.reject_candidate(candidate_id)
        return {"status": "rejected", "candidate_id": candidate_id}

    @app.post("/autonomy/memory/candidates/{candidate_id}/promote-shadow")
    async def autonomy_memory_candidate_promote_shadow(candidate_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        lesson = await app.state.memory_service.promote_candidate_to_shadow(candidate_id)
        if lesson is None:
            raise HTTPException(status_code=404, detail="candidate not found or cannot become role memory")
        return lesson.model_dump(mode="json")

    @app.post("/autonomy/memory/candidates/{candidate_id}/promote-active")
    async def autonomy_memory_candidate_promote_active(candidate_id: str, request: CandidatePromotionRequest | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        try:
            lesson = await app.state.memory_service.promote_candidate_to_active(
                candidate_id,
                human_review_confirmed=bool(request and request.human_review_confirmed),
                change_control_id=(request.change_control_id if request else ""),
                approved_for_role_injection_roles=(request.approved_for_role_injection_roles if request else []),
                reviewer=(request.reviewer if request else "api"),
            )
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        if lesson is None:
            raise HTTPException(status_code=404, detail="candidate not found or cannot become active memory")
        return lesson.model_dump(mode="json")

    @app.post("/autonomy/feedback")
    async def autonomy_feedback(request: AutonomyFeedbackRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        feedback = OperatorFeedback(id=f"fb_{uuid4().hex}", source="api", actor_id=request.actor_id, target_type=cast(Any, request.target_type), target_id=request.target_id, rating=cast(Any, request.rating), note=request.note, created_at_ms=int(time.time() * 1000), metadata=request.metadata)
        candidate = await app.state.memory_service.record_feedback(feedback)
        return {"feedback": feedback.model_dump(mode="json"), "candidate": candidate.model_dump(mode="json") if candidate else None}

    @app.get("/autonomy/feedback")
    async def autonomy_feedback_list(target_type: str | None = None, target_id: str | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        items = await app.state.repository.list_operator_feedback(target_type=target_type, target_id=target_id, limit=200)
        return {"items": items, "count": len(items)}

    @app.get("/autonomy/tuning-proposals")
    async def autonomy_tuning_proposals(status: str | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        items = await app.state.tuning_service.list(status=status, limit=200)
        return {"items": items, "count": len(items), "auto_apply_enabled": False}

    @app.get("/autonomy/tuning-proposals/{proposal_id}")
    async def autonomy_tuning_proposal(proposal_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        item = await app.state.tuning_service.get(proposal_id)
        if item is None:
            raise HTTPException(status_code=404, detail="tuning proposal not found")
        return {**item, "auto_apply_enabled": False}

    @app.post("/autonomy/tuning-proposals/{proposal_id}/mark-reviewed")
    async def autonomy_tuning_proposal_reviewed(proposal_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        await app.state.tuning_service.mark_reviewed(proposal_id)
        return {"status": "accepted_manually", "proposal_id": proposal_id, "auto_apply_enabled": False}

    @app.post("/autonomy/tuning-proposals/{proposal_id}/reject")
    async def autonomy_tuning_proposal_reject(proposal_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        await app.state.tuning_service.reject(proposal_id)
        return {"status": "rejected", "proposal_id": proposal_id, "auto_apply_enabled": False}

    @app.post("/autonomy/tuning-proposals/{proposal_id}/expire")
    async def autonomy_tuning_proposal_expire(proposal_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_agent_api(settings, authorization)
        await app.state.tuning_service.expire(proposal_id)
        return {"status": "expired", "proposal_id": proposal_id, "auto_apply_enabled": False}

    @app.get("/metrics")
    async def metrics(authorization: str | None = Header(default=None)):
        if settings.metrics_bearer_token:
            expected = f"Bearer {settings.metrics_bearer_token}"
            if authorization != expected:
                raise HTTPException(status_code=401, detail="metrics token required")
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    register_engine_routes(app, settings, _require_agent_api)
    register_orchestration_routes(app, settings, _require_agent_api)
    register_hip4_routes(app, settings, _require_agent_api)
    register_newswire_routes(app)
    return app


def _annotate_heartbeats(settings: Settings, heartbeats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now_ms = int(time.time() * 1000)
    stale_after_ms = max(1, int(settings.service_heartbeat_stale_seconds)) * 1000
    annotated: list[dict[str, Any]] = []
    for heartbeat in heartbeats:
        item = dict(heartbeat)
        updated_at_ms = item.get("updated_at_ms")
        age_ms: int | None = None
        stale = False
        if isinstance(updated_at_ms, int):
            age_ms = max(0, now_ms - updated_at_ms)
            stale = age_ms > stale_after_ms and str(item.get("status") or "").lower() not in {"stopping", "stopped"}
        item["heartbeat_age_ms"] = age_ms
        item["stale"] = stale
        annotated.append(item)
    return annotated


def _annotate_commands(settings: Settings, commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now_ms = int(time.time() * 1000)
    stale_after_ms = max(1, int(settings.worker_command_claim_stale_seconds)) * 1000
    annotated: list[dict[str, Any]] = []
    for command in commands:
        item = dict(command)
        requested_at_ms = item.get("requested_at_ms")
        claimed_at_ms = item.get("claimed_at_ms")
        item["age_ms"] = max(0, now_ms - requested_at_ms) if isinstance(requested_at_ms, int) else None
        item["claimed_age_ms"] = max(0, now_ms - claimed_at_ms) if isinstance(claimed_at_ms, int) else None
        item["claim_stale"] = bool(item.get("status") == "claimed" and isinstance(claimed_at_ms, int) and now_ms - claimed_at_ms > stale_after_ms)
        spec = COMMAND_REGISTRY.get(str(item.get("command_type") or ""))
        if spec is not None:
            item["registry"] = {"target_role": spec.target_role.value, "handler_name": spec.handler_name, "paper_state_mutation": spec.paper_state_mutation, "external_side_effect": spec.external_side_effect}
        else:
            item["registry"] = None
        annotated.append(item)
    return annotated


def _annotate_offsets(offsets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now_ms = int(time.time() * 1000)
    annotated: list[dict[str, Any]] = []
    for offset in offsets:
        item = dict(offset)
        updated_at_ms = item.get("updated_at_ms")
        item["offset_age_ms"] = max(0, now_ms - updated_at_ms) if isinstance(updated_at_ms, int) and updated_at_ms > 0 else None
        annotated.append(item)
    return annotated


def _command_counts(commands: list[dict[str, Any]]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    by_role: dict[str, int] = {}
    by_type: dict[str, int] = {}
    stale_claims = 0
    for command in commands:
        status = str(command.get("status") or "unknown")
        role = str(command.get("target_role") or "unknown")
        command_type = str(command.get("command_type") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
        by_role[role] = by_role.get(role, 0) + 1
        by_type[command_type] = by_type.get(command_type, 0) + 1
        if command.get("claim_stale") is True:
            stale_claims += 1
    return {"by_status": by_status, "by_role": by_role, "by_type": by_type, "stale_claims": stale_claims}


def _command_health(heartbeats: list[dict[str, Any]]) -> dict[str, Any]:
    live_roles = {str(item.get("service_role")): item for item in heartbeats if item.get("stale") is not True and str(item.get("status") or "") == "running"}
    optional_roles = {ServiceRole.DISCORD_PUBLISHER.value, ServiceRole.DISCORD_BOT.value, ServiceRole.LIQUIDATIONS.value}
    roles: dict[str, dict[str, Any]] = {}
    for spec in COMMAND_REGISTRY.values():
        role = spec.target_role.value
        if role in live_roles:
            worker_state = "running"
        elif role in optional_roles:
            worker_state = "optional_missing"
        else:
            worker_state = "missing"
        role_state = roles.setdefault(role, {"target_role": role, "worker_state": worker_state, "required_default": role not in optional_roles, "commands": []})
        role_state["commands"].append({"command_type": spec.command_type, "handler_name": spec.handler_name, "handler_status": spec.handler_status})
    missing_roles = sorted(role for role, data in roles.items() if data["worker_state"] == "missing")
    optional_missing_roles = sorted(role for role, data in roles.items() if data["worker_state"] == "optional_missing")
    return {"roles": sorted(roles.values(), key=lambda item: str(item["target_role"])), "missing_roles": missing_roles, "optional_missing_roles": optional_missing_roles, "registered_command_count": len(COMMAND_REGISTRY)}


def _runtime_dashboard_html() -> str:
    return r"""
<!doctype html><html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/><title>Runtime Dashboard</title>
<style>:root{color-scheme:dark;--bg:#0b1020;--panel:#121a2f;--muted:#94a3b8;--text:#e2e8f0;--ok:#22c55e;--warn:#f59e0b;--bad:#ef4444;--accent:#38bdf8}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui}header{padding:18px 22px;border-bottom:1px solid #1f2a44;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}main{padding:22px;display:grid;gap:14px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}.panel{background:var(--panel);border:1px solid #1f2a44;border-radius:14px;padding:14px;overflow:auto}.metric{font-size:28px;font-weight:800}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.muted{color:var(--muted)}input,button,select{background:#0f172a;color:var(--text);border:1px solid #334155;border-radius:8px;padding:8px}button{cursor:pointer}table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid #243149;padding:7px;text-align:left;vertical-align:top}pre{white-space:pre-wrap;background:#0f172a;border-radius:10px;padding:10px}.pill{border-radius:999px;background:#1e293b;padding:2px 8px}.tabs button{margin-right:6px;margin-bottom:6px}.tab{display:none}.tab.active{display:block}</style></head>
<body><header><div><h1>Runtime Dashboard</h1><div class="muted">Workers, commands, offsets, and command registry.</div></div><div><input id="token" type="password" placeholder="Bearer token"/><button onclick="saveToken()">Save</button><button onclick="load()">Refresh</button></div></header>
<main><section class="grid" id="summary"></section><div class="tabs"><button onclick="show('workers')">Workers</button><button onclick="show('commands')">Commands</button><button onclick="show('offsets')">Offsets</button><button onclick="show('registry')">Registry</button><button onclick="show('raw')">Raw</button></div><section id="workers" class="tab active panel"></section><section id="commands" class="tab panel"></section><section id="offsets" class="tab panel"></section><section id="registry" class="tab panel"></section><section id="raw" class="tab panel"><pre id="rawpre"></pre></section></main>
<script>
const $=id=>document.getElementById(id);function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]))}function headers(){const t=localStorage.getItem('agentToken')||'';return t?{'Authorization':'Bearer '+t}:{}}function saveToken(){localStorage.setItem('agentToken',$('token').value.trim());load()}function show(id){document.querySelectorAll('.tab').forEach(e=>e.classList.remove('active'));$(id).classList.add('active')}function metric(k,v,cls=''){return `<div class="panel"><div class="muted">${esc(k)}</div><div class="metric ${cls}">${esc(v)}</div></div>`}function table(items,cols){if(!items||!items.length)return '<div class="muted">No rows.</div>';return '<table><thead><tr>'+cols.map(c=>`<th>${esc(c[0])}</th>`).join('')+'</tr></thead><tbody>'+items.map(r=>'<tr>'+cols.map(c=>`<td>${c[2]?c[2](r):esc(r[c[1]])}</td>`).join('')+'</tr>').join('')+'</tbody></table>'}function fmtMs(ms){return ms==null?'':(ms/1000).toFixed(0)+'s'}async function api(p){const r=await fetch(p,{headers:headers()});if(!r.ok)throw new Error(r.status+' '+await r.text());return await r.json()}async function retry(id){await fetch('/commands/'+id+'/retry',{method:'POST',headers:headers()});load()}async function cancelCmd(id){await fetch('/commands/'+id+'/cancel',{method:'POST',headers:headers()});load()}async function load(){try{$('token').value=localStorage.getItem('agentToken')||'';const d=await api('/runtime/dashboard/data');const cc=d.commands.counts||{};$('summary').innerHTML=[metric('Workers',d.heartbeats.count,(d.heartbeats.stale_count?'bad':'ok')),metric('Stale',d.heartbeats.stale_count,d.heartbeats.stale_count?'bad':'ok'),metric('Commands',d.commands.count),metric('Failed',(cc.by_status||{}).failed||0,((cc.by_status||{}).failed?'bad':'ok')),metric('Offsets',d.offsets.count),metric('Missing roles',(d.command_health.missing_roles||[]).length,(d.command_health.missing_roles||[]).length?'warn':'ok')].join('');$('workers').innerHTML=table(d.heartbeats.items,[['Role','service_role'],['Status','status'],['Stale','stale',r=>r.stale?'<span class="bad">true</span>':'<span class="ok">false</span>'],['Age','heartbeat_age_ms',r=>fmtMs(r.heartbeat_age_ms)],['Errors','metadata',r=>esc(JSON.stringify((r.metadata||{}).world_model?.repository_last_error||(r.metadata||{}).newswire?.adapter_errors||''))]]);$('commands').innerHTML='<h2>Counts</h2><pre>'+esc(JSON.stringify(cc,null,2))+'</pre>'+table(d.commands.items,[['ID','command_id'],['Role','target_role'],['Type','command_type'],['Status','status'],['Age','age_ms',r=>fmtMs(r.age_ms)],['Error','last_error'],['Actions','command_id',r=>`<button onclick="retry('${esc(r.command_id)}')">Retry</button> <button onclick="cancelCmd('${esc(r.command_id)}')">Cancel</button>`]]);$('offsets').innerHTML=table(d.offsets.items,[['Consumer','consumer_name'],['Source','source_table'],['Last event','last_event_id'],['Event ts','last_event_ts_ms'],['Offset age','offset_age_ms',r=>fmtMs(r.offset_age_ms)]]);$('registry').innerHTML='<h2>Missing roles</h2><pre>'+esc(JSON.stringify(d.command_health.missing_roles,null,2))+'</pre>'+table(d.registry,[['Command','command_type'],['Role','target_role'],['Handler','handler_name'],['Side effect','external_side_effect'],['Paper mutation','paper_state_mutation']]);$('rawpre').textContent=JSON.stringify(d,null,2)}catch(e){$('summary').innerHTML=`<div class="panel bad"><b>Load failed</b><pre>${esc(e.message)}</pre></div>`}}load();
</script></body></html>
""".strip()


def _accepted_command(command: dict[str, Any]) -> dict[str, Any]:
    command_id = str(command.get("command_id") or "")
    return {
        "accepted": True,
        "command_id": command_id,
        "status_url": f"/commands/{command_id}",
        "target_role": command.get("target_role"),
        "command_type": command.get("command_type"),
        "status": command.get("status"),
    }


def _root_html(settings: Settings) -> str:
    links = [
        ("Unified trading dashboard", "/dashboard", "Engine readiness, Regime tab, validation, PnL, and governance."),
        ("World Model dashboard", "/world-model/dashboard", "Beliefs, events, prediction-market signals, memory, and supervision."),
        ("Health", "/health", "Liveness probe."),
        ("Readiness", "/ready", "Runtime readiness checks."),
        ("Config health", "/health/config", "Redacted subsystem configuration and warnings."),
        ("Newswire status", "/newswire/status", "Newswire adapter and bus status."),
        ("Newswire events", "/newswire/events", "Recent normalized newsfeed events."),
        ("Engine status", "/engine/status", "Institutional engine runtime status."),
        ("Latest BTC regime", "/engine/regime/latest?primary_asset=BTC", "Most recent engine RegimeVector snapshot."),
        ("BTC regime history", "/engine/regime/history?primary_asset=BTC&limit=500", "Time-series regime snapshots for dashboard charting."),
        ("OpenAPI docs", "/docs", "Interactive FastAPI documentation."),
    ]
    items = "".join(f'<a class="card" href="{href}"><b>{title}</b><span>{description}</span><code>{href}</code></a>' for title, href, description in links)
    return f"""
<!doctype html><html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/><title>Hyperliquid Trading Agent</title>
<style>:root{{color-scheme:dark;--bg:#0b1020;--panel:#121a2f;--line:#1f2a44;--muted:#94a3b8;--text:#e2e8f0;--accent:#38bdf8}}body{{margin:0;font-family:Inter,ui-sans-serif,system-ui;background:var(--bg);color:var(--text)}}main{{max-width:1100px;margin:0 auto;padding:32px 20px}}h1{{margin:0 0 8px;font-size:34px}}.muted{{color:var(--muted)}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;margin-top:22px}}.card{{display:flex;flex-direction:column;gap:8px;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px;color:var(--text);text-decoration:none}}.card:hover{{border-color:var(--accent)}}.card span{{color:var(--muted)}}code{{color:var(--accent);font-size:12px;word-break:break-all}}.pill{{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:4px 10px;margin-right:8px;color:var(--muted)}}</style></head>
<body><main><h1>Hyperliquid Trading Agent</h1><p class="muted">Informational index for dashboards and operational API surfaces.</p><p><span class="pill">version {__version__}</span><span class="pill">role {settings.service_role}</span><span class="pill">profile {settings.runtime_profile}</span><span class="pill">env {settings.environment}</span></p><section class="grid">{items}</section><p class="muted">Some API links require the configured agent bearer token. The dashboards include a token field in their header.</p></main></body></html>
""".strip()


def _autonomy_learning_degraded(autonomy_status: dict[str, Any], now_ms: int) -> bool:
    evaluation = autonomy_status.get("evaluation") or {}
    event_evaluation = autonomy_status.get("event_evaluation") or {}
    reports = autonomy_status.get("reports") or {}
    memory = autonomy_status.get("memory") or {}
    open_evaluations = int(evaluation.get("open_evaluations") or 0)
    last_mark_at = evaluation.get("last_mark_at_ms")
    if open_evaluations > 0 and last_mark_at and now_ms - int(last_mark_at) > 2 * 60 * 60 * 1000:
        return True
    if int(evaluation.get("error_count") or 0) >= 5:
        return True
    if int(event_evaluation.get("error_count") or 0) >= 5:
        return True
    if int(reports.get("error_count") or 0) >= 3:
        return True
    if int(memory.get("error_count") or 0) >= 5:
        return True
    return False


def _hip4_config_status(app: FastAPI) -> dict[str, Any]:
    settings: Settings = app.state.settings
    service = getattr(app.state, "hip4_service", None)
    service_status = service.status() if service is not None and callable(getattr(service, "status", None)) else {}
    return {
        "enabled": settings.hip4_enabled,
        "mode": settings.hip4_mode,
        "scan_enabled": settings.hip4_scan_enabled,
        "paper_execution_enabled": settings.hip4_paper_execution_enabled,
        "manual_ticket_export_enabled": settings.hip4_manual_ticket_export_enabled,
        "question_allowlist_count": len(settings.hip4_question_allowlist_ids),
        "mode_allows_scan": settings.hip4_mode_allows_scan,
        "mode_allows_paper": settings.hip4_mode_allows_paper,
        "mode_allows_manual_ticket": settings.hip4_mode_allows_manual_ticket,
        "warnings": settings.hip4_config_warnings(),
        "service": service_status,
        "safety": {
            "signing_enabled": False,
            "private_keys_enabled": False,
            "exchange_mutation_enabled": False,
            "live_orders_enabled": False,
            "llm_controlled_execution_enabled": False,
        },
    }


def _autonomy_config_status(app: FastAPI) -> dict[str, Any]:
    settings: Settings = app.state.settings
    service = getattr(app.state, "autonomy_service", None)
    service_status = service.status() if service is not None and callable(getattr(service, "status", None)) else {}
    return {
        "enabled": settings.autonomy_enabled,
        "mode": settings.autonomy_mode,
        "alert_channel_id_configured": settings.autonomy_alert_channel_configured,
        "require_human_signoff": settings.autonomy_require_human_signoff,
        "admin_user_count": len(settings.autonomy_admin_users),
        "admin_role_count": len(settings.autonomy_admin_roles),
        "universe": {
            "core_symbols": settings.autonomy_core_symbols,
            "top_n_perps": settings.autonomy_universe_top_n_perps,
            "max_tracked_assets": settings.autonomy_max_tracked_assets,
            "max_hot_l2_assets": settings.autonomy_max_hot_l2_assets,
            "hip3_dex_count": len(settings.autonomy_hip3_dex_names),
            "index_aliases": settings.autonomy_index_aliases,
        },
        "intervals_seconds": {
            "loop": settings.autonomy_loop_interval_seconds,
            "deep_scan": settings.autonomy_deep_scan_interval_seconds,
            "l2_refresh": settings.autonomy_l2_refresh_seconds,
            "candle_refresh": settings.autonomy_candle_refresh_seconds,
            "news_refresh": settings.autonomy_news_refresh_seconds,
            "portfolio_snapshot": settings.autonomy_portfolio_snapshot_seconds,
        },
        "signals": {
            "max_per_day": settings.autonomy_max_signals_per_day,
            "ttl_minutes": settings.autonomy_signal_ttl_minutes,
            "min_score": settings.autonomy_min_signal_score,
        },
        "paper": {
            "initial_equity_usd": settings.autonomy_paper_initial_equity_usd,
            "risk_pct_per_trade": settings.autonomy_paper_risk_pct_per_trade,
            "max_gross_leverage": settings.autonomy_paper_max_gross_leverage,
            "max_single_name_exposure_pct": settings.autonomy_paper_max_single_name_exposure_pct,
            "taker_fee_bps": settings.autonomy_paper_taker_fee_bps,
            "maker_fee_bps": settings.autonomy_paper_maker_fee_bps,
            "default_slippage_bps": settings.autonomy_paper_default_slippage_bps,
        },
        "model_insights": {
            "enabled": settings.autonomy_model_insights_enabled,
            "min_score": settings.autonomy_model_insight_min_score,
            "max_calls_per_hour": settings.autonomy_model_max_calls_per_hour,
        },
        "evaluation": {
            "enabled": settings.autonomy_evaluation_enabled,
            "effective_enabled": settings.autonomy_evaluation_effective_enabled,
            "horizons": settings.autonomy_eval_horizon_list,
            "max_open_signals": settings.autonomy_eval_max_open_signals,
            "price_source": settings.autonomy_eval_price_source,
        },
        "event_evaluation": {
            "enabled": settings.autonomy_event_evaluation_enabled,
            "effective_enabled": settings.autonomy_event_evaluation_effective_enabled,
            "horizons": settings.autonomy_event_eval_horizon_list,
            "min_importance": settings.autonomy_event_eval_min_importance,
            "min_source_score": settings.autonomy_event_eval_min_source_score,
            "max_open_events": settings.autonomy_event_eval_max_open_events,
            "symbols_per_event": settings.autonomy_event_eval_symbols_per_event,
            "macro_proxies": settings.autonomy_event_eval_macro_proxy_symbols,
            "worked_bps": settings.autonomy_event_eval_worked_bps,
            "failed_bps": settings.autonomy_event_eval_failed_bps,
            "volatility_bps": settings.autonomy_event_eval_volatility_bps,
        },
        "memory": {
            "enabled": settings.autonomy_memory_enabled,
            "effective_enabled": settings.autonomy_memory_effective_enabled,
            "role_max_active": settings.autonomy_memory_role_max_active,
            "operator_max_active": settings.autonomy_memory_operator_max_active,
            "ttl_days": {
                "candidate": settings.autonomy_memory_candidate_ttl_days,
                "shadow": settings.autonomy_memory_shadow_ttl_days,
                "role": settings.autonomy_memory_role_ttl_days,
                "process": settings.autonomy_memory_process_ttl_days,
                "incident": settings.autonomy_memory_incident_ttl_days,
            },
            "prompt_injection_policy": {
                "default_roles": settings.autonomy_memory_prompt_role_list,
                "excluded_without_change_control": ["risk", "execution", "treasury"],
                "risk_execution_treasury_change_control_required": settings.autonomy_memory_require_change_control_for_risk_execution,
            },
            "promotion": {
                "role_lesson_min_samples": settings.autonomy_role_lesson_min_samples,
                "operator_lesson_min_samples": settings.autonomy_operator_lesson_min_samples,
                "signal_lesson_min_samples": settings.autonomy_signal_lesson_min_samples,
                "lesson_min_confidence": settings.autonomy_lesson_min_confidence,
                "strategy_lesson_min_confidence": settings.autonomy_strategy_lesson_min_confidence,
                "strategy_affecting_requires_human_review": True,
            },
        },
        "reports": {
            "enabled": settings.autonomy_reports_enabled,
            "effective_enabled": settings.autonomy_reports_effective_enabled,
            "daily_enabled": settings.autonomy_daily_report_enabled,
            "daily_utc": settings.autonomy_daily_report_utc,
            "weekly_enabled": settings.autonomy_weekly_report_enabled,
            "weekly_day": settings.autonomy_weekly_report_day_normalized,
            "weekly_utc": settings.autonomy_weekly_report_utc,
        },
        "tuning_proposals": {
            "enabled": settings.autonomy_tuning_proposals_enabled,
            "effective_enabled": settings.autonomy_tuning_proposals_effective_enabled,
            "mode": "observe_and_recommend_only",
            "ttl_days": settings.autonomy_tuning_proposal_ttl_days,
            "auto_apply_enabled": False,
        },
        "newswire": {
            "enabled": settings.newswire_enabled,
            "query_count": len(settings.newswire_query_terms),
            "x_watchlist_count": len(settings.x_watchlist_users),
            "x_min_public_metric_score": settings.x_min_public_metric_score,
        },
        "safety": {
            "live_execution_enabled": False,
            "exchange_actions_enabled": settings.hyperliquid_exchange_enabled,
            "paper_only": True,
            "human_signoff_required": settings.autonomy_require_human_signoff,
            "strategy_mutation_enabled": False,
            "risk_limit_mutation_enabled": False,
            "tuning_auto_apply_enabled": False,
        },
        "warnings": settings.autonomy_config_warnings(),
        "service": service_status,
    }


def _tradfi_config_status(app: FastAPI) -> dict[str, Any]:
    settings: Settings = app.state.settings
    tradfi_client = getattr(app.state, "tradfi_client", None)
    equity_paper = getattr(app.state, "equity_paper", None)
    return {
        "enabled": settings.tradfi_enabled,
        "provider": "alpaca",
        "data_feed": settings.alpaca_data_feed,
        "client": tradfi_client.status() if tradfi_client is not None else {},
        "alpaca_news_enabled": settings.alpaca_news_enabled,
        "alpaca_trading_enabled": settings.alpaca_trading_enabled,
        "paper_only": True,
        "live_trading_enabled": False,
        "equity_autonomy": {
            "enabled": settings.autonomy_equity_enabled,
            "effective_enabled": settings.autonomy_equity_effective_enabled,
            "universe": settings.autonomy_equity_symbols,
            "max_signals_per_day": settings.autonomy_equity_max_signals_per_day,
            "min_signal_score": settings.autonomy_equity_min_signal_score,
        },
        "equity_paper": equity_paper.status() if equity_paper is not None else {},
        "options_flow": {
            "enabled": settings.options_flow_enabled,
            "effective_enabled": settings.options_flow_effective_enabled,
            "min_volume_oi_ratio": settings.options_flow_min_volume_oi_ratio,
            "min_premium": settings.options_flow_min_premium,
            "llm_enrich_enabled": settings.options_flow_llm_enrich_enabled,
            "llm_max_calls_per_hour": settings.options_flow_llm_enrich_max_calls_per_hour,
        },
        "warnings": settings.tradfi_config_warnings(),
    }


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


def _require_tradfi_client(app: FastAPI) -> TradFiClient:
    client = getattr(app.state, "tradfi_client", None)
    if client is None:
        raise HTTPException(status_code=409, detail="TradFi client is not configured")
    return cast(TradFiClient, client)


def _parse_optional_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid ISO date: {value}") from None


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
