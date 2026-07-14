from __future__ import annotations

import hashlib
from typing import Any

from hyperliquid_trading_agent.app.engine.alpha.base import candidate_contract_fields
from hyperliquid_trading_agent.app.engine.schemas import AlphaCandidate, FeatureSnapshot, RegimeVector, StrategySpec

PERP_DEX_VENUES = [
    "hyperliquid",
    "hyperliquid:main",
    "hyperliquid:xyz",
    "lighter",
    "aster",
    "dydx",
    "drift",
    "gmx",
    "orderly",
    "alpaca:paper",
]
WAVE_2A_IDS = {
    "cross_venue_lead_lag_v1",
    "liquidity_vacuum_breakout_v1",
    "stop_cluster_hunt_v1",
    "cross_venue_liquidation_divergence_v1",
}

WAVE_2B_IDS = {
    "crowded_long_unwind_v1",
    "crowded_short_squeeze_v1",
    "liquidation_cluster_followthrough_v1",
    "liquidation_cluster_exhaustion_v1",
}

WAVE_2C_IDS = {
    "perp_basis_momentum_v1",
    "perp_basis_reversion_v2",
    "funding_curve_dislocation_v1",
    "carry_risk_off_v1",
}

WAVE_2_IDS = WAVE_2A_IDS | WAVE_2B_IDS | WAVE_2C_IDS


def _spec(
    *,
    strategy_id: str,
    version: str,
    family: str,
    subwave: str,
    horizons: list[str],
    features: list[str],
    regimes: list[str],
    risk_tags: list[str],
) -> StrategySpec:
    return StrategySpec(
        strategy_id=strategy_id,
        version=version,
        family=family,
        supported_assets=["*"],
        supported_venues=PERP_DEX_VENUES,
        supported_horizons=horizons,
        required_features=features,
        valid_regimes=regimes,
        max_candidates_per_run=1,
        max_allocation_share_pct=25.0,
        cooldown_ms=0,
        min_confidence=0.35,
        min_ev_bps=8.0,
        risk_tags=risk_tags,
        enabled=True,
        counts_for_breadth=True,
        metadata={
            "wave": "2",
            "subwave": subwave,
            "activation_scope": "paper_shadow",
            "paper_eligible": True,
            "operator_promotion_required": False,
            "integration_status": "first_class",
            "replayable": True,
        },
    )


class Wave2Strategy:
    spec: StrategySpec
    strategy_id: str

    def generate(self, snapshot: FeatureSnapshot | None, regime: RegimeVector | None, *, timestamp_ms: int) -> list[AlphaCandidate]:
        if not self.spec.enabled or snapshot is None or regime is None:
            return []
        return _wave2_candidate(self.spec, snapshot, regime, timestamp_ms=timestamp_ms)


class CrossVenueLeadLagStrategy(Wave2Strategy):
    spec = _spec(
        strategy_id="cross_venue_lead_lag_v1",
        version="1.0.0",
        family="cross_venue_liquidity",
        subwave="2A",
        horizons=["5m", "15m"],
        features=["mid", "cross_venue_mid_delta_bps", "cross_venue_volume_imbalance", "spread_bps", "top_depth_usd"],
        regimes=["normal", "expanding", "buy_pressure", "sell_pressure"],
        risk_tags=["cross_venue", "lead_lag", "liquidity_map"],
    )
    strategy_id = spec.strategy_id


class LiquidityVacuumBreakoutStrategy(Wave2Strategy):
    spec = _spec(
        strategy_id="liquidity_vacuum_breakout_v1",
        version="1.0.0",
        family="cross_venue_liquidity",
        subwave="2A",
        horizons=["5m", "15m"],
        features=["mid", "top_depth_usd", "depth_thinning_5m_pct", "mid_return_5m_bps", "spread_bps"],
        regimes=["thin", "impaired", "expanding", "breakdown"],
        risk_tags=["liquidity_vacuum", "breakout", "depth_thinning"],
    )
    strategy_id = spec.strategy_id


