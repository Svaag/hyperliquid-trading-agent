from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

AssetSource = Literal["core", "top_volume", "hip3_alias"]
AssetKind = Literal["perp", "spot", "hip3_index"]
LevelKind = Literal[
    "support",
    "resistance",
    "liquidity_wall",
    "liquidation_known",
    "liquidation_inferred",
    "prior_high",
    "prior_low",
    "vwap",
]
LevelSource = Literal["candles", "l2", "public_account", "inferred"]
Trend = Literal["up", "down", "range", "unknown"]
VolatilityRegime = Literal["low", "normal", "high", "unknown"]
RiskRegime = Literal["risk_on", "risk_off", "mixed", "unknown"]
SignalStatus = Literal["candidate", "posted", "approved", "rejected", "expired", "paper_ordered", "cancelled", "flip_requested"]
SignalSide = Literal["long", "short"]
SignalEvaluationStatus = Literal["open", "complete", "partial", "expired_no_data", "error"]
SignalTerminalOutcome = Literal["tp_hit", "stop_hit", "expired_positive", "expired_negative", "expired_flat", "insufficient_data", "open"]
SignalEvaluationMarkStatus = Literal["pending", "marked", "missed_no_price", "error"]
EvaluationHorizon = Literal["5m", "15m", "1h", "4h", "24h", "72h", "expiry"]
RoleName = Literal["analyst", "quant", "research", "risk", "treasury", "execution", "adversary", "judge"]
LessonType = Literal["role_behavior", "signal_quality", "risk_discipline", "operator_output", "data_quality", "incident_warning"]
LessonValidationStatus = Literal["active", "needs_human_review", "shadow", "archived", "expired", "rejected"]
TuningProposalStatus = Literal["draft", "proposed", "accepted_manually", "rejected", "expired", "superseded"]
Sentiment = Literal["bullish", "bearish", "mixed", "unknown"]
Freshness = Literal["breaking", "fresh", "stale"]
OrderStatus = Literal["new", "filled", "cancelled", "rejected"]
PositionStatus = Literal["open", "closed"]


class MarketAsset(BaseModel):
    symbol: str
    display_name: str
    source: AssetSource
    dex: str | None = None
    kind: AssetKind = "perp"
    sz_decimals: int | None = None
    max_leverage: int | None = None
    day_volume_usd: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MarketLevel(BaseModel):
    id: str
    symbol: str
    kind: LevelKind
    price: float
    strength: float = Field(ge=0.0, le=100.0)
    timeframe: str
    source: LevelSource
    first_seen_ms: int
    last_seen_ms: int
    expires_at_ms: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LiquidationCluster(BaseModel):
    symbol: str
    price: float
    side_at_risk: Literal["longs", "shorts", "unknown"] = "unknown"
    notional_usd_known: float | None = None
    confidence: Literal["direct", "inferred_low", "inferred_medium"]
    source: Literal["public_account", "market_structure", "orderbook"]
    accounts: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class OrderflowState(BaseModel):
    spread_bps: float | None = None
    top_depth_usd: float | None = None
    depth_10bps_bid_usd: float | None = None
    depth_10bps_ask_usd: float | None = None
    depth_50bps_bid_usd: float | None = None
    depth_50bps_ask_usd: float | None = None
    imbalance_top: float | None = None
    imbalance_10bps: float | None = None
    microprice: float | None = None
    large_bid_walls: list[MarketLevel] = Field(default_factory=list)
    large_ask_walls: list[MarketLevel] = Field(default_factory=list)
    recent_trade_imbalance: float | None = None
    cvd_proxy: float | None = None


class NewsEvent(BaseModel):
    id: str
    source: str
    provider: str
    title: str
    text: str = ""
    url: str | None = None
    author_id: str | None = None
    created_at_ms: int | None = None
    observed_at_ms: int
    assets: list[str] = Field(default_factory=list)
    importance_score: float = Field(default=0.0, ge=0.0, le=100.0)
    sentiment: Sentiment = "unknown"
    freshness: Freshness = "fresh"
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssetNewsState(BaseModel):
    latest_events: list[NewsEvent] = Field(default_factory=list)
    max_importance_score: float = 0.0
    sentiment: Sentiment = "unknown"
    updated_at_ms: int | None = None


