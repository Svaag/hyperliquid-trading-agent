from __future__ import annotations

from typing import Protocol

from hyperliquid_trading_agent.app.engine.schemas import AlphaCandidate, FeatureSnapshot, RegimeVector, StrategySpec

CORE_CRYPTO_ASSETS = ["BTC", "ETH", "HYPE"]
HYPERLIQUID_VENUES = ["hyperliquid"]


class AlphaStrategy(Protocol):
    strategy_id: str
    spec: StrategySpec

    def generate(self, snapshot: FeatureSnapshot, regime: RegimeVector, *, timestamp_ms: int) -> list[AlphaCandidate]: ...


def feature_coverage_pct(snapshot: FeatureSnapshot, required_features: list[str]) -> float:
    if not required_features:
        return 100.0
    present = 0
    for feature_name in required_features:
        value = snapshot.features.get(feature_name)
        if value is not None:
            present += 1
    return round(present / len(required_features) * 100.0, 2)


def candidate_contract_fields(spec: StrategySpec, snapshot: FeatureSnapshot, *, expected_edge_bps: float = 0.0) -> dict:
    """Return common AlphaCandidate metadata derived from a strategy spec."""

    return {
        "strategy_version": spec.version,
        "strategy_family": spec.family,
        "valid_regimes": spec.valid_regimes,
        "required_features": spec.required_features,
        "feature_coverage_pct": feature_coverage_pct(snapshot, spec.required_features),
        "expected_edge_bps": expected_edge_bps,
        "risk_tags": spec.risk_tags,
        "counts_for_breadth": spec.counts_for_breadth,
        "source_integrity": {"spec_version": spec.version, "registry_contract": "strategy_spec_v1"},
    }
