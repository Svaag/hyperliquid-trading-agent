from __future__ import annotations

import statistics
import time
from collections import defaultdict, deque
from collections.abc import Iterable
from itertools import pairwise
from typing import Any, Literal

from hyperliquid_trading_agent.app.autonomy.levels import (
    dedupe_levels,
    derive_candle_levels,
    infer_liquidation_clusters,
    liquidity_levels,
    position_levels,
)
from hyperliquid_trading_agent.app.autonomy.orderflow import compute_orderflow_state
from hyperliquid_trading_agent.app.autonomy.schemas import (
    AssetMarketState,
    AssetNewsState,
    GlobalMarketMap,
    MarketAsset,
    MarketLevel,
    NewsEvent,
    PaperPosition,
)


def now_ms() -> int:
    return int(time.time() * 1000)


class MarketMapReducer:
    """Deterministic reducer for the autonomy market mental map."""

    def __init__(self, max_history: int = 720):
        self.map = GlobalMarketMap(timestamp_ms=0)
        self.assets: dict[str, MarketAsset] = {}
        self.price_history: dict[str, deque[tuple[int, float]]] = defaultdict(lambda: deque(maxlen=max_history))
        self.candle_levels: dict[str, list[MarketLevel]] = defaultdict(list)
        self.paper_levels: dict[str, list[MarketLevel]] = defaultdict(list)
        self.news_by_symbol: dict[str, list[NewsEvent]] = defaultdict(list)

    def set_universe(self, assets: Iterable[MarketAsset], timestamp_ms: int | None = None) -> GlobalMarketMap:
        ts = timestamp_ms or now_ms()
        for asset in assets:
            symbol = asset.symbol.upper()
            self.assets[symbol] = asset
            existing = self.map.assets.get(symbol)
            if existing is None:
                self.map.assets[symbol] = AssetMarketState(
                    symbol=symbol,
                    timestamp_ms=ts,
                    day_volume_usd=asset.day_volume_usd,
                    metadata={"asset": asset.model_dump(mode="json")},
                )
            else:
                self.map.assets[symbol] = existing.model_copy(
                    update={
                        "timestamp_ms": ts,
                        "day_volume_usd": asset.day_volume_usd or existing.day_volume_usd,
                        "metadata": {**existing.metadata, "asset": asset.model_dump(mode="json")},
                    }
                )
        return self._refresh_global(ts)

    def apply_all_mids(self, mids: dict[str, str | float], timestamp_ms: int | None = None) -> GlobalMarketMap:
        ts = timestamp_ms or now_ms()
        for raw_symbol, raw_px in mids.items():
            symbol = str(raw_symbol).upper()
            if self.assets and symbol not in self.assets:
                continue
            px = _float(raw_px)
            if px is None or px <= 0:
                continue
            self.price_history[symbol].append((ts, px))
            state = self._asset_state(symbol, ts)
            trend = _trend(self.price_history[symbol])
            volatility = _volatility_regime(self.price_history[symbol])
            self.map.assets[symbol] = state.model_copy(
                update={
                    "timestamp_ms": ts,
                    "mid": px,
                    "trend": trend,
                    "volatility_regime": volatility,
                    "regime_score": _regime_score(trend, volatility, state.funding_hourly, state.orderflow),
                }
            )
        return self._refresh_global(ts)

    def apply_asset_contexts(self, meta_and_ctxs: Any, timestamp_ms: int | None = None) -> GlobalMarketMap:
        ts = timestamp_ms or now_ms()
        universe, contexts = _split_meta_and_contexts(meta_and_ctxs)
        for raw, ctx in zip(universe, contexts, strict=False):
            symbol = str(raw.get("name") or ctx.get("coin") or "").upper()
            if not symbol or (self.assets and symbol not in self.assets):
                continue
            state = self._asset_state(symbol, ts)
            mark = _float(ctx.get("markPx"))
            oracle = _float(ctx.get("oraclePx"))
            funding = _float(ctx.get("funding"))
            oi = _float(ctx.get("openInterest"))
            day_volume = _float(ctx.get("dayNtlVlm"))
            self.map.assets[symbol] = state.model_copy(
                update={
                    "timestamp_ms": ts,
                    "mark": mark if mark is not None else state.mark,
                    "oracle": oracle if oracle is not None else state.oracle,
                    "funding_hourly": funding if funding is not None else state.funding_hourly,
                    "open_interest": oi if oi is not None else state.open_interest,
                    "day_volume_usd": day_volume if day_volume is not None else state.day_volume_usd,
                }
            )
        return self._refresh_global(ts)

    def apply_l2_book(self, symbol: str, l2_book: Any, timestamp_ms: int | None = None) -> GlobalMarketMap:
        ts = timestamp_ms or now_ms()
        symbol = symbol.upper()
        state = self._asset_state(symbol, ts)
        orderflow = compute_orderflow_state(symbol, l2_book, state.mid, ts)
        levels = dedupe_levels([*self.candle_levels.get(symbol, []), *liquidity_levels(orderflow), *self.paper_levels.get(symbol, [])])
        supports = [level for level in levels if _supportish(level, state.mid)]
        resistances = [level for level in levels if _resistanceish(level, state.mid)]
        self.map.assets[symbol] = state.model_copy(
            update={
                "timestamp_ms": ts,
                "orderflow": orderflow,
                "support_levels": supports[:12],
                "resistance_levels": resistances[:12],
                "liquidity_levels": liquidity_levels(orderflow),
                "liquidation_clusters": infer_liquidation_clusters(symbol, state.mid, levels, orderflow),
                "regime_score": _regime_score(state.trend, state.volatility_regime, state.funding_hourly, orderflow),
            }
        )
        return self._refresh_global(ts)

    def apply_candles(self, symbol: str, candles: list[dict[str, Any]], timeframe: str, timestamp_ms: int | None = None) -> GlobalMarketMap:
        ts = timestamp_ms or now_ms()
        symbol = symbol.upper()
        derived = derive_candle_levels(symbol, candles, ts, timeframe=timeframe)
        previous = [level for level in self.candle_levels.get(symbol, []) if level.timeframe != timeframe]
        self.candle_levels[symbol] = dedupe_levels([*previous, *derived])
        state = self._asset_state(symbol, ts)
        levels = dedupe_levels([*self.candle_levels[symbol], *self.paper_levels.get(symbol, [])])
        self.map.assets[symbol] = state.model_copy(
            update={
                "timestamp_ms": ts,
                "support_levels": [level for level in levels if _supportish(level, state.mid)][:12],
                "resistance_levels": [level for level in levels if _resistanceish(level, state.mid)][:12],
                "liquidation_clusters": infer_liquidation_clusters(symbol, state.mid, levels, state.orderflow),
            }
        )
        return self._refresh_global(ts)

    def apply_paper_positions(self, positions: list[PaperPosition], timestamp_ms: int | None = None) -> GlobalMarketMap:
        ts = timestamp_ms or now_ms()
        by_symbol: dict[str, list[PaperPosition]] = defaultdict(list)
        for position in positions:
            by_symbol[position.symbol.upper()].append(position)
        for symbol, symbol_positions in by_symbol.items():
            self.paper_levels[symbol] = position_levels(symbol_positions, ts)
            state = self._asset_state(symbol, ts)
            levels = dedupe_levels([*self.candle_levels.get(symbol, []), *self.paper_levels[symbol]])
            self.map.assets[symbol] = state.model_copy(
                update={
                    "timestamp_ms": ts,
                    "support_levels": [level for level in levels if _supportish(level, state.mid)][:12],
                    "resistance_levels": [level for level in levels if _resistanceish(level, state.mid)][:12],
                }
            )
        return self._refresh_global(ts)

    def apply_news(self, events: list[NewsEvent], timestamp_ms: int | None = None) -> GlobalMarketMap:
        ts = timestamp_ms or now_ms()
        for event in events:
            for symbol in event.assets:
                symbol = symbol.upper()
                existing = {item.id: item for item in self.news_by_symbol[symbol]}
                existing[event.id] = event
                self.news_by_symbol[symbol] = sorted(existing.values(), key=lambda item: item.observed_at_ms, reverse=True)[:20]
                sentiment = _news_sentiment(self.news_by_symbol[symbol])
                state = self._asset_state(symbol, ts)
                news_state = AssetNewsState(
                    latest_events=self.news_by_symbol[symbol][:8],
                    max_importance_score=max((item.importance_score for item in self.news_by_symbol[symbol]), default=0.0),
                    sentiment=sentiment,
                    updated_at_ms=ts,
                )
                self.map.assets[symbol] = state.model_copy(update={"timestamp_ms": ts, "news_state": news_state})
        return self._refresh_global(ts)

    def snapshot(self) -> GlobalMarketMap:
        return self.map

    def _asset_state(self, symbol: str, timestamp_ms: int) -> AssetMarketState:
        return self.map.assets.get(symbol.upper()) or AssetMarketState(symbol=symbol.upper(), timestamp_ms=timestamp_ms)

    def _refresh_global(self, timestamp_ms: int) -> GlobalMarketMap:
        changes: dict[str, float] = {}
        for symbol, history in self.price_history.items():
            if len(history) >= 2 and history[0][1] > 0:
                changes[symbol] = (history[-1][1] - history[0][1]) / history[0][1]
        leaders = [symbol for symbol, _change in sorted(changes.items(), key=lambda item: item[1], reverse=True)[:5]]
        laggards = [symbol for symbol, _change in sorted(changes.items(), key=lambda item: item[1])[:5]]
        btc_change = changes.get("BTC")
        btc_beta_notes = {symbol: round(change / btc_change, 3) for symbol, change in changes.items() if symbol != "BTC" and btc_change is not None and btc_change != 0}
        risk_regime = _risk_regime(self.map.assets)
        key_themes = _key_themes(self.map.assets)
        self.map = self.map.model_copy(
            update={
                "timestamp_ms": timestamp_ms,
                "risk_regime": risk_regime,
                "leaders": leaders,
                "laggards": laggards,
                "btc_beta_notes": btc_beta_notes,
                "correlated_clusters": [leaders[:3]] if len(leaders) >= 3 else [],
                "key_themes": key_themes,
            }
        )
        return self.map


