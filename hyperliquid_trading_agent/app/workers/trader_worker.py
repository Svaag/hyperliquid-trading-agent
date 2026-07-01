from __future__ import annotations

import time
from typing import Any

from hyperliquid_trading_agent.app.autonomy.memory import MemoryService
from hyperliquid_trading_agent.app.autonomy.schemas import SignalEvidence, TradeSignal
from hyperliquid_trading_agent.app.autonomy.service import AutonomousTradingLoopService
from hyperliquid_trading_agent.app.config import ServiceRole, Settings
from hyperliquid_trading_agent.app.engine.bandit import OfflineContextualBanditReporter
from hyperliquid_trading_agent.app.engine.replay_compare import EngineReplayComparisonService
from hyperliquid_trading_agent.app.engine.strategy_performance import refresh_strategy_regime_performance
from hyperliquid_trading_agent.app.hip4.service import Hip4Service
from hyperliquid_trading_agent.app.workers.base import BaseWorker


class TraderWorker(BaseWorker):
    role = ServiceRole.TRADER
    lock_name = "service:trader"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.command_count = 0
        self.last_command_type: str | None = None
        self._hip4_service: Hip4Service | None = None
        self._autonomy_service: AutonomousTradingLoopService | None = None
        self._memory_service: MemoryService | None = None

    async def run(self) -> None:
        await self.command_loop(
            {
                "engine_strategy_regime_refresh": self._handle_engine_strategy_regime_refresh,
                "engine_bandit_run": self._handle_engine_bandit_run,
                "engine_replay_comparison_run": self._handle_engine_replay_comparison_run,
                "hip4_loop_run_once": self._handle_hip4_loop_run_once,
                "hip4_scan_run": self._handle_hip4_scan_run,
                "hip4_paper_execute": self._handle_hip4_paper_execute,
                "hip4_reconcile_run": self._handle_hip4_reconcile_run,
                "hip4_manual_ticket": self._handle_hip4_manual_ticket,
                "autonomy_pause": self._handle_autonomy_pause,
                "autonomy_resume": self._handle_autonomy_resume,
                "autonomy_signal_approve": self._handle_autonomy_signal_approve,
                "autonomy_signal_reject": self._handle_autonomy_signal_reject,
                "autonomy_signal_expire": self._handle_autonomy_signal_expire,
                "autonomy_equity_signal_approve": self._handle_autonomy_equity_signal_approve,
                "autonomy_equity_signal_reject": self._handle_autonomy_equity_signal_reject,
                "tracking_pause": self._handle_tracking_pause,
                "tracking_resume": self._handle_tracking_resume,
                "tracking_stop": self._handle_tracking_stop,
                "admin_debug_seed_flip_demo": self._handle_admin_debug_seed_flip_demo,
            }
        )

    async def _handle_engine_strategy_regime_refresh(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        payload = self._payload(command)
        start_ms, end_ms = self._window(payload, default_hours=24)
        rows = await refresh_strategy_regime_performance(
            self.repository,
            window_start_ms=start_ms,
            window_end_ms=end_ms,
            limit=int(payload.get("limit") or 5000),
        )
        return self._result(command, window_start_ms=start_ms, window_end_ms=end_ms, refreshed_count=len(rows), items=rows[:50])

    async def _handle_engine_bandit_run(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        payload = self._payload(command)
        start_ms, end_ms = self._window(payload, default_hours=24 * 7)
        result = await OfflineContextualBanditReporter(self.repository).run(
            window_start_ms=start_ms,
            window_end_ms=end_ms,
            limit=int(payload.get("limit") or 1000),
        )
        return self._result(command, window_start_ms=start_ms, window_end_ms=end_ms, result=result)

    async def _handle_engine_replay_comparison_run(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        payload = self._payload(command)
        start_ms, end_ms = self._window(payload, default_hours=24)
        artifact = await EngineReplayComparisonService(repository=self.repository, settings=self.settings).compare_variant(
            baseline_config=self._dict_payload(payload.get("baseline_config")),
            candidate_config=self._dict_payload(payload.get("candidate_config")),
            window_start_ms=start_ms,
            window_end_ms=end_ms,
            universe=[str(symbol).upper() for symbol in payload.get("universe") or []],
            variant_id=str(payload.get("variant_id") or "") or None,
        )
        return self._result(command, window_start_ms=start_ms, window_end_ms=end_ms, result=artifact)

    async def _handle_hip4_loop_run_once(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        payload = self._payload(command)
        result = await self._get_hip4_service().run_proactive_cycle(manual=bool(payload.get("manual", True)))
        return self._result(command, result=result)

    async def _handle_hip4_scan_run(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        payload = self._payload(command)
        items = await self._get_hip4_service().run_scan(send_digest=bool(payload.get("send_digest", False)))
        return self._result(command, count=len(items), items=items)

    async def _handle_hip4_paper_execute(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        payload = self._payload(command)
        candidate_id = self._required_str(payload, "candidate_id")
        result = await self._get_hip4_service().execute_paper_candidate(candidate_id)
        return self._result(command, result=result)

    async def _handle_hip4_reconcile_run(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        result = await self._get_hip4_service().reconcile_paper()
        return self._result(command, result=result)

    async def _handle_hip4_manual_ticket(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        payload = self._payload(command)
        result = await self._get_hip4_service().manual_ticket(self._required_str(payload, "candidate_id"))
        return self._result(command, result=result)

    async def _handle_autonomy_pause(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        actor = self._actor(command)
        await self._get_autonomy_service().pause(actor=actor)
        return self._result(command, actor=actor, paused=True)

    async def _handle_autonomy_resume(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        actor = self._actor(command)
        await self._get_autonomy_service().resume(actor=actor)
        return self._result(command, actor=actor, paused=False)

    async def _handle_autonomy_signal_approve(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        payload = self._payload(command)
        result = await self._get_autonomy_service().approve_signal(self._required_str(payload, "signal_id"), actor=self._actor(command), mid=self._optional_float(payload.get("mid")))
        return self._result(command, result=result)

    async def _handle_autonomy_signal_reject(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        payload = self._payload(command)
        signal = await self._get_autonomy_service().reject_signal(self._required_str(payload, "signal_id"), actor=self._actor(command), reason=str(payload.get("reason") or ""))
        return self._result(command, signal=signal.model_dump(mode="json"))

    async def _handle_autonomy_signal_expire(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        payload = self._payload(command)
        signal = await self._get_autonomy_service().expire_signal(self._required_str(payload, "signal_id"), actor=self._actor(command))
        return self._result(command, signal=signal.model_dump(mode="json"))

    async def _handle_autonomy_equity_signal_approve(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        payload = self._payload(command)
        result = await self._get_autonomy_service().approve_equity_signal(self._required_str(payload, "signal_id"), actor=self._actor(command))
        return self._result(command, result=result)

    async def _handle_autonomy_equity_signal_reject(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        payload = self._payload(command)
        signal = await self._get_autonomy_service().reject_equity_signal(self._required_str(payload, "signal_id"), actor=self._actor(command), reason=str(payload.get("reason") or ""))
        return self._result(command, signal=signal.model_dump(mode="json"))

    async def _handle_tracking_pause(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        return await self._set_tracker_status(command, "paused")

    async def _handle_tracking_resume(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        return await self._set_tracker_status(command, "active")

    async def _handle_tracking_stop(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        return await self._set_tracker_status(command, "stopped")

    async def _handle_admin_debug_seed_flip_demo(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        payload = self._payload(command)
        now = int(time.time() * 1000)
        symbol = str(payload.get("symbol") or "BTC").upper()
        entry = float(payload.get("entry") or 50_000.0)
        opposing_side = str(payload.get("opposing_side") or "short")
        signal_side = "long" if opposing_side == "short" else "short"
        signal = TradeSignal(
            id=f"debug_flip_{now}",
            symbol=symbol,
            side=signal_side,  # type: ignore[arg-type]
            signal_type="debug_flip_demo",
            status="candidate",
            score=75.0,
            confidence=0.75,
            created_at_ms=now,
            expires_at_ms=now + 30 * 60 * 1000,
            entry=entry,
            stop=entry * (0.98 if signal_side == "long" else 1.02),
            take_profit=entry * (1.04 if signal_side == "long" else 0.96),
            invalidation="debug demo invalidation",
            thesis="Debug flip-demo signal for paper-only command-boundary testing.",
            evidence=[SignalEvidence(category="debug", label="seeded_by", value="trader worker", weight=0.5, source="risk", kind="text")],
            metadata={"debug_demo": True, "opposing_side": opposing_side, "exchange_actions": []},
        )
        service = self._get_autonomy_service()
        service.signals[signal.id] = signal
        if callable(getattr(service, "_persist_signal", None)):
            await service._persist_signal(signal)
        if callable(getattr(self.repository, "record_autonomy_event", None)):
            await self.repository.record_autonomy_event(
                "debug_flip_demo_seeded",
                actor=self._actor(command),
                symbol=symbol,
                payload={"signal_id": signal.id, "opposing_side": opposing_side, "paper_only": True, "exchange_actions": []},
            )
        return self._result(command, signal=signal.model_dump(mode="json"), paper_only=True)

    async def _set_tracker_status(self, command: dict[str, Any], status: str) -> dict[str, Any]:
        payload = self._payload(command)
        tracker_id = self._required_str(payload, "tracker_id")
        tracker = await self.repository.get_position_tracker(tracker_id)
        if tracker is None:
            raise KeyError("tracker not found")
        await self.repository.set_position_tracker_status(tracker_id, status, reason="trader_worker")
        updated = await self.repository.get_position_tracker(tracker_id)
        return self._result(command, status=status, tracker=updated)

    def _record_command(self, command: dict[str, Any]) -> None:
        self.command_count += 1
        self.last_command_type = str(command.get("command_type") or "")

    def _payload(self, command: dict[str, Any]) -> dict[str, Any]:
        payload = command.get("payload")
        return payload if isinstance(payload, dict) else {}

    def _actor(self, command: dict[str, Any]) -> str:
        payload = self._payload(command)
        return str(payload.get("actor") or command.get("requested_by") or "trader_worker")

    def _result(self, command: dict[str, Any], **result: Any) -> dict[str, Any]:
        return {"accepted_by": self.instance_id, "command_type": command.get("command_type"), "paper_only": True, "exchange_actions": [], **result}

    def _window(self, payload: dict[str, Any], *, default_hours: int) -> tuple[int, int]:
        end_ms = int(payload.get("window_end_ms") or int(time.time() * 1000))
        if payload.get("window_start_ms") is not None:
            start_ms = int(payload["window_start_ms"])
        else:
            hours = int(payload.get("window_hours") or default_hours)
            start_ms = end_ms - hours * 60 * 60 * 1000
        return start_ms, end_ms

    def _dict_payload(self, value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    def _required_str(self, payload: dict[str, Any], key: str) -> str:
        value = str(payload.get(key) or "")
        if not value:
            raise ValueError(f"missing payload.{key}")
        return value

    def _optional_float(self, value: Any) -> float | None:
        return float(value) if value is not None else None

    def _get_hip4_service(self) -> Hip4Service:
        if self._hip4_service is None:
            self._hip4_service = Hip4Service(settings=self.settings, repository=self.repository, hyperliquid=None, ws_worker=None, risk_gateway=None)
        return self._hip4_service

    def _get_memory_service(self) -> MemoryService:
        if self._memory_service is None:
            self._memory_service = MemoryService(settings=self.settings, repository=self.repository)
        return self._memory_service

    def _get_autonomy_service(self) -> AutonomousTradingLoopService:
        if self._autonomy_service is None:
            memory = self._get_memory_service()
            self._autonomy_service = AutonomousTradingLoopService(
                settings=self.settings,
                repository=self.repository,
                hyperliquid=None,
                news=None,
                memory_service=memory,
                alert_sink=None,
                model_gateway=None,
                risk_gateway=None,
            )
        return self._autonomy_service

    def heartbeat_metadata(self) -> dict[str, Any]:
        return {"trader": {"command_count": self.command_count, "last_command_type": self.last_command_type, "execution_authority": "paper-only/settings-gated"}}
