from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from hyperliquid_trading_agent.app.markets.schemas import stable_instrument_id

AssetClass = Literal["crypto", "equity", "macro", "fx", "commodity", "unknown"]
ExecutionMode = Literal["paper", "shadow"]
EngineSide = Literal["long", "short", "flat"]
OrderSide = Literal["buy", "sell"]
TrendState = Literal["bull", "bear", "range", "transition", "unknown"]
LiquidityState = Literal["deep", "normal", "thin", "impaired", "unknown"]
SpreadState = Literal["tight", "normal", "wide", "unknown"]
CandidateStatus = Literal[
    "new",
    "scored",
    "allocated",
    "risk_rejected",
    "debate_required",
    "debate_approved",
    "debate_downgraded",
    "debate_blocked",
    "approved_for_paper",
    "approved_for_shadow",
    "throttled",
    "expired",
    "cancelled",
]
DebateOutcome = Literal["approve", "downgrade", "block", "require_more_data"]
AllocationDecisionStatus = Literal["allocate", "skip", "reduce", "require_debate", "risk_rejected"]
CouncilVoteDecision = Literal["allow", "warn", "veto", "needs_more_evidence"]
CouncilReviewDecision = Literal["allow_shadow", "allow_paper", "reject", "needs_more_evidence"]
OrderType = Literal["marketable_limit", "post_only", "twap", "vwap", "pov"]
ExecutionStatus = Literal["accepted", "rejected", "filled", "partial", "cancelled", "expired"]
PositionThesisState = Literal[
    "proposed",
    "approved",
    "opening",
    "open",
    "scaling_in",
    "partial_take_profit",
    "de_risking",
    "trailing",
    "time_stop_pending",
    "exit_pending",
    "closed",
    "under_review",
]
ModelVersionStatus = Literal["candidate", "shadow", "approved", "deprecated"]
ReplayMode = Literal["signal", "decision", "execution"]
ReplayStatus = Literal["passed", "advisory_pass", "failed", "insufficient_data", "audit_only"]
KillSwitchScope = Literal["strategy", "asset", "venue", "account", "global", "human_emergency", "deadman"]
KillSwitchAction = Literal["armed", "triggered", "released", "expired"]
OutcomeWindow = Literal["5m", "15m", "1h", "4h", "24h"]


def _instrument_identity(
    *,
    asset: str,
    venue_id: str,
    provider_symbol: str,
    instrument_id: str,
    underlying_id: str,
    asset_class: str,
) -> tuple[str, str, str, str]:
    provider = (provider_symbol or asset).strip()
    if provider.upper().startswith("XYZ:"):
        provider = "xyz:" + provider.split(":", 1)[1].upper()
    venue = venue_id.strip().lower()
    if venue in {"hyperliquid", "hyperliquid:main", ""}:
        venue = "hyperliquid:xyz" if provider.lower().startswith("xyz:") else "hyperliquid:main"
    elif venue == "alpaca":
        venue = "alpaca:paper"
    display = provider.split(":", 1)[-1].upper()
    prefix = {
        "crypto": "CRYPTO",
        "equity": "EQUITY",
        "macro": "INDEX",
        "fx": "FX",
        "commodity": "COMMODITY",
    }.get(asset_class, "UNKNOWN")
    resolved_underlying = underlying_id.strip() or f"{prefix}:{display}"
    resolved_instrument = instrument_id.strip() or stable_instrument_id(venue, provider)
    return resolved_instrument, resolved_underlying, venue, provider


class NormalizedEvent(BaseModel):
    """Append-only normalized input event used by the institutional engine.

    ``received_ts_ms`` is mandatory so replay can prove what was known at decision
    time. ``event_ts_ms`` may be absent when an upstream source lacks a source
    timestamp; in that case staleness must be derived conservatively downstream.
    """

    event_id: str
    schema_version: int = 1
    event_type: str
    asset_class: AssetClass = "unknown"
    symbols: list[str] = Field(default_factory=list)
    source: str
    provider: str
    event_ts_ms: int | None = None
    received_ts_ms: int
    computed_ts_ms: int
    payload: dict[str, Any] = Field(default_factory=dict)
    quality_score: float = Field(default=1.0, ge=0.0, le=1.0)
    staleness_ms: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbols")
    @classmethod
    def _uppercase_symbols(cls, value: list[str]) -> list[str]:
        return sorted({item.upper().strip() for item in value if item and item.strip()})

    @model_validator(mode="after")
    def _validate_timestamps(self) -> Self:
        if self.received_ts_ms <= 0 or self.computed_ts_ms <= 0:
            raise ValueError("received_ts_ms and computed_ts_ms must be positive")
        if self.computed_ts_ms < self.received_ts_ms:
            raise ValueError("computed_ts_ms must be >= received_ts_ms")
        if self.event_ts_ms is not None and self.event_ts_ms <= 0:
            raise ValueError("event_ts_ms must be positive when supplied")
        return self


