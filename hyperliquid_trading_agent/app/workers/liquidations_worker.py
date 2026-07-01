from __future__ import annotations

from typing import Any

from hyperliquid_trading_agent.app.config import ServiceRole, Settings
from hyperliquid_trading_agent.app.liquidations.service import LiquidationService
from hyperliquid_trading_agent.app.workers.base import BaseWorker


class LiquidationsWorker(BaseWorker):
    role = ServiceRole.LIQUIDATIONS
    lock_name = "service:liquidations"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.service: LiquidationService | None = None

    async def run(self) -> None:
        if self.sessionmaker is None:
            raise RuntimeError("sessionmaker_unavailable")
        self.service = LiquidationService(self.settings, self.sessionmaker)
        await self.service.start()
        try:
            await self.wait_until_stopped()
        finally:
            await self.service.stop()

    def heartbeat_metadata(self) -> dict[str, Any]:
        return {"liquidations": self.service.status() if self.service is not None and hasattr(self.service, "status") else {"enabled": self.settings.liquidations_enabled}}
