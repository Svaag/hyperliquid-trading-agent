from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.newswire.policy import EngineAction, NewsDecision, NewswireAction
from hyperliquid_trading_agent.app.newswire.schemas import (
    AudienceScope,
    EngineRouteAction,
    FeedAction,
    NewswireAssessment,
    NewswireEvent,
    NewswireStory,
    Sentiment,
)
from hyperliquid_trading_agent.app.newswire.watchlist import EntityMatch

log = get_logger(__name__)

ASSESSMENT_VERSION = "newswire_assessment_v2.1"

_SYSTEMIC_TYPES = {"halt", "regulatory", "crypto_protocol", "exchange_status", "macro"}
_HIGH_IMPACT_TERMS = (
    "hack",
    "hacked",
    "exploit",
    "depeg",
    "trading halt",
    "halted",
    "outage",
    "withdrawals suspended",
    "emergency",
    "rate decision",
    "interest rate decision",
    "etf approved",
    "etf rejected",
)
_MATERIAL_TERMS = (
    "cftc",
    "sec charges",
    "sec approves",
    "sec rejects",
    "lawsuit",
    "settlement",
    "acquisition",
    "merger",
    "earnings",
    "guidance",
    "listing",
    "delisting",
    "token unlock",
    "inflow",
    "outflow",
)
_ROUTINE_MACRO_TERMS = (
    "employee of",
    "termination of enforcement action",
    "underserved nonmetropolitan",
    "community bank",
    "public meeting",
)
_SYSTEMIC_TOPICS = {"monetary_policy", "inflation", "employment", "exchange_risk", "protocol_security"}
_NEGATIVE_RISK_TYPES = {"halt", "regulatory", "crypto_protocol", "exchange_status"}
_ACTION_RANK: dict[FeedAction, int] = {"drop": 0, "watch": 1, "standard": 2, "high": 3, "breaking": 4}


class ModelAssessmentResult(BaseModel):
    event_type: str = "headline"
    symbols: list[str] = Field(default_factory=list)
    direction: Sentiment = "unknown"
    risk_bias: float = Field(default=0.0, ge=-1.0, le=1.0)
    impact_band: Literal["routine", "notable", "material", "systemic"] = "routine"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = ""


