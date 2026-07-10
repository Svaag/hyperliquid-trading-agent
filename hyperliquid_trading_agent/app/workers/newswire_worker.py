from __future__ import annotations

from typing import Any

from hyperliquid_trading_agent.app.agent.model_gateway import ModelGateway
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
        model_gateway = None
        if self.settings.newswire_model_classify_enabled:
            candidate_gateway = ModelGateway(settings=self.settings)
            if any(attempt.missing_reason is None for attempt in candidate_gateway.configured_attempts()):
                model_gateway = candidate_gateway
        self.service = NewswireService(
            settings=self.settings,
            repository=self.repository,
            model_gateway=model_gateway,
        )
        await self.service.start()
        try:
            await self.wait_until_stopped()
        finally:
            await self.service.stop()

    def heartbeat_metadata(self) -> dict[str, Any]:
        if self.service is None:
            return {"newswire": {"running": False}}
        return {"newswire": self.service.status()}