class AssetMarketState(BaseModel):
    symbol: str
    timestamp_ms: int
    mid: float | None = None
    mark: float | None = None
    oracle: float | None = None
    funding_hourly: float | None = None
    open_interest: float | None = None
    day_volume_usd: float | None = None
    trend: Trend = "unknown"
    volatility_regime: VolatilityRegime = "unknown"
    support_levels: list[MarketLevel] = Field(default_factory=list)
    resistance_levels: list[MarketLevel] = Field(default_factory=list)
    liquidity_levels: list[MarketLevel] = Field(default_factory=list)
    liquidation_clusters: list[LiquidationCluster] = Field(default_factory=list)
    orderflow: OrderflowState | None = None
    news_state: AssetNewsState | None = None
    regime_score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class GlobalMarketMap(BaseModel):
    timestamp_ms: int = 0
    risk_regime: RiskRegime = "unknown"
    leaders: list[str] = Field(default_factory=list)
    laggards: list[str] = Field(default_factory=list)
    btc_beta_notes: dict[str, float] = Field(default_factory=dict)
    correlated_clusters: list[list[str]] = Field(default_factory=list)
    key_themes: list[str] = Field(default_factory=list)
    assets: dict[str, AssetMarketState] = Field(default_factory=dict)


class SignalEvidence(BaseModel):
    category: str
    label: str
    value: str | float | int | bool | None = None
    weight: float = 0.0
    source: Literal["market_structure", "orderflow", "funding", "news", "risk", "execution", "model"] = "market_structure"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelMarketInsight(BaseModel):
    stance: Literal["support", "oppose", "needs_more_data"] = "needs_more_data"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    thesis_quality: float = Field(default=0.0, ge=0.0, le=1.0)
    hidden_risks: list[str] = Field(default_factory=list)
    what_would_invalidate: list[str] = Field(default_factory=list)
    suggested_adjustments: list[str] = Field(default_factory=list)
    summary: str = ""


class TradeSignal(BaseModel):
    id: str
    symbol: str
    side: SignalSide
    signal_type: str
    status: SignalStatus = "candidate"
    score: float = Field(ge=0.0, le=100.0)
    confidence: float = Field(ge=0.0, le=1.0)
    created_at_ms: int
    expires_at_ms: int
    entry: float
    stop: float
    take_profit: float | None = None
    invalidation: str
    thesis: str
    evidence: list[SignalEvidence] = Field(default_factory=list)
    feature_snapshot: dict[str, Any] = Field(default_factory=dict)
    risk_plan: dict[str, Any] = Field(default_factory=dict)
    model_insight: dict[str, Any] | None = None
    discord_channel_id: str | None = None
    discord_message_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PaperPortfolio(BaseModel):
    id: str
    name: str = "default"
    status: Literal["active", "paused", "closed"] = "active"
    initial_equity_usd: float
    cash_usd: float
    realized_pnl_usd: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at_ms: int
    updated_at_ms: int


class PaperOrder(BaseModel):
    id: str
    portfolio_id: str
    signal_id: str | None = None
    symbol: str
    side: SignalSide
    order_type: Literal["market"] = "market"
    status: OrderStatus = "new"
    quantity: float
    requested_px: float | None = None
    filled_px: float | None = None
    stop_px: float | None = None
    take_profit_px: float | None = None
    fee_bps: float
    slippage_bps: float
    created_at_ms: int
    filled_at_ms: int | None = None
    cancelled_at_ms: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PaperFill(BaseModel):
    id: str
    order_id: str
    portfolio_id: str
    symbol: str
    side: SignalSide
    quantity: float
    price: float
    fee_usd: float
    slippage_usd: float
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class PaperPosition(BaseModel):
    id: str
    portfolio_id: str
    signal_id: str | None = None
    symbol: str
    side: SignalSide
    status: PositionStatus = "open"
    quantity: float
    avg_entry_px: float
    mark_px: float | None = None
    stop_px: float
    take_profit_px: float | None = None
    realized_pnl_usd: float = 0.0
    unrealized_pnl_usd: float = 0.0
    opened_at_ms: int
    closed_at_ms: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PortfolioSnapshot(BaseModel):
    id: str
    portfolio_id: str
    timestamp_ms: int
    cash_usd: float
    equity_usd: float
    gross_exposure_usd: float
    net_exposure_usd: float
    realized_pnl_usd: float
    unrealized_pnl_usd: float
    total_pnl_usd: float
    drawdown_pct: float
    sharpe: float | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)