class StopClusterHuntStrategy(Wave2Strategy):
    spec = _spec(
        strategy_id="stop_cluster_hunt_v1",
        version="1.0.0",
        family="cross_venue_liquidity",
        subwave="2A",
        horizons=["5m", "15m"],
        features=["mid", "stop_cluster_distance_bps", "top_depth_usd", "mid_return_5m_bps", "liq_notional_5m"],
        regimes=["expanding", "long_flush", "short_squeeze", "buy_pressure", "sell_pressure"],
        risk_tags=["stop_cluster", "liquidity_map", "forced_flow"],
    )
    strategy_id = spec.strategy_id


class CrossVenueLiquidationDivergenceStrategy(Wave2Strategy):
    spec = _spec(
        strategy_id="cross_venue_liquidation_divergence_v1",
        version="1.0.0",
        family="cross_venue_liquidation",
        subwave="2A",
        horizons=["5m", "15m"],
        features=["liq_notional_5m", "cross_venue_liq_imbalance", "long_vs_short_liq_imbalance_5m", "mid_return_5m_bps"],
        regimes=["long_flush", "short_squeeze", "mixed", "expanding"],
        risk_tags=["cross_venue", "liquidation_divergence", "forced_flow"],
    )
    strategy_id = spec.strategy_id


class CrowdedLongUnwindStrategy(Wave2Strategy):
    spec = _spec(
        strategy_id="crowded_long_unwind_v1",
        version="1.0.0",
        family="crowding_forced_flow",
        subwave="2B",
        horizons=["15m", "1h"],
        features=["funding_hourly", "oi_delta_5m_pct", "liq_notional_5m", "mid_return_5m_bps", "depth_thinning_5m_pct"],
        regimes=["positive_extreme", "long_flush", "expanding", "thin"],
        risk_tags=["crowded_long", "unwind", "forced_flow"],
    )
    strategy_id = spec.strategy_id


class CrowdedShortSqueezeStrategy(Wave2Strategy):
    spec = _spec(
        strategy_id="crowded_short_squeeze_v1",
        version="1.0.0",
        family="crowding_forced_flow",
        subwave="2B",
        horizons=["15m", "1h"],
        features=["funding_hourly", "oi_delta_5m_pct", "liq_notional_5m", "mid_return_5m_bps", "depth_thinning_5m_pct"],
        regimes=["negative_extreme", "short_squeeze", "expanding", "thin"],
        risk_tags=["crowded_short", "squeeze", "forced_flow"],
    )
    strategy_id = spec.strategy_id


class LiquidationClusterFollowthroughStrategy(Wave2Strategy):
    spec = _spec(
        strategy_id="liquidation_cluster_followthrough_v1",
        version="1.0.0",
        family="crowding_forced_flow",
        subwave="2B",
        horizons=["5m", "15m"],
        features=["liq_notional_5m", "long_vs_short_liq_imbalance_5m", "confirmed_only_liq_score_5m", "top_depth_usd", "mid_return_5m_bps"],
        regimes=["long_flush", "short_squeeze", "mixed", "expanding"],
        risk_tags=["liquidation_cluster", "followthrough", "forced_flow"],
    )
    strategy_id = spec.strategy_id


class LiquidationClusterExhaustionStrategy(Wave2Strategy):
    spec = _spec(
        strategy_id="liquidation_cluster_exhaustion_v1",
        version="1.0.0",
        family="crowding_forced_flow",
        subwave="2B",
        horizons=["15m", "1h"],
        features=["liq_notional_5m", "largest_single_liq_5m", "top_imbalance", "spread_bps", "mid_return_5m_bps"],
        regimes=["long_flush", "short_squeeze", "mixed", "range"],
        risk_tags=["liquidation_cluster", "exhaustion", "mean_reversion"],
    )
    strategy_id = spec.strategy_id


