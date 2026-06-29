from __future__ import annotations

from typing import Any, Callable, Literal

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.world_model.reducer import now_ms
from hyperliquid_trading_agent.app.world_model.schemas import PredictionMarketSignal, WorldEvent

RequireAuth = Callable[[Settings, str | None], None]


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

    @app.get("/world-model/dashboard", response_class=HTMLResponse)
    async def world_model_dashboard() -> HTMLResponse:
        return HTMLResponse(_dashboard_html_v2())

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
        return await _dashboard_data(_service(), symbol=symbol, topic=topic, limit=limit, mode=mode, as_of_ms=as_of_ms)

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
        symbols = [symbol.upper()] if symbol else None
        topics = [topic.lower()] if topic else None
        snapshot = _service().snapshot(symbols=symbols, topics=topics)
        await _service().persist_snapshot(snapshot)
        return snapshot.model_dump(mode="json")

    @app.get("/world-model/events")
    async def world_model_events(
        limit: int = 100,
        source_type: str | None = None,
        symbol: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        items = await _service().list_events(limit=limit, source_type=source_type, symbol=symbol)
        return {"items": items, "count": len(items)}

    @app.get("/world-model/beliefs")
    async def world_model_beliefs(
        limit: int = 100,
        symbol: str | None = None,
        kind: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        items = await _service().list_beliefs(limit=limit, symbol=symbol, kind=kind)
        return {"items": items, "count": len(items)}

    @app.get("/world-model/prediction-markets")
    async def world_model_prediction_markets(
        limit: int = 100,
        venue: str | None = None,
        symbol: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        items = await _service().list_prediction_signals(limit=limit, venue=venue, symbol=symbol)
        return {"items": items, "count": len(items)}

    @app.get("/world-model/memory")
    async def world_model_memory(
        limit: int = 100,
        symbol: str | None = None,
        memory_type: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        items = await _service().list_memory(limit=limit, symbol=symbol, memory_type=memory_type)
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

    @app.post("/world-model/adapters/poll")
    async def world_model_adapters_poll(force: bool = False, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        return await _adapter_service().poll(force=force)

    @app.post("/world-model/adapters/{adapter_name}/poll")
    async def world_model_adapter_poll(adapter_name: str, force: bool = False, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        return await _adapter_service().poll(adapter_name, force=force)

    @app.post("/world-model/dev/seed")
    async def world_model_dev_seed(request: SeedRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        if not _seed_allowed(settings):
            raise HTTPException(status_code=403, detail="world model seed endpoint is disabled")
        return await _seed_world_model(_service(), request, settings)


async def _dashboard_data(
    service: Any,
    *,
    symbol: str | None,
    topic: str | None,
    limit: int,
    mode: str = "tree",
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    bounded_limit = min(500, max(1, int(limit or 100)))
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
    }


def _seed_allowed(settings: Settings) -> bool:
    environment = str(settings.environment or "").lower()
    return bool(settings.world_model_dev_seed_enabled) and (settings.runtime_profile == "dashboard_only" or environment in {"test", "local", "dev", "development"})


async def _seed_world_model(service: Any, request: SeedRequest, settings: Settings) -> dict[str, Any]:
    symbol = request.symbol.upper().strip() or "BTC"
    topic = request.topic.lower().strip() or "macro"
    ts = now_ms()
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


def _dashboard_html() -> str:
    return """
<!doctype html><html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/><title>World Model Dashboard</title>
<style>
:root{color-scheme:dark;--bg:#101113;--panel:#17191d;--panel2:#1f2329;--line:#303640;--text:#eef2f5;--muted:#9ba6b2;--accent:#2dd4bf;--good:#76d672;--warn:#f6b73c;--bad:#f36f56;--blue:#7aa8ff;--violet:#c38cff}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}header{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;padding:18px 20px;border-bottom:1px solid var(--line);background:#131518}h1{margin:0 0 5px;font-size:22px;letter-spacing:0}h2{margin:0 0 10px;font-size:15px}h3{margin:18px 0 8px;font-size:13px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}.muted{color:var(--muted)}.controls{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}input,select,button{height:34px;background:#111317;color:var(--text);border:1px solid var(--line);border-radius:7px;padding:0 10px;font:inherit}button{cursor:pointer;background:#20252c}button:hover{border-color:var(--accent)}main{display:grid;gap:14px;padding:14px 18px 22px}.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px}.metric,.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px}.metric{padding:11px 12px;min-height:70px}.metric b{display:block;font-size:24px;line-height:1.15}.workspace{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:14px;align-items:start}.graph-panel,.detail-panel{min-height:620px}.graph-panel{overflow:auto}.detail-panel{position:sticky;top:12px;max-height:calc(100vh - 24px);overflow:auto}.panel{padding:13px}.graph-head{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:10px}.legend{display:flex;gap:7px;flex-wrap:wrap}.chip{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--line);border-radius:999px;padding:3px 7px;color:var(--muted);font-size:12px}.dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}#world-model-graph{display:block;min-width:900px;width:100%;height:650px;background:#111317;border:1px solid #252b33;border-radius:8px}.edge{stroke:#4b5563;stroke-width:1.15;opacity:.7}.edge.contradicts{stroke:var(--bad);stroke-dasharray:4 4;opacity:.95}.edge.prediction_market{stroke:var(--accent)}.node circle{stroke:#101113;stroke-width:2}.node text{fill:var(--text);font-size:12px;pointer-events:none}.node:hover circle{stroke:var(--text)}.world{fill:var(--accent)}.narrative{fill:var(--blue)}.scope,.category{fill:#6ee7b7}.belief{fill:var(--warn)}.prediction_market{fill:var(--violet)}.memory{fill:var(--good)}.event{fill:#d1d5db}.source{fill:#fb923c}pre{white-space:pre-wrap;overflow:auto;background:#111317;border:1px solid #252b33;border-radius:7px;padding:10px;margin:0;max-height:420px}.tables{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:14px}.table-wrap{overflow:auto;max-height:390px}table{width:100%;border-collapse:collapse;font-size:13px}td,th{border-bottom:1px solid #272d35;padding:7px 6px;text-align:left;vertical-align:top}th{color:var(--muted);font-weight:600}.pill{display:inline-block;border-radius:999px;background:#252b33;padding:2px 7px}.good{color:var(--good)}.warn{color:var(--warn)}.bad{color:var(--bad)}@media (max-width:980px){header{display:block}.controls{justify-content:flex-start;margin-top:12px}.workspace{grid-template-columns:1fr}.detail-panel{position:static;max-height:none}#world-model-graph{min-width:760px}}
</style></head>
<body><header><div><h1>World Model Dashboard</h1><div class="muted">Market beliefs, prediction signals, memory, and evidence graph.</div></div><div class="controls"><input id="token" type="password" placeholder="Bearer token"/><input id="symbol" placeholder="Symbol"/><input id="topic" placeholder="Topic"/><select id="mode"><option value="tree">Tree</option><option value="timeline">Timeline</option><option value="contradictions">Contradictions</option><option value="prediction_consensus">Prediction consensus</option><option value="source_reliability">Source reliability</option></select><select id="limit"><option>50</option><option selected>100</option><option>250</option><option>500</option></select><button onclick="saveToken()">Save</button><button onclick="load()">Refresh</button></div></header>
<main><section class="metrics" id="summary"></section><section class="workspace"><div class="panel graph-panel"><div class="graph-head"><h2 id="graph-title">Graph Tree</h2><div class="legend"><span class="chip"><span class="dot narrative"></span>Narrative</span><span class="chip"><span class="dot belief"></span>Belief</span><span class="chip"><span class="dot prediction_market"></span>Prediction</span><span class="chip"><span class="dot memory"></span>Memory</span><span class="chip"><span class="dot event"></span>Event</span></div></div><svg id="world-model-graph" role="img" aria-label="World model graph"></svg></div><aside class="panel detail-panel"><h2>Node Detail</h2><div id="detail" class="muted">No node selected.</div><h3>Snapshot Summary</h3><pre id="snapshot"></pre></aside></section><section class="tables"><div class="panel"><h2>Beliefs</h2><div class="table-wrap" id="beliefs"></div></div><div class="panel"><h2>Prediction Markets</h2><div class="table-wrap" id="predictions"></div></div><div class="panel"><h2>Memory</h2><div class="table-wrap" id="memory"></div></div><div class="panel"><h2>Evidence Events</h2><div class="table-wrap" id="events"></div></div></section></main>
<script>
const $=id=>document.getElementById(id);let model={nodes:[],edges:[]};let selected=null;function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]))}function headers(){const t=localStorage.getItem('agentToken')||'';return t?{'Authorization':'Bearer '+t}:{}}function saveToken(){localStorage.setItem('agentToken',$('token').value.trim());load()}function metric(k,v,cls=''){return `<div class="metric"><span class="muted">${esc(k)}</span><b class="${cls}">${esc(v)}</b></div>`}function fmtPct(v){if(v===null||v===undefined||v==='')return '';const n=Number(v);return Number.isFinite(n)?(n*100).toFixed(1)+'%':String(v)}function fmtNum(v){if(v===null||v===undefined||v==='')return '';const n=Number(v);return Number.isFinite(n)?n.toFixed(2):String(v)}function table(items,cols){if(!items||!items.length)return '<div class="muted">No rows.</div>';return '<table><thead><tr>'+cols.map(c=>`<th>${esc(c[0])}</th>`).join('')+'</tr></thead><tbody>'+items.map(r=>'<tr>'+cols.map(c=>`<td>${c[2]?c[2](r):esc(r[c[1]])}</td>`).join('')+'</tr>').join('')+'</tbody></table>'}async function api(path){const r=await fetch(path,{headers:headers()});if(!r.ok)throw new Error(r.status+' '+await r.text());return await r.json()}
function depth(n){return ({world:0,narrative:1,scope:1,category:1,source:1,belief:2,prediction_market:2,memory:2,event:3}[n.type]??2)}function radius(n){return Math.max(7,Math.min(18,8+(Number(n.score)||0)*9))}function colorClass(n){return n.type}
function renderGraph(graph){model=graph||{nodes:[],edges:[]};const svg=$('world-model-graph');const nodes=model.nodes||[],edges=model.edges||[];const byId=Object.fromEntries(nodes.map(n=>[n.id,n]));const groups=[0,1,2,3].map(d=>nodes.filter(n=>depth(n)===d));const maxRows=Math.max(1,...groups.map(g=>g.length));const width=Math.max(900,svg.clientWidth||900);const height=Math.max(650,maxRows*56+80);svg.setAttribute('viewBox',`0 0 ${width} ${height}`);svg.style.height=height+'px';const pos={};groups.forEach((g,d)=>{const x=48+d*((width-110)/3);g.forEach((n,i)=>{pos[n.id]={x:x,y:50+(i+1)*((height-100)/(g.length+1))}})});svg.innerHTML='';edges.forEach(e=>{const a=pos[e.source],b=pos[e.target];if(!a||!b)return;const line=document.createElementNS('http://www.w3.org/2000/svg','path');const mid=(a.x+b.x)/2;line.setAttribute('d',`M${a.x},${a.y} C${mid},${a.y} ${mid},${b.y} ${b.x},${b.y}`);line.setAttribute('class','edge '+esc(e.type));line.setAttribute('fill','none');svg.appendChild(line)});nodes.forEach(n=>{const p=pos[n.id];if(!p)return;const g=document.createElementNS('http://www.w3.org/2000/svg','g');g.setAttribute('class','node');g.setAttribute('tabindex','0');g.onclick=()=>selectNode(n);const c=document.createElementNS('http://www.w3.org/2000/svg','circle');c.setAttribute('cx',p.x);c.setAttribute('cy',p.y);c.setAttribute('r',radius(n));c.setAttribute('class',colorClass(n));g.appendChild(c);const t=document.createElementNS('http://www.w3.org/2000/svg','text');t.setAttribute('x',p.x+radius(n)+7);t.setAttribute('y',p.y+4);t.textContent=n.label.length>52?n.label.slice(0,49)+'...':n.label;g.appendChild(t);svg.appendChild(g)});if(nodes.length&&!selected)selectNode(nodes[0])}
function targetFor(n){const [prefix,...rest]=String(n.id).split(':');const id=rest.join(':');const map={event:'event',belief:'belief',prediction:'prediction_signal',memory:'memory',source:'source',cluster:'narrative'};return map[prefix]?{target_type:map[prefix],target_id:id}:null}
async function annotate(action){if(!selected)return;const t=targetFor(selected);if(!t)return;const note=prompt('Annotation note','')||'';const r=await fetch('/world-model/annotations',{method:'POST',headers:{...headers(),'Content-Type':'application/json'},body:JSON.stringify({...t,action,note,metadata:{source:'dashboard'}})});if(!r.ok)throw new Error(await r.text());await load()}
function selectNode(n){selected=n;const t=targetFor(n);const controls=t?`<p><button onclick="annotate('confirmed')">Confirm</button> <button onclick="annotate('disputed')">Dispute</button> <button onclick="annotate('needs_review')">Review</button> <button onclick="annotate('pinned')">Pin</button></p>`:'';$('detail').innerHTML=`<h3>${esc(n.type)}</h3><p><b>${esc(n.label)}</b></p>${controls}<pre>${esc(JSON.stringify(n.data||{},null,2))}</pre>`}
async function load(){try{$('token').value=localStorage.getItem('agentToken')||'';const qs=new URLSearchParams();if($('symbol').value.trim())qs.set('symbol',$('symbol').value.trim());if($('topic').value.trim())qs.set('topic',$('topic').value.trim());qs.set('mode',$('mode').value);qs.set('limit',$('limit').value);const d=await api('/world-model/dashboard/data?'+qs.toString());const s=d.summary||{},st=d.status||{},snap=d.snapshot||{};const repo=st.repository_enabled?(st.repository_available?'OK':'FALLBACK'):'OFF';$('graph-title').textContent=($('mode').selectedOptions[0]||{}).textContent||'Graph';$('summary').innerHTML=[metric('Beliefs',s.beliefs??0),metric('Events',s.events??0),metric('Predictions',s.prediction_market_signals??0),metric('Annotations',s.annotations??0),metric('Repository',repo,repo==='OK'?'good':repo==='OFF'?'':'warn'),metric('Model errors',st.error_count??0,(st.error_count||0)?'bad':'good')].join('');$('snapshot').textContent=JSON.stringify({summary:snap.summary,quality_flags:snap.quality_flags,filters:d.filters,repository:{available:st.repository_available,last_error:st.repository_last_error,cooldown_until_ms:st.repository_cooldown_until_ms}},null,2);renderGraph(d.graph);$('beliefs').innerHTML=table(d.beliefs.items,[['Direction','direction',(r)=>`<span class="pill">${esc(r.direction)}</span>`],['Subject','subject'],['Belief','statement'],['Conf','confidence',(r)=>fmtPct(r.confidence)]]);$('predictions').innerHTML=table(d.prediction_markets.items,[['Venue','venue'],['Question','question'],['P','implied_probability',(r)=>fmtPct(r.implied_probability)],['Liq','liquidity_usd',(r)=>fmtNum(r.liquidity_usd)]]);$('memory').innerHTML=table(d.memory.items,[['Type','memory_type'],['Subject','subject'],['Content','content'],['Sal','salience',(r)=>fmtPct(r.salience)]]);$('events').innerHTML=table(d.events.items,[['Source','source'],['Type','source_type'],['Title','title'],['Imp','importance_score',(r)=>fmtNum(r.importance_score)]])}catch(e){$('summary').innerHTML=`<div class="metric"><span class="bad">Load failed</span><pre>${esc(e.message)}</pre></div>`}}load();
</script></body></html>
""".strip()


def _dashboard_html_v2() -> str:
    return """
<!doctype html><html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/><title>World Model Dashboard</title>
<style>
:root{color-scheme:dark;--bg:#101113;--panel:#17191d;--panel2:#20242b;--line:#303640;--text:#eef2f5;--muted:#9ba6b2;--accent:#2dd4bf;--good:#76d672;--warn:#f6b73c;--bad:#f36f56;--blue:#7aa8ff;--violet:#c38cff;--orange:#fb923c}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}header{display:grid;grid-template-columns:minmax(220px,1fr) minmax(320px,2fr);gap:16px;align-items:start;padding:18px 20px;border-bottom:1px solid var(--line);background:#131518}h1{margin:0 0 5px;font-size:22px;letter-spacing:0}h2{margin:0 0 10px;font-size:15px}h3{margin:18px 0 8px;font-size:13px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}.muted{color:var(--muted)}.controls,.toolrow{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}input,select,button{height:34px;background:#111317;color:var(--text);border:1px solid var(--line);border-radius:7px;padding:0 10px;font:inherit}input[type=range]{padding:0;min-width:220px}input[type=number]{width:90px}button{cursor:pointer;background:#20252c}button:hover{border-color:var(--accent)}button:disabled{opacity:.45;cursor:not-allowed}.check{display:inline-flex;align-items:center;gap:6px;height:34px;border:1px solid var(--line);border-radius:7px;padding:0 10px;color:var(--muted);background:#111317}.check input{height:auto}.small{font-size:12px}.wide{min-width:220px}main{display:grid;gap:14px;padding:14px 18px 22px}.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px}.metric,.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px}.metric{padding:11px 12px;min-height:70px}.metric b{display:block;font-size:24px;line-height:1.15}.workspace{display:grid;grid-template-columns:minmax(0,1fr) 380px;gap:14px;align-items:start}.graph-panel,.detail-panel{min-height:640px}.graph-panel{overflow:auto}.detail-panel{position:sticky;top:12px;max-height:calc(100vh - 24px);overflow:auto}.panel{padding:13px}.graph-head{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:10px}.legend{display:flex;gap:7px;flex-wrap:wrap}.chip{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--line);border-radius:999px;padding:3px 7px;color:var(--muted);font-size:12px}.dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}#world-model-graph{display:block;min-width:900px;width:100%;height:650px;background:#111317;border:1px solid #252b33;border-radius:8px}.edge{stroke:#4b5563;stroke-width:1.15;opacity:.7}.edge.contradicts{stroke:var(--bad);stroke-dasharray:4 4;opacity:.95}.edge.prediction_market{stroke:var(--accent)}.node circle{stroke:#101113;stroke-width:2}.node text{fill:var(--text);font-size:12px;pointer-events:none}.node:hover circle{stroke:var(--text)}.world{fill:var(--accent)}.narrative{fill:var(--blue)}.scope,.category{fill:#6ee7b7}.belief{fill:var(--warn)}.prediction_market{fill:var(--violet)}.memory{fill:var(--good)}.event{fill:#d1d5db}.source{fill:var(--orange)}pre{white-space:pre-wrap;overflow:auto;background:#111317;border:1px solid #252b33;border-radius:7px;padding:10px;margin:0;max-height:360px}.tables{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:14px}.table-wrap{overflow:auto;max-height:390px}table{width:100%;border-collapse:collapse;font-size:13px}td,th{border-bottom:1px solid #272d35;padding:7px 6px;text-align:left;vertical-align:top}th{color:var(--muted);font-weight:600}.pill{display:inline-block;border-radius:999px;background:#252b33;padding:2px 7px}.good{color:var(--good)}.warn{color:var(--warn)}.bad{color:var(--bad)}.side-list{display:grid;gap:7px}.queue-item{border:1px solid #272d35;border-radius:7px;padding:8px;background:#111317}.timebar{display:grid;gap:9px}.timebar .toolrow{justify-content:flex-start}.statusline{min-height:18px}@media (max-width:1040px){header{display:block}.controls{justify-content:flex-start;margin-top:12px}.workspace{grid-template-columns:1fr}.detail-panel{position:static;max-height:none}#world-model-graph{min-width:760px}}
</style></head>
<body><header><div><h1>World Model Dashboard</h1><div class="muted">Advisory market state, prediction consensus, source memory, and operator supervision.</div></div><div class="controls"><input id="token" type="password" placeholder="Bearer token"/><input id="symbol" placeholder="Symbol"/><input id="topic" placeholder="Topic"/><select id="mode"><option value="tree">Tree</option><option value="timeline">Timeline</option><option value="contradictions">Contradictions</option><option value="prediction_consensus">Prediction consensus</option><option value="source_reliability">Source reliability</option></select><select id="limit"><option>50</option><option selected>100</option><option>250</option><option>500</option></select><input id="search" class="wide" placeholder="Search nodes"/><input id="sourceFilter" placeholder="Source"/><input id="minScore" type="number" min="0" max="1" step="0.05" placeholder="Score"/><label class="check small"><input id="contradictionsOnly" type="checkbox"/>Contradictions</label><button onclick="saveToken()">Save</button><button onclick="load()">Refresh</button></div></header>
<main><section class="panel timebar"><div class="toolrow"><input id="asOf" type="datetime-local"/><input id="timeSlider" type="range" min="0" max="1" step="1"/><button onclick="loadSnapshots(true)">Snapshots</button><button onclick="loadAtTime()">At Time</button><button onclick="useNow()">Now</button><input id="replayStart" type="datetime-local"/><input id="replayEnd" type="datetime-local"/><button onclick="runReplay()">Replay</button><button onclick="pollAdapters()">Poll</button><button onclick="seedDemo()">Seed</button></div><div id="timeStatus" class="muted statusline"></div></section><section class="metrics" id="summary"></section><section class="workspace"><div class="panel graph-panel"><div class="graph-head"><h2 id="graph-title">Graph Tree</h2><div class="legend"><span class="chip"><span class="dot narrative"></span>Narrative</span><span class="chip"><span class="dot belief"></span>Belief</span><span class="chip"><span class="dot prediction_market"></span>Prediction</span><span class="chip"><span class="dot memory"></span>Memory</span><span class="chip"><span class="dot event"></span>Event</span></div></div><svg id="world-model-graph" role="img" aria-label="World model graph"></svg></div><aside class="panel detail-panel"><h2>Node Detail</h2><div id="detail" class="muted">No node selected.</div><h3>Annotation Queue</h3><select id="queueAction" onchange="renderAnnotationQueue(lastData)"><option value="">All annotations</option><option value="needs_review">Needs review</option><option value="disputed">Disputed</option><option value="pinned">Pinned</option><option value="confirmed">Confirmed</option></select><div id="annotationQueue" class="side-list"></div><h3>Replay</h3><pre id="replay"></pre><h3>Adapters</h3><pre id="adapters"></pre><h3>Snapshot Summary</h3><pre id="snapshot"></pre></aside></section><section class="tables"><div class="panel"><h2>Beliefs</h2><div class="table-wrap" id="beliefs"></div></div><div class="panel"><h2>Prediction Markets</h2><div class="table-wrap" id="predictions"></div></div><div class="panel"><h2>Calibration</h2><div class="table-wrap" id="calibration"></div></div><div class="panel"><h2>Outcomes</h2><div class="table-wrap" id="outcomes"></div></div><div class="panel"><h2>Memory</h2><div class="table-wrap" id="memory"></div></div><div class="panel"><h2>Evidence Events</h2><div class="table-wrap" id="events"></div></div></section></main>
<script>
const $=id=>document.getElementById(id);let rawGraph={nodes:[],edges:[]};let model={nodes:[],edges:[]};let selected=null;let asOfMs=null;let snapshots=[];let lastData=null;
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]))}
function headers(json=false){const t=localStorage.getItem('agentToken')||'';const h=t?{'Authorization':'Bearer '+t}:{};if(json)h['Content-Type']='application/json';return h}
function saveToken(){localStorage.setItem('agentToken',$('token').value.trim());load()}
function metric(k,v,cls=''){return `<div class="metric"><span class="muted">${esc(k)}</span><b class="${cls}">${esc(v)}</b></div>`}
function fmtPct(v){if(v===null||v===undefined||v==='')return '';const n=Number(v);return Number.isFinite(n)?(n*100).toFixed(1)+'%':String(v)}
function fmtNum(v){if(v===null||v===undefined||v==='')return '';const n=Number(v);return Number.isFinite(n)?n.toFixed(2):String(v)}
function fmtTime(ms){const n=Number(ms);return Number.isFinite(n)&&n>0?new Date(n).toLocaleString():''}
function table(items,cols){if(!items||!items.length)return '<div class="muted">No rows.</div>';return '<table><thead><tr>'+cols.map(c=>`<th>${esc(c[0])}</th>`).join('')+'</tr></thead><tbody>'+items.map(r=>'<tr>'+cols.map(c=>`<td>${c[2]?c[2](r):esc(r[c[1]])}</td>`).join('')+'</tr>').join('')+'</tbody></table>'}
async function api(path){const r=await fetch(path,{headers:headers()});if(!r.ok)throw new Error(r.status+' '+await r.text());return await r.json()}
async function post(path,body=null){const r=await fetch(path,{method:'POST',headers:headers(true),body:body?JSON.stringify(body):null});if(!r.ok)throw new Error(r.status+' '+await r.text());return await r.json()}
function depth(n){return ({world:0,narrative:1,scope:1,category:1,source:1,belief:2,prediction_market:2,memory:2,event:3}[n.type]??2)}
function radius(n){return Math.max(7,Math.min(18,8+(Number(n.score)||0)*9))}
function colorClass(n){return n.type}
function activeGraphFilters(){return {q:$('search').value.trim().toLowerCase(),source:$('sourceFilter').value.trim().toLowerCase(),min:Number($('minScore').value),contradictions:$('contradictionsOnly').checked}}
function filteredGraph(graph){const f=activeGraphFilters();const min=Number.isFinite(f.min)?f.min:null;const contradictionIds=new Set();(graph.edges||[]).filter(e=>e.type==='contradicts').forEach(e=>{contradictionIds.add(e.source);contradictionIds.add(e.target)});const filtered=(graph.nodes||[]).filter(n=>{if(n.id==='world')return true;if(f.contradictions&&!contradictionIds.has(n.id))return false;if(min!==null&&min>0&&Number(n.score||0)<min)return false;const data=n.data||{};const hay=(n.label+' '+n.type+' '+JSON.stringify(data)).toLowerCase();if(f.q&&!hay.includes(f.q))return false;if(f.source){const src=String(data.source||data.provider||data.venue||'').toLowerCase();if(!src.includes(f.source)&&!hay.includes(f.source))return false}return true});const ids=new Set(filtered.map(n=>n.id));return {nodes:filtered,edges:(graph.edges||[]).filter(e=>ids.has(e.source)&&ids.has(e.target))}}
function renderGraph(graph){rawGraph=graph||{nodes:[],edges:[]};model=filteredGraph(rawGraph);const svg=$('world-model-graph');const nodes=model.nodes||[],edges=model.edges||[];const groups=[0,1,2,3].map(d=>nodes.filter(n=>depth(n)===d));const maxRows=Math.max(1,...groups.map(g=>g.length));const width=Math.max(900,svg.clientWidth||900);const height=Math.max(650,maxRows*56+80);svg.setAttribute('viewBox',`0 0 ${width} ${height}`);svg.style.height=height+'px';const pos={};groups.forEach((g,d)=>{const x=48+d*((width-110)/3);g.forEach((n,i)=>{pos[n.id]={x:x,y:50+(i+1)*((height-100)/(g.length+1))}})});svg.innerHTML='';edges.forEach(e=>{const a=pos[e.source],b=pos[e.target];if(!a||!b)return;const line=document.createElementNS('http://www.w3.org/2000/svg','path');const mid=(a.x+b.x)/2;line.setAttribute('d',`M${a.x},${a.y} C${mid},${a.y} ${mid},${b.y} ${b.x},${b.y}`);line.setAttribute('class','edge '+esc(e.type));line.setAttribute('fill','none');svg.appendChild(line)});nodes.forEach(n=>{const p=pos[n.id];if(!p)return;const g=document.createElementNS('http://www.w3.org/2000/svg','g');g.setAttribute('class','node');g.setAttribute('tabindex','0');g.onclick=()=>selectNode(n);const c=document.createElementNS('http://www.w3.org/2000/svg','circle');c.setAttribute('cx',p.x);c.setAttribute('cy',p.y);c.setAttribute('r',radius(n));c.setAttribute('class',colorClass(n));g.appendChild(c);const t=document.createElementNS('http://www.w3.org/2000/svg','text');t.setAttribute('x',p.x+radius(n)+7);t.setAttribute('y',p.y+4);t.textContent=n.label.length>52?n.label.slice(0,49)+'...':n.label;g.appendChild(t);svg.appendChild(g)});if(nodes.length&&(!selected||!nodes.some(n=>n.id===selected.id)))selectNode(nodes[0])}
function targetFor(n){const [prefix,...rest]=String(n.id).split(':');const id=rest.join(':');const map={event:'event',belief:'belief',prediction:'prediction_signal',memory:'memory',source:'source',cluster:'narrative'};return map[prefix]?{target_type:map[prefix],target_id:id}:null}
function symbolForNode(n){const symbols=(n.data||{}).symbols||[];return symbols[0]||$('symbol').value.trim()||null}
async function annotate(action){if(!selected)return;const t=targetFor(selected);if(!t)return;const note=prompt('Annotation note','')||'';await post('/world-model/annotations',{...t,action,note,metadata:{source:'dashboard'}});await load()}
async function recordOutcome(outcome){if(!selected)return;const t=targetFor(selected);if(!t)return;await post('/world-model/outcomes',{...t,outcome,symbol:symbolForNode(selected),realized_value:(outcome==='yes'||outcome==='worked')?1.0:(outcome==='no'||outcome==='failed')?0.0:null,metadata:{source:'dashboard'}});await load()}
function selectNode(n){selected=n;const t=targetFor(n);let controls='';if(t){controls+=`<p><button onclick="annotate('confirmed')">Confirm</button> <button onclick="annotate('disputed')">Dispute</button> <button onclick="annotate('needs_review')">Review</button> <button onclick="annotate('pinned')">Pin</button></p>`}if(t&&t.target_type==='prediction_signal'){controls+=`<p><button onclick="recordOutcome('yes')">Mark Yes</button> <button onclick="recordOutcome('no')">Mark No</button></p>`}if(t&&t.target_type==='event'){controls+=`<p><button onclick="recordOutcome('worked')">Worked</button> <button onclick="recordOutcome('failed')">Failed</button></p>`}$('detail').innerHTML=`<h3>${esc(n.type)}</h3><p><b>${esc(n.label)}</b></p>${controls}<pre>${esc(JSON.stringify(n.data||{},null,2))}</pre>`}
function renderAnnotationQueue(d){if(!d){$('annotationQueue').innerHTML='<div class="muted">No rows.</div>';return}const action=$('queueAction').value;let items=((d.annotations||{}).items||[]);if(action)items=items.filter(x=>x.action===action);$('annotationQueue').innerHTML=items.slice(0,20).map(x=>`<div class="queue-item"><span class="pill">${esc(x.action)}</span> <b>${esc(x.target_type)}:${esc(x.target_id)}</b><div class="muted small">${esc(fmtTime(x.created_at_ms))}</div><div>${esc(x.note||'')}</div></div>`).join('')||'<div class="muted">No rows.</div>'}
function toLocalInput(ms){if(!ms)return '';const d=new Date(Number(ms));d.setMinutes(d.getMinutes()-d.getTimezoneOffset());return d.toISOString().slice(0,16)}
function fromLocalInput(v){const n=Date.parse(v);return Number.isFinite(n)?n:null}
async function loadSnapshots(showStatus=false){const qs=new URLSearchParams();if($('symbol').value.trim())qs.set('symbol',$('symbol').value.trim());if($('topic').value.trim())qs.set('topic',$('topic').value.trim());qs.set('limit','100');const d=await api('/world-model/snapshots?'+qs.toString());snapshots=(d.items||[]).sort((a,b)=>Number(a.as_of_ms||0)-Number(b.as_of_ms||0));if(snapshots.length){const min=Number(snapshots[0].as_of_ms),max=Number(snapshots[snapshots.length-1].as_of_ms);$('timeSlider').min=min;$('timeSlider').max=max;$('timeSlider').value=asOfMs||max;if(!$('replayStart').value)$('replayStart').value=toLocalInput(min);if(!$('replayEnd').value)$('replayEnd').value=toLocalInput(max);if(showStatus)$('timeStatus').textContent=`${snapshots.length} snapshots from ${fmtTime(min)} to ${fmtTime(max)}`}else if(showStatus){$('timeStatus').textContent='No snapshots found.'}}
function loadAtTime(){asOfMs=fromLocalInput($('asOf').value);if(asOfMs)$('timeSlider').value=asOfMs;load()}
function useNow(){asOfMs=null;$('asOf').value='';load()}
async function runReplay(){const start=fromLocalInput($('replayStart').value)||(snapshots[0]&&Number(snapshots[0].as_of_ms))||0;const end=fromLocalInput($('replayEnd').value)||asOfMs||Date.now();const qs=new URLSearchParams({start_ms:String(start),end_ms:String(end),limit:'200'});if($('symbol').value.trim())qs.set('symbol',$('symbol').value.trim());if($('topic').value.trim())qs.set('topic',$('topic').value.trim());const d=await api('/world-model/replay?'+qs.toString());$('replay').textContent=JSON.stringify({window:[fmtTime(start),fmtTime(end)],snapshots:(d.snapshots||[]).length,events:(d.events||[]).length,annotations:(d.annotations||[]).length,outcomes:(d.outcomes||[]).length,latest_event:(d.events||[]).slice(-1)[0]||null},null,2)}
async function loadAdapters(){try{const d=await api('/world-model/adapters/status');$('adapters').textContent=JSON.stringify(d,null,2)}catch(e){$('adapters').textContent=e.message}}
async function pollAdapters(){try{$('timeStatus').textContent='Polling adapters...';const d=await post('/world-model/adapters/poll?force=true');$('timeStatus').textContent='Adapter poll complete.';$('adapters').textContent=JSON.stringify(d,null,2);await load()}catch(e){$('timeStatus').textContent=e.message}}
async function seedDemo(){try{const body={symbol:$('symbol').value.trim()||'BTC',topic:$('topic').value.trim()||'macro'};const d=await post('/world-model/dev/seed',body);$('timeStatus').textContent=`Seeded ${d.symbol} / ${d.topic}`;await loadSnapshots(true);await load()}catch(e){$('timeStatus').textContent=e.message}}
async function load(){try{$('token').value=localStorage.getItem('agentToken')||'';const qs=new URLSearchParams();if($('symbol').value.trim())qs.set('symbol',$('symbol').value.trim());if($('topic').value.trim())qs.set('topic',$('topic').value.trim());qs.set('mode',$('mode').value);qs.set('limit',$('limit').value);if(asOfMs)qs.set('as_of_ms',String(asOfMs));const d=await api('/world-model/dashboard/data?'+qs.toString());lastData=d;const s=d.summary||{},st=d.status||{},snap=d.snapshot||{};const repo=st.repository_enabled?(st.repository_available?'OK':'FALLBACK'):'OFF';$('graph-title').textContent=($('mode').selectedOptions[0]||{}).textContent||'Graph';$('summary').innerHTML=[metric('Beliefs',s.beliefs??0),metric('Events',s.events??0),metric('Predictions',s.prediction_market_signals??0),metric('Annotations',s.annotations??0),metric('Repository',repo,repo==='OK'?'good':repo==='OFF'?'':'warn'),metric('Model errors',st.error_count??0,(st.error_count||0)?'bad':'good')].join('');$('snapshot').textContent=JSON.stringify({summary:snap.summary,as_of_ms:snap.as_of_ms,as_of:fmtTime(snap.as_of_ms),quality_flags:snap.quality_flags,filters:d.filters,repository:{available:st.repository_available,last_error:st.repository_last_error,cooldown_until_ms:st.repository_cooldown_until_ms}},null,2);if(asOfMs)$('asOf').value=toLocalInput(asOfMs);renderGraph(d.graph);renderAnnotationQueue(d);$('beliefs').innerHTML=table(d.beliefs.items,[['Direction','direction',r=>`<span class="pill">${esc(r.direction)}</span>`],['Subject','subject'],['Belief','statement'],['Conf','confidence',r=>fmtPct(r.confidence)]]);$('predictions').innerHTML=table(d.prediction_markets.items,[['Venue','venue'],['Question','question'],['P','implied_probability',r=>fmtPct(r.implied_probability)],['Delta','probability_delta',r=>fmtPct(r.probability_delta)],['Liq','liquidity_usd',r=>fmtNum(r.liquidity_usd)]]);$('calibration').innerHTML=table((d.prediction_calibration||{}).items,[['Venue','venue'],['Market','market_id'],['P','implied_probability',r=>fmtPct(r.implied_probability)],['Realized','realized_outcome',r=>fmtPct(r.realized_outcome)],['Brier','brier_score',r=>fmtNum(r.brier_score)]]);$('outcomes').innerHTML=table((d.outcomes||{}).items,[['Target','target_id'],['Type','target_type'],['Outcome','outcome'],['At','created_at_ms',r=>fmtTime(r.created_at_ms)]]);$('memory').innerHTML=table(d.memory.items,[['Type','memory_type'],['Subject','subject'],['Content','content'],['Sal','salience',r=>fmtPct(r.salience)]]);$('events').innerHTML=table(d.events.items,[['Source','source'],['Type','source_type'],['Title','title'],['Imp','importance_score',r=>fmtNum(r.importance_score)]]);await loadAdapters();if(!snapshots.length)await loadSnapshots(false)}catch(e){$('summary').innerHTML=`<div class="metric"><span class="bad">Load failed</span><pre>${esc(e.message)}</pre></div>`}}
['search','sourceFilter','minScore','contradictionsOnly'].forEach(id=>$(id).addEventListener('input',()=>renderGraph(rawGraph)));$('timeSlider').addEventListener('change',()=>{asOfMs=Number($('timeSlider').value);$('asOf').value=toLocalInput(asOfMs);load()});load();
</script></body></html>
""".strip()
