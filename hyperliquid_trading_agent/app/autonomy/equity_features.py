"""Equity-specific signal feature extractors for the shared signal engine."""

from __future__ import annotations

import hashlib
import time
from typing import Any

from hyperliquid_trading_agent.app.autonomy.schemas import (
    SignalEvidence,
    TradeSignal,
)
from hyperliquid_trading_agent.app.tradfi.options_flow import OptionsFlowDetector
from hyperliquid_trading_agent.app.tradfi.schemas import OptionsFlowEvent, StockSnapshot

EQUITY_SIGNAL_TYPES = {
    "earnings_catalyst",
    "options_flow_signal",
    "technical_breakout_equity",
    "sector_rotation",
    "corp_action_spike",
}


def _equity_signal_id(symbol: str, side: str, signal_type: str, timestamp_ms: int, entry: float, stop: float) -> str:
    key = f"eq_{symbol}:{side}:{signal_type}:{timestamp_ms}:{entry}:{stop}"
    return "eqsig_" + hashlib.sha1(key.encode()).hexdigest()[:24]


def detect_earnings_catalyst(
    symbol: str,
    snap: StockSnapshot,
    corporate_actions: list[dict[str, Any]],
    *,
    timestamp_ms: int | None = None,
) -> TradeSignal | None:
    """Generate a signal if earnings report is imminent and IV/volume elevated.

    This is a simplified heuristic — real earnings calendars would come from
    a data vendor. For now, checks if the daily range is unusually wide vs
    the 21-day average (a rough IV proxy).
    """
    ts = timestamp_ms or int(time.time() * 1000)
    if snap.daily_bar is None or snap.previous_close is None or snap.previous_close == 0:
        return None

    daily_range_pct = abs((snap.daily_bar.high - snap.daily_bar.low) / snap.previous_close * 100)
    if daily_range_pct < 2.5:
        return None

    direction = "long" if snap.change_pct and snap.change_pct > 0 else "short"
    entry = snap.daily_bar.close
    stop = entry * 0.97 if direction == "long" else entry * 1.03
    take_profit = entry * 1.05 if direction == "long" else entry * 0.95

    evidence = [
        SignalEvidence(category="earnings", label="range_expansion", value=round(daily_range_pct, 2), weight=min(20, daily_range_pct * 6), source="equity"),
    ]

    score = min(85.0, 45.0 + daily_range_pct * 8)
    ttl_ms = 60 * 60 * 1000  # 1 hour

    return TradeSignal(
        id=_equity_signal_id(symbol, direction, "earnings_catalyst", ts, entry, stop),
        symbol=symbol,
        side=direction,  # type: ignore[arg-type]
        signal_type="earnings_catalyst",
        status="candidate",
        score=round(score, 2),
        confidence=0.55,
        created_at_ms=ts,
        expires_at_ms=ts + ttl_ms,
        entry=round(entry, 2),
        stop=round(stop, 2),
        take_profit=round(take_profit, 2),
        invalidation=f"{symbol} earnings catalyst invalidated below {stop:.2f}.",
        thesis=f"Elevated daily range ({daily_range_pct:.1f}%) near earnings suggests volatile catalyst. Direction aligns with {direction}-side momentum.",
        evidence=evidence,
        feature_snapshot={"range_pct": daily_range_pct, "price": entry, "change_pct": snap.change_pct},
        risk_plan={"rr": round(abs(take_profit - entry) / abs(entry - stop), 2) if abs(entry - stop) > 0 else 0.0, "exchange_actions": []},
    )


def detect_options_flow_signal(
    symbol: str,
    snap: StockSnapshot,
    flow_events: list[OptionsFlowEvent],
    *,
    min_urgency: float = 60.0,
    timestamp_ms: int | None = None,
) -> TradeSignal | None:
    """Generate an equity signal from unusual options flow.

    If flow is strongly directional (all calls or all puts above urgency threshold),
    suggest a stock trade in that direction.
    """
    ts = timestamp_ms or int(time.time() * 1000)
    if not flow_events or snap.daily_bar is None:
        return None

    high_flow = [e for e in flow_events if e.urgency_score >= min_urgency]
    if not high_flow:
        return None

    call_flow = sum(1 for e in high_flow if e.flow_type in {"call_buy"})
    put_flow = sum(1 for e in high_flow if e.flow_type in {"put_buy"})
    if call_flow > put_flow and call_flow >= 2:
        direction = "long"
    elif put_flow > call_flow and put_flow >= 2:
        direction = "short"
    else:
        return None  # mixed flow, no clear direction

    entry = snap.daily_bar.close
    if direction == "long":
        stop = entry * 0.97
        take_profit = entry * 1.05
    else:
        stop = entry * 1.03
        take_profit = entry * 0.95

    max_urgency = max(e.urgency_score for e in high_flow)
    total_premium = sum(e.premium_estimate for e in high_flow)

    evidence = [
        SignalEvidence(category="options_flow", label="unusual_activity", value=round(max_urgency, 1), weight=min(25, max_urgency * 0.3), source="options_flow"),
        SignalEvidence(category="options_flow", label="premium", value=round(total_premium, 0), weight=min(15, total_premium / 200000 * 10), source="options_flow"),
    ]

    score = min(85.0, 40.0 + max_urgency * 0.4 + min(10, total_premium / 100000))
    ttl_ms = 30 * 60 * 1000  # 30 min for flow signals

    return TradeSignal(
        id=_equity_signal_id(symbol, direction, "options_flow_signal", ts, entry, stop),
        symbol=symbol,
        side=direction,  # type: ignore[arg-type]
        signal_type="options_flow_signal",
        status="candidate",
        score=round(score, 2),
        confidence=0.60,
        created_at_ms=ts,
        expires_at_ms=ts + ttl_ms,
        entry=round(entry, 2),
        stop=round(stop, 2),
        take_profit=round(take_profit, 2),
        invalidation=f"{symbol} flow signal invalidated when {'below' if direction == 'long' else 'above'} {stop:.2f}.",
        thesis=f"Unusual options flow: {call_flow} call vs {put_flow} put events. Max urgency {max_urgency:.0f}, total premium ${total_premium:,.0f}.",
        evidence=evidence,
        feature_snapshot={"max_urgency": max_urgency, "total_premium": total_premium, "call_count": call_flow, "put_count": put_flow},
        risk_plan={"rr": round(abs(take_profit - entry) / abs(entry - stop), 2) if abs(entry - stop) > 0 else 0.0, "exchange_actions": []},
    )