class PerpBasisMomentumStrategy(Wave2Strategy):
    spec = _spec(
        strategy_id="perp_basis_momentum_v1",
        version="1.0.0",
        family="perp_basis_carry_intelligence",
        subwave="2C",
        horizons=["1h", "4h"],
        features=["perp_basis_bps", "basis_delta_15m_bps", "funding_hourly", "oi_delta_5m_pct", "mid_return_5m_bps"],
        regimes=["expanding", "positive_extreme", "negative_extreme"],
        risk_tags=["perp_basis", "momentum", "carry_intelligence"],
    )
    strategy_id = spec.strategy_id


class PerpBasisReversionV2Strategy(Wave2Strategy):
    spec = _spec(
        strategy_id="perp_basis_reversion_v2",
        version="2.0.0",
        family="perp_basis_carry_intelligence",
        subwave="2C",
        horizons=["1h", "4h"],
        features=["perp_basis_bps", "basis_zscore", "realized_vol_15m_bps", "spread_bps", "top_depth_usd"],
        regimes=["compressed", "normal", "neutral", "range"],
        risk_tags=["perp_basis", "reversion", "relative_value"],
    )
    strategy_id = spec.strategy_id


class FundingCurveDislocationStrategy(Wave2Strategy):
    spec = _spec(
        strategy_id="funding_curve_dislocation_v1",
        version="1.0.0",
        family="perp_basis_carry_intelligence",
        subwave="2C",
        horizons=["1h", "4h"],
        features=["funding_hourly", "funding_curve_slope", "perp_basis_bps", "oi_delta_5m_pct", "source_consensus_score"],
        regimes=["positive_extreme", "negative_extreme", "neutral"],
        risk_tags=["funding_curve", "dislocation", "carry_intelligence"],
    )
    strategy_id = spec.strategy_id


class CarryRiskOffStrategy(Wave2Strategy):
    spec = _spec(
        strategy_id="carry_risk_off_v1",
        version="1.0.0",
        family="perp_basis_carry_intelligence",
        subwave="2C",
        horizons=["15m", "1h"],
        features=["funding_hourly", "oi_delta_5m_pct", "liq_notional_5m", "depth_thinning_5m_pct", "event_risk_pressure"],
        regimes=["positive_extreme", "negative_extreme", "event_risk", "risk_off", "thin"],
        risk_tags=["carry_risk_off", "funding", "crowding"],
    )
    strategy_id = spec.strategy_id


def _wave2_candidate(spec: StrategySpec, snapshot: FeatureSnapshot, regime: RegimeVector, *, timestamp_ms: int) -> list[AlphaCandidate]:
    mid = _float(snapshot.features.get("mid"))
    if mid is None or mid <= 0:
        return []
    data = _wave2_signal(spec.strategy_id, snapshot.features)
    if data is None:
        return []
    side, horizon, score, stop_bps, rr, thesis, metadata, expected_edge_bps = data
    stop, target = _stop_target(mid, side, stop_bps=stop_bps, rr=rr)
    cid = "cand_" + hashlib.sha1(f"{spec.strategy_id}:{snapshot.asset}:{side}:{timestamp_ms // 30_000}:{round(mid, 6)}".encode()).hexdigest()[:24]
    return [
        AlphaCandidate(
            candidate_id=cid,
            strategy_id=spec.strategy_id,
            **candidate_contract_fields(spec, snapshot, expected_edge_bps=expected_edge_bps),
            asset=snapshot.asset,
            asset_class=_asset_class(snapshot.underlying_id),
            venue=snapshot.venue_id,
            instrument_id=snapshot.instrument_id,
            underlying_id=snapshot.underlying_id,
            venue_id=snapshot.venue_id,
            provider_symbol=snapshot.provider_symbol,
            side=side,  # type: ignore[arg-type]
            horizon=horizon,
            proposed_entry=mid,
            stop=stop,
            targets=[max(target, 0.00000001)],
            thesis=f"{snapshot.asset} {side} Wave 2: {thesis}",
            invalidation_conditions=["Wave 2 feature edge decays", "RiskGateway/Council rejects intent", f"Price trades through {stop:.6g}"],
            feature_snapshot_id=snapshot.snapshot_id,
            regime_snapshot_id=regime.regime_snapshot_id,
            source_event_ids=[],
            raw_alpha_score=round(max(0.0, min(100.0, score)), 2),
            confidence=round(min(0.88, 0.32 + score / 190.0), 3),
            created_at_ms=timestamp_ms,
            expires_at_ms=timestamp_ms + _horizon_ms(horizon),
            metadata={**metadata, "regime_label": regime.regime_label, "strategy_family": spec.family, "wave": "2", "activation_scope": spec.metadata.get("activation_scope", "disabled")},
        )
    ]


