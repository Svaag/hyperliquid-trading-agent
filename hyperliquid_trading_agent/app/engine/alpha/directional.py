from __future__ import annotations

import hashlib

from hyperliquid_trading_agent.app.engine.alpha.base import (
    CORE_CRYPTO_ASSETS,
    HYPERLIQUID_VENUES,
    candidate_contract_fields,
)
from hyperliquid_trading_agent.app.engine.schemas import AlphaCandidate, FeatureSnapshot, RegimeVector, StrategySpec


class DirectionalMomentumStrategy:
    spec = StrategySpec(
        strategy_id="directional_momentum_v2",
        version="2.0.0",
        family="trend_following",
        supported_assets=CORE_CRYPTO_ASSETS,
        supported_venues=HYPERLIQUID_VENUES,
        supported_horizons=["30m"],
        required_features=["mid"],
        valid_regimes=["bull", "bear"],
        max_candidates_per_run=1,
        max_allocation_share_pct=45.0,
        cooldown_ms=300_000,
        min_confidence=0.35,
        min_ev_bps=8.0,
        risk_tags=["directional", "momentum", "trend"],
    )
    strategy_id = spec.strategy_id

    def generate(self, snapshot: FeatureSnapshot, regime: RegimeVector, *, timestamp_ms: int) -> list[AlphaCandidate]:
        if not regime.permissions.momentum_allowed:
            return []
        mid = _float(snapshot.features.get("mid"))
        if mid is None or mid <= 0:
            return []
        side = "long" if regime.trend_state == "bull" else "short" if regime.trend_state == "bear" else None
        if side is None:
            return []
        stop = mid * (0.992 if side == "long" else 1.008)
        target = mid + 2.2 * abs(mid - stop) if side == "long" else mid - 2.2 * abs(mid - stop)
        if target <= 0:
            return []
        score = min(100.0, 45.0 + regime.trend_confidence * 30.0 + regime.regime_stability_score * 25.0)
        cid = _candidate_id(snapshot.asset, self.strategy_id, side, timestamp_ms, mid)
        return [
            AlphaCandidate(
                candidate_id=cid,
                strategy_id=self.strategy_id,
                **candidate_contract_fields(self.spec, snapshot, expected_edge_bps=max(0.0, score - 50.0) / 2.0),
                asset=snapshot.asset,
                asset_class="crypto",
                venue="hyperliquid",
                side=side,  # type: ignore[arg-type]
                horizon="30m",
                proposed_entry=mid,
                stop=stop,
                targets=[target],
                thesis=f"{snapshot.asset} {side} momentum candidate under {regime.trend_state} regime.",
                invalidation_conditions=[f"{snapshot.asset} invalidates on sustained trade through {stop:.6g}"],
                feature_snapshot_id=snapshot.snapshot_id,
                regime_snapshot_id=regime.regime_snapshot_id,
                source_event_ids=[],
                raw_alpha_score=round(score, 2),
                confidence=round(min(0.95, 0.35 + score / 160.0), 3),
                created_at_ms=timestamp_ms,
                expires_at_ms=timestamp_ms + 30 * 60 * 1000,
                metadata={"regime_stability_score": regime.regime_stability_score},
            )
        ]


class SupportResistanceReversionStrategy:
    spec = StrategySpec(
        strategy_id="support_resistance_reversion_v2",
        version="2.0.0",
        family="mean_reversion",
        supported_assets=CORE_CRYPTO_ASSETS,
        supported_venues=HYPERLIQUID_VENUES,
        supported_horizons=["15m"],
        required_features=["mid", "top_imbalance"],
        valid_regimes=["range"],
        max_candidates_per_run=1,
        max_allocation_share_pct=45.0,
        cooldown_ms=180_000,
        min_confidence=0.30,
        min_ev_bps=8.0,
        risk_tags=["mean_reversion", "orderflow", "support_resistance"],
    )
    strategy_id = spec.strategy_id

    def generate(self, snapshot: FeatureSnapshot, regime: RegimeVector, *, timestamp_ms: int) -> list[AlphaCandidate]:
        if not regime.permissions.mean_reversion_allowed:
            return []
        mid = _float(snapshot.features.get("mid"))
        if mid is None or mid <= 0:
            return []
        imbalance = _float(snapshot.features.get("top_imbalance")) or 0.0
        if abs(imbalance) < 0.25:
            return []
        side = "long" if imbalance > 0 else "short"
        stop = mid * (0.995 if side == "long" else 1.005)
        target = mid + 1.6 * abs(mid - stop) if side == "long" else mid - 1.6 * abs(mid - stop)
        cid = _candidate_id(snapshot.asset, self.strategy_id, side, timestamp_ms, mid)
        score = min(100.0, 50.0 + abs(imbalance) * 25.0 + regime.regime_stability_score * 20.0)
        return [
            AlphaCandidate(
                candidate_id=cid,
                strategy_id=self.strategy_id,
                **candidate_contract_fields(self.spec, snapshot, expected_edge_bps=max(0.0, score - 50.0) / 2.5),
                asset=snapshot.asset,
                asset_class="crypto",
                venue="hyperliquid",
                side=side,  # type: ignore[arg-type]
                horizon="15m",
                proposed_entry=mid,
                stop=stop,
                targets=[max(target, 0.00000001)],
                thesis=f"{snapshot.asset} {side} mean-reversion candidate from range regime and book imbalance.",
                invalidation_conditions=[f"Book imbalance fades and price trades through {stop:.6g}"],
                feature_snapshot_id=snapshot.snapshot_id,
                regime_snapshot_id=regime.regime_snapshot_id,
                source_event_ids=[],
                raw_alpha_score=round(score, 2),
                confidence=round(min(0.9, 0.30 + score / 170.0), 3),
                created_at_ms=timestamp_ms,
                expires_at_ms=timestamp_ms + 15 * 60 * 1000,
                metadata={"top_imbalance": imbalance},
            )
        ]


def _candidate_id(asset: str, strategy_id: str, side: str, timestamp_ms: int, px: float) -> str:
    digest = hashlib.sha1(f"{asset}:{strategy_id}:{side}:{timestamp_ms // 60_000}:{round(px, 6)}".encode()).hexdigest()[:24]
    return "cand_" + digest


def _float(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
