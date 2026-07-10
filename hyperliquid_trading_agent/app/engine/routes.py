from __future__ import annotations

import time
from typing import Any, Callable

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.alpha_graph import build_strategy_regime_alpha_graph
from hyperliquid_trading_agent.app.engine.diagnostics import build_candidate_funnel, build_strategy_funnel
from hyperliquid_trading_agent.app.engine.news_risk_counterfactual import (
    latest_news_risk_counterfactual,
    list_news_risk_counterfactuals,
    run_news_risk_counterfactual,
)
from hyperliquid_trading_agent.app.engine.operator_proposals import project_operator_proposal_to_trade_signal
from hyperliquid_trading_agent.app.engine.paper_signoff import build_paper_signoff_preflight
from hyperliquid_trading_agent.app.engine.readiness import build_paper_readiness_scorecard
from hyperliquid_trading_agent.app.engine.replay_compare import (
    latest_engine_replay_comparison,
    list_engine_replay_comparisons,
)
from hyperliquid_trading_agent.app.engine.runtime import resolve_engine_runtime
from hyperliquid_trading_agent.app.engine.signal_comparison import build_signal_path_comparison
from hyperliquid_trading_agent.app.engine.signal_quality import build_signal_quality_report
from hyperliquid_trading_agent.app.engine.validation_report import (
    build_engine_validation_report,
    render_engine_validation_dashboard,
)
from hyperliquid_trading_agent.app.newswire.observability import build_engine_newsfeed_health

RequireAuth = Callable[[Settings, str | None], None]


class EngineStrategyRegimeRefreshRequest(BaseModel):
    window_hours: int = Field(default=24, ge=1, le=24 * 90)


class EngineBanditRecommendationRunRequest(BaseModel):
    window_hours: int = Field(default=24 * 7, ge=1, le=24 * 180)


class EnginePositionThesisCleanupRequest(BaseModel):
    before_ms: int = Field(ge=1)
    states: list[str] = Field(default_factory=lambda: ["approved"])
    reason: str = Field(default="stale_position_cleanup", max_length=96)
    limit: int = Field(default=20000, ge=1, le=100_000)
    dry_run: bool = True


class EngineReplayComparisonRequest(BaseModel):
    window_hours: int = Field(default=24, ge=1, le=24 * 30)
    universe: list[str] = Field(default_factory=list)
    baseline_config: dict[str, Any] = Field(default_factory=dict)
    candidate_config: dict[str, Any] = Field(default_factory=dict)
    variant_id: str | None = None


class EngineNewsRiskCounterfactualRequest(BaseModel):
    window_hours: int = Field(default=24, ge=1, le=24 * 90)
    as_of_ms: int | None = Field(default=None, ge=1)


class EngineOperatorProposalActionRequest(BaseModel):
    reason: str = Field(default="", max_length=1000)


