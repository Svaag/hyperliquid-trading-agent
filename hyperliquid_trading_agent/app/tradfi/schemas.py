"""TradFi data schemas — vendor-agnostic Pydantic models."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# --- Enums -------------------------------------------------------------------

AssetKind = Literal["stock", "etf", "index", "crypto", "option", "unknown"]

# --- Stock Data --------------------------------------------------------------


class StockQuote(BaseModel):
    """Latest NBBO quote for a single symbol."""

    symbol: str
    ask_price: float | None = None
    ask_size: float | None = None
    bid_price: float | None = None
    bid_size: float | None = None
    timestamp: datetime | None = None
    conditions: list[str] = Field(default_factory=list)
    tape: str = ""


class StockTrade(BaseModel):
    """Latest trade for a single symbol."""

    symbol: str
    price: float
    size: int
    timestamp: datetime | None = None
    exchange: str | None = None
    conditions: list[str] = Field(default_factory=list)
    tape: str = ""


class Bar(BaseModel):
    """OHLCV bar for any asset class."""

    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: int | None = None
    vwap: float | None = None
    timeframe: str = "1Day"  # e.g. 1Min, 15Min, 1Hour, 1Day


class StockSnapshot(BaseModel):
    """Multi-field snapshot for a single stock (latest quote + trade + daily bar)."""

    symbol: str
    latest_quote: StockQuote | None = None
    latest_trade: StockTrade | None = None
    daily_bar: Bar | None = None
    previous_close: float | None = None
    change_pct: float | None = None


# --- Corporate Actions -------------------------------------------------------


class CorporateAction(BaseModel):
    """Corporate action (split, dividend, merger, etc.)."""

    id: str
    symbol: str
    action_type: str
    declaration_date: date | None = None
    ex_date: date | None = None
    record_date: date | None = None
    payable_date: date | None = None
    description: str = ""
    # split-specific
    old_rate: float | None = None
    new_rate: float | None = None
    # dividend-specific
    dividend_rate: float | None = None
    dividend_type: str | None = None


class CalendarEvent(BaseModel):
    """Upcoming calendar event (earnings, dividend, IPO, etc.)."""

    symbol: str | None = None
    date: date
    event_type: str  # earnings, ex_dividend, ipo, split
    description: str = ""


# --- Options -----------------------------------------------------------------


class OptionContract(BaseModel):
    """Single options contract with greeks."""

    symbol: str  # OCC symbol, e.g. AAPL250620C00200000
    underlying: str
    strike_price: float
    expiration_date: date
    option_type: Literal["call", "put"]
    style: Literal["american", "european"] = "american"

    # market data
    bid: float | None = None
    ask: float | None = None
    last_price: float | None = None
    last_size: int | None = None
    volume: int | None = None
    open_interest: int | None = None
    implied_volatility: float | None = None

    # greeks
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None

    # status
    close_price: float | None = None
    close_price_date: date | None = None


class OptionsChain(BaseModel):
    """Full options chain for an underlying at a given expiration."""

    underlying: str
    underlying_price: float | None = None
    expiration_date: date | None = None
    contracts: list[OptionContract] = Field(default_factory=list)

    # convenience accessors
    @property
    def calls(self) -> list[OptionContract]:
        return [c for c in self.contracts if c.option_type == "call"]

    @property
    def puts(self) -> list[OptionContract]:
        return [c for c in self.contracts if c.option_type == "put"]


# --- Options Flow ------------------------------------------------------------


class OptionsFlowEvent(BaseModel):
    """Unusual options activity detected by the deterministic pre-filter."""

    symbol: str
    detected_at: datetime
    contract: OptionContract | None = None
    # detection signals
    volume_oi_ratio: float = 0.0  # current vol / open interest
    premium_estimate: float = 0.0  # volume * mid * 100
    is_sweep: bool = False
    cluster_score: float = 0.0  # 0-100, how unusual the strike/expiry clustering is
    # classification
    flow_type: Literal["call_buy", "call_sell", "put_buy", "put_sell", "multi_leg", "unknown"] = "unknown"
    urgency_score: float = Field(default=0.0, ge=0.0, le=100.0)
    # enrichment (LLM second-pass)
    enrichment: dict[str, Any] | None = None


# --- Configuration Constants -------------------------------------------------

# Supported bar timeframes. Maps common names to alpaca TimeFrame values.
BAR_TIMEFRAMES: dict[str, str] = {
    "1m": "1Min",
    "5m": "5Min",
    "15m": "15Min",
    "30m": "30Min",
    "1h": "1Hour",
    "2h": "2Hour",
    "4h": "4Hour",
    "1d": "1Day",
    "1w": "1Week",
    "1M": "1Month",
}

# Options flow: volume/OI ratio thresholds for different urgency levels.
FLOW_VOLUME_OI_THRESHOLDS: dict[str, float] = {
    "elevated": 2.0,
    "unusual": 5.0,
    "extreme": 10.0,
}

# Minimum premium (volume * mid * 100) to flag as unusual, by tier.
FLOW_PREMIUM_THRESHOLDS: dict[str, float] = {
    "elevated": 500_000.0,
    "unusual": 2_000_000.0,
    "extreme": 10_000_000.0,
}
