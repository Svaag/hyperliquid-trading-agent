from __future__ import annotations

from typing import Any

from hyperliquid_trading_agent.app.config import ServiceRole, Settings
from hyperliquid_trading_agent.app.newswire.service import NewswireService
from hyperliquid_trading_agent.app.workers.base import BaseWorker


class NewswireWorker(BaseWorker):
    role = ServiceRole.NEWSWIRE
    lock_name = "service:newswire"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.service: NewswireService | None = None

    async def run(self) -> None:
        self.service = NewswireService(settings=self.settings, repository=self.repository)
        await self.service.start()
        try:
            await self.wait_until_stopped()
        finally:
            await self.service.stop()

    def heartbeat_metadata(self) -> dict[str, Any]:
        if self.service is None:
            return {"newswire": {"running": False}}
        return {"newswire": self.service.status()}
