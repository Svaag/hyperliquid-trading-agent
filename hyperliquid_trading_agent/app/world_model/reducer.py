from __future__ import annotations

import hashlib
import time
from collections import defaultdict
from typing import Any

from hyperliquid_trading_agent.app.world_model.schemas import (
    BeliefDirection,
    MarketBelief,
    NarrativeCluster,
    PredictionMarketSignal,
    SourceCredibility,
    WorldEvent,
    WorldMemoryAtom,
    WorldModelSnapshot,
)


def now_ms() -> int:
    return int(time.time() * 1000)


class WorldModelReducer:
    """Deterministic reducer for the agent's market world model.

    The reducer tracks evidence-backed beliefs and memories. It intentionally does
    not expose any order, sizing, threshold, or risk-limit mutation surface.
    """

    def __init__(self, *, max_events: int = 2_000, max_memories: int = 1_000):
        self.max_events = max_events
        self.max_memories = max_memories
        self.events: dict[str, WorldEvent] = {}
        self.beliefs: dict[str, MarketBelief] = {}
        self.narratives: dict[str, NarrativeCluster] = {}
        self.prediction_signals: dict[str, PredictionMarketSignal] = {}
        self.source_credibility: dict[str, SourceCredibility] = {}
        self.memories: dict[str, WorldMemoryAtom] = {}
        self.last_update_at_ms: int | None = None

    def observe_event(self, event: WorldEvent) -> list[MarketBelief]:
        self.events[event.event_id] = event
        self._trim_events()
        self._update_source_credibility(event)
        beliefs = self._beliefs_from_event(event)
        for belief in beliefs:
            self._upsert_belief(belief)
            self._upsert_memory_from_belief(belief, event)
        self._refresh_narratives()
        self.last_update_at_ms = event.computed_ts_ms
        return beliefs

    def observe_prediction_market_signal(self, signal: PredictionMarketSignal) -> MarketBelief:
        self.prediction_signals[signal.signal_id] = signal
        event = _event_from_prediction_signal(signal)
        self.events[event.event_id] = event
        self._trim_events()
        self._update_source_credibility(event)
        belief = _belief_from_prediction_signal(signal, event)
        self._upsert_belief(belief)
        self._upsert_memory_from_belief(belief, event, memory_type="prediction_market")
        self._refresh_narratives()
        self.last_update_at_ms = signal.as_of_ms
        return belief

    def observe_outcome_evaluation(self, *, source_event_id: str, terminal_outcome: str, confidence_delta: float = 0.05, now: int | None = None) -> None:
        ts = now or now_ms()
        event = self.events.get(source_event_id)
        if event is None:
            return
        key = _source_key(event.source, event.provider)
        current = self.source_credibility.get(key)
        if current is None:
            return
        worked = terminal_outcome in {"worked", "tp_hit", "expired_positive"}
        failed = terminal_outcome in {"failed", "stop_hit", "expired_negative"}
        score = current.score
        confirmations = current.confirmations
        contradictions = current.contradictions
        notes = list(current.notes)
        if worked:
            confirmations += 1
            score = min(1.0, score + confidence_delta)
            notes.append(f"confirmed_by:{source_event_id}")
        elif failed:
            contradictions += 1
            score = max(0.0, score - confidence_delta)
            notes.append(f"contradicted_by:{source_event_id}")
        self.source_credibility[key] = current.model_copy(
            update={
                "score": score,
                "confirmations": confirmations,
                "contradictions": contradictions,
                "last_updated_at_ms": ts,
                "notes": notes[-20:],
            }
        )

    def snapshot(
        self,
        *,
        symbols: list[str] | None = None,
        topics: list[str] | None = None,
        max_beliefs: int = 20,
        max_clusters: int = 8,
        max_prediction_signals: int = 12,
        max_memories: int = 12,
        as_of_ms: int | None = None,
    ) -> WorldModelSnapshot:
        ts = as_of_ms or now_ms()
        symbol_set = {item.upper() for item in symbols or [] if item}
        topic_set = {item.lower() for item in topics or [] if item}
        beliefs = [item for item in self.beliefs.values() if item.status == "active" and _matches(item.symbols, item.topics, symbol_set, topic_set)]
        beliefs.sort(key=lambda item: (item.salience, item.confidence, item.updated_at_ms), reverse=True)
        clusters = [item for item in self.narratives.values() if _matches(item.symbols, item.topics, symbol_set, topic_set)]
        clusters.sort(key=lambda item: (abs(item.pressure_score), item.consensus_score, item.updated_at_ms), reverse=True)
        predictions = [item for item in self.prediction_signals.values() if _matches(item.symbols, item.topics, symbol_set, topic_set)]
        predictions.sort(key=lambda item: (item.confidence, item.liquidity_usd or 0.0, item.as_of_ms), reverse=True)
        memories = [item for item in self.memories.values() if _matches(item.symbols, item.topics, symbol_set, topic_set)]
        memories.sort(key=lambda item: (item.salience, item.confidence, item.last_reinforced_at_ms or item.created_at_ms), reverse=True)
        quality_flags = self._quality_flags(ts, predictions)
        selected_beliefs = beliefs[:max_beliefs]
        snapshot_id = "wm_" + hashlib.sha1(
            f"{ts}:{symbol_set}:{topic_set}:{[item.belief_id for item in selected_beliefs]}".encode()
        ).hexdigest()[:24]
        return WorldModelSnapshot(
            snapshot_id=snapshot_id,
            as_of_ms=ts,
            symbols=sorted(symbol_set),
            topics=sorted(topic_set),
            top_beliefs=selected_beliefs,
            narrative_clusters=clusters[:max_clusters],
            prediction_market_signals=predictions[:max_prediction_signals],
            source_credibility=sorted(self.source_credibility.values(), key=lambda item: (item.score, item.observations), reverse=True)[:20],
            memory_atoms=memories[:max_memories],
            quality_flags=quality_flags,
            summary=_summary(selected_beliefs, predictions[:max_prediction_signals], clusters[:max_clusters]),
            metadata={"paper_only": True, "execution_authority": "none"},
        )

    def wiki_block(
        self,
        *,
        symbols: list[str] | None = None,
        topics: list[str] | None = None,
        max_chars: int = 2_000,
    ) -> str:
        snapshot = self.snapshot(symbols=symbols, topics=topics, max_beliefs=8, max_clusters=4, max_prediction_signals=5, max_memories=5)
        if not snapshot.top_beliefs and not snapshot.prediction_market_signals and not snapshot.memory_atoms:
            return ""
        lines = ["Market world model (advisory evidence only; no execution authority):"]
        for belief in snapshot.top_beliefs:
            scope = ",".join(belief.symbols or belief.topics) or belief.subject
            probability = "" if belief.probability is None else f" p={belief.probability:.2f}"
            lines.append(f"- belief[{belief.kind}:{scope}] {belief.statement} confidence={belief.confidence:.2f}{probability}")
        for signal in snapshot.prediction_market_signals:
            probability = "n/a" if signal.implied_probability is None else f"{signal.implied_probability:.2f}"
            scope = ",".join(signal.symbols or signal.topics) or signal.market_id
            lines.append(f"- prediction[{signal.venue}:{scope}] {signal.question} -> {signal.outcome_name or 'outcome'} p={probability} confidence={signal.confidence:.2f}")
        for memory in snapshot.memory_atoms:
            scope = ",".join(memory.symbols or memory.topics) or memory.subject
            lines.append(f"- memory[{memory.memory_type}:{scope}] {memory.content} confidence={memory.confidence:.2f}")
        return "\n".join(lines)[:max_chars]

    def status(self) -> dict[str, Any]:
        return {
            "events": len(self.events),
            "beliefs": len(self.beliefs),
            "active_beliefs": len([item for item in self.beliefs.values() if item.status == "active"]),
            "narrative_clusters": len(self.narratives),
            "prediction_market_signals": len(self.prediction_signals),
            "source_credibility": len(self.source_credibility),
            "memory_atoms": len(self.memories),
            "last_update_at_ms": self.last_update_at_ms,
            "paper_only": True,
            "execution_authority": "none",
        }

    def _beliefs_from_event(self, event: WorldEvent) -> list[MarketBelief]:
        if event.source_type not in {"newswire", "social", "event_evaluation", "signal_evaluation", "engine"}:
            return []
        if event.importance_score < 10 and event.source_type in {"newswire", "social"}:
            return []
        direction = _direction(event.sentiment)
        kind = "catalyst" if event.source_type in {"newswire", "social"} else "memory"
        subject = _subject(event)
        statement = _event_statement(event)
        confidence = _clamp(0.15 + event.source_score * 0.35 + event.confidence * 0.25 + (event.importance_score / 100.0) * 0.25)
        salience = _clamp((event.importance_score / 100.0) * 0.65 + confidence * 0.35)
        ttl_ms = 7 * 86_400_000 if kind == "catalyst" else 30 * 86_400_000
        belief = MarketBelief(
            belief_id=_stable_id("bel", event.event_id, kind, subject),
            kind=kind,
            subject=subject,
            statement=statement,
            symbols=event.symbols,
            topics=event.topics or [_topic_for_event(event)],
            direction=direction,
            confidence=confidence,
            salience=salience,
            evidence_event_ids=[event.event_id],
            created_at_ms=event.computed_ts_ms,
            updated_at_ms=event.computed_ts_ms,
            expires_at_ms=event.computed_ts_ms + ttl_ms,
            metadata={"source_type": event.source_type, "event_type": event.event_type, "paper_only": True},
        )
        return [belief]

    def _upsert_belief(self, belief: MarketBelief) -> None:
        existing = self.beliefs.get(belief.belief_id)
        if existing is not None:
            belief = existing.model_copy(
                update={
                    "confidence": max(existing.confidence, belief.confidence),
                    "salience": max(existing.salience, belief.salience),
                    "evidence_event_ids": _dedupe([*existing.evidence_event_ids, *belief.evidence_event_ids])[-20:],
                    "updated_at_ms": max(existing.updated_at_ms, belief.updated_at_ms),
                    "expires_at_ms": max(existing.expires_at_ms or 0, belief.expires_at_ms or 0) or None,
                }
            )
        contradictions = self._contradicting_beliefs(belief)
        if contradictions:
            belief = belief.model_copy(update={"contradicts_belief_ids": _dedupe([*belief.contradicts_belief_ids, *[item.belief_id for item in contradictions]])})
            for item in contradictions:
                self.beliefs[item.belief_id] = item.model_copy(
                    update={"contradicts_belief_ids": _dedupe([*item.contradicts_belief_ids, belief.belief_id])}
                )
        self.beliefs[belief.belief_id] = belief

    def _contradicting_beliefs(self, belief: MarketBelief) -> list[MarketBelief]:
        if belief.direction not in {"bullish", "bearish"}:
            return []
        opposite = "bearish" if belief.direction == "bullish" else "bullish"
        out = []
        for item in self.beliefs.values():
            if item.belief_id == belief.belief_id or item.status != "active":
                continue
            if item.kind not in {belief.kind, "catalyst", "probability", "narrative"}:
                continue
            if item.direction != opposite:
                continue
            if set(item.symbols) & set(belief.symbols) or set(item.topics) & set(belief.topics):
                out.append(item)
        return out[:10]

    def _update_source_credibility(self, event: WorldEvent) -> None:
        key = _source_key(event.source, event.provider)
        previous = self.source_credibility.get(key)
        observed_score = _clamp(event.source_score or event.confidence or event.quality_score or 0.5)
        if previous is None:
            self.source_credibility[key] = SourceCredibility(
                source_key=key,
                source=event.source,
                provider=event.provider,
                score=observed_score,
                observations=1,
                last_updated_at_ms=event.computed_ts_ms,
                notes=[f"first_seen:{event.event_type}"],
            )
            return
        observations = previous.observations + 1
        score = _clamp((previous.score * previous.observations + observed_score) / observations)
        self.source_credibility[key] = previous.model_copy(
            update={
                "score": score,
                "observations": observations,
                "last_updated_at_ms": event.computed_ts_ms,
                "notes": [*previous.notes, f"observed:{event.event_type}"][-20:],
            }
        )

    def _upsert_memory_from_belief(self, belief: MarketBelief, event: WorldEvent, *, memory_type: str | None = None) -> None:
        mtype = memory_type or ("working" if belief.kind == "catalyst" else "episodic")
        memory_id = _stable_id("wmem", mtype, belief.subject, ",".join(belief.symbols), ",".join(belief.topics))
        existing = self.memories.get(memory_id)
        content = belief.statement
        if existing is not None:
            self.memories[memory_id] = existing.model_copy(
                update={
                    "confidence": max(existing.confidence, belief.confidence),
                    "salience": max(existing.salience, belief.salience),
                    "source_event_ids": _dedupe([*existing.source_event_ids, event.event_id])[-20:],
                    "source_belief_ids": _dedupe([*existing.source_belief_ids, belief.belief_id])[-20:],
                    "last_reinforced_at_ms": event.computed_ts_ms,
                }
            )
            return
        self.memories[memory_id] = WorldMemoryAtom(
            memory_id=memory_id,
            memory_type=mtype,  # type: ignore[arg-type]
            subject=belief.subject,
            content=content,
            symbols=belief.symbols,
            topics=belief.topics,
            source_event_ids=[event.event_id],
            source_belief_ids=[belief.belief_id],
            confidence=belief.confidence,
            salience=belief.salience,
            created_at_ms=event.computed_ts_ms,
            last_reinforced_at_ms=event.computed_ts_ms,
            expires_at_ms=belief.expires_at_ms,
            metadata={"paper_only": True},
        )
        self._trim_memories()

    def _refresh_narratives(self) -> None:
        grouped: dict[str, list[MarketBelief]] = defaultdict(list)
        for belief in self.beliefs.values():
            if belief.status != "active":
                continue
            key = (belief.symbols[0] if belief.symbols else belief.topics[0] if belief.topics else belief.subject).lower()
            grouped[key].append(belief)
        for key, beliefs in grouped.items():
            if not beliefs:
                continue
            symbols = _dedupe(symbol for belief in beliefs for symbol in belief.symbols)
            topics = _dedupe(topic for belief in beliefs for topic in belief.topics)
            event_ids = _dedupe(event_id for belief in beliefs for event_id in belief.evidence_event_ids)
            signed = [_signed_pressure(belief.direction) * belief.salience for belief in beliefs]
            pressure = _clamp(sum(signed) / max(len(signed), 1), -1.0, 1.0)
            bullish = len([belief for belief in beliefs if belief.direction == "bullish"])
            bearish = len([belief for belief in beliefs if belief.direction == "bearish"])
            directional = max(1, bullish + bearish)
            conflict = min(bullish, bearish) / directional
            consensus = 1.0 - conflict
            newest = max(belief.updated_at_ms for belief in beliefs)
            existing = self.narratives.get(key)
            created = existing.created_at_ms if existing is not None else min(belief.created_at_ms for belief in beliefs)
            title = ",".join(symbols[:3]) if symbols else key
            self.narratives[key] = NarrativeCluster(
                cluster_id=_stable_id("narr", key),
                title=title.upper() if symbols else title,
                summary=_cluster_summary(title, beliefs, pressure, conflict),
                symbols=symbols,
                topics=topics,
                belief_ids=[belief.belief_id for belief in sorted(beliefs, key=lambda item: item.salience, reverse=True)[:20]],
                event_ids=event_ids[-50:],
                pressure_score=pressure,
                consensus_score=consensus,
                conflict_score=conflict,
                created_at_ms=created,
                updated_at_ms=newest,
                metadata={"paper_only": True},
            )

    def _trim_events(self) -> None:
        if len(self.events) <= self.max_events:
            return
        for event_id, _ in sorted(self.events.items(), key=lambda item: item[1].computed_ts_ms)[: len(self.events) - self.max_events]:
            self.events.pop(event_id, None)

    def _trim_memories(self) -> None:
        if len(self.memories) <= self.max_memories:
            return
        ordered = sorted(self.memories.items(), key=lambda item: (item[1].salience, item[1].created_at_ms))
        for memory_id, _ in ordered[: len(self.memories) - self.max_memories]:
            self.memories.pop(memory_id, None)

    def _quality_flags(self, timestamp_ms: int, predictions: list[PredictionMarketSignal]) -> list[str]:
        flags = []
        if not self.events:
            flags.append("no_world_events")
        if predictions and all((item.staleness_ms or 0) > 300_000 for item in predictions):
            flags.append("prediction_markets_stale")
        if self.source_credibility and max(item.score for item in self.source_credibility.values()) < 0.4:
            flags.append("low_source_credibility")
        if self.last_update_at_ms and timestamp_ms - self.last_update_at_ms > 600_000:
            flags.append("world_model_stale")
        return flags