class FeatureValue(BaseModel):
    """Point-in-time feature value with event, receipt, and compute timestamps."""

    feature_id: str
    asset: str
    instrument_id: str = ""
    underlying_id: str = ""
    venue_id: str = "hyperliquid:main"
    provider_symbol: str = ""
    feature_group: str
    feature_name: str
    value: dict[str, Any] = Field(default_factory=dict)
    scalar_value: float | None = None
    event_ts_ms: int | None = None
    received_ts_ms: int
    computed_ts_ms: int
    source_event_id: str | None = None
    source: str
    version: str
    quality_score: float = Field(default=1.0, ge=0.0, le=1.0)
    staleness_ms: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("asset")
    @classmethod
    def _uppercase_asset(cls, value: str) -> str:
        value = value.upper().strip()
        if not value:
            raise ValueError("asset is required")
        return value

    @model_validator(mode="after")
    def _validate_timestamps(self) -> Self:
        if self.computed_ts_ms < self.received_ts_ms:
            raise ValueError("computed_ts_ms must be >= received_ts_ms")
        self.instrument_id, self.underlying_id, self.venue_id, self.provider_symbol = _instrument_identity(
            asset=self.asset,
            venue_id=self.venue_id,
            provider_symbol=self.provider_symbol,
            instrument_id=self.instrument_id,
            underlying_id=self.underlying_id,
            asset_class="crypto",
        )
        return self


class FeatureSnapshot(BaseModel):
    snapshot_id: str
    asset: str
    instrument_id: str = ""
    underlying_id: str = ""
    venue_id: str = "hyperliquid:main"
    provider_symbol: str = ""
    as_of_ms: int
    feature_ids: list[str] = Field(default_factory=list)
    features: dict[str, Any] = Field(default_factory=dict)
    regime_snapshot_id: str | None = None
    quality_flags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("asset")
    @classmethod
    def _uppercase_asset(cls, value: str) -> str:
        return value.upper().strip()

    @model_validator(mode="after")
    def _derive_instrument_identity(self) -> Self:
        self.instrument_id, self.underlying_id, self.venue_id, self.provider_symbol = _instrument_identity(
            asset=self.asset,
            venue_id=self.venue_id,
            provider_symbol=self.provider_symbol,
            instrument_id=self.instrument_id,
            underlying_id=self.underlying_id,
            asset_class="crypto",
        )
        return self


class StrategyPermissions(BaseModel):
    momentum_allowed: bool = False
    mean_reversion_allowed: bool = False
    market_making_allowed: bool = False
    news_event_allowed: bool = False
    carry_allowed: bool = False
    relative_value_allowed: bool = False
    reason_codes: list[str] = Field(default_factory=list)


class StrategySpec(BaseModel):
    """Declarative contract for an institutional alpha strategy.

    The registry uses this metadata for breadth accounting, allocation caps,
    readiness evidence, and deterministic replay.  Defaults are intentionally
    conservative so legacy candidates can be adapted without silently counting
    as independent alpha breadth.
    """

    strategy_id: str
    version: str
    family: str
    supported_assets: list[str] = Field(default_factory=list)
    supported_venues: list[str] = Field(default_factory=list)
    supported_horizons: list[str] = Field(default_factory=list)
    required_features: list[str] = Field(default_factory=list)
    valid_regimes: list[str] = Field(default_factory=list)
    max_candidates_per_run: int = Field(default=1, ge=0)
    max_allocation_share_pct: float = Field(default=45.0, ge=0.0, le=100.0)
    cooldown_ms: int = Field(default=0, ge=0)
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    min_ev_bps: float = 0.0
    risk_tags: list[str] = Field(default_factory=list)
    counts_for_breadth: bool = True
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("strategy_id", "version", "family")
    @classmethod
    def _required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("strategy_id, version, and family are required")
        return value

    @field_validator("supported_assets")
    @classmethod
    def _uppercase_supported_assets(cls, value: list[str]) -> list[str]:
        return sorted({item.upper().strip() for item in value if item and item.strip()})

    @field_validator("supported_venues", "supported_horizons", "required_features", "valid_regimes", "risk_tags")
    @classmethod
    def _dedupe_text_lists(cls, value: list[str]) -> list[str]:
        return sorted({item.strip() for item in value if item and item.strip()})


