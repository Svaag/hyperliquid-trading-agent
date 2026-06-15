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
SignalStatus = Literal["candidate", "posted", "approved", "rejected", "expired", "paper_ordered", "cancelled"]
SignalSide = Literal["long", "short"]
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
    action: Literal["approve", "reject", "signal", "signals", "portfolio", "positions", "orders", "market_map", "pause", "resume"]
    signal_id: str | None = None


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