def _event_from_prediction_signal(signal: PredictionMarketSignal) -> WorldEvent:
    ts = now_ms()
    staleness = max(0, ts - signal.as_of_ms)
    return WorldEvent(
        event_id=_stable_id("wevt", "prediction", signal.signal_id),
        source_type="prediction_market",
        source=signal.venue,
        provider=signal.venue,
        event_type="prediction_market_quote",
        asset_class="prediction_market",
        symbols=signal.symbols,
        topics=signal.topics,
        title=signal.question,
        body=signal.outcome_name,
        event_ts_ms=signal.as_of_ms,
        received_ts_ms=signal.as_of_ms,
        computed_ts_ms=max(ts, signal.as_of_ms),
        importance_score=_clamp((signal.liquidity_usd or 0.0) / 10_000.0, 0.0, 1.0) * 100.0,
        sentiment=_direction_from_probability(signal.implied_probability),
        confidence=signal.confidence,
        source_score=signal.confidence,
        quality_score=signal.confidence,
        staleness_ms=signal.staleness_ms if signal.staleness_ms is not None else staleness,
        payload=signal.model_dump(mode="json"),
        metadata={"paper_only": True, "execution_authority": "none"},
    )


def _belief_from_prediction_signal(signal: PredictionMarketSignal, event: WorldEvent) -> MarketBelief:
    probability_text = "unknown" if signal.implied_probability is None else f"{signal.implied_probability:.1%}"
    outcome = f" for {signal.outcome_name}" if signal.outcome_name else ""
    statement = f"{signal.venue} implies {probability_text} probability{outcome}: {signal.question}"
    salience = _clamp((signal.confidence * 0.5) + min((signal.liquidity_usd or 0.0) / 50_000.0, 1.0) * 0.3 + (abs(signal.probability_delta or 0.0) * 0.2))
    return MarketBelief(
        belief_id=_stable_id("bel", "prediction", signal.signal_id),
        kind="probability",
        subject=signal.question,
        statement=statement,
        symbols=signal.symbols,
        topics=signal.topics or ["prediction_market"],
        direction=_direction_from_probability(signal.implied_probability),
        probability=signal.implied_probability,
        confidence=signal.confidence,
        salience=salience,
        evidence_event_ids=[event.event_id, *signal.source_event_ids],
        created_at_ms=event.computed_ts_ms,
        updated_at_ms=event.computed_ts_ms,
        expires_at_ms=event.computed_ts_ms + 6 * 60 * 60 * 1000,
        metadata={"venue": signal.venue, "market_id": signal.market_id, "paper_only": True, "execution_authority": "none"},
    )


