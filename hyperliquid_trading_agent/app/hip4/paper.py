from __future__ import annotations

import time
from decimal import Decimal
from typing import Any
from uuid import uuid4

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.hip4.mechanics import apply_action, apply_actions, residual_inventory
from hyperliquid_trading_agent.app.hip4.schemas import (
    ZERO,
    Hip4Candidate,
    Hip4PaperFill,
    Hip4PaperPortfolio,
    PaperNativeAction,
)


class Hip4PaperLedger:
    def __init__(self, *, settings: Settings, repository: Any | None = None, quote_token: str = "USDC"):
        self.settings = settings
        self.repository = repository
        self.quote_token = quote_token
        self.portfolio = Hip4PaperPortfolio(
            quote_token=quote_token,
            cash=settings.hip4_paper_initial_equity_usd,
            balances={quote_token: settings.hip4_paper_initial_equity_usd},
            updated_at_ms=int(time.time() * 1000),
        )
        self.actions: list[PaperNativeAction] = []
        self.fills: list[Hip4PaperFill] = []
        self.executed_candidate_ids: set[str] = set()

    async def execute_candidate(self, candidate: Hip4Candidate) -> dict[str, Any]:
        if not self.settings.hip4_paper_execution_enabled:
            raise PermissionError("HIP4_PAPER_EXECUTION_ENABLED is false")
        if candidate.candidate_id in self.executed_candidate_ids:
            raise ValueError("candidate already executed")
        if candidate.quote_token and candidate.quote_token != self.quote_token:
            raise ValueError("candidate quote token does not match HIP-4 paper ledger quote token")
        if candidate.residual_inventory and not self.settings.hip4_allow_inventory_carry:
            raise ValueError("risk-free paper execution cannot carry residual inventory")
        notional = abs(candidate.gross_cost_or_proceeds)
        if notional > self.settings.hip4_max_paper_notional_per_candidate_usd:
            raise ValueError("candidate notional exceeds HIP-4 paper cap")
        if self.portfolio.daily_notional + notional > self.settings.hip4_max_paper_daily_notional_usd:
            raise ValueError("daily HIP-4 paper notional cap exceeded")

        starting = dict(self.portfolio.balances)
        modeled_fee = candidate.size * candidate.fee_stress_bps / Decimal("10000")
        execution_actions = list(candidate.actions)
        if modeled_fee > ZERO:
            execution_actions.append(
                PaperNativeAction(
                    action_type="MARK_TO_BOOK",
                    amount=modeled_fee,
                    metadata={"quote_debit": self.quote_token, "reason": "fee_stress"},
                )
            )
        ending = apply_actions(starting, execution_actions, quote_token=self.quote_token)
        residual = residual_inventory(ending, quote_token=self.quote_token)
        if residual and not self.settings.hip4_allow_inventory_carry:
            raise ValueError("paper action path leaves residual inventory")

        now_ms = int(time.time() * 1000)
        for action in execution_actions:
            self.actions.append(action)
            fill = _fill_from_action(candidate.candidate_id, action, now_ms)
            if fill is not None:
                self.fills.append(fill)
        self.portfolio.balances = ending
        self.portfolio.cash = ending.get(self.quote_token, ZERO)
        self.portfolio.realized_pnl += candidate.expected_net_edge_usd
        self.portfolio.modeled_fees += modeled_fee
        self.portfolio.daily_notional += notional
        self.portfolio.updated_at_ms = now_ms
        self.executed_candidate_ids.add(candidate.candidate_id)
        await self._persist_execution(candidate, now_ms, execution_actions)
        return {"candidate_id": candidate.candidate_id, "portfolio": self.snapshot(), "fills": [fill.model_dump(mode="json") for fill in self.fills if fill.candidate_id == candidate.candidate_id]}

    def apply_settlement(self, action: PaperNativeAction) -> None:
        self.portfolio.balances = apply_action(self.portfolio.balances, action, quote_token=self.quote_token)
        self.portfolio.cash = self.portfolio.balances.get(self.quote_token, ZERO)
        self.portfolio.updated_at_ms = int(time.time() * 1000)
        self.actions.append(action)

    def reconcile(self) -> dict[str, Any]:
        rebuilt = {self.quote_token: self.settings.hip4_paper_initial_equity_usd}
        discrepancies: list[dict[str, str]] = []
        for action in self.actions:
            rebuilt = apply_action(rebuilt, action, quote_token=self.quote_token)
        keys = set(rebuilt) | set(self.portfolio.balances)
        for key in sorted(keys):
            expected = rebuilt.get(key, ZERO)
            actual = self.portfolio.balances.get(key, ZERO)
            if expected != actual:
                discrepancies.append({"token": key, "expected": str(expected), "actual": str(actual)})
        return {
            "run_id": f"hip4recon_{uuid4().hex}",
            "status": "ok" if not discrepancies else "mismatch",
            "discrepancies": discrepancies,
            "rebuilt_balances": {key: str(value) for key, value in rebuilt.items()},
            "stored_balances": {key: str(value) for key, value in self.portfolio.balances.items()},
            "created_at_ms": int(time.time() * 1000),
        }

    def snapshot(self) -> dict[str, Any]:
        return self.portfolio.model_dump(mode="json")

    def list_actions(self) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in self.actions]

    def list_fills(self) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in self.fills]

    async def _persist_execution(self, candidate: Hip4Candidate, now_ms: int, execution_actions: list[PaperNativeAction]) -> None:
        if self.repository is None or not getattr(self.repository, "enabled", False):
            return
        record = getattr(self.repository, "record_hip4_paper_execution", None)
        if callable(record):
            await record(
                {
                    "candidate": candidate.model_dump(mode="json"),
                    "portfolio": self.snapshot(),
                    "actions": [item.model_dump(mode="json") for item in execution_actions],
                    "fills": [item.model_dump(mode="json") for item in self.fills if item.candidate_id == candidate.candidate_id],
                    "created_at_ms": now_ms,
                }
            )


def _fill_from_action(candidate_id: str, action: PaperNativeAction, created_at_ms: int) -> Hip4PaperFill | None:
    if action.action_type not in {"BUY_SIDE_TOKEN", "SELL_SIDE_TOKEN"} or action.coin is None or action.price is None:
        return None
    side = "buy" if action.action_type == "BUY_SIDE_TOKEN" else "sell"
    notional = action.amount * action.price
    return Hip4PaperFill(
        fill_id=f"hip4fill_{uuid4().hex}",
        candidate_id=candidate_id,
        coin=action.coin,
        side=side,  # type: ignore[arg-type]
        size=action.amount,
        price=action.price,
        notional=notional,
        fee=ZERO,
        created_at_ms=created_at_ms,
    )
