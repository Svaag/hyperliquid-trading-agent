from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from hyperliquid_trading_agent.app.autonomy.schemas import Freshness, NewsEvent, Sentiment

# --- canonical enums ---------------------------------------------------------

Transport = Literal["websocket", "rss", "rest", "poll"]
Action = Literal["created", "updated", "removed"]
AssetClass = Literal["equity", "crypto", "macro", "fx", "commodity", "unknown"]
Urgency = Literal["breaking", "normal", "background"]
WatchPriority = Literal["position", "core", "active", "top_volume", "unwatched"]
AudienceScope = Literal["watched_asset", "broad_market", "unwatched_single_name", "general"]
FeedAction = Literal["drop", "watch", "standard", "high", "breaking"]
EngineRouteAction = Literal["ignore", "ledger_only", "risk_only", "directional_feature", "macro_proxy"]
ModelReviewState = Literal["not_required", "pending", "applied", "fallback", "unavailable"]
StoryStatus = Literal["active", "corrected", "retracted"]
StoryUpdateType = Literal["created", "confirmed", "updated", "corrected", "retracted", "reclassified", "duplicate"]
EventType = Literal[
    "analyst_rating",
    "earnings",
    "sec_filing",
    "halt",
    "macro",
    "mna",
    "regulatory",
    "crypto_protocol",
    "exchange_status",
    "press_release",
    "social",
    "headline",
    "other",
]

URGENCY_RANK: dict[str, int] = {"background": 0, "normal": 1, "breaking": 2}


class NewswireAssessment(BaseModel):
    """Versioned routing decision shared by every Newswire consumer.

    The scalar scores remain explainable and independently inspectable.  Consumers route
    from ``feed_action`` / ``engine_action`` rather than rebuilding their own threshold
    logic from ``importance_score``.
    """

    assessment_version: str = "newswire_assessment_v2.1"
    decision_id: str
    story_id: str
    story_revision: int = 1
    watch_priority: WatchPriority = "unwatched"
    audience_scope: AudienceScope = "general"
    matched_symbols: list[str] = Field(default_factory=list)
    symbol_match_reasons: dict[str, list[str]] = Field(default_factory=dict)
    topics: list[str] = Field(default_factory=list)
    relevance_score: float = Field(default=0.0, ge=0.0, le=100.0)
    impact_score: float = Field(default=0.0, ge=0.0, le=100.0)
    urgency_score: float = Field(default=0.0, ge=0.0, le=100.0)
    source_quality_score: float = Field(default=0.0, ge=0.0, le=100.0)
    novelty_score: float = Field(default=0.0, ge=0.0, le=100.0)
    priority_score: float = Field(default=0.0, ge=0.0, le=100.0)
    direction: Sentiment = "unknown"
    direction_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    risk_bias: float = Field(default=0.0, ge=-1.0, le=1.0)
    risk_severity: float = Field(default=0.0, ge=0.0, le=1.0)
    feed_action: FeedAction = "drop"
    engine_action: EngineRouteAction = "ignore"
    reason_codes: list[str] = Field(default_factory=list)
    penalty_codes: list[str] = Field(default_factory=list)
    model_review_state: ModelReviewState = "not_required"
    model_review: dict[str, Any] | None = None
    assessed_at_ms: int