def _summary(beliefs: list[MarketBelief], predictions: list[PredictionMarketSignal], clusters: list[NarrativeCluster]) -> str:
    parts = []
    if clusters:
        top = clusters[0]
        bias = "bullish" if top.pressure_score > 0.15 else "bearish" if top.pressure_score < -0.15 else "mixed"
        parts.append(f"Top narrative {top.title} is {bias} with consensus={top.consensus_score:.2f}.")
    if predictions:
        signal = predictions[0]
        probability = "unknown" if signal.implied_probability is None else f"{signal.implied_probability:.1%}"
        parts.append(f"Top prediction market prior: {probability} on {signal.question}.")
    if beliefs:
        parts.append(f"{len(beliefs)} active scoped beliefs selected.")
    return " ".join(parts)


def _event_statement(event: WorldEvent) -> str:
    title = event.title.strip() or event.event_type
    source = event.source if event.provider in {"", "unknown", event.source} else f"{event.source}/{event.provider}"
    return f"{title} [{source}]"


def _subject(event: WorldEvent) -> str:
    if event.symbols:
        return event.symbols[0]
    if event.topics:
        return event.topics[0]
    return event.event_type


def _topic_for_event(event: WorldEvent) -> str:
    if event.event_type and event.event_type != "unknown":
        return event.event_type.lower()
    if event.asset_class and event.asset_class != "unknown":
        return event.asset_class.lower()
    return event.source_type


