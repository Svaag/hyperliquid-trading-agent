from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from hyperliquid_trading_agent.app.autonomy.schemas import Freshness, NewsEvent, Sentiment

# --- canonical enums ---------------------------------------------------------

Transport = Literal["websocket", "rss", "rest", "poll"]
Action = Literal["created", "updated", "removed"]
AssetClass = Literal["equity", "crypto", "macro", "fx", "commodity", "unknown"]
Urgency = Literal["breaking", "normal", "background"]
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