class RegimeVector(BaseModel):
    """Probabilistic regime snapshot replacing the legacy single risk-regime label."""

    regime_snapshot_id: str
    primary_asset: str = "GLOBAL"
    created_at_ms: int
    as_of_ms: int
    trend_state: TrendState = "unknown"
    trend_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    realized_vol_percentile: float | None = Field(default=None, ge=0.0, le=1.0)
    implied_vol_percentile: float | None = Field(default=None, ge=0.0, le=1.0)
    liquidity_state: LiquidityState = "unknown"
    spread_state: SpreadState = "unknown"
    volatility_state: str = "unknown"
    funding_state: str = "unknown"
    oi_state: str = "unknown"
    liquidation_state: str = "unknown"
    orderflow_state: str = "unknown"
    news_state: str = "no_event"
    correlation_state: str = "unknown"
    session_state: str = "unknown"
    feature_coverage_pct: float = Field(default=0.0, ge=0.0, le=100.0)
    regime_label: str = "unknown"
    funding_stress_z: float | None = None
    open_interest_velocity_z: float | None = None
    liquidation_imbalance_z: float | None = None
    dominance_pressure_z: float | None = None
    cross_asset_risk_on_z: float | None = None
    stablecoin_liquidity_z: float | None = None
    correlation_breakdown_prob: float = Field(default=0.0, ge=0.0, le=1.0)
    news_catalyst_pressure: float = Field(default=0.0, ge=0.0, le=1.0)
    news_directional_pressure: float = Field(default=0.0, ge=-1.0, le=1.0)
    news_risk_pressure: float = Field(default=0.0, ge=0.0, le=1.0)
    news_risk_mode: Literal["neutral", "risk_on", "risk_off", "shock"] = "neutral"
    regime_stability_score: float = Field(default=0.0, ge=0.0, le=1.0)
    permissions: StrategyPermissions = Field(default_factory=StrategyPermissions)
    feature_refs: list[str] = Field(default_factory=list)
    raw_feature_refs: dict[str, str] = Field(default_factory=dict)
    derived_labels: dict[str, str] = Field(default_factory=dict)
    quality_flags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("primary_asset")
    @classmethod
    def _uppercase_primary_asset(cls, value: str) -> str:
        return value.upper().strip() or "GLOBAL"

    @model_validator(mode="after")
    def _validate_times(self) -> Self:
        if self.created_at_ms <= 0 or self.as_of_ms <= 0:
            raise ValueError("created_at_ms and as_of_ms must be positive")
        return self