def detect_technical_breakout_equity(
    symbol: str,
    snap: StockSnapshot,
    *,
    timestamp_ms: int | None = None,
) -> TradeSignal | None:
    """Generate a signal from a technical breakout in an equity.

    Uses the daily bar vs previous close and volume for a simplified breakout detection.
    """
    ts = timestamp_ms or int(time.time() * 1000)
    if snap.daily_bar is None or snap.previous_close is None or snap.previous_close == 0:
        return None

    change_pct = (snap.daily_bar.close - snap.previous_close) / snap.previous_close * 100
    if abs(change_pct) < 2.0:
        return None

    # Check volume spikes
    avg_vol = 10_000_000  # rough default; would be 21-day average in production
    vol_ratio = snap.daily_bar.volume / avg_vol if avg_vol > 0 else 1.0
    if vol_ratio < 1.3:
        return None

    direction = "long" if change_pct > 0 else "short"
    entry = snap.daily_bar.close
    stop = entry * 0.97 if direction == "long" else entry * 1.03
    take_profit = entry * 1.06 if direction == "long" else entry * 0.94

    evidence = [
        SignalEvidence(category="technical", label="breakout_pct", value=round(change_pct, 2), weight=min(20, abs(change_pct) * 5), source="equity"),
        SignalEvidence(category="technical", label="volume_ratio", value=round(vol_ratio, 2), weight=min(10, vol_ratio * 3), source="equity"),
    ]

    score = min(80.0, 35.0 + abs(change_pct) * 6 + min(10, vol_ratio * 2))
    ttl_ms = 2 * 60 * 60 * 1000  # 2 hours

    return TradeSignal(
        id=_equity_signal_id(symbol, direction, "technical_breakout_equity", ts, entry, stop),
        symbol=symbol,
        side=direction,  # type: ignore[arg-type]
        signal_type="technical_breakout_equity",
        status="candidate",
        score=round(score, 2),
        confidence=0.50,
        created_at_ms=ts,
        expires_at_ms=ts + ttl_ms,
        entry=round(entry, 2),
        stop=round(stop, 2),
        take_profit=round(take_profit, 2),
        invalidation=f"{symbol} breakout invalidated below {stop:.2f}.",
        thesis=f"Technical breakout: {change_pct:+.1f}% move on {vol_ratio:.1f}x volume.",
        evidence=evidence,
        feature_snapshot={"change_pct": change_pct, "vol_ratio": vol_ratio, "price": entry},
        risk_plan={"rr": round(abs(take_profit - entry) / abs(entry - stop), 2) if abs(entry - stop) > 0 else 0.0, "exchange_actions": []},
    )


class EquitySignalGenerator:
    """Generates equity-specific signals from TradFi data."""

    def __init__(
        self,
        *,
        min_signal_score: float = 75.0,
        max_signals_per_day: int = 5,
        signal_ttl_minutes: int = 60,
        flow_detector: OptionsFlowDetector | None = None,
    ):
        self.min_signal_score = min_signal_score
        self.max_signals_per_day = max_signals_per_day
        self.signal_ttl_minutes = signal_ttl_minutes
        self.flow_detector = flow_detector or OptionsFlowDetector()

    def generate_from_snapshot(
        self,
        symbol: str,
        snap: StockSnapshot,
        *,
        corporate_actions: list[dict[str, Any]] | None = None,
        flow_events: list[OptionsFlowEvent] | None = None,
        signals_today: int = 0,
        timestamp_ms: int | None = None,
    ) -> list[TradeSignal]:
        """Generate signals for a single equity from a snapshot."""
        ts = timestamp_ms or int(time.time() * 1000)
        if signals_today >= self.max_signals_per_day:
            return []

        candidates: list[TradeSignal] = []

        # Earnings catalyst
        earnings_signal = detect_earnings_catalyst(symbol, snap, corporate_actions or [], timestamp_ms=ts)
        if earnings_signal and earnings_signal.score >= self.min_signal_score:
            candidates.append(earnings_signal)

        # Options flow
        if flow_events:
            flow_signal = detect_options_flow_signal(symbol, snap, flow_events, timestamp_ms=ts)
            if flow_signal and flow_signal.score >= self.min_signal_score:
                candidates.append(flow_signal)

        # Technical breakout
        breakout = detect_technical_breakout_equity(symbol, snap, timestamp_ms=ts)
        if breakout and breakout.score >= self.min_signal_score:
            candidates.append(breakout)

        candidates.sort(key=lambda s: s.score, reverse=True)
        remaining = max(0, self.max_signals_per_day - signals_today)
        return candidates[:remaining]
