from __future__ import annotations

import hashlib

from hyperliquid_trading_agent.app.engine.schemas import AlphaCandidate, FeatureSnapshot, RegimeVector


class NewsEventAlphaStrategy:
    strategy_id = "news_event_alpha_v1"

    def generate(self, snapshot: FeatureSnapshot, regime: RegimeVector, *, timestamp_ms: int) -> list[AlphaCandidate]:
        if not regime.permissions.news_event_allowed:
            return []
        mid = _float(snapshot.features.get("mid"))
        pressure = _float(snapshot.features.get("catalyst_pressure")) or 0.0
        if mid is None or mid <= 0 or abs(pressure) < 0.35:
            return []
        side = "long" if pressure > 0 else "short"
        stop = mid * (0.99 if side == "long" else 1.01)
        target = mid + 2.0 * abs(mid - stop) if side == "long" else mid - 2.0 * abs(mid - stop)
        score = min(100.0, 55.0 + abs(pressure) * 30.0 + regime.regime_stability_score * 10.0)
        digest = hashlib.sha1(f"{snapshot.asset}:{self.strategy_id}:{side}:{timestamp_ms // 60_000}:{pressure:.3f}".encode()).hexdigest()[:24]
        return [
            AlphaCandidate(
                candidate_id="cand_" + digest,
                strategy_id=self.strategy_id,
                asset=snapshot.asset,
                asset_class="crypto",
                venue="hyperliquid",
                side=side,  # type: ignore[arg-type]
                horizon="1h",
                proposed_entry=mid,
                stop=stop,
                targets=[max(target, 0.00000001)],
                thesis=f"{snapshot.asset} {side} first-pass catalyst candidate; pressure={pressure:.2f}.",
                invalidation_conditions=["Catalyst contradicted by reliable source", f"Price trades through {stop:.6g}"],
                feature_snapshot_id=snapshot.snapshot_id,
                regime_snapshot_id=regime.regime_snapshot_id,
                source_event_ids=[],
                raw_alpha_score=round(score, 2),
                confidence=round(min(0.9, 0.35 + score / 170.0), 3),
                created_at_ms=timestamp_ms,
                expires_at_ms=timestamp_ms + 60 * 60 * 1000,
                metadata={"catalyst_pressure": pressure},
            )
        ]


def _float(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
