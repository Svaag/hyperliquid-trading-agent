"""Equity paper trading simulation — separate from crypto paper portfolio."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class EquityPaperPortfolio(BaseModel):
    """A standalone paper portfolio for equities (stocks only; options deferred)."""

    id: str = Field(default_factory=lambda: uuid4().hex)
    name: str = "equity_paper"
    status: str = "active"
    initial_equity_usd: float = 100_000.0
    cash_usd: float = 100_000.0
    realized_pnl_usd: float = 0.0

    @property
    def equity_usd(self) -> float:
        # Open-position MTM is added by EquityPaperSimulator.snapshot().
        # cash_usd already includes realized PnL adjustments, so do not add
        # realized_pnl_usd again here.
        return self.cash_usd


class EquityPaperOrder(BaseModel):
    """A paper order for an equity (stock only; options deferred)."""

    id: str = Field(default_factory=lambda: uuid4().hex)
    portfolio_id: str
    signal_id: str | None = None
    symbol: str
    side: str  # long / short
    order_type: str = "market"
    status: str = "pending"
    quantity: float  # number of shares
    requested_px: float | None = None
    filled_px: float | None = None
    stop_px: float | None = None
    take_profit_px: float | None = None
    fee_bps: float = 2.0  # equity commissions are negligible
    slippage_bps: float = 1.0
    filled_at: datetime | None = None
    cancelled_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EquityPaperFill(BaseModel):
    """A paper fill for equity orders."""

    id: str = Field(default_factory=lambda: uuid4().hex)
    order_id: str
    portfolio_id: str
    symbol: str
    side: str
    quantity: float
    price: float
    fee_usd: float = 0.0
    slippage_usd: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class EquityPaperPosition(BaseModel):
    """An open paper equity position."""

    id: str = Field(default_factory=lambda: uuid4().hex)
    portfolio_id: str
    signal_id: str | None = None
    symbol: str
    side: str
    status: str = "open"  # open / closed
    quantity: float  # shares
    avg_entry_px: float
    mark_px: float | None = None
    stop_px: float | None = None
    take_profit_px: float | None = None
    realized_pnl_usd: float = 0.0
    unrealized_pnl_usd: float = 0.0
    opened_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EquityPortfolioSnapshot(BaseModel):
    """Periodic snapshot of equity paper portfolio state."""

    id: str = Field(default_factory=lambda: uuid4().hex)
    portfolio_id: str
    timestamp_ms: int
    cash_usd: float
    equity_usd: float
    gross_exposure_usd: float = 0.0
    net_exposure_usd: float = 0.0
    realized_pnl_usd: float = 0.0
    unrealized_pnl_usd: float = 0.0
    total_pnl_usd: float = 0.0
    metrics: dict[str, Any] = Field(default_factory=dict)


class EquityTradeRequest(BaseModel):
    """Request to open a paper equity position."""

    symbol: str
    side: str  # long / short
    quantity: float | None = None  # if None, size from risk parameters
    entry: float | None = None
    stop: float | None = None
    take_profit: float | None = None
    account_equity_usd: float | None = None
    risk_pct: float = 1.0
    signal_id: str | None = None
    thesis: str = ""


class EquityRiskControlError(ValueError):
    """Raised when a trade violates risk limits."""
