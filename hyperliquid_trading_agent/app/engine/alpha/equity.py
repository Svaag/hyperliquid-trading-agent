from __future__ import annotations

from hyperliquid_trading_agent.app.engine.schemas import AlphaCandidate, FeatureSnapshot, RegimeVector


class EquityOptionsFlowStrategy:
    """Placeholder strategy adapter for the future equity/options-flow candidate path."""

    strategy_id = "equity_options_flow_v1"

    def generate(self, snapshot: FeatureSnapshot, regime: RegimeVector, *, timestamp_ms: int) -> list[AlphaCandidate]:
        # TradFi feature normalization lands in a later milestone; keep the family registered
        # but inert rather than emitting low-quality pseudo-candidates.
        return []
