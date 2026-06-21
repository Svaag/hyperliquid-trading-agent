from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Hip4Mode = Literal["read_only", "shadow", "paper_shadow"]
Hip4SubsystemStatus = Literal["disabled", "degraded", "ok"]
Hip4ExecutionMode = Literal["shadow", "paper", "manual_ticket"]
Hip4CandidateStatus = Literal["candidate", "rejected", "paper_executed", "expired"]
Hip4StrategyType = Literal["binary_split_sell", "binary_buy_merge", "question_complete_set_sell", "question_complete_set_buy", "shadow_inventory_carry"]
Hip4BookSide = Literal["bid", "ask"]
Hip4ActionType = Literal[
    "BUY_SIDE_TOKEN",
    "SELL_SIDE_TOKEN",
    "SPLIT_OUTCOME",
    "MERGE_OUTCOME",
    "NEGATE_OUTCOME",
    "MERGE_QUESTION",
    "SETTLE_OUTCOME",
    "MARK_TO_BOOK",
]

HIP4_INTEGRATION_BOUNDARIES: tuple[str, ...] = (
    "settings",
    "repository",
    "routes",
    "metrics",
    "websocket_worker",
    "discord_reporting",
    "risk_gateway",
)
ZERO = Decimal("0")
ONE = Decimal("1")


class Hip4SafetyPosture(BaseModel):
    """Static safety posture for the HIP-4 MVP."""

    read_only: bool = True
    shadow_only_allowed: bool = True
    paper_only_allowed: bool = True
    signing_enabled: bool = False
    private_keys_enabled: bool = False
    exchange_mutation_enabled: bool = False
    live_orders_enabled: bool = False
    sdk_exchange_enabled: bool = False
    llm_controlled_execution_enabled: bool = False
    autonomy_promotion_enabled: bool = False
    perps_engine_promotion_enabled: bool = False
    integration_boundaries: list[str] = Field(default_factory=lambda: list(HIP4_INTEGRATION_BOUNDARIES))


class Hip4CapabilityProbe(BaseModel):
    network: Literal["mainnet", "testnet"]
    probed_at_ms: int
    outcome_meta_available: bool = False
    outcome_meta_error: str | None = None
    outcome_meta_top_level_keys: list[str] = Field(default_factory=list)
    outcome_meta_schema_hash: str | None = None
    supports_outcomes: bool = False
    supports_questions: bool = False
    supports_question_fields: bool = False
    question_fields_seen: list[str] = Field(default_factory=list)
    missing_question_fields: list[str] = Field(default_factory=list)
    supports_outcome_meta_ws: bool = False
    outcome_meta_ws_status: Literal["confirmed", "unconfirmed", "unsupported", "disabled"] = "disabled"
    supports_quote_token: bool = False
    quote_tokens_seen: list[str] = Field(default_factory=list)
    supports_authoritative_size_metadata: bool = False
    size_metadata_source: Literal["meta", "spotMeta", "outcomeMeta", "inferred", "unknown"] = "unknown"
    supports_authoritative_tick_metadata: bool = False
    tick_metadata_source: Literal["meta", "spotMeta", "outcomeMeta", "inferred", "unknown"] = "unknown"
    supports_abstract_native_mechanics: bool = False
    supports_user_outcome_action_json: bool = False
    supports_native_action_modeling: bool = False
    supports_question_mechanics: bool = False
    supports_manual_ticket_export: bool = False
    docs_scope_status: Literal["verified_not_testnet_only", "testnet_only", "unknown"] = "unknown"
    undocumented_fields: dict[str, list[str]] = Field(default_factory=dict)
    network_dependent_fields: list[str] = Field(default_factory=list)
    degraded_reasons: list[str] = Field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return bool(self.degraded_reasons) or not (self.outcome_meta_available and self.supports_outcomes)


class OutcomeSpec(BaseModel):
    outcome_id: int
    name: str
    description: str = ""
    quote_token: str | None = None
    side0_name: str = "YES"
    side1_name: str = "NO"
    settled: bool = False
    settle_fraction: Decimal | None = None
    settlement_details: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class QuestionSpec(BaseModel):
    question_id: int
    name: str
    description: str = ""
    fallback_outcome_id: int | None = None
    named_outcome_ids: list[int] = Field(default_factory=list)
    settled_named_outcome_ids: list[int] = Field(default_factory=list)
    outcome_ids: list[int] = Field(default_factory=list)
    status: Literal["open", "partial_settled", "settled"] = "open"
    raw: dict[str, Any] = Field(default_factory=dict)


class RawPayloadRecord(BaseModel):
    source: str
    network: str
    payload_json: dict[str, Any]
    schema_hash: str
    schema_version: int = 1
    observed_at_ms: int


class PriceLevel(BaseModel):
    px: Decimal
    sz: Decimal
    n: int | None = None

    @field_validator("px", "sz", mode="before")
    @classmethod
    def _decimal(cls, value: Any) -> Decimal:
        return _to_decimal(value)


