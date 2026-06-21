from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from hyperliquid_trading_agent.app.hip4.ids import parse_coin
from hyperliquid_trading_agent.app.hip4.schemas import ZERO, NormalizedOutcomeBook, PriceLevel


def parse_l2_book(coin: str, payload: Any, *, source: str = "rest", as_of_ms: int | None = None) -> NormalizedOutcomeBook:
    asset = parse_coin(coin)
    data = payload if isinstance(payload, dict) else {}
    levels = data.get("levels") if isinstance(data, dict) else None
    bids_raw: list[Any] = []
    asks_raw: list[Any] = []
    if isinstance(levels, list) and len(levels) >= 2:
        bids_raw = levels[0] if isinstance(levels[0], list) else []
        asks_raw = levels[1] if isinstance(levels[1], list) else []
    return NormalizedOutcomeBook(
        coin=coin,
        outcome_id=asset.outcome_id,
        side=asset.side,  # type: ignore[arg-type]
        bids=_parse_levels(bids_raw),
        asks=_parse_levels(asks_raw),
        as_of_ms=as_of_ms or int(data.get("time") or time.time() * 1000),
        source=source,  # type: ignore[arg-type]
        raw=data,
    )


def executable_vwap(levels: list[PriceLevel], target_size: Decimal) -> tuple[Decimal, Decimal]:
    """Return `(filled_size, avg_price)` for a taker consuming depth."""

    if target_size <= ZERO:
        return ZERO, ZERO
    remaining = target_size
    notional = ZERO
    filled = ZERO
    for level in levels:
        if remaining <= ZERO:
            break
        take = min(level.sz, remaining)
        if take <= ZERO:
            continue
        filled += take
        notional += take * level.px
        remaining -= take
    if filled <= ZERO:
        return ZERO, ZERO
    return filled, notional / filled


def total_size(levels: list[PriceLevel]) -> Decimal:
    total = ZERO
    for level in levels:
        total += level.sz
    return total


def book_is_fresh(book: NormalizedOutcomeBook, *, now_ms: int, max_staleness_ms: int) -> bool:
    return not book.stale and now_ms - int(book.as_of_ms) <= max_staleness_ms


def _parse_levels(raw: list[Any]) -> list[PriceLevel]:
    levels: list[PriceLevel] = []
    for item in raw:
        if isinstance(item, dict):
            px = item.get("px")
            sz = item.get("sz")
            n = item.get("n")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            px = item[0]
            sz = item[1]
            n = item[2] if len(item) > 2 else None
        else:
            continue
        try:
            level = PriceLevel(px=Decimal(str(px)), sz=Decimal(str(sz)), n=int(n) if n is not None else None)
        except Exception:
            continue
        if level.px > ZERO and level.sz > ZERO:
            levels.append(level)
    return levels
