from __future__ import annotations

import hashlib
from typing import Any

from hyperliquid_trading_agent.app.engine.alpha.base import CORE_CRYPTO_ASSETS, HYPERLIQUID_VENUES, candidate_contract_fields
from hyperliquid_trading_agent.app.engine.schemas import AlphaCandidate, FeatureSnapshot, RegimeVector, StrategySpec


class MicrostructureAbsorptionStrategy:
    spec = StrategySpec(
        strategy_id="microstructure_absorption_v1",
        version="1.0.0",
        family="microstructure_orderflow",
        supported_assets=CORE_CRYPTO_ASSETS,
        supported_venues=HYPERLIQUID_VENUES,
        supported_horizons=["5m", "15m"],
        required_features=["mid", "spread_bps", "top_imbalance", "mid_return_5m_bps"],
        valid_regimes=["balanced", "buy_pressure", "sell_pressure", "range"],
        max_candidates_per_run=1,
        max_allocation_share_pct=35.0,
        cooldown_ms=120_000,
        min_confidence=0.35,
        min_ev_bps=8.0,
        risk_tags=["absorption", "microstructure", "trap_reversal"],
        enabled=True,
        metadata={"wave": "1C", "replayable": True},
    )
    strategy_id = spec.strategy_id

    def generate(self, snapshot: FeatureSnapshot, regime: RegimeVector, *, timestamp_ms: int) -> list[AlphaCandidate]:
        if regime.spread_state == "wide" or regime.liquidity_state == "impaired":
            return []
        mid = _float(snapshot.features.get("mid"))
        spread = _float(snapshot.features.get("spread_bps"))
        imbalance = _float(snapshot.features.get("top_imbalance"))
        ret_5m = _float(snapshot.features.get("mid_return_5m_bps"))
        if mid is None or mid <= 0 or spread is None or imbalance is None or ret_5m is None:
            return []
        if spread > 15 or abs(imbalance) < 0.35 or abs(ret_5m) > 18:
            return []
        side = "short" if imbalance > 0 else "long"
        stop_bps = max(18.0, min(38.0, abs(ret_5m) + 18.0))
        stop, target = _stop_target(mid, side, stop_bps=stop_bps, rr=1.6)
        absorption_score = abs(imbalance) * 55.0 + max(0.0, 18.0 - abs(ret_5m))
        score = min(100.0, 42.0 + absorption_score + max(0.0, 15.0 - spread))
        return [_candidate(self.spec, snapshot, regime, timestamp_ms=timestamp_ms, side=side, horizon="5m", entry=mid, stop=stop, target=target, score=score, confidence=min(0.88, 0.32 + score / 180.0), thesis=f"{snapshot.asset} {side} absorption: book imbalance {imbalance:.2f} failed to produce continuation ({ret_5m:.1f} bps).", invalidation=["Absorbed flow turns into continuation", "Spread widens", f"Price trades through {stop:.6g}"], metadata={"top_imbalance": imbalance, "mid_return_5m_bps": ret_5m, "spread_bps": spread}, expected_edge_bps=max(0.0, score - 45.0) / 2.8)]