class NewswireAssessor:
    def __init__(self, settings: Settings):
        self.settings = settings

    def assess(self, event: NewswireEvent, story: NewswireStory, entity: EntityMatch) -> NewswireAssessment:
        audience_scope = _audience_scope(event, entity)
        relevance, relevance_reason = _relevance(event, entity, audience_scope)
        impact, impact_reasons = _impact(event, entity.topics)
        urgency, urgency_reason = _urgency(event)
        source_quality = round(max(0.0, min(100.0, float(event.source_score or 0.0) * 100.0)), 4)
        novelty, novelty_reason = _novelty(story)
        priority = _priority(relevance, impact, urgency, source_quality, novelty)
        direction_confidence = _direction_confidence(event, impact, source_quality)
        risk_bias = _risk_bias(event.sentiment)
        risk_severity = _risk_severity(event, impact)
        feed_action, penalties = _feed_action(
            event,
            story,
            entity,
            priority,
            source_quality,
            impact,
            audience_scope=audience_scope,
        )
        engine_action = _engine_action(
            event,
            story,
            entity,
            impact=impact,
            source_quality=source_quality,
            direction_confidence=direction_confidence,
            risk_severity=risk_severity,
            audience_scope=audience_scope,
        )
        model_review_required, model_review_reason = self.model_review_eligibility(
            event,
            entity,
            priority,
            audience_scope=audience_scope,
        )
        decision_id = _decision_id(story.story_id, story.revision, ASSESSMENT_VERSION)
        reasons = [
            relevance_reason,
            *impact_reasons,
            urgency_reason,
            f"source_quality:{source_quality:.1f}",
            novelty_reason,
            f"priority:{priority:.1f}",
            f"audience_scope:{audience_scope}",
            f"feed_action:{feed_action}",
            f"engine_action:{engine_action}",
            f"model_review:{model_review_reason}",
        ]
        return NewswireAssessment(
            decision_id=decision_id,
            story_id=story.story_id,
            story_revision=story.revision,
            watch_priority=entity.watch_priority,
            audience_scope=audience_scope,
            matched_symbols=list(entity.symbols),
            symbol_match_reasons=dict(entity.reasons),
            topics=list(entity.topics),
            relevance_score=relevance,
            impact_score=impact,
            urgency_score=urgency,
            source_quality_score=source_quality,
            novelty_score=novelty,
            priority_score=priority,
            direction=event.sentiment,
            direction_confidence=direction_confidence,
            risk_bias=risk_bias,
            risk_severity=risk_severity,
            feed_action=feed_action,
            engine_action=engine_action,
            reason_codes=reasons,
            penalty_codes=penalties,
            model_review_state="pending" if model_review_required else "not_required",
            assessed_at_ms=_now_ms(),
        )

    def should_model_review(self, event: NewswireEvent, entity: EntityMatch, impact: float, priority: float) -> bool:
        required, _ = self.model_review_eligibility(
            event,
            entity,
            priority,
            audience_scope=_audience_scope(event, entity),
        )
        return required

    def model_review_eligibility(
        self,
        event: NewswireEvent,
        entity: EntityMatch,
        priority: float,
        *,
        audience_scope: AudienceScope,
    ) -> tuple[bool, str]:
        if not getattr(self.settings, "newswire_model_classify_enabled", True):
            return False, "disabled"
        if event.freshness == "stale":
            return False, "stale"
        if bool(event.metadata.get("newswire_startup_backlog")):
            return False, "startup_backlog"
        if bool(event.metadata.get("newswire_reclassification")) or bool(event.metadata.get("replay")):
            return False, "offline_operation"
        if event.asset_class == "equity" and audience_scope == "unwatched_single_name":
            return False, "unwatched_equity"
        boundary = next((item for item in (35.0, 50.0, 70.0, 80.0) if abs(priority - item) <= 3.0), None)
        if boundary is None:
            return False, "outside_boundary_band"
        return True, f"boundary_{int(boundary)}"

    def apply_model_review(
        self,
        event: NewswireEvent,
        story: NewswireStory,
        entity: EntityMatch,
        assessment: NewswireAssessment,
        review: ModelAssessmentResult | None,
        *,
        state: Literal["applied", "fallback", "unavailable"],
    ) -> NewswireAssessment:
        if review is None or review.confidence < 0.75:
            return assessment.model_copy(
                update={
                    "model_review_state": state if review is None else "fallback",
                    "model_review": review.model_dump(mode="json") if review is not None else None,
                }
            )
        band_target = {"routine": 30.0, "notable": 50.0, "material": 70.0, "systemic": 90.0}[review.impact_band]
        adjusted_impact = max(0.0, min(100.0, assessment.impact_score + max(-15.0, min(15.0, band_target - assessment.impact_score))))
        direction = review.direction if review.direction != "unknown" else assessment.direction
        direction_confidence = max(assessment.direction_confidence, review.confidence if direction != "unknown" else 0.0)
        risk_bias = review.risk_bias if direction != "unknown" else assessment.risk_bias
        priority = _priority(
            assessment.relevance_score,
            adjusted_impact,
            assessment.urgency_score,
            assessment.source_quality_score,
            assessment.novelty_score,
        )
        proposed_action, penalties = _feed_action(
            event,
            story,
            entity,
            priority,
            assessment.source_quality_score,
            adjusted_impact,
            audience_scope=assessment.audience_scope,
        )
        # A model can move one tier and can never independently manufacture breaking.
        max_rank = min(3, _ACTION_RANK[assessment.feed_action] + 1)
        min_rank = max(0, _ACTION_RANK[assessment.feed_action] - 1)
        proposed_rank = max(min_rank, min(max_rank, _ACTION_RANK[proposed_action]))
        feed_action = next(action for action, rank in _ACTION_RANK.items() if rank == proposed_rank)
        risk_severity = _risk_severity(event, adjusted_impact)
        engine_action = _engine_action(
            event,
            story,
            entity,
            impact=adjusted_impact,
            source_quality=assessment.source_quality_score,
            direction_confidence=direction_confidence,
            risk_severity=risk_severity,
            audience_scope=assessment.audience_scope,
        )
        return assessment.model_copy(
            update={
                "impact_score": adjusted_impact,
                "priority_score": priority,
                "direction": direction,
                "direction_confidence": direction_confidence,
                "risk_bias": risk_bias,
                "risk_severity": risk_severity,
                "feed_action": feed_action,
                "engine_action": engine_action,
                "penalty_codes": sorted(set([*assessment.penalty_codes, *penalties])),
                "reason_codes": [*assessment.reason_codes, f"model_review:{review.impact_band}:{review.confidence:.2f}"],
                "model_review_state": "applied",
                "model_review": review.model_dump(mode="json"),
                "assessed_at_ms": _now_ms(),
            }
        )


