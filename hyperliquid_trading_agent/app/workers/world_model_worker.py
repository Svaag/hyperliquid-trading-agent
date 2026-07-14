from __future__ import annotations

import asyncio
from typing import Any

from hyperliquid_trading_agent.app.config import ServiceRole, Settings
from hyperliquid_trading_agent.app.workers.base import BaseWorker
from hyperliquid_trading_agent.app.workers.stored_newswire_story_pump import StoredNewswireStoryPump
from hyperliquid_trading_agent.app.world_model.adapters import WorldModelAdapterService
from hyperliquid_trading_agent.app.world_model.factory import build_world_model_service
from hyperliquid_trading_agent.app.world_model.reducer import now_ms
from hyperliquid_trading_agent.app.world_model.schemas import WorldEvent
from hyperliquid_trading_agent.app.world_model.streams import WorldModelStreamService
from hyperliquid_trading_agent.app.world_model.v2_schemas import EvidenceV2
from hyperliquid_trading_agent.app.world_model.v2_sources import OfficialMacroBaseline


class WorldModelWorker(BaseWorker):
    role = ServiceRole.WORLD_MODEL
    lock_name = "service:world_model"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.service: Any | None = None
        self.adapter_service: WorldModelAdapterService | None = None
        self.stream_service: WorldModelStreamService | None = None
        self.pump: StoredNewswireStoryPump | None = None
        self.macro_baseline: OfficialMacroBaseline | None = None

    async def run(self) -> None:
        self.service = build_world_model_service(settings=self.settings, repository=self.repository)
        hydrate = getattr(self.service, "hydrate", None)
        if callable(hydrate):
            await hydrate()
        self.adapter_service = WorldModelAdapterService(settings=self.settings, world_model_service=self.service)
        self.stream_service = WorldModelStreamService(settings=self.settings, world_model_service=self.service)
        self.pump = StoredNewswireStoryPump(
            consumer_name="world_model_v2:newswire" if self.settings.world_model_v2_enabled else "world_model:newswire",
            repository=self.repository,
            callbacks=[self.service.observe_newswire_event],
            poll_seconds=self.settings.consumer_poll_seconds,
            batch_size=self.settings.consumer_batch_size,
            bootstrap_from_latest=self.settings.world_model_v2_enabled,
            bootstrap_metadata={"world_model_version": 2, "clean_cutover": True},
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
        if self.settings.world_model_adapters_enabled:
            tasks.append(asyncio.create_task(self._adapter_poll_loop(), name="world-model-adapter-poll"))
        if self.settings.world_model_v2_enabled:
            self.macro_baseline = OfficialMacroBaseline(settings=self.settings, service=self.service)
            tasks.append(asyncio.create_task(self._macro_backfill_once(), name="world-model-v2-macro-baseline"))
            tasks.append(asyncio.create_task(self._v2_maintenance_loop(), name="world-model-v2-maintenance"))
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
        await self._persist_v2_snapshot()
        return {"poll": result}

    async def _adapter_poll_loop(self) -> None:
        while True:
            if self.adapter_service is not None:
                await self.adapter_service.poll()
                await self._persist_v2_snapshot()
            await asyncio.sleep(max(10.0, float(self.settings.world_model_adapter_poll_interval_seconds)))

    async def _macro_backfill_once(self) -> None:
        if self.macro_baseline is None:
            return
        await self.macro_baseline.backfill()
        await self._persist_v2_snapshot()

    async def _persist_v2_snapshot(self) -> None:
        if not self.settings.world_model_v2_enabled or self.service is None:
            return
        persist = getattr(self.service, "persist_snapshot", None)
        if callable(persist):
            await persist(force=True)

    async def _v2_maintenance_loop(self) -> None:
        while True:
            compact = getattr(self.repository, "compact_world_model_v2", None)
            if callable(compact):
                await compact(now_ms=now_ms())
            await asyncio.sleep(3_600)

    async def _handle_dev_seed(self, command: dict[str, Any]) -> dict[str, Any]:
        if self.service is None:
            raise RuntimeError("world_model_service_unavailable")
        payload = command.get("payload") or {}
        symbol = str(payload.get("symbol") or "BTC").upper()
        topic = str(payload.get("topic") or "macro").lower()
        ts = now_ms()
        if self.settings.world_model_v2_enabled:
            evidence = EvidenceV2(
                evidence_id=f"wm2_seed_{symbol}_{topic}_{ts}", source_type="operator", source="worker_command",
                provider="local", title=f"Seeded {symbol} {topic} v2 evidence", available_at_ms=ts,
                observed_at_ms=ts, admission_status="admitted", factor_ids=[topic] if topic in {"inflation", "labor", "growth", "policy_stance", "rates", "real_rates", "usd", "liquidity", "financial_conditions"} else [],
                instrument_ids=[symbol], admission_reason_codes=["operator_seed"],
                metadata={"command_id": command.get("command_id"), "shadow_only": True, "execution_authority": "none"},
            )
            await self.service.observe_evidence(evidence)
            return {"evidence_id": evidence.evidence_id, "version": 2}
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
            "macro_baseline": self.macro_baseline.status() if self.macro_baseline is not None else {},
        }
