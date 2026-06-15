from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.newswire.bus import QueueSubscriber
from hyperliquid_trading_agent.app.newswire.classify import SOURCE_SCORES
from hyperliquid_trading_agent.app.newswire.schemas import NewswireFilter
from hyperliquid_trading_agent.app.newswire.service import NewswireService

log = get_logger(__name__)

router = APIRouter()


def register_newswire_routes(app: FastAPI) -> None:
    app.include_router(router)


def _service(request: Request) -> NewswireService:
    service = getattr(request.app.state, "newswire_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="newswire is not enabled")
    return service


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
    events = _service(request).list_events(filter=flt, limit=max(1, min(limit, 500)))
    return {"items": [event.model_dump(mode="json") for event in events], "count": len(events)}


@router.get("/newswire/events/{event_id}")
async def get_newswire_event(request: Request, event_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _auth(request.app.state.settings, authorization)
    event = _service(request).get_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="newswire event not found")
    return event.model_dump(mode="json")


@router.get("/newswire/status")
async def newswire_status(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _auth(request.app.state.settings, authorization)
    return _service(request).status()


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
    service: NewswireService | None = getattr(websocket.app.state, "newswire_service", None)
    if service is None:
        await websocket.close(code=1011)
        return
    if not _ws_authorized(settings, websocket.query_params.get("token")):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    flt = await _read_filter_frame(websocket)
    try:
        async with QueueSubscriber(service.bus, filter=flt) as subscription:
            while True:
                event = await subscription.get()
                await websocket.send_json(event.model_dump(mode="json"))
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