class AlphaCandidate(BaseModel):
    candidate_id: str
    strategy_id: str
    strategy_version: str = "unknown"
    strategy_family: str = "unknown"
    valid_regimes: list[str] = Field(default_factory=list)
    required_features: list[str] = Field(default_factory=list)
    feature_coverage_pct: float = Field(default=0.0, ge=0.0, le=100.0)
    expected_edge_bps: float = 0.0
    risk_tags: list[str] = Field(default_factory=list)
    counts_for_breadth: bool = True
    portfolio_concentration_impact: dict[str, Any] = Field(default_factory=dict)
    source_integrity: dict[str, Any] = Field(default_factory=dict)
    asset: str
    asset_class: AssetClass = "crypto"
    venue: str
    instrument_id: str = ""
    underlying_id: str = ""
    venue_id: str = ""
    provider_symbol: str = ""
    evidence_epoch_id: str = "legacy"
    side: EngineSide
    horizon: str
    proposed_entry: float = Field(gt=0)
    stop: float = Field(gt=0)
    targets: list[float] = Field(default_factory=list)
    thesis: str
    invalidation_conditions: list[str] = Field(default_factory=list)
    feature_snapshot_id: str
    regime_snapshot_id: str
    source_event_ids: list[str] = Field(default_factory=list)
    raw_alpha_score: float = Field(ge=0.0, le=100.0)
    confidence: float = Field(ge=0.0, le=1.0)
    status: CandidateStatus = "new"
    created_at_ms: int
    expires_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("asset")
    @classmethod
    def _uppercase_asset(cls, value: str) -> str:
        return value.upper().strip()

    @field_validator("targets")
    @classmethod
    def _positive_targets(cls, value: list[float]) -> list[float]:
        if any(item <= 0 for item in value):
            raise ValueError("targets must be positive")
        return value

    @model_validator(mode="after")
    def _validate_lifecycle(self) -> Self:
        if self.expires_at_ms <= self.created_at_ms:
            raise ValueError("expires_at_ms must be > created_at_ms")
        if self.side != "flat" and not self.invalidation_conditions:
            raise ValueError("directional candidates require invalidation_conditions")
        self.instrument_id, self.underlying_id, self.venue_id, self.provider_symbol = _instrument_identity(
            asset=self.asset,
            venue_id=self.venue_id or self.venue,
            provider_symbol=self.provider_symbol,
            instrument_id=self.instrument_id,
            underlying_id=self.underlying_id,
            asset_class=self.asset_class,
        )
        return self


class CandidateBookSnapshot(BaseModel):
    candidate_book_id: str
    created_at_ms: int
    as_of_ms: int
    candidate_ids: list[str] = Field(default_factory=list)
    ranked_candidate_ids: list[str] = Field(default_factory=list)
    rejected_candidate_ids: list[str] = Field(default_factory=list)
    portfolio_state_ref: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EVEstimate(BaseModel):
    estimate_id: str
    candidate_id: str
    model_version_id: str
    p_target: float = Field(ge=0.0, le=1.0)
    p_stop: float = Field(ge=0.0, le=1.0)
    p_timeout: float = Field(ge=0.0, le=1.0)
    expected_favorable_bps: float
    expected_adverse_bps: float
    expected_holding_ms: int = Field(ge=0)
    expected_fee_bps: float = Field(ge=0.0)
    expected_spread_cost_bps: float = Field(ge=0.0)
    expected_slippage_bps: float = Field(ge=0.0)
    expected_market_impact_bps: float = Field(ge=0.0)
    expected_funding_cost_bps: float
    tail_loss_bps: float = Field(ge=0.0)
    net_ev_bps: float
    risk_adjusted_utility: float
    uncertainty: float = Field(ge=0.0, le=1.0)
    calibration_bucket: str
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_probabilities(self) -> Self:
        total = self.p_target + self.p_stop + self.p_timeout
        if not 0.95 <= total <= 1.05:
            raise ValueError("p_target + p_stop + p_timeout must be approximately 1")
        return self


class AllocationDecision(BaseModel):
    allocation_id: str
    candidate_id: str
    candidate_book_id: str | None = None
    status: AllocationDecisionStatus
    allocated_size: float = Field(default=0.0, ge=0.0)
    allocated_notional_usd: float = Field(default=0.0, ge=0.0)
    risk_usd: float = Field(default=0.0, ge=0.0)
    max_size_multiplier: float = Field(default=1.0, ge=0.0, le=1.0)
    opportunity_cost_rank: int | None = Field(default=None, ge=1)
    constraints: dict[str, Any] = Field(default_factory=dict)
    reason_codes: list[str] = Field(default_factory=list)
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_skip_size(self) -> Self:
        if self.status in {"skip", "risk_rejected"} and (self.allocated_size or self.allocated_notional_usd or self.risk_usd):
            raise ValueError("skipped/risk-rejected allocations cannot carry size or risk")
        return self


class CandidateEvidenceLink(BaseModel):
    link_id: str
    candidate_id: str
    strategy_id: str
    strategy_version: str = "unknown"
    strategy_family: str = "unknown"
    asset: str
    venue: str = "hyperliquid"
    instrument_id: str = ""
    underlying_id: str = ""
    venue_id: str = ""
    horizon: str
    regime_snapshot_id: str
    feature_snapshot_id: str
    risk_decision_id: str | None = None
    council_review_id: str | None = None
    replay_context_id: str | None = None
    allocation_id: str | None = None
    packet_id: str | None = None
    outcome_window_ids: list[str] = Field(default_factory=list)
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("asset")
    @classmethod
    def _uppercase_asset(cls, value: str) -> str:
        return value.upper().strip()

    @model_validator(mode="after")
    def _derive_instrument_identity(self) -> Self:
        self.instrument_id, self.underlying_id, self.venue_id, _ = _instrument_identity(
            asset=self.asset,
            venue_id=self.venue_id or self.venue,
            provider_symbol="",
            instrument_id=self.instrument_id,
            underlying_id=self.underlying_id,
            asset_class="crypto",
        )
        return self