class NewswireStory(BaseModel):
    """Canonical, clustered story delivered to product and engine consumers."""

    story_id: str
    schema_version: int = 2
    revision: int = Field(default=1, ge=1)
    canonical_event_id: str
    headline: str
    body: str = ""
    url: str | None = None
    source: str
    provider: str
    sources: list[str] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list)
    member_event_ids: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    asset_class: AssetClass = "unknown"
    event_type: EventType = "headline"
    urgency: Urgency = "normal"
    sentiment: Sentiment = "unknown"
    source_score: float = Field(default=0.0, ge=0.0, le=1.0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    published_at_ms: int | None = None
    first_seen_at_ms: int
    last_updated_at_ms: int
    source_count: int = Field(default=1, ge=1)
    independent_source_count: int = Field(default=1, ge=1)
    status: StoryStatus = "active"
    assessment: NewswireAssessment | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_event(self, *, update_type: StoryUpdateType = "created") -> "NewswireEvent":
        """Project a story revision onto the existing event/bus compatibility contract."""
        action: Action = "removed" if update_type == "retracted" else "created" if self.revision == 1 else "updated"
        assessment = self.assessment
        metadata = {
            **self.metadata,
            "story_id": self.story_id,
            "story_revision": self.revision,
            "story_update_type": update_type,
            "story_sources": list(self.sources),
            "story_member_event_ids": list(self.member_event_ids),
        }
        if assessment is not None:
            shadow_only = str(self.metadata.get("newswire_routing_mode") or "active") == "shadow"
            metadata["newswire_assessment"] = assessment.model_dump(mode="json")
            # Existing formatters/engine bridge understand this summary key.  It is now
            # a compatibility projection of the authoritative V2 assessment.
            metadata["newswire_policy_decision"] = {
                "decision_id": assessment.decision_id,
                "policy_version": assessment.assessment_version,
                "policy_type": "static",
                "shadow_only": shadow_only,
                "audience_scope": assessment.audience_scope,
                "newswire_action": assessment.feed_action,
                "engine_action": assessment.engine_action,
                "quality_score": round((assessment.source_quality_score + assessment.novelty_score) / 2.0, 4),
                "market_impact_score": assessment.impact_score,
                "relevance_score": assessment.relevance_score,
                "novelty_score": assessment.novelty_score,
                "urgency_score": assessment.urgency_score,
                "source_score": assessment.source_quality_score / 100.0,
                "confidence": self.confidence,
                "direction_score": assessment.risk_bias,
                "direction_confidence": assessment.direction_confidence,
                "risk_score": assessment.risk_severity,
                "reasons": list(assessment.reason_codes),
                "penalties": list(assessment.penalty_codes),
            }
        return NewswireEvent(
            event_id=f"nws_{self.story_id.removeprefix('nws_')}_r{self.revision}",
            schema_version=2,
            source=self.source,
            provider=self.provider,
            transport="rest",
            received_at_ms=self.last_updated_at_ms,
            published_at_ms=self.published_at_ms,
            updated_at_ms=self.last_updated_at_ms if self.revision > 1 else None,
            action=action,
            headline=self.headline,
            body=self.body,
            url=self.url,
            symbols=list(self.symbols),
            asset_class=self.asset_class,
            event_type=self.event_type,
            urgency=self.urgency,
            importance_score=assessment.priority_score if assessment is not None else 0.0,
            sentiment=self.sentiment,
            freshness=_story_freshness(self.last_updated_at_ms, self.published_at_ms),
            confidence=self.confidence,
            source_score=self.source_score,
            assessment=assessment,
            story_id=self.story_id,
            story_revision=self.revision,
            topics=list(self.topics),
            metadata=metadata,
        )


class NewswireStoryRevision(BaseModel):
    revision_id: str
    story_id: str
    revision: int = Field(ge=1)
    update_type: StoryUpdateType
    emitted_at_ms: int
    story: NewswireStory


class RawNewsItem(BaseModel):
    """A loosely-typed item emitted by an adapter, before normalization.

    Adapters fill in whatever they have; the service normalizes the rest. ``external_id``
    is the upstream-stable id used for dedupe and update/delete correlation.
    """

    source: str
    provider: str | None = None
    transport: Transport
    external_id: str | None = None
    action: Action = "created"
    headline: str = ""
    body: str = ""
    url: str | None = None
    author: str | None = None
    published_at_ms: int | None = None
    symbols: list[str] = Field(default_factory=list)
    asset_class: AssetClass | None = None
    event_type: EventType | None = None
    public_metrics: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)
    query: str | None = None


