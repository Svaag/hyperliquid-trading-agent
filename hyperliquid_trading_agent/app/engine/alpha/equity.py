from __future__ import annotations

from hyperliquid_trading_agent.app.engine.alpha.base import candidate_contract_fields
from hyperliquid_trading_agent.app.engine.schemas import AlphaCandidate, FeatureSnapshot, RegimeVector, StrategySpec


class EquityOptionsFlowStrategy:
    """Placeholder strategy adapter for the future equity/options-flow candidate path."""

    spec = StrategySpec(
        strategy_id="equity_options_flow_v1",
        version="1.0.0",
        family="tradfi_options_flow",
        supported_assets=[],
        supported_venues=[],
        supported_horizons=["1d"],
        required_features=["options_flow_imbalance"],
        valid_regimes=["tradfi_session"],
        max_candidates_per_run=0,
        max_allocation_share_pct=0.0,
        cooldown_ms=3_600_000,
        min_confidence=1.0,
        min_ev_bps=999.0,
        risk_tags=["placeholder", "tradfi"],
        counts_for_breadth=False,
        enabled=False,
    )
    strategy_id = spec.strategy_id

    def generate(self, snapshot: FeatureSnapshot, regime: RegimeVector, *, timestamp_ms: int) -> list[AlphaCandidate]:
        _ = candidate_contract_fields(self.spec, snapshot)
        # TradFi feature normalization lands in a later milestone; keep the family registered
        # but inert rather than emitting low-quality pseudo-candidates.
        return []
