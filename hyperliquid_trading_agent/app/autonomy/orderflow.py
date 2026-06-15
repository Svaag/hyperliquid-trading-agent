from __future__ import annotations

import statistics
from typing import Any

from hyperliquid_trading_agent.app.autonomy.schemas import MarketLevel, OrderflowState

BookSide = list[tuple[float, float]]


def compute_orderflow_state(symbol: str, l2_book: Any, mid: float | None, timestamp_ms: int) -> OrderflowState:
    bids, asks = parse_l2_book(l2_book)
    best_bid = bids[0] if bids else None
    best_ask = asks[0] if asks else None
    spread_bps = None
    microprice = None
    top_depth = None
    imbalance_top = None
    if best_bid and best_ask:
        midpoint = mid or (best_bid[0] + best_ask[0]) / 2
        if midpoint > 0:
            spread_bps = (best_ask[0] - best_bid[0]) / midpoint * 10_000
        bid_top_usd = best_bid[0] * best_bid[1]
        ask_top_usd = best_ask[0] * best_ask[1]
        top_depth = bid_top_usd + ask_top_usd
        imbalance_top = _imbalance(bid_top_usd, ask_top_usd)
        size_sum = best_bid[1] + best_ask[1]
        if size_sum > 0:
            microprice = (best_ask[0] * best_bid[1] + best_bid[0] * best_ask[1]) / size_sum

    reference = mid or (best_bid[0] if best_bid else best_ask[0] if best_ask else None)
    bid_10 = _depth_usd(bids, reference, side="bid", bps=10)
    ask_10 = _depth_usd(asks, reference, side="ask", bps=10)
    bid_50 = _depth_usd(bids, reference, side="bid", bps=50)
    ask_50 = _depth_usd(asks, reference, side="ask", bps=50)
    large_bid_walls = _large_walls(symbol, bids, timestamp_ms, side="bid", reference=reference)
    large_ask_walls = _large_walls(symbol, asks, timestamp_ms, side="ask", reference=reference)
    return OrderflowState(
        spread_bps=spread_bps,
        top_depth_usd=top_depth,
        depth_10bps_bid_usd=bid_10,
        depth_10bps_ask_usd=ask_10,
        depth_50bps_bid_usd=bid_50,
        depth_50bps_ask_usd=ask_50,
        imbalance_top=imbalance_top,
        imbalance_10bps=_imbalance(bid_10, ask_10),
        microprice=microprice,
        large_bid_walls=large_bid_walls,
        large_ask_walls=large_ask_walls,
    )


def parse_l2_book(l2_book: Any) -> tuple[BookSide, BookSide]:
    if not isinstance(l2_book, dict):
        return [], []
    raw_levels = l2_book.get("levels") or l2_book.get("book") or []
    if not isinstance(raw_levels, list) or len(raw_levels) < 2:
        return [], []
    bids = _parse_side(raw_levels[0])
    asks = _parse_side(raw_levels[1])
    return sorted(bids, key=lambda item: item[0], reverse=True), sorted(asks, key=lambda item: item[0])


def _parse_side(raw: Any) -> BookSide:
    if not isinstance(raw, list):
        return []
    levels: BookSide = []
    for item in raw:
        parsed = _parse_level(item)
        if parsed is not None:
            levels.append(parsed)
    return levels


def _parse_level(item: Any) -> tuple[float, float] | None:
    try:
        if isinstance(item, dict):
            px = item.get("px") or item.get("price")
            sz = item.get("sz") or item.get("size")
            if px is None or sz is None:
                return None
            return float(px), float(sz)
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            return float(item[0]), float(item[1])
    except (TypeError, ValueError):
        return None
    return None


def _depth_usd(levels: BookSide, reference: float | None, *, side: str, bps: float) -> float | None:
    if reference is None or reference <= 0:
        return None
    if side == "bid":
        cutoff = reference * (1 - bps / 10_000)
        return sum(px * sz for px, sz in levels if px >= cutoff)
    cutoff = reference * (1 + bps / 10_000)
    return sum(px * sz for px, sz in levels if px <= cutoff)


def _imbalance(bid_usd: float | None, ask_usd: float | None) -> float | None:
    if bid_usd is None or ask_usd is None:
        return None
    total = bid_usd + ask_usd
    if total <= 0:
        return None
    return (bid_usd - ask_usd) / total


def _large_walls(symbol: str, levels: BookSide, timestamp_ms: int, *, side: str, reference: float | None) -> list[MarketLevel]:
    if not levels:
        return []
    notionals = [px * sz for px, sz in levels[:25]]
    if not notionals:
        return []
    median = statistics.median(notionals)
    total = sum(notionals)
    threshold = max(median * 2.5, total * 0.10)
    walls: list[MarketLevel] = []
    for px, sz in levels[:25]:
        notional = px * sz
        if notional < threshold:
            continue
        distance_bps = abs(px - reference) / reference * 10_000 if reference and reference > 0 else None
        strength = min(100.0, 35.0 + (notional / max(median, 1.0)) * 10.0)
        walls.append(
            MarketLevel(
                id=f"{symbol.lower()}:{side}_wall:{round(px, 8)}",
                symbol=symbol.upper(),
                kind="liquidity_wall",
                price=px,
                strength=strength,
                timeframe="l2",
                source="l2",
                first_seen_ms=timestamp_ms,
                last_seen_ms=timestamp_ms,
                metadata={"side": side, "size": sz, "notional_usd": notional, "distance_bps": distance_bps},
            )
        )
    return walls[:5]
