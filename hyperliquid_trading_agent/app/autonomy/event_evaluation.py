from __future__ import annotations

import hashlib
import time
from typing import Any, Literal

from hyperliquid_trading_agent.app.autonomy.schemas import (
    AlphaEventEvaluation,
    AlphaEventEvaluationMark,
    NewsEvent,
)
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.logging import get_logger

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
_MACRO_EVENT_TYPES = {"macro", "regulatory", "exchange_status", "halt"}


class AlphaEventEvaluationService:
    """Deterministic catalyst/event outcome tracker.

    It answers: "would this high-signal news/event have worked?" without ever
    approving or placing trades. Outcomes are paper/shadow-only and feed Token
    Capital, memory, and tuning proposals as evidence.
    """

    def __init__(self, *, settings: Settings, repository: Any = None, memory_service: Any | None = None, world_model_service: Any | None = None):
        self.settings = settings
        self.repository = repository
        self.memory_service = memory_service
        self.world_model_service = world_model_service
        self.evaluations: dict[str, AlphaEventEvaluation] = {}
        self.marks: dict[str, AlphaEventEvaluationMark] = {}
        self.last_mark_at_ms: int | None = None
        self.error_count = 0
        self.last_error: str | None = None

    async def load_open(self) -> None:
        if not self._repo_enabled():
            return
        try:
            for item in await self.repository.list_open_alpha_event_evaluations(
                limit=self.settings.autonomy_event_eval_max_open_events
            ):
                evaluation = AlphaEventEvaluation(**item)
                self.evaluations[evaluation.id] = evaluation
                for mark in evaluation.marks:
                    self.marks[mark.id] = mark
        except Exception as exc:  # pragma: no cover - startup resilience
            self._record_error(exc)

    def status(self) -> dict[str, Any]:
        open_evals = [item for item in self.evaluations.values() if item.status in {"open", "partial"}]
        pending_marks = [item for item in self.marks.values() if item.status == "pending"]
        return {
            "enabled": self.settings.autonomy_event_evaluation_enabled,
            "effective_enabled": self.settings.autonomy_event_evaluation_effective_enabled,
            "open_evaluations": len(open_evals),
            "pending_marks": len(pending_marks),
            "last_mark_at_ms": self.last_mark_at_ms,
            "error_count": self.error_count,
            "last_error": self.last_error,
            "capture_policy": {
                "min_importance": self.settings.autonomy_event_eval_min_importance,
                "min_source_score": self.settings.autonomy_event_eval_min_source_score,
                "max_symbols_per_event": self.settings.autonomy_event_eval_symbols_per_event,
                "macro_proxies": self.settings.autonomy_event_eval_macro_proxy_symbols,
                "horizons": self.settings.autonomy_event_eval_horizon_list,
            },
        }

    async def create_for_newswire_event(
        self, event: Any, *, market_regime: str = "unknown"
    ) -> list[AlphaEventEvaluation]:
        if callable(getattr(event, "to_news_event", None)):
            return await self.create_for_news_event(event.to_news_event(), market_regime=market_regime)
        if isinstance(event, NewsEvent):
            return await self.create_for_news_event(event, market_regime=market_regime)
        return await self.create_for_news_event(_news_event_from_mapping(event), market_regime=market_regime)

    async def create_for_news_event(
        self, event: NewsEvent, *, market_regime: str = "unknown"
    ) -> list[AlphaEventEvaluation]:
        if not self.settings.autonomy_event_evaluation_enabled:
            return []
        if not _passes_capture_policy(event, self.settings):
            return []
        symbols = _symbols_for_event(event, self.settings)
        if not symbols:
            return []
        created: list[AlphaEventEvaluation] = []
        for symbol in symbols[: self.settings.autonomy_event_eval_symbols_per_event]:
            existing = await self._existing(event.id, symbol)
            if existing is not None:
                created.append(existing)
                continue
            evaluation = _evaluation_from_event(
                event, symbol=symbol, settings=self.settings, market_regime=market_regime
            )
            evaluation.marks = self._build_marks(evaluation)
            self.evaluations[evaluation.id] = evaluation
            for mark in evaluation.marks:
                self.marks[mark.id] = mark
            await self._persist(evaluation)
            await self._persist_marks(evaluation.marks)
            await self._record_event(
                "alpha_event_evaluation_created",
                symbol,
                {
                    "event_id": event.id,
                    "evaluation_id": evaluation.id,
                    "event_type": evaluation.event_type,
                    "exchange_actions": [],
                },
            )
            created.append(evaluation)
        return created

    async def on_price(self, symbol: str, asset_class: str | None, price: float, timestamp_ms: int) -> None:
        if not self.settings.autonomy_event_evaluation_enabled or price <= 0:
            return
        symbol = symbol.upper()
        evaluations = [
            item for item in self.evaluations.values() if item.symbol == symbol and item.status in {"open", "partial"}
        ]
        if not evaluations and self._repo_enabled():
            for item in await self.repository.list_open_alpha_event_evaluations(
                symbol=symbol, limit=self.settings.autonomy_event_eval_max_open_events
            ):
                evaluation = AlphaEventEvaluation(**item)
                self.evaluations[evaluation.id] = evaluation
                for mark in evaluation.marks:
                    self.marks[mark.id] = mark
                evaluations.append(evaluation)
        for evaluation in evaluations:
            updated = self._update_path(evaluation, price, timestamp_ms)
            self.evaluations[updated.id] = updated
            await self._persist(updated)

    async def mark_due(self, now_ms: int | None = None) -> list[AlphaEventEvaluationMark]:
        now_ms = now_ms or _now_ms()
        due = [item for item in self.marks.values() if item.status == "pending" and item.due_at_ms <= now_ms]
        seen = {item.id for item in due}
        if self._repo_enabled():
            for item in await self.repository.list_due_alpha_event_evaluation_marks(now_ms, limit=500):
                mark = AlphaEventEvaluationMark(**item)
                if mark.id not in seen:
                    self.marks[mark.id] = mark
                    due.append(mark)
                    seen.add(mark.id)
        marked: list[AlphaEventEvaluationMark] = []
        for mark in sorted(due, key=lambda item: item.due_at_ms):
            evaluation = await self.get(mark.evaluation_id)
            if evaluation is None:
                continue
            updated_mark = self._mark_horizon(evaluation, mark, now_ms)
            self.marks[updated_mark.id] = updated_mark
            marked.append(updated_mark)
            await self._persist_mark(updated_mark)
            evaluation = self._maybe_complete(evaluation, now_ms)
            self.evaluations[evaluation.id] = evaluation
            await self._persist(evaluation)
            if evaluation.status == "complete" or evaluation.status == "expired_no_data":
                await self._on_completed(evaluation)
        if marked:
            self.last_mark_at_ms = now_ms
        return marked

    async def expire_overdue_events(self, now_ms: int | None = None) -> None:
        now_ms = now_ms or _now_ms()
        for evaluation in list(self.evaluations.values()):
            if evaluation.status not in {"open", "partial"}:
                continue
            if evaluation.marks and max(mark.due_at_ms for mark in evaluation.marks) <= now_ms:
                await self.mark_due(now_ms)
                break

    async def get(self, evaluation_id: str) -> AlphaEventEvaluation | None:
        evaluation = self.evaluations.get(evaluation_id)
        if evaluation is not None:
            return evaluation
        if self._repo_enabled():
            data = await self.repository.get_alpha_event_evaluation(evaluation_id)
            if data:
                evaluation = AlphaEventEvaluation(**data)
                self.evaluations[evaluation.id] = evaluation
                for mark in evaluation.marks:
                    self.marks[mark.id] = mark
                return evaluation
        return None

    async def get_by_event_id(self, event_id: str) -> list[AlphaEventEvaluation]:
        items = [item for item in self.evaluations.values() if item.event_id == event_id]
        if items:
            return sorted(items, key=lambda item: item.symbol)
        if self._repo_enabled():
            return [
                AlphaEventEvaluation(**item)
                for item in await self.repository.get_alpha_event_evaluation_by_event_id(event_id)
            ]
        return []

    async def list_evaluations(
        self, status: str | None = None, symbol: str | None = None, limit: int = 100
    ) -> list[AlphaEventEvaluation]:
        if self._repo_enabled():
            return [
                AlphaEventEvaluation(**item)
                for item in await self.repository.list_alpha_event_evaluations(
                    status=status, symbol=symbol, limit=limit
                )
            ]
        items = list(self.evaluations.values())
        if status:
            items = [item for item in items if item.status == status]
        if symbol:
            items = [item for item in items if item.symbol == symbol.upper()]
        return sorted(items, key=lambda item: item.received_at_ms, reverse=True)[:limit]

    def _build_marks(self, evaluation: AlphaEventEvaluation) -> list[AlphaEventEvaluationMark]:
        marks: list[AlphaEventEvaluationMark] = []
        seen: set[str] = set()
        for horizon in self.settings.autonomy_event_eval_horizon_list:
            if horizon in seen or horizon == "expiry":
                continue
            seen.add(horizon)
            due_at = evaluation.received_at_ms + HORIZON_MS.get(horizon, 0)
            if due_at <= evaluation.received_at_ms:
                continue
            marks.append(
                AlphaEventEvaluationMark(
                    id=_mark_id(evaluation.id, horizon),
                    evaluation_id=evaluation.id,
                    event_id=evaluation.event_id,
                    symbol=evaluation.symbol,
                    asset_class=evaluation.asset_class,
                    horizon=horizon,
                    due_at_ms=due_at,
                    metadata={"exchange_actions": []},
                )
            )
        return marks

    def _update_path(self, evaluation: AlphaEventEvaluation, price: float, timestamp_ms: int) -> AlphaEventEvaluation:
        reference_price = evaluation.reference_price if evaluation.reference_price is not None else price
        reference_price_at_ms = (
            evaluation.reference_price_at_ms if evaluation.reference_price_at_ms is not None else timestamp_ms
        )
        favorable_price = evaluation.max_favorable_price
        adverse_price = evaluation.max_adverse_price
        current_bps = _directional_bps(evaluation.direction, reference_price, price) or 0.0
        previous_favorable_bps = _directional_bps(evaluation.direction, reference_price, favorable_price)
        previous_adverse_bps = _directional_bps(evaluation.direction, reference_price, adverse_price)
        if favorable_price is None or current_bps > (
            previous_favorable_bps if previous_favorable_bps is not None else -1e18
        ):
            favorable_price = price
        if adverse_price is None or current_bps < (previous_adverse_bps if previous_adverse_bps is not None else 1e18):
            adverse_price = price
        max_favorable_bps = _directional_bps(evaluation.direction, reference_price, favorable_price)
        max_adverse_bps = _directional_bps(evaluation.direction, reference_price, adverse_price)
        max_abs_move_bps = max(
            abs(max_favorable_bps or 0.0), abs(max_adverse_bps or 0.0), abs(_raw_bps(reference_price, price) or 0.0)
        )
        return evaluation.model_copy(
            update={
                "reference_price": reference_price,
                "reference_price_at_ms": reference_price_at_ms,
                "latest_price": price,
                "latest_price_at_ms": timestamp_ms,
                "max_favorable_price": favorable_price,
                "max_adverse_price": adverse_price,
                "max_favorable_bps": max_favorable_bps,
                "max_adverse_bps": max_adverse_bps,
                "max_abs_move_bps": max_abs_move_bps,
                "realized_or_marked_bps": current_bps
                if evaluation.direction != "neutral"
                else _raw_bps(reference_price, price),
                "terminal_outcome": _terminal_outcome(
                    evaluation,
                    max_favorable_bps=max_favorable_bps,
                    max_adverse_bps=max_adverse_bps,
                    max_abs_move_bps=max_abs_move_bps,
                ),
            }
        )

    def _mark_horizon(
        self, evaluation: AlphaEventEvaluation, mark: AlphaEventEvaluationMark, now_ms: int
    ) -> AlphaEventEvaluationMark:
        price = evaluation.latest_price
        latest_at = evaluation.latest_price_at_ms
        if price is None or latest_at is None or now_ms - latest_at > FRESH_MARK_PRICE_MS:
            return mark.model_copy(
                update={
                    "marked_at_ms": now_ms,
                    "status": "missed_no_price",
                    "metadata": {**mark.metadata, "reason": "no_fresh_price", "exchange_actions": []},
                }
            )
        return mark.model_copy(
            update={
                "marked_at_ms": now_ms,
                "price": price,
                "direction_adjusted_return_bps": evaluation.realized_or_marked_bps,
                "abs_move_bps": abs(_raw_bps(evaluation.reference_price, price) or 0.0),
                "max_favorable_bps_until_mark": evaluation.max_favorable_bps,
                "max_adverse_bps_until_mark": evaluation.max_adverse_bps,
                "max_abs_move_bps_until_mark": evaluation.max_abs_move_bps,
                "status": "marked",
                "metadata": {**mark.metadata, "exchange_actions": []},
            }
        )

    def _maybe_complete(self, evaluation: AlphaEventEvaluation, now_ms: int) -> AlphaEventEvaluation:
        marks = [self.marks.get(mark.id, mark) for mark in evaluation.marks]
        evaluation = evaluation.model_copy(update={"marks": marks})
        if not marks:
            return evaluation.model_copy(
                update={"status": "expired_no_data", "completed_at_ms": now_ms, "terminal_outcome": "insufficient_data"}
            )
        if any(mark.status == "pending" and mark.due_at_ms <= now_ms for mark in marks):
            return evaluation.model_copy(
                update={"status": "partial" if any(mark.status != "pending" for mark in marks) else evaluation.status}
            )
        if all(mark.status != "pending" for mark in marks):
            if evaluation.reference_price is None or evaluation.latest_price is None:
                return evaluation.model_copy(
                    update={
                        "status": "expired_no_data",
                        "completed_at_ms": now_ms,
                        "terminal_outcome": "insufficient_data",
                    }
                )
            terminal = _terminal_outcome(
                evaluation,
                max_favorable_bps=evaluation.max_favorable_bps,
                max_adverse_bps=evaluation.max_adverse_bps,
                max_abs_move_bps=evaluation.max_abs_move_bps,
            )
            if terminal == "open":
                terminal = "insufficient_data"
            return evaluation.model_copy(
                update={"status": "complete", "completed_at_ms": now_ms, "terminal_outcome": terminal}
            )
        return evaluation

    async def _on_completed(self, evaluation: AlphaEventEvaluation) -> None:
        await self._record_event(
            "alpha_event_evaluation_completed",
            evaluation.symbol,
            {
                "event_id": evaluation.event_id,
                "evaluation_id": evaluation.id,
                "terminal_outcome": evaluation.terminal_outcome,
                "exchange_actions": [],
            },
        )
        if self.memory_service is not None:
            observe = getattr(self.memory_service, "observe_event_evaluation", None)
            if callable(observe):
                await observe(evaluation)
        if self.world_model_service is not None:
            observe_world = getattr(self.world_model_service, "observe_alpha_event_evaluation", None)
            if callable(observe_world):
                try:
                    await observe_world(evaluation)
                except Exception as exc:  # pragma: no cover - advisory memory must not break event evaluation
                    log.warning("world_model_event_evaluation_observe_failed", error=type(exc).__name__)

    async def _existing(self, event_id: str, symbol: str) -> AlphaEventEvaluation | None:
        evaluation_id = _evaluation_id(event_id, symbol)
        if evaluation_id in self.evaluations:
            return self.evaluations[evaluation_id]
        if self._repo_enabled():
            items = await self.repository.get_alpha_event_evaluation_by_event_id(event_id, symbol=symbol)
            if items:
                evaluation = AlphaEventEvaluation(**items[0])
                self.evaluations[evaluation.id] = evaluation
                for mark in evaluation.marks:
                    self.marks[mark.id] = mark
                return evaluation
        return None

    async def _persist(self, evaluation: AlphaEventEvaluation) -> None:
        if self._repo_enabled():
            await self.repository.upsert_alpha_event_evaluation(evaluation.model_dump(mode="json", exclude={"marks"}))

    async def _persist_mark(self, mark: AlphaEventEvaluationMark) -> None:
        if self._repo_enabled():
            await self.repository.upsert_alpha_event_evaluation_mark(mark.model_dump(mode="json"))
            await self._record_event(
                "alpha_event_evaluation_marked",
                mark.symbol,
                {"event_id": mark.event_id, "horizon": mark.horizon, "status": mark.status, "exchange_actions": []},
            )

    async def _persist_marks(self, marks: list[AlphaEventEvaluationMark]) -> None:
        if self._repo_enabled():
            await self.repository.upsert_alpha_event_evaluation_marks([mark.model_dump(mode="json") for mark in marks])

    async def _record_event(self, event_type: str, symbol: str | None, payload: dict[str, Any]) -> None:
        if self._repo_enabled():
            await self.repository.record_autonomy_event(
                event_type, actor="alpha_event_evaluation", symbol=symbol, payload=payload
            )

    def _repo_enabled(self) -> bool:
        return self.repository is not None and getattr(self.repository, "enabled", False)

    def _record_error(self, exc: Exception) -> None:
        self.error_count += 1
        self.last_error = type(exc).__name__
        log.warning("alpha_event_evaluation_service_error", error=type(exc).__name__)


