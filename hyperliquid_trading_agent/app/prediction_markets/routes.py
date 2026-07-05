from __future__ import annotations

from typing import Any, Callable

from fastapi import FastAPI, Header, HTTPException

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.prediction_markets.paper import PredictionMarketPaperService
from hyperliquid_trading_agent.app.prediction_markets.schemas import (
    PredictionMarketBetCancelRequest,
    PredictionMarketBetConfirmRequest,
    PredictionMarketBetDraftRequest,
    PredictionMarketPositionCloseRequest,
    PredictionMarketSettlementRequest,
)

RequireAuth = Callable[[Settings, str | None], None]


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


def register_prediction_market_routes(app: FastAPI, settings: Settings, require_auth: RequireAuth) -> None:
    def _auth(authorization: str | None) -> None:
        require_auth(settings, authorization)

    def _service() -> PredictionMarketPaperService:
        repository = getattr(app.state, "repository", None)
        if repository is None:
            raise HTTPException(status_code=503, detail="repository unavailable")
        return PredictionMarketPaperService(settings=settings, repository=repository)

    async def _enqueue_command(command_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        repo = getattr(app.state, "repository", None)
        if repo is None or not callable(getattr(repo, "enqueue_worker_command", None)):
            return {"command_id": f"unpersisted_{command_type}", "target_role": "trader", "command_type": command_type, "status": "accepted_unpersisted"}
        return await repo.enqueue_worker_command(target_role="trader", command_type=command_type, payload=payload, requested_by="api")

    @app.get("/prediction-markets/search")
    async def prediction_market_search(
        q: str = "",
        venue: str | None = None,
        limit: int = 10,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        items = [item.model_dump(mode="json") for item in await _service().search(q, venue=venue, limit=limit)]
        return {"items": items, "count": len(items)}

    @app.get("/prediction-markets/paper/portfolio")
    async def prediction_market_portfolio(
        guild_id: str,
        discord_user_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        return await _service().portfolio(discord_guild_id=guild_id, discord_user_id=discord_user_id)

    @app.get("/prediction-markets/paper/leaderboard")
    async def prediction_market_leaderboard(
        guild_id: str,
        limit: int = 20,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        rows = [row.model_dump(mode="json") for row in await _service().leaderboard(discord_guild_id=guild_id, limit=limit)]
        return {"items": rows, "count": len(rows)}

    @app.post("/prediction-markets/paper/drafts", status_code=202)
    async def prediction_market_draft(request: PredictionMarketBetDraftRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        if not settings.prediction_market_paper_enabled:
            raise HTTPException(status_code=409, detail="prediction-market paper trading is disabled")
        command = await _enqueue_command("prediction_market_bet_draft", request.model_dump(mode="json"))
        return _accepted_command(command)

    @app.post("/prediction-markets/paper/drafts/{draft_id}/confirm", status_code=202)
    async def prediction_market_confirm(draft_id: str, request: PredictionMarketBetConfirmRequest | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        payload = request.model_dump(mode="json") if request else {}
        command = await _enqueue_command("prediction_market_bet_confirm", {"draft_id": draft_id, **payload})
        return _accepted_command(command)

    @app.post("/prediction-markets/paper/drafts/{draft_id}/cancel", status_code=202)
    async def prediction_market_cancel(draft_id: str, request: PredictionMarketBetCancelRequest | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        payload = request.model_dump(mode="json") if request else {}
        command = await _enqueue_command("prediction_market_bet_cancel", {"draft_id": draft_id, **payload})
        return _accepted_command(command)

    @app.post("/prediction-markets/paper/positions/{position_ref}/close", status_code=202)
    async def prediction_market_close(position_ref: str, request: PredictionMarketPositionCloseRequest | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        payload = request.model_dump(mode="json") if request else {}
        command = await _enqueue_command("prediction_market_position_close", {"position_ref": position_ref, **payload})
        return _accepted_command(command)

    @app.post("/prediction-markets/settlements", status_code=202)
    async def prediction_market_settle(request: PredictionMarketSettlementRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        command = await _enqueue_command("prediction_market_settlement_apply", request.model_dump(mode="json"))
        return _accepted_command(command)

    @app.post("/prediction-markets/settlements/sweep", status_code=202)
    async def prediction_market_settlement_sweep(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        command = await _enqueue_command("prediction_market_settlement_sweep", {})
        return _accepted_command(command)