def _direction(value: str) -> BeliefDirection:
    if value in {"bullish", "bearish", "mixed", "neutral"}:
        return value  # type: ignore[return-value]
    return "unknown"


def _direction_from_probability(probability: float | None) -> BeliefDirection:
    if probability is None:
        return "unknown"
    if probability >= 0.58:
        return "bullish"
    if probability <= 0.42:
        return "bearish"
    return "neutral"


def _signed_pressure(direction: str) -> float:
    if direction == "bullish":
        return 1.0
    if direction == "bearish":
        return -1.0
    return 0.0


def _cluster_summary(title: str, beliefs: list[MarketBelief], pressure: float, conflict: float) -> str:
    direction = "bullish" if pressure > 0.15 else "bearish" if pressure < -0.15 else "mixed"
    return f"{title} narrative is {direction}; evidence_count={len(beliefs)} conflict={conflict:.2f}."


def _matches(symbols: list[str], topics: list[str], symbol_set: set[str], topic_set: set[str]) -> bool:
    if not symbol_set and not topic_set:
        return True
    return bool(set(symbols) & symbol_set or set(topics) & topic_set)


def _source_key(source: str, provider: str) -> str:
    return f"{source or 'unknown'}:{provider or 'unknown'}"


def _stable_id(prefix: str, *parts: object) -> str:
    digest = hashlib.sha1(":".join(str(part) for part in parts).encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _dedupe(items) -> list:
    out = []
    for item in items:
        if item is None or item == "":
            continue
        if item not in out:
            out.append(item)
    return out


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, float(value)))