def _split_meta_and_contexts(meta_and_ctxs: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if isinstance(meta_and_ctxs, list) and len(meta_and_ctxs) >= 2:
        meta = meta_and_ctxs[0] if isinstance(meta_and_ctxs[0], dict) else {}
        contexts = meta_and_ctxs[1] if isinstance(meta_and_ctxs[1], list) else []
        universe = meta.get("universe", []) if isinstance(meta.get("universe"), list) else []
        return [item for item in universe if isinstance(item, dict)], [item for item in contexts if isinstance(item, dict)]
    return [], []


def _trend(history: deque[tuple[int, float]]) -> Literal["up", "down", "range", "unknown"]:
    if len(history) < 3:
        return "unknown"
    first = history[0][1]
    last = history[-1][1]
    if first <= 0:
        return "unknown"
    change_bps = (last - first) / first * 10_000
    if change_bps > 35:
        return "up"
    if change_bps < -35:
        return "down"
    return "range"


def _volatility_regime(history: deque[tuple[int, float]]) -> Literal["low", "normal", "high", "unknown"]:
    if len(history) < 4:
        return "unknown"
    returns = []
    prices = [item[1] for item in history]
    for previous, current in pairwise(prices):
        if previous > 0:
            returns.append((current - previous) / previous)
    if len(returns) < 3:
        return "unknown"
    vol = statistics.pstdev(returns) * (len(returns) ** 0.5)
    if vol < 0.003:
        return "low"
    if vol > 0.02:
        return "high"
    return "normal"


def _regime_score(trend: str, volatility: str, funding: float | None, orderflow: Any) -> float:
    score = 50.0
    if trend == "up":
        score += 18
    elif trend == "down":
        score -= 18
    if volatility == "high":
        score -= 8
    elif volatility == "low":
        score += 4
    if funding is not None:
        score -= min(12.0, abs(funding) * 100_000) if abs(funding) > 0.0003 else 0
    imbalance = getattr(orderflow, "imbalance_10bps", None)
    if imbalance is not None:
        score += max(-10.0, min(10.0, imbalance * 10))
    return max(0.0, min(100.0, score))


def _supportish(level: MarketLevel, mid: float | None) -> bool:
    if level.kind in {"support", "prior_low", "vwap"} and (mid is None or level.price <= mid * 1.01):
        return True
    return level.kind == "liquidity_wall" and str(level.metadata.get("side")) == "bid"


def _resistanceish(level: MarketLevel, mid: float | None) -> bool:
    if level.kind in {"resistance", "prior_high", "vwap"} and (mid is None or level.price >= mid * 0.99):
        return True
    return level.kind == "liquidity_wall" and str(level.metadata.get("side")) == "ask"


def _risk_regime(assets: dict[str, AssetMarketState]) -> Literal["risk_on", "risk_off", "mixed", "unknown"]:
    majors = [assets[symbol].trend for symbol in ("BTC", "ETH") if symbol in assets]
    if majors and all(item == "up" for item in majors):
        return "risk_on"
    if majors and all(item == "down" for item in majors):
        return "risk_off"
    if majors:
        return "mixed"
    return "unknown"


def _key_themes(assets: dict[str, AssetMarketState]) -> list[str]:
    themes: list[str] = []
    if any((asset.news_state and asset.news_state.max_importance_score >= 75) for asset in assets.values()):
        themes.append("high-importance news active")
    if any(abs(asset.funding_hourly or 0.0) > 0.0005 for asset in assets.values()):
        themes.append("funding stress")
    if any(asset.volatility_regime == "high" for asset in assets.values()):
        themes.append("high realized volatility")
    return themes[:5]


def _news_sentiment(events: list[NewsEvent]) -> Literal["bullish", "bearish", "mixed", "unknown"]:
    bullish = sum(item.importance_score for item in events if item.sentiment == "bullish")
    bearish = sum(item.importance_score for item in events if item.sentiment == "bearish")
    if bullish > bearish * 1.25 and bullish > 0:
        return "bullish"
    if bearish > bullish * 1.25 and bearish > 0:
        return "bearish"
    if bullish or bearish:
        return "mixed"
    return "unknown"


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
