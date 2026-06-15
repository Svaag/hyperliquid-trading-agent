from __future__ import annotations

import hashlib
import math
from typing import Any, Literal, cast

from hyperliquid_trading_agent.app.autonomy.schemas import (
    LiquidationCluster,
    MarketLevel,
    OrderflowState,
    PaperPosition,
)


def derive_candle_levels(symbol: str, candles: list[dict[str, Any]], timestamp_ms: int, timeframe: str = "1h") -> list[MarketLevel]:
    cleaned: list[tuple[int, float, float, float, float]] = []
    for item in candles:
        parsed = _candle(item)
        if parsed is not None:
            cleaned.append(parsed)
    if not cleaned:
        return []
    highs = [item[1] for item in cleaned]
    lows = [item[2] for item in cleaned]
    closes = [item[3] for item in cleaned]
    volumes = [item[4] for item in cleaned]
    levels: list[MarketLevel] = []
    recent_high = max(highs[-min(20, len(highs)) :])
    recent_low = min(lows[-min(20, len(lows)) :])
    levels.append(_level(symbol, "support", recent_low, 62, timeframe, "candles", timestamp_ms, {"method": "recent_low"}))
    levels.append(_level(symbol, "resistance", recent_high, 62, timeframe, "candles", timestamp_ms, {"method": "recent_high"}))
    if len(cleaned) >= 3:
        for index in range(1, len(cleaned) - 1):
            _ts, high, low, _close, _volume = cleaned[index]
            if high >= cleaned[index - 1][1] and high >= cleaned[index + 1][1]:
                levels.append(_level(symbol, "resistance", high, 54, timeframe, "candles", timestamp_ms, {"method": "pivot_high"}))
            if low <= cleaned[index - 1][2] and low <= cleaned[index + 1][2]:
                levels.append(_level(symbol, "support", low, 54, timeframe, "candles", timestamp_ms, {"method": "pivot_low"}))
    if len(cleaned) >= 24:
        prior = cleaned[-24:]
        levels.append(_level(symbol, "prior_high", max(item[1] for item in prior), 58, timeframe, "candles", timestamp_ms, {"method": "prior_window"}))
        levels.append(_level(symbol, "prior_low", min(item[2] for item in prior), 58, timeframe, "candles", timestamp_ms, {"method": "prior_window"}))
    vwap = _vwap(closes, volumes)
    if vwap is not None:
        latest = closes[-1]
        kind = "support" if vwap <= latest else "resistance"
        levels.append(_level(symbol, "vwap", vwap, 50, timeframe, "candles", timestamp_ms, {"method": "vwap", "acts_as": kind}))
    return dedupe_levels(levels)


def position_levels(positions: list[PaperPosition], timestamp_ms: int) -> list[MarketLevel]:
    levels: list[MarketLevel] = []
    for position in positions:
        if position.status != "open":
            continue
        kind = "support" if position.side == "long" else "resistance"
        levels.append(
            _level(
                position.symbol,
                kind,
                position.stop_px,
                70,
                "paper_position",
                "inferred",
                timestamp_ms,
                {"position_id": position.id, "level_role": "paper_stop", "source_label": "paper position stop"},
            )
        )
        if position.take_profit_px:
            tp_kind = "resistance" if position.side == "long" else "support"
            levels.append(
                _level(
                    position.symbol,
                    tp_kind,
                    position.take_profit_px,
                    58,
                    "paper_position",
                    "inferred",
                    timestamp_ms,
                    {"position_id": position.id, "level_role": "paper_take_profit"},
                )
            )
    return levels


def liquidity_levels(orderflow: OrderflowState | None) -> list[MarketLevel]:
    if orderflow is None:
        return []
    return [*orderflow.large_bid_walls, *orderflow.large_ask_walls]


