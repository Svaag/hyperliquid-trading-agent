from __future__ import annotations

import re
from statistics import mean
from typing import Any

from hyperliquid_trading_agent.app.hyperliquid.risk_math import fixed_risk_position_size

NUMBER_RE = re.compile(r"(?P<label>entry|stop|sl|tp|take profit|target|equity|account|risk)\s*[:=]?\s*\$?(?P<value>\d+(?:\.\d+)?)", re.IGNORECASE)
ENTRY_PHRASE_RE = re.compile(r"\b(?:entered|bought|longed|shorted|got\s+in(?:to)?)\b.{0,80}?\bat\s+\$?(?P<value>\d+(?:\.\d+)?)", re.IGNORECASE)
STOP_PHRASE_RE = re.compile(r"\b(?:stop(?:\s+loss)?|sl)\b(?:\s+(?:is|at|around|near|to))?\s+\$?(?P<value>\d+(?:\.\d+)?)", re.IGNORECASE)
TARGET_PHRASE_RE = re.compile(r"\b(?:target|tp|take\s+profit|exit)\b(?:\s+(?:is|at|around|near|to|before))?\s+\$?(?P<value>\d+(?:\.\d+)?)", re.IGNORECASE)


def build_deterministic_features(prompt: str, tool_results: list[dict[str, Any]], request_overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    setup = parse_trade_setup(prompt)
    overrides = request_overrides or {}
    if overrides.get("account_equity_usd") is not None:
        setup["account_equity_usd"] = overrides["account_equity_usd"]
    if overrides.get("risk_pct") is not None:
        setup["risk_pct"] = overrides["risk_pct"]
    if overrides.get("account_address"):
        setup["account_address"] = str(overrides["account_address"]).lower()

    features: dict[str, Any] = {
        "parsed_setup": setup,
        "market": {},
        "candles": {},
        "funding": {},
        "order_book": {},
        "account": {},
        "risk": {},
        "tool_summary": [],
    }
    for result in tool_results:
        tool = str(result.get("tool", ""))
        features["tool_summary"].append(f"{tool} from {result.get('source')} at {result.get('timestamp_ms')}")
        data = result.get("data")
        if tool == "get_market_snapshot" and isinstance(data, dict):
            features["market"].update(_market_features(data))
            features["order_book"].update(_embedded_l2_features(data))
        elif tool == "get_candles":
            coin = _coin_from_result(data, result)
            features["candles"][coin] = _candle_features(data)
        elif tool == "get_funding_context" and isinstance(data, dict):
            coin = str(data.get("coin", "UNKNOWN"))
            features["funding"][coin] = _funding_features(data)
        elif tool == "get_public_user_state" and isinstance(data, dict):
            features["account"] = _account_features(data)
        elif tool.endswith("user_fills") or "fills" in tool.lower():
            features.setdefault("fills", {})[tool] = _fills_features(data)
        elif "fees" in tool.lower():
            features["account"]["fees"] = data
        elif "portfolio" in tool.lower():
            features["account"]["portfolio"] = data
        elif "ledger" in tool.lower():
            features["account"]["ledger_updates"] = _ledger_features(data)
        elif "vault_equities" in tool.lower():
            features["account"]["vault_equities"] = data
        elif "open_orders" in tool.lower() and isinstance(data, list):
            features["account"].setdefault("open_orders_detail", data[:20])
    features["risk"] = _risk_features(setup)
    features["execution"] = _execution_features(features)
    return features


def parse_trade_setup(prompt: str) -> dict[str, Any]:
    lowered = prompt.lower()
    side = "short" if "short" in lowered else "long" if "long" in lowered else None
    values: dict[str, float] = {}
    for match in NUMBER_RE.finditer(prompt):
        label = match.group("label").lower()
        value = float(match.group("value"))
        if label == "sl":
            label = "stop"
        if label in {"take profit", "target"}:
            label = "tp"
        if label == "account":
            label = "equity"
        values[label] = value
    if "entry" not in values:
        entry_match = ENTRY_PHRASE_RE.search(prompt)
        if entry_match:
            values["entry"] = float(entry_match.group("value"))
    if "stop" not in values:
        stop_match = STOP_PHRASE_RE.search(prompt)
        if stop_match:
            values["stop"] = float(stop_match.group("value"))
    if "tp" not in values:
        target_match = TARGET_PHRASE_RE.search(prompt)
        if target_match:
            values["tp"] = float(target_match.group("value"))
    entry = values.get("entry")
    stop = values.get("stop")
    if side is None and entry is not None and stop is not None:
        side = "long" if stop < entry else "short" if stop > entry else None
    return {
        "side": side,
        "entry": entry,
        "stop": stop,
        "take_profit": values.get("tp"),
        "account_equity_usd": values.get("equity"),
        "risk_pct": values.get("risk"),
        "timeframe": _infer_timeframe(prompt),
    }


def _market_features(data: dict[str, Any]) -> dict[str, Any]:
    assets = data.get("assets", {})
    if not isinstance(assets, dict):
        return {}
    out: dict[str, Any] = {}
    for coin, asset in assets.items():
        if not isinstance(asset, dict):
            continue
        ctx_candidate = asset.get("context")
        ctx: dict[str, Any] = ctx_candidate if isinstance(ctx_candidate, dict) else {}
        out[str(coin)] = {
            "query_symbol": asset.get("query_symbol"),
            "coin": asset.get("coin", coin),
            "kind": asset.get("kind"),
            "asset_id": asset.get("asset_id"),
            "mid": _float_or_none(asset.get("mid")),
            "mark": _float_or_none(ctx.get("markPx")),
            "oracle": _float_or_none(ctx.get("oraclePx")),
            "funding": _float_or_none(ctx.get("funding")),
            "open_interest": _float_or_none(ctx.get("openInterest")),
            "day_volume": _float_or_none(ctx.get("dayNtlVlm")),
            "day_base_volume": _float_or_none(ctx.get("dayBaseVlm")),
            "premium": _float_or_none(ctx.get("premium")),
            "prev_day_px": _float_or_none(ctx.get("prevDayPx")),
            "impact_pxs": ctx.get("impactPxs"),
            "mark_oracle_divergence_bps": _bps_diff(_float_or_none(ctx.get("markPx")), _float_or_none(ctx.get("oraclePx"))),
            "max_leverage": asset.get("max_leverage"),
            "sz_decimals": asset.get("sz_decimals"),
        }
    return out


def _embedded_l2_features(data: dict[str, Any]) -> dict[str, Any]:
    assets = data.get("assets", {})
    if not isinstance(assets, dict):
        return {}
    out: dict[str, Any] = {}
    for coin, asset in assets.items():
        if isinstance(asset, dict) and asset.get("l2"):
            out[str(coin)] = _l2_features(asset.get("l2"))
    return out


def _l2_features(book: Any) -> dict[str, Any]:
    if not isinstance(book, dict):
        return {}
    levels = book.get("levels")
    if not isinstance(levels, list) or len(levels) < 2:
        return {}
    bids = levels[0] if isinstance(levels[0], list) else []
    asks = levels[1] if isinstance(levels[1], list) else []
    best_bid = _level_px(bids[0]) if bids else None
    best_ask = _level_px(asks[0]) if asks else None
    bid_sz = sum(_level_sz(level) or 0.0 for level in bids[:5])
    ask_sz = sum(_level_sz(level) or 0.0 for level in asks[:5])
    mid = ((best_bid + best_ask) / 2) if best_bid is not None and best_ask is not None else None
    spread = best_ask - best_bid if best_bid is not None and best_ask is not None else None
    spread_bps = (spread / mid) * 10_000 if spread is not None and mid else None
    bid_notional = bid_sz * (best_bid or 0.0)
    ask_notional = ask_sz * (best_ask or 0.0)
    imbalance = (bid_sz - ask_sz) / (bid_sz + ask_sz) if bid_sz + ask_sz > 0 else None
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid_from_book": mid,
        "spread": spread,
        "spread_bps": spread_bps,
        "top5_bid_size": bid_sz,
        "top5_ask_size": ask_sz,
        "top5_bid_notional": bid_notional,
        "top5_ask_notional": ask_notional,
        "top5_imbalance": imbalance,
        "depth_warning": "thin_top5_depth" if min(bid_notional, ask_notional) < 50_000 else "",
    }