async def _enqueue_command(repo: Any, *, target_role: str, command_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if callable(getattr(repo, "enqueue_worker_command", None)):
        return await repo.enqueue_worker_command(target_role=target_role, command_type=command_type, payload=payload, requested_by="api")
    return {"command_id": f"unpersisted_{command_type}", "target_role": target_role, "command_type": command_type, "status": "accepted_unpersisted"}


def _accepted_command(command: dict[str, Any]) -> dict[str, Any]:
    command_id = str(command.get("command_id") or "")
    return {
        "accepted": True,
        "command_id": command_id,
        "status_url": f"/commands/{command_id}",
        "target_role": command.get("target_role"),
        "command_type": command.get("command_type"),
        "status": command.get("status"),
        "report_only": True,
        "auto_apply_allowed": False,
    }


def _strategy_catalog_from_specs(specs: list[dict[str, Any]], *, mode: str) -> dict[str, Any]:
    runtime_ids = {str(spec.get("strategy_id")) for spec in specs if spec.get("enabled")}
    paper_ids = {str(spec.get("strategy_id")) for spec in specs if spec.get("enabled") and _spec_paper_eligible(spec)}
    shadow_ids = {str(spec.get("strategy_id")) for spec in specs if spec.get("enabled") and _spec_shadow_only(spec)}
    families: list[dict[str, Any]] = []
    for family in sorted({str(spec.get("family") or "unknown") for spec in specs}):
        family_specs = [spec for spec in specs if str(spec.get("family") or "unknown") == family]
        families.append(
            {
                "family": family,
                "total_specs": len(family_specs),
                "runtime_enabled": len([spec for spec in family_specs if spec.get("enabled")]),
                "paper_eligible": len([spec for spec in family_specs if spec.get("enabled") and _spec_paper_eligible(spec)]),
                "shadow_only": len([spec for spec in family_specs if spec.get("enabled") and _spec_shadow_only(spec)]),
                "strategy_ids": [str(spec.get("strategy_id")) for spec in family_specs],
            }
        )
    return {
        "mode": mode,
        "total_specs": len(specs),
        "runtime_enabled": len(runtime_ids),
        "enabled_specs": len(runtime_ids),
        "paper_eligible": len(paper_ids),
        "shadow_only": len(shadow_ids),
        "spec_only": len([spec for spec in specs if str(spec.get("strategy_id")) not in runtime_ids]),
        "runtime_enabled_ids": sorted(runtime_ids),
        "paper_eligible_ids": sorted(paper_ids),
        "shadow_only_ids": sorted(shadow_ids),
        "spec_only_ids": sorted(str(spec.get("strategy_id")) for spec in specs if str(spec.get("strategy_id")) not in runtime_ids),
        "families": families,
    }


def _spec_metadata(spec: dict[str, Any]) -> dict[str, Any]:
    value = spec.get("metadata")
    return dict(value) if isinstance(value, dict) else {}


def _spec_paper_eligible(spec: dict[str, Any]) -> bool:
    metadata = _spec_metadata(spec)
    return bool(metadata.get("paper_eligible", True)) and str(metadata.get("activation_scope") or "paper_shadow") != "shadow_only"


def _spec_shadow_only(spec: dict[str, Any]) -> bool:
    metadata = _spec_metadata(spec)
    return str(metadata.get("activation_scope") or "paper_shadow") == "shadow_only" or bool(metadata.get("operator_promotion_required"))


async def _latest_trader_metadata(repository: Any, key: str) -> dict[str, Any]:
    if not callable(getattr(repository, "list_service_heartbeats", None)):
        return {}
    heartbeats = await repository.list_service_heartbeats(service_role="trader", limit=5)
    for heartbeat in heartbeats:
        metadata = heartbeat.get("metadata") if isinstance(heartbeat, dict) else None
        item = metadata.get(key) if isinstance(metadata, dict) else None
        if isinstance(item, dict):
            return {
                "service_role": heartbeat.get("service_role"),
                "instance_id": heartbeat.get("instance_id"),
                "status": heartbeat.get("status"),
                "updated_at_ms": heartbeat.get("updated_at_ms"),
                **item,
            }
    return {}


async def _latest_trader_engine_newsfeed(repository: Any) -> dict[str, Any]:
    return await _latest_trader_metadata(repository, "engine_newsfeed")


async def _latest_trader_engine_loop(repository: Any) -> dict[str, Any]:
    return await _latest_trader_metadata(repository, "engine_loop")


async def _latest_trader_operator_proposals(repository: Any) -> dict[str, Any]:
    return await _latest_trader_metadata(repository, "engine_operator_proposals")


async def _latest_trader_validation_monitor(repository: Any) -> dict[str, Any]:
    return await _latest_trader_metadata(repository, "engine_validation_monitor")


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
        news_consumer = getattr(app.state, "engine_news_consumer", None)
        news_status = news_consumer.status() if news_consumer is not None and callable(getattr(news_consumer, "status", None)) else {}
        news_runtime = {} if news_status.get("running") else await _latest_trader_engine_newsfeed(repository)
        try:
            engine_news_offset = await repository.get_consumer_offset(
                "trader:engine_newswire",
                source_table="newswire_story_revisions",
            )
            latest_newswire_stories = await repository.list_newswire_stories(limit=1)
            newswire_heartbeats = await repository.list_service_heartbeats(service_role="newswire", limit=5)
        except Exception:
            engine_news_offset = {}
            latest_newswire_stories = []
            newswire_heartbeats = []
        newsfeed_health = build_engine_newsfeed_health(
            settings,
            news_runtime or {"consumer": news_status, "pump": {}},
            engine_news_offset,
            newswire_active=bool(
                latest_newswire_stories and any(item.get("status") == "running" for item in newswire_heartbeats)
            ),
            latest_source_at_ms=(
                int(latest_newswire_stories[0].get("last_updated_at_ms") or 0)
                if latest_newswire_stories
                else None
            ),
        )
        engine_runtime = await resolve_engine_runtime(repository, settings, local_service=service)
        operator_proposals_runtime = await _latest_trader_operator_proposals(repository)
        validation_monitor_runtime = await _latest_trader_validation_monitor(repository)
        runtime_running = bool(engine_runtime.get("runtime_running") or engine_runtime.get("running"))
        return {
            "enabled": bool(engine_runtime.get("enabled")),
            "running": runtime_running,
            "owner_role": str(engine_runtime.get("owner_role") or "trader"),
            "runtime_source": str(engine_runtime.get("runtime_source") or "local_service"),
            "configured_for_api_role": settings.engine_enabled,
            "mode": settings.engine_mode,
            "execution_modes": engine_runtime.get("execution_modes") or settings.engine_execution_mode_list,
            "paper_enabled": bool(engine_runtime.get("paper_enabled", settings.engine_paper_enabled)),
            "shadow_enabled": bool(engine_runtime.get("shadow_enabled", settings.engine_shadow_enabled)),
            "live_enabled": bool(engine_runtime.get("live_enabled", settings.engine_live_enabled)),
            "wave_policy": {
                "wave1c_enabled": bool(engine_runtime.get("wave1c_enabled", settings.engine_wave1c_enabled)),
                "wave2_enabled": bool(engine_runtime.get("wave2_enabled", settings.engine_wave2_enabled)),
                "wave2_status": "deferred_until_wave1_evidence_replay_readiness",
            },
            "repository_enabled": getattr(repository, "enabled", False),
            "service": engine_runtime,
            "local_service": service_status,
            "engine_runtime": engine_runtime,
            "operator_proposals": operator_proposals_runtime,
            "newsfeed": news_status,
            "newsfeed_runtime": news_runtime,
            "newsfeed_health": newsfeed_health,
            "validation_monitor": validation_monitor_runtime
            or {**monitor_status, "running": False, "owner_role": "trader", "runtime_source": "awaiting_trader_heartbeat"},
            "pnl_attribution": pnl_status,
            "debate": {"enabled": settings.engine_debate_enabled, "max_per_day": settings.engine_debate_max_per_day, "priority_min": settings.engine_debate_priority_min},
            "retention": {
                "event_days": settings.engine_event_retention_days,
                "feature_days": settings.engine_feature_retention_days,
                "rollup_days": settings.engine_rollup_retention_days,
            },
        }

    @app.get("/engine/operator-proposals")
    async def engine_operator_proposals(
        status: str | None = None,
        asset: str | None = None,
        limit: int = 100,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        repository = _repo()
        await repository.expire_engine_operator_proposals(now_ms=int(time.time() * 1000))
        items = await repository.list_engine_operator_proposals(
            status=status,
            asset=asset,
            limit=max(1, min(1000, limit)),
        )
        return {
            "items": items,
            "count": len(items),
            "execution_authority": "none",
            "acknowledgment_only": True,
        }

    @app.get("/engine/operator-proposals/{proposal_id}")
    async def engine_operator_proposal(
        proposal_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        item = await _repo().get_engine_operator_proposal(proposal_id)
        if item is None:
            raise HTTPException(status_code=404, detail="engine operator proposal not found")
        return {**item, "signal_projection": project_operator_proposal_to_trade_signal(item)}

    @app.post("/engine/operator-proposals/{proposal_id}/acknowledge", status_code=202)
    async def acknowledge_engine_operator_proposal(
        proposal_id: str,
        request: EngineOperatorProposalActionRequest | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        command = await _enqueue_command(
            _repo(),
            target_role="trader",
            command_type="engine_operator_proposal_ack",
            payload={"proposal_id": proposal_id, "reason": request.reason if request else ""},
        )
        return {**_accepted_command(command), "acknowledgment_only": True, "paper_order_created": False}

    @app.post("/engine/operator-proposals/{proposal_id}/reject", status_code=202)
    async def reject_engine_operator_proposal(
        proposal_id: str,
        request: EngineOperatorProposalActionRequest | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        command = await _enqueue_command(
            _repo(),
            target_role="trader",
            command_type="engine_operator_proposal_reject",
            payload={"proposal_id": proposal_id, "reason": request.reason if request else ""},
        )
        return {**_accepted_command(command), "paper_order_created": False}

    @app.post("/engine/validation-monitor/run-once", status_code=202)
    async def run_engine_validation_monitor_once(
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        command = await _enqueue_command(
            _repo(),
            target_role="trader",
            command_type="engine_validation_monitor_run_once",
            payload={},
        )
        return _accepted_command(command)

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

    @app.get("/engine/regime/history")
    async def engine_regime_history(primary_asset: str | None = None, since_ms: int | None = None, limit: int = 500, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _auth(authorization)
        method = getattr(_repo(), "list_regime_snapshots", None)
        if not callable(method):
            return []
        return await method(primary_asset=primary_asset, since_ms=since_ms, limit=limit)

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

    @app.get("/engine/candidate-funnel")
    async def engine_candidate_funnel(
        window_hours: int = 24,
        as_of_ms: int | None = None,
        strategy_id: str | None = None,
        asset: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        return await build_candidate_funnel(
            _repo(),
            window_hours=max(1, min(24 * 90, window_hours)),
            as_of_ms=as_of_ms,
            strategy_id=strategy_id,
            asset=asset,
        )

    @app.get("/engine/strategy-funnel")
    async def engine_strategy_funnel(
        window_hours: int = 24,
        as_of_ms: int | None = None,
        strategy_id: str | None = None,
        asset: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        return await build_strategy_funnel(
            _repo(),
            window_hours=max(1, min(24 * 90, window_hours)),
            as_of_ms=as_of_ms,
            strategy_id=strategy_id,
            asset=asset,
        )

    @app.get("/engine/strategies")
    async def engine_strategies(family: str | None = None, enabled: bool | None = None, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _auth(authorization)
        service = getattr(app.state, "engine_service", None)
        registry = getattr(service, "strategy_registry", None)
        if registry is not None:
            specs = [spec.model_dump(mode="json") for spec in registry.specs(enabled_only=enabled is True)]
            if family:
                specs = [spec for spec in specs if spec.get("family") == family]
            if enabled is not None:
                specs = [spec for spec in specs if bool(spec.get("enabled")) is enabled]
            return specs
        return await _repo().list_strategy_specs(family=family, enabled=enabled, limit=500)

    @app.get("/engine/strategy-catalog")
    async def engine_strategy_catalog(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        service = getattr(app.state, "engine_service", None)
        registry = getattr(service, "strategy_registry", None)
        if registry is not None and callable(getattr(registry, "catalog_summary", None)):
            return registry.catalog_summary()
        specs = await _repo().list_strategy_specs(limit=500)
        return _strategy_catalog_from_specs(specs, mode=getattr(settings, "engine_alpha_catalog_mode", "wave1a_locked"))

    @app.get("/engine/strategies/{strategy_id}")
    async def engine_strategy(strategy_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        service = getattr(app.state, "engine_service", None)
        registry = getattr(service, "strategy_registry", None)
        if registry is not None:
            spec = registry.spec(strategy_id)
            if spec is not None:
                return spec.model_dump(mode="json")
        item = await _repo().get_strategy_spec(strategy_id)
        if item is None:
            raise HTTPException(status_code=404, detail="strategy spec not found")
        return item

    @app.get("/engine/strategy-regime-performance")
    async def engine_strategy_regime_performance(strategy_id: str | None = None, regime_label: str | None = None, limit: int = 100, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _auth(authorization)
        return await _repo().list_strategy_regime_performance(strategy_id=strategy_id, regime_label=regime_label, limit=limit)

    @app.get("/engine/strategy-regime-performance/{strategy_id}")
    async def engine_strategy_regime_performance_for_strategy(strategy_id: str, limit: int = 100, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _auth(authorization)
        return await _repo().list_strategy_regime_performance(strategy_id=strategy_id, limit=limit)

    @app.post("/engine/strategy-regime-performance/refresh")
    async def engine_strategy_regime_performance_refresh(request: EngineStrategyRegimeRefreshRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        command = await _enqueue_command(_repo(), target_role="trader", command_type="engine_strategy_regime_refresh", payload=request.model_dump(mode="json"))
        return _accepted_command(command)

    @app.post("/engine/position-theses/cleanup")
    async def engine_position_thesis_cleanup(request: EnginePositionThesisCleanupRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        command = await _enqueue_command(_repo(), target_role="trader", command_type="engine_position_thesis_cleanup", payload=request.model_dump(mode="json"))
        return _accepted_command(command)

    @app.get("/engine/candidate-trade-packets")
    async def engine_candidate_trade_packets(candidate_id: str | None = None, strategy_id: str | None = None, limit: int = 100, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _auth(authorization)
        return await _repo().list_candidate_trade_packets(candidate_id=candidate_id, strategy_id=strategy_id, limit=limit)

    @app.get("/engine/candidate-evidence-links")
    async def engine_candidate_evidence_links(candidate_id: str | None = None, strategy_id: str | None = None, limit: int = 100, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _auth(authorization)
        return await _repo().list_candidate_evidence_links(candidate_id=candidate_id, strategy_id=strategy_id, limit=limit)

    @app.get("/engine/candidate-outcome-attributions")
    async def engine_candidate_outcome_attributions(candidate_id: str | None = None, strategy_id: str | None = None, outcome_window: str | None = None, terminal_state: str | None = None, limit: int = 100, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _auth(authorization)
        return await _repo().list_candidate_outcome_attributions(candidate_id=candidate_id, strategy_id=strategy_id, outcome_window=outcome_window, terminal_state=terminal_state, limit=limit)

    @app.get("/engine/council-reviews")
    async def engine_council_reviews(candidate_id: str | None = None, strategy_id: str | None = None, decision: str | None = None, limit: int = 100, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _auth(authorization)
        return await _repo().list_council_reviews(candidate_id=candidate_id, strategy_id=strategy_id, decision=decision, limit=limit)

    @app.get("/engine/diversity-events")
    async def engine_diversity_events(strategy_id: str | None = None, decision: str | None = None, limit: int = 100, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _auth(authorization)
        return await _repo().list_allocation_diversity_events(strategy_id=strategy_id, decision=decision, limit=limit)

    @app.get("/engine/portfolio-concentration-events")
    async def engine_portfolio_concentration_events(strategy_id: str | None = None, decision: str | None = None, limit: int = 100, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _auth(authorization)
        return await _repo().list_portfolio_concentration_events(strategy_id=strategy_id, decision=decision, limit=limit)

    @app.get("/engine/replay-result-links")
    async def engine_replay_result_links(replay_id: str | None = None, candidate_id: str | None = None, limit: int = 100, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _auth(authorization)
        return await _repo().list_replay_result_links(replay_id=replay_id, candidate_id=candidate_id, limit=limit)

    @app.get("/engine/bandit-recommendations")
    async def engine_bandit_recommendations(strategy_id: str | None = None, policy_id: str | None = None, limit: int = 100, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _auth(authorization)
        return await _repo().list_bandit_recommendations(strategy_id=strategy_id, policy_id=policy_id, limit=limit)

    @app.get("/engine/alpha-graph")
    async def engine_alpha_graph(limit: int = 1000, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        return await build_strategy_regime_alpha_graph(_repo(), limit=limit)

    @app.post("/engine/bandit-recommendations/run")
    async def engine_bandit_recommendations_run(request: EngineBanditRecommendationRunRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        command = await _enqueue_command(_repo(), target_role="trader", command_type="engine_bandit_run", payload=request.model_dump(mode="json"))
        return _accepted_command(command)

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
        return await build_engine_validation_report(_repo(), limit=limit, settings=settings)

    @app.get("/engine/signal-comparison")
    async def engine_signal_comparison(
        window_hours: int = 24,
        limit: int = 5000,
        overlap_tolerance_minutes: int = 30,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        return await build_signal_path_comparison(
            _repo(),
            settings=settings,
            window_hours=max(1, min(24 * 90, window_hours)),
            limit=max(1, min(20_000, limit)),
            overlap_tolerance_minutes=max(1, min(24 * 60, overlap_tolerance_minutes)),
        )

    @app.get("/engine/signal-quality")
    async def engine_signal_quality(
        window_hours: int = 24,
        as_of_ms: int | None = None,
        strategy_id: str | None = None,
        symbol: str | None = None,
        regime_label: str | None = None,
        outcome_window: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        return await build_signal_quality_report(
            _repo(),
            window_hours=max(1, min(24 * 90, window_hours)),
            as_of_ms=as_of_ms,
            strategy_id=strategy_id,
            symbol=symbol,
            regime_label=regime_label,
            outcome_window=outcome_window,
        )

    @app.post("/engine/news-risk-counterfactuals/run")
    async def engine_news_risk_counterfactual_run(
        request: EngineNewsRiskCounterfactualRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        return await run_news_risk_counterfactual(
            _repo(),
            window_hours=request.window_hours,
            as_of_ms=request.as_of_ms,
            persist=True,
        )

    @app.get("/engine/news-risk-counterfactuals")
    async def engine_news_risk_counterfactual_list(
        limit: int = 100,
        authorization: str | None = Header(default=None),
    ) -> list[dict[str, Any]]:
        _auth(authorization)
        return await list_news_risk_counterfactuals(_repo(), limit=max(1, min(1000, limit)))

    @app.get("/engine/news-risk-counterfactuals/latest")
    async def engine_news_risk_counterfactual_latest(
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        item = await latest_news_risk_counterfactual(_repo())
        if item is None:
            raise HTTPException(status_code=404, detail="news risk counterfactual not found")
        return item

    @app.get("/engine/readiness")
    async def engine_readiness(window_hours: int | None = None, limit: int = 1000, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        service = getattr(app.state, "engine_service", None)
        return await build_paper_readiness_scorecard(_repo(), settings, service, window_hours=window_hours, limit=limit)

    @app.get("/engine/paper-signoff/preflight")
    async def engine_paper_signoff_preflight(symbols: str | None = None, window_hours: int | None = None, limit: int = 1000, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        service = getattr(app.state, "engine_service", None)
        symbol_list = [item.strip().upper() for item in (symbols or "").split(",") if item.strip()]
        return await build_paper_signoff_preflight(_repo(), settings, service, symbols=symbol_list, window_hours=window_hours, limit=limit)

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
        command = await _enqueue_command(_repo(), target_role="trader", command_type="engine_replay_comparison_run", payload=request.model_dump(mode="json"))
        return _accepted_command(command)

    @app.get("/engine/dashboard", response_class=HTMLResponse)
    async def engine_dashboard(limit: int = 500, authorization: str | None = Header(default=None)) -> HTMLResponse:
        _auth(authorization)
        report = await build_engine_validation_report(_repo(), limit=limit, settings=settings)
        return HTMLResponse(render_engine_validation_dashboard(report))

    @app.get("/engine/retention")
    async def engine_retention(limit: int = 100, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        _auth(authorization)
        return await _repo().list_retention_runs(limit=limit)
