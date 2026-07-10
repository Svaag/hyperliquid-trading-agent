from __future__ import annotations

import hashlib
import time
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.newswire.keyword_matcher import score_importance_details
from hyperliquid_trading_agent.app.newswire.schemas import NewswireEvent

POLICY_TYPE = Literal["static", "bandit"]
POLICY_STATUS = Literal["shadow", "candidate", "canary", "promoted", "retired"]
EVALUATOR_TYPE = Literal["human", "auto", "market", "confirmation"]


class NewswireAction(str, Enum):
    DROP = "drop"
    WATCH = "watch"
    STANDARD = "standard"
    HIGH = "high"
    BREAKING = "breaking"


class EngineAction(str, Enum):
    IGNORE = "ignore"
    LEDGER_ONLY = "ledger_only"
    RISK_ONLY = "risk_only"
    DIRECTIONAL_FEATURE = "directional_feature"
    MACRO_PROXY = "macro_proxy"


class NewsDecision(BaseModel):
    decision_id: str
    event_id: str
    policy_version: str
    policy_type: POLICY_TYPE = "static"
    raw_event_hash: str
    cluster_id: str | None = None
    source: str
    provider: str
    source_type: str = "unknown"
    symbols: list[str] = Field(default_factory=list)
    event_type: str
    asset_class: str
    features: dict[str, Any] = Field(default_factory=dict)
    scores: dict[str, Any] = Field(default_factory=dict)
    newswire_action: NewswireAction
    engine_action: EngineAction
    market_impact_score: float = Field(ge=0.0, le=100.0)
    quality_score: float = Field(ge=0.0, le=100.0)
    relevance_score: float = Field(ge=0.0, le=100.0)
    novelty_score: float = Field(ge=0.0, le=100.0)
    urgency_score: float = Field(ge=0.0, le=100.0)
    source_score: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    direction_score: float = Field(ge=-1.0, le=1.0)
    direction_confidence: float = Field(ge=0.0, le=1.0)
    risk_score: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)
    penalties: list[str] = Field(default_factory=list)
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class NewsEval(BaseModel):
    eval_id: str | None = None
    event_id: str
    decision_id: str | None = None
    policy_version: str | None = None
    evaluator_type: EVALUATOR_TYPE
    evaluator_id: str | None = None
    label_type: str
    label_value: Any
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    reason: str | None = None
    notes: str | None = None
    created_at_ms: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class NewsReward(BaseModel):
    reward_id: str
    event_id: str
    decision_id: str | None = None
    policy_version: str
    total_reward: float
    reward_components: dict[str, float] = Field(default_factory=dict)
    labels: dict[str, Any] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class NewsPolicyVersion(BaseModel):
    policy_version: str
    policy_type: POLICY_TYPE
    status: POLICY_STATUS
    params: dict[str, Any] = Field(default_factory=dict)
    model_uri: str | None = None
    replay_metrics: dict[str, Any] = Field(default_factory=dict)
    canary_metrics: dict[str, Any] = Field(default_factory=dict)
    created_at_ms: int
    promoted_at_ms: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


BASELINE_POLICY_VERSION = "newswire_baseline_v1"


