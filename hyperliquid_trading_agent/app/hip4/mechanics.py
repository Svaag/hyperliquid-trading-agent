from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from hyperliquid_trading_agent.app.hip4.ids import balance_token
from hyperliquid_trading_agent.app.hip4.schemas import ZERO, PaperNativeAction

BalanceVector = dict[str, Decimal]


def apply_action(balances: BalanceVector, action: PaperNativeAction, *, quote_token: str = "USDC") -> BalanceVector:
    updated = defaultdict(Decimal, {key: Decimal(str(value)) for key, value in balances.items()})
    amount = Decimal(str(action.amount))
    if amount < ZERO:
        raise ValueError("HIP-4 action amount must be non-negative")

    if action.action_type == "SPLIT_OUTCOME":
        _require(action.outcome_id is not None, "split requires outcome_id")
        _debit(updated, quote_token, amount)
        updated[balance_token(action.outcome_id or 0, 0)] += amount
        updated[balance_token(action.outcome_id or 0, 1)] += amount
    elif action.action_type == "MERGE_OUTCOME":
        _require(action.outcome_id is not None, "merge requires outcome_id")
        _debit(updated, balance_token(action.outcome_id or 0, 0), amount)
        _debit(updated, balance_token(action.outcome_id or 0, 1), amount)
        updated[quote_token] += amount
    elif action.action_type == "NEGATE_OUTCOME":
        _require(action.outcome_id is not None, "negate requires outcome_id")
        outcome_ids = [int(item) for item in (action.metadata.get("question_outcome_ids") or [])]
        _require(outcome_ids, "negate requires question_outcome_ids metadata")
        _debit(updated, balance_token(action.outcome_id or 0, 1), amount)
        for outcome_id in outcome_ids:
            if outcome_id != action.outcome_id:
                updated[balance_token(outcome_id, 0)] += amount
    elif action.action_type == "MERGE_QUESTION":
        outcome_ids = [int(item) for item in (action.metadata.get("question_outcome_ids") or [])]
        _require(outcome_ids, "mergeQuestion requires question_outcome_ids metadata")
        for outcome_id in outcome_ids:
            _debit(updated, balance_token(outcome_id, 0), amount)
        updated[quote_token] += amount
    elif action.action_type == "BUY_SIDE_TOKEN":
        _require(action.outcome_id is not None and action.side is not None and action.price is not None, "buy requires outcome_id, side, price")
        cost = amount * Decimal(str(action.price))
        _debit(updated, quote_token, cost)
        updated[balance_token(action.outcome_id or 0, action.side or 0)] += amount
    elif action.action_type == "SELL_SIDE_TOKEN":
        _require(action.outcome_id is not None and action.side is not None and action.price is not None, "sell requires outcome_id, side, price")
        _debit(updated, balance_token(action.outcome_id or 0, action.side or 0), amount)
        updated[quote_token] += amount * Decimal(str(action.price))
    elif action.action_type == "SETTLE_OUTCOME":
        _require(action.outcome_id is not None and action.side is not None and action.price is not None, "settle requires outcome_id, side, settle price")
        _debit(updated, balance_token(action.outcome_id or 0, action.side or 0), amount)
        updated[quote_token] += amount * Decimal(str(action.price))
    elif action.action_type == "MARK_TO_BOOK":
        quote_debit = action.metadata.get("quote_debit")
        if quote_debit:
            _debit(updated, str(quote_debit), amount)
        quote_credit = action.metadata.get("quote_credit")
        if quote_credit:
            updated[str(quote_credit)] += amount
    else:  # pragma: no cover - pydantic Literal normally prevents this
        raise ValueError(f"unsupported HIP-4 action type: {action.action_type}")
    return _clean(dict(updated))


def apply_actions(balances: BalanceVector, actions: list[PaperNativeAction], *, quote_token: str = "USDC") -> BalanceVector:
    updated = dict(balances)
    for action in actions:
        updated = apply_action(updated, action, quote_token=quote_token)
    return updated


def residual_inventory(balances: BalanceVector, *, quote_token: str = "USDC") -> BalanceVector:
    return {key: value for key, value in _clean(balances).items() if key != quote_token and value != ZERO}


def _debit(balances: defaultdict[str, Decimal], key: str, amount: Decimal) -> None:
    if balances[key] < amount:
        raise ValueError(f"insufficient HIP-4 paper balance for {key}")
    balances[key] -= amount


def _clean(balances: BalanceVector) -> BalanceVector:
    return {key: value for key, value in balances.items() if value != ZERO}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)
