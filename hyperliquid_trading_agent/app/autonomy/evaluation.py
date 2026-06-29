from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

from hyperliquid_trading_agent.app.autonomy.schemas import SignalEvaluation, SignalEvaluationMark, TradeSignal
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.metrics import (
    SIGNAL_EVALUATION_ERRORS,
    SIGNAL_EVALUATION_MARKS_COMPLETED,
    SIGNAL_EVALUATIONS_CREATED,
    SIGNAL_OUTCOMES_BY_TYPE,
)

log = get_logger(__name__)

FRESH_MARK_PRICE_MS = 5 * 60 * 1000
HORIZON_MS = {
    "5m": 5 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "24h": 24 * 60 * 60 * 1000,
    "72h": 72 * 60 * 60 * 1000,
}


class SignalEvaluationService:
    """Deterministic signal outcome tracker.

    This service never calls models and never executes orders. It attributes the
    market path after each signal so reports/memory can learn from posted,
    approved, rejected, and expired ideas.
    """

    def __init__(self, *, settings: Settings, repository: Any = None, memory_service: Any | None = None, world_model_service: Any | None = None):
        self.settings = settings
        self.repository = repository
        self.memory_service = memory_service
        self.world_model_service = world_model_service
        self.evaluations: dict[str, SignalEvaluation] = {}
        self.marks: dict[str, SignalEvaluationMark] = {}
        self.last_mark_at_ms: int | None = None
        self.error_count = 0
        self.last_error: str | None = None

    async def load_open(self) -> None:
        if not self._repo_enabled():
            return
        try:
            for item in await self.repository.list_open_signal_evaluations(limit=self.settings.autonomy_eval_max_open_signals):
                evaluation = SignalEvaluation(**item)
                self.evaluations[evaluation.signal_id] = evaluation
                for mark in evaluation.marks:
                    self.marks[mark.id] = mark
        except Exception as exc:  # pragma: no cover - startup resilience
            self._record_error(exc)

    def status(self) -> dict[str, Any]:
        open_evals = [item for item in self.evaluations.values() if item.status in {"open", "partial"}]
        pending_marks = [item for item in self.marks.values() if item.status == "pending"]
        return {
            "enabled": self.settings.autonomy_evaluation_enabled,
            "effective_enabled": self.settings.autonomy_evaluation_effective_enabled,
            "open_evaluations": len(open_evals),
            "pending_marks": len(pending_marks),
            "last_mark_at_ms": self.last_mark_at_ms,
            "error_count": self.error_count,
            "last_error": self.last_error,
        }

    async def create_for_signal(self, signal: TradeSignal, *, market_regime: str = "unknown") -> SignalEvaluation | None:
        if not self.settings.autonomy_evaluation_enabled:
            return None
        existing = self.evaluations.get(signal.id)
        if existing is not None:
            return existing
        if self._repo_enabled():
            stored = await self.repository.get_signal_evaluation_by_signal_id(signal.id)
            if stored:
                evaluation = SignalEvaluation(**stored)
                self.evaluations[evaluation.signal_id] = evaluation
                for mark in evaluation.marks:
                    self.marks[mark.id] = mark
                return evaluation
        evaluation = SignalEvaluation(
            id=f"eval_{uuid4().hex}",
            signal_id=signal.id,
            symbol=signal.symbol.upper(),
            side=signal.side,
            signal_type=signal.signal_type,
            status="open",
            created_at_ms=signal.created_at_ms,
            entry=signal.entry,
            stop=signal.stop,
            take_profit=signal.take_profit,
            signal_score=signal.score,
            signal_confidence=signal.confidence,
            signal_status_at_eval_start=signal.status,
            feature_snapshot=signal.feature_snapshot,
            evidence_snapshot=[item.model_dump(mode="json") for item in signal.evidence],
            market_regime=market_regime,
            approved=signal.status in {"approved", "paper_ordered"},
            rejected=signal.status == "rejected",
            paper_ordered=signal.status == "paper_ordered",
            metadata={"exchange_actions": []},
        )
        marks = self._build_marks(evaluation, signal)
        evaluation.marks = marks
        self.evaluations[signal.id] = evaluation
        for mark in marks:
            self.marks[mark.id] = mark
        await self._persist(evaluation)
        await self._persist_marks(marks)
        SIGNAL_EVALUATIONS_CREATED.labels(symbol=evaluation.symbol, signal_type=evaluation.signal_type).inc()
        await self._record_event("signal_evaluation_created", signal.symbol, {"signal_id": signal.id, "evaluation_id": evaluation.id, "exchange_actions": []})
        return evaluation

    async def update_signal_status(self, signal: TradeSignal, *, paper_position_id: str | None = None) -> None:
        evaluation = await self.get_by_signal_id(signal.id)
        if evaluation is None:
            return
        evaluation.approved = signal.status in {"approved", "paper_ordered"} or evaluation.approved
        evaluation.rejected = signal.status == "rejected" or evaluation.rejected
        evaluation.paper_ordered = signal.status == "paper_ordered" or evaluation.paper_ordered
        evaluation.paper_position_id = paper_position_id or evaluation.paper_position_id
        evaluation.metadata = {**evaluation.metadata, "latest_signal_status": signal.status, "exchange_actions": []}
        await self._persist(evaluation)

    async def on_price(self, symbol: str, price: float, timestamp_ms: int) -> None:
        if not self.settings.autonomy_evaluation_enabled or price <= 0:
            return
        symbol = symbol.upper()
        evaluations = [item for item in self.evaluations.values() if item.symbol == symbol and item.status in {"open", "partial"}]
        if not evaluations and self._repo_enabled():
            for item in await self.repository.list_open_signal_evaluations(symbol=symbol, limit=self.settings.autonomy_eval_max_open_signals):
                evaluation = SignalEvaluation(**item)
                self.evaluations[evaluation.signal_id] = evaluation
                for mark in evaluation.marks:
                    self.marks[mark.id] = mark
                evaluations.append(evaluation)
        for evaluation in evaluations:
            updated = self._update_path(evaluation, price, timestamp_ms)
            self.evaluations[updated.signal_id] = updated
            await self._persist(updated)

    async def mark_due(self, now_ms: int | None = None) -> list[SignalEvaluationMark]:
        now_ms = now_ms or _now_ms()
        due = [item for item in self.marks.values() if item.status == "pending" and item.due_at_ms <= now_ms]
        if self._repo_enabled():
            for item in await self.repository.list_due_signal_evaluation_marks(now_ms, limit=500):
                mark = SignalEvaluationMark(**item)
                if mark.id not in self.marks:
                    self.marks[mark.id] = mark
                    due.append(mark)
        marked: list[SignalEvaluationMark] = []
        for mark in sorted(due, key=lambda item: item.due_at_ms):
            evaluation = await self.get_by_signal_id(mark.signal_id)
            if evaluation is None:
                continue
            updated = self._mark_horizon(evaluation, mark, now_ms)
            self.marks[updated.id] = updated
            marked.append(updated)
            await self._persist_mark(updated)
            SIGNAL_EVALUATION_MARKS_COMPLETED.labels(symbol=updated.symbol, horizon=updated.horizon, status=updated.status).inc()
            evaluation = self._maybe_complete(evaluation, now_ms)
            self.evaluations[evaluation.signal_id] = evaluation
            await self._persist(evaluation)
            if evaluation.status == "complete":
                await self._on_completed(evaluation)
        if marked:
            self.last_mark_at_ms = now_ms
        return marked

    async def expire_overdue_signals(self, now_ms: int | None = None) -> None:
        now_ms = now_ms or _now_ms()
        for evaluation in list(self.evaluations.values()):
            if evaluation.status not in {"open", "partial"}:
                continue
            expiry_marks = [mark for mark in evaluation.marks if mark.horizon == "expiry"]
            if expiry_marks and expiry_marks[0].due_at_ms <= now_ms:
                await self.mark_due(now_ms)
                break

    async def get_by_signal_id(self, signal_id: str) -> SignalEvaluation | None:
        evaluation = self.evaluations.get(signal_id)
        if evaluation is not None:
            return evaluation
        if self._repo_enabled():
            data = await self.repository.get_signal_evaluation_by_signal_id(signal_id)
            if data:
                evaluation = SignalEvaluation(**data)
                self.evaluations[evaluation.signal_id] = evaluation
                for mark in evaluation.marks:
                    self.marks[mark.id] = mark
                return evaluation
        return None

    async def get(self, evaluation_id: str) -> SignalEvaluation | None:
        for evaluation in self.evaluations.values():
            if evaluation.id == evaluation_id:
                return evaluation
        if self._repo_enabled():
            data = await self.repository.get_signal_evaluation(evaluation_id)
            if data:
                evaluation = SignalEvaluation(**data)
                self.evaluations[evaluation.signal_id] = evaluation
                for mark in evaluation.marks:
                    self.marks[mark.id] = mark
                return evaluation
        return None

    async def list_evaluations(self, status: str | None = None, symbol: str | None = None, limit: int = 100) -> list[SignalEvaluation]:
        if self._repo_enabled():
            return [SignalEvaluation(**item) for item in await self.repository.list_signal_evaluations(status=status, symbol=symbol, limit=limit)]
        items = list(self.evaluations.values())
        if status:
            items = [item for item in items if item.status == status]
        if symbol:
            items = [item for item in items if item.symbol == symbol.upper()]
        return sorted(items, key=lambda item: item.created_at_ms, reverse=True)[:limit]

    def _build_marks(self, evaluation: SignalEvaluation, signal: TradeSignal) -> list[SignalEvaluationMark]:
        marks: list[SignalEvaluationMark] = []
        seen: set[str] = set()
        for horizon in self.settings.autonomy_eval_horizon_list:
            if horizon in seen:
                continue
            seen.add(horizon)
            due_at = signal.expires_at_ms if horizon == "expiry" else signal.created_at_ms + HORIZON_MS.get(horizon, 0)
            if due_at <= signal.created_at_ms and horizon != "expiry":
                continue
            marks.append(
                SignalEvaluationMark(
                    id=f"mark_{signal.id}_{horizon}",
                    evaluation_id=evaluation.id,
                    signal_id=signal.id,
                    symbol=signal.symbol.upper(),
                    horizon=horizon,
                    due_at_ms=due_at,
                    metadata={"exchange_actions": []},
                )
            )
        return marks

    def _update_path(self, evaluation: SignalEvaluation, price: float, timestamp_ms: int) -> SignalEvaluation:
        first_price = evaluation.first_price if evaluation.first_price is not None else price
        favorable_price = evaluation.max_favorable_price
        adverse_price = evaluation.max_adverse_price
        if favorable_price is None or _directional_delta(evaluation.side, evaluation.entry, price) > _directional_delta(evaluation.side, evaluation.entry, favorable_price):
            favorable_price = price
        if adverse_price is None or _directional_delta(evaluation.side, evaluation.entry, price) < _directional_delta(evaluation.side, evaluation.entry, adverse_price):
            adverse_price = price
        stop_hit = evaluation.stop_hit or _stop_hit(evaluation.side, price, evaluation.stop)
        stop_hit_at_ms = evaluation.stop_hit_at_ms or (timestamp_ms if stop_hit and not evaluation.stop_hit else None)
        tp_hit = evaluation.take_profit_hit or (evaluation.take_profit is not None and _tp_hit(evaluation.side, price, evaluation.take_profit))
        tp_hit_at_ms = evaluation.take_profit_hit_at_ms or (timestamp_ms if tp_hit and not evaluation.take_profit_hit else None)
        return evaluation.model_copy(
            update={
                "first_price": first_price,
                "latest_price": price,
                "latest_price_at_ms": timestamp_ms,
                "max_favorable_price": favorable_price,
                "max_adverse_price": adverse_price,
                "max_favorable_bps": _directional_return_bps(evaluation.side, evaluation.entry, favorable_price),
                "max_adverse_bps": _directional_return_bps(evaluation.side, evaluation.entry, adverse_price),
                "max_favorable_r": _r_multiple(evaluation.side, evaluation.entry, evaluation.stop, favorable_price),
                "max_adverse_r": _r_multiple(evaluation.side, evaluation.entry, evaluation.stop, adverse_price),
                "stop_hit": stop_hit,
                "stop_hit_at_ms": stop_hit_at_ms,
                "take_profit_hit": tp_hit,
                "take_profit_hit_at_ms": tp_hit_at_ms,
                "terminal_outcome": _terminal_outcome(evaluation, latest_price=price),
                "realized_or_marked_r": _r_multiple(evaluation.side, evaluation.entry, evaluation.stop, price),
            }
        )

    def _mark_horizon(self, evaluation: SignalEvaluation, mark: SignalEvaluationMark, now_ms: int) -> SignalEvaluationMark:
        price = evaluation.latest_price
        latest_at = evaluation.latest_price_at_ms
        if price is None or latest_at is None or now_ms - latest_at > FRESH_MARK_PRICE_MS:
            return mark.model_copy(update={"marked_at_ms": now_ms, "status": "missed_no_price", "metadata": {**mark.metadata, "reason": "no_fresh_price", "exchange_actions": []}})
        return mark.model_copy(
            update={
                "marked_at_ms": now_ms,
                "price": price,
                "direction_adjusted_return_bps": _directional_return_bps(evaluation.side, evaluation.entry, price),
                "r_multiple": _r_multiple(evaluation.side, evaluation.entry, evaluation.stop, price),
                "mfe_bps_until_mark": evaluation.max_favorable_bps,
                "mae_bps_until_mark": evaluation.max_adverse_bps,
                "mfe_r_until_mark": evaluation.max_favorable_r,
                "mae_r_until_mark": evaluation.max_adverse_r,
                "stop_hit_before_mark": bool(evaluation.stop_hit and evaluation.stop_hit_at_ms and evaluation.stop_hit_at_ms <= now_ms),
                "take_profit_hit_before_mark": bool(evaluation.take_profit_hit and evaluation.take_profit_hit_at_ms and evaluation.take_profit_hit_at_ms <= now_ms),
                "status": "marked",
                "metadata": {**mark.metadata, "exchange_actions": []},
            }
        )

    def _maybe_complete(self, evaluation: SignalEvaluation, now_ms: int) -> SignalEvaluation:
        marks = [self.marks.get(mark.id, mark) for mark in evaluation.marks]
        evaluation = evaluation.model_copy(update={"marks": marks})
        expiry_done = any(mark.horizon == "expiry" and mark.status != "pending" for mark in marks)
        all_short_due_done = all(mark.status != "pending" or mark.due_at_ms > now_ms for mark in marks)
        terminal_hit = evaluation.stop_hit or evaluation.take_profit_hit
        if expiry_done or (terminal_hit and all_short_due_done):
            terminal = _terminal_outcome(evaluation, latest_price=evaluation.latest_price)
            if terminal == "open":
                terminal = _expiry_outcome(evaluation)
            opportunity_cost = None
            if evaluation.rejected and evaluation.max_favorable_r is not None:
                opportunity_cost = max(0.0, evaluation.max_favorable_r)
            return evaluation.model_copy(update={"status": "complete", "completed_at_ms": now_ms, "terminal_outcome": terminal, "opportunity_cost_r": opportunity_cost})
        return evaluation

    async def _on_completed(self, evaluation: SignalEvaluation) -> None:
        SIGNAL_OUTCOMES_BY_TYPE.labels(symbol=evaluation.symbol, signal_type=evaluation.signal_type, outcome=evaluation.terminal_outcome).inc()
        await self._record_event("signal_evaluation_completed", evaluation.symbol, {"signal_id": evaluation.signal_id, "evaluation_id": evaluation.id, "terminal_outcome": evaluation.terminal_outcome, "exchange_actions": []})
        if self.memory_service is not None:
            observe = getattr(self.memory_service, "observe_signal_evaluation", None)
            if callable(observe):
                await observe(evaluation)
        if self.world_model_service is not None:
            observe_world = getattr(self.world_model_service, "observe_signal_evaluation", None)
            if callable(observe_world):
                try:
                    await observe_world(evaluation)
                except Exception as exc:  # pragma: no cover - advisory memory must not break evaluation
                    log.warning("world_model_signal_evaluation_observe_failed", error=type(exc).__name__)

    async def _persist(self, evaluation: SignalEvaluation) -> None:
        if self._repo_enabled():
            await self.repository.upsert_signal_evaluation(evaluation.model_dump(mode="json", exclude={"marks"}))

    async def _persist_mark(self, mark: SignalEvaluationMark) -> None:
        if self._repo_enabled():
            await self.repository.upsert_signal_evaluation_mark(mark.model_dump(mode="json"))
            await self._record_event("signal_evaluation_marked", mark.symbol, {"signal_id": mark.signal_id, "horizon": mark.horizon, "status": mark.status, "exchange_actions": []})

    async def _persist_marks(self, marks: list[SignalEvaluationMark]) -> None:
        if self._repo_enabled():
            await self.repository.upsert_signal_evaluation_marks([mark.model_dump(mode="json") for mark in marks])

    async def _record_event(self, event_type: str, symbol: str | None, payload: dict[str, Any]) -> None:
        if self._repo_enabled():
            await self.repository.record_autonomy_event(event_type, actor="autonomy_evaluation", symbol=symbol, payload=payload)

    def _repo_enabled(self) -> bool:
        return self.repository is not None and getattr(self.repository, "enabled", False)

    def _record_error(self, exc: Exception) -> None:
        self.error_count += 1
        self.last_error = type(exc).__name__
        SIGNAL_EVALUATION_ERRORS.labels(error=type(exc).__name__).inc()
        log.warning("signal_evaluation_service_error", error=type(exc).__name__)