def _passes_capture_policy(event: NewsEvent, settings: Settings) -> bool:
    source_score = _source_score(event)
    if event.importance_score < settings.autonomy_event_eval_min_importance:
        return False
    if source_score < settings.autonomy_event_eval_min_source_score:
        return False
    return bool(_symbols_for_event(event, settings))


def _symbols_for_event(event: NewsEvent, settings: Settings) -> list[str]:
    symbols = [symbol.upper() for symbol in event.assets if symbol]
    if symbols:
        return _dedupe(symbols)
    metadata = event.metadata or {}
    event_type = str(metadata.get("event_type") or "").lower()
    asset_class = str(metadata.get("asset_class") or "").lower()
    headline = f"{event.title} {event.text}".lower()
    if (
        event_type in _MACRO_EVENT_TYPES
        or asset_class == "macro"
        or any(term in headline for term in ("fed", "fomc", "cpi", "inflation", "rates", "sec", "halt"))
    ):
        return settings.autonomy_event_eval_macro_proxy_symbols
    return []


def _evaluation_from_event(
    event: NewsEvent, *, symbol: str, settings: Settings, market_regime: str
) -> AlphaEventEvaluation:
    metadata = dict(event.metadata or {})
    asset_class = str(metadata.get("asset_class") or _asset_class_for_symbol(symbol))
    event_type = str(metadata.get("event_type") or "headline")
    direction = _direction_from_sentiment(event.sentiment)
    return AlphaEventEvaluation(
        id=_evaluation_id(event.id, symbol),
        event_id=event.id,
        event_source=event.source,
        provider=event.provider,
        event_type=event_type,
        asset_class=asset_class,
        symbol=symbol.upper(),
        direction=direction,
        sentiment=event.sentiment,
        status="open",
        terminal_outcome="open",
        received_at_ms=event.observed_at_ms,
        headline=event.title,
        url=event.url,
        importance_score=event.importance_score,
        source_score=_source_score(event),
        urgency=str(metadata.get("urgency") or ("breaking" if event.freshness == "breaking" else "normal")),
        freshness=event.freshness,
        market_regime=market_regime,
        metadata={
            "raw_event": event.model_dump(mode="json"),
            "capture_policy": {
                "min_importance": settings.autonomy_event_eval_min_importance,
                "min_source_score": settings.autonomy_event_eval_min_source_score,
            },
            "worked_bps": settings.autonomy_event_eval_worked_bps,
            "failed_bps": settings.autonomy_event_eval_failed_bps,
            "volatility_bps": settings.autonomy_event_eval_volatility_bps,
            "exchange_actions": [],
        },
    )