class AutonomyCommand(BaseModel):
    action: Literal[
        "approve",
        "reject",
        "signal",
        "signals",
        "portfolio",
        "positions",
        "orders",
        "market_map",
        "pause",
        "resume",
        "daily_report",
        "weekly_report",
        "token_capital",
        "signal_outcome",
        "feedback_signal",
        "feedback_bot",
        "memories",
        "memory",
        "tuning_proposals",
        "tuning_proposal",
        "apply_tuning_proposal",
        "approve_flip",
    ]
    signal_id: str | None = None
    lesson_id: str | None = None
    proposal_id: str | None = None
    role: str | None = None
    rating: str | None = None
    note: str = ""


class AutonomyServiceStatus(BaseModel):
    enabled: bool
    running: bool
    paused: bool
    mode: str
    universe_count: int = 0
    hot_l2_assets: list[str] = Field(default_factory=list)
    signals_today: int = 0
    open_positions: int = 0
    last_market_data_at_ms: int | None = None
    last_iteration_at_ms: int | None = None
    last_error: str | None = None
    paper_portfolio_id: str | None = None


class SignalEvaluationMark(BaseModel):
    id: str
    evaluation_id: str
    signal_id: str
    symbol: str
    horizon: str
    due_at_ms: int
    marked_at_ms: int | None = None
    price: float | None = None
    direction_adjusted_return_bps: float | None = None
    r_multiple: float | None = None
    mfe_bps_until_mark: float | None = None
    mae_bps_until_mark: float | None = None
    mfe_r_until_mark: float | None = None
    mae_r_until_mark: float | None = None
    stop_hit_before_mark: bool = False
    take_profit_hit_before_mark: bool = False
    status: SignalEvaluationMarkStatus = "pending"
    metadata: dict[str, Any] = Field(default_factory=dict)


class SignalEvaluation(BaseModel):
    id: str
    signal_id: str
    symbol: str
    side: SignalSide
    signal_type: str
    status: SignalEvaluationStatus = "open"
    created_at_ms: int
    completed_at_ms: int | None = None
    entry: float
    stop: float
    take_profit: float | None = None
    signal_score: float
    signal_confidence: float
    signal_status_at_eval_start: str
    first_price: float | None = None
    latest_price: float | None = None
    latest_price_at_ms: int | None = None
    max_favorable_price: float | None = None
    max_adverse_price: float | None = None
    max_favorable_bps: float | None = None
    max_adverse_bps: float | None = None
    max_favorable_r: float | None = None
    max_adverse_r: float | None = None
    stop_hit: bool = False
    stop_hit_at_ms: int | None = None
    take_profit_hit: bool = False
    take_profit_hit_at_ms: int | None = None
    terminal_outcome: SignalTerminalOutcome = "open"
    realized_or_marked_r: float | None = None
    opportunity_cost_r: float | None = None
    approved: bool = False
    rejected: bool = False
    paper_ordered: bool = False
    paper_position_id: str | None = None
    feature_snapshot: dict[str, Any] = Field(default_factory=dict)
    evidence_snapshot: list[dict[str, Any]] = Field(default_factory=list)
    market_regime: str = "unknown"
    error: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    marks: list[SignalEvaluationMark] = Field(default_factory=list)