def _directional_delta(side: str, entry: float, price: float) -> float:
    return price - entry if side == "long" else entry - price


def _directional_return_bps(side: str, entry: float, price: float | None) -> float | None:
    if price is None or entry <= 0:
        return None
    return _directional_delta(side, entry, price) / entry * 10_000


def _r_multiple(side: str, entry: float, stop: float, price: float | None) -> float | None:
    if price is None:
        return None
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    return _directional_delta(side, entry, price) / risk


def _stop_hit(side: str, price: float, stop: float) -> bool:
    return price <= stop if side == "long" else price >= stop


def _tp_hit(side: str, price: float, take_profit: float) -> bool:
    return price >= take_profit if side == "long" else price <= take_profit


def _terminal_outcome(evaluation: SignalEvaluation, *, latest_price: float | None) -> str:
    if evaluation.stop_hit and evaluation.take_profit_hit:
        stop_at = evaluation.stop_hit_at_ms or 0
        tp_at = evaluation.take_profit_hit_at_ms or 0
        return "tp_hit" if tp_at and (not stop_at or tp_at <= stop_at) else "stop_hit"
    if evaluation.take_profit_hit:
        return "tp_hit"
    if evaluation.stop_hit:
        return "stop_hit"
    if latest_price is None:
        return "open"
    return "open"


def _expiry_outcome(evaluation: SignalEvaluation) -> str:
    if evaluation.latest_price is None:
        return "insufficient_data"
    r = _r_multiple(evaluation.side, evaluation.entry, evaluation.stop, evaluation.latest_price)
    if r is None:
        return "insufficient_data"
    if r > 0.05:
        return "expired_positive"
    if r < -0.05:
        return "expired_negative"
    return "expired_flat"


def _now_ms() -> int:
    return int(time.time() * 1000)
