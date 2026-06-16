from __future__ import annotations

from typing import Any, Callable

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.readiness import build_paper_readiness_scorecard
from hyperliquid_trading_agent.app.engine.replay_compare import (
    EngineReplayComparisonService,
    latest_engine_replay_comparison,
    list_engine_replay_comparisons,
)
from hyperliquid_trading_agent.app.engine.validation_report import (
    build_engine_validation_report,
    render_engine_validation_dashboard,
)

RequireAuth = Callable[[Settings, str | None], None]


class EngineReplayComparisonRequest(BaseModel):
    window_hours: int = Field(default=24, ge=1, le=24 * 30)
    universe: list[str] = Field(default_factory=list)
    baseline_config: dict[str, Any] = Field(default_factory=dict)
    candidate_config: dict[str, Any] = Field(default_factory=dict)
    variant_id: str | None = None


def register_engine_routes(app: FastAPI, settings: Settings, require_auth: RequireAuth) -> None:
    def _repo():
        repository = getattr(app.state, "repository", None)
        if repository is None:
            raise HTTPException(status_code=503, detail="repository unavailable")
        return repository

    def _auth(authorization: str | None) -> None:
        require_auth(settings, authorization)

    @app.get("/engine/status")
    async def engine_status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        repository = _repo()
        service = getattr(app.state, "engine_service", None)
        service_status = service.status() if service is not None and callable(getattr(service, "status", None)) else {}
        monitor = getattr(app.state, "engine_validation_monitor", None)
        monitor_status = monitor.status() if monitor is not None and callable(getattr(monitor, "status", None)) else {}
        pnl = getattr(app.state, "engine_pnl_attribution", None)
        pnl_status = pnl.status() if pnl is not None and callable(getattr(pnl, "status", None)) else {}
        return {
            "enabled": settings.engine_enabled,
            "mode": settings.engine_mode,
            "execution_modes": settings.engine_execution_mode_list,
            "paper_enabled": settings.engine_paper_enabled,
            "shadow_enabled": settings.engine_shadow_enabled,
            "live_enabled": settings.engine_live_enabled,
            "repository_enabled": getattr(repository, "enabled", False),
            "service": service_status,
            "validation_monitor": monitor_status,
            "pnl_attribution": pnl_status,
            "debate": {"enabled": settings.engine_debate_enabled, "max_per_day": settings.engine_debate_max_per_day, "priority_min": settings.engine_debate_priority_min},
            "retention": {
                "event_days": settings.engine_event_retention_days,
                "feature_days": settings.engine_feature_retention_days,
                "rollup_days": settings.engine_rollup_retention_days,
            },
        }

    @app.get("/engine/events")
    async def engine_events(limit: int = 100, event_type: str | None = None, asset_class: str | None = None, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _auth(authorization)
        return await _repo().list_normalized_events(limit=limit, event_type=event_type, asset_class=asset_class)

    @app.get("/engine/events/{event_id}")
    async def engine_event(event_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        item = await _repo().get_normalized_event(event_id)
        if item is None:
            raise HTTPException(status_code=404, detail="engine event not found")
        return item

    @app.get("/engine/features")
    async def engine_features(asset: str | None = None, feature_name: str | None = None, limit: int = 100, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _auth(authorization)
        if not asset:
            raise HTTPException(status_code=400, detail="asset is required")
        return await _repo().list_feature_values(asset=asset, feature_name=feature_name, limit=limit)

    @app.get("/engine/regime/latest")
    async def engine_regime_latest(primary_asset: str | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        item = await _repo().latest_regime_snapshot(primary_asset=primary_asset)
        if item is None:
            raise HTTPException(status_code=404, detail="regime snapshot not found")
        return item

    @app.get("/engine/candidates")
    async def engine_candidates(status: str | None = None, asset: str | None = None, limit: int = 100, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _auth(authorization)
        return await _repo().list_alpha_candidates(status=status, asset=asset, limit=limit)

    @app.get("/engine/candidates/{candidate_id}")
    async def engine_candidate(candidate_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        item = await _repo().get_alpha_candidate(candidate_id)
        if item is None:
            raise HTTPException(status_code=404, detail="candidate not found")
        return item

    @app.get("/engine/candidate-book/latest")
    async def engine_candidate_book_latest(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        item = await _repo().latest_candidate_book_snapshot()
        if item is None:
            raise HTTPException(status_code=404, detail="candidate book not found")
        return item

    @app.get("/engine/ev-estimates")
    async def engine_ev_estimates(candidate_id: str | None = None, limit: int = 100, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _auth(authorization)
        return await _repo().list_ev_estimates(candidate_id=candidate_id, limit=limit)

    @app.get("/engine/allocations")
    async def engine_allocations(candidate_id: str | None = None, limit: int = 100, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _auth(authorization)
        return await _repo().list_allocation_decisions(candidate_id=candidate_id, limit=limit)

    @app.get("/engine/evidence-packs/{evidence_pack_id}")
    async def engine_evidence_pack(evidence_pack_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        item = await _repo().get_evidence_pack(evidence_pack_id)
        if item is None:
            raise HTTPException(status_code=404, detail="evidence pack not found")
        return item

    @app.get("/engine/debate-decisions")
    async def engine_debate_decisions(candidate_id: str | None = None, limit: int = 100, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _auth(authorization)
        return await _repo().list_debate_decisions(candidate_id=candidate_id, limit=limit)

    @app.get("/engine/order-intents")
    async def engine_order_intents(execution_mode: str | None = None, limit: int = 100, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _auth(authorization)
        return await _repo().list_order_intents(execution_mode=execution_mode, limit=limit)

    @app.get("/engine/execution-reports")
    async def engine_execution_reports(intent_id: str | None = None, limit: int = 100, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _auth(authorization)
        return await _repo().list_execution_reports(intent_id=intent_id, limit=limit)

    @app.get("/engine/positions")
    async def engine_positions(state: str | None = None, asset: str | None = None, limit: int = 100, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _auth(authorization)
        return await _repo().list_position_theses(state=state, asset=asset, limit=limit)

    @app.get("/engine/reconciliation")
    async def engine_reconciliation(execution_mode: str | None = None, limit: int = 100, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _auth(authorization)
        return await _repo().list_reconciliation_runs(execution_mode=execution_mode, limit=limit)

    @app.get("/engine/model-versions")
    async def engine_model_versions(status: str | None = None, model_type: str | None = None, limit: int = 100, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _auth(authorization)
        return await _repo().list_model_versions(status=status, model_type=model_type, limit=limit)

    @app.get("/engine/risk-rejects")
    async def engine_risk_rejects(limit: int = 100, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _auth(authorization)
        return await _repo().list_risk_gateway_decisions(limit=limit, decision="reject")

    @app.get("/engine/pnl-attribution")
    async def engine_pnl_attribution(strategy_id: str | None = None, asset: str | None = None, limit: int = 100, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _auth(authorization)
        return await _repo().list_pnl_attribution(strategy_id=strategy_id, asset=asset, limit=limit)

    @app.get("/engine/validation-report")
    async def engine_validation_report(limit: int = 500, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        return await build_engine_validation_report(_repo(), limit=limit)

    @app.get("/engine/readiness")
    async def engine_readiness(window_hours: int | None = None, limit: int = 1000, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        service = getattr(app.state, "engine_service", None)
        return await build_paper_readiness_scorecard(_repo(), settings, service, window_hours=window_hours, limit=limit)

    @app.get("/engine/replay-comparisons")
    async def engine_replay_comparisons(limit: int = 100, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _auth(authorization)
        return await list_engine_replay_comparisons(_repo(), limit=limit)

    @app.get("/engine/replay-comparisons/latest")
    async def engine_replay_comparison_latest(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        item = await latest_engine_replay_comparison(_repo())
        if item is None:
            raise HTTPException(status_code=404, detail="engine replay comparison not found")
        return item

    @app.post("/engine/replay-comparisons/run")
    async def engine_replay_comparison_run(request: EngineReplayComparisonRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        end_ms = int(__import__("time").time() * 1000)
        start_ms = end_ms - request.window_hours * 60 * 60 * 1000
        service = EngineReplayComparisonService(repository=_repo(), settings=settings)
        return await service.compare_variant(
            baseline_config=request.baseline_config,
            candidate_config=request.candidate_config,
            window_start_ms=start_ms,
            window_end_ms=end_ms,
            universe=request.universe or settings.autonomy_core_symbols,
            variant_id=request.variant_id,
        )

    @app.get("/engine/dashboard", response_class=HTMLResponse)
    async def engine_dashboard(limit: int = 500, authorization: str | None = Header(default=None)) -> HTMLResponse:
        _auth(authorization)
        report = await build_engine_validation_report(_repo(), limit=limit)
        return HTMLResponse(render_engine_validation_dashboard(report))

    @app.get("/engine/retention")
    async def engine_retention(limit: int = 100, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _auth(authorization)
        return await _repo().list_retention_runs(limit=limit)