def _wave2_signal(strategy_id: str, features: dict[str, Any]) -> tuple[str, str, float, float, float, str, dict[str, Any], float] | None:
    if strategy_id == "cross_venue_lead_lag_v1":
        delta = _float(features.get("cross_venue_mid_delta_bps"))
        volume_imbalance = _float(features.get("cross_venue_volume_imbalance")) or 0.0
        spread = _float(features.get("spread_bps"))
        depth = _float(features.get("top_depth_usd")) or 0.0
        if delta is None or spread is None or spread > 18 or depth < 100_000 or abs(delta) < 4 or abs(volume_imbalance) < 0.1:
            return None
        side = "long" if delta > 0 else "short"
        score = 44.0 + min(24.0, abs(delta) * 3.0) + min(18.0, abs(volume_imbalance) * 30.0) + min(12.0, depth / 250_000.0)
        return side, "5m", score, 24.0, 1.55, f"cross-venue lead/lag delta {delta:.1f} bps with volume imbalance {volume_imbalance:.2f}", {"cross_venue_mid_delta_bps": delta, "cross_venue_volume_imbalance": volume_imbalance, "spread_bps": spread, "top_depth_usd": depth}, max(0.0, score - 48.0) / 2.5

    if strategy_id == "liquidity_vacuum_breakout_v1":
        thinning = _float(features.get("depth_thinning_5m_pct"))
        ret_5m = _float(features.get("mid_return_5m_bps"))
        spread = _float(features.get("spread_bps"))
        depth = _float(features.get("top_depth_usd")) or 0.0
        if thinning is None or ret_5m is None or spread is None or thinning < 25 or abs(ret_5m) < 14 or spread > 25:
            return None
        side = "long" if ret_5m > 0 else "short"
        score = 43.0 + min(24.0, thinning / 2.0) + min(20.0, abs(ret_5m) / 2.0) + max(0.0, 12.0 - min(spread, 12.0))
        return side, "5m", score, max(26.0, abs(ret_5m) * 0.8), 1.75, f"liquidity vacuum breakout with depth thinning {thinning:.1f}% and return {ret_5m:.1f} bps", {"depth_thinning_5m_pct": thinning, "mid_return_5m_bps": ret_5m, "spread_bps": spread, "top_depth_usd": depth}, max(0.0, score - 50.0) / 2.4

    if strategy_id == "stop_cluster_hunt_v1":
        distance = _float(features.get("stop_cluster_distance_bps"))
        ret_5m = _float(features.get("mid_return_5m_bps"))
        liq = _float(features.get("liq_notional_5m")) or 0.0
        depth = _float(features.get("top_depth_usd")) or 0.0
        if distance is None or ret_5m is None or distance > 22 or abs(ret_5m) < 8 or depth < 75_000:
            return None
        side = "long" if ret_5m > 0 else "short"
        score = 42.0 + max(0.0, 22.0 - distance) * 1.2 + min(22.0, abs(ret_5m) / 2.0) + min(12.0, liq / 100_000.0)
        return side, "5m", score, 24.0, 1.6, f"price is {distance:.1f} bps from inferred stop cluster with {ret_5m:.1f} bps impulse", {"stop_cluster_distance_bps": distance, "mid_return_5m_bps": ret_5m, "liq_notional_5m": liq, "top_depth_usd": depth}, max(0.0, score - 48.0) / 2.6

    if strategy_id == "cross_venue_liquidation_divergence_v1":
        cross = _float(features.get("cross_venue_liq_imbalance"))
        local = _float(features.get("long_vs_short_liq_imbalance_5m")) or 0.0
        liq = _float(features.get("liq_notional_5m")) or 0.0
        ret_5m = _float(features.get("mid_return_5m_bps")) or 0.0
        if cross is None or abs(cross) < 75_000 or liq < 75_000 or abs(cross - local) < 50_000:
            return None
        side = "short" if cross > 0 else "long"
        score = 44.0 + min(28.0, abs(cross) / 50_000.0) + min(14.0, abs(cross - local) / 75_000.0) + min(10.0, abs(ret_5m) / 4.0)
        return side, "15m", score, 34.0, 1.7, f"cross-venue liquidation imbalance ${cross:,.0f} diverges from local ${local:,.0f}", {"cross_venue_liq_imbalance": cross, "long_vs_short_liq_imbalance_5m": local, "liq_notional_5m": liq, "mid_return_5m_bps": ret_5m}, max(0.0, score - 50.0) / 2.3

    if strategy_id in {"crowded_long_unwind_v1", "crowded_short_squeeze_v1"}:
        funding = _float(features.get("funding_hourly"))
        oi_delta = _float(features.get("oi_delta_5m_pct")) or 0.0
        liq = _float(features.get("liq_notional_5m")) or 0.0
        ret_5m = _float(features.get("mid_return_5m_bps")) or 0.0
        thinning = _float(features.get("depth_thinning_5m_pct")) or 0.0
        long_unwind = strategy_id == "crowded_long_unwind_v1"
        if funding is None:
            return None
        if long_unwind:
            if funding < 0.00012 or (ret_5m > -10 and liq < 100_000) or thinning < 10:
                return None
            side = "short"
        else:
            if funding > -0.00008 or (ret_5m < 10 and liq < 100_000) or thinning < 10:
                return None
            side = "long"
        crowding = abs(funding) * 100_000 + max(0.0, oi_delta) * 4.0 + thinning / 2.0 + liq / 100_000.0
        score = min(100.0, 43.0 + min(42.0, crowding) + min(12.0, abs(ret_5m) / 3.0))
        label = "crowded long unwind" if long_unwind else "crowded short squeeze"
        return side, "15m", score, max(32.0, abs(ret_5m) * 0.8), 1.8, f"{label}: funding {funding:.5f}, OI {oi_delta:.1f}%, thinning {thinning:.1f}%", {"funding_hourly": funding, "oi_delta_5m_pct": oi_delta, "liq_notional_5m": liq, "mid_return_5m_bps": ret_5m, "depth_thinning_5m_pct": thinning}, max(0.0, score - 50.0) / 2.2

    if strategy_id == "liquidation_cluster_followthrough_v1":
        liq = _float(features.get("liq_notional_5m"))
        imbalance = _float(features.get("long_vs_short_liq_imbalance_5m"))
        confirmed = _float(features.get("confirmed_only_liq_score_5m")) or 0.0
        depth = _float(features.get("top_depth_usd")) or 0.0
        ret_5m = _float(features.get("mid_return_5m_bps")) or 0.0
        if liq is None or imbalance is None or liq < 120_000 or abs(imbalance) < 80_000 or confirmed < 0.25 or depth < 75_000:
            return None
        side = "short" if imbalance > 0 else "long"
        if (side == "short" and ret_5m > 10) or (side == "long" and ret_5m < -10):
            return None
        pressure = abs(imbalance) / max(liq, 1.0)
        score = 46.0 + min(26.0, pressure * 26.0) + min(18.0, liq / 100_000.0) + confirmed * 12.0
        return side, "5m", score, 34.0, 1.75, f"liquidation cluster followthrough ${liq:,.0f}, imbalance ${imbalance:,.0f}, confirmed {confirmed:.2f}", {"liq_notional_5m": liq, "long_vs_short_liq_imbalance_5m": imbalance, "confirmed_only_liq_score_5m": confirmed, "top_depth_usd": depth, "mid_return_5m_bps": ret_5m}, max(0.0, score - 50.0) / 2.1

    if strategy_id == "liquidation_cluster_exhaustion_v1":
        liq = _float(features.get("liq_notional_5m"))
        largest = _float(features.get("largest_single_liq_5m")) or 0.0
        imbalance = _float(features.get("top_imbalance")) or 0.0
        spread = _float(features.get("spread_bps"))
        ret_5m = _float(features.get("mid_return_5m_bps")) or 0.0
        if liq is None or spread is None or liq < 100_000 or largest < 25_000 or spread > 20 or abs(ret_5m) < 15:
            return None
        side = "long" if ret_5m < 0 else "short"
        score = 44.0 + min(26.0, largest / max(liq, 1.0) * 45.0) + min(18.0, abs(ret_5m) / 2.0) + max(0.0, 20.0 - spread)
        return side, "15m", score, 38.0, 1.55, f"liquidation exhaustion after ${liq:,.0f} cluster and {ret_5m:.1f} bps move", {"liq_notional_5m": liq, "largest_single_liq_5m": largest, "top_imbalance": imbalance, "spread_bps": spread, "mid_return_5m_bps": ret_5m}, max(0.0, score - 49.0) / 2.4

    if strategy_id == "perp_basis_momentum_v1":
        basis = _float(features.get("perp_basis_bps")) or 0.0
        basis_delta = _float(features.get("basis_delta_15m_bps"))
        funding = _float(features.get("funding_hourly")) or 0.0
        oi_delta = _float(features.get("oi_delta_5m_pct")) or 0.0
        ret_5m = _float(features.get("mid_return_5m_bps")) or 0.0
        if basis_delta is None or abs(basis_delta) < 4 or oi_delta < 1.0 or abs(ret_5m) < 8:
            return None
        side = "long" if basis_delta > 0 and ret_5m > 0 else "short" if basis_delta < 0 and ret_5m < 0 else None
        if side is None:
            return None
        score = 43.0 + min(28.0, abs(basis_delta) * 3.0) + min(18.0, oi_delta * 3.0) + min(12.0, abs(funding) * 100_000)
        return side, "1h", score, max(35.0, abs(ret_5m)), 1.75, f"perp basis momentum: basis {basis:.1f} bps, 15m delta {basis_delta:.1f} bps", {"perp_basis_bps": basis, "basis_delta_15m_bps": basis_delta, "funding_hourly": funding, "oi_delta_5m_pct": oi_delta, "mid_return_5m_bps": ret_5m}, max(0.0, score - 50.0) / 2.3

    if strategy_id == "perp_basis_reversion_v2":
        basis = _float(features.get("perp_basis_bps")) or 0.0
        zscore = _float(features.get("basis_zscore"))
        vol = _float(features.get("realized_vol_15m_bps"))
        spread = _float(features.get("spread_bps"))
        depth = _float(features.get("top_depth_usd")) or 0.0
        if zscore is None or vol is None or spread is None or abs(zscore) < 1.3 or vol > 95 or spread > 14 or depth < 100_000:
            return None
        side = "short" if zscore > 0 else "long"
        score = 45.0 + min(30.0, abs(zscore) * 12.0) + max(0.0, 95.0 - vol) / 5.0 + max(0.0, 14.0 - spread)
        return side, "1h", score, max(30.0, vol * 0.5), 1.45, f"perp basis reversion z-score {zscore:.2f}, basis {basis:.1f} bps", {"perp_basis_bps": basis, "basis_zscore": zscore, "realized_vol_15m_bps": vol, "spread_bps": spread, "top_depth_usd": depth}, max(0.0, score - 49.0) / 2.2

    if strategy_id == "funding_curve_dislocation_v1":
        funding = _float(features.get("funding_hourly"))
        slope = _float(features.get("funding_curve_slope"))
        basis = _float(features.get("perp_basis_bps")) or 0.0
        oi_delta = _float(features.get("oi_delta_5m_pct")) or 0.0
        source_score = _float(features.get("source_consensus_score")) or 0.0
        if funding is None or slope is None or source_score < 0.5 or (abs(funding) < 0.0001 and abs(slope) < 0.00008):
            return None
        composite = funding + slope
        side = "short" if composite > 0 else "long"
        score = 43.0 + min(28.0, abs(composite) * 100_000) + min(14.0, abs(basis) / 3.0) + min(12.0, max(0.0, oi_delta) * 2.0) + source_score * 8.0
        return side, "1h", score, 42.0, 1.55, f"funding curve dislocation: funding {funding:.5f}, slope {slope:.5f}, basis {basis:.1f} bps", {"funding_hourly": funding, "funding_curve_slope": slope, "perp_basis_bps": basis, "oi_delta_5m_pct": oi_delta, "source_consensus_score": source_score}, max(0.0, score - 50.0) / 2.4

    if strategy_id == "carry_risk_off_v1":
        funding = _float(features.get("funding_hourly")) or 0.0
        oi_delta = _float(features.get("oi_delta_5m_pct")) or 0.0
        liq = _float(features.get("liq_notional_5m")) or 0.0
        thinning = _float(features.get("depth_thinning_5m_pct")) or 0.0
        event_risk = _float(features.get("event_risk_pressure")) or 0.0
        if event_risk < 0.35 and thinning < 25 and liq < 150_000:
            return None
        side = "short" if funding >= 0 else "long"
        score = 42.0 + event_risk * 24.0 + min(22.0, thinning / 2.0) + min(16.0, liq / 100_000.0) + min(10.0, max(0.0, oi_delta) * 2.0)
        return side, "15m", score, 40.0, 1.5, f"carry risk-off: event risk {event_risk:.2f}, thinning {thinning:.1f}%, liquidations ${liq:,.0f}", {"funding_hourly": funding, "oi_delta_5m_pct": oi_delta, "liq_notional_5m": liq, "depth_thinning_5m_pct": thinning, "event_risk_pressure": event_risk}, max(0.0, score - 50.0) / 2.5

    return None


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


