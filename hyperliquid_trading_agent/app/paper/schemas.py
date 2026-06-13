from __future__ import annotations

from pydantic import BaseModel, Field


class PaperTradeRequest(BaseModel):
    coin: str
    side: str = Field(pattern="^(long|short)$")
    entry: float
    stop: float
    take_profit: float | None = None
    account_equity_usd: float
    risk_pct: float = 1.0
    thesis: str = ""


class PaperTradePlan(BaseModel):
    coin: str
    side: str
    entry: float
    stop: float
    take_profit: float | None
    risk_usd: float
    size_units: float
    notional_usd: float
    invalidation: str