def _candle_features(data: Any) -> dict[str, Any]:
    candles: list[Any] = data if isinstance(data, list) else []
    closes = _filter_floats([_float_or_none(item.get("c")) for item in candles if isinstance(item, dict)])
    highs = _filter_floats([_float_or_none(item.get("h")) for item in candles if isinstance(item, dict)])
    lows = _filter_floats([_float_or_none(item.get("l")) for item in candles if isinstance(item, dict)])
    if len(closes) < 2:
        return {"count": len(closes), "trend": "insufficient_data"}
    first = closes[0]
    last = closes[-1]
    change_pct = ((last - first) / first) * 100 if first else None
    ranges = [high - low for high, low in zip(highs, lows, strict=False)]
    avg_range = mean(ranges) if ranges else None
    atr_pct = (avg_range / last) * 100 if avg_range and last else None
    returns = [((closes[idx] - closes[idx - 1]) / closes[idx - 1]) * 100 for idx in range(1, len(closes)) if closes[idx - 1]]
    trend = "up" if last > first else "down" if last < first else "flat"
    regime = "high_volatility" if atr_pct and atr_pct > 2 else "trend" if abs(change_pct or 0) > 2 else "range"
    recent_window = min(24, len(closes))
    momentum_window = min(12, len(closes) - 1)
    recent_lows = lows[-recent_window:] if lows else []
    recent_highs = highs[-recent_window:] if highs else []
    recent_change_pct = ((last - closes[-1 - momentum_window]) / closes[-1 - momentum_window]) * 100 if momentum_window > 0 and closes[-1 - momentum_window] else None
    last_3_change_pct = ((last - closes[-4]) / closes[-4]) * 100 if len(closes) >= 4 and closes[-4] else None
    return {
        "count": len(closes),
        "first_close": first,
        "last_close": last,
        "change_pct": change_pct,
        "recent_change_pct": recent_change_pct,
        "last_3_change_pct": last_3_change_pct,
        "avg_range": avg_range,
        "atr_proxy": avg_range,
        "atr_pct": atr_pct,
        "return_avg_pct": mean(returns) if returns else None,
        "return_abs_avg_pct": mean([abs(item) for item in returns]) if returns else None,
        "support": min(lows) if lows else None,
        "resistance": max(highs) if highs else None,
        "recent_support": min(recent_lows) if recent_lows else None,
        "recent_resistance": max(recent_highs) if recent_highs else None,
        "trend": trend,
        "regime": regime,
    }


