from __future__ import annotations

import anyio

from hyperliquid_trading_agent.app.config import ServiceRole, Settings
from hyperliquid_trading_agent.app.workers.trader_worker import TraderWorker


class _Dumpable:
    def __init__(self, **data: object) -> None:
        self.data = data

    def model_dump(self, mode: str = "json") -> dict[str, object]:
        return self.data


class _DummyAutonomyService:
    def __init__(self) -> None:
        self.paused: bool | None = None
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    async def pause(self, actor: str = "api") -> None:
        self.paused = True
        self.calls.append(("pause", (), {"actor": actor}))

    async def resume(self, actor: str = "api") -> None:
        self.paused = False
        self.calls.append(("resume", (), {"actor": actor}))

    async def approve_signal(self, signal_id: str, actor: str, mid: float | None = None) -> dict[str, object]:
        self.calls.append(("approve_signal", (signal_id,), {"actor": actor, "mid": mid}))
        return {"signal": {"id": signal_id, "status": "paper_ordered"}}

    async def reject_signal(self, signal_id: str, actor: str, reason: str = "") -> _Dumpable:
        self.calls.append(("reject_signal", (signal_id,), {"actor": actor, "reason": reason}))
        return _Dumpable(id=signal_id, status="rejected")

    async def expire_signal(self, signal_id: str, actor: str = "api") -> _Dumpable:
        self.calls.append(("expire_signal", (signal_id,), {"actor": actor}))
        return _Dumpable(id=signal_id, status="expired")


class _DummyHip4Service:
    async def run_proactive_cycle(self, manual: bool = False) -> dict[str, object]:
        return {"cycle_id": "c1", "manual": manual}

    async def run_scan(self, send_digest: bool = True) -> list[dict[str, object]]:
        return [{"candidate_id": "h1", "send_digest": send_digest}]

    async def execute_paper_candidate(self, candidate_id: str) -> dict[str, object]:
        return {"candidate_id": candidate_id, "status": "paper_executed"}

    async def reconcile_paper(self) -> dict[str, object]:
        return {"status": "ok"}


class _DummyRepository:
    def __init__(self) -> None:
        self.trackers = {"t1": {"id": "t1", "status": "active"}}

    async def get_position_tracker(self, tracker_id: str) -> dict[str, object] | None:
        return self.trackers.get(tracker_id)

    async def set_position_tracker_status(self, tracker_id: str, status: str, reason: str = "") -> None:
        self.trackers[tracker_id] = {"id": tracker_id, "status": status, "reason": reason}


def test_trader_autonomy_handlers_delegate_to_real_service_methods() -> None:
    async def run() -> None:
        worker = TraderWorker(Settings(environment="test"))
        service = _DummyAutonomyService()
        worker._autonomy_service = service

        approve = await worker._handle_autonomy_signal_approve({"command_type": "autonomy_signal_approve", "requested_by": "api", "payload": {"signal_id": "s1", "mid": 101}})
        reject = await worker._handle_autonomy_signal_reject({"command_type": "autonomy_signal_reject", "payload": {"signal_id": "s1", "reason": "nope"}})
        pause = await worker._handle_autonomy_pause({"command_type": "autonomy_pause", "payload": {"actor": "operator"}})
        resume = await worker._handle_autonomy_resume({"command_type": "autonomy_resume", "payload": {"actor": "operator"}})

        assert approve["result"] == {"signal": {"id": "s1", "status": "paper_ordered"}}
        assert reject["signal"]["status"] == "rejected"
        assert pause["paused"] is True
        assert resume["paused"] is False
        assert service.calls[0] == ("approve_signal", ("s1",), {"actor": "api", "mid": 101.0})
        assert worker.heartbeat_metadata()["trader"]["command_count"] == 4

    anyio.run(run)


def test_trader_engine_newsfeed_pump_starts_with_persisted_consumer() -> None:
    async def run() -> None:
        settings = Settings(environment="test", engine_enabled=True, engine_newsfeed_enabled=True, newswire_enabled=False, _env_file=None)
        worker = TraderWorker(settings)
        await worker._start_engine_newsfeed()
        metadata = worker.heartbeat_metadata()["engine_newsfeed"]

        assert worker._engine_service is not None
        assert worker._engine_news_bus is not None
        assert worker._engine_news_consumer is not None
        assert worker._engine_news_pump is not None
        assert worker._engine_news_pump.consumer_name == "trader:engine_newswire"
        assert metadata["enabled"] is True
        assert metadata["consumer_name"] == "trader:engine_newswire"
        assert metadata["consumer"]["effective_enabled"] is True
        assert metadata["consumer"]["running"] is True
        assert metadata["pump"]["bootstrap_from_latest"] is True
        await worker._engine_news_consumer.stop()

    anyio.run(run)


def test_trader_role_keeps_newswire_ingestion_disabled_while_engine_newsfeed_enabled() -> None:
    settings = Settings(service_role=ServiceRole.TRADER, environment="prod", engine_enabled=True, engine_newsfeed_enabled=True, newswire_enabled=False, _env_file=None)

    assert settings.service_role == ServiceRole.TRADER
    assert settings.newswire_enabled is False
    assert settings.engine_newsfeed_enabled is True


def test_trader_hip4_and_tracking_handlers_delegate_to_real_service_methods() -> None:
    async def run() -> None:
        worker = TraderWorker(Settings(environment="test"))
        worker._hip4_service = _DummyHip4Service()
        worker.repository = _DummyRepository()  # type: ignore[assignment]

        cycle = await worker._handle_hip4_loop_run_once({"command_type": "hip4_loop_run_once", "payload": {"manual": True}})
        scan = await worker._handle_hip4_scan_run({"command_type": "hip4_scan_run", "payload": {"send_digest": False}})
        paper = await worker._handle_hip4_paper_execute({"command_type": "hip4_paper_execute", "payload": {"candidate_id": "h1"}})
        tracker = await worker._handle_tracking_pause({"command_type": "tracking_pause", "payload": {"tracker_id": "t1"}})

        assert cycle["result"] == {"cycle_id": "c1", "manual": True}
        assert scan["count"] == 1
        assert paper["result"]["status"] == "paper_executed"
        assert tracker["status"] == "paused"
        assert tracker["tracker"] == {"id": "t1", "status": "paused", "reason": "trader_worker"}

    anyio.run(run)
