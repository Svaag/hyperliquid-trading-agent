from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

PredictionMarketBetSide = Literal["yes", "no"]
PredictionMarketDraftStatus = Literal["new", "confirmed", "cancelled", "expired"]
PredictionMarketPositionStatus = Literal["open", "closed", "settled"]
PredictionMarketFillAction = Literal["open", "close", "settle"]
PredictionMarketSettlementSource = Literal["provider", "admin"]


class PredictionMarketQuote(BaseModel):
    quote_id: str
    signal_id: str
    venue: str
    market_id: str
    question: str
    outcome_id: str | None = None
    outcome_name: str = ""
    side: PredictionMarketBetSide = "yes"
    implied_probability: float | None = Field(default=None, ge=0.0, le=1.0)
    best_bid: float | None = Field(default=None, ge=0.0, le=1.0)
    best_ask: float | None = Field(default=None, ge=0.0, le=1.0)
    price: float = Field(gt=0.0, le=1.0)
    liquidity_usd: float | None = Field(default=None, ge=0.0)
    volume_usd: float | None = Field(default=None, ge=0.0)
    status: str = "open"
    as_of_ms: int
    staleness_ms: int | None = Field(default=None, ge=0)
    symbols: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, value: list[str]) -> list[str]:
        return sorted({item.upper().strip() for item in value if item and item.strip()})

    @field_validator("topics")
    @classmethod
    def _topics(cls, value: list[str]) -> list[str]:
        return sorted({item.lower().strip() for item in value if item and item.strip()})


class PredictionMarketBetDraftRequest(BaseModel):
    discord_guild_id: str
    discord_user_id: str
    side: PredictionMarketBetSide = "yes"
    stake_usd: float | None = Field(default=None, gt=0.0)
    query: str = ""
    market_ref: str | None = None
    actor: str = "discord"
    source: str = "discord"
    metadata: dict[str, object] = Field(default_factory=dict)


class PredictionMarketBetConfirmRequest(BaseModel):
    actor: str = "discord"


class PredictionMarketBetCancelRequest(BaseModel):
    actor: str = "discord"
    reason: str = "cancelled"


class PredictionMarketPositionCloseRequest(BaseModel):
    actor: str = "discord"
    reason: str = "manual"


class PredictionMarketSettlementRequest(BaseModel):
    venue: str
    market_id: str
    outcome_id: str | None = None
    settlement_fraction: float = Field(ge=0.0, le=1.0)
    source: PredictionMarketSettlementSource = "admin"
    actor: str = "api"
    metadata: dict[str, object] = Field(default_factory=dict)


class PredictionMarketPaperAccount(BaseModel):
    account_id: str
    discord_guild_id: str
    discord_user_id: str
    status: str = "active"
    initial_cash_usd: float
    cash_usd: float
    realized_pnl_usd: float = 0.0
    metadata: dict[str, object] = Field(default_factory=dict)
    created_at_ms: int | None = None
    updated_at_ms: int | None = None


class PredictionMarketBetDraft(BaseModel):
    draft_id: str
    account_id: str
    discord_guild_id: str
    discord_user_id: str
    venue: str
    market_id: str
    outcome_id: str | None = None
    outcome_name: str = ""
    question: str
    side: PredictionMarketBetSide
    stake_usd: float = Field(gt=0.0)
    price: float = Field(gt=0.0, le=1.0)
    shares: float = Field(gt=0.0)
    quote_signal_id: str | None = None
    status: PredictionMarketDraftStatus = "new"
    created_at_ms: int
    expires_at_ms: int
    confirmed_at_ms: int | None = None
    cancelled_at_ms: int | None = None
    metadata: dict[str, object] = Field(default_factory=dict)


class PredictionMarketPosition(BaseModel):
    position_id: str
    account_id: str
    discord_guild_id: str
    discord_user_id: str
    draft_id: str | None = None
    venue: str
    market_id: str
    outcome_id: str | None = None
    outcome_name: str = ""
    question: str
    side: PredictionMarketBetSide
    status: PredictionMarketPositionStatus = "open"
    shares: float = Field(gt=0.0)
    avg_entry_price: float = Field(gt=0.0, le=1.0)
    cost_usd: float = Field(gt=0.0)
    mark_price: float | None = Field(default=None, ge=0.0, le=1.0)
    current_value_usd: float = 0.0
    realized_pnl_usd: float = 0.0
    unrealized_pnl_usd: float = 0.0
    opened_at_ms: int
    closed_at_ms: int | None = None
    settled_at_ms: int | None = None
    result: Literal["won", "lost", "push", "closed", "open"] = "open"
    metadata: dict[str, object] = Field(default_factory=dict)


class PredictionMarketFill(BaseModel):
    fill_id: str
    account_id: str
    position_id: str | None = None
    draft_id: str | None = None
    action: PredictionMarketFillAction
    venue: str
    market_id: str
    outcome_id: str | None = None
    shares: float = 0.0
    price: float = Field(ge=0.0, le=1.0)
    cash_delta_usd: float
    realized_pnl_usd: float = 0.0
    created_at_ms: int
    metadata: dict[str, object] = Field(default_factory=dict)


class PredictionMarketSettlement(BaseModel):
    settlement_id: str
    venue: str
    market_id: str
    outcome_id: str | None = None
    settlement_fraction: float = Field(ge=0.0, le=1.0)
    source: PredictionMarketSettlementSource = "provider"
    applied_by: str = "system"
    created_at_ms: int
    metadata: dict[str, object] = Field(default_factory=dict)


class PredictionMarketLeaderboardRow(BaseModel):
    discord_guild_id: str
    discord_user_id: str
    account_id: str
    cash_usd: float
    open_value_usd: float
    equity_usd: float
    realized_pnl_usd: float
    unrealized_pnl_usd: float
    total_pnl_usd: float
    roi_pct: float
    won: int = 0
    lost: int = 0
    open_positions: int = 0
    settled_positions: int = 0
