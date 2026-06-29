"""Public + admin HTTP/WS surface for the liquidation flow monitor.

Mounted into the agent's FastAPI app via `register_liquidation_routes` (same
shape as `register_dashboard_routes`). Public surfaces serve the redacted
projection (counterparties hashed, raw dropped); the raw single-event endpoint is
admin-gated through the shared `require_auth`.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.liquidations.service import LiquidationService

RequireAuth = Callable[[Settings, str | None], None]

_DIR = Path(__file__).parent
_TEMPLATES = _DIR / "templates"
_STATIC = _DIR / "static"

_SSE_HEARTBEAT_S = 15.0


def _now_ms() -> int:
    return int(time.time() * 1000)


def register_liquidation_routes(app: FastAPI, settings: Settings, require_auth: RequireAuth) -> None:
    def _service() -> LiquidationService:
        svc: LiquidationService | None = getattr(app.state, "liquidation_service", None)
        if svc is None:
            raise HTTPException(status_code=503, detail="liquidations subsystem is disabled")
        return svc

    # ---------------------------------------------------------------- page

    @app.get("/liquidations", response_class=HTMLResponse)
    async def liquidations_page() -> HTMLResponse:
        return HTMLResponse((_TEMPLATES / "liquidations.html").read_text())

    @app.get("/liquidations/app.js")
    async def liquidations_app_js() -> Response:
        return Response((_STATIC / "liquidations.js").read_text(), media_type="application/javascript")

    # ---------------------------------------------------------------- json api

    @app.get("/liquidations/api/recent")
    async def liquidations_recent(
        venue: str | None = None,
        symbol: str | None = None,
        min_notional: float | None = None,
        source_integrity: str | None = None,
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> dict[str, Any]:
        items = _service().recent(
            limit=limit,
            venue=venue,
            symbol=symbol,
            min_notional=min_notional,
            source_integrity=source_integrity,
        )
        return {"items": items, "count": len(items)}

    @app.get("/liquidations/api/summary")
    async def liquidations_summary() -> dict[str, Any]:
        return _service().summary(_now_ms())

    @app.get("/liquidations/api/venues")
    async def liquidations_venues() -> dict[str, Any]:
        return {"venues": _service().venues(_now_ms())}

    @app.get("/liquidations/api/signal/{symbol}")
    async def liquidations_signal(symbol: str, venue: str = "all", window: str = "5m") -> dict[str, Any]:
        from hyperliquid_trading_agent.app.liquidations.aggregator import WINDOWS_MS

        window_ms = WINDOWS_MS.get(window, WINDOWS_MS["5m"])
        signal = _service().aggregator.signal(_now_ms(), venue=venue, symbol=symbol, window_ms=window_ms)
        return signal.model_dump(mode="json")

    @app.get("/liquidations/api/events/{event_id}")
    async def liquidations_event(event_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)  # admin-gated: raw payload + un-redacted counterparties
        event = await _service().get_event(event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="event not found")
        return event

    # ---------------------------------------------------------------- realtime

    @app.get("/liquidations/sse")
    async def liquidations_sse(request: Request) -> StreamingResponse:
        service = _service()

        async def gen() -> AsyncIterator[bytes]:
            async with service.subscribe() as stream:
                yield b": connected\n\n"
                iterator = stream.__aiter__()
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(iterator.__anext__(), timeout=_SSE_HEARTBEAT_S)
                    except TimeoutError:
                        yield b": ping\n\n"
                        continue
                    except StopAsyncIteration:  # pragma: no cover
                        break
                    payload = json.dumps(event.public_view())
                    yield f"data: {payload}\n\n".encode()

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.websocket("/liquidations/ws")
    async def liquidations_ws(websocket: WebSocket) -> None:
        service: LiquidationService | None = getattr(websocket.app.state, "liquidation_service", None)
        if service is None:
            await websocket.close(code=1013)  # try again later
            return
        await websocket.accept()
        try:
            async with service.subscribe() as stream:
                async for event in stream:
                    await websocket.send_text(json.dumps(event.public_view()))
        except WebSocketDisconnect:
            return
        except Exception:  # pragma: no cover - client/transport teardown
            return

    # ---------------------------------------------------------------- ops

    @app.get("/liquidations/healthz")
    async def liquidations_healthz() -> dict[str, Any]:
        return {"status": "ok"}

    @app.get("/liquidations/readyz")
    async def liquidations_readyz() -> dict[str, Any]:
        service: LiquidationService | None = getattr(app.state, "liquidation_service", None)
        if service is None:
            return {"status": "disabled"}
        now_ms = _now_ms()
        venues = service.venues(now_ms)
        connected = [v for v in venues if v.get("connected") and not v.get("stale")]
        status = "ready" if (not venues or connected) else "degraded"
        return {"status": status, "adapters": len(venues), "connected": len(connected), "service": service.status()}