class DeterministicNewswirePolicy:
    """Deterministic hot-path policy used for promoted and shadow scoring."""

    def __init__(self, settings: Settings, *, policy_version: str = BASELINE_POLICY_VERSION, params: dict[str, Any] | None = None):
        self.settings = settings
        self.policy_version = policy_version
        self.params = params or {}

    def score(self, event: NewswireEvent) -> NewsDecision:
        now = _now_ms()
        importance = score_importance_details(event.headline, event.body, "", {})
        source_score = _source_score(event, self.params)
        confidence = max(0.0, min(1.0, float(event.confidence or 0.0)))
        market_impact = _clamp100(importance.score)
        relevance = _relevance_score(event)
        novelty = 45.0 if bool((event.metadata or {}).get("duplicate_cluster_id")) else 75.0
        urgency = _urgency_score(event)
        quality = _quality_score(event, market_impact=market_impact, relevance=relevance, novelty=novelty, source_score=source_score, penalties=importance.penalties)
        direction_score, direction_confidence = _direction(event, confidence=confidence)
        risk_score = _risk_score(event, market_impact=market_impact)
        composite = _composite_priority(
            market_impact=market_impact,
            quality=quality,
            relevance=relevance,
            novelty=novelty,
            urgency=urgency,
            source_score=source_score,
            confidence=confidence,
        )
        newswire_action = _newswire_action(event, composite=composite, quality=quality)
        engine_action = _engine_action(event, newswire_action=newswire_action, quality=quality, source_score=source_score, direction_confidence=direction_confidence, risk_score=risk_score)
        reasons = [*importance.reasons, f"composite:{composite:.1f}", f"newswire_action:{newswire_action.value}", f"engine_action:{engine_action.value}"]
        if source_score < 0.55:
            reasons.append("low_source_score_guard")
        if event.sentiment == "unknown":
            reasons.append("unknown_direction")
        penalties = list(importance.penalties)
        if event.freshness == "stale":
            penalties.append("stale_event")
        raw_hash = hashlib.sha1(event.model_dump_json(exclude={"metadata"}).encode()).hexdigest()[:24]
        decision_id = "nwd_" + hashlib.sha1(f"{event.event_id}:{self.policy_version}:{raw_hash}".encode()).hexdigest()[:24]
        return NewsDecision(
            decision_id=decision_id,
            event_id=event.event_id,
            policy_version=self.policy_version,
            policy_type="static" if self.params.get("learner") is None else "bandit",
            raw_event_hash=raw_hash,
            cluster_id=str((event.metadata or {}).get("cluster_id") or "") or None,
            source=event.source,
            provider=event.provider,
            source_type=_source_type(event.source),
            symbols=list(event.symbols),
            event_type=event.event_type,
            asset_class=event.asset_class,
            features={
                "importance_keyword_hits": importance.keyword_hits,
                "urgency": event.urgency,
                "freshness": event.freshness,
                "sentiment": event.sentiment,
                "symbol_count": len(event.symbols),
            },
            scores={
                "composite_priority": composite,
                "legacy_importance_score": event.importance_score,
                "policy_importance_score": importance.score,
            },
            newswire_action=newswire_action,
            engine_action=engine_action,
            market_impact_score=market_impact,
            quality_score=quality,
            relevance_score=relevance,
            novelty_score=novelty,
            urgency_score=urgency,
            source_score=source_score,
            confidence=confidence,
            direction_score=direction_score,
            direction_confidence=direction_confidence,
            risk_score=risk_score,
            reasons=reasons,
            penalties=penalties,
            created_at_ms=now,
            metadata={"shadow_only": bool(getattr(self.settings, "newswire_policy_shadow_only", True))},
        )


def decision_summary(decision: NewsDecision | dict[str, Any] | None) -> dict[str, Any] | None:
    if decision is None:
        return None
    data = decision.model_dump(mode="json") if isinstance(decision, NewsDecision) else dict(decision)
    raw_metadata = data.get("metadata")
    metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
    return {
        "decision_id": data.get("decision_id"),
        "policy_version": data.get("policy_version"),
        "policy_type": data.get("policy_type"),
        "shadow_only": bool(metadata.get("shadow_only", False)),
        "newswire_action": data.get("newswire_action"),
        "engine_action": data.get("engine_action"),
        "quality_score": data.get("quality_score"),
        "market_impact_score": data.get("market_impact_score"),
        "relevance_score": data.get("relevance_score"),
        "novelty_score": data.get("novelty_score"),
        "urgency_score": data.get("urgency_score"),
        "source_score": data.get("source_score"),
        "confidence": data.get("confidence"),
        "direction_score": data.get("direction_score"),
        "direction_confidence": data.get("direction_confidence"),
        "risk_score": data.get("risk_score"),
        "reasons": list(data.get("reasons") or [])[:12],
        "penalties": list(data.get("penalties") or [])[:12],
    }


def _newswire_action(event: NewswireEvent, *, composite: float, quality: float) -> NewswireAction:
    if event.freshness == "stale" or event.action == "removed":
        return NewswireAction.DROP
    if event.urgency == "breaking" and quality >= 50 and composite >= 60:
        return NewswireAction.BREAKING
    if composite >= 85:
        return NewswireAction.BREAKING
    if composite >= 70:
        return NewswireAction.HIGH
    if composite >= 50:
        return NewswireAction.STANDARD
    if composite >= 30:
        return NewswireAction.WATCH
    return NewswireAction.DROP


