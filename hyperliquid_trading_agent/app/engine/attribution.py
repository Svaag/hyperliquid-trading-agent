from __future__ import annotations

import hashlib
from typing import Any

from hyperliquid_trading_agent.app.engine.event_ledger import now_ms
from hyperliquid_trading_agent.app.engine.schemas import PnLAttributionRecord


class AttributionService:
    def __init__(self, repository: Any | None = None):
        self.repository = repository

    async def record_basic(
        self,
        *,
        strategy_id: str,
        asset: str,
        window_start_ms: int,
        window_end_ms: int,
        total_pnl_usd: float,
        fees_usd: float = 0.0,
        funding_usd: float = 0.0,
        position_id: str | None = None,
        candidate_id: str | None = None,
    ) -> PnLAttributionRecord:
        digest = hashlib.sha1(f"{strategy_id}:{asset}:{window_start_ms}:{window_end_ms}:{position_id}:{candidate_id}".encode()).hexdigest()[:24]
        residual = total_pnl_usd - fees_usd - funding_usd
        item = PnLAttributionRecord(
            attribution_id="attr_" + digest,
            position_id=position_id,
            candidate_id=candidate_id,
            strategy_id=strategy_id,
            asset=asset,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
            alpha_pnl_usd=residual,
            fees_usd=fees_usd,
            funding_usd=funding_usd,
            residual_pnl_usd=0.0,
            total_pnl_usd=total_pnl_usd,
            metrics={"created_at_ms": now_ms()},
        )
        if self.repository is not None and getattr(self.repository, "enabled", False):
            record = getattr(self.repository, "record_pnl_attribution", None)
            if callable(record):
                await record(item.model_dump(mode="json"))
        return item
