from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any

from hyperliquid_trading_agent.app.autonomy.memory import MemoryService
from hyperliquid_trading_agent.app.autonomy.schemas import SignalEvidence, TradeSignal
from hyperliquid_trading_agent.app.autonomy.service import AutonomousTradingLoopService
from hyperliquid_trading_agent.app.config import ServiceRole, Settings
from hyperliquid_trading_agent.app.engine.bandit import OfflineContextualBanditReporter
from hyperliquid_trading_agent.app.engine.evidence_loop import EngineEvidenceRefreshLoopService
from hyperliquid_trading_agent.app.engine.monitor import EngineValidationMonitorService
from hyperliquid_trading_agent.app.engine.newswire_bridge import EngineNewsConsumer
from hyperliquid_trading_agent.app.engine.newswire_replay import NewswireEngineReplayService
from hyperliquid_trading_agent.app.engine.operator_proposals import EngineOperatorProposalService
from hyperliquid_trading_agent.app.engine.pnl_loop import EnginePnLAttributionLoopService
from hyperliquid_trading_agent.app.engine.replay_compare import EngineReplayComparisonService
from hyperliquid_trading_agent.app.engine.service import InstitutionalEngineService
from hyperliquid_trading_agent.app.engine.strategy_performance import refresh_strategy_regime_performance
from hyperliquid_trading_agent.app.governance.risk_gateway import RiskGateway
from hyperliquid_trading_agent.app.hip4.service import Hip4Service
from hyperliquid_trading_agent.app.hyperliquid.client import HyperliquidClient
from hyperliquid_trading_agent.app.liquidations.signals import StoreBackedLiquidationSignalBridge
from hyperliquid_trading_agent.app.liquidations.store import LiquidationStore
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.markets.lighter_adapter import LighterSDKMarketDataAdapter
from hyperliquid_trading_agent.app.markets.sync import MarketUniverseSyncService, run_market_universe_sync_loop
from hyperliquid_trading_agent.app.newswire.bus import InProcessNewswireBus
from hyperliquid_trading_agent.app.paper.schemas import PaperTradeDraftRequest
from hyperliquid_trading_agent.app.prediction_markets.paper import PredictionMarketPaperService
from hyperliquid_trading_agent.app.prediction_markets.schemas import (
    PredictionMarketBetDraftRequest,
    PredictionMarketSettlementRequest,
)
from hyperliquid_trading_agent.app.tradfi.alpaca_paper_execution import AlpacaPaperExecutionAdapter
from hyperliquid_trading_agent.app.workers.base import BaseWorker
from hyperliquid_trading_agent.app.workers.stored_newswire_story_pump import StoredNewswireStoryPump

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
        self._engine_operator_proposals: EngineOperatorProposalService | None = None
        self._engine_validation_monitor: EngineValidationMonitorService | None = None
        self._engine_hyperliquid: HyperliquidClient | None = None
        self._engine_pnl_attribution: EnginePnLAttributionLoopService | None = None
        self._engine_evidence_refresh: EngineEvidenceRefreshLoopService | None = None
        self._engine_loop_task: asyncio.Task | None = None
        self._engine_loop_last_result: dict[str, Any] | None = None
        self._engine_loop_last_error: str | None = None
        self._engine_news_bus: InProcessNewswireBus | None = None
        self._engine_news_consumer: EngineNewsConsumer | None = None
        self._engine_news_pump: StoredNewswireStoryPump | None = None
        self._alpaca_paper_execution: AlpacaPaperExecutionAdapter | None = None
        self._market_universe_sync: MarketUniverseSyncService | None = None
        self._market_universe_sync_task: asyncio.Task | None = None

    async def run(self) -> None:
        await self._start_market_universe_sync()
        await self._start_engine_loop()
        await self._start_engine_validation_monitor()
        await self._start_engine_pnl_attribution()
        await self._start_engine_evidence_refresh()
        await self._start_engine_newsfeed()
        await self._start_autonomy_loop()
        tasks = [asyncio.create_task(self.command_loop(self._command_handlers()), name="trader-command-loop")]
        if self._engine_loop_task is not None:
            tasks.append(self._engine_loop_task)
        if self._engine_news_pump is not None:
            tasks.append(asyncio.create_task(self._engine_news_pump.run_forever(), name="trader-engine-newswire-pump"))
        if self._market_universe_sync_task is not None:
            tasks.append(self._market_universe_sync_task)
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
            "engine_position_thesis_cleanup": self._handle_engine_position_thesis_cleanup,
            "engine_bandit_run": self._handle_engine_bandit_run,
            "engine_replay_comparison_run": self._handle_engine_replay_comparison_run,
            "engine_newswire_replay": self._handle_engine_newswire_replay,
            "engine_operator_proposal_ack": self._handle_engine_operator_proposal_ack,
            "engine_operator_proposal_reject": self._handle_engine_operator_proposal_reject,
            "engine_operator_proposal_expire": self._handle_engine_operator_proposal_expire,
            "engine_validation_monitor_run_once": self._handle_engine_validation_monitor_run_once,
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
        self._engine_news_pump = StoredNewswireStoryPump(
            consumer_name="trader:engine_newswire",
            repository=self.repository,
            callbacks=[self._engine_news_bus.publish],
            poll_seconds=self.settings.consumer_poll_seconds,
            batch_size=self.settings.consumer_batch_size,
            bootstrap_from_latest=True,
            bootstrap_metadata={"consumer": "engine_newsfeed", "owner_role": self.role.value},
        )

    async def _start_market_universe_sync(self) -> None:
        if not self.settings.market_universe_enabled or self._market_universe_sync_task is not None:
            return
        hyperliquid = self._get_hyperliquid_client()
        lighter_adapter = None
        if self.settings.lighter_enabled:
            lighter_adapter = LighterSDKMarketDataAdapter(base_url=self.settings.lighter_base_url)
        alpaca_adapter = None
        if self.settings.alpaca_paper_trading_enabled:
            if self._alpaca_paper_execution is None:
                self._alpaca_paper_execution = AlpacaPaperExecutionAdapter(
                    api_key=self.settings.alpaca_paper_api_key,
                    api_secret=self.settings.alpaca_paper_api_secret,
                    repository=self.repository,
                    base_url=self.settings.alpaca_paper_base_url,
                    data_feed=self.settings.alpaca_data_feed,
                )
            alpaca_adapter = self._alpaca_paper_execution
        self._market_universe_sync = MarketUniverseSyncService(
            settings=self.settings,
            repository=self.repository,
            hyperliquid=hyperliquid,
            lighter_adapter=lighter_adapter,
            alpaca_adapter=alpaca_adapter,
        )
        self._market_universe_sync_task = asyncio.create_task(
            run_market_universe_sync_loop(
                self._market_universe_sync,
                self._stop,
                interval_seconds=self.settings.market_universe_snapshot_refresh_seconds,
            ),
            name="trader-market-universe-sync",
        )

    async def _start_autonomy_loop(self) -> None:
        if not self.settings.autonomy_enabled:
            return
        await self._get_autonomy_service().start()

    async def _start_engine_loop(self) -> None:
        if self._engine_loop_task is not None or not self.settings.engine_enabled:
            return
        self._ensure_engine_service()
        self._engine_loop_task = asyncio.create_task(self._run_engine_loop(), name="trader-engine-shadow-loop")
        log.info("trader_engine_loop_started", interval_seconds=self.settings.engine_loop_interval_seconds)

    async def _start_engine_pnl_attribution(self) -> None:
        if not self.settings.engine_enabled:
            return
        if self._engine_pnl_attribution is None:
            self._engine_pnl_attribution = EnginePnLAttributionLoopService(
                settings=self.settings,
                repository=self.repository,
                hyperliquid=self._get_hyperliquid_client(),
            )
        await self._engine_pnl_attribution.start()

    async def _start_engine_validation_monitor(self) -> None:
        if not self.settings.engine_enabled:
            return
        if self._engine_validation_monitor is None:
            self._engine_validation_monitor = EngineValidationMonitorService(
                settings=self.settings,
                repository=self.repository,
                engine_service=self._ensure_engine_service(),
                alert_sink=None,
            )
        await self._engine_validation_monitor.start()

    async def _start_engine_evidence_refresh(self) -> None:
        if not self.settings.engine_enabled:
            return
        if self._engine_evidence_refresh is None:
            self._engine_evidence_refresh = EngineEvidenceRefreshLoopService(settings=self.settings, repository=self.repository)
        await self._engine_evidence_refresh.start()

    async def _run_engine_loop(self) -> None:
        interval = max(5, int(self.settings.engine_loop_interval_seconds))
        while not self._stop.is_set():
            try:
                service = self._ensure_engine_service()
                self._engine_loop_last_result = await service.run_once(symbols=await self._engine_symbols())
                try:
                    proposal_result = await self._get_engine_operator_proposal_service().process_candidate_book(
                        self._engine_loop_last_result.get("candidate_book_id")
                    )
                    self._engine_loop_last_result["operator_proposals"] = proposal_result
                except Exception as exc:
                    log.warning("trader_engine_operator_proposals_failed", error=type(exc).__name__)
                    self._engine_loop_last_result["operator_proposals"] = {
                        "enabled": self.settings.engine_operator_proposals_enabled,
                        "error": type(exc).__name__,
                    }
                self._engine_loop_last_error = None
                stage_ms = self._engine_loop_last_result.get("stage_ms") or {}
                slowest_stage = max(stage_ms, key=lambda name: float(stage_ms.get(name) or 0)) if stage_ms else None
                log.info(
                    "trader_engine_loop_completed",
                    candidates=self._engine_loop_last_result.get("candidates"),
                    executed=self._engine_loop_last_result.get("executed"),
                    run_count=service.run_count,
                    duration_ms=self._engine_loop_last_result.get("duration_ms"),
                    slowest_stage=slowest_stage,
                )
                try:
                    await self._heartbeat("running")
                except Exception:
                    log.warning("trader_engine_loop_heartbeat_failed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - worker resilience
                self._engine_loop_last_error = type(exc).__name__
                log.warning("trader_engine_loop_failed", error=type(exc).__name__)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                continue

    async def _engine_symbols(self) -> list[str]:
        """Use active canonical Hyperliquid pins, retaining config as fallback."""

        symbols = list(self.settings.autonomy_core_symbols)
        try:
            memberships = await self.repository.list_watchlist_memberships(limit=20_000)
            desired_ids = {
                str(item.get("instrument_id"))
                for item in memberships
                if item.get("desired") and item.get("enabled")
            }
            instruments = await self.repository.list_instruments(
                tradability_status="active",
                limit=20_000,
            )
            symbols.extend(
                str(
                    item.get("display_symbol")
                    or str(item.get("provider_symbol") or "").split(":", 1)[-1]
                ).upper()
                for item in instruments
                if item.get("instrument_id") in desired_ids
                and str(item.get("venue_id") or "").startswith("hyperliquid:")
                and item.get("instrument_type")
                in {
                    "crypto_perp",
                    "hip3_perp",
                    "index_benchmark",
                    "commodity_perp",
                    "fx_perp",
                    "synthetic_perp",
                }
            )
        except Exception:
            pass
        return list(dict.fromkeys(symbol for symbol in symbols if symbol))[: self.settings.autonomy_max_tracked_assets]

    def _ensure_engine_service(self) -> InstitutionalEngineService:
        if self._engine_service is None:
            self._engine_hyperliquid = self._get_hyperliquid_client()
            risk_gateway = RiskGateway(settings=self.settings, repository=self.repository)
            liquidation_bridge = None
            if bool(getattr(self.settings, "engine_liquidation_features_enabled", True)) and self.sessionmaker is not None:
                liquidation_bridge = StoreBackedLiquidationSignalBridge(LiquidationStore(self.sessionmaker))
            self._engine_service = InstitutionalEngineService(
                settings=self.settings,
                repository=self.repository,
                hyperliquid=self._engine_hyperliquid,
                risk_gateway=risk_gateway,
                portfolio_service=None,
                world_model_service=None,
                liquidation_bridge=liquidation_bridge,
            )
        return self._engine_service

    async def _shutdown_engine_runtime(self) -> None:
        if self._autonomy_service is not None:
            await self._autonomy_service.stop()
            self._autonomy_service = None
        if self._engine_validation_monitor is not None:
            await self._engine_validation_monitor.stop()
            self._engine_validation_monitor = None
        if self._engine_evidence_refresh is not None:
            await self._engine_evidence_refresh.stop()
            self._engine_evidence_refresh = None
        if self._engine_pnl_attribution is not None:
            await self._engine_pnl_attribution.stop()
            self._engine_pnl_attribution = None
        if self._market_universe_sync is not None:
            await self._market_universe_sync.close()
            self._market_universe_sync = None
        self._market_universe_sync_task = None
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

    async def _handle_engine_position_thesis_cleanup(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        payload = self._payload(command)
        before_ms = int(payload.get("before_ms") or 0)
        states = [str(state) for state in payload.get("states") or ["approved"]]
        reason = str(payload.get("reason") or "stale_position_cleanup")
        dry_run = bool(payload.get("dry_run", True))
        affected = await self.repository.close_stale_position_theses(
            before_ms=before_ms,
            states=states,
            reason=reason,
            limit=int(payload.get("limit") or 20000),
            dry_run=dry_run,
        )
        return self._result(command, before_ms=before_ms, states=states, reason=reason, dry_run=dry_run, affected_count=affected)

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

    async def _handle_engine_newswire_replay(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        payload = dict(self._payload(command))
        payload.setdefault(
            "replay_run_id",
            "nwr_" + hashlib.sha1(str(command.get("command_id") or self.instance_id).encode()).hexdigest()[:20],
        )
        result = await NewswireEngineReplayService(
            settings=self.settings,
            repository=self.repository,
        ).run(payload)
        return self._result(
            command,
            result=result,
            report_only=True,
            execution_authority="none",
            live_consumer_offset_write_performed=False,
        )

    async def _handle_engine_operator_proposal_ack(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        payload = self._payload(command)
        proposal = await self._get_engine_operator_proposal_service().acknowledge(
            self._required_str(payload, "proposal_id"),
            actor=self._actor(command),
        )
        if proposal is None:
            raise KeyError("engine operator proposal not found")
        return self._result(
            command,
            proposal=proposal,
            acknowledgment_only=True,
            paper_order_created=False,
        )

    async def _handle_engine_operator_proposal_reject(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        payload = self._payload(command)
        proposal = await self._get_engine_operator_proposal_service().reject(
            self._required_str(payload, "proposal_id"),
            actor=self._actor(command),
            reason=str(payload.get("reason") or ""),
        )
        if proposal is None:
            raise KeyError("engine operator proposal not found")
        return self._result(command, proposal=proposal, paper_order_created=False)

    async def _handle_engine_operator_proposal_expire(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        payload = self._payload(command)
        proposal = await self._get_engine_operator_proposal_service().expire(
            self._required_str(payload, "proposal_id"),
            actor=self._actor(command),
        )
        if proposal is None:
            raise KeyError("engine operator proposal not found")
        return self._result(command, proposal=proposal, paper_order_created=False)

    async def _handle_engine_validation_monitor_run_once(self, command: dict[str, Any]) -> dict[str, Any]:
        self._record_command(command)
        if self._engine_validation_monitor is None:
            await self._start_engine_validation_monitor()
        if self._engine_validation_monitor is None:
            raise RuntimeError("engine_validation_monitor_disabled")
        result = await self._engine_validation_monitor.run_once(post=True)
        return self._result(command, result=result, report_only=True)

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
            self._prediction_market_paper = PredictionMarketPaperService(settings=self.settings, repository=self.repository, hyperliquid=self._get_hyperliquid_client())
        return self._prediction_market_paper

    def _get_memory_service(self) -> MemoryService:
        if self._memory_service is None:
            self._memory_service = MemoryService(settings=self.settings, repository=self.repository)
        return self._memory_service

    def _get_autonomy_service(self) -> AutonomousTradingLoopService:
        if self._autonomy_service is None:
            memory = self._get_memory_service()
            if self.settings.alpaca_paper_trading_enabled and self._alpaca_paper_execution is None:
                self._alpaca_paper_execution = AlpacaPaperExecutionAdapter(
                    api_key=self.settings.alpaca_paper_api_key,
                    api_secret=self.settings.alpaca_paper_api_secret,
                    repository=self.repository,
                    base_url=self.settings.alpaca_paper_base_url,
                    data_feed=self.settings.alpaca_data_feed,
                )
            self._autonomy_service = AutonomousTradingLoopService(
                settings=self.settings,
                repository=self.repository,
                hyperliquid=self._get_hyperliquid_client(),
                news=None,
                memory_service=memory,
                alert_sink=None,
                model_gateway=None,
                risk_gateway=None,
                equity_paper_execution=self._alpaca_paper_execution,
            )
        return self._autonomy_service

    def _get_engine_operator_proposal_service(self) -> EngineOperatorProposalService:
        if self._engine_operator_proposals is None:
            self._engine_operator_proposals = EngineOperatorProposalService(
                settings=self.settings,
                repository=self.repository,
            )
        return self._engine_operator_proposals

    def heartbeat_metadata(self) -> dict[str, Any]:
        return {
            "trader": {"command_count": self.command_count, "last_command_type": self.last_command_type, "execution_authority": "paper-only/settings-gated"},
            "autonomy_loop": (
                {
                    **self._autonomy_service.status(),
                    "owner_role": "trader",
                    "runtime_source": "trader_heartbeat",
                }
                if self._autonomy_service is not None
                else {
                    "enabled": self.settings.autonomy_enabled,
                    "running": False,
                    "owner_role": "trader",
                    "runtime_source": "trader_heartbeat",
                    "signals_run_with_engine_enabled": self.settings.autonomy_signals_run_with_engine_enabled,
                }
            ),
            "engine_loop": self._engine_loop_metadata(),
            "engine_pnl_attribution": self._engine_pnl_attribution_metadata(),
            "engine_evidence_refresh": self._engine_evidence_refresh.status() if self._engine_evidence_refresh is not None else {"enabled": False, "running": False},
            "engine_newsfeed": self._engine_newsfeed_metadata(),
            "engine_operator_proposals": (
                self._engine_operator_proposals.status()
                if self._engine_operator_proposals is not None
                else {
                    "enabled": self.settings.engine_operator_proposals_enabled,
                    "execution_authority": "none",
                    "acknowledgment_only": True,
                }
            ),
            "alpaca_paper": {
                "enabled": self.settings.alpaca_paper_trading_enabled,
                "configured": self._alpaca_paper_execution is not None,
                "source_of_truth": "alpaca_paper" if self._alpaca_paper_execution is not None else None,
                "live_capable": False,
                "last_reconciliation": (
                    self._alpaca_paper_execution.last_reconciliation
                    if self._alpaca_paper_execution is not None
                    else None
                ),
            },
            "market_universe": (
                self._market_universe_sync.status()
                if self._market_universe_sync is not None
                else {"enabled": self.settings.market_universe_enabled, "running": False}
            ),
            "engine_validation_monitor": (
                self._engine_validation_monitor.status()
                if self._engine_validation_monitor is not None
                else {
                    "enabled": bool(self.settings.engine_enabled and self.settings.engine_validation_digest_enabled),
                    "running": False,
                    "owner_role": "trader",
                }
            ),
        }

    def _engine_loop_metadata(self) -> dict[str, Any]:
        enabled = bool(self.settings.engine_enabled)
        task_running = self._engine_loop_task is not None and not self._engine_loop_task.done()
        return {
            "enabled": enabled,
            "running": task_running,
            "interval_seconds": self.settings.engine_loop_interval_seconds,
            "execution_modes": self.settings.engine_execution_mode_list,
            "shadow_enabled": self.settings.engine_shadow_enabled,
            "paper_enabled": self.settings.engine_paper_enabled,
            "live_enabled": self.settings.engine_live_enabled,
            "wave1c_enabled": self.settings.engine_wave1c_enabled,
            "wave2_enabled": self.settings.engine_wave2_enabled,
            "last_result": self._engine_loop_last_result or {},
            "last_error": self._engine_loop_last_error,
            "service": self._engine_service.status() if self._engine_service is not None else {},
        }

    def _engine_pnl_attribution_metadata(self) -> dict[str, Any]:
        return self._engine_pnl_attribution.status() if self._engine_pnl_attribution is not None else {"enabled": bool(self.settings.engine_pnl_attribution_enabled), "running": False}

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
