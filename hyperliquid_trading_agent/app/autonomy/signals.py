from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from hyperliquid_trading_agent.app.autonomy.schemas import (
    AssetMarketState,
    GlobalMarketMap,
    ModelMarketInsight,
    PaperPosition,
    SignalEvidence,
    TradeSignal,
)
from hyperliquid_trading_agent.app.config import Settings

SIGNAL_TYPES = {
    "breakout_retest",
    "support_bounce",
    "resistance_rejection",
    "liquidation_sweep_reversal",
    "funding_oi_squeeze",
    "news_catalyst_momentum",
    "trend_continuation",
    "risk_off_deleveraging",
}


class SignalEngine:
    """Deterministic alpha candidate generator and scoring contract."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def generate(
        self,
        market_map: GlobalMarketMap,
        *,
        existing_signals: list[TradeSignal] | None = None,
        open_positions: list[PaperPosition] | None = None,
        signals_today: int = 0,
        timestamp_ms: int | None = None,
    ) -> list[TradeSignal]:
        ts = timestamp_ms or int(time.time() * 1000)
        existing = existing_signals or []
        positions = open_positions or []
        if signals_today >= self.settings.autonomy_max_signals_per_day:
            return []
        candidates: list[TradeSignal] = []
        for state in market_map.assets.values():
            if state.mid is None or state.mid <= 0:
                continue
            for side in ("long", "short"):
                signal = self._candidate(state, market_map, side, ts)
                if signal is None:
                    continue
                vetoes = risk_vetoes(signal, state, existing, positions)
                if vetoes:
                    continue
                if signal.score >= self.settings.autonomy_min_signal_score:
                    candidates.append(signal)
        candidates.sort(key=lambda item: item.score, reverse=True)
        remaining = max(0, self.settings.autonomy_max_signals_per_day - signals_today)
        return candidates[:remaining]

    def _candidate(self, state: AssetMarketState, market_map: GlobalMarketMap, side: str, timestamp_ms: int) -> TradeSignal | None:
        assert side in {"long", "short"}
        entry = state.mid
        if entry is None or entry <= 0:
            return None
        signal_type = _signal_type(state, market_map, side)
        if signal_type is None:
            return None
        stop, take_profit = _levels_for_side(state, side, entry)
        if stop <= 0 or stop == entry:
            return None
        rr = risk_reward(side, entry, stop, take_profit)
        evidence, score = score_components(state, market_map, side, rr)
        confidence = max(0.0, min(1.0, 0.35 + score / 150.0))
        ttl_ms = self.settings.autonomy_signal_ttl_minutes * 60 * 1000
        symbol = state.symbol.upper()
        return TradeSignal(
            id=_signal_id(symbol, side, signal_type, timestamp_ms, entry, stop),
            symbol=symbol,
            side=side,  # type: ignore[arg-type]
            signal_type=signal_type,
            status="candidate",
            score=round(score, 2),
            confidence=round(confidence, 3),
            created_at_ms=timestamp_ms,
            expires_at_ms=timestamp_ms + ttl_ms,
            entry=round(entry, 8),
            stop=round(stop, 8),
            take_profit=round(take_profit, 8) if take_profit else None,
            invalidation=f"{symbol} {side} invalidates on sustained trade through {stop:.6g}.",
            thesis=_thesis(state, market_map, side, signal_type),
            evidence=evidence,
            feature_snapshot=state.model_dump(mode="json"),
            risk_plan={
                "rr": rr,
                "risk_pct": self.settings.autonomy_paper_risk_pct_per_trade,
                "max_gross_leverage": self.settings.autonomy_paper_max_gross_leverage,
                "max_single_name_exposure_pct": self.settings.autonomy_paper_max_single_name_exposure_pct,
                "exchange_actions": [],
                "human_signoff_required": self.settings.autonomy_require_human_signoff,
            },
        )


def score_components(state: AssetMarketState, market_map: GlobalMarketMap, side: str, rr: float | None) -> tuple[list[SignalEvidence], float]:
    evidence: list[SignalEvidence] = []
    structure = 0.0
    if side == "long" and state.trend == "up":
        structure += 18
    elif side == "short" and state.trend == "down":
        structure += 18
    elif state.trend == "range":
        structure += 8
    if side == "long" and state.support_levels:
        structure += 5
    if side == "short" and state.resistance_levels:
        structure += 5
    if any(cluster.confidence.startswith("inferred") for cluster in state.liquidation_clusters):
        structure += 2
    structure = min(25, structure)
    evidence.append(SignalEvidence(category="market_structure", label=f"trend={state.trend}", value=round(structure, 2), weight=structure, source="market_structure"))

    orderflow_score = 0.0
    of = state.orderflow
    if of is not None:
        imbalance = of.imbalance_10bps or of.imbalance_top or 0.0
        if side == "long" and imbalance > 0:
            orderflow_score += min(12, imbalance * 20)
        if side == "short" and imbalance < 0:
            orderflow_score += min(12, abs(imbalance) * 20)
        if of.spread_bps is not None and of.spread_bps <= 10:
            orderflow_score += 4
        if of.top_depth_usd is not None and of.top_depth_usd >= 25_000:
            orderflow_score += 4
    evidence.append(SignalEvidence(category="orderflow", label="depth/imbalance/spread", value=round(orderflow_score, 2), weight=orderflow_score, source="orderflow"))

    rr_score = 0.0 if rr is None else min(15.0, max(0.0, (rr - 1.0) / 2.0 * 15.0))
    evidence.append(SignalEvidence(category="risk_reward", label="reward:risk", value=round(rr or 0.0, 2), weight=rr_score, source="risk"))

    funding_score = 7.0
    if state.funding_hourly is not None:
        if side == "long" and state.funding_hourly < 0.00015:
            funding_score += 5
        if side == "short" and state.funding_hourly > -0.00015:
            funding_score += 5
        if abs(state.funding_hourly) > 0.0008:
            funding_score -= 4
    funding_score = max(0.0, min(15.0, funding_score))
    evidence.append(SignalEvidence(category="funding_oi", label="funding/OI", value=state.funding_hourly, weight=funding_score, source="funding"))

    news_score = 0.0
    if state.news_state is not None:
        news = state.news_state
        if side == "long" and news.sentiment == "bullish":
            news_score = min(10.0, news.max_importance_score / 8.0)
        elif side == "short" and news.sentiment == "bearish":
            news_score = min(10.0, news.max_importance_score / 8.0)
        elif news.sentiment == "mixed":
            news_score = 3.0
    evidence.append(SignalEvidence(category="news", label="fresh catalysts", value=round(news_score, 2), weight=news_score, source="news"))

    regime_score = 5.0
    if side == "long" and market_map.risk_regime == "risk_on":
        regime_score = 10.0
    elif side == "short" and market_map.risk_regime == "risk_off":
        regime_score = 10.0
    elif market_map.risk_regime == "mixed":
        regime_score = 6.0
    evidence.append(SignalEvidence(category="cross_asset_regime", label=market_map.risk_regime, value=regime_score, weight=regime_score, source="market_structure"))

    execution = 0.0
    if of is not None:
        if of.spread_bps is not None and of.spread_bps <= 8:
            execution += 3
        if of.depth_10bps_bid_usd and of.depth_10bps_ask_usd:
            execution += 2
    evidence.append(SignalEvidence(category="execution", label="spread/depth quality", value=round(execution, 2), weight=execution, source="execution"))
    return evidence, sum(item.weight for item in evidence)


def risk_vetoes(signal: TradeSignal, state: AssetMarketState, existing_signals: list[TradeSignal], open_positions: list[PaperPosition]) -> list[str]:
    vetoes: list[str] = []
    if signal.stop <= 0 or signal.stop == signal.entry:
        vetoes.append("missing_stop")
    rr = signal.risk_plan.get("rr")
    if not isinstance(rr, (int, float)) or rr < 1.5:
        vetoes.append("rr_below_1_5")
    of = state.orderflow
    if of is not None and of.spread_bps is not None and of.spread_bps > 35:
        vetoes.append("spread_too_wide")
    if of is not None and of.top_depth_usd is not None and of.top_depth_usd < 2_500:
        vetoes.append("top_depth_too_thin")
    for existing in existing_signals:
        if existing.status in {"candidate", "posted", "approved", "paper_ordered"} and existing.symbol == signal.symbol and existing.side == signal.side:
            vetoes.append("duplicate_active_signal")
            break
    for position in open_positions:
        if position.status == "open" and position.symbol == signal.symbol and position.side == signal.side:
            vetoes.append("same_direction_position_exists")
            break
    return vetoes


def risk_reward(side: str, entry: float, stop: float, take_profit: float | None) -> float | None:
    risk = abs(entry - stop)
    if risk <= 0 or take_profit is None:
        return None
    reward = take_profit - entry if side == "long" else entry - take_profit
    if reward <= 0:
        return None
    return reward / risk


async def maybe_attach_model_insight(signal: TradeSignal, model_gateway: Any, settings: Settings) -> TradeSignal:
    if not settings.autonomy_model_insights_enabled or signal.score < settings.autonomy_model_insight_min_score:
        return signal
    prompt = (
        "Review this deterministic paper-trading signal. Return JSON with keys: "
        "stance, confidence, thesis_quality, hidden_risks, what_would_invalidate, suggested_adjustments, summary. "
        "You cannot approve or place trades. Signal:\n" + json.dumps(signal.model_dump(mode="json"), separators=(",", ":"))[:8000]
    )
    try:
        result = await model_gateway.complete(prompt, "You are a skeptical senior market-risk reviewer. Be concise and concrete.", timeout_seconds=30)
        insight = _parse_insight(result.content)
        return signal.model_copy(update={"model_insight": insight.model_dump(mode="json")})
    except Exception as exc:  # model insight must never block/postpone deterministic signals
        return signal.model_copy(update={"model_insight": {"status": "unavailable", "error": type(exc).__name__}})


def _parse_insight(content: str) -> ModelMarketInsight:
    try:
        start = content.find("{")
        end = content.rfind("}")
        data = json.loads(content[start : end + 1] if start >= 0 and end > start else content)
        return ModelMarketInsight(**data)
    except Exception:
        return ModelMarketInsight(stance="needs_more_data", confidence=0.0, thesis_quality=0.0, summary=content[:500])


def _signal_type(state: AssetMarketState, market_map: GlobalMarketMap, side: str) -> str | None:
    news = state.news_state
    if news is not None:
        if side == "long" and news.sentiment == "bullish" and news.max_importance_score >= 55:
            return "news_catalyst_momentum"
        if side == "short" and news.sentiment == "bearish" and news.max_importance_score >= 55:
            return "news_catalyst_momentum"
    if side == "long" and state.trend == "up":
        return "trend_continuation"
    if side == "short" and state.trend == "down":
        return "trend_continuation"
    if side == "long" and state.trend == "range" and state.support_levels:
        return "support_bounce"
    if side == "short" and state.trend == "range" and state.resistance_levels:
        return "resistance_rejection"
    if side == "short" and market_map.risk_regime == "risk_off" and state.trend in {"down", "range"}:
        return "risk_off_deleveraging"
    return None


def _levels_for_side(state: AssetMarketState, side: str, entry: float) -> tuple[float, float | None]:
    supports = sorted([level.price for level in state.support_levels if level.price < entry], reverse=True)
    resistances = sorted([level.price for level in state.resistance_levels if level.price > entry])
    if side == "long":
        stop = (supports[0] * 0.997) if supports else entry * 0.992
        fallback_tp = entry + 2.2 * (entry - stop)
        take_profit = _first_target_with_min_rr("long", entry, stop, resistances) or fallback_tp
        if take_profit <= entry:
            take_profit = entry + 2.0 * (entry - stop)
        return stop, take_profit
    resistance_above = resistances[0] if resistances else entry * 1.008
    stop = resistance_above * 1.003
    fallback_tp = entry - 2.2 * (stop - entry)
    take_profit = _first_target_with_min_rr("short", entry, stop, supports) or fallback_tp
    if take_profit >= entry:
        take_profit = entry - 2.0 * (stop - entry)
    return stop, max(take_profit, 0.00000001)


def _first_target_with_min_rr(side: str, entry: float, stop: float, targets: list[float], min_rr: float = 1.5) -> float | None:
    for target in targets:
        rr = risk_reward(side, entry, stop, target)
        if rr is not None and rr >= min_rr:
            return target
    return None


def _thesis(state: AssetMarketState, market_map: GlobalMarketMap, side: str, signal_type: str) -> str:
    pieces = [f"{state.symbol} {side} {signal_type.replace('_', ' ')} setup"]
    pieces.append(f"trend={state.trend}, volatility={state.volatility_regime}, global={market_map.risk_regime}")
    if state.orderflow and state.orderflow.imbalance_10bps is not None:
        pieces.append(f"10 bps orderflow imbalance {state.orderflow.imbalance_10bps:.2f}")
    if state.news_state and state.news_state.sentiment != "unknown":
        pieces.append(f"news sentiment {state.news_state.sentiment} score {state.news_state.max_importance_score:.0f}")
    return "; ".join(pieces) + "."


def _signal_id(symbol: str, side: str, signal_type: str, timestamp_ms: int, entry: float, stop: float) -> str:
    bucket = timestamp_ms // 60_000
    digest = hashlib.sha1(f"{symbol}:{side}:{signal_type}:{bucket}:{round(entry, 6)}:{round(stop, 6)}".encode()).hexdigest()[:18]
    return f"sig_{digest}"