class SelectiveAssessmentReviewer:
    def __init__(self, settings: Settings, model_gateway: Any | None):
        self.settings = settings
        self.model_gateway = model_gateway
        self._call_times: list[float] = []
        self._cache: dict[str, ModelAssessmentResult] = {}
        self._queue: asyncio.Queue[
            tuple[NewswireEvent, NewswireAssessment, asyncio.Future[tuple[ModelAssessmentResult | None, str]]]
        ] = asyncio.Queue(maxsize=max(1, int(getattr(settings, "newswire_model_classify_queue_size", 32))))
        self._worker_task: asyncio.Task[None] | None = None
        self.queue_dropped = 0
        self.completed = 0
        self.fallbacks = 0

    async def start(self) -> None:
        if self.model_gateway is not None and (self._worker_task is None or self._worker_task.done()):
            self._worker_task = asyncio.create_task(self._run(), name="newswire-model-review")

    async def stop(self) -> None:
        if self._worker_task is None:
            return
        self._worker_task.cancel()
        try:
            await self._worker_task
        except asyncio.CancelledError:
            pass
        self._worker_task = None
        while not self._queue.empty():
            _, _, future = self._queue.get_nowait()
            if not future.done():
                future.set_result((None, "fallback"))
            self._queue.task_done()

    async def review(self, event: NewswireEvent, assessment: NewswireAssessment) -> tuple[ModelAssessmentResult | None, str]:
        if self.model_gateway is None:
            return None, "unavailable"
        await self.start()
        future: asyncio.Future[tuple[ModelAssessmentResult | None, str]] = asyncio.get_running_loop().create_future()
        try:
            self._queue.put_nowait((event, assessment, future))
        except asyncio.QueueFull:
            self.queue_dropped += 1
            return None, "fallback"
        return await future

    async def _run(self) -> None:
        while True:
            event, assessment, future = await self._queue.get()
            try:
                result = await self._review_once(event, assessment)
                self.completed += 1
                if result[0] is None:
                    self.fallbacks += 1
                if not future.done():
                    future.set_result(result)
            except asyncio.CancelledError:
                if not future.done():
                    future.set_result((None, "fallback"))
                raise
            finally:
                self._queue.task_done()

    async def _review_once(
        self,
        event: NewswireEvent,
        assessment: NewswireAssessment,
    ) -> tuple[ModelAssessmentResult | None, str]:
        cache_key = hashlib.sha1(f"{event.headline}\n{event.body}".encode()).hexdigest()
        if cache_key in self._cache:
            return self._cache[cache_key], "applied"
        now = time.monotonic()
        self._call_times = [item for item in self._call_times if now - item < 3600]
        limit = max(0, int(getattr(self.settings, "newswire_model_classify_max_calls_per_hour", 30)))
        if len(self._call_times) >= limit:
            return None, "fallback"
        self._call_times.append(now)
        prompt = (
            "Classify this market-news story. Use only evidence in the text. Do not give trade advice. "
            "Return the requested structured fields; risk_bias is -1 risk-off to +1 risk-on.\n"
            f"Headline: {event.headline}\nBody: {event.body[:1800]}\n"
            f"Source: {event.source}/{event.provider}\nDeterministic assessment: {assessment.model_dump_json()[:3000]}"
        )
        system = "You are a cautious financial-news classifier. Never invent tickers, facts, or confirmations."
        try:
            gateway = self.model_gateway
            if gateway is None:
                return None, "unavailable"
            configured = [
                attempt.model
                for attempt in gateway.configured_attempts()
                if getattr(attempt, "missing_reason", None) is None
            ]
            model_chain = configured[:1] or list(getattr(self.settings, "model_chain", []))[:1]
            if not model_chain:
                return None, "unavailable"
            response = await asyncio.wait_for(
                gateway.complete_with_chain(
                    prompt,
                    system
                    + " Return one valid JSON object only with event_type, symbols, direction, risk_bias, impact_band, confidence, and rationale.",
                    model_chain=model_chain,
                    temperature=0.0,
                    max_tokens=350,
                    attempt_timeout_budget=max(
                        1.0,
                        float(getattr(self.settings, "newswire_model_classify_timeout_seconds", 5.0)),
                    ),
                ),
                timeout=max(1.0, float(getattr(self.settings, "newswire_model_classify_timeout_seconds", 5.0))),
            )
            result = ModelAssessmentResult.model_validate_json(response.content)
            self._cache[cache_key] = result
            if len(self._cache) > 1000:
                self._cache.pop(next(iter(self._cache)))
            return result, "applied"
        except TimeoutError:
            return None, "fallback"
        except Exception as exc:  # pragma: no cover - provider behavior
            log.warning("newswire_model_classify_failed", error=type(exc).__name__)
            return None, "fallback"

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        calls = len([item for item in self._call_times if now - item < 3600])
        return {
            "configured": self.model_gateway is not None,
            "running": self._worker_task is not None and not self._worker_task.done(),
            "calls_last_hour": calls,
            "cache_entries": len(self._cache),
            "queue_depth": self._queue.qsize(),
            "queue_capacity": self._queue.maxsize,
            "queue_dropped": self.queue_dropped,
            "completed": self.completed,
            "fallbacks": self.fallbacks,
            "attempt_policy": "one_model_call_no_repair",
        }


