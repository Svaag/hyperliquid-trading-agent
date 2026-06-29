from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from hyperliquid_trading_agent.app.agent.model_gateway import ModelGateway
from hyperliquid_trading_agent.app.autonomy.discord import (
    AutonomyAlertSink,
    format_event_evaluation,
    format_flip_request,
    format_market_map,
    format_memories,
    format_orders,
    format_portfolio_snapshot,
    format_positions,
    format_signal_alert,
    format_signal_detail,
    format_signal_evaluation,
    format_signals,
    format_tuning_proposal,
    format_tuning_proposals,
)
from hyperliquid_trading_agent.app.autonomy.market_map import MarketMapReducer
from hyperliquid_trading_agent.app.autonomy.newswire import AutonomyNewswire
from hyperliquid_trading_agent.app.autonomy.portfolio import PaperPortfolioService, RiskControlError
from hyperliquid_trading_agent.app.autonomy.schemas import (
    AutonomyCommand,
    MarketAsset,
    NewsEvent,
    OperatorFeedback,
    TradeSignal,
)
from hyperliquid_trading_agent.app.autonomy.signals import SignalEngine, maybe_attach_model_insight
from hyperliquid_trading_agent.app.autonomy.universe import MarketUniverseResolver
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.governance.risk_gateway import RiskGateway
from hyperliquid_trading_agent.app.hyperliquid.ws_worker import SubscriptionSpec
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.metrics import (
    AUTONOMY_LOOP_ITERATIONS,
    AUTONOMY_MARKET_OBSERVATIONS,
    AUTONOMY_MODEL_INSIGHT_CALLS,
    AUTONOMY_PAPER_FILLS,
    AUTONOMY_PAPER_ORDERS,
    AUTONOMY_PORTFOLIO_DRAWDOWN,
    AUTONOMY_PORTFOLIO_EQUITY,
    AUTONOMY_SIGNALS_APPROVED,
    AUTONOMY_SIGNALS_CREATED,
    AUTONOMY_SIGNALS_POSTED,
    AUTONOMY_SIGNALS_REJECTED,
    NEWSWIRE_EVENTS,
)
from hyperliquid_trading_agent.app.tradfi.paper.schemas import EquityRiskControlError, EquityTradeRequest

log = get_logger(__name__)


