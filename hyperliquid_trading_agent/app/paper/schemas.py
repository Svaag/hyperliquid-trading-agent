from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


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


class PaperTradeDraftRequest(BaseModel):
    symbol: str
    side: Literal["long", "short"]
    entry: float | None = Field(default=None, gt=0)
    market: bool = False
    stop: float = Field(gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    risk_pct: float | None = Field(default=None, gt=0)
    quantity: float | None = Field(default=None, gt=0)
    thesis: str = ""
    actor: str = "api"
    source: str = "api"
    proposal_id: str | None = None
    close_opposite: bool = False
    metadata: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_trade_shape(self) -> "PaperTradeDraftRequest":
        if self.entry is None and not self.market:
            raise ValueError("entry price or market=true is required")
        if self.risk_pct is not None and self.quantity is not None:
            raise ValueError("provide risk_pct or quantity, not both")
        if self.side == "long" and self.stop >= (self.entry or self.stop + 1):
            if self.entry is not None:
                raise ValueError("long stop must be below entry")
        if self.side == "short" and self.stop <= (self.entry or self.stop - 1):
            if self.entry is not None:
                raise ValueError("short stop must be above entry")
        return self


class PaperTradeConfirmRequest(BaseModel):
    actor: str = "api"
    mid: float | None = Field(default=None, gt=0)
    close_opposite: bool = False


class PaperTradeCancelRequest(BaseModel):
    actor: str = "api"
    reason: str = "cancelled"


class PaperPositionCloseRequest(BaseModel):
    actor: str = "api"
    price: float | None = Field(default=None, gt=0)
    reason: str = "manual"