def _engine_action(event: NewswireEvent, *, newswire_action: NewswireAction, quality: float, source_score: float, direction_confidence: float, risk_score: float) -> EngineAction:
    if newswire_action in {NewswireAction.DROP, NewswireAction.WATCH}:
        return EngineAction.IGNORE
    if event.asset_class == "macro" and source_score >= 0.8 and quality >= 70:
        return EngineAction.MACRO_PROXY
    if source_score < 0.55:
        return EngineAction.LEDGER_ONLY
    if event.event_type in {"halt", "regulatory", "exchange_status"} or risk_score >= 0.65:
        return EngineAction.RISK_ONLY
    if direction_confidence >= 0.55 and quality >= 60:
        return EngineAction.DIRECTIONAL_FEATURE
    return EngineAction.RISK_ONLY


def _composite_priority(*, market_impact: float, quality: float, relevance: float, novelty: float, urgency: float, source_score: float, confidence: float) -> float:
    score = (
        market_impact * 0.28
        + quality * 0.26
        + relevance * 0.18
        + novelty * 0.08
        + urgency * 0.12
        + source_score * 100.0 * 0.05
        + confidence * 100.0 * 0.03
    )
    return _clamp100(score)


def _quality_score(event: NewswireEvent, *, market_impact: float, relevance: float, novelty: float, source_score: float, penalties: list[str]) -> float:
    score = 25.0 + market_impact * 0.25 + relevance * 0.2 + novelty * 0.15 + source_score * 25.0 + float(event.confidence or 0) * 15.0
    score -= 18.0 * len(penalties)
    if event.freshness == "stale":
        score -= 35.0
    return _clamp100(score)


def _relevance_score(event: NewswireEvent) -> float:
    if event.asset_class == "macro":
        return 75.0 if event.importance_score >= 60 else 50.0
    if event.symbols:
        return 85.0 if event.asset_class in {"crypto", "equity"} else 65.0
    if event.asset_class == "crypto":
        return 55.0
    return 35.0


def _urgency_score(event: NewswireEvent) -> float:
    if event.urgency == "breaking":
        return 90.0
    if event.urgency == "background":
        return 20.0
    return 55.0


def _direction(event: NewswireEvent, *, confidence: float) -> tuple[float, float]:
    if event.sentiment == "bullish":
        return 1.0, confidence
    if event.sentiment == "bearish":
        return -1.0, confidence
    if event.sentiment == "mixed":
        return 0.0, min(0.35, confidence)
    return 0.0, 0.0


def _risk_score(event: NewswireEvent, *, market_impact: float) -> float:
    base = market_impact / 100.0
    if event.event_type in {"halt", "regulatory", "exchange_status", "crypto_protocol", "macro"}:
        base += 0.25
    if event.urgency == "breaking":
        base += 0.15
    if event.sentiment in {"bearish", "mixed", "unknown"}:
        base += 0.05
    return max(0.0, min(1.0, base))


def _source_score(event: NewswireEvent, params: dict[str, Any]) -> float:
    learned = (params.get("source_reputation") or {}).get(event.source)
    value = float(learned if learned is not None else event.source_score or 0.0)
    return max(0.0, min(_source_cap(event.source), value))


def _source_type(source: str) -> str:
    lowered = source.lower()
    if lowered in {"sec_edgar", "nasdaq_halts", "federal_reserve", "ecb"}:
        return "official"
    if lowered in {"alpaca", "benzinga", "coindesk", "cointelegraph", "globe_newswire", "business_wire", "trading_economics"}:
        return "media_or_wire"
    if lowered.startswith("x"):
        return "social"
    return "unknown"


def _source_cap(source: str) -> float:
    source_type = _source_type(source)
    if source_type == "official":
        return 1.0
    if source_type == "media_or_wire":
        return 0.9
    if source_type == "social":
        return 0.7 if "allowlist" in source else 0.45
    return 0.5


def _clamp100(value: float) -> float:
    return round(max(0.0, min(100.0, float(value))), 4)


def _now_ms() -> int:
    return int(time.time() * 1000)