class CandidateOutcomeAttribution(BaseModel):
    attribution_id: str
    candidate_id: str
    strategy_id: str
    strategy_version: str = "unknown"
    strategy_family: str = "unknown"
    asset: str
    venue: str = "hyperliquid"
    instrument_id: str = ""
    underlying_id: str = ""
    venue_id: str = ""
    side: EngineSide
    candidate_horizon: str
    regime_snapshot_id: str
    feature_snapshot_id: str
    risk_decision_id: str | None = None
    council_review_id: str | None = None
    replay_context_id: str | None = None
    allocation_id: str | None = None
    outcome_window: OutcomeWindow
    window_start_ms: int
    window_end_ms: int
    entry_px: float = Field(gt=0)
    mark_px: float | None = Field(default=None, gt=0)
    gross_return_bps: float = 0.0
    fees_bps: float = 0.0
    slippage_bps: float = 0.0
    funding_bps: float = 0.0
    net_return_bps: float = 0.0
    realized_r: float = 0.0
    mfe_bps: float = 0.0
    mae_bps: float = 0.0
    risk_decision: str = "unknown"
    council_decision: str = "unknown"
    allocation_status: str = "unknown"
    terminal_state: str = "pending"
    quality_flags: list[str] = Field(default_factory=list)
    created_at_ms: int
    updated_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("asset")
    @classmethod
    def _uppercase_asset(cls, value: str) -> str:
        return value.upper().strip()

    @model_validator(mode="after")
    def _validate_window(self) -> Self:
        if self.window_end_ms <= self.window_start_ms:
            raise ValueError("outcome window end must be after start")
        self.instrument_id, self.underlying_id, self.venue_id, _ = _instrument_identity(
            asset=self.asset,
            venue_id=self.venue_id or self.venue,
            provider_symbol="",
            instrument_id=self.instrument_id,
            underlying_id=self.underlying_id,
            asset_class="crypto",
        )
        return self


class ReplayResultLink(BaseModel):
    link_id: str
    replay_id: str
    candidate_id: str | None = None
    strategy_id: str = "unknown"
    strategy_version: str = "unknown"
    strategy_family: str = "unknown"
    asset: str = "GLOBAL"
    venue: str = "unknown"
    regime_snapshot_id: str | None = None
    horizon: str = "unknown"
    outcome_window: str = "unknown"
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("asset")
    @classmethod
    def _uppercase_asset(cls, value: str) -> str:
        return value.upper().strip() or "GLOBAL"


class PortfolioConcentrationEvent(BaseModel):
    event_id: str
    candidate_id: str
    allocation_id: str | None = None
    strategy_id: str
    strategy_version: str = "unknown"
    strategy_family: str = "unknown"
    asset: str
    venue: str = "hyperliquid"
    decision: str
    reason_codes: list[str] = Field(default_factory=list)
    strategy_share_pct: float = 0.0
    family_share_pct: float = 0.0
    symbol_strategy_share_pct: float = 0.0
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("asset")
    @classmethod
    def _uppercase_asset(cls, value: str) -> str:
        return value.upper().strip()


