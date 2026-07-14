from __future__ import annotations

from typing import Any

from hyperliquid_trading_agent.app.autonomy.event_evaluation import AlphaEventEvaluationService
from hyperliquid_trading_agent.app.autonomy.memory import MemoryService
from hyperliquid_trading_agent.app.autonomy.reports import AutonomyReportService
from hyperliquid_trading_agent.app.config import ServiceRole, Settings
from hyperliquid_trading_agent.app.orchestration.agent_core_trace import AgentCoreTraceEmitter
from hyperliquid_trading_agent.app.orchestration.wave_supervisor import WaveSupervisor, WaveSupervisorRunOptions
from hyperliquid_trading_agent.app.workers.base import BaseWorker


class SchedulerWorker(BaseWorker):
    role = ServiceRole.SCHEDULER
    lock_name = "service:scheduler"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.command_count = 0
        self.last_command_type: str | None = None
        self._memory_service: MemoryService | None = None
        self._event_evaluation_service: AlphaEventEvaluationService | None = None
        self._report_service: AutonomyReportService | None = None
        self._wave_supervisor: WaveSupervisor | None = None

    async def run(self) -> None:
        supervisor = self._get_wave_supervisor()
        await supervisor.start()
        try:
            await self.command_loop(
                {
                    "orchestration_wave_run_once": self._handle_orchestration_wave_run_once,
                    "autonomy_event_evaluations_backfill": self._handle_autonomy_event_evaluations_backfill,
                    "autonomy_daily_report_run": self._handle_autonomy_daily_report_run,
                    "autonomy_weekly_report_run": self._handle_autonomy_weekly_report_run,
                }
            )
        finally:
            await supervisor.stop()

    async def _handle_orchestration_wave_run_once(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        payload = self._payload(command)
        result = await self._get_wave_supervisor().run_once(
            WaveSupervisorRunOptions(
                perform_maintenance=bool(payload.get("perform_maintenance")),
                escalate=bool(payload.get("escalate")),
            )
        )
        return {"accepted_by": self.instance_id, "command_type": command.get("command_type"), "result": result}

    async def _handle_autonomy_event_evaluations_backfill(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        payload = self._payload(command)
        now_ms = self._payload_now_ms(payload)
        service = self._get_event_evaluation_service()
        await service.load_open()
        marked = await service.mark_due(now_ms)
        await service.expire_overdue_events(now_ms)
        return {"accepted_by": self.instance_id, "command_type": command.get("command_type"), "marked_count": len(marked), "backfilled": True, "now_ms": now_ms}

    async def _handle_autonomy_daily_report_run(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        payload = self._payload(command)
        report = await self._get_report_service().generate_daily(now_ms=self._payload_now_ms(payload), post=bool(payload.get("post", False)))
        return {"accepted_by": self.instance_id, "command_type": command.get("command_type"), "report": report.model_dump(mode="json")}

    async def _handle_autonomy_weekly_report_run(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        payload = self._payload(command)
        report = await self._get_report_service().generate_weekly(now_ms=self._payload_now_ms(payload), post=bool(payload.get("post", False)))
        return {"accepted_by": self.instance_id, "command_type": command.get("command_type"), "report": report.model_dump(mode="json")}

    def _record_command(self, command: dict[str, Any]) -> None:
        self.command_count += 1
        self.last_command_type = str(command.get("command_type") or "")

    def _payload(self, command: dict[str, Any]) -> dict[str, Any]:
        payload = command.get("payload")
        return payload if isinstance(payload, dict) else {}

    def _payload_now_ms(self, payload: dict[str, Any]) -> int | None:
        value = payload.get("now_ms")
        return int(value) if value is not None else None

    def _get_memory_service(self) -> MemoryService:
        if self._memory_service is None:
            self._memory_service = MemoryService(settings=self.settings, repository=self.repository)
        return self._memory_service

    def _get_event_evaluation_service(self) -> AlphaEventEvaluationService:
        if self._event_evaluation_service is None:
            self._event_evaluation_service = AlphaEventEvaluationService(settings=self.settings, repository=self.repository, memory_service=self._get_memory_service())
        return self._event_evaluation_service

    def _get_report_service(self) -> AutonomyReportService:
        if self._report_service is None:
            self._report_service = AutonomyReportService(
                settings=self.settings,
                repository=self.repository,
                event_evaluation_service=self._get_event_evaluation_service(),
                memory_service=self._get_memory_service(),
                tuning_service=None,
                portfolio_service=None,
                alert_sink=None,
            )
        return self._report_service

    def _get_wave_supervisor(self) -> WaveSupervisor:
        if self._wave_supervisor is None:
            self._wave_supervisor = WaveSupervisor(
                settings=self.settings,
                repository=self.repository,
                engine_service=None,
                trace_emitter=AgentCoreTraceEmitter(settings=self.settings),
            )
        return self._wave_supervisor

    def heartbeat_metadata(self) -> dict[str, Any]:
        supervisor_status = getattr(self._wave_supervisor, "status", None)
        return {
            "scheduler": {"command_count": self.command_count, "last_command_type": self.last_command_type},
            "wave_supervisor": (
                supervisor_status()
                if callable(supervisor_status)
                else {"enabled": self.settings.orchestration_wave_supervisor_enabled, "running": False}
            ),
        }