class FundingSqueezeStrategy:
    spec = StrategySpec(
        strategy_id="funding_squeeze_v1",
        version="1.0.0",
        family="funding_basis",
        supported_assets=CORE_CRYPTO_ASSETS,
        supported_venues=HYPERLIQUID_VENUES,
        supported_horizons=["15m", "1h"],
        required_features=["mid", "funding_hourly", "oi_delta_5m_pct", "mid_return_5m_bps", "spread_bps"],
        valid_regimes=["positive_extreme", "negative_extreme", "expanding"],
        max_candidates_per_run=1,
        max_allocation_share_pct=35.0,
        cooldown_ms=300_000,
        min_confidence=0.38,
        min_ev_bps=10.0,
        risk_tags=["funding", "squeeze", "crowding"],
        enabled=True,
        metadata={"wave": "1C", "replayable": True},
    )
    strategy_id = spec.strategy_id

    def generate(self, snapshot: FeatureSnapshot, regime: RegimeVector, *, timestamp_ms: int) -> list[AlphaCandidate]:
        mid = _float(snapshot.features.get("mid"))
        funding = _float(snapshot.features.get("funding_hourly"))
        oi_delta = _float(snapshot.features.get("oi_delta_5m_pct"))
        ret_5m = _float(snapshot.features.get("mid_return_5m_bps"))
        spread = _float(snapshot.features.get("spread_bps"))
        if mid is None or mid <= 0 or funding is None or oi_delta is None or ret_5m is None or spread is None:
            return []
        if spread > 20 or abs(funding) < 0.00015 or oi_delta < 1.5 or abs(ret_5m) < 15:
            return []
        side = "short" if funding > 0 and ret_5m < 0 else "long" if funding < 0 and ret_5m > 0 else None
        if side is None:
            return []
        stop, target = _stop_target(mid, side, stop_bps=max(32.0, abs(ret_5m) * 0.9), rr=1.85)
        crowding = min(35.0, abs(funding) * 100_000) + min(25.0, oi_delta * 3.0) + min(20.0, abs(ret_5m) / 2.0)
        score = min(100.0, 42.0 + crowding + max(0.0, 20 - spread))
        return [_candidate(self.spec, snapshot, regime, timestamp_ms=timestamp_ms, side=side, horizon="1h", entry=mid, stop=stop, target=target, score=score, confidence=min(0.9, 0.34 + score / 170.0), thesis=f"{snapshot.asset} {side} funding squeeze: funding {funding:.5f}, OI +{oi_delta:.1f}%, return {ret_5m:.1f} bps.", invalidation=["Funding crowding normalizes", "OI expansion reverses", f"Price trades through {stop:.6g}"], metadata={"funding_hourly": funding, "oi_delta_5m_pct": oi_delta, "mid_return_5m_bps": ret_5m, "spread_bps": spread}, expected_edge_bps=max(0.0, score - 50.0) / 2.2)]


class BasisReversionStrategy:
    spec = StrategySpec(
        strategy_id="basis_reversion_v1",
        version="1.0.0",
        family="funding_basis",
        supported_assets=CORE_CRYPTO_ASSETS,
        supported_venues=HYPERLIQUID_VENUES,
        supported_horizons=["1h", "4h"],
        required_features=["mid", "perp_basis_bps", "realized_vol_15m_bps", "spread_bps"],
        valid_regimes=["compressed", "normal", "neutral"],
        max_candidates_per_run=1,
        max_allocation_share_pct=30.0,
        cooldown_ms=900_000,
        min_confidence=0.38,
        min_ev_bps=7.0,
        risk_tags=["basis", "relative_value", "mean_reversion"],
        enabled=True,
        metadata={"wave": "1C", "replayable": True},
    )
    strategy_id = spec.strategy_id

    def generate(self, snapshot: FeatureSnapshot, regime: RegimeVector, *, timestamp_ms: int) -> list[AlphaCandidate]:
        mid = _float(snapshot.features.get("mid"))
        basis = _float(snapshot.features.get("perp_basis_bps"))
        vol = _float(snapshot.features.get("realized_vol_15m_bps"))
        spread = _float(snapshot.features.get("spread_bps"))
        if mid is None or mid <= 0 or basis is None or vol is None or spread is None:
            return []
        if spread > 12 or vol > 90 or abs(basis) < 18:
            return []
        side = "short" if basis > 0 else "long"
        stop, target = _stop_target(mid, side, stop_bps=max(28.0, vol * 0.45), rr=1.3)
        score = min(100.0, 45.0 + min(30.0, abs(basis)) + max(0.0, 90 - vol) / 4.0 + max(0.0, 12 - spread))
        return [_candidate(self.spec, snapshot, regime, timestamp_ms=timestamp_ms, side=side, horizon="4h", entry=mid, stop=stop, target=target, score=score, confidence=min(0.85, 0.36 + score / 190.0), thesis=f"{snapshot.asset} {side} basis reversion: perp basis {basis:.1f} bps in low-vol/liquid regime.", invalidation=["Basis widens with directional confirmation", "Volatility regime becomes elevated", f"Price trades through {stop:.6g}"], metadata={"perp_basis_bps": basis, "realized_vol_15m_bps": vol, "spread_bps": spread}, expected_edge_bps=max(0.0, abs(basis) * 0.35 - spread))]