def assessment_to_decision(event: NewswireEvent, story: NewswireStory, assessment: NewswireAssessment) -> NewsDecision:
    direction_score = 1.0 if assessment.direction == "bullish" else -1.0 if assessment.direction == "bearish" else 0.0
    quality = round((assessment.source_quality_score * 0.55 + assessment.novelty_score * 0.25 + float(event.confidence) * 100 * 0.20), 4)
    raw_hash = hashlib.sha1(event.model_dump_json(exclude={"metadata", "assessment"}).encode()).hexdigest()[:24]
    return NewsDecision(
        decision_id=assessment.decision_id,
        event_id=event.event_id,
        policy_version=assessment.assessment_version,
        policy_type="static",
        raw_event_hash=raw_hash,
        cluster_id=story.story_id,
        source=event.source,
        provider=event.provider,
        source_type=_source_type(event.source),
        symbols=list(assessment.matched_symbols),
        event_type=event.event_type,
        asset_class=event.asset_class,
        features={
            "watch_priority": assessment.watch_priority,
            "audience_scope": assessment.audience_scope,
            "topics": assessment.topics,
            "symbol_match_reasons": assessment.symbol_match_reasons,
            "model_review_state": assessment.model_review_state,
            "story_revision": story.revision,
            "independent_source_count": story.independent_source_count,
        },
        scores={
            "composite_priority": assessment.priority_score,
            "legacy_importance_score": event.importance_score,
            "policy_importance_score": assessment.impact_score,
        },
        newswire_action=NewswireAction(assessment.feed_action),
        engine_action=EngineAction(assessment.engine_action),
        market_impact_score=assessment.impact_score,
        quality_score=quality,
        relevance_score=assessment.relevance_score,
        novelty_score=assessment.novelty_score,
        urgency_score=assessment.urgency_score,
        source_score=assessment.source_quality_score / 100.0,
        confidence=event.confidence,
        direction_score=direction_score,
        direction_confidence=assessment.direction_confidence,
        risk_score=assessment.risk_severity,
        reasons=list(assessment.reason_codes),
        penalties=list(assessment.penalty_codes),
        created_at_ms=assessment.assessed_at_ms,
        metadata={
            "shadow_only": str(story.metadata.get("newswire_routing_mode") or "active") == "shadow",
            "story_id": story.story_id,
            "assessment_version": ASSESSMENT_VERSION,
        },
    )