class StrategyRegimePerformance(BaseModel):
    performance_id: str
    strategy_id: str
    strategy_version: str = "unknown"
    strategy_family: str = "unknown"
    regime_label: str
    asset: str = "GLOBAL"
    venue: str = "unknown"
    outcome_window: str = "unknown"
    window_start_ms: int
    window_end_ms: int
    candidate_count: int = Field(default=0, ge=0)
    allocation_count: int = Field(default=0, ge=0)
    risk_reject_count: int = Field(default=0, ge=0)
    council_veto_count: int = Field(default=0, ge=0)
    concentration_event_count: int = Field(default=0, ge=0)
    win_rate_pct: float = 0.0
    avg_net_ev_bps: float = 0.0
    avg_net_return_bps: float = 0.0
    avg_realized_r: float = 0.0
    avg_drawdown_bps: float = 0.0
    avg_fees_bps: float = 0.0
    avg_slippage_bps: float = 0.0
    realized_pnl_usd: float = 0.0
    score: float = 0.0
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class BanditPolicySnapshot(BaseModel):
    policy_id: str
    policy_version: str
    status: str = "report_only"
    trained_window_start_ms: int
    trained_window_end_ms: int
    context_features: list[str] = Field(default_factory=list)
    arms: list[str] = Field(default_factory=list)
    policy_json: dict[str, Any] = Field(default_factory=dict)
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class BanditRecommendation(BaseModel):
    recommendation_id: str
    policy_id: str
    strategy_id: str
    asset: str = "GLOBAL"
    regime_label: str = "unknown"
    recommendation: str
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    expected_score_delta: float = 0.0
    auto_apply_allowed: bool = False
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _report_only_never_auto_applies(self) -> Self:
        if self.auto_apply_allowed:
            raise ValueError("bandit recommendations are report-only; auto_apply_allowed must be false")
        return self


class CandidateTradePacket(BaseModel):
    packet_id: str
    candidate_id: str
    strategy_id: str
    strategy_version: str = "unknown"
    strategy_family: str = "unknown"
    asset: str
    side: EngineSide
    horizon: str
    feature_snapshot_id: str
    regime_snapshot_id: str
    candidate: dict[str, Any] = Field(default_factory=dict)
    ev_estimate: dict[str, Any] = Field(default_factory=dict)
    allocation: dict[str, Any] = Field(default_factory=dict)
    order_intent: dict[str, Any] | None = None
    risk_decision: dict[str, Any] = Field(default_factory=dict)
    replay_context: dict[str, Any] = Field(default_factory=dict)
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("asset")
    @classmethod
    def _uppercase_asset(cls, value: str) -> str:
        return value.upper().strip()


class CouncilVote(BaseModel):
    vote_id: str
    review_id: str
    role: str
    decision: CouncilVoteDecision
    rationale: str
    vetoes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    scores: dict[str, float] = Field(default_factory=dict)
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class CouncilReview(BaseModel):
    review_id: str
    packet_id: str
    candidate_id: str
    strategy_id: str
    decision: CouncilReviewDecision
    vetoes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    regime_fit_score: float = Field(default=0.0, ge=0.0, le=1.0)
    strategy_regime_score: float = Field(default=0.0, ge=0.0, le=1.0)
    portfolio_impact_score: float = Field(default=0.0, ge=0.0, le=1.0)
    votes: list[CouncilVote] = Field(default_factory=list)
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_reject_has_reason(self) -> Self:
        if self.decision == "reject" and not self.vetoes:
            raise ValueError("rejected council reviews require at least one veto")
        return self


class EvidencePack(BaseModel):
    evidence_pack_id: str
    candidate_id: str
    strategy_id: str
    asset: str
    side: str
    horizon: str
    feature_snapshot_id: str
    market_regime_snapshot: dict[str, Any] = Field(default_factory=dict)
    orderflow_summary: dict[str, Any] = Field(default_factory=dict)
    news_summary: dict[str, Any] = Field(default_factory=dict)
    risk_summary: dict[str, Any] = Field(default_factory=dict)
    historical_analogs: list[dict[str, Any]] = Field(default_factory=list)
    model_outputs: dict[str, Any] = Field(default_factory=dict)
    known_missing_data: list[str] = Field(default_factory=list)
    data_quality_flags: list[str] = Field(default_factory=list)
    proposed_trade_plan: dict[str, Any] = Field(default_factory=dict)
    invalidation_conditions: list[str] = Field(default_factory=list)
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("asset")
    @classmethod
    def _uppercase_asset(cls, value: str) -> str:
        return value.upper().strip()

    @model_validator(mode="after")
    def _forbid_exchange_actions(self) -> Self:
        actions = self.proposed_trade_plan.get("exchange_actions")
        if actions:
            raise ValueError("EvidencePack proposed_trade_plan cannot contain exchange_actions")
        return self


