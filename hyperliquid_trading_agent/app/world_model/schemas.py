from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

WorldEventSourceType = Literal[
    "newswire",
    "social",
    "prediction_market",
    "market_data",
    "event_evaluation",
    "engine",
    "operator",
    "unknown",
]
BeliefKind = Literal["fact", "probability", "narrative", "risk", "catalyst", "source_reliability", "memory", "contradiction"]
BeliefStatus = Literal["active", "superseded", "expired", "contradicted"]
BeliefDirection = Literal["bullish", "bearish", "mixed", "neutral", "unknown"]
PredictionMarketStatus = Literal["open", "closed", "settled", "stale", "unknown"]
WorldMemoryType = Literal["working", "fact", "episodic", "source_reliability", "narrative", "role_lesson", "prediction_market"]
WorldAnnotationTargetType = Literal["event", "belief", "prediction_signal", "memory", "source", "narrative"]
WorldAnnotationAction = Literal["confirmed", "disputed", "needs_review", "pinned"]
WorldOutcomeTargetType = Literal["event", "belief", "prediction_signal", "source"]


class WorldEvent(BaseModel):
    """Canonical evidence item known to the market world model."""

    event_id: str
    source_type: WorldEventSourceType = "unknown"
    source: str = "unknown"
    provider: str = "unknown"
    event_type: str = "unknown"
    asset_class: str = "unknown"
    symbols: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    title: str = ""
    body: str = ""
    url: str | None = None
    event_ts_ms: int | None = None
    received_ts_ms: int
    computed_ts_ms: int
    importance_score: float = Field(default=0.0, ge=0.0, le=100.0)
    sentiment: BeliefDirection = "unknown"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source_score: float = Field(default=0.0, ge=0.0, le=1.0)
    quality_score: float = Field(default=1.0, ge=0.0, le=1.0)
    staleness_ms: int | None = Field(default=None, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, value: list[str]) -> list[str]:
        return sorted({item.upper().strip() for item in value if item and item.strip()})

    @field_validator("topics")
    @classmethod
    def _topics(cls, value: list[str]) -> list[str]:
        return sorted({item.lower().strip() for item in value if item and item.strip()})

    @model_validator(mode="after")
    def _validate_times(self) -> Self:
        if self.received_ts_ms <= 0 or self.computed_ts_ms <= 0:
            raise ValueError("received_ts_ms and computed_ts_ms must be positive")
        if self.computed_ts_ms < self.received_ts_ms:
            raise ValueError("computed_ts_ms must be >= received_ts_ms")
        return self


class MarketBelief(BaseModel):
    """A scoped, evidence-backed belief used as advisory context."""

    belief_id: str
    kind: BeliefKind
    subject: str
    statement: str
    symbols: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    direction: BeliefDirection = "unknown"
    probability: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    salience: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_event_ids: list[str] = Field(default_factory=list)
    contradicts_belief_ids: list[str] = Field(default_factory=list)
    status: BeliefStatus = "active"
    created_at_ms: int
    updated_at_ms: int
    expires_at_ms: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, value: list[str]) -> list[str]:
        return sorted({item.upper().strip() for item in value if item and item.strip()})

    @field_validator("topics")
    @classmethod
    def _topics(cls, value: list[str]) -> list[str]:
        return sorted({item.lower().strip() for item in value if item and item.strip()})


class NarrativeCluster(BaseModel):
    cluster_id: str
    title: str
    summary: str
    symbols: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    belief_ids: list[str] = Field(default_factory=list)
    event_ids: list[str] = Field(default_factory=list)
    pressure_score: float = Field(default=0.0, ge=-1.0, le=1.0)
    consensus_score: float = Field(default=0.0, ge=0.0, le=1.0)
    conflict_score: float = Field(default=0.0, ge=0.0, le=1.0)
    created_at_ms: int
    updated_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class PredictionMarketSignal(BaseModel):
    signal_id: str
    venue: str
    market_id: str
    question: str
    outcome_id: str | None = None
    outcome_name: str = ""
    symbols: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    implied_probability: float | None = Field(default=None, ge=0.0, le=1.0)
    probability_delta: float | None = Field(default=None, ge=-1.0, le=1.0)
    best_bid: float | None = Field(default=None, ge=0.0, le=1.0)
    best_ask: float | None = Field(default=None, ge=0.0, le=1.0)
    liquidity_usd: float | None = Field(default=None, ge=0.0)
    volume_usd: float | None = Field(default=None, ge=0.0)
    status: PredictionMarketStatus = "unknown"
    source_event_ids: list[str] = Field(default_factory=list)
    as_of_ms: int
    staleness_ms: int | None = Field(default=None, ge=0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, value: list[str]) -> list[str]:
        return sorted({item.upper().strip() for item in value if item and item.strip()})

    @field_validator("topics")
    @classmethod
    def _topics(cls, value: list[str]) -> list[str]:
        return sorted({item.lower().strip() for item in value if item and item.strip()})


class SourceCredibility(BaseModel):
    source_key: str
    source: str
    provider: str = "unknown"
    score: float = Field(default=0.5, ge=0.0, le=1.0)
    observations: int = Field(default=0, ge=0)
    confirmations: int = Field(default=0, ge=0)
    contradictions: int = Field(default=0, ge=0)
    last_updated_at_ms: int
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorldMemoryAtom(BaseModel):
    memory_id: str
    memory_type: WorldMemoryType
    subject: str
    content: str
    symbols: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    source_event_ids: list[str] = Field(default_factory=list)
    source_belief_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    salience: float = Field(default=0.0, ge=0.0, le=1.0)
    created_at_ms: int
    last_reinforced_at_ms: int | None = None
    expires_at_ms: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorldModelSnapshot(BaseModel):
    snapshot_id: str
    as_of_ms: int
    symbols: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    top_beliefs: list[MarketBelief] = Field(default_factory=list)
    narrative_clusters: list[NarrativeCluster] = Field(default_factory=list)
    prediction_market_signals: list[PredictionMarketSignal] = Field(default_factory=list)
    source_credibility: list[SourceCredibility] = Field(default_factory=list)
    memory_atoms: list[WorldMemoryAtom] = Field(default_factory=list)
    quality_flags: list[str] = Field(default_factory=list)
    summary: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorldModelAnnotation(BaseModel):
    """Append-only operator supervision mark. It is audit context only."""

    annotation_id: str
    target_type: WorldAnnotationTargetType
    target_id: str
    action: WorldAnnotationAction
    note: str = ""
    actor_id: str | None = None
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorldModelOutcome(BaseModel):
    """Realized outcome used to calibrate beliefs, sources, and prediction priors."""

    outcome_id: str
    target_type: WorldOutcomeTargetType
    target_id: str
    outcome: str
    symbol: str | None = None
    horizon: str | None = None
    realized_value: float | None = None
    confidence_delta: float = Field(default=0.05, ge=0.0, le=0.5)
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class PredictionMarketCalibration(BaseModel):
    calibration_id: str
    signal_id: str
    venue: str
    market_id: str
    implied_probability: float | None = Field(default=None, ge=0.0, le=1.0)
    realized_outcome: float | None = Field(default=None, ge=0.0, le=1.0)
    brier_score: float | None = Field(default=None, ge=0.0, le=1.0)
    settled_at_ms: int | None = None
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)
