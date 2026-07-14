from __future__ import annotations

import asyncio

import anyio

from hyperliquid_trading_agent.app.config import ServiceRole, Settings
from hyperliquid_trading_agent.app.workers.trader_worker import TraderWorker


class _DummyAutonomyService:
    def __init__(self) -> None:
        self.paused: bool | None = None
        self.running = False
        self.started = 0
        self.stopped = 0
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    async def start(self) -> None:
        self.running = True
        self.started += 1

    async def stop(self) -> None:
        self.running = False
        self.stopped += 1

    def status(self) -> dict[str, object]:
        return {"enabled": True, "running": self.running, "last_error": None}

    async def pause(self, actor: str = "api") -> None:
        self.paused = True
        self.calls.append(("pause", (), {"actor": actor}))

    async def resume(self, actor: str = "api") -> None:
        self.paused = False
        self.calls.append(("resume", (), {"actor": actor}))

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

        pause = await worker._handle_autonomy_pause({"command_type": "autonomy_pause", "payload": {"actor": "operator"}})
        resume = await worker._handle_autonomy_resume({"command_type": "autonomy_resume", "payload": {"actor": "operator"}})

        assert pause["paused"] is True
        assert resume["paused"] is False
        assert service.calls == [
            ("pause", (), {"actor": "operator"}),
            ("resume", (), {"actor": "operator"}),
        ]
        assert worker.heartbeat_metadata()["trader"]["command_count"] == 2

    anyio.run(run)


def test_trader_manual_paper_handlers_mutate_unified_paper_portfolio() -> None:
    async def run() -> None:
        worker = TraderWorker(Settings(environment="test", paper_trading_enabled=True, _env_file=None))

        draft = await worker._handle_paper_trade_draft(
            {
                "command_type": "paper_trade_draft",
                "requested_by": "discord_bot",
                "payload": {"symbol": "BTC", "side": "long", "entry": 100, "stop": 95, "actor": "u1", "source": "manual_discord"},
            }
        )
        order_id = draft["order"]["id"]
        assert draft["order"]["status"] == "new"

        confirm = await worker._handle_paper_trade_confirm(
            {
                "command_type": "paper_trade_confirm",
                "requested_by": "discord_bot",
                "payload": {"order_id": order_id, "mid": 101, "actor": "u1"},
            }
        )

        assert confirm["order"]["status"] == "filled"
        assert confirm["fill"]["order_id"] == order_id
        assert confirm["position"]["status"] == "open"
        assert confirm["paper_only"] is True
        assert confirm["exchange_actions"] == []

    anyio.run(run)


def test_trader_manual_paper_handlers_require_feature_flag() -> None:
    async def run() -> None:
        worker = TraderWorker(Settings(environment="test", paper_trading_enabled=False, _env_file=None))
        try:
            await worker._handle_paper_trade_draft(
                {
                    "command_type": "paper_trade_draft",
                    "payload": {"symbol": "BTC", "side": "long", "entry": 100, "stop": 95},
                }
            )
        except RuntimeError as exc:
            assert "PAPER_TRADING_ENABLED" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("paper draft should require PAPER_TRADING_ENABLED")

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
        await worker._shutdown_engine_runtime()

    anyio.run(run)


def test_trader_engine_loop_starts_with_shared_engine_service() -> None:
    async def run() -> None:
        settings = Settings(environment="test", engine_enabled=True, engine_newsfeed_enabled=True, newswire_enabled=False, engine_loop_interval_seconds=60, _env_file=None)
        worker = TraderWorker(settings)

        await worker._start_engine_loop()
        await worker._start_engine_newsfeed()
        metadata = worker.heartbeat_metadata()

        assert worker._engine_service is not None
        assert worker._engine_loop_task is not None
        assert worker._engine_loop_task.get_name() == "trader-engine-shadow-loop"
        assert metadata["engine_loop"]["enabled"] is True
        assert metadata["engine_loop"]["running"] is True
        assert metadata["engine_loop"]["interval_seconds"] == 60
        assert metadata["engine_newsfeed"]["consumer"]["running"] is True
        assert worker._engine_news_consumer is not None
        assert worker._engine_news_consumer.engine_service is worker._engine_service

        worker._engine_loop_task.cancel()
        try:
            await worker._engine_loop_task
        except asyncio.CancelledError:
            pass
        await worker._engine_news_consumer.stop()
        await worker._shutdown_engine_runtime()

    anyio.run(run)


def test_trader_owns_optional_observation_loop_and_reports_runtime() -> None:
    async def run() -> None:
        worker = TraderWorker(
            Settings(
                environment="test",
                autonomy_enabled=True,
                _env_file=None,
            )
        )
        service = _DummyAutonomyService()
        worker._autonomy_service = service

        await worker._start_autonomy_loop()
        metadata = worker.heartbeat_metadata()["autonomy_loop"]

        assert service.started == 1
        assert metadata["running"] is True
        assert metadata["owner_role"] == "trader"
        assert metadata["runtime_source"] == "trader_heartbeat"

        await worker._shutdown_engine_runtime()
        assert service.stopped == 1

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