class DebateDecision(BaseModel):
    debate_decision_id: str
    evidence_pack_id: str
    candidate_id: str
    decision: DebateOutcome
    confidence_adjustment: float = Field(ge=-1.0, le=1.0)
    max_size_multiplier: float = Field(ge=0.0, le=1.0)
    reason_codes: list[str] = Field(default_factory=list)
    required_invalidation_checks: list[str] = Field(default_factory=list)
    audit_summary: str
    role_outputs: list[dict[str, Any]] = Field(default_factory=list)
    judge_model: str | None = None
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_block_size(self) -> Self:
        if self.decision == "block" and self.max_size_multiplier != 0:
            raise ValueError("blocked debate decisions must have max_size_multiplier=0")
        return self


class OrderIntent(BaseModel):
    intent_id: str
    parent_candidate_id: str
    portfolio_decision_id: str
    asset: str
    asset_class: AssetClass = "crypto"
    venue: str
    instrument_id: str = ""
    underlying_id: str = ""
    venue_id: str = ""
    provider_symbol: str = ""
    side: OrderSide
    order_type: OrderType
    time_in_force: str
    target_size: float = Field(gt=0)
    target_notional_usd: float = Field(gt=0)
    max_slippage_bps: float = Field(ge=0.0)
    price_limit: float | None = Field(default=None, gt=0)
    reduce_only: bool = False
    post_only: bool = False
    deadline_ts_ms: int
    strategy_id: str
    model_version_id: str
    config_version_id: str
    risk_budget_id: str
    execution_mode: ExecutionMode
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("asset")
    @classmethod
    def _uppercase_asset(cls, value: str) -> str:
        return value.upper().strip()

    @model_validator(mode="after")
    def _validate_execution_contract(self) -> Self:
        if self.deadline_ts_ms <= self.created_at_ms:
            raise ValueError("deadline_ts_ms must be > created_at_ms")
        if self.order_type == "post_only" and not self.post_only:
            raise ValueError("post_only order_type requires post_only=True")
        if self.execution_mode not in {"paper", "shadow"}:
            raise ValueError("only paper/shadow execution modes are supported")
        self.instrument_id, self.underlying_id, self.venue_id, self.provider_symbol = _instrument_identity(
            asset=self.asset,
            venue_id=self.venue_id or self.venue,
            provider_symbol=self.provider_symbol,
            instrument_id=self.instrument_id,
            underlying_id=self.underlying_id,
            asset_class=self.asset_class,
        )
        return self


class ExecutionReport(BaseModel):
    report_id: str
    intent_id: str
    execution_mode: ExecutionMode
    status: ExecutionStatus
    requested_size: float = Field(gt=0)
    filled_size: float = Field(default=0.0, ge=0.0)
    avg_fill_px: float | None = Field(default=None, gt=0)
    fees_usd: float = Field(default=0.0, ge=0.0)
    slippage_bps: float = Field(default=0.0, ge=0.0)
    market_impact_bps: float | None = Field(default=None, ge=0.0)
    adapter: Literal["paper", "shadow"]
    assumptions: dict[str, Any] = Field(default_factory=dict)
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_report(self) -> Self:
        if self.filled_size > self.requested_size:
            raise ValueError("filled_size cannot exceed requested_size")
        if self.adapter != self.execution_mode:
            raise ValueError("adapter must match execution_mode")
        if self.status in {"filled", "partial"} and self.avg_fill_px is None:
            raise ValueError("filled/partial reports require avg_fill_px")
        return self


class PositionThesis(BaseModel):
    position_id: str
    entry_candidate_id: str
    strategy_id: str
    asset: str
    asset_class: AssetClass = "crypto"
    venue: str
    side: Literal["long", "short"]
    entry_reason: str
    expected_horizon: str
    stop: float = Field(gt=0)
    targets: list[float] = Field(default_factory=list)
    invalidation_rules: list[str] = Field(default_factory=list)
    thesis_features_at_entry: dict[str, Any] = Field(default_factory=dict)
    current_thesis_score: float = Field(default=1.0, ge=0.0, le=1.0)
    degradation_reasons: list[str] = Field(default_factory=list)
    position_state: PositionThesisState = "proposed"
    execution_report_ids: list[str] = Field(default_factory=list)
    opened_at_ms: int | None = None
    updated_at_ms: int
    closed_at_ms: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("asset")
    @classmethod
    def _uppercase_asset(cls, value: str) -> str:
        return value.upper().strip()

    @model_validator(mode="after")
    def _validate_state_times(self) -> Self:
        if self.position_state == "closed" and self.closed_at_ms is None:
            raise ValueError("closed positions require closed_at_ms")
        if self.closed_at_ms is not None and self.opened_at_ms is not None and self.closed_at_ms < self.opened_at_ms:
            raise ValueError("closed_at_ms cannot be before opened_at_ms")
        return self


