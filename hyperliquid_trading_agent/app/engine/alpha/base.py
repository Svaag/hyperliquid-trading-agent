from __future__ import annotations

from typing import Protocol

from hyperliquid_trading_agent.app.engine.schemas import AlphaCandidate, FeatureSnapshot, RegimeVector


class AlphaStrategy(Protocol):
    strategy_id: str

    def generate(self, snapshot: FeatureSnapshot, regime: RegimeVector, *, timestamp_ms: int) -> list[AlphaCandidate]: ...
