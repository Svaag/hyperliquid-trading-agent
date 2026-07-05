from __future__ import annotations

import asyncio
import time
from typing import Any

from hyperliquid_trading_agent.app.autonomy.memory import MemoryService
from hyperliquid_trading_agent.app.autonomy.schemas import SignalEvidence, TradeSignal
from hyperliquid_trading_agent.app.autonomy.service import AutonomousTradingLoopService
from hyperliquid_trading_agent.app.config import ServiceRole, Settings
from hyperliquid_trading_agent.app.engine.bandit import OfflineContextualBanditReporter
from hyperliquid_trading_agent.app.engine.newswire_bridge import EngineNewsConsumer
from hyperliquid_trading_agent.app.engine.replay_compare import EngineReplayComparisonService
from hyperliquid_trading_agent.app.engine.service import InstitutionalEngineService
from hyperliquid_trading_agent.app.engine.strategy_performance import refresh_strategy_regime_performance
from hyperliquid_trading_agent.app.governance.risk_gateway import RiskGateway
from hyperliquid_trading_agent.app.hip4.service import Hip4Service
from hyperliquid_trading_agent.app.hyperliquid.client import HyperliquidClient
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.newswire.bus import InProcessNewswireBus
from hyperliquid_trading_agent.app.paper.schemas import PaperTradeDraftRequest
from hyperliquid_trading_agent.app.prediction_markets.paper import PredictionMarketPaperService
from hyperliquid_trading_agent.app.prediction_markets.schemas import (
    PredictionMarketBetDraftRequest,
    PredictionMarketSettlementRequest,
)
from hyperliquid_trading_agent.app.workers.base import BaseWorker
from hyperliquid_trading_agent.app.workers.stored_newswire_pump import StoredNewswirePump

log = get_logger(__name__)