def _news_event_from_mapping(data: Any) -> NewsEvent:
    if hasattr(data, "model_dump"):
        data = data.model_dump(mode="json")
    mapping = dict(data or {})
    if "event_id" in mapping and "id" not in mapping:
        from hyperliquid_trading_agent.app.newswire.schemas import NewswireEvent

        return NewswireEvent(**mapping).to_news_event()
    return NewsEvent(**mapping)


def _source_score(event: NewsEvent) -> float:
    metadata = event.metadata or {}
    value = metadata.get("source_score")
    try:
        if value is not None:
            return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        pass
    # Legacy RSS/search/X items do not carry Newswire source_score. Treat them as
    # acceptable but not perfect so fallback polling can still be evaluated.
    if event.provider in {"rss", "alpaca", "trading_economics"}:
        return 0.8
    if event.provider == "x":
        return 0.4
    return 1.0


def _direction_from_sentiment(sentiment: str) -> Literal["long", "short", "neutral"]:
    if sentiment == "bullish":
        return "long"
    if sentiment == "bearish":
        return "short"
    return "neutral"


def _asset_class_for_symbol(symbol: str) -> str:
    token = symbol.upper()
    if token in {"BTC", "ETH", "HYPE", "SOL", "DOGE", "XRP", "BNB", "AVAX", "LINK"}:
        return "crypto"
    if token in {"SPY", "QQQ", "IWM", "DIA"} or token.isalpha():
        return "equity"
    return "unknown"