def _funding_features(data: dict[str, Any]) -> dict[str, Any]:
    history_candidate = data.get("funding_history_48h")
    history: list[Any] = history_candidate if isinstance(history_candidate, list) else []
    rates = _filter_floats([_float_or_none(item.get("fundingRate")) for item in history if isinstance(item, dict)])
    latest = rates[-1] if rates else None
    average = mean(rates) if rates else None
    stress = "adverse_positive" if latest is not None and latest > 0.0005 else "adverse_negative" if latest is not None and latest < -0.0005 else "normal"
    return {"history_count": len(rates), "latest": latest, "average_48h": average, "funding_stress": stress, "predicted": data.get("predicted_fundings")}


def _account_features(data: dict[str, Any]) -> dict[str, Any]:
    perps_candidate = data.get("perps")
    perps: dict[str, Any] = perps_candidate if isinstance(perps_candidate, dict) else {}
    margin_candidate = perps.get("marginSummary")
    margin: dict[str, Any] = margin_candidate if isinstance(margin_candidate, dict) else {}
    positions_candidate = perps.get("assetPositions")
    positions: list[Any] = positions_candidate if isinstance(positions_candidate, list) else []
    open_orders_candidate = data.get("open_orders")
    open_orders: list[Any] = open_orders_candidate if isinstance(open_orders_candidate, list) else []
    account_value = _float_or_none(margin.get("accountValue"))
    total_margin_used = _float_or_none(margin.get("totalMarginUsed"))
    total_ntl_pos = _float_or_none(margin.get("totalNtlPos"))
    position_details = _position_details(positions)
    return {
        "account_value": account_value,
        "withdrawable": _float_or_none(perps.get("withdrawable")),
        "total_margin_used": total_margin_used,
        "total_notional_position": total_ntl_pos,
        "margin_utilization": (total_margin_used / account_value) if account_value and total_margin_used is not None else None,
        "notional_to_equity": (total_ntl_pos / account_value) if account_value and total_ntl_pos is not None else None,
        "position_count": len(positions),
        "positions": position_details,
        "open_order_count": len(open_orders),
        "open_orders_detail": open_orders[:20],
        "rate_limit": data.get("rate_limit"),
        "note": data.get("note"),
    }