class AutonomousTradingLoopService:
    """Background autonomy loop: market map -> signals -> Discord signoff -> paper portfolio."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: Any,
        hyperliquid: Any,
        news: Any,
        ws_worker: Any | None = None,
        model_gateway: ModelGateway | None = None,
        alert_sink: AutonomyAlertSink | None = None,
        evaluation_service: Any | None = None,
        event_evaluation_service: Any | None = None,
        memory_service: Any | None = None,
        report_service: Any | None = None,
        tuning_service: Any | None = None,
        tradfi: Any | None = None,
        equity_portfolio: Any | None = None,
        equity_signal_generator: Any | None = None,
        options_flow: Any | None = None,
        flow_enricher: Any | None = None,
        decision_context_recorder: Any | None = None,
        risk_gateway: RiskGateway | None = None,
        engine_service: Any | None = None,
        world_model_service: Any | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.hyperliquid = hyperliquid
        self.news = news
        self.ws_worker = ws_worker
        self.model_gateway = model_gateway
        self.alert_sink = alert_sink
        self.evaluation_service = evaluation_service
        self.event_evaluation_service = event_evaluation_service
        self.memory_service = memory_service
        self.report_service = report_service
        self.tuning_service = tuning_service
        self.tradfi = tradfi
        self.equity_portfolio = equity_portfolio
        self.equity_signal_generator = equity_signal_generator
        self.options_flow = options_flow
        self.flow_enricher = flow_enricher
        self.decision_context_recorder = decision_context_recorder
        self.risk_gateway = risk_gateway or RiskGateway(settings=settings, repository=repository, decision_context_recorder=decision_context_recorder)
        self.engine_service = engine_service
        self.world_model_service = world_model_service
        self.universe_resolver = MarketUniverseResolver(settings, hyperliquid)
        self.reducer = MarketMapReducer()
        self.newswire = AutonomyNewswire(settings, news)
        self.signal_engine = SignalEngine(settings)
        self.portfolio = PaperPortfolioService(settings, repository)
        self.running = False
        self.paused = False
        self.last_error: str | None = None
        self.last_market_data_at_ms: int | None = None
        self.last_iteration_at_ms: int | None = None
        self.hot_l2_assets: list[str] = []
        self.universe: list[MarketAsset] = []
        self.signals: dict[str, TradeSignal] = {}
        self.news_events: dict[str, NewsEvent] = {}
        self.equity_signals: dict[str, TradeSignal] = {}
        self.equity_flow_events: dict[str, Any] = {}
        self._task: asyncio.Task | None = None
        self._subscription_id: str | None = None
        self._last_deep_scan_ms = 0
        self._last_news_ms = 0
        self._last_portfolio_snapshot_ms = 0
        self._last_equity_scan_ms = 0
        self._model_call_timestamps: list[float] = []

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.paused = False
        if self.memory_service is not None and callable(getattr(self.memory_service, "load", None)):
            await self.memory_service.load()
        if self.evaluation_service is not None and callable(getattr(self.evaluation_service, "load_open", None)):
            await self.evaluation_service.load_open()
        if self.event_evaluation_service is not None and callable(getattr(self.event_evaluation_service, "load_open", None)):
            await self.event_evaluation_service.load_open()
        if self.ws_worker is not None:
            self._subscription_id = await self.ws_worker.subscribe(SubscriptionSpec("allMids"), self._on_all_mids)
        self._task = asyncio.create_task(self._run(), name="autonomous-trading-loop")
        await self._record_event("autonomy_started", payload={"mode": self.settings.autonomy_mode, "exchange_actions": []})
        log.info("autonomy_task_started", mode=self.settings.autonomy_mode)

    async def stop(self) -> None:
        self.running = False
        if self.ws_worker is not None and self._subscription_id is not None:
            await self.ws_worker.unsubscribe(self._subscription_id)
            self._subscription_id = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._record_event("autonomy_stopped", payload={"exchange_actions": []})

    def status(self) -> dict[str, Any]:
        snapshot = self.portfolio.latest_snapshot()
        equity_snapshot = self.equity_portfolio.snapshots[-1] if self.equity_portfolio is not None and self.equity_portfolio.snapshots else None
        return {
            "enabled": self.settings.autonomy_enabled,
            "running": self.running,
            "paused": self.paused,
            "mode": self.settings.autonomy_mode,
            "universe_count": len(self.universe),
            "universe_symbols": [asset.symbol for asset in self.universe],
            "hot_l2_assets": self.hot_l2_assets,
            "signals_today": self.signals_today(),
            "open_positions": len(self.portfolio.open_positions()),
            "last_market_data_at_ms": self.last_market_data_at_ms,
            "last_iteration_at_ms": self.last_iteration_at_ms,
            "last_error": self.last_error,
            "signals_run_with_engine_enabled": self.settings.autonomy_signals_run_with_engine_enabled,
            "paper_portfolio_id": self.portfolio.portfolio.id if self.portfolio.portfolio else None,
            "portfolio_equity_usd": snapshot.equity_usd if snapshot else None,
            "equity": {
                "enabled": self.settings.autonomy_equity_enabled,
                "effective_enabled": self.settings.autonomy_equity_effective_enabled and self.tradfi is not None,
                "universe_symbols": self.settings.autonomy_equity_symbols,
                "signals_today": self.equity_signals_today(),
                "open_positions": len([p for p in self.equity_portfolio.positions.values() if p.status == "open"]) if self.equity_portfolio is not None else 0,
                "portfolio_equity_usd": equity_snapshot.equity_usd if equity_snapshot else None,
                "last_scan_at_ms": self._last_equity_scan_ms or None,
            },
            "evaluation": self.evaluation_service.status() if self.evaluation_service is not None and callable(getattr(self.evaluation_service, "status", None)) else {},
            "event_evaluation": self.event_evaluation_service.status() if self.event_evaluation_service is not None and callable(getattr(self.event_evaluation_service, "status", None)) else {},
            "memory": self.memory_service.status() if self.memory_service is not None and callable(getattr(self.memory_service, "status", None)) else {},
            "reports": self.report_service.status() if self.report_service is not None and callable(getattr(self.report_service, "status", None)) else {},
            "tuning_proposals": self.tuning_service.status() if self.tuning_service is not None and callable(getattr(self.tuning_service, "status", None)) else {},
            "warnings": [*self.settings.autonomy_config_warnings(), *self.universe_resolver.warnings],
            "world_model": self.world_model_service.status() if self.world_model_service is not None and callable(getattr(self.world_model_service, "status", None)) else {},
        }

    def signals_today(self) -> int:
        today = datetime.now(UTC).date()
        return sum(1 for signal in self.signals.values() if datetime.fromtimestamp(signal.created_at_ms / 1000, tz=UTC).date() == today)

    def equity_signals_today(self) -> int:
        today = datetime.now(UTC).date()
        return sum(1 for signal in self.equity_signals.values() if datetime.fromtimestamp(signal.created_at_ms / 1000, tz=UTC).date() == today)

    async def pause(self, actor: str = "api") -> None:
        self.paused = True
        await self._record_event("autonomy_paused", actor=actor, payload={"exchange_actions": []})

    async def resume(self, actor: str = "api") -> None:
        self.paused = False
        await self._record_event("autonomy_resumed", actor=actor, payload={"exchange_actions": []})

    async def run_once(self) -> None:
        await self._run_iteration()

    async def approve_signal(self, signal_id: str, actor: str, mid: float | None = None) -> dict[str, Any]:
        signal = await self._get_signal(signal_id)
        if signal is None:
            if signal_id in self.equity_signals:
                return await self.approve_equity_signal(signal_id, actor=actor)
            raise KeyError("signal not found")
        if signal.status not in {"candidate", "posted", "flip_requested"}:
            raise RiskControlError(f"signal status {signal.status} cannot be approved")
        if signal.status == "flip_requested":
            return await self._approve_flip(signal, actor, mid=mid)
        now = _now_ms()
        if signal.expires_at_ms <= now:
            signal = signal.model_copy(update={"status": "expired"})
            self.signals[signal.id] = signal
            await self._persist_signal(signal)
            raise RiskControlError("signal is expired")
        state = self.reducer.snapshot().assets.get(signal.symbol)
        ref_px = mid or (state.mid if state is not None else None) or signal.entry
        await self._persist_signal(signal)
        risk_decision = await self._check_risk_gateway(signal, ref_px=ref_px, asset_class="crypto")
        if not risk_decision.allowed:
            raise RiskControlError(f"risk gateway rejected signal: {risk_decision.violations}")
        try:
            order, fill, position = await self.portfolio.approve_signal(signal, actor, mid=ref_px, timestamp_ms=now)
        except RiskControlError as exc:
            flip = await self._maybe_request_flip(signal, actor, reason=str(exc), ref_px=ref_px)
            if flip is not None:
                return flip
            raise
        updated = signal.model_copy(update={"status": "paper_ordered"})
        self.signals[updated.id] = updated
        AUTONOMY_SIGNALS_APPROVED.inc()
        AUTONOMY_PAPER_ORDERS.labels(status="filled").inc()
        AUTONOMY_PAPER_FILLS.labels(symbol=fill.symbol).inc()
        await self._persist_signal(updated, approved_by=actor)
        if self.evaluation_service is not None:
            await self.evaluation_service.update_signal_status(updated, paper_position_id=position.id)
        await self._record_event(
            "signal_approved_paper_ordered",
            actor=actor,
            symbol=signal.symbol,
            payload={"signal_id": signal.id, "order_id": order.id, "fill_id": fill.id, "position_id": position.id, "risk_gateway_decision_id": risk_decision.decision_id, "exchange_actions": []},
        )
        return {"signal": updated.model_dump(mode="json"), "order": order.model_dump(mode="json"), "fill": fill.model_dump(mode="json"), "position": position.model_dump(mode="json")}

    async def _maybe_request_flip(self, signal: TradeSignal, actor: str, *, reason: str, ref_px: float) -> dict[str, Any] | None:
        """If the only reason a new position can't be sized is an opposing open
        position exhausting the single-name exposure cap, close the opposing
        position and mark the signal as ``flip_requested`` so the operator can
        approve the new side in a second step.

        Returns the result dict when a flip was requested, otherwise ``None``.
        """
        opposing = self.portfolio.find_opposing_position(signal.symbol, signal.side)
        if opposing is None:
            return None
        diagnostics = self.portfolio.sizing_diagnostics(signal, ref_px)
        if not diagnostics.get("opposing_position_id"):
            return None
        now = _now_ms()
        close_price = ref_px
        closed = await self.portfolio.close_position(
            opposing.id, close_price, reason="flip_requested", timestamp_ms=now
        )
        updated = signal.model_copy(
            update={
                "status": "flip_requested",
                "metadata": {**(signal.metadata or {}), "flip_from_position_id": closed.id},
            }
        )
        self.signals[updated.id] = updated
        await self._persist_signal(updated)
        if self.evaluation_service is not None:
            await self.evaluation_service.update_signal_status(updated)
        await self._record_event(
            "signal_flip_requested",
            actor=actor,
            symbol=signal.symbol,
            payload={
                "signal_id": signal.id,
                "closed_position_id": closed.id,
                "closed_realized_pnl_usd": closed.realized_pnl_usd,
                "reason": reason,
                "diagnostics": diagnostics,
                "exchange_actions": [],
            },
        )
        if self.alert_sink is not None and self.settings.autonomy_alert_channel_id:
            try:
                await self.alert_sink.send(
                    self.settings.autonomy_alert_channel_id,
                    format_flip_request(
                        updated,
                        opposing_position=closed.model_dump(mode="json"),
                        diagnostics=diagnostics,
                    ),
                )
            except Exception as exc:  # pragma: no cover - alert sink is best effort
                log.warning("flip_alert_failed", error=type(exc).__name__)
        return {
            "signal": updated.model_dump(mode="json"),
            "closed_position": closed.model_dump(mode="json"),
            "diagnostics": diagnostics,
            "flip_required": True,
            "reason": reason,
        }

    async def _approve_flip(self, signal: TradeSignal, actor: str, *, mid: float | None) -> dict[str, Any]:
        now = _now_ms()
        if signal.expires_at_ms <= now:
            updated = signal.model_copy(update={"status": "expired"})
            self.signals[updated.id] = updated
            await self._persist_signal(updated)
            raise RiskControlError("flip request expired")
        state = self.reducer.snapshot().assets.get(signal.symbol)
        ref_px = mid or (state.mid if state is not None else None) or signal.entry
        await self._persist_signal(signal)
        risk_decision = await self._check_risk_gateway(signal, ref_px=ref_px, asset_class="crypto")
        if not risk_decision.allowed:
            raise RiskControlError(f"risk gateway rejected signal: {risk_decision.violations}")
        order, fill, position = await self.portfolio.approve_signal(signal, actor, mid=ref_px, timestamp_ms=now)
        updated = signal.model_copy(update={"status": "paper_ordered"})
        self.signals[updated.id] = updated
        AUTONOMY_SIGNALS_APPROVED.inc()
        AUTONOMY_PAPER_ORDERS.labels(status="filled").inc()
        AUTONOMY_PAPER_FILLS.labels(symbol=fill.symbol).inc()
        await self._persist_signal(updated, approved_by=actor)
        if self.evaluation_service is not None:
            await self.evaluation_service.update_signal_status(updated, paper_position_id=position.id)
        await self._record_event(
            "signal_flip_approved_paper_ordered",
            actor=actor,
            symbol=signal.symbol,
            payload={
                "signal_id": signal.id,
                "order_id": order.id,
                "fill_id": fill.id,
                "position_id": position.id,
                "risk_gateway_decision_id": risk_decision.decision_id,
                "exchange_actions": [],
            },
        )
        return {
            "signal": updated.model_dump(mode="json"),
            "order": order.model_dump(mode="json"),
            "fill": fill.model_dump(mode="json"),
            "position": position.model_dump(mode="json"),
            "flip_required": False,
        }

    async def reject_signal(self, signal_id: str, actor: str, reason: str = "") -> TradeSignal:
        signal = await self._get_signal(signal_id)
        if signal is None:
            if signal_id in self.equity_signals:
                return await self.reject_equity_signal(signal_id, actor=actor, reason=reason)
            raise KeyError("signal not found")
        updated = signal.model_copy(update={"status": "rejected"})
        self.signals[updated.id] = updated
        AUTONOMY_SIGNALS_REJECTED.inc()
        await self._persist_signal(updated, rejected_by=actor)
        if self.evaluation_service is not None:
            await self.evaluation_service.update_signal_status(updated)
        await self._record_event("signal_rejected", actor=actor, symbol=signal.symbol, payload={"signal_id": signal.id, "reason": reason, "exchange_actions": []})
        return updated

    async def expire_signal(self, signal_id: str, actor: str = "api") -> TradeSignal:
        signal = await self._get_signal(signal_id)
        if signal is None:
            raise KeyError("signal not found")
        updated = signal.model_copy(update={"status": "expired"})
        self.signals[updated.id] = updated
        await self._persist_signal(updated)
        if self.evaluation_service is not None:
            await self.evaluation_service.update_signal_status(updated)
        await self._record_event("signal_expired", actor=actor, symbol=signal.symbol, payload={"signal_id": signal.id, "exchange_actions": []})
        return updated

    async def handle_discord_command(self, command: AutonomyCommand, *, user_id: str | None, role_ids: set[int]) -> str:
        mutating = command.action in {"approve", "reject", "pause", "resume", "approve_flip"}
        if mutating and not self._is_admin(user_id, role_ids):
            return "Not authorized for autonomy signoff/admin commands."
        try:
            if command.action == "approve" and command.signal_id:
                result = await self.approve_signal(command.signal_id, actor=user_id or "discord")
                if result.get("flip_required"):
                    closed = result.get("closed_position") or {}
                    diag = result.get("diagnostics") or {}
                    return (
                        f"Flip requested for `{command.signal_id}`. Closed opposing position `{closed.get('id', '-')[:8]}` "
                        f"with realized PnL `${float(closed.get('realized_pnl_usd', 0)):,.2f}`. "
                        f"Single-name exposure was `{diag.get('current_symbol_exposure_usd', 0):,.2f}` vs cap "
                        f"`{diag.get('max_single_name_exposure_pct', 0)}%` (${diag.get('max_single_name_exposure_usd', 0):,.2f}). "
                        f"Confirm the new side with `approve flip {command.signal_id}` or `reject signal {command.signal_id}`. "
                        f"No live trade was placed."
                    )
                order = result["order"]
                position = result["position"]
                return f"Approved `{command.signal_id}`. Paper order `{order['id'][:8]}` filled; position `{position['id'][:8]}` opened. No live trade was placed."
            if command.action == "approve_flip" and command.signal_id:
                result = await self.approve_signal(command.signal_id, actor=user_id or "discord")
                order = result["order"]
                position = result["position"]
                return f"Flip approved for `{command.signal_id}`. Paper order `{order['id'][:8]}` filled; position `{position['id'][:8]}` opened in the new side. No live trade was placed."
            if command.action == "reject" and command.signal_id:
                await self.reject_signal(command.signal_id, actor=user_id or "discord", reason="discord_reject")
                return f"Rejected `{command.signal_id}`. No paper order was created."
            if command.action == "pause":
                await self.pause(actor=user_id or "discord")
                return "Autonomy paused. Market tracking can continue, but no new signals will post."
            if command.action == "resume":
                await self.resume(actor=user_id or "discord")
                return "Autonomy resumed."
            if command.action == "signal" and command.signal_id:
                signal = await self._get_signal(command.signal_id) or self.equity_signals.get(command.signal_id)
                return format_signal_detail(signal) if signal else "Signal not found."
            if command.action == "signals":
                return format_signals(self.list_signals())
            if command.action == "portfolio":
                return format_portfolio_snapshot(self.portfolio.latest_snapshot())
            if command.action == "positions":
                return format_positions(list(self.portfolio.positions.values()))
            if command.action == "orders":
                return format_orders(list(self.portfolio.orders.values()))
            if command.action == "market_map":
                return format_market_map(self.reducer.snapshot())
            if command.action == "signal_outcome" and command.signal_id and self.evaluation_service is not None:
                evaluation = await self.evaluation_service.get_by_signal_id(command.signal_id)
                return format_signal_evaluation(evaluation.model_dump(mode="json") if evaluation else None)
            if command.action == "event_outcome" and command.signal_id and self.event_evaluation_service is not None:
                evaluations = await self.event_evaluation_service.get_by_event_id(command.signal_id)
                return format_event_evaluation([item.model_dump(mode="json") for item in evaluations] if evaluations else None)
            if command.action == "daily_report" and self.report_service is not None:
                report = await self.report_service.generate_daily(post=False)
                return report.summary
            if command.action == "weekly_report" and self.report_service is not None:
                report = await self.report_service.generate_weekly(post=False)
                return report.summary
            if command.action == "token_capital" and self.report_service is not None:
                latest = getattr(self.report_service, "latest_token_capital", None)
                if latest is None:
                    report = await self.report_service.generate_daily(post=False)
                    latest = report.token_capital
                return f"Token Capital: **{latest.total_score:.0f}/100**\nRisk-adjusted: `{latest.risk_adjusted_performance_score:.0f}` | Signal quality: `{latest.signal_quality_score:.0f}` | Memory: `{latest.memory_compounding_score:.0f}` | Risk: `{latest.risk_discipline_score:.0f}`\nNo strategy changes were applied."
            if command.action == "feedback_signal" and command.signal_id and self.memory_service is not None:
                feedback = OperatorFeedback(id=f"fb_{uuid4().hex}", source="discord", actor_id=user_id, target_type="signal", target_id=command.signal_id, rating=cast(Any, command.rating or "unclear"), note=command.note, created_at_ms=_now_ms(), metadata={"exchange_actions": []})
                await self.memory_service.record_feedback(feedback)
                return f"Feedback recorded for signal `{command.signal_id}` as `{feedback.rating}`. No strategy settings were changed."
            if command.action == "feedback_bot" and self.memory_service is not None:
                feedback = OperatorFeedback(id=f"fb_{uuid4().hex}", source="discord", actor_id=user_id, target_type="bot", target_id="discord_bot", rating=cast(Any, command.rating or "unclear"), note=command.note, created_at_ms=_now_ms(), metadata={"exchange_actions": []})
                await self.memory_service.record_feedback(feedback)
                return "Bot feedback recorded. It can become an operator-output lesson only after evidence-gated validation."
            if command.action == "memories" and self.memory_service is not None:
                items = await self.memory_service.list_lessons(role=command.role, status="active", include_shadow=False, limit=20)
                return format_memories(items, title=f"Active {command.role or 'role'} memories")
            if command.action == "memory" and command.lesson_id and self.memory_service is not None:
                item = await self.memory_service.get_lesson(command.lesson_id)
                return format_memories([item], title="Memory") if item else "Memory not found."
            if command.action == "tuning_proposals" and self.tuning_service is not None:
                items = await self.tuning_service.list(status=None, limit=20)
                return format_tuning_proposals(items)
            if command.action == "tuning_proposal" and command.proposal_id and self.tuning_service is not None:
                item = await self.tuning_service.get(command.proposal_id)
                return format_tuning_proposal(item)
            if command.action == "apply_tuning_proposal":
                return "Tuning proposals are observe-and-recommend only in this phase. Apply manually after review. No runtime strategy settings were changed."
        except (RiskControlError, EquityRiskControlError) as exc:
            return (
                f"Autonomy command blocked by risk control: {exc}. "
                f"Single-name exposure cap "
                f"crypto cap `{self.settings.autonomy_paper_max_single_name_exposure_pct}%` / `{self.settings.autonomy_paper_max_gross_leverage}x`, "
                f"equity cap `{self.settings.autonomy_equity_paper_max_single_name_exposure_pct}%` / `{self.settings.autonomy_equity_paper_max_gross_leverage}x`. No live trade was placed."
            )
        except Exception as exc:
            return f"Autonomy command failed: {type(exc).__name__}: {exc}. No live trade was placed."
        return "Unknown autonomy command."

    def list_signals(self, status: str | None = None) -> list[TradeSignal]:
        items = list(self.signals.values())
        if status:
            items = [item for item in items if item.status == status]
        return sorted(items, key=lambda item: item.created_at_ms, reverse=True)

    def list_equity_signals(self, status: str | None = None) -> list[TradeSignal]:
        items = list(self.equity_signals.values())
        if status:
            items = [item for item in items if item.status == status]
        return sorted(items, key=lambda item: item.created_at_ms, reverse=True)

    async def approve_equity_signal(self, signal_id: str, actor: str) -> dict[str, Any]:
        signal = self.equity_signals.get(signal_id)
        if signal is None:
            raise KeyError("equity signal not found")
        if self.equity_portfolio is None:
            raise EquityRiskControlError("equity paper portfolio is not configured")
        if signal.status not in {"candidate", "posted"}:
            raise EquityRiskControlError(f"equity signal status {signal.status} cannot be approved")
        now = _now_ms()
        if signal.expires_at_ms <= now:
            updated_expired = signal.model_copy(update={"status": "expired"})
            self.equity_signals[updated_expired.id] = updated_expired
            await self._persist_signal(updated_expired)
            if self.evaluation_service is not None:
                await self.evaluation_service.update_signal_status(updated_expired)
            raise EquityRiskControlError("equity signal is expired")
        request = EquityTradeRequest(
            symbol=signal.symbol,
            side=signal.side,
            entry=signal.entry,
            stop=signal.stop,
            take_profit=signal.take_profit,
            account_equity_usd=(self.equity_portfolio.snapshots[-1].equity_usd if self.equity_portfolio.snapshots else self.equity_portfolio.portfolio.equity_usd),
            risk_pct=self.settings.autonomy_equity_paper_risk_pct_per_trade,
            signal_id=signal.id,
            thesis=signal.thesis,
        )
        risk_decision = await self._check_risk_gateway(signal, ref_px=signal.entry, asset_class="equity")
        if not risk_decision.allowed:
            raise EquityRiskControlError(f"risk gateway rejected equity signal: {risk_decision.violations}")
        order = await self.equity_portfolio.place_order(request)
        fill = next((item for item in self.equity_portfolio.fills.values() if item.order_id == order.id), None)
        position = next((item for item in self.equity_portfolio.positions.values() if item.signal_id == signal.id and item.status == "open"), None)
        if position is None:
            position = next((item for item in self.equity_portfolio.positions.values() if item.symbol == signal.symbol and item.side == signal.side and item.status == "open"), None)
        snapshot = self.equity_portfolio.snapshot()
        updated = signal.model_copy(update={"status": "paper_ordered", "metadata": {**(signal.metadata or {}), "approved_by": actor, "asset_class": "equity"}})
        self.equity_signals[updated.id] = updated
        await self._persist_signal(updated, approved_by=actor)
        if self.evaluation_service is not None:
            await self.evaluation_service.update_signal_status(updated, paper_position_id=position.id if position else None)
        await self._record_event(
            "equity_signal_approved_paper_ordered",
            actor=actor,
            symbol=signal.symbol,
            payload={"signal_id": signal.id, "order_id": order.id, "position_id": position.id if position else None, "risk_gateway_decision_id": risk_decision.decision_id, "exchange_actions": []},
        )
        return {
            "signal": updated.model_dump(mode="json"),
            "order": order.model_dump(mode="json"),
            "fill": fill.model_dump(mode="json") if fill else None,
            "position": position.model_dump(mode="json") if position else None,
            "snapshot": snapshot.model_dump(mode="json"),
        }

    async def reject_equity_signal(self, signal_id: str, actor: str, reason: str = "") -> TradeSignal:
        signal = self.equity_signals.get(signal_id)
        if signal is None:
            raise KeyError("equity signal not found")
        updated = signal.model_copy(update={"status": "rejected", "metadata": {**(signal.metadata or {}), "rejected_by": actor, "reject_reason": reason, "asset_class": "equity"}})
        self.equity_signals[updated.id] = updated
        await self._persist_signal(updated, rejected_by=actor)
        if self.evaluation_service is not None:
            await self.evaluation_service.update_signal_status(updated)
        await self._record_event("equity_signal_rejected", actor=actor, symbol=signal.symbol, payload={"signal_id": signal.id, "reason": reason, "exchange_actions": []})
        return updated

    async def _run(self) -> None:
        while self.running:
            try:
                await asyncio.sleep(max(1, self.settings.autonomy_loop_interval_seconds))
                if self.paused:
                    continue
                await self._run_iteration()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - loop resilience
                self.last_error = type(exc).__name__
                log.warning("autonomy_loop_iteration_failed", error=type(exc).__name__)
                await self._record_event("autonomy_loop_error", payload={"error": type(exc).__name__, "exchange_actions": []})

    async def _run_iteration(self) -> None:
        ts = _now_ms()
        if self.settings.engine_enabled and self.engine_service is not None:
            await self.engine_service.run_once(symbols=self.settings.autonomy_core_symbols)
            if not self.settings.autonomy_signals_run_with_engine_enabled:
                self.last_iteration_at_ms = ts
                AUTONOMY_LOOP_ITERATIONS.labels(result="ok").inc()
                return
        await self._ensure_universe(ts)
        mids = await self._current_mids()
        if mids:
            self.reducer.apply_all_mids(mids, timestamp_ms=ts)
            numeric_mids = {key.upper(): float(value) for key, value in mids.items() if _float(value) is not None}
            await self.portfolio.mark_to_market(numeric_mids, timestamp_ms=ts)
            if self.evaluation_service is not None:
                for symbol, price in numeric_mids.items():
                    await self.evaluation_service.on_price(symbol, price, ts)
            if self.event_evaluation_service is not None:
                for symbol, price in numeric_mids.items():
                    await self.event_evaluation_service.on_price(symbol, "crypto", price, ts)
            self.last_market_data_at_ms = ts
        if ts - self._last_deep_scan_ms >= self.settings.autonomy_deep_scan_interval_seconds * 1000:
            await self._deep_market_scan(ts)
            self._last_deep_scan_ms = ts
        if not self.settings.newswire_enabled and ts - self._last_news_ms >= self.settings.autonomy_news_refresh_seconds * 1000:
            # When the free-standing Newswire is enabled, AgentNewsConsumer push-feeds the
            # reducer instead; this in-loop poll is the fallback when it is disabled.
            events = await self.newswire.poll([asset.symbol for asset in self.universe])
            self.reducer.apply_news(events, timestamp_ms=ts)
            for event in events:
                self.news_events[event.id] = event
                NEWSWIRE_EVENTS.labels(provider=event.provider).inc()
                if self.repository is not None and getattr(self.repository, "enabled", False):
                    await self.repository.record_news_event(event.model_dump(mode="json"))
                if self.world_model_service is not None and callable(getattr(self.world_model_service, "observe_news_event", None)):
                    await self.world_model_service.observe_news_event(event)
                if self.event_evaluation_service is not None:
                    await self.event_evaluation_service.create_for_news_event(event, market_regime=self.reducer.snapshot().risk_regime)
            self._last_news_ms = ts
        self.reducer.apply_paper_positions(self.portfolio.open_positions(), timestamp_ms=ts)
        await self._persist_market_observations()
        await self._generate_and_post_signals(ts)
        if self.settings.autonomy_equity_effective_enabled and self.tradfi is not None:
            await self._run_equity_iteration(ts)
        if self.evaluation_service is not None:
            await self.evaluation_service.mark_due(ts)
            await self.evaluation_service.expire_overdue_signals(ts)
        if self.event_evaluation_service is not None:
            await self.event_evaluation_service.mark_due(ts)
            await self.event_evaluation_service.expire_overdue_events(ts)
        if self.memory_service is not None:
            await self.memory_service.archive_expired(now_ms=ts)
        if self.report_service is not None:
            await self.report_service.maybe_run_scheduled(ts)
        if ts - self._last_portfolio_snapshot_ms >= self.settings.autonomy_portfolio_snapshot_seconds * 1000:
            snapshot = await self.portfolio.snapshot(ts)
            AUTONOMY_PORTFOLIO_EQUITY.set(snapshot.equity_usd)
            AUTONOMY_PORTFOLIO_DRAWDOWN.set(snapshot.drawdown_pct)
            self._last_portfolio_snapshot_ms = ts
        self.last_iteration_at_ms = ts
        AUTONOMY_LOOP_ITERATIONS.labels(result="ok").inc()

    async def _run_equity_iteration(self, timestamp_ms: int) -> None:
        if self.tradfi is None or self.equity_signal_generator is None or self.equity_portfolio is None:
            return
        min_interval_ms = max(5, self.settings.autonomy_equity_loop_interval_seconds) * 1000
        if self._last_equity_scan_ms and timestamp_ms - self._last_equity_scan_ms < min_interval_ms:
            return
        symbols = self.settings.autonomy_equity_symbols[: self.settings.autonomy_equity_max_tracked_assets]
        if not symbols:
            return
        self._expire_equity_signals(timestamp_ms)
        try:
            snapshots = await self.tradfi.get_snapshots(symbols)
        except Exception as exc:
            self.last_error = type(exc).__name__
            log.warning("equity_snapshot_scan_failed", error=type(exc).__name__)
            return
        try:
            corporate_actions = await self.tradfi.get_corporate_actions(symbols)
        except Exception as exc:
            corporate_actions = {}
            log.warning("equity_corp_actions_scan_failed", error=type(exc).__name__)
        for symbol in symbols:
            snap = snapshots.get(symbol.upper())
            if snap is None:
                continue
            equity_px = _equity_snapshot_price(snap)
            if equity_px is not None:
                if self.evaluation_service is not None:
                    await self.evaluation_service.on_price(symbol.upper(), equity_px, timestamp_ms)
                if self.event_evaluation_service is not None:
                    await self.event_evaluation_service.on_price(symbol.upper(), "equity", equity_px, timestamp_ms)
            flow_events: list[Any] = []
            if self.settings.options_flow_effective_enabled and self.options_flow is not None:
                try:
                    chain = await self.tradfi.get_options_chain(symbol)
                    flow_events = self.options_flow.detect(chain)
                    if self.flow_enricher is not None:
                        for event in flow_events[:2]:
                            enrichment = await self.flow_enricher.maybe_enrich(event)
                            if enrichment:
                                event.enrichment = enrichment
                    await self._persist_equity_flow_events(flow_events)
                except Exception as exc:
                    log.warning("equity_options_flow_scan_failed", symbol=symbol, error=type(exc).__name__)
            candidates = self.equity_signal_generator.generate_from_snapshot(
                symbol.upper(),
                snap,
                corporate_actions=[item.model_dump(mode="json") for item in corporate_actions.get(symbol.upper(), [])],
                flow_events=flow_events,
                signals_today=self.equity_signals_today(),
                timestamp_ms=timestamp_ms,
            )
            for signal in candidates:
                if self._is_duplicate_equity_signal(signal, timestamp_ms):
                    continue
                signal = signal.model_copy(update={"metadata": {**(signal.metadata or {}), "asset_class": "equity", "paper_only": True}})
                signal = await self._attach_decision_context(signal, source_type="equity_signal", timestamp_ms=timestamp_ms)
                self.equity_signals[signal.id] = signal
                AUTONOMY_SIGNALS_CREATED.labels(signal_type=signal.signal_type).inc()
                await self._persist_signal(signal)
                if self.evaluation_service is not None:
                    await self.evaluation_service.create_for_signal(signal, market_regime="equity")
                await self._record_event("equity_signal_created", symbol=signal.symbol, payload={"signal_id": signal.id, "score": signal.score, "exchange_actions": []})
                await self._post_equity_signal(signal)
        try:
            await self.equity_portfolio.update_marks()
            self.equity_portfolio.snapshot()
        except Exception as exc:
            log.warning("equity_portfolio_mark_failed", error=type(exc).__name__)
        self._last_equity_scan_ms = timestamp_ms

    def _expire_equity_signals(self, timestamp_ms: int) -> None:
        for signal in list(self.equity_signals.values()):
            if signal.status in {"candidate", "posted"} and signal.expires_at_ms <= timestamp_ms:
                self.equity_signals[signal.id] = signal.model_copy(update={"status": "expired"})

    def _is_duplicate_equity_signal(self, signal: TradeSignal, timestamp_ms: int) -> bool:
        for existing in self.equity_signals.values():
            if existing.status not in {"candidate", "posted", "paper_ordered"}:
                continue
            if existing.expires_at_ms <= timestamp_ms:
                continue
            if existing.symbol == signal.symbol and existing.side == signal.side and existing.signal_type == signal.signal_type:
                return True
        return False

    async def _post_equity_signal(self, signal: TradeSignal) -> None:
        if not self.settings.autonomy_alert_channel_configured or self.alert_sink is None:
            return
        message_id = await self.alert_sink.send(self.settings.autonomy_alert_channel_id, format_signal_alert(signal))
        posted = signal.model_copy(update={"status": "posted", "discord_channel_id": self.settings.autonomy_alert_channel_id, "discord_message_id": message_id})
        self.equity_signals[posted.id] = posted
        AUTONOMY_SIGNALS_POSTED.inc()
        await self._persist_signal(posted)
        if self.evaluation_service is not None:
            await self.evaluation_service.update_signal_status(posted)
        await self._record_event("equity_signal_posted", symbol=posted.symbol, payload={"signal_id": posted.id, "discord_message_id": message_id, "exchange_actions": []})

    async def _persist_equity_flow_events(self, events: list[Any]) -> None:
        if self.repository is None or not getattr(self.repository, "enabled", False):
            return
        for event in events:
            data = event.model_dump(mode="json") if callable(getattr(event, "model_dump", None)) else dict(event)
            data.setdefault("id", f"flow_{uuid4().hex}")
            await self.repository.record_equity_options_flow_event(data)

    async def _ensure_universe(self, timestamp_ms: int) -> None:
        if self.universe and timestamp_ms - self._last_deep_scan_ms < self.settings.autonomy_deep_scan_interval_seconds * 1000:
            return
        assets = await self.universe_resolver.resolve()
        if assets:
            self.universe = assets
            self.reducer.set_universe(assets, timestamp_ms=timestamp_ms)
            if self.repository is not None and getattr(self.repository, "enabled", False):
                for asset in assets:
                    await self.repository.upsert_market_asset(asset.model_dump(mode="json"))

    async def _current_mids(self) -> dict[str, str | float]:
        cached = getattr(getattr(self.ws_worker, "cache", None), "all_mids", {}) if self.ws_worker is not None else {}
        if cached:
            return {str(key).upper(): value for key, value in cached.items()}
        try:
            return {str(key).upper(): value for key, value in (await self.hyperliquid.all_mids()).items()}
        except Exception as exc:
            self.last_error = type(exc).__name__
            return {}

    async def _deep_market_scan(self, timestamp_ms: int) -> None:
        try:
            self.reducer.apply_asset_contexts(await self.hyperliquid.meta_and_asset_ctxs(), timestamp_ms=timestamp_ms)
        except Exception as exc:
            self.last_error = type(exc).__name__
        self.hot_l2_assets = self._select_hot_assets()
        for symbol in self.hot_l2_assets:
            try:
                state = self.reducer.snapshot().assets.get(symbol)
                self.reducer.apply_l2_book(symbol, await self.hyperliquid.l2_book(symbol), timestamp_ms=timestamp_ms)
                end = timestamp_ms
                start = end - 48 * 60 * 60 * 1000
                candles = await self.hyperliquid.candle_snapshot(symbol, "1h", start, end)
                if isinstance(candles, list):
                    self.reducer.apply_candles(symbol, candles, "1h", timestamp_ms=timestamp_ms)
                if state is not None:
                    AUTONOMY_MARKET_OBSERVATIONS.labels(symbol=symbol).inc()
            except Exception as exc:
                self.last_error = type(exc).__name__
                continue

    def _select_hot_assets(self) -> list[str]:
        selected: list[str] = []
        for position in self.portfolio.open_positions():
            selected.append(position.symbol)
        for signal in self.list_signals():
            if signal.status in {"candidate", "posted", "approved", "paper_ordered"}:
                selected.append(signal.symbol)
        selected.extend(self.settings.autonomy_core_symbols)
        selected.extend(asset.symbol for asset in self.universe)
        out: list[str] = []
        for symbol in selected:
            symbol = symbol.upper()
            if symbol not in out:
                out.append(symbol)
            if len(out) >= self.settings.autonomy_max_hot_l2_assets:
                break
        return out

    async def _generate_and_post_signals(self, timestamp_ms: int) -> None:
        signals = self.signal_engine.generate(
            self.reducer.snapshot(),
            existing_signals=list(self.signals.values()),
            open_positions=self.portfolio.open_positions(),
            signals_today=self.signals_today(),
            timestamp_ms=timestamp_ms,
        )
        for signal in signals:
            enriched = signal
            if self._can_call_model_insight(signal):
                AUTONOMY_MODEL_INSIGHT_CALLS.labels(result="attempt").inc()
                enriched = await maybe_attach_model_insight(signal, self.model_gateway, self.settings, memory_service=self.memory_service)
                self._model_call_timestamps.append(time.monotonic())
                AUTONOMY_MODEL_INSIGHT_CALLS.labels(result="ok" if enriched.model_insight and enriched.model_insight.get("status") != "unavailable" else "fallback").inc()
            enriched = await self._attach_decision_context(enriched, source_type="autonomy_signal", timestamp_ms=timestamp_ms)
            enriched = self._attach_world_model_snapshot(enriched)
            self.signals[enriched.id] = enriched
            AUTONOMY_SIGNALS_CREATED.labels(signal_type=enriched.signal_type).inc()
            await self._persist_signal(enriched)
            if self.evaluation_service is not None:
                await self.evaluation_service.create_for_signal(enriched, market_regime=self.reducer.snapshot().risk_regime)
            if self.event_evaluation_service is not None:
                for event_id in enriched.metadata.get("source_event_ids", [])[:5]:
                    await self.event_evaluation_service.link_signal(event_id, enriched.id, enriched.symbol)
            await self._record_event("signal_created", symbol=enriched.symbol, payload={"signal_id": enriched.id, "score": enriched.score, "source_event_ids": enriched.metadata.get("source_event_ids", []), "exchange_actions": []})
            await self._post_signal(enriched)

    def _can_call_model_insight(self, signal: TradeSignal) -> bool:
        if self.model_gateway is None or not self.settings.autonomy_model_insights_enabled:
            return False
        if signal.score < self.settings.autonomy_model_insight_min_score:
            return False
        cutoff = time.monotonic() - 3600
        self._model_call_timestamps = [item for item in self._model_call_timestamps if item >= cutoff]
        return len(self._model_call_timestamps) < self.settings.autonomy_model_max_calls_per_hour

    async def _post_signal(self, signal: TradeSignal) -> None:
        if not self.settings.autonomy_alert_channel_configured or self.alert_sink is None:
            return
        message_id = await self.alert_sink.send(self.settings.autonomy_alert_channel_id, format_signal_alert(signal))
        posted = signal.model_copy(update={"status": "posted", "discord_channel_id": self.settings.autonomy_alert_channel_id, "discord_message_id": message_id})
        self.signals[posted.id] = posted
        AUTONOMY_SIGNALS_POSTED.inc()
        await self._persist_signal(posted)
        if self.evaluation_service is not None:
            await self.evaluation_service.update_signal_status(posted)
        await self._record_event("signal_posted", symbol=posted.symbol, payload={"signal_id": posted.id, "discord_message_id": message_id, "exchange_actions": []})

    async def _on_all_mids(self, message: dict[str, Any]) -> None:
        data = message.get("data") if isinstance(message, dict) else {}
        mids = data.get("mids", data) if isinstance(data, dict) else {}
        if not isinstance(mids, dict):
            return
        ts = _now_ms()
        self.reducer.apply_all_mids({str(key).upper(): value for key, value in mids.items()}, timestamp_ms=ts)
        self.last_market_data_at_ms = ts
        numeric_mids = {str(key).upper(): float(value) for key, value in mids.items() if _float(value) is not None}
        await self.portfolio.mark_to_market(numeric_mids, timestamp_ms=ts)
        if self.evaluation_service is not None:
            for symbol, price in numeric_mids.items():
                await self.evaluation_service.on_price(symbol, price, ts)
        if self.event_evaluation_service is not None:
            for symbol, price in numeric_mids.items():
                await self.event_evaluation_service.on_price(symbol, "crypto", price, ts)

    async def _persist_market_observations(self) -> None:
        if self.repository is None or not getattr(self.repository, "enabled", False):
            return
        for state in self.reducer.snapshot().assets.values():
            await self.repository.record_market_observation(state.model_dump(mode="json"))
            levels = [*state.support_levels, *state.resistance_levels, *state.liquidity_levels]
            if levels:
                await self.repository.upsert_market_levels([item.model_dump(mode="json") for item in levels])

    async def _check_risk_gateway(self, signal: TradeSignal, *, ref_px: float | None, asset_class: str):
        portfolio_snapshot: dict[str, Any] = {}
        if asset_class == "equity" and self.equity_portfolio is not None:
            snapshot = self.equity_portfolio.snapshots[-1] if self.equity_portfolio.snapshots else None
            portfolio_snapshot = snapshot.model_dump(mode="json") if snapshot is not None else self.equity_portfolio.portfolio.model_dump(mode="json")
        else:
            snapshot = self.portfolio.latest_snapshot()
            portfolio_snapshot = snapshot.model_dump(mode="json") if snapshot is not None else self.portfolio.portfolio.model_dump(mode="json") if self.portfolio.portfolio is not None else {}
        state = self.reducer.snapshot().assets.get(signal.symbol)
        market_snapshot = {
            "ref_px": ref_px,
            "last_market_data_at_ms": self.last_market_data_at_ms,
            "asset_timestamp_ms": state.timestamp_ms if state is not None else None,
            "mid": state.mid if state is not None else None,
        }
        return await self.risk_gateway.check_signal(
            signal,
            mode="paper",
            ref_px=ref_px,
            asset_class=asset_class,
            portfolio_snapshot=portfolio_snapshot,
            market_snapshot=market_snapshot,
        )

    async def _attach_decision_context(
        self,
        signal: TradeSignal,
        *,
        source_type: str,
        timestamp_ms: int | None = None,
    ) -> TradeSignal:
        if signal.metadata.get("decision_context"):
            return signal
        recorder = self.decision_context_recorder
        if recorder is None:
            return signal
        context = recorder.new_decision_context(
            source_type=source_type,
            source_id=signal.id,
            market_snapshot_refs=[f"market_map:{timestamp_ms or signal.created_at_ms}"],
            data_freshness={
                "last_market_data_at_ms": self.last_market_data_at_ms,
                "last_iteration_at_ms": self.last_iteration_at_ms,
                "signal_created_at_ms": signal.created_at_ms,
            },
            metadata={
                "symbol": signal.symbol,
                "side": signal.side,
                "signal_type": signal.signal_type,
                "asset_class": signal.metadata.get("asset_class", "crypto"),
                "paper_only": True,
            },
        )
        await recorder.record_decision_context(context, source_type=source_type, source_id=signal.id)
        return signal.model_copy(update={"metadata": {**signal.metadata, "decision_context": context.model_dump(mode="json")}})

    def _attach_world_model_snapshot(self, signal: TradeSignal) -> TradeSignal:
        if self.world_model_service is None or signal.metadata.get("world_model_snapshot"):
            return signal
        snapshot = getattr(self.world_model_service, "snapshot", None)
        if not callable(snapshot):
            return signal
        try:
            world_snapshot = snapshot(symbols=[signal.symbol], max_beliefs=8)
        except Exception:
            return signal
        return signal.model_copy(
            update={
                "metadata": {
                    **signal.metadata,
                    "world_model_snapshot": world_snapshot.model_dump(mode="json"),
                    "world_model_snapshot_id": world_snapshot.snapshot_id,
                }
            }
        )

    async def _persist_signal(self, signal: TradeSignal, approved_by: str | None = None, rejected_by: str | None = None) -> None:
        if self.repository is None or not getattr(self.repository, "enabled", False):
            return
        await self.repository.create_or_update_trade_signal(signal.model_dump(mode="json"), approved_by=approved_by, rejected_by=rejected_by)

    async def _get_signal(self, signal_id: str) -> TradeSignal | None:
        signal = self.signals.get(signal_id)
        if signal is not None:
            return signal
        if self.repository is not None and getattr(self.repository, "enabled", False):
            data = await self.repository.get_autonomy_trade_signal(signal_id)
            if data:
                signal = TradeSignal(**data)
                self.signals[signal.id] = signal
                return signal
        return None

    async def _record_event(self, event_type: str, actor: str = "autonomy", symbol: str | None = None, payload: dict[str, Any] | None = None) -> None:
        if self.repository is None or not getattr(self.repository, "enabled", False):
            return
        await self.repository.record_autonomy_event(event_type=event_type, actor=actor, symbol=symbol, payload=payload or {})

    def _is_admin(self, user_id: str | None, role_ids: set[int]) -> bool:
        if user_id:
            try:
                if int(user_id) in self.settings.autonomy_admin_users:
                    return True
            except ValueError:
                pass
        return bool(role_ids & self.settings.autonomy_admin_roles)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _equity_snapshot_price(snapshot: Any) -> float | None:
    trade = getattr(snapshot, "latest_trade", None)
    if trade is not None and getattr(trade, "price", None) is not None:
        return _float(getattr(trade, "price"))
    quote = getattr(snapshot, "latest_quote", None)
    if quote is not None:
        bid = _float(getattr(quote, "bid_price", None))
        ask = _float(getattr(quote, "ask_price", None))
        if bid and ask:
            return (bid + ask) / 2
    bar = getattr(snapshot, "daily_bar", None)
    if bar is not None and getattr(bar, "close", None) is not None:
        return _float(getattr(bar, "close"))
    return None