class MemoryObservation(BaseModel):
    id: str
    source_type: Literal["signal_evaluation", "daily_report", "weekly_report", "operator_feedback", "role_output", "schema_validation", "incident"]
    source_id: str
    role: str | None = None
    symbol: str | None = None
    signal_type: str | None = None
    market_regime: str | None = None
    observation: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    severity: Literal["info", "warning", "critical"] = "info"
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class CandidateLesson(BaseModel):
    id: str
    lesson_type: LessonType
    role: str | None = None
    scope: dict[str, Any] = Field(default_factory=dict)
    claim: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    source_observation_ids: list[str] = Field(default_factory=list)
    source_run_ids: list[str] = Field(default_factory=list)
    source_signal_ids: list[str] = Field(default_factory=list)
    sample_size: int = 0
    counterexamples: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    expected_future_behavior_change: str = ""
    strategy_affecting: bool = False
    risk_affecting: bool = False
    execution_affecting: bool = False
    capital_allocation_affecting: bool = False
    status: Literal["candidate", "shadow", "promoted", "rejected", "expired"] = "candidate"
    created_at_ms: int
    expires_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class RoleLessonMemory(BaseModel):
    id: str
    role: RoleName
    lesson_type: str
    scope: dict[str, Any] = Field(default_factory=dict)
    claim: str
    instruction: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    source_candidate_id: str | None = None
    source_run_ids: list[str] = Field(default_factory=list)
    source_signal_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    sample_size: int = 0
    counterexamples: list[dict[str, Any]] = Field(default_factory=list)
    validation_status: LessonValidationStatus = "active"
    strategy_affecting: bool = False
    risk_affecting: bool = False
    execution_affecting: bool = False
    capital_allocation_affecting: bool = False
    created_at_ms: int
    activated_at_ms: int | None = None
    expires_at_ms: int
    last_revalidated_at_ms: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class OperatorOutputLessonMemory(BaseModel):
    id: str
    scope: dict[str, Any] = Field(default_factory=dict)
    issue_or_pattern: str
    preferred_behavior: str
    bad_examples: list[dict[str, Any]] = Field(default_factory=list)
    good_examples: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    sample_size: int = 0
    validation_status: LessonValidationStatus = "active"
    created_at_ms: int
    expires_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class OperatorFeedback(BaseModel):
    id: str
    source: Literal["discord", "api"] = "api"
    actor_id: str | None = None
    target_type: Literal["signal", "report", "lesson", "discord_message", "tuning_proposal", "bot"]
    target_id: str
    rating: Literal["good", "bad", "unclear", "too_noisy", "useful", "wrong"]
    note: str = ""
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class TuningProposal(BaseModel):
    id: str
    proposal_type: Literal["threshold_change", "weight_change", "cooldown_change", "risk_rule_change", "universe_change", "messaging_change", "data_quality_gate", "role_prompt_change"]
    status: TuningProposalStatus = "draft"
    title: str
    summary: str
    affected_scope: dict[str, Any] = Field(default_factory=dict)
    current_behavior: dict[str, Any] = Field(default_factory=dict)
    proposed_diff: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    source_lesson_ids: list[str] = Field(default_factory=list)
    source_signal_ids: list[str] = Field(default_factory=list)
    expected_impact: str
    risk_assessment: str
    blast_radius: Literal["low", "medium", "high"] = "low"
    rollback_plan: str
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    sample_size: int = 0
    created_at_ms: int
    expires_at_ms: int
    evaluation_window: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class TokenCapitalSnapshot(BaseModel):
    id: str
    timestamp_ms: int
    window: Literal["daily", "weekly", "rolling_30d"] = "daily"
    total_score: float = Field(ge=0.0, le=100.0)
    risk_adjusted_performance_score: float = Field(ge=0.0, le=100.0)
    signal_quality_score: float = Field(ge=0.0, le=100.0)
    memory_compounding_score: float = Field(ge=0.0, le=100.0)
    risk_discipline_score: float = Field(ge=0.0, le=100.0)
    operator_communication_score: float = Field(ge=0.0, le=100.0)
    reliability_score: float = Field(ge=0.0, le=100.0)
    hard_gate_penalties: list[dict[str, Any]] = Field(default_factory=list)
    component_details: dict[str, Any] = Field(default_factory=dict)
    created_from_report_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AutonomyReport(BaseModel):
    id: str
    report_type: Literal["daily", "weekly"]
    key: str
    period_start_ms: int
    period_end_ms: int
    generated_at_ms: int
    token_capital: TokenCapitalSnapshot
    summary: str
    report: dict[str, Any] = Field(default_factory=dict)
    discord_channel_id: str | None = None
    discord_message_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