def _asset_class(underlying_id: str) -> str:
    return {
        "CRYPTO": "crypto",
        "EQUITY": "equity",
        "ETF": "equity",
        "INDEX": "macro",
        "FX": "fx",
        "COMMODITY": "commodity",
    }.get(underlying_id.split(":", 1)[0].upper(), "unknown")


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def wave_2_strategy_instances() -> list[Any]:
    return [
        CrossVenueLeadLagStrategy(),
        LiquidityVacuumBreakoutStrategy(),
        StopClusterHuntStrategy(),
        CrossVenueLiquidationDivergenceStrategy(),
        CrowdedLongUnwindStrategy(),
        CrowdedShortSqueezeStrategy(),
        LiquidationClusterFollowthroughStrategy(),
        LiquidationClusterExhaustionStrategy(),
        PerpBasisMomentumStrategy(),
        PerpBasisReversionV2Strategy(),
        FundingCurveDislocationStrategy(),
        CarryRiskOffStrategy(),
    ]


def wave_2_specs(*, enabled: bool = True) -> list[StrategySpec]:
    specs = [strategy.spec for strategy in wave_2_strategy_instances()]
    if enabled:
        return specs
    return [
        spec.model_copy(
            update={
                "enabled": False,
                "counts_for_breadth": False,
                "metadata": {**spec.metadata, "runtime_enabled_reason": "catalog_excludes_wave2"},
            }
        )
        for spec in specs
    ]
