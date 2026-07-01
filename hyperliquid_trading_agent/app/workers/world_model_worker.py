from __future__ import annotations

import asyncio
from typing import Any

from hyperliquid_trading_agent.app.config import ServiceRole, Settings
from hyperliquid_trading_agent.app.workers.base import BaseWorker
from hyperliquid_trading_agent.app.workers.stored_newswire_pump import StoredNewswirePump
from hyperliquid_trading_agent.app.world_model.adapters import WorldModelAdapterService
from hyperliquid_trading_agent.app.world_model.reducer import now_ms
from hyperliquid_trading_agent.app.world_model.schemas import WorldEvent
from hyperliquid_trading_agent.app.world_model.service import WorldModelService
from hyperliquid_trading_agent.app.world_model.streams import WorldModelStreamService


class WorldModelWorker(BaseWorker):
    role = ServiceRole.WORLD_MODEL
    lock_name = "service:world_model"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.service: WorldModelService | None = None
        self.adapter_service: WorldModelAdapterService | None = None
        self.stream_service: WorldModelStreamService | None = None
        self.pump: StoredNewswirePump | None = None

    async def run(self) -> None:
        self.service = WorldModelService(settings=self.settings, repository=self.repository)
        self.adapter_service = WorldModelAdapterService(settings=self.settings, world_model_service=self.service)
        self.stream_service = WorldModelStreamService(settings=self.settings, world_model_service=self.service)
        self.pump = StoredNewswirePump(
            consumer_name="world_model:newswire",
            repository=self.repository,
            callbacks=[self.service.observe_newswire_event],
            poll_seconds=self.settings.consumer_poll_seconds,
            batch_size=self.settings.consumer_batch_size,
        )
        await self.stream_service.start()
        tasks = [
            asyncio.create_task(self.pump.run_forever(), name="world-model-newswire-pump"),
            asyncio.create_task(
                self.command_loop(
                    {
                        "world_model_adapter_poll": self._handle_adapter_poll,
                        "world_model_dev_seed": self._handle_dev_seed,
                    }
                ),
                name="world-model-command-loop",
            ),
        ]
        try:
            await self.wait_until_stopped()
        finally:
            if self.pump is not None:
                await self.pump.stop()
            if self.stream_service is not None:
                await self.stream_service.stop()
            for task in tasks:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def _handle_adapter_poll(self, command: dict[str, Any]) -> dict[str, Any]:
        if self.adapter_service is None:
            raise RuntimeError("world_model_adapter_service_unavailable")
        payload = command.get("payload") or {}
        adapter_name = payload.get("adapter_name")
        result = await self.adapter_service.poll(adapter_name, force=bool(payload.get("force")))
        return {"poll": result}

    async def _handle_dev_seed(self, command: dict[str, Any]) -> dict[str, Any]:
        if self.service is None:
            raise RuntimeError("world_model_service_unavailable")
        payload = command.get("payload") or {}
        symbol = str(payload.get("symbol") or "BTC").upper()
        topic = str(payload.get("topic") or "macro").lower()
        ts = now_ms()
        event = WorldEvent(
            event_id=f"wevt_seed_{symbol}_{topic}_{ts}",
            source_type="operator_seed",
            source="api_command",
            provider="local",
            event_type="operator_seed",
            asset_class="crypto",
            symbols=[symbol],
            topics=[topic],
            title=f"Seeded {symbol} {topic} world-model event",
            body="Local operator seed command for dashboard verification.",
            event_ts_ms=ts,
            received_ts_ms=ts,
            computed_ts_ms=ts,
            importance_score=50,
            sentiment="neutral",
            confidence=0.5,
            source_score=0.5,
            quality_score=0.5,
            payload={"command_id": command.get("command_id")},
            metadata={"source": "worker_command"},
        )
        beliefs = await self.service.observe_event(event)
        return {"event_id": event.event_id, "belief_count": len(beliefs)}

    def heartbeat_metadata(self) -> dict[str, Any]:
        return {
            "world_model": self.service.status() if self.service is not None else {},
            "streams": self.stream_service.status() if self.stream_service is not None else {},
            "pump": self.pump.status() if self.pump is not None else {},
        }
