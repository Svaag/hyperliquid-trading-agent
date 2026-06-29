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

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.liquidations import export
from hyperliquid_trading_agent.app.liquidations.models import EventType, SourceIntegrity
from hyperliquid_trading_agent.app.liquidations.ratelimit import RateLimiter, build_rate_limit_dependency
from hyperliquid_trading_agent.app.liquidations.service import LiquidationService
from hyperliquid_trading_agent.app.liquidations.store import LiquidationStore
from hyperliquid_trading_agent.app.metrics import (
    LIQUIDATION_EXPORT_REQUESTS,
    LIQUIDATION_EXPORT_ROWS,
    LIQUIDATION_RATE_LIMITED,
)

RequireAuth = Callable[[Settings, str | None], None]

# Honesty notes surfaced verbatim by /api/meta so the public API self-documents
# its source quality without a human reading the HTML page.
_INTEGRITY_NOTES: dict[str, str] = {
    SourceIntegrity.CONFIRMED: "Venue/indexer explicitly marks a liquidation/deleverage. Exact.",
    SourceIntegrity.SNAPSHOT_THROTTLED: "Public stream coalesces/drops (e.g. Aster forceOrder). Good signal, not a guaranteed full stream.",
    SourceIntegrity.ACCOUNT_PRIVATE: "Exact, but only for a subscribed account (own/whale/vault).",
    SourceIntegrity.DERIVED: "Inferred from trades/flow. liquidation_pressure events are estimates, never confirmed executions.",
    SourceIntegrity.VENDOR: "Provider-indexed all-fills source. High trust but not first-party venue confirmation.",
}

_DIR = Path(__file__).parent
_TEMPLATES = _DIR / "templates"
_STATIC = _DIR / "static"

_SSE_HEARTBEAT_S = 15.0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _export_format(fmt: str) -> tuple[str, str]:
    if fmt == "csv":
        return export.CSV_MEDIA_TYPE, "csv"
    if fmt == "ndjson":
        return export.NDJSON_MEDIA_TYPE, "ndjson"
    return "application/json", "json"


async def _export_body(fmt: str, rows: AsyncIterator[dict[str, Any]]) -> AsyncIterator[bytes]:
    """Stream the redacted projection in the requested format, counting rows."""
    if fmt == "csv":
        yield export.format_csv_header().encode()
        async for row in rows:
            LIQUIDATION_EXPORT_ROWS.labels(format="csv").inc()
            yield export.format_csv_row(row).encode()
    elif fmt == "ndjson":
        async for row in rows:
            LIQUIDATION_EXPORT_ROWS.labels(format="ndjson").inc()
            yield export.format_ndjson_row(row).encode()
    else:  # json array
        yield b"["
        first = True
        async for row in rows:
            LIQUIDATION_EXPORT_ROWS.labels(format="json").inc()
            yield (b"" if first else b",") + json.dumps(row, separators=(",", ":")).encode()
            first = False
        yield b"]"


