from __future__ import annotations

import hashlib
from typing import Any

from hyperliquid_trading_agent.app.engine.event_ledger import now_ms
from hyperliquid_trading_agent.app.engine.schemas import AllocationDecision, AlphaCandidate, EVEstimate, RegimeVector


class PortfolioAllocator:
    def __init__(
        self,
        *,
        min_net_ev_bps: float = 8.0,
        min_risk_adjusted_utility: float = 0.25,
        max_single_name_exposure_pct: float = 20.0,
        risk_pct_per_trade: float = 0.25,
        repository: Any | None = None,
    ):
        self.min_net_ev_bps = min_net_ev_bps
        self.min_risk_adjusted_utility = min_risk_adjusted_utility
        self.max_single_name_exposure_pct = max_single_name_exposure_pct
        self.risk_pct_per_trade = risk_pct_per_trade
        self.repository = repository

    async def allocate(
        self,
        candidate: AlphaCandidate,
        ev: EVEstimate,
        *,
        regime: RegimeVector | None = None,
        portfolio_state: dict[str, Any] | None = None,
        candidate_book_id: str | None = None,
    ) -> AllocationDecision:
        portfolio_state = portfolio_state or {}
        equity = float(portfolio_state.get("equity_usd") or portfolio_state.get("initial_equity_usd") or 100_000)
        reason_codes: list[str] = []
        status = "allocate"
        if candidate.side == "flat":
            reason_codes.append("defensive_flat_no_trade")
            status = "skip"
        if ev.net_ev_bps < self.min_net_ev_bps:
            reason_codes.append("net_ev_below_minimum")
            status = "skip"
        if ev.risk_adjusted_utility < self.min_risk_adjusted_utility:
            reason_codes.append("risk_adjusted_utility_below_minimum")
            status = "skip"
        if regime is not None and not _strategy_allowed(candidate.strategy_id, regime):
            reason_codes.append("regime_disallows_strategy")
            status = "skip"
        stop_loss_bps = max(abs(candidate.proposed_entry - candidate.stop) / candidate.proposed_entry * 10_000, 1.0)
        risk_budget = equity * self.risk_pct_per_trade / 100.0
        risk_sized_notional = risk_budget / (stop_loss_bps / 10_000.0)
        single_name_cap = equity * self.max_single_name_exposure_pct / 100.0
        allocated_notional = min(risk_sized_notional, single_name_cap)
        if status == "skip":
            allocated_notional = 0.0
            risk_budget = 0.0
        size = allocated_notional / candidate.proposed_entry if candidate.proposed_entry > 0 else 0.0
        digest = hashlib.sha1(f"{candidate.candidate_id}:{ev.estimate_id}:{candidate_book_id}".encode()).hexdigest()[:24]
        allocation = AllocationDecision(
            allocation_id="alloc_" + digest,
            candidate_id=candidate.candidate_id,
            candidate_book_id=candidate_book_id,
            status=status,  # type: ignore[arg-type]
            allocated_size=size,
            allocated_notional_usd=allocated_notional,
            risk_usd=risk_budget,
            max_size_multiplier=1.0,
            constraints={
                "equity_usd": equity,
                "risk_pct_per_trade": self.risk_pct_per_trade,
                "stop_loss_bps": stop_loss_bps,
                "max_single_name_exposure_pct": self.max_single_name_exposure_pct,
                "single_name_cap_usd": single_name_cap,
            },
            reason_codes=reason_codes,
            created_at_ms=now_ms(),
        )
        if self.repository is not None and getattr(self.repository, "enabled", False):
            record = getattr(self.repository, "record_allocation_decision", None)
            if callable(record):
                await record(allocation.model_dump(mode="json"))
        return allocation


def _strategy_allowed(strategy_id: str, regime: RegimeVector) -> bool:
    permissions = regime.permissions
    if "momentum" in strategy_id:
        return permissions.momentum_allowed
    if "reversion" in strategy_id:
        return permissions.mean_reversion_allowed
    if "microstructure" in strategy_id or "ofi" in strategy_id:
        return permissions.market_making_allowed or regime.spread_state != "wide"
    if "news" in strategy_id:
        return permissions.news_event_allowed
    if "carry" in strategy_id:
        return permissions.carry_allowed
    return True
