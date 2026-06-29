from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

from hyperliquid_trading_agent.app.autonomy.schemas import AlphaEventEvaluation, NewsEvent, SignalEvaluation
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.newswire.schemas import NewswireEvent
from hyperliquid_trading_agent.app.world_model.reducer import WorldModelReducer, now_ms
from hyperliquid_trading_agent.app.world_model.schemas import (
    BeliefDirection,
    MarketBelief,
    PredictionMarketCalibration,
    PredictionMarketSignal,
    WorldEvent,
    WorldModelAnnotation,
    WorldModelOutcome,
    WorldModelSnapshot,
)

log = get_logger(__name__)


class WorldModelService:
    """Repository-backed facade for the agent's market world model."""

    def __init__(self, *, settings: Settings, repository: Any | None = None):
        self.settings = settings
        self.repository = repository
        self.reducer = WorldModelReducer()
        self.last_error: str | None = None
        self.error_count = 0
        self.repository_last_error: str | None = None
        self.repository_error_count = 0
        self.repository_unavailable_until_ms: int | None = None
        self.repository_cooldown_ms = 30_000
        self.snapshot_coalesce_ms = 30_000
        self._last_snapshot_persist_at_ms: dict[str, int] = {}
        self.annotations: dict[str, WorldModelAnnotation] = {}
        self.outcomes: dict[str, WorldModelOutcome] = {}
        self.calibrations: dict[str, PredictionMarketCalibration] = {}

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "repository_enabled": self._repo_enabled(),
            "repository_available": self._repo_available(),
            "repository_last_error": self.repository_last_error,
            "repository_error_count": self.repository_error_count,
            "repository_cooldown_until_ms": self.repository_unavailable_until_ms if self._repo_in_cooldown() else None,
            "last_error": self.last_error,
            "error_count": self.error_count,
            **self.reducer.status(),
        }

    async def observe_newswire_event(self, event: NewswireEvent) -> list[MarketBelief]:
        world_event = _world_event_from_newswire(event)
        return await self.observe_event(world_event)

    async def observe_news_event(self, event: NewsEvent) -> list[MarketBelief]:
        world_event = _world_event_from_legacy_news(event)
        return await self.observe_event(world_event)

    async def observe_event(self, event: WorldEvent) -> list[MarketBelief]:
        try:
            beliefs = self.reducer.observe_event(event)
            await self._persist_event(event)
            await self._persist_state(beliefs=beliefs)
            await self.persist_snapshot(self.snapshot(symbols=event.symbols or None, topics=event.topics or None))
            return beliefs
        except Exception as exc:  # pragma: no cover - world model must not break ingest
            self._record_error(exc)
            return []

    async def observe_prediction_market_signal(self, signal: PredictionMarketSignal) -> MarketBelief | None:
        try:
            belief = self.reducer.observe_prediction_market_signal(signal)
            await self._persist_prediction_signal(signal)
            event = next((item for item in self.reducer.events.values() if item.payload.get("signal_id") == signal.signal_id), None)
            if event is not None:
                await self._persist_event(event)
            await self._persist_state(beliefs=[belief])
            await self.persist_snapshot(self.snapshot(symbols=signal.symbols or None, topics=signal.topics or None))
            return belief
        except Exception as exc:  # pragma: no cover
            self._record_error(exc)
            return None

    async def observe_hip4_book(
        self,
        book: Any,
        *,
        question: Any | None = None,
        outcome: Any | None = None,
        now: int | None = None,
    ) -> PredictionMarketSignal | None:
        signal = prediction_signal_from_hip4_book(book, question=question, outcome=outcome, settings=self.settings, now=now)
        if signal is None:
            return None
        await self.observe_prediction_market_signal(signal)
        return signal

    async def observe_hip4_candidate(self, candidate: Any) -> list[MarketBelief]:
        data = _model_dump(candidate)
        ts = int(data.get("as_of_ms") or now_ms())
        event = WorldEvent(
            event_id=f"wevt_hip4_candidate_{data.get('candidate_id')}",
            source_type="prediction_market",
            source="hip4",
            provider="hyperliquid",
            event_type="prediction_market_edge",
            asset_class="prediction_market",
            symbols=_symbols_from_text(str(data.get("candidate_id") or ""), self.settings),
            topics=["hip4", "prediction_market", str(data.get("strategy_type") or "edge")],
            title=f"HIP-4 {data.get('strategy_type') or 'candidate'} edge",
            body=f"Expected edge {data.get('expected_net_edge_usd')} ({data.get('expected_net_edge_bps')} bps)",
            event_ts_ms=ts,
            received_ts_ms=ts,
            computed_ts_ms=max(now_ms(), ts),
            importance_score=_edge_importance(data),
            sentiment="neutral",
            confidence=0.65,
            source_score=0.75,
            quality_score=0.75,
            payload=data,
            metadata={"paper_only": True, "execution_authority": "none", "source": "hip4_candidate"},
        )
        return await self.observe_event(event)

    async def observe_signal_evaluation(self, evaluation: SignalEvaluation) -> list[MarketBelief]:
        event = _world_event_from_signal_evaluation(evaluation)
        beliefs = await self.observe_event(event)
        for source_event_id in evaluation.metadata.get("source_event_ids", []) if isinstance(evaluation.metadata, dict) else []:
            self.reducer.observe_outcome_evaluation(source_event_id=str(source_event_id), terminal_outcome=evaluation.terminal_outcome)
        await self._persist_state(beliefs=[])
        return beliefs

    async def observe_alpha_event_evaluation(self, evaluation: AlphaEventEvaluation) -> list[MarketBelief]:
        event = _world_event_from_alpha_event_evaluation(evaluation)
        beliefs = await self.observe_event(event)
        self.reducer.observe_outcome_evaluation(source_event_id=evaluation.event_id, terminal_outcome=evaluation.terminal_outcome)
        await self._persist_state(beliefs=[])
        return beliefs

    def snapshot(
        self,
        *,
        symbols: list[str] | None = None,
        topics: list[str] | None = None,
        max_beliefs: int = 20,
        as_of_ms: int | None = None,
    ) -> WorldModelSnapshot:
        return self.reducer.snapshot(symbols=symbols, topics=topics, max_beliefs=max_beliefs, as_of_ms=as_of_ms)

    async def persist_snapshot(self, snapshot: WorldModelSnapshot | None = None, *, force: bool = False) -> None:
        if not self._repo_available():
            return
        snapshot = snapshot or self.snapshot()
        if not force and not self._should_persist_snapshot(snapshot):
            return
        record = getattr(self.repository, "upsert_world_model_snapshot", None)
        if callable(record):
            try:
                await record(snapshot.model_dump(mode="json"))
                self._last_snapshot_persist_at_ms[self._snapshot_scope_key(snapshot)] = now_ms()
            except Exception as exc:  # pragma: no cover - read-side dashboard must keep working
                self._record_repository_error(exc, operation="upsert_world_model_snapshot")

    def wiki_block(self, *, symbols: list[str] | None = None, topics: list[str] | None = None, max_chars: int = 2_000) -> str:
        return self.reducer.wiki_block(symbols=symbols, topics=topics, max_chars=max_chars)

    async def list_events(self, *, limit: int = 100, source_type: str | None = None, symbol: str | None = None) -> list[dict[str, Any]]:
        if self._repo_available() and callable(getattr(self.repository, "list_world_events", None)):
            try:
                return await self.repository.list_world_events(limit=limit, source_type=source_type, symbol=symbol)
            except Exception as exc:  # pragma: no cover - fallback keeps operator UI available
                self._record_repository_error(exc, operation="list_world_events")
        events = list(self.reducer.events.values())
        if source_type:
            events = [item for item in events if item.source_type == source_type]
        if symbol:
            events = [item for item in events if symbol.upper() in item.symbols]
        return [item.model_dump(mode="json") for item in sorted(events, key=lambda item: item.computed_ts_ms, reverse=True)[:limit]]

    async def list_beliefs(self, *, limit: int = 100, symbol: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        if self._repo_available() and callable(getattr(self.repository, "list_market_beliefs", None)):
            try:
                return await self.repository.list_market_beliefs(limit=limit, symbol=symbol, kind=kind)
            except Exception as exc:  # pragma: no cover
                self._record_repository_error(exc, operation="list_market_beliefs")
        beliefs = list(self.reducer.beliefs.values())
        if symbol:
            beliefs = [item for item in beliefs if symbol.upper() in item.symbols]
        if kind:
            beliefs = [item for item in beliefs if item.kind == kind]
        return [item.model_dump(mode="json") for item in sorted(beliefs, key=lambda item: item.updated_at_ms, reverse=True)[:limit]]

    async def list_prediction_signals(self, *, limit: int = 100, venue: str | None = None, symbol: str | None = None) -> list[dict[str, Any]]:
        if self._repo_available() and callable(getattr(self.repository, "list_prediction_market_signals", None)):
            try:
                return await self.repository.list_prediction_market_signals(limit=limit, venue=venue, symbol=symbol)
            except Exception as exc:  # pragma: no cover
                self._record_repository_error(exc, operation="list_prediction_market_signals")
        signals = list(self.reducer.prediction_signals.values())
        if venue:
            signals = [item for item in signals if item.venue == venue]
        if symbol:
            signals = [item for item in signals if symbol.upper() in item.symbols]
        return [item.model_dump(mode="json") for item in sorted(signals, key=lambda item: item.as_of_ms, reverse=True)[:limit]]

    async def list_memory(self, *, limit: int = 100, symbol: str | None = None, memory_type: str | None = None) -> list[dict[str, Any]]:
        if self._repo_available() and callable(getattr(self.repository, "list_world_memory_atoms", None)):
            try:
                return await self.repository.list_world_memory_atoms(limit=limit, symbol=symbol, memory_type=memory_type)
            except Exception as exc:  # pragma: no cover
                self._record_repository_error(exc, operation="list_world_memory_atoms")
        memories = list(self.reducer.memories.values())
        if symbol:
            memories = [item for item in memories if symbol.upper() in item.symbols]
        if memory_type:
            memories = [item for item in memories if item.memory_type == memory_type]
        return [item.model_dump(mode="json") for item in sorted(memories, key=lambda item: item.last_reinforced_at_ms or item.created_at_ms, reverse=True)[:limit]]

    async def annotate(
        self,
        *,
        target_type: str,
        target_id: str,
        action: str,
        note: str = "",
        actor_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        now: int | None = None,
    ) -> WorldModelAnnotation:
        ts = now or now_ms()
        annotation = WorldModelAnnotation(
            annotation_id=f"wma_{uuid4().hex}",
            target_type=target_type,  # type: ignore[arg-type]
            target_id=target_id,
            action=action,  # type: ignore[arg-type]
            note=note,
            actor_id=actor_id,
            created_at_ms=ts,
            metadata={"paper_only": True, "execution_authority": "none", **(metadata or {})},
        )
        self.annotations[annotation.annotation_id] = annotation
        record = getattr(self.repository, "upsert_world_model_annotation", None)
        if self._repo_available() and callable(record):
            try:
                await record(annotation.model_dump(mode="json"))
            except Exception as exc:  # pragma: no cover
                self._record_repository_error(exc, operation="upsert_world_model_annotation")
        await self.persist_snapshot(self.snapshot(), force=True)
        return annotation

    async def list_annotations(
        self,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        action: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        record = getattr(self.repository, "list_world_model_annotations", None)
        if self._repo_available() and callable(record):
            try:
                return await record(target_type=target_type, target_id=target_id, action=action, limit=limit)
            except Exception as exc:  # pragma: no cover
                self._record_repository_error(exc, operation="list_world_model_annotations")
        items = list(self.annotations.values())
        if target_type:
            items = [item for item in items if item.target_type == target_type]
        if target_id:
            items = [item for item in items if item.target_id == target_id]
        if action:
            items = [item for item in items if item.action == action]
        return [item.model_dump(mode="json") for item in sorted(items, key=lambda item: item.created_at_ms, reverse=True)[:limit]]

    async def record_outcome(
        self,
        *,
        target_type: str,
        target_id: str,
        outcome: str,
        symbol: str | None = None,
        horizon: str | None = None,
        realized_value: float | None = None,
        confidence_delta: float = 0.05,
        metadata: dict[str, Any] | None = None,
        now: int | None = None,
    ) -> WorldModelOutcome:
        ts = now or now_ms()
        item = WorldModelOutcome(
            outcome_id=f"wmo_{uuid4().hex}",
            target_type=target_type,  # type: ignore[arg-type]
            target_id=target_id,
            outcome=outcome,
            symbol=symbol.upper() if symbol else None,
            horizon=horizon,
            realized_value=realized_value,
            confidence_delta=confidence_delta,
            created_at_ms=ts,
            metadata={"paper_only": True, "execution_authority": "none", **(metadata or {})},
        )
        self.outcomes[item.outcome_id] = item
        if target_type == "event":
            self.reducer.observe_outcome_evaluation(source_event_id=target_id, terminal_outcome=outcome, confidence_delta=confidence_delta, now=ts)
            await self._persist_state(beliefs=[])
        if target_type == "prediction_signal":
            calibration = await self._calibrate_prediction_signal(item)
            if calibration is not None:
                self.calibrations[calibration.calibration_id] = calibration
        record = getattr(self.repository, "upsert_world_model_outcome", None)
        if self._repo_available() and callable(record):
            try:
                await record(item.model_dump(mode="json"))
            except Exception as exc:  # pragma: no cover
                self._record_repository_error(exc, operation="upsert_world_model_outcome")
        await self.persist_snapshot(self.snapshot(symbols=[item.symbol] if item.symbol else None), force=True)
        return item

    async def list_outcomes(self, *, target_type: str | None = None, target_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        record = getattr(self.repository, "list_world_model_outcomes", None)
        if self._repo_available() and callable(record):
            try:
                return await record(target_type=target_type, target_id=target_id, limit=limit)
            except Exception as exc:  # pragma: no cover
                self._record_repository_error(exc, operation="list_world_model_outcomes")
        items = list(self.outcomes.values())
        if target_type:
            items = [item for item in items if item.target_type == target_type]
        if target_id:
            items = [item for item in items if item.target_id == target_id]
        return [item.model_dump(mode="json") for item in sorted(items, key=lambda item: item.created_at_ms, reverse=True)[:limit]]

    async def list_calibrations(self, *, signal_id: str | None = None, venue: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        record = getattr(self.repository, "list_prediction_market_calibrations", None)
        if self._repo_available() and callable(record):
            try:
                return await record(signal_id=signal_id, venue=venue, limit=limit)
            except Exception as exc:  # pragma: no cover
                self._record_repository_error(exc, operation="list_prediction_market_calibrations")
        items = list(self.calibrations.values())
        if signal_id:
            items = [item for item in items if item.signal_id == signal_id]
        if venue:
            items = [item for item in items if item.venue == venue]
        return [item.model_dump(mode="json") for item in sorted(items, key=lambda item: item.created_at_ms, reverse=True)[:limit]]

    async def list_snapshots(
        self,
        *,
        limit: int = 100,
        symbol: str | None = None,
        topic: str | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        record = getattr(self.repository, "list_world_model_snapshots", None)
        if self._repo_available() and callable(record):
            try:
                return await record(limit=limit, symbol=symbol, topic=topic, start_ms=start_ms, end_ms=end_ms)
            except Exception as exc:  # pragma: no cover
                self._record_repository_error(exc, operation="list_world_model_snapshots")
        current = self.snapshot(symbols=[symbol.upper()] if symbol else None, topics=[topic.lower()] if topic else None)
        return [current.model_dump(mode="json")]

    async def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        record = getattr(self.repository, "get_world_model_snapshot", None)
        if self._repo_available() and callable(record):
            try:
                return await record(snapshot_id)
            except Exception as exc:  # pragma: no cover
                self._record_repository_error(exc, operation="get_world_model_snapshot")
        current = self.snapshot().model_dump(mode="json")
        return current if current.get("snapshot_id") == snapshot_id else None

    async def nearest_snapshot(self, *, as_of_ms: int, symbol: str | None = None, topic: str | None = None) -> dict[str, Any] | None:
        record = getattr(self.repository, "nearest_world_model_snapshot", None)
        if self._repo_available() and callable(record):
            try:
                return await record(as_of_ms, symbol=symbol, topic=topic)
            except Exception as exc:  # pragma: no cover
                self._record_repository_error(exc, operation="nearest_world_model_snapshot")
        return self.snapshot(symbols=[symbol.upper()] if symbol else None, topics=[topic.lower()] if topic else None, as_of_ms=as_of_ms).model_dump(mode="json")

    async def replay(
        self,
        *,
        start_ms: int,
        end_ms: int,
        symbol: str | None = None,
        topic: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        snapshots = await self.list_snapshots(limit=limit, symbol=symbol, topic=topic, start_ms=start_ms, end_ms=end_ms)
        events = await self.list_events(limit=limit, symbol=symbol)
        if topic:
            events = [item for item in events if topic.lower() in {str(value).lower() for value in item.get("topics") or []}]
        events = [item for item in events if start_ms <= int(item.get("computed_ts_ms") or item.get("received_ts_ms") or 0) <= end_ms]
        annotations = await self.list_annotations(limit=limit)
        annotations = [item for item in annotations if start_ms <= int(item.get("created_at_ms") or 0) <= end_ms]
        outcomes = await self.list_outcomes(limit=limit)
        outcomes = [item for item in outcomes if start_ms <= int(item.get("created_at_ms") or 0) <= end_ms]
        return {
            "start_ms": start_ms,
            "end_ms": end_ms,
            "symbol": symbol.upper() if symbol else None,
            "topic": topic.lower() if topic else None,
            "snapshots": sorted(snapshots, key=lambda item: int(item.get("as_of_ms") or 0)),
            "events": sorted(events, key=lambda item: int(item.get("computed_ts_ms") or 0)),
            "annotations": sorted(annotations, key=lambda item: int(item.get("created_at_ms") or 0)),
            "outcomes": sorted(outcomes, key=lambda item: int(item.get("created_at_ms") or 0)),
        }

    async def repository_health(self) -> dict[str, Any]:
        ping = {"ok": False, "error": "repository_disabled"}
        record = getattr(self.repository, "ping", None)
        if callable(record):
            ping = await record()
        repository_available = self._repo_available() and bool(ping.get("ok"))
        return {**self.status(), "enabled": self._repo_enabled(), "available": repository_available, "repository_available": repository_available, "ping": ping}

    async def _persist_event(self, event: WorldEvent) -> None:
        if not self._repo_available() or not callable(getattr(self.repository, "upsert_world_event", None)):
            return
        try:
            await self.repository.upsert_world_event(event.model_dump(mode="json"))
        except Exception as exc:  # pragma: no cover - persistence must not break the reducer
            self._record_repository_error(exc, operation="upsert_world_event")

    async def _persist_prediction_signal(self, signal: PredictionMarketSignal) -> None:
        if not self._repo_available() or not callable(getattr(self.repository, "upsert_prediction_market_signal", None)):
            return
        try:
            await self.repository.upsert_prediction_market_signal(signal.model_dump(mode="json"))
        except Exception as exc:  # pragma: no cover
            self._record_repository_error(exc, operation="upsert_prediction_market_signal")

    async def _persist_state(self, *, beliefs: list[MarketBelief]) -> None:
        if not self._repo_available():
            return
        try:
            for belief in beliefs:
                record_belief = getattr(self.repository, "upsert_market_belief", None)
                if callable(record_belief):
                    await record_belief(belief.model_dump(mode="json"))
            record_cluster = getattr(self.repository, "upsert_narrative_cluster", None)
            if callable(record_cluster):
                for cluster in self.reducer.narratives.values():
                    await record_cluster(cluster.model_dump(mode="json"))
            record_source = getattr(self.repository, "upsert_source_credibility", None)
            if callable(record_source):
                for source in self.reducer.source_credibility.values():
                    await record_source(source.model_dump(mode="json"))
            record_memory = getattr(self.repository, "upsert_world_memory_atom", None)
            if callable(record_memory):
                for memory in self.reducer.memories.values():
                    await record_memory(memory.model_dump(mode="json"))
        except Exception as exc:  # pragma: no cover
            self._record_repository_error(exc, operation="persist_world_model_state")

    def _repo_enabled(self) -> bool:
        return self.repository is not None and getattr(self.repository, "enabled", False)

    def _repo_available(self) -> bool:
        return self._repo_enabled() and not self._repo_in_cooldown()

    def _repo_in_cooldown(self) -> bool:
        return self.repository_unavailable_until_ms is not None and now_ms() < self.repository_unavailable_until_ms

    def _snapshot_scope_key(self, snapshot: WorldModelSnapshot) -> str:
        symbols = ",".join(snapshot.symbols)
        topics = ",".join(snapshot.topics)
        return f"{symbols}|{topics}"

    def _should_persist_snapshot(self, snapshot: WorldModelSnapshot) -> bool:
        key = self._snapshot_scope_key(snapshot)
        previous = self._last_snapshot_persist_at_ms.get(key)
        return previous is None or now_ms() - previous >= self.snapshot_coalesce_ms

    def _record_error(self, exc: Exception) -> None:
        self.error_count += 1
        self.last_error = type(exc).__name__
        log.warning("world_model_error", error=type(exc).__name__)

    def _record_repository_error(self, exc: Exception, *, operation: str) -> None:
        ts = now_ms()
        if not self._repo_in_cooldown():
            self.repository_error_count += 1
            log.warning("world_model_repository_unavailable", operation=operation, error=type(exc).__name__)
        self.repository_last_error = type(exc).__name__
        self.repository_unavailable_until_ms = ts + self.repository_cooldown_ms

    async def _calibrate_prediction_signal(self, outcome: WorldModelOutcome) -> PredictionMarketCalibration | None:
        signal = self.reducer.prediction_signals.get(outcome.target_id)
        if signal is None:
            return None
        realized = _realized_outcome_value(outcome.outcome, outcome.realized_value)
        probability = signal.implied_probability
        brier = None if probability is None or realized is None else (probability - realized) ** 2
        calibration = PredictionMarketCalibration(
            calibration_id=f"pmcal_{uuid4().hex}",
            signal_id=signal.signal_id,
            venue=signal.venue,
            market_id=signal.market_id,
            implied_probability=probability,
            realized_outcome=realized,
            brier_score=brier,
            settled_at_ms=outcome.created_at_ms,
            created_at_ms=outcome.created_at_ms,
            metadata={"outcome_id": outcome.outcome_id, "paper_only": True, "execution_authority": "none"},
        )
        record = getattr(self.repository, "upsert_prediction_market_calibration", None)
        if self._repo_available() and callable(record):
            try:
                await record(calibration.model_dump(mode="json"))
            except Exception as exc:  # pragma: no cover
                self._record_repository_error(exc, operation="upsert_prediction_market_calibration")
        return calibration


def prediction_signal_from_hip4_book(
    book: Any,
    *,
    question: Any | None,
    outcome: Any | None,
    settings: Settings,
    now: int | None = None,
) -> PredictionMarketSignal | None:
    data = _model_dump(book)
    coin = str(data.get("coin") or "")
    if not coin:
        return None
    ts = int(data.get("as_of_ms") or now_ms())
    best_bid = _top_probability(data.get("bids"), kind="bid")
    best_ask = _top_probability(data.get("asks"), kind="ask")
    mid = _mid_probability(best_bid, best_ask)
    question_data = _model_dump(question) if question is not None else {}
    outcome_data = _model_dump(outcome) if outcome is not None else {}
    title = str(question_data.get("name") or outcome_data.get("name") or f"HIP-4 outcome {data.get('outcome_id')}")
    topics = ["hip4", "prediction_market", "outcome_market"]
    topics.extend(_topics_from_text(title))
    symbols = _symbols_from_text(title, settings)
    staleness = max(0, (now or now_ms()) - ts)
    liquidity = _book_liquidity_usd(data)
    confidence = _prediction_confidence(best_bid, best_ask, liquidity, staleness)
    return PredictionMarketSignal(
        signal_id=f"pm_hip4_{coin}_{ts}",
        venue="hip4",
        market_id=str(question_data.get("question_id") or data.get("outcome_id") or coin),
        question=title,
        outcome_id=str(data.get("outcome_id") or "") or None,
        outcome_name=str(outcome_data.get("name") or data.get("side") or ""),
        symbols=symbols,
        topics=topics,
        implied_probability=mid,
        best_bid=best_bid,
        best_ask=best_ask,
        liquidity_usd=liquidity,
        status="stale" if staleness > settings.hip4_scan_max_book_staleness_ms else "open",
        as_of_ms=ts,
        staleness_ms=staleness,
        confidence=confidence,
        metadata={
            "coin": coin,
            "side": data.get("side"),
            "source": data.get("source"),
            "paper_only": True,
            "execution_authority": "none",
        },
    )


def _world_event_from_newswire(event: NewswireEvent) -> WorldEvent:
    computed = max(now_ms(), event.received_at_ms)
    staleness = None if event.published_at_ms is None else max(0, event.received_at_ms - event.published_at_ms)
    return WorldEvent(
        event_id=f"wevt_{event.event_id}",
        source_type="social" if event.event_type == "social" or event.source.startswith("x_") else "newswire",
        source=event.source,
        provider=event.provider,
        event_type=event.event_type,
        asset_class=event.asset_class,
        symbols=event.symbols,
        topics=[event.event_type, event.asset_class, event.urgency],
        title=event.headline,
        body=event.body,
        url=event.url,
        event_ts_ms=event.published_at_ms,
        received_ts_ms=event.received_at_ms,
        computed_ts_ms=computed,
        importance_score=event.importance_score,
        sentiment=_direction(event.sentiment),
        confidence=event.confidence,
        source_score=event.source_score,
        quality_score=max(event.confidence, event.source_score),
        staleness_ms=staleness,
        payload=event.model_dump(mode="json"),
        metadata={"paper_only": True, "execution_authority": "none"},
    )


def _world_event_from_legacy_news(event: NewsEvent) -> WorldEvent:
    computed = max(now_ms(), event.observed_at_ms)
    staleness = None if event.created_at_ms is None else max(0, event.observed_at_ms - event.created_at_ms)
    metadata = dict(event.metadata or {})
    return WorldEvent(
        event_id=f"wevt_{event.id}",
        source_type="newswire",
        source=event.source,
        provider=event.provider,
        event_type=str(metadata.get("event_type") or "headline"),
        asset_class=str(metadata.get("asset_class") or "unknown"),
        symbols=event.assets,
        topics=[str(metadata.get("event_type") or "headline"), str(metadata.get("asset_class") or "unknown")],
        title=event.title,
        body=event.text,
        url=event.url,
        event_ts_ms=event.created_at_ms,
        received_ts_ms=event.observed_at_ms,
        computed_ts_ms=computed,
        importance_score=event.importance_score,
        sentiment=_direction(event.sentiment),
        confidence=float(metadata.get("confidence") or event.importance_score / 100.0),
        source_score=float(metadata.get("source_score") or 0.5),
        quality_score=max(float(metadata.get("source_score") or 0.5), event.importance_score / 100.0),
        staleness_ms=staleness,
        payload=event.model_dump(mode="json"),
        metadata={"paper_only": True, "execution_authority": "none"},
    )


def _world_event_from_signal_evaluation(evaluation: SignalEvaluation) -> WorldEvent:
    ts = evaluation.completed_at_ms or evaluation.latest_price_at_ms or evaluation.created_at_ms or now_ms()
    direction = "bullish" if evaluation.terminal_outcome in {"tp_hit", "expired_positive"} else "bearish" if evaluation.terminal_outcome in {"stop_hit", "expired_negative"} else "neutral"
    return WorldEvent(
        event_id=f"wevt_signal_eval_{evaluation.id}",
        source_type="signal_evaluation",
        source="autonomy_evaluation",
        provider="internal",
        event_type="signal_evaluation",
        asset_class=str(evaluation.metadata.get("asset_class") or "crypto") if isinstance(evaluation.metadata, dict) else "crypto",
        symbols=[evaluation.symbol],
        topics=[evaluation.signal_type, evaluation.market_regime, evaluation.terminal_outcome],
        title=f"{evaluation.symbol} {evaluation.signal_type} signal outcome {evaluation.terminal_outcome}",
        body=f"Marked result={evaluation.realized_or_marked_r}; MFE={evaluation.max_favorable_r}; MAE={evaluation.max_adverse_r}",
        received_ts_ms=ts,
        computed_ts_ms=max(now_ms(), ts),
        importance_score=65.0 if evaluation.terminal_outcome in {"tp_hit", "stop_hit"} else 40.0,
        sentiment=direction,  # type: ignore[arg-type]
        confidence=min(0.9, max(0.3, abs(float(evaluation.realized_or_marked_r or 0.0)) / 3.0)),
        source_score=0.9,
        quality_score=0.9,
        payload=evaluation.model_dump(mode="json"),
        metadata={"paper_only": True, "execution_authority": "none"},
    )


def _world_event_from_alpha_event_evaluation(evaluation: AlphaEventEvaluation) -> WorldEvent:
    ts = evaluation.completed_at_ms or evaluation.latest_price_at_ms or evaluation.received_at_ms or now_ms()
    direction = "bullish" if evaluation.terminal_outcome == "worked" else "bearish" if evaluation.terminal_outcome == "failed" else "neutral"
    return WorldEvent(
        event_id=f"wevt_alpha_eval_{evaluation.id}",
        source_type="event_evaluation",
        source=evaluation.event_source,
        provider=evaluation.provider,
        event_type=f"event_evaluation:{evaluation.event_type}",
        asset_class=evaluation.asset_class,
        symbols=[evaluation.symbol],
        topics=[evaluation.event_type, evaluation.terminal_outcome, evaluation.market_regime],
        title=f"{evaluation.symbol} catalyst outcome {evaluation.terminal_outcome}: {evaluation.headline}",
        body=f"Move={evaluation.realized_or_marked_bps}; max favorable={evaluation.max_favorable_bps}; max adverse={evaluation.max_adverse_bps}",
        url=evaluation.url,
        event_ts_ms=evaluation.received_at_ms,
        received_ts_ms=ts,
        computed_ts_ms=max(now_ms(), ts),
        importance_score=evaluation.importance_score,
        sentiment=direction,  # type: ignore[arg-type]
        confidence=max(0.25, min(0.9, evaluation.source_score)),
        source_score=evaluation.source_score,
        quality_score=evaluation.source_score,
        payload=evaluation.model_dump(mode="json"),
        metadata={"paper_only": True, "execution_authority": "none", "source_event_id": evaluation.event_id},
    )


def _top_probability(levels: Any, *, kind: str) -> float | None:
    if not isinstance(levels, list) or not levels:
        return None
    level = levels[0]
    if isinstance(level, dict):
        raw = level.get("px")
    else:
        raw = getattr(level, "px", None)
    value = _float(raw)
    if value is None:
        return None
    return max(0.0, min(1.0, value))


def _mid_probability(best_bid: float | None, best_ask: float | None) -> float | None:
    if best_bid is not None and best_ask is not None:
        return (best_bid + best_ask) / 2.0
    return best_bid if best_bid is not None else best_ask


def _book_liquidity_usd(data: dict[str, Any]) -> float | None:
    total = Decimal("0")
    for side in ("bids", "asks"):
        for level in data.get(side) or []:
            if isinstance(level, dict):
                px = _decimal(level.get("px"))
                sz = _decimal(level.get("sz"))
            else:
                px = _decimal(getattr(level, "px", None))
                sz = _decimal(getattr(level, "sz", None))
            total += px * sz
    return float(total) if total > 0 else None


def _prediction_confidence(best_bid: float | None, best_ask: float | None, liquidity_usd: float | None, staleness_ms: int) -> float:
    spread_penalty = 0.0
    if best_bid is not None and best_ask is not None:
        spread_penalty = min(0.35, max(0.0, best_ask - best_bid))
    liquidity_score = min((liquidity_usd or 0.0) / 20_000.0, 0.25)
    stale_penalty = min(staleness_ms / 600_000.0, 0.35)
    return max(0.05, min(0.95, 0.55 + liquidity_score - spread_penalty - stale_penalty))


def _symbols_from_text(text: str, settings: Settings) -> list[str]:
    upper = f" {text.upper()} "
    symbols = []
    for symbol in [*settings.autonomy_core_symbols, *settings.newswire_symbols_universe]:
        if f" {symbol.upper()} " in upper or f"${symbol.upper()}" in upper:
            symbols.append(symbol.upper())
    return sorted(set(symbols))


def _topics_from_text(text: str) -> list[str]:
    lowered = text.lower()
    topics = []
    for term in ("fed", "fomc", "cpi", "rates", "inflation", "election", "crypto", "bitcoin", "ethereum", "hyperliquid"):
        if term in lowered:
            topics.append(term)
    return topics


def _edge_importance(data: dict[str, Any]) -> float:
    edge = abs(float(_decimal(data.get("expected_net_edge_bps"))))
    usd = abs(float(_decimal(data.get("expected_net_edge_usd"))))
    return min(100.0, edge / 2.0 + usd / 100.0)


def _model_dump(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if callable(getattr(value, "model_dump", None)):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return dict(value)
    return dict(getattr(value, "__dict__", {}) or {})


def _direction(value: str) -> BeliefDirection:
    if value in {"bullish", "bearish", "mixed", "neutral"}:
        return value  # type: ignore[return-value]
    return "unknown"


def _realized_outcome_value(outcome: str, realized_value: float | None) -> float | None:
    if realized_value is not None:
        return max(0.0, min(1.0, float(realized_value)))
    normalized = outcome.lower().strip()
    if normalized in {"worked", "tp_hit", "expired_positive", "true", "yes", "settled_yes", "win", "confirmed"}:
        return 1.0
    if normalized in {"failed", "stop_hit", "expired_negative", "false", "no", "settled_no", "loss", "disconfirmed"}:
        return 0.0
    return None


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except Exception:
        return Decimal("0")