def register_liquidation_routes(app: FastAPI, settings: Settings, require_auth: RequireAuth) -> None:
    def _service() -> LiquidationService:
        svc: LiquidationService | None = getattr(app.state, "liquidation_service", None)
        if svc is None:
            raise HTTPException(status_code=503, detail="liquidations subsystem is disabled")
        return svc

    def _store() -> LiquidationStore:
        """Durable store for the history/export/stats endpoints (inherently DB-backed)."""
        if not settings.liquidations_export_enabled:
            raise HTTPException(status_code=404, detail="export API is disabled")
        store = _service().store
        if not store.enabled:
            raise HTTPException(status_code=503, detail="durable store not configured")
        return store

    def _resolve_range(since: int | None, until: int | None, *, default_span_ms: int) -> tuple[int, int]:
        now_ms = _now_ms()
        until_ms = until if until is not None else now_ms
        since_ms = since if since is not None else until_ms - default_span_ms
        if until_ms < since_ms:
            raise HTTPException(status_code=400, detail="until must be >= since")
        if until_ms - since_ms > settings.liquidations_export_max_range_ms:
            raise HTTPException(
                status_code=400, detail=f"time range exceeds max {settings.liquidations_export_max_range_ms} ms"
            )
        return since_ms, until_ms

    limiter = RateLimiter(settings.liquidations_export_rate_per_min, settings.liquidations_export_burst)
    rate_limited = build_rate_limit_dependency(
        limiter, trust_proxy=settings.liquidations_trust_proxy, on_rejected=LIQUIDATION_RATE_LIMITED.inc
    )

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

    # -------------------------------------------------- public export API (Phase 2)

    @app.get("/liquidations/api/history")
    async def liquidations_history(
        venue: str | None = None,
        symbol: str | None = None,
        source_integrity: str | None = None,
        event_type: str | None = None,
        side: str | None = None,
        min_notional: float | None = None,
        since: int | None = None,
        until: int | None = None,
        cursor: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
        _rl: None = Depends(rate_limited),
    ) -> dict[str, Any]:
        """Durable, keyset-paginated history — the persisted analog of /api/recent."""
        since_ms, until_ms = _resolve_range(since, until, default_span_ms=settings.liquidations_export_max_range_ms)
        result = await _store().query(
            limit=limit,
            cursor=cursor,
            venue=venue,
            symbol=symbol,
            source_integrity=source_integrity,
            event_type=event_type,
            side=side,
            min_notional=min_notional,
            since_ms=since_ms,
            until_ms=until_ms,
        )
        LIQUIDATION_EXPORT_REQUESTS.labels(endpoint="history", format="json", status="ok").inc()
        return {**result, "count": len(result["items"])}

    @app.get("/liquidations/api/export")
    async def liquidations_export(
        format: str = "csv",
        venue: str | None = None,
        symbol: str | None = None,
        source_integrity: str | None = None,
        event_type: str | None = None,
        side: str | None = None,
        min_notional: float | None = None,
        since: int | None = None,
        until: int | None = None,
        _rl: None = Depends(rate_limited),
    ) -> StreamingResponse:
        """Streamed bulk export (csv|ndjson|json) of the redacted public projection.

        Bounded by ``liquidations_export_max_rows`` and ``..._max_range_ms``; raw
        payloads and un-hashed counterparties are never emitted.
        """
        fmt = format.lower()
        if fmt not in {"csv", "ndjson", "json"}:
            raise HTTPException(status_code=400, detail="format must be csv, ndjson, or json")
        since_ms, until_ms = _resolve_range(since, until, default_span_ms=settings.liquidations_export_max_range_ms)
        store = _store()
        rows = store.stream_query(
            max_rows=settings.liquidations_export_max_rows,
            venue=venue,
            symbol=symbol,
            source_integrity=source_integrity,
            event_type=event_type,
            side=side,
            min_notional=min_notional,
            since_ms=since_ms,
            until_ms=until_ms,
        )
        media_type, extension = _export_format(fmt)
        LIQUIDATION_EXPORT_REQUESTS.labels(endpoint="export", format=fmt, status="ok").inc()
        return StreamingResponse(
            _export_body(fmt, rows),
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="liquidations.{extension}"'},
        )

    @app.get("/liquidations/api/stats")
    async def liquidations_stats(
        since: int | None = None,
        until: int | None = None,
        bucket: int = Query(default=60_000, ge=1_000),
        venue: str | None = None,
        symbol: str | None = None,
        source_integrity: str | None = None,
        side: str | None = None,
        min_notional: float | None = None,
        _rl: None = Depends(rate_limited),
    ) -> dict[str, Any]:
        """Durable, bucketed aggregates over an arbitrary range (executions only)."""
        since_ms, until_ms = _resolve_range(since, until, default_span_ms=3_600_000)
        if (until_ms - since_ms) // bucket > settings.liquidations_stats_max_buckets:
            raise HTTPException(
                status_code=400, detail=f"too many buckets (max {settings.liquidations_stats_max_buckets}); widen bucket"
            )
        result = await _store().stats(
            since_ms=since_ms,
            until_ms=until_ms,
            bucket_ms=bucket,
            venue=venue,
            symbol=symbol,
            source_integrity=source_integrity,
            side=side,
            min_notional=min_notional,
        )
        LIQUIDATION_EXPORT_REQUESTS.labels(endpoint="stats", format="json", status="ok").inc()
        return result

    @app.get("/liquidations/api/meta")
    async def liquidations_meta() -> dict[str, Any]:
        """Self-describing schema + honesty notes + export limits (no rate limit)."""
        return {
            "venues": ["hyperliquid", "lighter", "aster", "dydx", "drift", "gmx", "orderly", "other"],
            "source_integrity": _INTEGRITY_NOTES,
            "event_types": [str(t) for t in EventType],
            "fields": export.EXPORT_COLUMNS,
            "redaction": "liquidated_user/liquidator are hashed; raw payloads are admin-gated and never exported.",
            "export": {
                "formats": ["csv", "ndjson", "json"],
                "max_rows": settings.liquidations_export_max_rows,
                "max_range_ms": settings.liquidations_export_max_range_ms,
                "rate_per_min": settings.liquidations_export_rate_per_min,
                "rate_burst": settings.liquidations_export_burst,
            },
            "endpoints": {
                "history": "/liquidations/api/history",
                "export": "/liquidations/api/export?format=csv|ndjson|json",
                "stats": "/liquidations/api/stats?since&until&bucket",
                "recent": "/liquidations/api/recent (live in-memory)",
                "summary": "/liquidations/api/summary (live rolling windows)",
                "reconcile": "/liquidations/api/reconcile",
            },
        }

    @app.get("/liquidations/api/reconcile")
    async def liquidations_reconcile(_rl: None = Depends(rate_limited)) -> dict[str, Any]:
        """Derived-vs-confirmed reconciliation over the live HL tape."""
        if not settings.liquidations_reconcile_enabled:
            raise HTTPException(status_code=404, detail="reconciliation harness is disabled")
        return _service().reconcile_report(_now_ms())

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
