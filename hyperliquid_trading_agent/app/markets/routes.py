from __future__ import annotations

from typing import Callable

from fastapi import FastAPI, Header

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.markets.schemas import WatchlistChangeRequest
from hyperliquid_trading_agent.app.markets.universe import WatchlistService

RequireAuth = Callable[[Settings, str | None], None]


def register_market_universe_routes(app: FastAPI, settings: Settings, require_auth: RequireAuth) -> None:
    def _auth(authorization: str | None) -> None:
        require_auth(settings, authorization)

    def _service() -> WatchlistService:
        service = getattr(app.state, "watchlist_service", None)
        if service is None:
            service = WatchlistService(app.state.repository)
            app.state.watchlist_service = service
        return service

    @app.get("/engine/universe")
    async def market_universe(
        tier: str | None = None,
        venue_id: str | None = None,
        status: str | None = None,
        limit: int = 1000,
        authorization: str | None = Header(default=None),
    ) -> dict:
        _auth(authorization)
        service = _service()
        await service.seed_if_empty()
        return {"summary": await service.summary(), "items": await service.list(tier=tier, venue_id=venue_id, status=status, limit=limit)}

    @app.get("/engine/universe/unresolved")
    async def unresolved_market_universe(limit: int = 1000, authorization: str | None = Header(default=None)) -> dict:
        _auth(authorization)
        service = _service()
        await service.seed_if_empty()
        return {"items": await service.unresolved(limit=limit)}

    @app.get("/engine/universe/history")
    async def market_universe_history(limit: int = 100, authorization: str | None = Header(default=None)) -> dict:
        _auth(authorization)
        return {
            "changes": await app.state.repository.list_watchlist_change_events(limit=limit),
            "snapshots": await app.state.repository.list_universe_snapshots(limit=limit),
        }

    @app.post("/engine/admin/watchlist/changes")
    async def create_watchlist_change(request: WatchlistChangeRequest, authorization: str | None = Header(default=None)) -> dict:
        _auth(authorization)
        service = _service()
        await service.seed_if_empty()
        return await service.request_change(request)

    @app.post("/engine/admin/watchlist/changes/{change_id}/confirm")
    async def confirm_watchlist_change(change_id: str, actor: str = "api", authorization: str | None = Header(default=None)) -> dict:
        _auth(authorization)
        return await _service().confirm(change_id, actor=actor)

    @app.get("/engine/venue-market-snapshots")
    async def venue_market_snapshots(
        instrument_id: str | None = None,
        underlying_id: str | None = None,
        venue_id: str | None = None,
        since_ms: int | None = None,
        limit: int = 1000,
        authorization: str | None = Header(default=None),
    ) -> list[dict]:
        _auth(authorization)
        return await app.state.repository.list_venue_market_snapshots(
            instrument_id=instrument_id,
            underlying_id=underlying_id,
            venue_id=venue_id,
            since_ms=since_ms,
            limit=limit,
        )

    @app.get("/engine/cross-venue-feature-snapshots")
    async def cross_venue_feature_snapshots(
        underlying_id: str | None = None,
        since_ms: int | None = None,
        limit: int = 1000,
        authorization: str | None = Header(default=None),
    ) -> list[dict]:
        _auth(authorization)
        return await app.state.repository.list_cross_venue_feature_snapshots(
            underlying_id=underlying_id,
            since_ms=since_ms,
            limit=limit,
        )
