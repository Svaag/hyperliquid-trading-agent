from __future__ import annotations

from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.hyperliquid.client import HyperliquidClient


class Hip4InfoClient:
    """Read-only HIP-4 info facade.

    This wrapper intentionally exposes only `/info` reads. It does not import or
    instantiate any signing/exchange client.
    """

    def __init__(self, *, settings: Settings, hyperliquid: HyperliquidClient):
        self.settings = settings
        self.hyperliquid = hyperliquid

    async def outcome_meta(self) -> dict[str, Any]:
        data = await self.hyperliquid.outcome_meta()
        return data if isinstance(data, dict) else {}

    async def settled_outcome(self, outcome_id: int) -> Any:
        return await self.hyperliquid.settled_outcome(outcome_id)

    async def l2_book(self, coin: str) -> Any:
        return await self.hyperliquid.l2_book(coin)
