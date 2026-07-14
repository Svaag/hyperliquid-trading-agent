from __future__ import annotations

import anyio

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.workers.scheduler_worker import SchedulerWorker


class _DummyEventEvaluationService:
    def __init__(self) -> None:
        self.loaded = False
        self.marked_now_ms: int | None = None
        self.expired_now_ms: int | None = None

    async def load_open(self) -> None:
        self.loaded = True

    async def mark_due(self, now_ms: int | None = None) -> list[object]:
        self.marked_now_ms = now_ms
        return [object()]

    async def expire_overdue_events(self, now_ms: int | None = None) -> None:
        self.expired_now_ms = now_ms


class _DummyWaveSupervisor:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def run_once(self, options: object | None = None) -> dict[str, object]:
        return {"ok": True, "options": options.__class__.__name__ if options else None}

    def status(self) -> dict[str, object]:
        return {"enabled": True, "running": self.started and not self.stopped, "owner_role": "scheduler"}


def test_scheduler_event_evaluation_and_wave_handlers_run_real_service_methods() -> None:
    async def run() -> None:
        worker = SchedulerWorker(Settings(environment="test"))
        event_service = _DummyEventEvaluationService()
        worker._event_evaluation_service = event_service
        event_result = await worker._handle_autonomy_event_evaluations_backfill({"command_type": "autonomy_event_evaluations_backfill", "payload": {"now_ms": 456}})
        assert event_result["marked_count"] == 1
        assert event_service.loaded is True
        assert event_service.marked_now_ms == 456
        assert event_service.expired_now_ms == 456

        worker._wave_supervisor = _DummyWaveSupervisor()
        wave_result = await worker._handle_orchestration_wave_run_once({"command_type": "orchestration_wave_run_once", "payload": {"perform_maintenance": True}})
        assert wave_result["result"]["ok"] is True
        assert worker.heartbeat_metadata()["scheduler"]["command_count"] == 2

    anyio.run(run)


def test_scheduler_owns_wave_supervisor_lifecycle() -> None:
    async def run() -> None:
        worker = SchedulerWorker(
            Settings(
                environment="test",
                orchestration_wave_supervisor_enabled=True,
                _env_file=None,
            )
        )
        supervisor = _DummyWaveSupervisor()
        worker._wave_supervisor = supervisor

        async def command_loop(handlers):
            assert "orchestration_wave_run_once" in handlers

        worker.command_loop = command_loop  # type: ignore[method-assign]
        await worker.run()

        assert supervisor.started is True
        assert supervisor.stopped is True

    anyio.run(run)
