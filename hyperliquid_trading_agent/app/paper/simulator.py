from __future__ import annotations

from hyperliquid_trading_agent.app.hyperliquid.risk_math import fixed_risk_position_size
from hyperliquid_trading_agent.app.paper.schemas import PaperTradePlan, PaperTradeRequest


class PaperTradeSimulator:
    """Local paper-trading calculator; no exchange calls."""

    def plan(self, request: PaperTradeRequest) -> PaperTradePlan:
        sizing = fixed_risk_position_size(
            account_equity_usd=request.account_equity_usd,
            risk_pct=request.risk_pct,
            entry=request.entry,
            stop=request.stop,
        )
        if sizing.invalid:
            raise ValueError(sizing.reason)
        return PaperTradePlan(
            coin=request.coin,
            side=request.side,
            entry=request.entry,
            stop=request.stop,
            take_profit=request.take_profit,
            risk_usd=sizing.risk_usd,
            size_units=sizing.size_units,
            notional_usd=sizing.notional_usd,
            invalidation=f"{request.side} invalidates at stop {request.stop}",
        )