class NewsImpulseStrategy:
    spec = StrategySpec(
        strategy_id="news_impulse_v1",
        version="1.0.0",
        family="event_driven_news",
        supported_assets=CORE_CRYPTO_ASSETS,
        supported_venues=HYPERLIQUID_VENUES,
        supported_horizons=["15m", "1h"],
        required_features=["mid", "catalyst_pressure", "source_consensus_score", "mid_return_5m_bps", "day_volume_usd"],
        valid_regimes=["catalyst", "news_catalyst", "event_risk"],
        max_candidates_per_run=1,
        max_allocation_share_pct=30.0,
        cooldown_ms=600_000,
        min_confidence=0.40,
        min_ev_bps=10.0,
        risk_tags=["news", "catalyst", "impulse"],
        enabled=True,
        metadata={"wave": "1C", "replayable": True, "requires_source_score": True},
    )
    strategy_id = spec.strategy_id

    def generate(self, snapshot: FeatureSnapshot, regime: RegimeVector, *, timestamp_ms: int) -> list[AlphaCandidate]:
        mid = _float(snapshot.features.get("mid"))
        pressure = _float(snapshot.features.get("catalyst_pressure"))
        source_score = _float(snapshot.features.get("source_consensus_score"))
        ret_5m = _float(snapshot.features.get("mid_return_5m_bps"))
        volume = _float(snapshot.features.get("day_volume_usd"))
        if mid is None or mid <= 0 or pressure is None or source_score is None or ret_5m is None or volume is None:
            return []
        if abs(pressure) < 0.45 or source_score < 0.55 or volume < 25_000_000 or abs(ret_5m) < 20:
            return []
        side = "long" if pressure > 0 and ret_5m > 0 else "short" if pressure < 0 and ret_5m < 0 else None
        if side is None:
            return []
        stop, target = _stop_target(mid, side, stop_bps=max(45.0, abs(ret_5m) * 0.8), rr=1.9)
        score = min(100.0, 44.0 + abs(pressure) * 25.0 + source_score * 20.0 + min(15.0, abs(ret_5m) / 4.0))
        return [_candidate(self.spec, snapshot, regime, timestamp_ms=timestamp_ms, side=side, horizon="1h", entry=mid, stop=stop, target=target, score=score, confidence=min(0.9, 0.35 + score / 175.0), thesis=f"{snapshot.asset} {side} news impulse: pressure {pressure:.2f}, source consensus {source_score:.2f}, price confirmation {ret_5m:.1f} bps.", invalidation=["Catalyst contradicted or source score deteriorates", "Price/volume confirmation fails", f"Price trades through {stop:.6g}"], metadata={"catalyst_pressure": pressure, "source_consensus_score": source_score, "mid_return_5m_bps": ret_5m, "day_volume_usd": volume}, expected_edge_bps=max(0.0, score - 52.0) / 2.0)]


class RangeRotationStrategy:
    spec = StrategySpec(
        strategy_id="range_rotation_v1",
        version="1.0.0",
        family="mean_reversion",
        supported_assets=CORE_CRYPTO_ASSETS,
        supported_venues=HYPERLIQUID_VENUES,
        supported_horizons=["15m", "1h"],
        required_features=["mid", "range_position", "realized_vol_15m_bps", "spread_bps"],
        valid_regimes=["range", "compressed"],
        max_candidates_per_run=1,
        max_allocation_share_pct=25.0,
        cooldown_ms=600_000,
        min_confidence=0.45,
        min_ev_bps=8.0,
        risk_tags=["range", "rotation", "mean_reversion"],
        enabled=False,
        counts_for_breadth=False,
        metadata={"wave": "1C_optional", "disabled_reason": "needs_feature_replay_depth"},
    )
    strategy_id = spec.strategy_id

    def generate(self, snapshot: FeatureSnapshot, regime: RegimeVector, *, timestamp_ms: int) -> list[AlphaCandidate]:
        return []


