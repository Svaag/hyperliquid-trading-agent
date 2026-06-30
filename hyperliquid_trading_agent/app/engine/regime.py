from __future__ import annotations

import hashlib
import statistics
from datetime import UTC, datetime
from itertools import pairwise
from typing import Iterable

from hyperliquid_trading_agent.app.engine.event_ledger import now_ms
from hyperliquid_trading_agent.app.engine.schemas import FeatureValue, RegimeVector, StrategyPermissions


class RegimeEngine:
    """Computes a first-version RegimeVector from point-in-time features."""

    def compute(self, features: Iterable[FeatureValue], *, primary_asset: str = "GLOBAL", as_of_ms: int | None = None) -> RegimeVector:
        items = sorted(list(features), key=lambda item: (item.computed_ts_ms, item.received_ts_ms, item.feature_id))
        ts = as_of_ms or max((item.computed_ts_ms for item in items), default=now_ms())
        by_name: dict[str, list[FeatureValue]] = {}
        for item in items:
            by_name.setdefault(item.feature_name, []).append(item)

        mids = [item.scalar_value for item in by_name.get("mid", []) if item.scalar_value is not None]
        trend_state, trend_confidence = _trend(mids)
        spreads = [item.scalar_value for item in by_name.get("spread_bps", []) if item.scalar_value is not None]
        depths = [item.scalar_value for item in by_name.get("top_depth_usd", []) if item.scalar_value is not None]
        funding = [item.scalar_value for item in by_name.get("funding_hourly", []) if item.scalar_value is not None]
        oi = [item.scalar_value for item in by_name.get("open_interest", []) if item.scalar_value is not None]
        catalyst = [abs(item.scalar_value or 0) for item in by_name.get("catalyst_pressure", [])]
        top_imbalance = _latest_scalar(by_name, "top_imbalance")
        liq_notional_5m = _latest_scalar(by_name, "liq_notional_5m") or 0.0
        liq_imbalance_5m = _latest_scalar(by_name, "long_vs_short_liq_imbalance_5m") or 0.0
        liq_event_count_5m = _latest_scalar(by_name, "liq_event_count_5m") or 0.0
        quality_flags: list[str] = []
        required_core = {
            "mid": mids,
            "spread_bps": spreads,
            "top_depth_usd": depths,
            "funding_hourly": funding,
            "open_interest": oi,
            "top_imbalance": [item.scalar_value for item in by_name.get("top_imbalance", []) if item.scalar_value is not None],
        }
        for feature_name, values in required_core.items():
            if not values:
                quality_flags.append(f"missing_{feature_name}_features")

        realized_vol = _realized_vol_percentile(mids)
        spread_state = _spread_state(spreads[-1] if spreads else None)
        liquidity_state = _liquidity_state(depths[-1] if depths else None)
        funding_stress_z = _zscore(funding)
        oi_velocity_z = _latest_scalar(by_name, "oi_velocity_z")
        if oi_velocity_z is None:
            oi_velocity_z = _velocity_z(oi)
        news_pressure = min(1.0, max(catalyst) if catalyst else 0.0)
        stability = _stability_score(trend_confidence, realized_vol, spread_state, liquidity_state, news_pressure, quality_flags)
        volatility_state = _volatility_state(realized_vol)
        funding_state = _funding_state(funding_stress_z)
        oi_state = _oi_state(oi_velocity_z)
        liquidation_state = _liquidation_state(liq_notional_5m=liq_notional_5m, imbalance=liq_imbalance_5m, event_count=liq_event_count_5m)
        orderflow_state = _orderflow_state(top_imbalance)
        news_state = "catalyst" if news_pressure >= 0.35 else "no_event"
        correlation_breakdown_prob = max(0.0, min(1.0, (realized_vol or 0.0) * (1.0 - stability)))
        correlation_state = _correlation_state(correlation_breakdown_prob)
        session_state = _session_state(ts)
        feature_coverage_pct = round(sum(1 for values in required_core.values() if values) / len(required_core) * 100.0, 2)
        regime_label = _regime_label(
            trend_state=trend_state,
            volatility_state=volatility_state,
            funding_state=funding_state,
            oi_state=oi_state,
            liquidation_state=liquidation_state,
            orderflow_state=orderflow_state,
            news_state=news_state,
            session_state=session_state,
        )
        permissions = StrategyPermissions(
            momentum_allowed=trend_state in {"bull", "bear"} and stability >= 0.35 and (funding_stress_z is None or abs(funding_stress_z) < 2.5),
            mean_reversion_allowed=trend_state == "range" and news_pressure < 0.5,
            market_making_allowed=spread_state in {"tight", "normal"} and liquidity_state in {"deep", "normal"} and news_pressure < 0.4,
            news_event_allowed=news_pressure >= 0.35,
            carry_allowed=funding_stress_z is not None and abs(funding_stress_z) >= 1.0,
            relative_value_allowed=stability >= 0.45,
            reason_codes=quality_flags.copy(),
        )
        digest = hashlib.sha1(f"{primary_asset}:{ts}:{[item.feature_id for item in items[-50:]]}".encode()).hexdigest()[:24]
        derived_labels = {
            "trend": trend_state,
            "liquidity": liquidity_state,
            "spread": spread_state,
            "volatility": volatility_state,
            "funding": funding_state,
            "oi": oi_state,
            "liquidation": liquidation_state,
            "orderflow": orderflow_state,
            "news": news_state,
            "correlation": correlation_state,
            "session": session_state,
            "regime_label": regime_label,
        }
        return RegimeVector(
            regime_snapshot_id=f"reg_{digest}",
            primary_asset=primary_asset,
            created_at_ms=now_ms(),
            as_of_ms=ts,
            trend_state=trend_state,
            trend_confidence=trend_confidence,
            realized_vol_percentile=realized_vol,
            implied_vol_percentile=None,
            liquidity_state=liquidity_state,
            spread_state=spread_state,
            volatility_state=volatility_state,
            funding_state=funding_state,
            oi_state=oi_state,
            liquidation_state=liquidation_state,
            orderflow_state=orderflow_state,
            news_state=news_state,
            correlation_state=correlation_state,
            session_state=session_state,
            feature_coverage_pct=feature_coverage_pct,
            regime_label=regime_label,
            funding_stress_z=funding_stress_z,
            open_interest_velocity_z=oi_velocity_z,
            liquidation_imbalance_z=liq_imbalance_5m,
            dominance_pressure_z=None,
            cross_asset_risk_on_z=None,
            stablecoin_liquidity_z=None,
            correlation_breakdown_prob=correlation_breakdown_prob,
            news_catalyst_pressure=news_pressure,
            regime_stability_score=stability,
            permissions=permissions,
            feature_refs=[item.feature_id for item in items],
            raw_feature_refs={item.feature_name: item.feature_id for item in items[-100:]},
            derived_labels=derived_labels,
            quality_flags=quality_flags,
        )