def _risk_features(setup: dict[str, Any]) -> dict[str, Any]:
    entry = setup.get("entry")
    stop = setup.get("stop")
    take_profit = setup.get("take_profit")
    equity_is_assumed = setup.get("account_equity_usd") is None
    risk_pct_is_assumed = setup.get("risk_pct") is None
    equity = setup.get("account_equity_usd") or 10_000.0
    risk_pct = setup.get("risk_pct") or 1.0
    if entry is None or stop is None:
        return {
            "status": "needs_entry_and_stop",
            "risk_pct": risk_pct,
            "account_equity_usd": equity,
            "equity_is_assumed": equity_is_assumed,
            "risk_pct_is_assumed": risk_pct_is_assumed,
        }
    sizing = fixed_risk_position_size(float(equity), float(risk_pct), float(entry), float(stop))
    rr = None
    if take_profit is not None and not sizing.invalid:
        reward = abs(float(take_profit) - float(entry))
        risk = abs(float(entry) - float(stop))
        rr = reward / risk if risk else None
    return {
        "status": "invalid" if sizing.invalid else "ok",
        "reason": sizing.reason,
        "risk_usd": sizing.risk_usd,
        "size_units": sizing.size_units,
        "notional_usd": sizing.notional_usd,
        "risk_pct": risk_pct,
        "account_equity_usd": equity,
        "risk_reward_ratio": rr,
        "stop_distance_pct": (abs(float(entry) - float(stop)) / float(entry)) * 100 if entry else None,
        "equity_is_assumed": equity_is_assumed,
        "risk_pct_is_assumed": risk_pct_is_assumed,
    }


