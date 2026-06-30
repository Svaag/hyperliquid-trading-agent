from __future__ import annotations

import hashlib

from hyperliquid_trading_agent.app.engine.alpha.base import (
    CORE_CRYPTO_ASSETS,
    HYPERLIQUID_VENUES,
    candidate_contract_fields,
)
from hyperliquid_trading_agent.app.engine.schemas import AlphaCandidate, FeatureSnapshot, RegimeVector, StrategySpec


class MicrostructureOFIStrategy:
    spec = StrategySpec(
        strategy_id="microstructure_ofi_v1",
        version="1.0.0",
        family="microstructure_orderflow",
        supported_assets=CORE_CRYPTO_ASSETS,
        supported_venues=HYPERLIQUID_VENUES,
        supported_horizons=["5m"],
        required_features=["mid", "spread_bps", "top_imbalance"],
        valid_regimes=["balanced", "buy_pressure", "sell_pressure"],
        max_candidates_per_run=1,
        max_allocation_share_pct=45.0,
        cooldown_ms=60_000,
        min_confidence=0.25,
        min_ev_bps=8.0,
        risk_tags=["microstructure", "ofi", "short_horizon"],
    )
    strategy_id = spec.strategy_id

    def generate(self, snapshot: FeatureSnapshot, regime: RegimeVector, *, timestamp_ms: int) -> list[AlphaCandidate]:
        if regime.liquidity_state == "impaired" or regime.spread_state == "wide":
            return []
        mid = _float(snapshot.features.get("mid"))
        imbalance = _float(snapshot.features.get("top_imbalance")) or 0.0
        spread = _float(snapshot.features.get("spread_bps"))
        if mid is None or mid <= 0 or spread is None or spread > 20 or abs(imbalance) < 0.35:
            return []
        side = "long" if imbalance > 0 else "short"
        stop = mid * (0.997 if side == "long" else 1.003)
        target = mid + 1.4 * abs(mid - stop) if side == "long" else mid - 1.4 * abs(mid - stop)
        score = min(100.0, 40.0 + abs(imbalance) * 35.0 + max(0.0, 20 - spread))
        digest = hashlib.sha1(f"{snapshot.asset}:{self.strategy_id}:{side}:{timestamp_ms // 10_000}:{imbalance:.3f}".encode()).hexdigest()[:24]
        return [
            AlphaCandidate(
                candidate_id="cand_" + digest,
                strategy_id=self.strategy_id,
                **candidate_contract_fields(self.spec, snapshot, expected_edge_bps=max(0.0, score - 45.0) / 3.0),
                asset=snapshot.asset,
                asset_class="crypto",
                venue="hyperliquid",
                side=side,  # type: ignore[arg-type]
                horizon="5m",
                proposed_entry=mid,
                stop=stop,
                targets=[max(target, 0.00000001)],
                thesis=f"{snapshot.asset} {side} short-horizon OFI candidate from top-book imbalance {imbalance:.2f}.",
                invalidation_conditions=["Orderflow imbalance fails to persist", f"Price trades through {stop:.6g}"],
                feature_snapshot_id=snapshot.snapshot_id,
                regime_snapshot_id=regime.regime_snapshot_id,
                source_event_ids=[],
                raw_alpha_score=round(score, 2),
                confidence=round(min(0.8, 0.25 + score / 180.0), 3),
                created_at_ms=timestamp_ms,
                expires_at_ms=timestamp_ms + 5 * 60 * 1000,
                metadata={"top_imbalance": imbalance, "spread_bps": spread},
            )
        ]


def _float(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