def _directional_bps(direction: str, reference: float | None, price: float | None) -> float | None:
    if reference is None or price is None or reference <= 0:
        return None
    raw = (price - reference) / reference * 10_000
    if direction == "short":
        return -raw
    return raw


def _raw_bps(reference: float | None, price: float | None) -> float | None:
    if reference is None or price is None or reference <= 0:
        return None
    return (price - reference) / reference * 10_000


def _terminal_outcome(
    evaluation: AlphaEventEvaluation,
    *,
    max_favorable_bps: float | None,
    max_adverse_bps: float | None,
    max_abs_move_bps: float | None,
) -> str:
    if max_favorable_bps is None and max_adverse_bps is None:
        return "open"
    worked = (
        (max_favorable_bps or 0.0) >= evaluation.metadata.get("worked_bps", 0)
        if "worked_bps" in evaluation.metadata
        else (max_favorable_bps or 0.0) >= 50.0
    )
    failed_threshold = evaluation.metadata.get("failed_bps", None)
    failed_level = float(failed_threshold) if isinstance(failed_threshold, (int, float)) else -35.0
    failed = (max_adverse_bps or 0.0) <= failed_level
    volatility_threshold = evaluation.metadata.get("volatility_bps", None)
    vol_level = float(volatility_threshold) if isinstance(volatility_threshold, (int, float)) else 75.0
    if evaluation.direction == "neutral":
        return "volatility_only" if (max_abs_move_bps or 0.0) >= vol_level else "mixed"
    if worked and failed:
        return "mixed"
    if worked:
        return "worked"
    if failed:
        return "failed"
    return "mixed"


def _evaluation_id(event_id: str, symbol: str) -> str:
    digest = hashlib.sha1(f"{event_id}:{symbol.upper()}".encode()).hexdigest()[:24]
    return f"aeval_{digest}"


def _mark_id(evaluation_id: str, horizon: str) -> str:
    return f"aemark_{evaluation_id}_{horizon}"


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _now_ms() -> int:
    return int(time.time() * 1000)