def _trend(mids: list[float]) -> tuple[str, float]:
    if len(mids) < 3 or mids[0] <= 0:
        return "unknown", 0.0
    change_bps = (mids[-1] - mids[0]) / mids[0] * 10_000
    confidence = min(1.0, abs(change_bps) / 100.0)
    if change_bps > 35:
        return "bull", confidence
    if change_bps < -35:
        return "bear", confidence
    return "range", max(0.2, 1.0 - confidence)


def _realized_vol_percentile(mids: list[float]) -> float | None:
    if len(mids) < 5:
        return None
    returns = [(cur - prev) / prev for prev, cur in pairwise(mids) if prev > 0]
    if len(returns) < 4:
        return None
    vol = statistics.pstdev(returns)
    # Conservative heuristic until historical percentile rollups are available.
    return max(0.0, min(1.0, vol / 0.02))


def _latest_scalar(by_name: dict[str, list[FeatureValue]], feature_name: str) -> float | None:
    values = [item.scalar_value for item in by_name.get(feature_name, []) if item.scalar_value is not None]
    return values[-1] if values else None


def _volatility_state(realized_vol_percentile: float | None) -> str:
    if realized_vol_percentile is None:
        return "unknown"
    pct = realized_vol_percentile * 100.0
    if pct < 25:
        return "compressed"
    if pct < 70:
        return "normal"
    if pct < 90:
        return "elevated"
    return "extreme"


