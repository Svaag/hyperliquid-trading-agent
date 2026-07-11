from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hyperliquid_trading_agent.app.engine.schemas import RegimeVector, StrategySpec


@dataclass
class StrategySelection:
    strategies: list[Any]
    skipped: list[dict[str, Any]]
    news_risk_tier: str

    def summary(self) -> dict[str, Any]:
        return {
            "selected": [strategy.spec.strategy_id for strategy in self.strategies],
            "skipped": self.skipped,
            "news_risk_tier": self.news_risk_tier,
        }


class ConservativeStrategySelector:
    """Deterministic pre-generation strategy gate.

    This selector is intentionally conservative: it can only remove enabled strategies
    from the current loop. It never enables a disabled strategy, flips wave flags, or
    changes paper/live execution settings.
    """

    def select(
        self,
        strategies: list[Any],
        regime: RegimeVector,
        *,
        asset: str | None = None,
        venue: str | None = None,
    ) -> StrategySelection:
        tier = _news_risk_tier(regime)
        selected: list[Any] = []
        skipped: list[dict[str, Any]] = []
        labels = _regime_labels(regime)
        for strategy in strategies:
            spec = strategy.spec
            if not spec.enabled:
                skipped.append(_skip(spec, "strategy_disabled", tier))
                continue
            if asset and not _supports_asset(spec, asset):
                skipped.append(_skip(spec, "unsupported_asset", tier))
                continue
            if venue and not _supports_venue(spec, venue):
                skipped.append(_skip(spec, "unsupported_venue", tier))
                continue
            if not _valid_for_regime(spec, labels):
                skipped.append(_skip(spec, "regime_mismatch", tier))
                continue
            suppression_reason = _news_suppression_reason(spec, tier)
            if suppression_reason is not None:
                skipped.append(_skip(spec, suppression_reason, tier))
                continue
            selected.append(strategy)
        return StrategySelection(strategies=selected, skipped=skipped, news_risk_tier=tier)


def _supports_asset(spec: StrategySpec, asset: str) -> bool:
    supported = {item.upper() for item in spec.supported_assets}
    return not supported or "*" in supported or asset.upper() in supported


def _supports_venue(spec: StrategySpec, venue: str) -> bool:
    supported = {_canonical_venue(item) for item in spec.supported_venues}
    return not supported or "*" in supported or _canonical_venue(venue) in supported


def _canonical_venue(venue: str) -> str:
    value = venue.strip().lower()
    return "hyperliquid:main" if value == "hyperliquid" else value


def _skip(spec: StrategySpec, reason: str, news_risk_tier: str) -> dict[str, Any]:
    return {
        "strategy_id": spec.strategy_id,
        "strategy_family": spec.family,
        "reason": reason,
        "news_risk_tier": news_risk_tier,
    }


def _valid_for_regime(spec: StrategySpec, labels: set[str]) -> bool:
    valid_regimes = set(spec.valid_regimes or [])
    if not valid_regimes:
        return True
    return bool(valid_regimes & labels) or _special_regime_match(valid_regimes, labels)


def _regime_labels(regime: RegimeVector) -> set[str]:
    return {
        regime.trend_state,
        regime.volatility_state,
        regime.funding_state,
        regime.oi_state,
        regime.liquidation_state,
        regime.orderflow_state,
        regime.news_state,
        regime.correlation_state,
        regime.session_state,
        regime.liquidity_state,
        regime.spread_state,
        regime.regime_label,
    }


def _special_regime_match(valid_regimes: set[str], labels: set[str]) -> bool:
    if "news_catalyst" in valid_regimes and "catalyst" in labels:
        return True
    if "event_risk" in valid_regimes and "catalyst" in labels:
        return True
    if "risk_off" in valid_regimes and ({"extreme", "impaired", "wide", "breakdown"} & labels):
        return True
    return False


def _news_risk_tier(regime: RegimeVector) -> str:
    if regime.news_risk_mode == "shock":
        return "event_shock"
    if regime.news_risk_mode == "risk_off":
        return "event_risk"
    tier = str(regime.derived_labels.get("news_risk_tier") or "").strip()
    if tier in {"no_event", "catalyst", "event_risk", "event_shock"}:
        return tier
    pressure = float(regime.news_catalyst_pressure or 0.0)
    if pressure >= 0.75:
        return "event_shock"
    if pressure >= 0.50:
        return "event_risk"
    if pressure >= 0.35:
        return "catalyst"
    return "no_event"


def _news_suppression_reason(spec: StrategySpec, tier: str) -> str | None:
    if tier in {"no_event", "catalyst"}:
        return None
    if tier == "event_risk" and _is_reversion_or_range(spec):
        return "news_event_risk_suppression"
    if tier == "event_shock" and (_is_reversion_or_range(spec) or _is_microstructure_orderflow(spec) or _is_funding_basis(spec)):
        return "news_event_shock_suppression"
    return None


def _is_reversion_or_range(spec: StrategySpec) -> bool:
    text = _spec_text(spec)
    return any(token in text for token in ("mean_reversion", "reversion", "revert", "range"))


def _is_microstructure_orderflow(spec: StrategySpec) -> bool:
    text = _spec_text(spec)
    return any(token in text for token in ("microstructure", "orderflow", "market_making", "ofi"))


def _is_funding_basis(spec: StrategySpec) -> bool:
    text = _spec_text(spec)
    return any(token in text for token in ("funding", "basis", "carry"))


def _spec_text(spec: StrategySpec) -> str:
    return " ".join([spec.strategy_id, spec.family, *spec.risk_tags]).lower()