class Tradability(BaseModel):
    """Deterministic risk gate attached to every event.

    ``allow_auto_trade`` is hard-wired ``False`` everywhere: news only sets evidence and
    confirmation requirements, never auto-execution (consistent with the paper-only,
    human-signoff posture enforced in config).
    """

    allow_auto_trade: bool = False
    requires_confirmation: bool = True
    halt_state_checked: bool = False
    halted_symbols: list[str] = Field(default_factory=list)


class NewswireEvent(BaseModel):
    """The single canonical contract published on the bus and over HTTP/WS."""

    # identity
    event_id: str
    schema_version: int = 1
    # provenance
    source: str
    provider: str
    transport: Transport
    # timing (UTC ms)
    received_at_ms: int
    published_at_ms: int | None = None
    updated_at_ms: int | None = None
    # lifecycle
    action: Action = "created"
    # content
    headline: str
    body: str = ""
    url: str | None = None
    author: str | None = None
    # classification (deterministic-first)
    symbols: list[str] = Field(default_factory=list)
    asset_class: AssetClass = "unknown"
    event_type: EventType = "headline"
    urgency: Urgency = "normal"
    importance_score: float = Field(default=0.0, ge=0.0, le=100.0)
    sentiment: Sentiment = "unknown"
    freshness: Freshness = "fresh"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source_score: float = Field(default=0.0, ge=0.0, le=1.0)
    # risk gate
    tradability: Tradability = Field(default_factory=Tradability)
    # optional LLM second-pass
    enrichment: dict[str, Any] | None = None
    # raw + extras
    metadata: dict[str, Any] = Field(default_factory=dict)
    # V2 additive story/assessment contract. Old persisted rows validate with defaults.
    story_id: str | None = None
    story_revision: int | None = None
    topics: list[str] = Field(default_factory=list)
    assessment: NewswireAssessment | None = None

    def to_news_event(self) -> NewsEvent:
        """Bridge to the legacy ``NewsEvent`` consumed by the autonomy market map."""
        return NewsEvent(
            id=self.event_id,
            source=self.source,
            provider=self.provider,
            title=self.headline,
            text=self.body,
            url=self.url,
            author_id=self.author,
            created_at_ms=self.published_at_ms,
            observed_at_ms=self.received_at_ms,
            assets=list(self.symbols),
            importance_score=self.importance_score,
            sentiment=self.sentiment,
            freshness=self.freshness,
            metadata={**self.metadata, "event_type": self.event_type, "asset_class": self.asset_class, "urgency": self.urgency, "source_score": self.source_score},
        )


class NewswireFilter(BaseModel):
    """Subscription filter shared by internal consumers and the WS endpoint."""

    symbols: list[str] = Field(default_factory=list)
    asset_classes: list[AssetClass] = Field(default_factory=list)
    event_types: list[EventType] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    min_importance: float = 0.0
    urgency_at_least: Urgency | None = None

    def matches(self, event: NewswireEvent) -> bool:
        if self.min_importance and event.importance_score < self.min_importance:
            return False
        if self.urgency_at_least and URGENCY_RANK.get(event.urgency, 1) < URGENCY_RANK.get(self.urgency_at_least, 0):
            return False
        if self.asset_classes and event.asset_class not in self.asset_classes:
            return False
        if self.event_types and event.event_type not in self.event_types:
            return False
        if self.sources and event.source not in self.sources:
            return False
        if self.symbols:
            wanted = {symbol.upper() for symbol in self.symbols}
            if not wanted & {symbol.upper() for symbol in event.symbols}:
                return False
        return True


def _story_freshness(received_at_ms: int, published_at_ms: int | None) -> Freshness:
    if published_at_ms is None:
        return "fresh"
    age = max(0, received_at_ms - published_at_ms)
    if age <= 30 * 60 * 1000:
        return "breaking"
    if age <= 24 * 60 * 60 * 1000:
        return "fresh"
    return "stale"
