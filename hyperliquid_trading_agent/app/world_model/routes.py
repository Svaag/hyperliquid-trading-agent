from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.world_model.reducer import now_ms
from hyperliquid_trading_agent.app.world_model.schemas import PredictionMarketSignal, WorldEvent
from hyperliquid_trading_agent.app.world_model.v2_schemas import EvidenceV2

RequireAuth = Callable[[Settings, str | None], None]
_TEMPLATES = Path(__file__).with_name("templates")
_STATIC = Path(__file__).with_name("static")


class AnnotationRequest(BaseModel):
    target_type: Literal["event", "belief", "prediction_signal", "memory", "source", "narrative"]
    target_id: str
    action: Literal["confirmed", "disputed", "needs_review", "pinned"]
    note: str = ""
    actor_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class OutcomeRequest(BaseModel):
    target_type: Literal["event", "belief", "prediction_signal", "source"]
    target_id: str
    outcome: str
    symbol: str | None = None
    horizon: str | None = None
    realized_value: float | None = None
    confidence_delta: float = Field(default=0.05, ge=0.0, le=0.5)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SeedRequest(BaseModel):
    symbol: str = "BTC"
    topic: str = "macro"
    actor_id: str | None = "dashboard_seed"


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


def register_world_model_routes(app: FastAPI, settings: Settings, require_auth: RequireAuth) -> None:
    def _auth(authorization: str | None) -> None:
        require_auth(settings, authorization)

    def _service():
        service = getattr(app.state, "world_model_service", None)
        if service is None:
            raise HTTPException(status_code=503, detail="world model service unavailable")
        return service

    def _adapter_service():
        service = getattr(app.state, "world_model_adapter_service", None)
        if service is None:
            raise HTTPException(status_code=503, detail="world model adapter service unavailable")
        return service

    def _stream_service():
        return getattr(app.state, "world_model_stream_service", None)

    @app.get("/world-model/dashboard", response_class=HTMLResponse)
    async def world_model_dashboard() -> HTMLResponse:
        return HTMLResponse((_TEMPLATES / "dashboard.html").read_text(encoding="utf-8"))

    @app.get("/world-model/dashboard/app.js")
    async def world_model_dashboard_js() -> Response:
        return Response((_STATIC / "dashboard.js").read_text(encoding="utf-8"), media_type="application/javascript")

    @app.get("/world-model/dashboard/app.css")
    async def world_model_dashboard_css() -> Response:
        return Response((_STATIC / "dashboard.css").read_text(encoding="utf-8"), media_type="text/css")

    @app.get("/world-model/dashboard/data")
    async def world_model_dashboard_data(
        symbol: str | None = None,
        topic: str | None = None,
        limit: int = 100,
        mode: str = "tree",
        as_of_ms: int | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        stream_service = _stream_service()
        return await _dashboard_data(
            _service(),
            symbol=symbol,
            topic=topic,
            limit=limit,
            mode=mode,
            as_of_ms=as_of_ms,
            streams=stream_service.status() if stream_service is not None else {},
        )

    @app.get("/world-model/status")
    async def world_model_status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        return _service().status()

    @app.get("/world-model/snapshot")
    async def world_model_snapshot(
        symbol: str | None = None,
        topic: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        snapshots = await _service().list_snapshots(limit=1, symbol=symbol, topic=topic)
        if snapshots:
            return snapshots[0]
        symbols = [symbol.upper()] if symbol else None
        topics = [topic.lower()] if topic else None
        return _service().snapshot(symbols=symbols, topics=topics).model_dump(mode="json")

    @app.get("/world-model/events")
    async def world_model_events(
        limit: int = 100,
        source_type: str | None = None,
        symbol: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        service = _service()
        if service.status().get("version") == 2:
            items = await service.list_evidence(limit=limit)
        else:
            items = await service.list_events(limit=limit, source_type=source_type, symbol=symbol)
        return {"items": items, "count": len(items)}

    @app.get("/world-model/beliefs")
    async def world_model_beliefs(
        limit: int = 100,
        symbol: str | None = None,
        kind: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        service = _service()
        if service.status().get("version") == 2:
            states = await service.list_macro_states(limit=limit)
            impacts = await service.list_asset_impacts(limit=limit, instrument_id=symbol)
            items = [
                {"assertion_type": "macro_state", **item} for item in states
            ] + [{"assertion_type": "asset_impact", **item} for item in impacts]
            items = items[:limit]
        else:
            items = await service.list_beliefs(limit=limit, symbol=symbol, kind=kind)
        return {"items": items, "count": len(items)}

    @app.get("/world-model/prediction-markets")
    async def world_model_prediction_markets(
        limit: int = 100,
        venue: str | None = None,
        symbol: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        service = _service()
        if service.status().get("version") == 2:
            items = await service.list_forecasts(limit=limit)
            if symbol:
                items = [item for item in items if symbol.upper() in item.get("instrument_ids", [])]
        else:
            items = await service.list_prediction_signals(limit=limit, venue=venue, symbol=symbol)
        return {"items": items, "count": len(items)}

    @app.get("/world-model/macro-state")
    async def world_model_macro_state(limit: int = 100, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        service = _service()
        if service.status().get("version") != 2:
            raise HTTPException(status_code=404, detail="world model v2 is disabled")
        items = await service.list_macro_states(limit=limit)
        return {"items": items, "count": len(items)}

    @app.get("/world-model/asset-impacts")
    async def world_model_asset_impacts(instrument_id: str | None = None, limit: int = 200, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        service = _service()
        if service.status().get("version") != 2:
            raise HTTPException(status_code=404, detail="world model v2 is disabled")
        items = await service.list_asset_impacts(limit=limit, instrument_id=instrument_id)
        return {"items": items, "count": len(items)}

    @app.get("/world-model/quality")
    async def world_model_quality(limit: int = 100, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        service = _service()
        if service.status().get("version") != 2:
            raise HTTPException(status_code=404, detail="world model v2 is disabled")
        quarantined = await service.list_evidence(limit=limit, admission_status="quarantined")
        rejected = await service.list_evidence(limit=limit, admission_status="rejected")
        prediction_quarantine = await service.list_prediction_markets(limit=limit, admission_status="quarantined")
        snapshot = service.snapshot().model_dump(mode="json")
        return {"quality_flags": snapshot.get("quality_flags", []), "coverage": snapshot.get("coverage", {}), "quarantined": quarantined, "rejected": rejected, "prediction_quarantine": prediction_quarantine}

    @app.get("/world-model/memory")
    async def world_model_memory(
        limit: int = 100,
        symbol: str | None = None,
        memory_type: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        service = _service()
        items = [] if service.status().get("version") == 2 else await service.list_memory(limit=limit, symbol=symbol, memory_type=memory_type)
        return {"items": items, "count": len(items)}

    @app.post("/world-model/annotations")
    async def world_model_annotation_create(request: AnnotationRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        item = await _service().annotate(
            target_type=request.target_type,
            target_id=request.target_id,
            action=request.action,
            note=request.note,
            actor_id=request.actor_id,
            metadata=request.metadata,
        )
        return {"item": item.model_dump(mode="json")}

    @app.get("/world-model/annotations")
    async def world_model_annotations(
        target_type: str | None = None,
        target_id: str | None = None,
        action: str | None = None,
        limit: int = 100,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        items = await _service().list_annotations(target_type=target_type, target_id=target_id, action=action, limit=limit)
        return {"items": items, "count": len(items)}

    @app.post("/world-model/outcomes")
    async def world_model_outcome_create(request: OutcomeRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        item = await _service().record_outcome(
            target_type=request.target_type,
            target_id=request.target_id,
            outcome=request.outcome,
            symbol=request.symbol,
            horizon=request.horizon,
            realized_value=request.realized_value,
            confidence_delta=request.confidence_delta,
            metadata=request.metadata,
        )
        return {"item": item.model_dump(mode="json")}

    @app.get("/world-model/outcomes")
    async def world_model_outcomes(
        target_type: str | None = None,
        target_id: str | None = None,
        limit: int = 100,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        items = await _service().list_outcomes(target_type=target_type, target_id=target_id, limit=limit)
        return {"items": items, "count": len(items)}

    @app.get("/world-model/prediction-calibration")
    async def world_model_prediction_calibration(
        signal_id: str | None = None,
        venue: str | None = None,
        limit: int = 100,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        items = await _service().list_calibrations(signal_id=signal_id, venue=venue, limit=limit)
        return {"items": items, "count": len(items)}

    @app.get("/world-model/snapshots")
    async def world_model_snapshots(
        limit: int = 100,
        symbol: str | None = None,
        topic: str | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        items = await _service().list_snapshots(limit=limit, symbol=symbol, topic=topic, start_ms=start_ms, end_ms=end_ms)
        return {"items": items, "count": len(items)}

    @app.get("/world-model/snapshots/nearest")
    async def world_model_snapshot_nearest(
        as_of_ms: int,
        symbol: str | None = None,
        topic: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        item = await _service().nearest_snapshot(as_of_ms=as_of_ms, symbol=symbol, topic=topic)
        if item is None:
            raise HTTPException(status_code=404, detail="snapshot not found")
        return {"item": item}

    @app.get("/world-model/snapshots/{snapshot_id}")
    async def world_model_snapshot_get(snapshot_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        item = await _service().get_snapshot(snapshot_id)
        if item is None:
            raise HTTPException(status_code=404, detail="snapshot not found")
        return {"item": item}

    @app.get("/world-model/replay")
    async def world_model_replay(
        start_ms: int,
        end_ms: int,
        symbol: str | None = None,
        topic: str | None = None,
        limit: int = 200,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        return await _service().replay(start_ms=start_ms, end_ms=end_ms, symbol=symbol, topic=topic, limit=limit)

    @app.get("/world-model/repository/health")
    async def world_model_repository_health(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        return await _service().repository_health()

    @app.get("/world-model/adapters/status")
    async def world_model_adapters_status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        return _adapter_service().status()

    @app.get("/world-model/streams/status")
    async def world_model_streams_status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        stream_service = _stream_service()
        if stream_service is None:
            return {"enabled": False, "running": False, "streams": [], "execution_authority": "none"}
        return stream_service.status()

    @app.post("/world-model/adapters/poll")
    async def world_model_adapters_poll(force: bool = False, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        command = await app.state.repository.enqueue_worker_command(target_role="world_model", command_type="world_model_adapter_poll", payload={"force": force}, requested_by="api")
        return _accepted_command(command)

    @app.post("/world-model/adapters/{adapter_name}/poll")
    async def world_model_adapter_poll(adapter_name: str, force: bool = False, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        command = await app.state.repository.enqueue_worker_command(target_role="world_model", command_type="world_model_adapter_poll", payload={"adapter_name": adapter_name, "force": force}, requested_by="api")
        return _accepted_command(command)

    @app.post("/world-model/dev/seed")
    async def world_model_dev_seed(request: SeedRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        if not _seed_allowed(settings):
            raise HTTPException(status_code=403, detail="world model seed endpoint is disabled")
        if settings.environment.lower() in {"test"}:
            return await _seed_world_model(_service(), request, settings)
        command = await app.state.repository.enqueue_worker_command(target_role="world_model", command_type="world_model_dev_seed", payload=request.model_dump(mode="json"), requested_by="api")
        return _accepted_command(command)


async def _dashboard_data(
    service: Any,
    *,
    symbol: str | None,
    topic: str | None,
    limit: int,
    mode: str = "tree",
    as_of_ms: int | None = None,
    streams: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bounded_limit = min(500, max(1, int(limit or 100)))
    if service.status().get("version") == 2:
        snapshot = service.snapshot(symbols=[symbol.upper()] if symbol else None)
        snapshot_data = snapshot.model_dump(mode="json")
        evidence = await service.list_evidence(limit=bounded_limit)
        quarantined = await service.list_evidence(limit=bounded_limit, admission_status="quarantined")
        quarantined_predictions = await service.list_prediction_markets(limit=bounded_limit, admission_status="quarantined")
        forecasts = await service.list_forecasts(limit=bounded_limit)
        impacts = await service.list_asset_impacts(limit=bounded_limit, instrument_id=symbol)
        states = await service.list_macro_states(limit=bounded_limit)
        return {
            "status": service.status(),
            "filters": {"symbol": symbol.upper() if symbol else None, "topic": topic, "limit": bounded_limit, "mode": "macro", "as_of_ms": as_of_ms},
            "snapshot": snapshot_data,
            "summary": {
                "macro_factors": len(states), "asset_impacts": len(impacts), "relevant_forecasts": len(forecasts),
                "evidence": len(evidence), "quarantined": len(quarantined) + len(quarantined_predictions), "quality_flags": snapshot_data.get("quality_flags", []),
            },
            "macro_state": {"items": states, "count": len(states)},
            "asset_impacts": {"items": impacts, "count": len(impacts)},
            "prediction_markets": {"items": forecasts, "count": len(forecasts)},
            "evidence": {"items": evidence, "count": len(evidence)},
            "quality": {"items": [{"record_type": "evidence", **item} for item in quarantined] + [{"record_type": "prediction_market", **item} for item in quarantined_predictions], "count": len(quarantined) + len(quarantined_predictions), "coverage": snapshot_data.get("coverage", {})},
            # Compatibility aliases contain typed v2 assertions, never raw quotes.
            "events": {"items": evidence, "count": len(evidence)},
            "beliefs": {"items": [{"assertion_type": "macro_state", **item} for item in states] + [{"assertion_type": "asset_impact", **item} for item in impacts], "count": len(states) + len(impacts)},
            "memory": {"items": [], "count": 0}, "annotations": {"items": [], "count": 0},
            "outcomes": {"items": [], "count": 0}, "prediction_calibration": {"items": [], "count": 0},
            "graph": _build_v2_graph(snapshot_data), "streams": streams or {},
        }
    graph_mode = mode if mode in {"tree", "timeline", "contradictions", "prediction_consensus", "source_reliability"} else "tree"
    normalized_symbol = symbol.upper().strip() if symbol and symbol.strip() else None
    normalized_topic = topic.lower().strip() if topic and topic.strip() else None
    symbols = [normalized_symbol] if normalized_symbol else None
    topics = [normalized_topic] if normalized_topic else None
    historical = False
    if as_of_ms is not None:
        snapshot_data = await service.nearest_snapshot(as_of_ms=as_of_ms, symbol=normalized_symbol, topic=normalized_topic)
        historical = snapshot_data is not None
    else:
        snapshot_data = None
    if snapshot_data is None:
        snapshot = service.snapshot(symbols=symbols, topics=topics, max_beliefs=min(100, bounded_limit), as_of_ms=as_of_ms)
        snapshot_data = snapshot.model_dump(mode="json")
    query_limit = min(1_000, bounded_limit * 3 if normalized_topic else bounded_limit)
    events = _filter_topic(await service.list_events(limit=query_limit, symbol=normalized_symbol), normalized_topic)[:bounded_limit]
    if as_of_ms is not None:
        cutoff = int(snapshot_data.get("as_of_ms") or as_of_ms)
        events = [item for item in events if int(item.get("computed_ts_ms") or item.get("received_ts_ms") or 0) <= cutoff][:bounded_limit]
        beliefs = _filter_topic(snapshot_data.get("top_beliefs", []), normalized_topic)[:bounded_limit]
        predictions = _filter_topic(snapshot_data.get("prediction_market_signals", []), normalized_topic)[:bounded_limit]
        memories = _filter_topic(snapshot_data.get("memory_atoms", []), normalized_topic)[:bounded_limit]
    else:
        beliefs = _filter_topic(await service.list_beliefs(limit=query_limit, symbol=normalized_symbol), normalized_topic)[:bounded_limit]
        predictions = _filter_topic(await service.list_prediction_signals(limit=query_limit, symbol=normalized_symbol), normalized_topic)[:bounded_limit]
        memories = _filter_topic(await service.list_memory(limit=query_limit, symbol=normalized_symbol), normalized_topic)[:bounded_limit]
    annotations = await service.list_annotations(limit=bounded_limit)
    outcomes = await service.list_outcomes(limit=bounded_limit)
    calibrations = await service.list_calibrations(limit=bounded_limit)
    if as_of_ms is not None:
        cutoff = int(snapshot_data.get("as_of_ms") or as_of_ms)
        annotations = [item for item in annotations if int(item.get("created_at_ms") or 0) <= cutoff]
        outcomes = [item for item in outcomes if int(item.get("created_at_ms") or 0) <= cutoff]
        calibrations = [item for item in calibrations if int(item.get("created_at_ms") or 0) <= cutoff]
    graph = _build_world_model_graph(
        snapshot=snapshot_data,
        events=events,
        beliefs=beliefs or snapshot_data.get("top_beliefs", []),
        predictions=predictions or snapshot_data.get("prediction_market_signals", []),
        memories=memories or snapshot_data.get("memory_atoms", []),
        annotations=annotations,
        mode=graph_mode,
    )
    return {
        "status": service.status(),
        "filters": {
            "symbol": normalized_symbol,
            "topic": normalized_topic,
            "limit": bounded_limit,
            "mode": graph_mode,
            "as_of_ms": as_of_ms,
            "historical_snapshot": historical,
        },
        "snapshot": snapshot_data,
        "summary": {
            "events": len(events),
            "beliefs": len(beliefs),
            "prediction_market_signals": len(predictions),
            "memory_atoms": len(memories),
            "narrative_clusters": len(snapshot_data.get("narrative_clusters", [])),
            "annotations": len(annotations),
            "outcomes": len(outcomes),
            "quality_flags": snapshot_data.get("quality_flags", []),
        },
        "graph": graph,
        "events": {"items": events, "count": len(events)},
        "beliefs": {"items": beliefs, "count": len(beliefs)},
        "prediction_markets": {"items": predictions, "count": len(predictions)},
        "memory": {"items": memories, "count": len(memories)},
        "annotations": {"items": annotations, "count": len(annotations)},
        "outcomes": {"items": outcomes, "count": len(outcomes)},
        "prediction_calibration": {"items": calibrations, "count": len(calibrations)},
        "streams": streams or {},
    }


def _build_v2_graph(snapshot: dict[str, Any]) -> dict[str, Any]:
    nodes = [{"id": "world", "type": "world", "label": "World Model v2", "score": 1.0, "data": {"as_of_ms": snapshot.get("as_of_ms"), "quality_flags": snapshot.get("quality_flags", [])}}]
    edges: list[dict[str, Any]] = []
    seen = {"world"}
    for state in snapshot.get("macro_states", []):
        node_id = f"factor:{state.get('factor_id')}"
        nodes.append({"id": node_id, "type": "narrative", "label": f"{state.get('factor_id')}: {state.get('regime')}", "score": float(state.get("coverage") or 0), "data": state})
        edges.append({"source": "world", "target": node_id, "type": "macro_state", "weight": max(0.1, float(state.get("coverage") or 0)), "label": str(state.get("semantic_axis") or "factor")})
        seen.add(node_id)
    for impact in snapshot.get("asset_impacts", []):
        asset_id = f"asset:{impact.get('instrument_id')}"
        if asset_id not in seen:
            nodes.append({"id": asset_id, "type": "scope", "label": str(impact.get("instrument_id")), "score": float(impact.get("strength") or 0), "data": {"instrument_id": impact.get("instrument_id")}})
            seen.add(asset_id)
        factor_id = f"factor:{impact.get('factor_id')}"
        if factor_id in seen:
            edges.append({"source": factor_id, "target": asset_id, "type": "asset_impact", "weight": max(0.1, float(impact.get("strength") or 0)), "label": f"{impact.get('horizon')} {impact.get('direction')} ({impact.get('mode')})"})
    for forecast in snapshot.get("forecasts", []):
        forecast_id = f"prediction:{forecast.get('hypothesis_id')}"
        nodes.append({"id": forecast_id, "type": "prediction_market", "label": str(forecast.get("question") or "forecast"), "score": float(forecast.get("confidence") or 0), "data": forecast})
        edges.append({"source": "world", "target": forecast_id, "type": "prediction_market", "weight": max(0.1, float(forecast.get("confidence") or 0)), "label": _probability_label(forecast.get("yes_probability"))})
        for instrument in forecast.get("instrument_ids", []):
            asset_id = f"asset:{instrument}"
            if asset_id in seen:
                edges.append({"source": forecast_id, "target": asset_id, "type": "conditional", "weight": 0.5, "label": "conditional scenario"})
    return {"nodes": nodes, "edges": edges}


def _seed_allowed(settings: Settings) -> bool:
    environment = str(settings.environment or "").lower()
    return bool(settings.world_model_dev_seed_enabled) and (settings.runtime_profile == "dashboard_only" or environment in {"test", "local", "dev", "development"})


async def _seed_world_model(service: Any, request: SeedRequest, settings: Settings) -> dict[str, Any]:
    symbol = request.symbol.upper().strip() or "BTC"
    topic = request.topic.lower().strip() or "macro"
    ts = now_ms()
    if service.status().get("version") == 2:
        factor_ids = [topic] if topic in {"inflation", "labor", "growth", "policy_stance", "rates", "real_rates", "usd", "liquidity", "financial_conditions"} else []
        evidence = EvidenceV2(
            evidence_id=f"wm2_seed_{symbol}_{topic}_{ts}", source_type="operator", source="dashboard_seed", provider="local",
            title=f"Seeded {symbol} {topic} v2 evidence", available_at_ms=ts, observed_at_ms=ts,
            admission_status="admitted", admission_reason_codes=["operator_seed"], factor_ids=factor_ids,
            instrument_ids=[symbol], metadata={"shadow_only": True, "execution_authority": "none"},
        )
        await service.observe_evidence(evidence)
        await service.persist_snapshot(force=True)
        return {"seeded": True, "version": 2, "symbol": symbol, "topic": topic, "evidence": 1, "snapshot_id": service.snapshot().snapshot_id, "execution_authority": "none"}
    base = ts - 15 * 60 * 1000
    events = [
        WorldEvent(
            event_id=f"wevt_seed_{symbol.lower()}_bullish_flow",
            source_type="newswire",
            source="dashboard_seed",
            provider="internal",
            event_type="macro_flow",
            asset_class="crypto",
            symbols=[symbol],
            topics=[topic, "seed", "flows"],
            title=f"{symbol} bid strengthens as ETF and macro flow improve",
            body=f"Seed event: advisory-only {symbol} catalyst used for dashboard smoke testing.",
            received_ts_ms=base,
            computed_ts_ms=base + 1,
            importance_score=78.0,
            sentiment="bullish",
            confidence=0.74,
            source_score=0.72,
            quality_score=0.8,
            metadata={"seeded": True, "paper_only": True, "execution_authority": "none"},
        ),
        WorldEvent(
            event_id=f"wevt_seed_{symbol.lower()}_bearish_positioning",
            source_type="social",
            source="dashboard_seed_social",
            provider="internal",
            event_type="positioning_warning",
            asset_class="crypto",
            symbols=[symbol],
            topics=[topic, "seed", "positioning"],
            title=f"{symbol} social positioning looks crowded into resistance",
            body=f"Seed event: contradictory social/positioning read for {symbol}.",
            received_ts_ms=base + 4 * 60 * 1000,
            computed_ts_ms=base + 4 * 60 * 1000 + 1,
            importance_score=62.0,
            sentiment="bearish",
            confidence=0.62,
            source_score=0.52,
            quality_score=0.55,
            metadata={"seeded": True, "paper_only": True, "execution_authority": "none"},
        ),
    ]
    signal = PredictionMarketSignal(
        signal_id=f"pm_seed_{symbol.lower()}_macro_yes",
        venue="dashboard_seed",
        market_id=f"seed:{symbol}:macro",
        question=f"Will {symbol} trade higher after the next macro catalyst?",
        outcome_id="yes",
        outcome_name="YES",
        symbols=[symbol],
        topics=["prediction_market", topic, "seed"],
        implied_probability=0.64,
        probability_delta=0.04,
        best_bid=0.62,
        best_ask=0.66,
        liquidity_usd=25_000.0,
        volume_usd=90_000.0,
        status="open",
        as_of_ms=base + 8 * 60 * 1000,
        confidence=0.7,
        metadata={"seeded": True, "source_id": f"seed:{symbol}:macro:yes", "paper_only": True, "execution_authority": "none"},
    )
    for event in events:
        await service.observe_event(event)
    await service.observe_prediction_market_signal(signal)
    snapshot = service.snapshot(symbols=[symbol], topics=[topic], max_beliefs=20)
    if snapshot.top_beliefs:
        await service.annotate(
            target_type="belief",
            target_id=snapshot.top_beliefs[0].belief_id,
            action="needs_review",
            note="Seeded dashboard supervision item.",
            actor_id=request.actor_id,
            metadata={"seeded": True},
        )
    await service.record_outcome(
        target_type="event",
        target_id=events[0].event_id,
        outcome="worked",
        symbol=symbol,
        horizon="seed",
        confidence_delta=0.03,
        metadata={"seeded": True},
    )
    await service.record_outcome(
        target_type="prediction_signal",
        target_id=signal.signal_id,
        outcome="yes",
        symbol=symbol,
        horizon="seed",
        realized_value=1.0,
        metadata={"seeded": True},
    )
    await service.persist_snapshot(service.snapshot(symbols=[symbol], topics=[topic], max_beliefs=20), force=True)
    return {
        "seeded": True,
        "symbol": symbol,
        "topic": topic,
        "events": len(events),
        "prediction_market_signals": 1,
        "annotations": 1 if snapshot.top_beliefs else 0,
        "outcomes": 2,
        "snapshot_id": snapshot.snapshot_id,
        "execution_authority": "none",
        "settings_environment": settings.environment,
    }


def _build_world_model_graph(
    *,
    snapshot: dict[str, Any],
    events: list[dict[str, Any]],
    beliefs: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    memories: list[dict[str, Any]],
    annotations: list[dict[str, Any]] | None = None,
    mode: str = "tree",
) -> dict[str, Any]:
    if mode == "timeline":
        return _build_timeline_graph(snapshot=snapshot, events=events, predictions=predictions, annotations=annotations or [])
    if mode == "contradictions":
        return _build_contradiction_graph(snapshot=snapshot, beliefs=beliefs, annotations=annotations or [])
    if mode == "prediction_consensus":
        return _build_prediction_consensus_graph(snapshot=snapshot, predictions=predictions, annotations=annotations or [])
    if mode == "source_reliability":
        return _build_source_reliability_graph(snapshot=snapshot, events=events, annotations=annotations or [])
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen_nodes: set[str] = set()
    seen_edges: set[tuple[str, str, str]] = set()
    events_by_id = {str(item.get("event_id")): item for item in events if item.get("event_id")}
    beliefs_by_id = {str(item.get("belief_id")): item for item in beliefs if item.get("belief_id")}
    annotations_by_target = _annotations_by_target(annotations or [])

    def add_node(node_id: str, node_type: str, label: str, *, score: float = 0.0, data: dict[str, Any] | None = None) -> None:
        if node_id in seen_nodes:
            return
        seen_nodes.add(node_id)
        node_data = data or {}
        node_annotations = _node_annotations(node_id, annotations_by_target)
        if node_annotations:
            node_data = {**node_data, "annotations": node_annotations}
        nodes.append(
            {
                "id": node_id,
                "type": node_type,
                "label": _short_label(label, 72),
                "score": round(float(score or 0.0), 4),
                "data": node_data,
            }
        )

    def add_edge(source: str, target: str, edge_type: str, *, weight: float = 1.0, label: str = "") -> None:
        key = (source, target, edge_type)
        if key in seen_edges:
            return
        if source not in seen_nodes or target not in seen_nodes:
            return
        seen_edges.add(key)
        edges.append({"source": source, "target": target, "type": edge_type, "weight": round(float(weight or 0.0), 4), "label": label})

    add_node(
        "world",
        "world",
        "Market World Model",
        score=1.0,
        data={
            "summary": snapshot.get("summary", ""),
            "as_of_ms": snapshot.get("as_of_ms"),
            "quality_flags": snapshot.get("quality_flags", []),
        },
    )

    cluster_for_belief: dict[str, str] = {}
    for cluster in snapshot.get("narrative_clusters", [])[:24]:
        cluster_id = f"cluster:{cluster.get('cluster_id')}"
        label = str(cluster.get("title") or _scope_label(cluster) or "Narrative")
        add_node(cluster_id, "narrative", label, score=abs(float(cluster.get("pressure_score") or 0.0)), data=_node_data(cluster))
        add_edge("world", cluster_id, "narrative", weight=max(0.1, abs(float(cluster.get("pressure_score") or 0.0))), label="narrative")
        for belief_id in cluster.get("belief_ids") or []:
            cluster_for_belief[str(belief_id)] = cluster_id

    prediction_root = "category:prediction_markets"
    memory_root = "category:memory"
    source_root = "category:sources"
    if predictions:
        add_node(prediction_root, "category", "Prediction Markets", score=0.8, data={"count": len(predictions)})
        add_edge("world", prediction_root, "category", weight=0.8, label="oracle")
    if memories:
        add_node(memory_root, "category", "Memory", score=0.7, data={"count": len(memories)})
        add_edge("world", memory_root, "category", weight=0.7, label="memory")
    if snapshot.get("source_credibility"):
        add_node(source_root, "category", "Sources", score=0.6, data={"count": len(snapshot.get("source_credibility", []))})
        add_edge("world", source_root, "category", weight=0.6, label="credibility")

    for belief in beliefs[:80]:
        belief_id_raw = str(belief.get("belief_id") or "")
        if not belief_id_raw:
            continue
        belief_id = f"belief:{belief_id_raw}"
        parent_id = cluster_for_belief.get(belief_id_raw)
        if parent_id is None:
            parent_id = _scope_node_id(belief)
            add_node(parent_id, "scope", _scope_label(belief), score=float(belief.get("salience") or 0.0), data={"scope": _scope_label(belief)})
            add_edge("world", parent_id, "scope", weight=max(0.1, float(belief.get("salience") or 0.0)), label="scope")
        add_node(
            belief_id,
            "belief",
            str(belief.get("statement") or belief.get("subject") or belief_id_raw),
            score=float(belief.get("salience") or belief.get("confidence") or 0.0),
            data=_node_data(belief),
        )
        add_edge(parent_id, belief_id, "belief", weight=max(0.1, float(belief.get("confidence") or 0.0)), label=str(belief.get("kind") or "belief"))
        for event_id_raw in (belief.get("evidence_event_ids") or [])[:8]:
            event = events_by_id.get(str(event_id_raw))
            if event is None:
                continue
            event_id = f"event:{event_id_raw}"
            add_node(event_id, "event", str(event.get("title") or event.get("event_type") or event_id_raw), score=float(event.get("importance_score") or 0.0) / 100.0, data=_node_data(event))
            add_edge(belief_id, event_id, "evidence", weight=max(0.1, float(event.get("confidence") or 0.0)), label=str(event.get("source_type") or "evidence"))
        for contradicted_id_raw in (belief.get("contradicts_belief_ids") or [])[:6]:
            contradicted = beliefs_by_id.get(str(contradicted_id_raw))
            if contradicted is None:
                continue
            contradicted_id = f"belief:{contradicted_id_raw}"
            add_node(
                contradicted_id,
                "belief",
                str(contradicted.get("statement") or contradicted.get("subject") or contradicted_id_raw),
                score=float(contradicted.get("salience") or contradicted.get("confidence") or 0.0),
                data=_node_data(contradicted),
            )
            add_edge(belief_id, contradicted_id, "contradicts", weight=1.0, label="contradicts")

    for signal in predictions[:48]:
        signal_id_raw = str(signal.get("signal_id") or "")
        if not signal_id_raw:
            continue
        signal_id = f"prediction:{signal_id_raw}"
        probability = signal.get("implied_probability")
        label = str(signal.get("question") or signal.get("market_id") or signal_id_raw)
        add_node(signal_id, "prediction_market", label, score=float(signal.get("confidence") or 0.0), data=_node_data(signal))
        add_edge(prediction_root, signal_id, "prediction_market", weight=max(0.1, float(signal.get("confidence") or 0.0)), label=_probability_label(probability))
        for event_id_raw in (signal.get("source_event_ids") or [])[:6]:
            event = events_by_id.get(str(event_id_raw))
            if event is None:
                continue
            event_id = f"event:{event_id_raw}"
            add_node(event_id, "event", str(event.get("title") or event.get("event_type") or event_id_raw), score=float(event.get("importance_score") or 0.0) / 100.0, data=_node_data(event))
            add_edge(signal_id, event_id, "source_event", weight=0.7, label="source")

    for memory in memories[:48]:
        memory_id_raw = str(memory.get("memory_id") or "")
        if not memory_id_raw:
            continue
        memory_id = f"memory:{memory_id_raw}"
        add_node(memory_id, "memory", str(memory.get("content") or memory.get("subject") or memory_id_raw), score=float(memory.get("salience") or 0.0), data=_node_data(memory))
        add_edge(memory_root, memory_id, "memory", weight=max(0.1, float(memory.get("salience") or 0.0)), label=str(memory.get("memory_type") or "memory"))
        for belief_id_raw in (memory.get("source_belief_ids") or [])[:6]:
            belief_id = f"belief:{belief_id_raw}"
            if belief_id in seen_nodes:
                add_edge(memory_id, belief_id, "remembers", weight=0.6, label="belief")
        for event_id_raw in (memory.get("source_event_ids") or [])[:6]:
            event_id = f"event:{event_id_raw}"
            if event_id in seen_nodes:
                add_edge(memory_id, event_id, "remembers", weight=0.4, label="event")

    for source in snapshot.get("source_credibility", [])[:24]:
        source_key = str(source.get("source_key") or "")
        if not source_key:
            continue
        source_id = f"source:{source_key}"
        add_node(source_id, "source", str(source.get("source") or source_key), score=float(source.get("score") or 0.0), data=_node_data(source))
        add_edge(source_root, source_id, "source", weight=max(0.1, float(source.get("score") or 0.0)), label="score")
        for event in events[:80]:
            if event.get("source") == source.get("source") and event.get("provider") == source.get("provider"):
                event_id = f"event:{event.get('event_id')}"
                if event_id in seen_nodes:
                    add_edge(source_id, event_id, "published", weight=0.4, label=str(event.get("event_type") or "event"))

    return {"nodes": nodes, "edges": edges}


def _build_timeline_graph(
    *,
    snapshot: dict[str, Any],
    events: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    annotations: list[dict[str, Any]],
) -> dict[str, Any]:
    nodes = [_node("world", "world", "Timeline", data={"as_of_ms": snapshot.get("as_of_ms"), "summary": snapshot.get("summary")})]
    edges: list[dict[str, Any]] = []
    previous = "world"
    for item in sorted(events, key=lambda row: int(row.get("computed_ts_ms") or 0))[:120]:
        node_id = f"event:{item.get('event_id')}"
        nodes.append(_node(node_id, "event", str(item.get("title") or item.get("event_type") or node_id), score=float(item.get("importance_score") or 0.0) / 100.0, data=_with_annotations(node_id, _node_data(item), annotations)))
        edges.append(_edge(previous, node_id, "next", label=str(item.get("source_type") or "event")))
        previous = node_id
    for signal in predictions[:48]:
        node_id = f"prediction:{signal.get('signal_id')}"
        nodes.append(_node(node_id, "prediction_market", str(signal.get("question") or node_id), score=float(signal.get("confidence") or 0.0), data=_with_annotations(node_id, _node_data(signal), annotations)))
        edges.append(_edge("world", node_id, "prediction_market", label=_probability_label(signal.get("implied_probability"))))
    return {"nodes": nodes, "edges": edges}


def _build_contradiction_graph(*, snapshot: dict[str, Any], beliefs: list[dict[str, Any]], annotations: list[dict[str, Any]]) -> dict[str, Any]:
    nodes = [_node("world", "world", "Contradictions", data={"as_of_ms": snapshot.get("as_of_ms")})]
    edges: list[dict[str, Any]] = []
    beliefs_by_id = {str(item.get("belief_id")): item for item in beliefs if item.get("belief_id")}
    for belief in beliefs[:120]:
        belief_id = str(belief.get("belief_id") or "")
        if not belief_id:
            continue
        node_id = f"belief:{belief_id}"
        nodes.append(_node(node_id, "belief", str(belief.get("statement") or belief.get("subject") or belief_id), score=float(belief.get("salience") or 0.0), data=_with_annotations(node_id, _node_data(belief), annotations)))
        edges.append(_edge("world", node_id, "belief", label=str(belief.get("direction") or "unknown")))
        for other_id in (belief.get("contradicts_belief_ids") or [])[:12]:
            if str(other_id) in beliefs_by_id:
                edges.append(_edge(node_id, f"belief:{other_id}", "contradicts", weight=1.0, label="contradicts"))
    return _dedupe_graph(nodes, edges)


def _build_prediction_consensus_graph(*, snapshot: dict[str, Any], predictions: list[dict[str, Any]], annotations: list[dict[str, Any]]) -> dict[str, Any]:
    nodes = [_node("world", "world", "Prediction Consensus", data={"as_of_ms": snapshot.get("as_of_ms")})]
    edges: list[dict[str, Any]] = []
    for signal in predictions[:120]:
        scope = _scope_label(signal)
        scope_id = f"scope:{scope.lower().replace(' ', '_')}"
        nodes.append(_node(scope_id, "scope", scope, score=0.5, data={"scope": scope}))
        signal_id = f"prediction:{signal.get('signal_id')}"
        nodes.append(_node(signal_id, "prediction_market", str(signal.get("question") or signal_id), score=float(signal.get("confidence") or 0.0), data=_with_annotations(signal_id, _node_data(signal), annotations)))
        edges.append(_edge("world", scope_id, "scope", label="scope"))
        edges.append(_edge(scope_id, signal_id, "prediction_market", label=_probability_label(signal.get("implied_probability"))))
    return _dedupe_graph(nodes, edges)


def _build_source_reliability_graph(*, snapshot: dict[str, Any], events: list[dict[str, Any]], annotations: list[dict[str, Any]]) -> dict[str, Any]:
    nodes = [_node("world", "world", "Source Reliability", data={"as_of_ms": snapshot.get("as_of_ms")})]
    edges: list[dict[str, Any]] = []
    for source in snapshot.get("source_credibility", [])[:80]:
        source_id = f"source:{source.get('source_key')}"
        nodes.append(_node(source_id, "source", str(source.get("source") or source_id), score=float(source.get("score") or 0.0), data=_with_annotations(source_id, _node_data(source), annotations)))
        edges.append(_edge("world", source_id, "source", label=f"score {float(source.get('score') or 0.0):.2f}"))
        for event in events[:160]:
            if event.get("source") == source.get("source") and event.get("provider") == source.get("provider"):
                event_id = f"event:{event.get('event_id')}"
                nodes.append(_node(event_id, "event", str(event.get("title") or event_id), score=float(event.get("importance_score") or 0.0) / 100.0, data=_with_annotations(event_id, _node_data(event), annotations)))
                edges.append(_edge(source_id, event_id, "published", label=str(event.get("event_type") or "event")))
    return _dedupe_graph(nodes, edges)


def _node(node_id: str, node_type: str, label: str, *, score: float = 0.0, data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": node_id, "type": node_type, "label": _short_label(label, 72), "score": round(float(score or 0.0), 4), "data": data or {}}


def _edge(source: str, target: str, edge_type: str, *, weight: float = 1.0, label: str = "") -> dict[str, Any]:
    return {"source": source, "target": target, "type": edge_type, "weight": round(float(weight or 0.0), 4), "label": label}


def _dedupe_graph(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
    out_nodes = []
    seen_nodes = set()
    for node in nodes:
        if node["id"] in seen_nodes:
            continue
        seen_nodes.add(node["id"])
        out_nodes.append(node)
    out_edges = []
    seen_edges = set()
    for edge in edges:
        key = (edge["source"], edge["target"], edge["type"])
        if key in seen_edges or edge["source"] not in seen_nodes or edge["target"] not in seen_nodes:
            continue
        seen_edges.add(key)
        out_edges.append(edge)
    return {"nodes": out_nodes, "edges": out_edges}


def _scope_node_id(item: dict[str, Any]) -> str:
    return f"scope:{_scope_label(item).lower().replace(' ', '_')}"


def _filter_topic(items: list[dict[str, Any]], topic: str | None) -> list[dict[str, Any]]:
    if not topic:
        return items
    return [item for item in items if topic in {str(value).lower() for value in item.get("topics") or []}]


def _annotations_by_target(annotations: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in annotations:
        key = (str(item.get("target_type") or ""), str(item.get("target_id") or ""))
        out.setdefault(key, []).append(item)
    return out


def _node_annotations(node_id: str, annotations_by_target: dict[tuple[str, str], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    target_type, _, target_id = node_id.partition(":")
    if target_type == "prediction":
        target_type = "prediction_signal"
    if target_type == "cluster":
        target_type = "narrative"
    return annotations_by_target.get((target_type, target_id), [])


def _with_annotations(node_id: str, data: dict[str, Any], annotations: list[dict[str, Any]]) -> dict[str, Any]:
    hits = _node_annotations(node_id, _annotations_by_target(annotations))
    return {**data, "annotations": hits} if hits else data


def _scope_label(item: dict[str, Any]) -> str:
    symbols = [str(symbol).upper() for symbol in item.get("symbols") or [] if symbol]
    topics = [str(topic).lower() for topic in item.get("topics") or [] if topic]
    if symbols:
        return symbols[0]
    if topics:
        return topics[0]
    return str(item.get("subject") or item.get("venue") or "Unscoped")


def _node_data(item: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "event_id",
        "belief_id",
        "cluster_id",
        "signal_id",
        "memory_id",
        "source_key",
        "source_type",
        "source",
        "provider",
        "kind",
        "subject",
        "statement",
        "title",
        "question",
        "outcome_name",
        "symbols",
        "topics",
        "direction",
        "sentiment",
        "status",
        "confidence",
        "salience",
        "importance_score",
        "implied_probability",
        "probability_delta",
        "liquidity_usd",
        "pressure_score",
        "consensus_score",
        "conflict_score",
        "score",
        "observations",
        "computed_ts_ms",
        "updated_at_ms",
        "as_of_ms",
        "last_reinforced_at_ms",
        "url",
        "metadata",
    ]
    return {key: item.get(key) for key in keys if key in item and item.get(key) is not None}


def _short_label(value: str, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _probability_label(value: Any) -> str:
    if value is None:
        return "p n/a"
    try:
        return f"p {float(value):.2f}"
    except (TypeError, ValueError):
        return "p n/a"
