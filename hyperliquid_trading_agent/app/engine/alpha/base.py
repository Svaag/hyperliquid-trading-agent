from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from hyperliquid_trading_agent.app.engine.schemas import AlphaCandidate, FeatureSnapshot, RegimeVector, StrategySpec

CORE_CRYPTO_ASSETS = ["BTC", "ETH", "HYPE"]
HYPERLIQUID_VENUES = ["hyperliquid"]


class AlphaStrategy(Protocol):
    strategy_id: str
    spec: StrategySpec

    def generate(self, snapshot: FeatureSnapshot, regime: RegimeVector, *, timestamp_ms: int) -> list[AlphaCandidate]: ...


@dataclass(frozen=True)
class StrategyGenerationTrace:
    """Structured generation result used by the activation funnel.

    Strategies may provide a richer ``evaluate`` method over time.  The generic
    adapter still records a trustworthy denominator and distinguishes missing
    required data from a selected strategy whose trigger simply did not fire.
    """

    candidates: list[AlphaCandidate] = field(default_factory=list)
    outcome: Literal["generated", "no_trigger", "data_unavailable"] = "no_trigger"
    reason_codes: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)


def evaluate_strategy(
    strategy: AlphaStrategy,
    snapshot: FeatureSnapshot,
    regime: RegimeVector,
    *,
    timestamp_ms: int,
) -> StrategyGenerationTrace:
    """Evaluate a strategy while preserving the legacy ``generate`` contract."""

    richer = getattr(strategy, "evaluate", None)
    if callable(richer):
        result = richer(snapshot, regime, timestamp_ms=timestamp_ms)
        if isinstance(result, StrategyGenerationTrace):
            return result
    candidates = strategy.generate(snapshot, regime, timestamp_ms=timestamp_ms)
    required = list(strategy.spec.required_features or [])
    missing = [name for name in required if snapshot.features.get(name) is None]
    diagnostics = {
        "required_features": required,
        "missing_features": missing,
        "observed_required_features": {
            name: snapshot.features.get(name) for name in required if snapshot.features.get(name) is not None
        },
        "regime_label": regime.regime_label,
    }
    if candidates:
        return StrategyGenerationTrace(
            candidates=candidates,
            outcome="generated",
            reason_codes=["candidate_generated"],
            diagnostics=diagnostics,
        )
    if missing:
        return StrategyGenerationTrace(
            candidates=[],
            outcome="data_unavailable",
            reason_codes=["missing_required_features", *[f"missing_feature:{name}" for name in missing]],
            diagnostics=diagnostics,
        )
    return StrategyGenerationTrace(
        candidates=[],
        outcome="no_trigger",
        reason_codes=["trigger_conditions_not_met"],
        diagnostics=diagnostics,
    )


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

    activation_scope = str(spec.metadata.get("activation_scope") or "paper_shadow")
    paper_eligible = bool(spec.metadata.get("paper_eligible", True)) and activation_scope != "shadow_only"
    return {
        "strategy_version": spec.version,
        "strategy_family": spec.family,
        "valid_regimes": spec.valid_regimes,
        "required_features": spec.required_features,
        "feature_coverage_pct": feature_coverage_pct(snapshot, spec.required_features),
        "expected_edge_bps": expected_edge_bps,
        "risk_tags": spec.risk_tags,
        "counts_for_breadth": spec.counts_for_breadth,
        "source_integrity": {
            "spec_version": spec.version,
            "registry_contract": "strategy_spec_v1",
            "activation_scope": activation_scope,
            "paper_eligible": paper_eligible,
            "operator_promotion_required": bool(spec.metadata.get("operator_promotion_required", False)),
        },
    }