def _execution_features(features: dict[str, Any]) -> dict[str, Any]:
    risk = features.get("risk", {})
    coin = _first_coin(features)
    market = features.get("market", {}).get(coin, {}) if coin else {}
    book = features.get("order_book", {}).get(coin, {}) if coin else {}
    candles = features.get("candles", {}).get(coin, {}) if coin else {}
    notional = risk.get("notional_usd")
    stop_distance = risk.get("stop_distance_pct")
    atr_pct = candles.get("atr_pct")
    top_depth = min(book.get("top5_bid_notional") or 0.0, book.get("top5_ask_notional") or 0.0)
    estimated_slippage_bps = None
    if notional and top_depth:
        estimated_slippage_bps = (book.get("spread_bps") or 0.0) + min(100.0, (float(notional) / top_depth) * 10.0)
    return {
        "coin": coin,
        "asset_id": market.get("asset_id"),
        "asset_kind": market.get("kind"),
        "sz_decimals": market.get("sz_decimals"),
        "max_leverage": market.get("max_leverage"),
        "spread_bps": book.get("spread_bps"),
        "top_depth_notional": top_depth or None,
        "estimated_slippage_bps": estimated_slippage_bps,
        "stop_vs_atr": (stop_distance / atr_pct) if stop_distance is not None and atr_pct else None,
        "order_type_assumptions": ["manual confirmation", "limit preferred unless urgent", "IOC/market implies slippage", "post-only only if maker intent"],
        "trigger_assumptions": ["TP/SL trigger semantics must be manually verified", "reduce-only should be used for exits when reducing existing exposure"],
        "api_rate_limit_readiness": features.get("account", {}).get("rate_limit"),
    }


def _fills_features(data: Any) -> dict[str, Any]:
    fills = data if isinstance(data, list) else []
    pnls = _filter_floats([_float_or_none(item.get("closedPnl")) for item in fills if isinstance(item, dict)])
    fees = _filter_floats([_float_or_none(item.get("fee")) for item in fills if isinstance(item, dict)])
    return {"fill_count": len(fills), "closed_pnl_sum": sum(pnls), "fee_sum": sum(fees), "recent_fills": fills[:10]}


def _ledger_features(data: Any) -> dict[str, Any]:
    items = data if isinstance(data, list) else []
    return {"update_count": len(items), "recent_updates": items[:10]}


def _position_details(positions: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in positions:
        if not isinstance(item, dict):
            continue
        position_candidate = item.get("position")
        position: dict[str, Any] = position_candidate if isinstance(position_candidate, dict) else item
        out.append(
            {
                "coin": position.get("coin"),
                "szi": _float_or_none(position.get("szi")),
                "entry_px": _float_or_none(position.get("entryPx")),
                "liquidation_px": _float_or_none(position.get("liquidationPx")),
                "margin_used": _float_or_none(position.get("marginUsed")),
                "position_value": _float_or_none(position.get("positionValue")),
                "unrealized_pnl": _float_or_none(position.get("unrealizedPnl")),
                "return_on_equity": _float_or_none(position.get("returnOnEquity")),
                "leverage": position.get("leverage"),
            }
        )
    return out


def _first_coin(features: dict[str, Any]) -> str | None:
    market = features.get("market", {})
    if isinstance(market, dict) and market:
        return str(next(iter(market.keys())))
    return None


def _coin_from_result(data: Any, result: dict[str, Any]) -> str:
    if isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("s"):
        return str(data[0]["s"])
    input_candidate = result.get("input_json")
    input_json: dict[str, Any] = input_candidate if isinstance(input_candidate, dict) else {}
    return str(input_json.get("coin", "UNKNOWN"))


def _level_px(level: Any) -> float | None:
    if isinstance(level, dict):
        return _float_or_none(level.get("px"))
    if isinstance(level, list) and level:
        return _float_or_none(level[0])
    return None


def _level_sz(level: Any) -> float | None:
    if isinstance(level, dict):
        return _float_or_none(level.get("sz"))
    if isinstance(level, list) and len(level) > 1:
        return _float_or_none(level[1])
    return None


def _filter_floats(values: list[float | None]) -> list[float]:
    return [value for value in values if value is not None]


def _bps_diff(first: float | None, second: float | None) -> float | None:
    if first is None or second is None or second == 0:
        return None
    return ((first - second) / second) * 10_000


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _infer_timeframe(prompt: str) -> str | None:
    lowered = prompt.lower()
    for interval in ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d", "1w"]:
        if interval in lowered:
            return interval
    if "daily" in lowered:
        return "1d"
    if "swing" in lowered:
        return "4h"
    if "scalp" in lowered:
        return "15m"
    return None