class NormalizedOutcomeBook(BaseModel):
    coin: str
    outcome_id: int
    side: Literal[0, 1]
    bids: list[PriceLevel] = Field(default_factory=list)
    asks: list[PriceLevel] = Field(default_factory=list)
    as_of_ms: int
    source: Literal["rest", "ws", "fixture"] = "rest"
    stale: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)


class ExecutableLeg(BaseModel):
    coin: str
    outcome_id: int
    side: Literal[0, 1]
    book_side: Hip4BookSide
    size: Decimal
    avg_price: Decimal
    notional: Decimal
    max_slippage_bps: Decimal = ZERO

    @field_validator("size", "avg_price", "notional", "max_slippage_bps", mode="before")
    @classmethod
    def _decimal(cls, value: Any) -> Decimal:
        return _to_decimal(value)


class PaperNativeAction(BaseModel):
    action_type: Hip4ActionType
    outcome_id: int | None = None
    question_id: int | None = None
    side: Literal[0, 1] | None = None
    coin: str | None = None
    amount: Decimal = ZERO
    price: Decimal | None = None
    balance_deltas: dict[str, Decimal] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("amount", "price", mode="before")
    @classmethod
    def _optional_decimal(cls, value: Any) -> Decimal | None:
        if value is None:
            return None
        return _to_decimal(value)


class Hip4Candidate(BaseModel):
    candidate_id: str
    strategy_type: Hip4StrategyType
    mode: Hip4ExecutionMode = "shadow"
    question_id: int | None = None
    outcome_ids: list[int] = Field(default_factory=list)
    as_of_ms: int
    size: Decimal
    gross_cost_or_proceeds: Decimal
    expected_net_edge_usd: Decimal
    expected_net_edge_bps: Decimal
    min_profit_usd: Decimal = ZERO
    fee_stress_bps: Decimal = ZERO
    quote_token: str = ""
    legs: list[ExecutableLeg] = Field(default_factory=list)
    actions: list[PaperNativeAction] = Field(default_factory=list)
    residual_inventory: dict[str, Decimal] = Field(default_factory=dict)
    proof: dict[str, Any] = Field(default_factory=dict)
    risk_flags: list[str] = Field(default_factory=list)
    reject_reasons: list[str] = Field(default_factory=list)
    status: Hip4CandidateStatus = "candidate"

    @field_validator(
        "size",
        "gross_cost_or_proceeds",
        "expected_net_edge_usd",
        "expected_net_edge_bps",
        "min_profit_usd",
        "fee_stress_bps",
        mode="before",
    )
    @classmethod
    def _decimal(cls, value: Any) -> Decimal:
        return _to_decimal(value)


class OutcomeOrderIntent(BaseModel):
    intent_id: str
    mode: Hip4ExecutionMode
    strategy_type: Hip4StrategyType
    question_id: int | None = None
    outcome_ids: list[int] = Field(default_factory=list)
    as_of_ms: int
    deadline_ts_ms: int
    target_quote_amount: Decimal
    min_profit_usd: Decimal
    expected_net_edge_usd: Decimal
    expected_net_edge_bps: Decimal
    max_slippage_bps: Decimal
    path: list[PaperNativeAction] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    status: str = "proposed"
    exchange_actions: list[dict[str, Any]] = Field(default_factory=list)


class Hip4PaperFill(BaseModel):
    fill_id: str
    candidate_id: str
    coin: str
    side: Literal["buy", "sell"]
    size: Decimal
    price: Decimal
    notional: Decimal
    fee: Decimal = ZERO
    created_at_ms: int


class Hip4PaperPortfolio(BaseModel):
    portfolio_id: str = "hip4_default"
    quote_token: str = "USDC"
    cash: Decimal
    realized_pnl: Decimal = ZERO
    unrealized_pnl: Decimal = ZERO
    settlement_pnl: Decimal = ZERO
    modeled_fees: Decimal = ZERO
    daily_notional: Decimal = ZERO
    balances: dict[str, Decimal] = Field(default_factory=dict)
    updated_at_ms: int = 0


class Hip4RiskDecision(BaseModel):
    allowed: bool
    decision: Literal["allow", "reject"]
    violations: list[dict[str, Any]] = Field(default_factory=list)
    risk_gateway_decision: dict[str, Any] | None = None


class Hip4ServiceStatus(BaseModel):
    enabled: bool
    mode: Hip4Mode
    status: Hip4SubsystemStatus
    degraded_reasons: list[str] = Field(default_factory=list)
    safety: Hip4SafetyPosture = Field(default_factory=Hip4SafetyPosture)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    registry: dict[str, Any] = Field(default_factory=dict)
    market_data: dict[str, Any] = Field(default_factory=dict)
    scanner: dict[str, Any] = Field(default_factory=dict)
    paper: dict[str, Any] = Field(default_factory=dict)
    proactive_loop: dict[str, Any] = Field(default_factory=dict)
    learning: dict[str, Any] = Field(default_factory=dict)


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return ZERO
    return Decimal(str(value))