class TraderWorker(BaseWorker):
    role = ServiceRole.TRADER
    lock_name = "service:trader"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.command_count = 0
        self.last_command_type: str | None = None
        self._hip4_service: Hip4Service | None = None
        self._prediction_market_paper: PredictionMarketPaperService | None = None
        self._autonomy_service: AutonomousTradingLoopService | None = None
        self._memory_service: MemoryService | None = None
        self._engine_service: InstitutionalEngineService | None = None
        self._engine_hyperliquid: HyperliquidClient | None = None
        self._engine_loop_task: asyncio.Task | None = None
        self._engine_loop_last_result: dict[str, Any] | None = None
        self._engine_loop_last_error: str | None = None
        self._engine_news_bus: InProcessNewswireBus | None = None
        self._engine_news_consumer: EngineNewsConsumer | None = None
        self._engine_news_pump: StoredNewswirePump | None = None

    async def run(self) -> None:
        await self._start_engine_loop()
        await self._start_engine_newsfeed()
        tasks = [asyncio.create_task(self.command_loop(self._command_handlers()), name="trader-command-loop")]
        if self._engine_loop_task is not None:
            tasks.append(self._engine_loop_task)
        if self._engine_news_pump is not None:
            tasks.append(asyncio.create_task(self._engine_news_pump.run_forever(), name="trader-engine-newswire-pump"))
        try:
            await self.wait_until_stopped()
        finally:
            if self._engine_news_pump is not None:
                await self._engine_news_pump.stop()
            if self._engine_news_consumer is not None:
                await self._engine_news_consumer.stop()
            for task in tasks:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await self._shutdown_engine_runtime()

    def _command_handlers(self):
        return {
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
            "paper_trade_draft": self._handle_paper_trade_draft,
            "paper_trade_confirm": self._handle_paper_trade_confirm,
            "paper_trade_cancel": self._handle_paper_trade_cancel,
            "paper_position_close": self._handle_paper_position_close,
            "prediction_market_bet_draft": self._handle_prediction_market_bet_draft,
            "prediction_market_bet_confirm": self._handle_prediction_market_bet_confirm,
            "prediction_market_bet_cancel": self._handle_prediction_market_bet_cancel,
            "prediction_market_position_close": self._handle_prediction_market_position_close,
            "prediction_market_settlement_apply": self._handle_prediction_market_settlement_apply,
            "prediction_market_settlement_sweep": self._handle_prediction_market_settlement_sweep,
            "tracking_pause": self._handle_tracking_pause,
            "tracking_resume": self._handle_tracking_resume,
            "tracking_stop": self._handle_tracking_stop,
            "admin_debug_seed_flip_demo": self._handle_admin_debug_seed_flip_demo,
        }

    async def _start_engine_newsfeed(self) -> None:
        if self._engine_news_pump is not None or not (self.settings.engine_enabled and self.settings.engine_newsfeed_enabled):
            return
        engine_service = self._ensure_engine_service()
        self._engine_news_bus = InProcessNewswireBus()
        self._engine_news_consumer = EngineNewsConsumer(settings=self.settings, bus=self._engine_news_bus, engine_service=engine_service)
        await self._engine_news_consumer.start()
        self._engine_news_pump = StoredNewswirePump(
            consumer_name="trader:engine_newswire",
            repository=self.repository,
            callbacks=[self._engine_news_bus.publish],
            poll_seconds=self.settings.consumer_poll_seconds,
            batch_size=self.settings.consumer_batch_size,
            bootstrap_from_latest=True,
            bootstrap_metadata={"consumer": "engine_newsfeed", "owner_role": self.role.value},
        )

    async def _start_engine_loop(self) -> None:
        if self._engine_loop_task is not None or not self.settings.engine_enabled:
            return
        self._ensure_engine_service()
        self._engine_loop_task = asyncio.create_task(self._run_engine_loop(), name="trader-engine-shadow-loop")
        log.info("trader_engine_loop_started", interval_seconds=self.settings.engine_loop_interval_seconds)

    async def _run_engine_loop(self) -> None:
        interval = max(5, int(self.settings.engine_loop_interval_seconds))
        while not self._stop.is_set():
            try:
                service = self._ensure_engine_service()
                self._engine_loop_last_result = await service.run_once(symbols=self.settings.autonomy_core_symbols)
                self._engine_loop_last_error = None
                log.info(
                    "trader_engine_loop_completed",
                    candidates=self._engine_loop_last_result.get("candidates"),
                    executed=self._engine_loop_last_result.get("executed"),
                    run_count=service.run_count,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - worker resilience
                self._engine_loop_last_error = type(exc).__name__
                log.warning("trader_engine_loop_failed", error=type(exc).__name__)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                continue

    def _ensure_engine_service(self) -> InstitutionalEngineService:
        if self._engine_service is None:
            self._engine_hyperliquid = HyperliquidClient(settings=self.settings)
            risk_gateway = RiskGateway(settings=self.settings, repository=self.repository)
            self._engine_service = InstitutionalEngineService(
                settings=self.settings,
                repository=self.repository,
                hyperliquid=self._engine_hyperliquid,
                risk_gateway=risk_gateway,
                portfolio_service=None,
                world_model_service=None,
                liquidation_bridge=None,
            )
        return self._engine_service

    async def _shutdown_engine_runtime(self) -> None:
        if self._engine_hyperliquid is not None:
            await self._engine_hyperliquid.close()
            self._engine_hyperliquid = None

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

    async def _handle_paper_trade_draft(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        self._require_manual_paper_enabled()
        payload = self._payload(command)
        request = await self._paper_draft_request(payload, actor=self._actor(command))
        mid = self._optional_float(payload.get("mid"))
        if mid is None and request.entry is None:
            mid = await self._latest_mid(request.symbol)
        order = await self._get_autonomy_service().portfolio.draft_trade(request, mid=mid)
        return self._result(command, order=order.model_dump(mode="json"))

    async def _handle_paper_trade_confirm(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        self._require_manual_paper_enabled()
        payload = self._payload(command)
        order_id = self._required_str(payload, "order_id")
        service = self._get_autonomy_service().portfolio
        await service.initialize()
        order = service.orders.get(order_id)
        mid = self._optional_float(payload.get("mid"))
        if mid is None and order is not None:
            mid = await self._latest_mid(order.symbol)
        order, fill, position = await service.confirm_draft(
            order_id,
            actor=self._actor(command),
            mid=mid,
            close_opposite=bool(payload.get("close_opposite", False)),
        )
        return self._result(command, order=order.model_dump(mode="json"), fill=fill.model_dump(mode="json"), position=position.model_dump(mode="json"))

    async def _handle_paper_trade_cancel(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        self._require_manual_paper_enabled()
        payload = self._payload(command)
        order = await self._get_autonomy_service().portfolio.cancel_draft(
            self._required_str(payload, "order_id"),
            actor=self._actor(command),
            reason=str(payload.get("reason") or "cancelled"),
        )
        return self._result(command, order=order.model_dump(mode="json"))

    async def _handle_paper_position_close(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        self._require_manual_paper_enabled()
        payload = self._payload(command)
        ref = self._required_str(payload, "position_ref")
        service = self._get_autonomy_service().portfolio
        await service.initialize()
        price = self._optional_float(payload.get("price"))
        if price is None:
            symbol = ref.upper()
            if ref in service.positions:
                symbol = service.positions[ref].symbol
            price = await self._latest_mid(symbol)
        if price is None:
            raise ValueError("paper close requires price when no latest mid is available")
        reason = str(payload.get("reason") or "manual")
        if ref in service.positions:
            position = await service.close_position(ref, price, reason=reason)
        else:
            position = await service.close_position_by_symbol(ref, price, reason=reason)
        return self._result(command, position=position.model_dump(mode="json"))

    async def _handle_prediction_market_bet_draft(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        payload = {**self._payload(command), "actor": self._actor(command)}
        request = PredictionMarketBetDraftRequest.model_validate(payload)
        result = await self._get_prediction_market_paper_service().draft_bet(request)
        return self._result(command, result=result)

    async def _handle_prediction_market_bet_confirm(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        payload = self._payload(command)
        result = await self._get_prediction_market_paper_service().confirm_draft(self._required_str(payload, "draft_id"), actor=self._actor(command))
        return self._result(command, result=result)

    async def _handle_prediction_market_bet_cancel(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        payload = self._payload(command)
        result = await self._get_prediction_market_paper_service().cancel_draft(
            self._required_str(payload, "draft_id"),
            actor=self._actor(command),
            reason=str(payload.get("reason") or "cancelled"),
        )
        return self._result(command, result=result)

    async def _handle_prediction_market_position_close(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        payload = self._payload(command)
        result = await self._get_prediction_market_paper_service().close_position(
            self._required_str(payload, "position_ref"),
            actor=self._actor(command),
            reason=str(payload.get("reason") or "manual"),
        )
        return self._result(command, result=result)

    async def _handle_prediction_market_settlement_apply(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        payload = {**self._payload(command), "actor": self._actor(command)}
        result = await self._get_prediction_market_paper_service().apply_settlement(PredictionMarketSettlementRequest.model_validate(payload))
        return self._result(command, result=result)

    async def _handle_prediction_market_settlement_sweep(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        result = await self._get_prediction_market_paper_service().settlement_sweep()
        return self._result(command, result=result)

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

    def _require_manual_paper_enabled(self) -> None:
        if not getattr(self.settings, "paper_trading_enabled", False):
            raise RuntimeError("PAPER_TRADING_ENABLED is false")

    async def _paper_draft_request(self, payload: dict[str, Any], *, actor: str) -> PaperTradeDraftRequest:
        if payload.get("proposal_id") and not payload.get("symbol"):
            proposal = await self.repository.get_trade_proposal(str(payload["proposal_id"]))
            if proposal is None:
                raise KeyError("trade proposal not found")
            payload = {**_paper_payload_from_proposal(proposal), **{k: v for k, v in payload.items() if v not in (None, "")}}
        payload = {**payload, "actor": str(payload.get("actor") or actor)}
        return PaperTradeDraftRequest.model_validate(payload)

    async def _latest_mid(self, symbol: str) -> float | None:
        try:
            client = self._get_hyperliquid_client()
            mids = await client.all_mids()
            value = mids.get(symbol.upper()) or mids.get(symbol)
            return float(value) if value is not None else None
        except Exception:
            return None

    def _get_hyperliquid_client(self) -> HyperliquidClient:
        if self._engine_hyperliquid is None:
            self._engine_hyperliquid = HyperliquidClient(settings=self.settings)
        return self._engine_hyperliquid

    def _get_hip4_service(self) -> Hip4Service:
        if self._hip4_service is None:
            self._hip4_service = Hip4Service(settings=self.settings, repository=self.repository, hyperliquid=None, ws_worker=None, risk_gateway=None)
        return self._hip4_service

    def _get_prediction_market_paper_service(self) -> PredictionMarketPaperService:
        if self._prediction_market_paper is None:
            self._prediction_market_paper = PredictionMarketPaperService(settings=self.settings, repository=self.repository)
        return self._prediction_market_paper

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
        return {
            "trader": {"command_count": self.command_count, "last_command_type": self.last_command_type, "execution_authority": "paper-only/settings-gated"},
            "engine_loop": self._engine_loop_metadata(),
            "engine_newsfeed": self._engine_newsfeed_metadata(),
        }

    def _engine_loop_metadata(self) -> dict[str, Any]:
        enabled = bool(self.settings.engine_enabled)
        task_running = self._engine_loop_task is not None and not self._engine_loop_task.done()
        return {
            "enabled": enabled,
            "running": task_running,
            "interval_seconds": self.settings.engine_loop_interval_seconds,
            "last_result": self._engine_loop_last_result or {},
            "last_error": self._engine_loop_last_error,
            "service": self._engine_service.status() if self._engine_service is not None else {},
        }

    def _engine_newsfeed_metadata(self) -> dict[str, Any]:
        enabled = bool(self.settings.engine_enabled and self.settings.engine_newsfeed_enabled)
        return {
            "enabled": enabled,
            "consumer_name": "trader:engine_newswire",
            "consumer": self._engine_news_consumer.status() if self._engine_news_consumer is not None else {},
            "pump": self._engine_news_pump.status() if self._engine_news_pump is not None else {},
            "bus": self._engine_news_bus.status() if self._engine_news_bus is not None else {},
            "thresholds": {
                "min_importance": self.settings.engine_news_min_importance,
                "min_source_score": self.settings.engine_news_min_source_score,
                "macro_min_importance": self.settings.engine_news_macro_min_importance,
                "catalyst_threshold": self.settings.engine_news_catalyst_threshold,
            },
        }


def _paper_payload_from_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    data = proposal.get("proposal") or proposal.get("proposal_json") or proposal
    if not isinstance(data, dict):
        data = {}
    return {
        "symbol": data.get("coin"),
        "side": data.get("side"),
        "entry": data.get("entry"),
        "stop": data.get("stop"),
        "take_profit": data.get("take_profit"),
        "risk_pct": data.get("risk_pct"),
        "thesis": data.get("thesis") or proposal.get("content") or "",
        "source": "trade_proposal",
        "proposal_id": proposal.get("id") or data.get("proposal_id"),
    }