def _audience_scope(event: NewswireEvent, entity: EntityMatch) -> AudienceScope:
    if entity.watch_priority != "unwatched":
        return "watched_asset"
    systemic_macro = event.asset_class == "macro" and bool(set(entity.topics) & _SYSTEMIC_TOPICS)
    broad_crypto_shock = event.asset_class == "crypto" and event.event_type in {"crypto_protocol", "exchange_status"}
    if systemic_macro or broad_crypto_shock:
        return "broad_market"
    if event.asset_class == "equity" and bool(entity.symbols):
        return "unwatched_single_name"
    return "general"


def _relevance(
    event: NewswireEvent,
    entity: EntityMatch,
    audience_scope: AudienceScope,
) -> tuple[float, str]:
    score_by_priority = {"position": 100.0, "core": 90.0, "active": 82.0, "top_volume": 70.0, "unwatched": 0.0}
    if entity.watch_priority != "unwatched":
        score = score_by_priority[entity.watch_priority]
        return score, f"watch_priority:{entity.watch_priority}"
    if audience_scope == "broad_market" and event.asset_class == "macro":
        return 75.0, "systemic_macro_scope"
    if audience_scope == "broad_market":
        return 65.0, "broad_market_scope"
    if audience_scope == "unwatched_single_name":
        return 25.0, "unwatched_single_name_scope"
    if event.asset_class == "crypto":
        return 45.0, "broad_crypto_scope"
    if event.symbols:
        return 40.0, "provider_symbol_unwatched"
    return 15.0, "unwatched_general"


def _impact(event: NewswireEvent, topics: list[str]) -> tuple[float, list[str]]:
    text = f"{event.headline} {event.body}".lower()
    if event.asset_class == "macro" and any(term in text for term in _ROUTINE_MACRO_TERMS):
        return 25.0, ["impact:routine_macro_administration"]
    if any(term in text for term in _HIGH_IMPACT_TERMS):
        return 90.0, ["impact:systemic_language"]
    if event.event_type == "halt":
        return 88.0, ["impact:trading_halt"]
    if event.event_type in {"crypto_protocol", "exchange_status"}:
        return 82.0, [f"impact:{event.event_type}"]
    if event.event_type == "macro" and set(topics) & {"monetary_policy", "inflation", "employment"}:
        return 75.0, ["impact:market_macro"]
    if event.event_type in {"mna", "regulatory", "earnings"} or any(term in text for term in _MATERIAL_TERMS):
        return 70.0, [f"impact:material_{event.event_type}"]
    if event.event_type in {"analyst_rating", "sec_filing", "press_release"}:
        return 50.0, [f"impact:notable_{event.event_type}"]
    if set(topics) & {"etf_flows", "liquidations"}:
        return 50.0, ["impact:notable_market_flow"]
    return 30.0, ["impact:routine_headline"]


def _urgency(event: NewswireEvent) -> tuple[float, str]:
    if event.freshness == "stale":
        return 0.0, "urgency:stale"
    if event.published_at_ms is None:
        return (65.0 if event.transport == "websocket" else 55.0), "urgency:publication_time_unknown"
    age_ms = max(0, int(event.received_at_ms) - int(event.published_at_ms))
    if age_ms <= 5 * 60_000:
        return 100.0, "urgency:under_5m"
    if age_ms <= 30 * 60_000:
        return 80.0, "urgency:under_30m"
    if age_ms <= 2 * 60 * 60_000:
        return 60.0, "urgency:under_2h"
    if age_ms <= 24 * 60 * 60_000:
        return 30.0, "urgency:under_24h"
    return 0.0, "urgency:stale"


def _novelty(story: NewswireStory) -> tuple[float, str]:
    update_type = str(story.metadata.get("last_update_type") or "created")
    if story.status == "retracted":
        return 0.0, "novelty:retracted"
    if story.revision == 1:
        return 100.0, "novelty:new_story"
    if update_type in {"corrected", "updated"}:
        return 75.0, f"novelty:{update_type}"
    if update_type == "confirmed":
        return 60.0, "novelty:independent_confirmation"
    return 20.0, "novelty:duplicate_update"


def _priority(relevance: float, impact: float, urgency: float, source_quality: float, novelty: float) -> float:
    return round(max(0.0, min(100.0, relevance * 0.35 + impact * 0.30 + urgency * 0.15 + source_quality * 0.10 + novelty * 0.10)), 4)


