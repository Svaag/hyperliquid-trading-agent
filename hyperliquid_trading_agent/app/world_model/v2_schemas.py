from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

AdmissionStatus = Literal["admitted", "quarantined", "rejected"]
ImpactDirection = Literal["supportive", "adverse", "neutral", "unknown"]
ImpactHorizon = Literal["intraday", "swing", "regime"]
ImpactMode = Literal["current", "conditional"]
PredictionStatus = Literal["open", "closed", "settled", "stale"]


def _clean(values: list[str], *, upper: bool = False) -> list[str]:
    normalized = {(value.upper() if upper else value.lower()).strip() for value in values if value and value.strip()}
    return sorted(normalized)


class EvidenceV2(BaseModel):
    evidence_id: str
    source_type: str
    source: str
    provider: str
    title: str = ""
    body: str = ""
    url: str | None = None
    event_at_ms: int | None = None
    available_at_ms: int
    observed_at_ms: int
    admission_status: AdmissionStatus
    admission_reason_codes: list[str] = Field(default_factory=list)
    factor_ids: list[str] = Field(default_factory=list)
    instrument_ids: list[str] = Field(default_factory=list)
    quality_score: float = Field(default=1.0, ge=0.0, le=1.0)
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("factor_ids")
    @classmethod
    def _factors(cls, value: list[str]) -> list[str]:
        return _clean(value)

    @field_validator("instrument_ids")
    @classmethod
    def _instruments(cls, value: list[str]) -> list[str]:
        return _clean(value, upper=True)

    @model_validator(mode="after")
    def _timestamps(self) -> Self:
        if self.available_at_ms <= 0 or self.observed_at_ms <= 0:
            raise ValueError("availability and observation timestamps must be positive")
        if self.observed_at_ms < self.available_at_ms:
            raise ValueError("observed_at_ms cannot precede available_at_ms")
        return self


class MacroObservationV2(BaseModel):
    observation_id: str
    series_id: str
    factor_id: str
    geography: str = "US"
    period: str
    value: float
    units: str
    frequency: str
    vintage: str
    event_at_ms: int
    available_at_ms: int
    previous_value: float | None = None
    forecast_value: float | None = None
    surprise: float | None = None
    source: str
    evidence_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _no_lookahead(self) -> Self:
        if self.available_at_ms < self.event_at_ms:
            raise ValueError("available_at_ms cannot precede event_at_ms")
        if self.forecast_value is None and self.surprise is not None:
            raise ValueError("surprise must be null when forecast is absent")
        return self


class MacroFactorStateV2(BaseModel):
    factor_id: str
    semantic_axis: str
    as_of_ms: int
    level_score: float | None = Field(default=None, ge=-5.0, le=5.0)
    momentum_score: float | None = Field(default=None, ge=-5.0, le=5.0)
    surprise_score: float | None = Field(default=None, ge=-5.0, le=5.0)
    regime: str = "unknown"
    freshness_ms: int | None = Field(default=None, ge=0)
    coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    source_observation_ids: list[str] = Field(default_factory=list)
    quality_flags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PredictionMarketV2(BaseModel):
    market_key: str
    venue: str
    market_id: str
    question: str
    status: PredictionStatus = "open"
    accepting_orders: bool = True
    closes_at_ms: int | None = None
    liquidity_usd: float | None = Field(default=None, ge=0.0)
    volume_usd: float | None = Field(default=None, ge=0.0)
    factor_ids: list[str] = Field(default_factory=list)
    instrument_ids: list[str] = Field(default_factory=list)
    admission_status: AdmissionStatus = "quarantined"
    admission_reason_codes: list[str] = Field(default_factory=list)
    outcome_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PredictionQuoteV2(BaseModel):
    quote_key: str
    market_key: str
    venue: str
    market_id: str
    outcome_id: str
    outcome_name: str
    probability: float = Field(ge=0.0, le=1.0)
    best_bid: float | None = Field(default=None, ge=0.0, le=1.0)
    best_ask: float | None = Field(default=None, ge=0.0, le=1.0)
    spread: float | None = Field(default=None, ge=0.0, le=1.0)
    provider_at_ms: int
    observed_at_ms: int
    delta_5m: float | None = Field(default=None, ge=-1.0, le=1.0)
    delta_1h: float | None = Field(default=None, ge=-1.0, le=1.0)
    delta_24h: float | None = Field(default=None, ge=-1.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ForecastHypothesisV2(BaseModel):
    hypothesis_id: str
    market_key: str
    question: str
    as_of_ms: int
    yes_probability: float | None = Field(default=None, ge=0.0, le=1.0)
    outcome_probabilities: dict[str, float] = Field(default_factory=dict)
    factor_ids: list[str] = Field(default_factory=list)
    instrument_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _distribution(self) -> Self:
        if self.yes_probability is None and not self.outcome_probabilities:
            raise ValueError("a forecast requires a binary probability or outcome distribution")
        if self.yes_probability is not None and self.outcome_probabilities:
            raise ValueError("binary and multi-outcome probabilities are mutually exclusive")
        if self.outcome_probabilities:
            if any(value < 0 or value > 1 for value in self.outcome_probabilities.values()):
                raise ValueError("outcome probabilities must be between zero and one")
            if abs(sum(self.outcome_probabilities.values()) - 1.0) > 0.02:
                raise ValueError("outcome distribution must sum to one")
        return self


class AssetImpactV2(BaseModel):
    impact_id: str
    instrument_id: str
    factor_id: str
    horizon: ImpactHorizon
    direction: ImpactDirection
    mode: ImpactMode = "current"
    strength: float = Field(default=0.0, ge=0.0, le=1.0)
    as_of_ms: int
    rationale: str = ""
    condition: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    mapping_version: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _conditional_requires_condition(self) -> Self:
        if self.mode == "conditional" and not self.condition:
            raise ValueError("conditional impacts require a condition")
        return self


class WorldModelSnapshotV2(BaseModel):
    snapshot_id: str
    as_of_ms: int
    macro_states: list[MacroFactorStateV2] = Field(default_factory=list)
    asset_impacts: list[AssetImpactV2] = Field(default_factory=list)
    forecasts: list[ForecastHypothesisV2] = Field(default_factory=list)
    evidence: list[EvidenceV2] = Field(default_factory=list)
    quality_flags: list[str] = Field(default_factory=list)
    coverage: dict[str, float] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=lambda: {"execution_authority": "none", "shadow_only": True})


class SupervisionV2(BaseModel):
    supervision_id: str
    target_type: str
    target_id: str
    action: str
    note: str = ""
    actor_id: str | None = None
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)
