from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from hyperliquid_trading_agent.app.agent.model_gateway import ModelGateway
from hyperliquid_trading_agent.app.autonomy.discord import (
    AutonomyAlertSink,
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
        memory_service: Any | None = None,
        report_service: Any | None = None,
        tuning_service: Any | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.hyperliquid = hyperliquid
        self.news = news
        self.ws_worker = ws_worker
        self.model_gateway = model_gateway
        self.alert_sink = alert_sink
        self.evaluation_service = evaluation_service
        self.memory_service = memory_service
        self.report_service = report_service
        self.tuning_service = tuning_service
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
        self._task: asyncio.Task | None = None
        self._subscription_id: str | None = None
        self._last_deep_scan_ms = 0
        self._last_news_ms = 0
        self._last_portfolio_snapshot_ms = 0
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
            "paper_portfolio_id": self.portfolio.portfolio.id if self.portfolio.portfolio else None,
            "portfolio_equity_usd": snapshot.equity_usd if snapshot else None,
            "evaluation": self.evaluation_service.status() if self.evaluation_service is not None and callable(getattr(self.evaluation_service, "status", None)) else {},
            "memory": self.memory_service.status() if self.memory_service is not None and callable(getattr(self.memory_service, "status", None)) else {},
            "reports": self.report_service.status() if self.report_service is not None and callable(getattr(self.report_service, "status", None)) else {},
            "tuning_proposals": self.tuning_service.status() if self.tuning_service is not None and callable(getattr(self.tuning_service, "status", None)) else {},
            "warnings": [*self.settings.autonomy_config_warnings(), *self.universe_resolver.warnings],
        }

    def signals_today(self) -> int:
        today = datetime.now(UTC).date()
        return sum(1 for signal in self.signals.values() if datetime.fromtimestamp(signal.created_at_ms / 1000, tz=UTC).date() == today)

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
            raise KeyError("signal not found")
        if signal.status not in {"candidate", "posted"}:
            raise RiskControlError(f"signal status {signal.status} cannot be approved")
        now = _now_ms()
        if signal.expires_at_ms <= now:
            signal = signal.model_copy(update={"status": "expired"})
            self.signals[signal.id] = signal
            await self._persist_signal(signal)
            raise RiskControlError("signal is expired")
        state = self.reducer.snapshot().assets.get(signal.symbol)
        ref_px = mid or (state.mid if state is not None else None) or signal.entry
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
            "signal_approved_paper_ordered",
            actor=actor,
            symbol=signal.symbol,
            payload={"signal_id": signal.id, "order_id": order.id, "fill_id": fill.id, "position_id": position.id, "exchange_actions": []},
        )
        return {"signal": updated.model_dump(mode="json"), "order": order.model_dump(mode="json"), "fill": fill.model_dump(mode="json"), "position": position.model_dump(mode="json")}

    async def reject_signal(self, signal_id: str, actor: str, reason: str = "") -> TradeSignal:
        signal = await self._get_signal(signal_id)
        if signal is None:
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
        mutating = command.action in {"approve", "reject", "pause", "resume"}
        if mutating and not self._is_admin(user_id, role_ids):
            return "Not authorized for autonomy signoff/admin commands."
        try:
            if command.action == "approve" and command.signal_id:
                result = await self.approve_signal(command.signal_id, actor=user_id or "discord")
                order = result["order"]
                position = result["position"]
                return f"Approved `{command.signal_id}`. Paper order `{order['id'][:8]}` filled; position `{position['id'][:8]}` opened. No live trade was placed."
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
                signal = await self._get_signal(command.signal_id)
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
        except Exception as exc:
            return f"Autonomy command failed: {type(exc).__name__}. No live trade was placed."
        return "Unknown autonomy command."

    def list_signals(self, status: str | None = None) -> list[TradeSignal]:
        items = list(self.signals.values())
        if status:
            items = [item for item in items if item.status == status]
        return sorted(items, key=lambda item: item.created_at_ms, reverse=True)

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
        await self._ensure_universe(ts)
        mids = await self._current_mids()
        if mids:
            self.reducer.apply_all_mids(mids, timestamp_ms=ts)
            numeric_mids = {key.upper(): float(value) for key, value in mids.items() if _float(value) is not None}
            await self.portfolio.mark_to_market(numeric_mids, timestamp_ms=ts)
            if self.evaluation_service is not None:
                for symbol, price in numeric_mids.items():
                    await self.evaluation_service.on_price(symbol, price, ts)
            self.last_market_data_at_ms = ts
        if ts - self._last_deep_scan_ms >= self.settings.autonomy_deep_scan_interval_seconds * 1000:
            await self._deep_market_scan(ts)
            self._last_deep_scan_ms = ts
        if ts - self._last_news_ms >= self.settings.autonomy_news_refresh_seconds * 1000:
            events = await self.newswire.poll([asset.symbol for asset in self.universe])
            self.reducer.apply_news(events, timestamp_ms=ts)
            for event in events:
                self.news_events[event.id] = event
                NEWSWIRE_EVENTS.labels(provider=event.provider).inc()
                if self.repository is not None and getattr(self.repository, "enabled", False):
                    await self.repository.record_news_event(event.model_dump(mode="json"))
            self._last_news_ms = ts
        self.reducer.apply_paper_positions(self.portfolio.open_positions(), timestamp_ms=ts)
        await self._persist_market_observations()
        await self._generate_and_post_signals(ts)
        if self.evaluation_service is not None:
            await self.evaluation_service.mark_due(ts)
            await self.evaluation_service.expire_overdue_signals(ts)
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
            self.signals[enriched.id] = enriched
            AUTONOMY_SIGNALS_CREATED.labels(signal_type=enriched.signal_type).inc()
            await self._persist_signal(enriched)
            if self.evaluation_service is not None:
                await self.evaluation_service.create_for_signal(enriched, market_regime=self.reducer.snapshot().risk_regime)
            await self._record_event("signal_created", symbol=enriched.symbol, payload={"signal_id": enriched.id, "score": enriched.score, "exchange_actions": []})
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

    async def _persist_market_observations(self) -> None:
        if self.repository is None or not getattr(self.repository, "enabled", False):
            return
        for state in self.reducer.snapshot().assets.values():
            await self.repository.record_market_observation(state.model_dump(mode="json"))
            levels = [*state.support_levels, *state.resistance_levels, *state.liquidity_levels]
            if levels:
                await self.repository.upsert_market_levels([item.model_dump(mode="json") for item in levels])

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