def _direction_confidence(event: NewswireEvent, impact: float, source_quality: float) -> float:
    if event.sentiment == "unknown":
        return 0.0
    if event.sentiment == "mixed":
        return min(0.4, event.confidence)
    return round(max(0.0, min(1.0, event.confidence * 0.65 + source_quality / 100.0 * 0.20 + impact / 100.0 * 0.15)), 4)


def _risk_bias(sentiment: Sentiment) -> float:
    if sentiment == "bullish":
        return 1.0
    if sentiment == "bearish":
        return -1.0
    return 0.0


def _risk_severity(event: NewswireEvent, impact: float) -> float:
    severity = impact / 100.0 * 0.75
    if event.event_type in _SYSTEMIC_TYPES:
        severity += 0.15
    if event.urgency == "breaking":
        severity += 0.10
    if event.event_type in _NEGATIVE_RISK_TYPES and event.sentiment in {"bearish", "unknown", "mixed"}:
        severity += 0.05
    return round(max(0.0, min(1.0, severity)), 4)


def _feed_action(
    event: NewswireEvent,
    story: NewswireStory,
    entity: EntityMatch,
    priority: float,
    source_quality: float,
    impact: float,
    *,
    audience_scope: AudienceScope,
) -> tuple[FeedAction, list[str]]:
    penalties: list[str] = []
    if event.action == "removed" or story.status == "retracted":
        return "drop", ["retracted"]
    if event.freshness == "stale":
        return "drop", ["stale"]
    if story.revision > 1 and story.metadata.get("last_update_type") == "duplicate":
        return "drop", ["duplicate"]
    trusted_shock = (
        audience_scope in {"watched_asset", "broad_market"}
        and source_quality >= 85
        and impact >= 85
        and event.event_type in _SYSTEMIC_TYPES
    )
    if audience_scope == "unwatched_single_name":
        if priority >= 35 or source_quality >= 85 or impact >= 85:
            return "watch", ["unwatched_single_name_cap"]
        return "drop", ["unwatched_single_name_cap"]
    if trusted_shock or priority >= 80:
        return "breaking", penalties
    if priority >= 70:
        return "high", penalties
    if priority >= 50:
        return "standard", penalties
    if entity.watch_priority in {"position", "core", "active"} and source_quality >= 45 and story.revision == 1:
        return "standard", ["watched_asset_minimum_standard"]
    if priority >= 35:
        return "watch", penalties
    return "drop", penalties


def _engine_action(
    event: NewswireEvent,
    story: NewswireStory,
    entity: EntityMatch,
    *,
    impact: float,
    source_quality: float,
    direction_confidence: float,
    risk_severity: float,
    audience_scope: AudienceScope,
) -> EngineRouteAction:
    if event.freshness == "stale" or event.action == "removed" or story.status == "retracted":
        return "ignore"
    systemic_macro = event.asset_class == "macro" and bool(set(entity.topics) & _SYSTEMIC_TOPICS)
    if systemic_macro and impact >= 65 and source_quality >= 70:
        return "macro_proxy"
    relevant = audience_scope in {"watched_asset", "broad_market"} or systemic_macro
    if not relevant:
        return "ledger_only" if impact >= 55 and source_quality >= 60 else "ignore"
    corroborated = source_quality >= 90 or story.independent_source_count >= 2
    if (
        audience_scope == "watched_asset"
        and impact >= 65
        and direction_confidence >= 0.70
        and source_quality >= 70
        and corroborated
        and entity.symbols
    ):
        return "directional_feature"
    if risk_severity >= 0.55 and impact >= 55 and source_quality >= 60:
        return "risk_only"
    return "ledger_only"


def _source_type(source: str) -> str:
    lowered = source.lower()
    if lowered in {"sec_edgar", "nasdaq_halts", "federal_reserve", "ecb"}:
        return "official"
    if lowered.startswith("x"):
        return "social"
    if lowered in {"alpaca", "benzinga", "coindesk", "cointelegraph", "globe_newswire", "business_wire", "trading_economics"}:
        return "media_or_wire"
    return "unknown"


def _decision_id(story_id: str, revision: int, version: str) -> str:
    return "nwd_" + hashlib.sha1(f"{story_id}:{revision}:{version}".encode()).hexdigest()[:24]


def _now_ms() -> int:
    return int(time.time() * 1000)
