from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from hyperliquid_trading_agent import __version__
from hyperliquid_trading_agent.app.agent.model_gateway import ModelGateway
from hyperliquid_trading_agent.app.agent.runner import AgentContext, TradingAgentRunner
from hyperliquid_trading_agent.app.agent.tools import AgentTools
from hyperliquid_trading_agent.app.config import Settings, load_settings
from hyperliquid_trading_agent.app.db.repository import Repository
from hyperliquid_trading_agent.app.db.session import create_engine, create_sessionmaker
from hyperliquid_trading_agent.app.discord_bot import DiscordTradingBot
from hyperliquid_trading_agent.app.hyperliquid.client import HyperliquidClient
from hyperliquid_trading_agent.app.hyperliquid.ws_worker import HyperliquidWebSocketWorker
from hyperliquid_trading_agent.app.logging import configure_logging, get_logger
from hyperliquid_trading_agent.app.metrics import SERVICE_INFO, UP
from hyperliquid_trading_agent.app.news.service import NewsService

log = get_logger(__name__)


class AskRequest(BaseModel):
    prompt: str


class AskResponse(BaseModel):
    content: str
    refused: bool = False
    fallback_used: bool = False
    model_used: str | None = None
    tool_count: int = 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    UP.set(1)
    SERVICE_INFO.info({"version": __version__, "environment": settings.environment})

    engine = create_engine(settings)
    sessionmaker = create_sessionmaker(engine)
    repository = Repository(sessionmaker)
    hyperliquid = HyperliquidClient(settings=settings)
    news = NewsService(settings=settings, repository=repository)
    tools = AgentTools(hyperliquid=hyperliquid, news=news, repository=repository)
    model_gateway = ModelGateway(settings=settings)
    runner = TradingAgentRunner(tools=tools, model_gateway=model_gateway, repository=repository)
    ws_worker = HyperliquidWebSocketWorker(settings=settings)
    bot = DiscordTradingBot(settings=settings, runner=runner)

    app.state.engine = engine
    app.state.repository = repository
    app.state.hyperliquid = hyperliquid
    app.state.news = news
    app.state.agent_runner = runner
    app.state.discord_bot = bot
    app.state.ws_worker = ws_worker

    bot_task: asyncio.Task | None = None
    ws_task: asyncio.Task | None = None
    if settings.hyperliquid_ws_enabled:
        ws_task = asyncio.create_task(ws_worker.start(), name="hyperliquid-ws")
        log.info("hyperliquid_ws_task_started")
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
        await ws_worker.stop()
        await hyperliquid.close()
        await engine.dispose()
        for task in [bot_task, ws_task]:
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
        return {"status": "ready", "checks": ready_checks}

    @app.get("/health/config")
    async def config_health() -> dict[str, Any]:
        attempts = ModelGateway(settings).configured_attempts()
        return {
            "environment": settings.environment,
            "hyperliquid_network": settings.hyperliquid_network,
            "hyperliquid_exchange_enabled": settings.hyperliquid_exchange_enabled,
            "hyperliquid_ws_enabled": settings.hyperliquid_ws_enabled,
            "models": [{"model": item.model, "provider": item.provider, "missing": item.missing_reason} for item in attempts],
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
        )

    @app.get("/metrics")
    async def metrics(authorization: str | None = Header(default=None)):
        if settings.metrics_bearer_token:
            expected = f"Bearer {settings.metrics_bearer_token}"
            if authorization != expected:
                raise HTTPException(status_code=401, detail="metrics token required")
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


def main() -> None:
    settings = load_settings()
    uvicorn.run("hyperliquid_trading_agent.app.main:create_app", factory=True, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