def infer_liquidation_clusters(symbol: str, mid: float | None, levels: list[MarketLevel], orderflow: OrderflowState | None = None) -> list[LiquidationCluster]:
    clusters: list[LiquidationCluster] = []
    for level in levels:
        if level.source == "public_account" and level.kind == "liquidation_known":
            clusters.append(
                LiquidationCluster(
                    symbol=symbol.upper(),
                    price=level.price,
                    side_at_risk=_side_at_risk(level.metadata.get("side_at_risk")),
                    notional_usd_known=_float_or_none(level.metadata.get("notional_usd")),
                    confidence="direct",
                    source="public_account",
                    accounts=[str(account) for account in level.metadata.get("accounts", [])] if isinstance(level.metadata.get("accounts"), list) else [],
                    metadata={"level_id": level.id},
                )
            )
        elif level.kind in {"support", "resistance", "liquidity_wall", "prior_high", "prior_low"}:
            side_at_risk: Literal["longs", "shorts", "unknown"] = "longs" if level.price < (mid or level.price) else "shorts" if level.price > (mid or level.price) else "unknown"
            source: Literal["public_account", "market_structure", "orderbook"] = "orderbook" if level.source == "l2" else "market_structure"
            confidence: Literal["direct", "inferred_low", "inferred_medium"] = "inferred_medium" if level.strength >= 65 else "inferred_low"
            clusters.append(
                LiquidationCluster(
                    symbol=symbol.upper(),
                    price=level.price,
                    side_at_risk=side_at_risk,
                    confidence=confidence,
                    source=source,
                    metadata={"level_id": level.id, "important": "inferred, not directly observable stop/liquidation data"},
                )
            )
    if orderflow is not None:
        for wall in liquidity_levels(orderflow):
            if all(abs(wall.price - item.price) / max(wall.price, 1.0) > 0.0005 for item in clusters):
                clusters.append(
                    LiquidationCluster(
                        symbol=symbol.upper(),
                        price=wall.price,
                        side_at_risk="longs" if wall.price < (mid or wall.price) else "shorts",
                        confidence="inferred_medium",
                        source="orderbook",
                        metadata={"level_id": wall.id, "important": "inferred from visible L2 liquidity wall"},
                    )
                )
    return clusters[:12]


def dedupe_levels(levels: list[MarketLevel], bps: float = 5.0) -> list[MarketLevel]:
    ordered = sorted(levels, key=lambda item: item.strength, reverse=True)
    kept: list[MarketLevel] = []
    for level in ordered:
        duplicate = False
        for existing in kept:
            if existing.symbol != level.symbol or existing.timeframe != level.timeframe:
                continue
            if abs(existing.price - level.price) / max(existing.price, 1.0) * 10_000 <= bps:
                duplicate = True
                break
        if not duplicate:
            kept.append(level)
    return sorted(kept, key=lambda item: (item.symbol, item.price))


def _level(symbol: str, kind: str, price: float, strength: float, timeframe: str, source: str, timestamp_ms: int, metadata: dict[str, Any]) -> MarketLevel:
    return MarketLevel(
        id=_level_id(symbol, kind, price, timeframe, source),
        symbol=symbol.upper(),
        kind=kind,  # type: ignore[arg-type]
        price=float(price),
        strength=max(0.0, min(100.0, float(strength))),
        timeframe=timeframe,
        source=source,  # type: ignore[arg-type]
        first_seen_ms=timestamp_ms,
        last_seen_ms=timestamp_ms,
        metadata=metadata,
    )


def _level_id(symbol: str, kind: str, price: float, timeframe: str, source: str) -> str:
    digest = hashlib.sha1(f"{symbol}:{kind}:{timeframe}:{source}:{round(price, 8)}".encode()).hexdigest()[:16]
    return f"lvl_{digest}"


def _candle(item: dict[str, Any]) -> tuple[int, float, float, float, float] | None:
    try:
        ts = int(item.get("t") or item.get("T") or item.get("time") or item.get("timestamp") or 0)
        high_value = item.get("h") or item.get("high")
        low_value = item.get("l") or item.get("low")
        close_value = item.get("c") or item.get("close")
        if high_value is None or low_value is None or close_value is None:
            return None
        high = float(high_value)
        low = float(low_value)
        close = float(close_value)
        volume = float(item.get("v") or item.get("volume") or 0.0)
        if not all(math.isfinite(value) for value in [high, low, close, volume]):
            return None
        return ts, high, low, close, volume
    except (TypeError, ValueError):
        return None


def _vwap(closes: list[float], volumes: list[float]) -> float | None:
    volume_sum = sum(volumes)
    if volume_sum <= 0:
        return None
    return sum(close * volume for close, volume in zip(closes, volumes, strict=False)) / volume_sum


def _side_at_risk(value: Any) -> Literal["longs", "shorts", "unknown"]:
    text = str(value or "unknown")
    if text in {"longs", "shorts", "unknown"}:
        return cast(Literal["longs", "shorts", "unknown"], text)
    return "unknown"


def _float_or_none(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