class ReconciliationRun(BaseModel):
    reconciliation_id: str
    execution_mode: ExecutionMode
    status: Literal["ok", "mismatch", "error"]
    expected_positions: list[dict[str, Any]] = Field(default_factory=list)
    observed_positions: list[dict[str, Any]] = Field(default_factory=list)
    mismatches: list[dict[str, Any]] = Field(default_factory=list)
    started_at_ms: int
    completed_at_ms: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PnLAttributionRecord(BaseModel):
    attribution_id: str
    position_id: str | None = None
    candidate_id: str | None = None
    strategy_id: str
    asset: str
    window_start_ms: int
    window_end_ms: int
    alpha_pnl_usd: float = 0.0
    timing_pnl_usd: float = 0.0
    execution_pnl_usd: float = 0.0
    fees_usd: float = 0.0
    funding_usd: float = 0.0
    residual_pnl_usd: float = 0.0
    total_pnl_usd: float = 0.0
    metrics: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class KillSwitchEvent(BaseModel):
    event_id: str
    scope: KillSwitchScope
    action: KillSwitchAction
    triggered_by: str
    reason: str
    affected_assets: list[str] = Field(default_factory=list)
    affected_strategies: list[str] = Field(default_factory=list)
    block_new_orders: bool = True
    cancel_open_orders: bool = False
    freeze_config_changes: bool = True
    created_at_ms: int
    expires_at_ms: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("affected_assets")
    @classmethod
    def _uppercase_assets(cls, value: list[str]) -> list[str]:
        return sorted({item.upper().strip() for item in value if item and item.strip()})


class ModelVersion(BaseModel):
    model_version_id: str
    model_type: str
    artifact_uri: str
    training_data_hash: str
    feature_schema_hash: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    status: ModelVersionStatus = "candidate"
    approved_by: str | None = None
    approved_at_ms: int | None = None
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_approval(self) -> Self:
        if self.status == "approved" and not (self.approved_by and self.approved_at_ms):
            raise ValueError("approved model versions require approved_by and approved_at_ms")
        return self


class ModelTrainingRun(BaseModel):
    training_run_id: str
    model_version_id: str | None = None
    model_type: str
    dataset_start_ms: int
    dataset_end_ms: int
    training_data_hash: str
    feature_schema_hash: str
    code_version: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    artifact_uri: str | None = None
    status: Literal["started", "completed", "failed"] = "started"
    created_at_ms: int
    completed_at_ms: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class FeatureSchemaVersion(BaseModel):
    feature_schema_version_id: str
    schema_hash: str
    feature_names: list[str]
    feature_definitions: dict[str, Any] = Field(default_factory=dict)
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class FeatureRollup(BaseModel):
    rollup_id: str
    asset: str
    feature_group: str
    feature_name: str
    interval: Literal["1m", "5m", "1h", "1d"]
    window_start_ms: int
    window_end_ms: int
    min_value: float | None = None
    max_value: float | None = None
    avg_value: float | None = None
    last_value: float | None = None
    count: int = Field(ge=0)
    quality_avg: float | None = Field(default=None, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetentionRun(BaseModel):
    retention_run_id: str
    status: Literal["started", "completed", "failed"]
    started_at_ms: int
    completed_at_ms: int | None = None
    deleted_counts: dict[str, int] = Field(default_factory=dict)
    rollup_counts: dict[str, int] = Field(default_factory=dict)
    caveats: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReplayResult(BaseModel):
    replay_id: str
    mode: ReplayMode
    candidate_id: str | None = None
    decision_id: str | None = None
    status: ReplayStatus = "audit_only"
    baseline_metrics: dict[str, Any] = Field(default_factory=dict)
    replay_metrics: dict[str, Any] = Field(default_factory=dict)
    diffs: dict[str, Any] = Field(default_factory=dict)
    caveats: list[str] = Field(default_factory=list)
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)
