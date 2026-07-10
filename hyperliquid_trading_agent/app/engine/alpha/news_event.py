from __future__ import annotations

import hashlib
from typing import Any

from hyperliquid_trading_agent.app.engine.alpha.base import (
    CORE_CRYPTO_ASSETS,
    HYPERLIQUID_VENUES,
    candidate_contract_fields,
)
from hyperliquid_trading_agent.app.engine.schemas import AlphaCandidate, FeatureSnapshot, RegimeVector, StrategySpec


class NewsEventAlphaStrategy:
    spec = StrategySpec(
        strategy_id="news_event_alpha_v2",
        version="2.0.0",
        family="event_driven_news",
        supported_assets=CORE_CRYPTO_ASSETS,
        supported_venues=HYPERLIQUID_VENUES,
        supported_horizons=["1h"],
        required_features=["mid", "news_story_impact", "news_direction_confidence", "news_source_quality", "news_story_context"],
        valid_regimes=["news_catalyst", "event_risk"],
        max_candidates_per_run=1,
        max_allocation_share_pct=45.0,
        cooldown_ms=600_000,
        min_confidence=0.50,
        min_ev_bps=8.0,
        risk_tags=["event_driven", "news", "catalyst"],
    )
    strategy_id = spec.strategy_id

    def __init__(self) -> None:
        self.mode = "shadow"
        self.min_impact = 0.65
        self.min_direction_confidence = 0.75
        self.max_age_seconds = 1800

    def configure(self, settings) -> None:
        self.mode = str(getattr(settings, "engine_news_alpha_mode", "shadow"))
        self.min_impact = float(getattr(settings, "engine_news_alpha_min_impact", 65.0)) / 100.0
        self.min_direction_confidence = float(getattr(settings, "engine_news_alpha_min_direction_confidence", 0.75))
        self.max_age_seconds = int(getattr(settings, "engine_news_alpha_max_age_seconds", 1800))

    def generate(self, snapshot: FeatureSnapshot, regime: RegimeVector, *, timestamp_ms: int) -> list[AlphaCandidate]:
        observed_risk_mode = str(regime.metadata.get("observed_news_risk_mode") or regime.news_risk_mode)
        if self.mode == "off" or not regime.permissions.news_event_allowed or observed_risk_mode == "shock":
            return []
        mid = _float(snapshot.features.get("mid"))
        impact = _float(snapshot.features.get("news_story_impact")) or 0.0
        direction_confidence = _float(snapshot.features.get("news_direction_confidence")) or 0.0
        source_quality = _float(snapshot.features.get("news_source_quality")) or 0.0
        source_count = _float(snapshot.features.get("news_independent_source_count")) or 1.0
        raw_context = snapshot.features.get("news_story_context")
        context: dict[str, Any] = raw_context if isinstance(raw_context, dict) else {}
        direction = _float(context.get("direction_score")) or 0.0
        engine_action = str(context.get("engine_action") or "")
        story_ts_ms = _int(context.get("published_at_ms")) or _int(context.get("received_at_ms"))
        story_age_ms = timestamp_ms - story_ts_ms if story_ts_ms is not None else None
        trusted_or_corroborated = source_quality >= 0.90 or source_count >= 2
        if (
            mid is None
            or mid <= 0
            or impact < self.min_impact
            or direction_confidence < self.min_direction_confidence
            or source_quality < 0.70
            or not trusted_or_corroborated
            or engine_action != "directional_feature"
            or direction == 0
            or story_age_ms is None
            or story_age_ms < 0
            or story_age_ms > self.max_age_seconds * 1000
        ):
            return []
        side = "long" if direction > 0 else "short"
        if not _market_confirms(snapshot, side):
            return []
        if side == "long" and observed_risk_mode == "risk_off":
            return []
        stop = mid * (0.99 if side == "long" else 1.01)
        target = mid + 2.0 * abs(mid - stop) if side == "long" else mid - 2.0 * abs(mid - stop)
        score = min(100.0, 50.0 + impact * 20.0 + direction_confidence * 15.0 + source_quality * 10.0 + regime.regime_stability_score * 5.0)
        story_id = str(context.get("story_id") or "")
        source_ids = [str(item) for item in context.get("story_member_event_ids") or []]
        digest = hashlib.sha1(f"{snapshot.asset}:{self.strategy_id}:{side}:{story_id}:{timestamp_ms // 60_000}".encode()).hexdigest()[:24]
        return [
            AlphaCandidate(
                candidate_id="cand_" + digest,
                strategy_id=self.strategy_id,
                **candidate_contract_fields(self.spec, snapshot, expected_edge_bps=max(0.0, score - 50.0) / 2.0),
                asset=snapshot.asset,
                asset_class="crypto",
                venue="hyperliquid",
                side=side,  # type: ignore[arg-type]
                horizon="1h",
                proposed_entry=mid,
                stop=stop,
                targets=[max(target, 0.00000001)],
                thesis=f"{snapshot.asset} {side} corroborated news catalyst; impact={impact:.2f}, direction_confidence={direction_confidence:.2f}.",
                invalidation_conditions=["Catalyst contradicted by reliable source", f"Price trades through {stop:.6g}"],
                feature_snapshot_id=snapshot.snapshot_id,
                regime_snapshot_id=regime.regime_snapshot_id,
                source_event_ids=source_ids,
                raw_alpha_score=round(score, 2),
                confidence=round(min(0.9, 0.35 + score / 170.0), 3),
                created_at_ms=timestamp_ms,
                expires_at_ms=timestamp_ms + 30 * 60 * 1000,
                metadata={
                    "story_id": story_id,
                    "impact": impact,
                    "direction_confidence": direction_confidence,
                    "source_quality": source_quality,
                    "independent_source_count": source_count,
                    "news_alpha_mode": self.mode,
                    "paper_only": True,
                },
            )
        ]


def _float(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _int(value) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _market_confirms(snapshot: FeatureSnapshot, side: str) -> bool:
    returns = _float(snapshot.features.get("mid_return_5m_bps"))
    imbalance = _float(snapshot.features.get("top_imbalance"))
    spread = _float(snapshot.features.get("spread_bps"))
    if returns is None or imbalance is None or spread is None or spread > 20.0:
        return False
    if side == "long":
        return returns >= 20.0 and imbalance >= 0.0
    return returns <= -20.0 and imbalance <= 0.0