class VolatilityCompressionBreakoutStrategy:
    spec = StrategySpec(
        strategy_id="volatility_compression_breakout_v1",
        version="1.0.0",
        family="trend_following",
        supported_assets=CORE_CRYPTO_ASSETS,
        supported_venues=HYPERLIQUID_VENUES,
        supported_horizons=["15m", "1h"],
        required_features=["mid", "realized_vol_15m_bps", "top_depth_usd", "mid_return_5m_bps"],
        valid_regimes=["compressed", "expanding"],
        max_candidates_per_run=1,
        max_allocation_share_pct=25.0,
        cooldown_ms=600_000,
        min_confidence=0.45,
        min_ev_bps=9.0,
        risk_tags=["compression", "breakout", "trend"],
        enabled=False,
        counts_for_breadth=False,
        metadata={"wave": "1C_optional", "disabled_reason": "needs_pre_expansion_replay_depth"},
    )
    strategy_id = spec.strategy_id

    def generate(self, snapshot: FeatureSnapshot, regime: RegimeVector, *, timestamp_ms: int) -> list[AlphaCandidate]:
        return []


def wave_1c_strategy_instances() -> list[Any]:
    return [
        MicrostructureAbsorptionStrategy(),
        FundingSqueezeStrategy(),
        BasisReversionStrategy(),
        NewsImpulseStrategy(),
    ]


def wave_1c_specs(*, enabled: bool = False) -> list[StrategySpec]:
    active = [strategy.spec.model_copy(update={"enabled": enabled, "counts_for_breadth": enabled}) for strategy in wave_1c_strategy_instances()]
    optional = [RangeRotationStrategy().spec, VolatilityCompressionBreakoutStrategy().spec]
    return [*active, *optional]


def _candidate(
    spec: StrategySpec,
    snapshot: FeatureSnapshot,
    regime: RegimeVector,
    *,
    timestamp_ms: int,
    side: str,
    horizon: str,
    entry: float,
    stop: float,
    target: float,
    score: float,
    confidence: float,
    thesis: str,
    invalidation: list[str],
    metadata: dict[str, Any],
    expected_edge_bps: float,
) -> AlphaCandidate:
    cid = "cand_" + hashlib.sha1(f"{spec.strategy_id}:{snapshot.asset}:{side}:{timestamp_ms // 30_000}:{round(entry, 6)}".encode()).hexdigest()[:24]
    return AlphaCandidate(
        candidate_id=cid,
        strategy_id=spec.strategy_id,
        **candidate_contract_fields(spec, snapshot, expected_edge_bps=expected_edge_bps),
        asset=snapshot.asset,
        asset_class="crypto",
        venue="hyperliquid",
        side=side,  # type: ignore[arg-type]
        horizon=horizon,
        proposed_entry=entry,
        stop=stop,
        targets=[max(target, 0.00000001)],
        thesis=thesis,
        invalidation_conditions=invalidation,
        feature_snapshot_id=snapshot.snapshot_id,
        regime_snapshot_id=regime.regime_snapshot_id,
        source_event_ids=[],
        raw_alpha_score=round(max(0.0, min(100.0, score)), 2),
        confidence=round(max(0.0, min(1.0, confidence)), 3),
        created_at_ms=timestamp_ms,
        expires_at_ms=timestamp_ms + _horizon_ms(horizon),
        metadata={**metadata, "regime_label": regime.regime_label, "strategy_family": spec.family},
    )


def _stop_target(mid: float, side: str, *, stop_bps: float, rr: float) -> tuple[float, float]:
    stop_distance = mid * stop_bps / 10_000.0
    if side == "long":
        return mid - stop_distance, mid + stop_distance * rr
    return mid + stop_distance, mid - stop_distance * rr


def _horizon_ms(horizon: str) -> int:
    text = horizon.lower().strip()
    if text.endswith("m"):
        return int(float(text[:-1]) * 60_000)
    if text.endswith("h"):
        return int(float(text[:-1]) * 3_600_000)
    return 30 * 60_000


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