def _funding_state(funding_z: float | None) -> str:
    if funding_z is None:
        return "unknown"
    if funding_z >= 2:
        return "positive_extreme"
    if funding_z <= -2:
        return "negative_extreme"
    if abs(funding_z) < 1:
        return "neutral"
    return "positive" if funding_z > 0 else "negative"


def _oi_state(oi_velocity_z: float | None) -> str:
    if oi_velocity_z is None:
        return "unknown"
    if oi_velocity_z > 1:
        return "expanding"
    if oi_velocity_z < -1:
        return "contracting"
    return "flat"


def _liquidation_state(*, liq_notional_5m: float, imbalance: float, event_count: float) -> str:
    active = liq_notional_5m >= 100_000 or event_count >= 3
    if not active:
        return "calm"
    if liq_notional_5m > 0 and abs(imbalance) / max(liq_notional_5m, 1.0) < 0.35:
        return "mixed"
    if imbalance > 0:
        return "long_flush"
    if imbalance < 0:
        return "short_squeeze"
    return "mixed"


def _orderflow_state(top_imbalance: float | None) -> str:
    if top_imbalance is None:
        return "unknown"
    if top_imbalance > 0.2:
        return "buy_pressure"
    if top_imbalance < -0.2:
        return "sell_pressure"
    return "balanced"


def _correlation_state(probability: float) -> str:
    if probability >= 0.60:
        return "breakdown"
    if probability >= 0.25:
        return "watch"
    return "normal"


def _session_state(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
    if dt.weekday() >= 5:
        return "weekend"
    hour = dt.hour
    if hour < 1:
        return "rollover"
    if hour < 8:
        return "asia"
    if hour < 13:
        return "europe"
    if hour < 21:
        return "us"
    return "rollover"


def _regime_label(**labels: str) -> str:
    ordered_keys = ["trend_state", "volatility_state", "funding_state", "oi_state", "liquidation_state", "orderflow_state", "news_state", "session_state"]
    return "|".join(f"{key.removesuffix('_state')}={labels.get(key, 'unknown')}" for key in ordered_keys)


def _spread_state(spread_bps: float | None) -> str:
    if spread_bps is None:
        return "unknown"
    if spread_bps <= 5:
        return "tight"
    if spread_bps <= 20:
        return "normal"
    return "wide"


def _liquidity_state(depth_usd: float | None) -> str:
    if depth_usd is None:
        return "unknown"
    if depth_usd >= 250_000:
        return "deep"
    if depth_usd >= 25_000:
        return "normal"
    if depth_usd >= 2_500:
        return "thin"
    return "impaired"


def _zscore(values: list[float]) -> float | None:
    if len(values) < 3:
        return None
    sigma = statistics.pstdev(values)
    if sigma <= 0:
        return 0.0
    return (values[-1] - statistics.mean(values)) / sigma


def _velocity_z(values: list[float]) -> float | None:
    if len(values) < 4:
        return None
    changes = [cur - prev for prev, cur in pairwise(values)]
    return _zscore(changes)


def _stability_score(trend_confidence: float, vol_pct: float | None, spread_state: str, liquidity_state: str, news_pressure: float, flags: list[str]) -> float:
    score = 0.55 + trend_confidence * 0.15
    if vol_pct is not None:
        score -= max(0.0, vol_pct - 0.6) * 0.25
    if spread_state == "wide":
        score -= 0.2
    if liquidity_state in {"thin", "impaired"}:
        score -= 0.2
    score -= min(0.2, news_pressure * 0.2)
    score -= min(0.25, len(flags) * 0.04)
    return max(0.0, min(1.0, score))
