from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.logging import get_logger

log = get_logger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class EnginePnLAttributionLoopService:
    def __init__(self, *, settings: Settings, repository: Any, hyperliquid: Any):
        self.settings = settings
        self.repository = repository
        self.hyperliquid = hyperliquid
        self._task: asyncio.Task | None = None
        self.last_run_at_ms: int | None = None
        self.last_error: str | None = None
        self.records_created = 0
        self.positions_marked = 0

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.engine_pnl_attribution_enabled,
            "running": self._task is not None and not self._task.done(),
            "last_run_at_ms": self.last_run_at_ms,
            "last_error": self.last_error,
            "records_created": self.records_created,
            "positions_marked": self.positions_marked,
        }

    async def start(self) -> None:
        if self._task is not None or not self.settings.engine_enabled or not self.settings.engine_pnl_attribution_enabled:
            return
        self._task = asyncio.create_task(self._run(), name="engine-pnl-attribution")
        log.info("engine_pnl_attribution_started", interval_seconds=self.settings.engine_pnl_attribution_interval_seconds)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        await asyncio.sleep(max(30, self.settings.engine_pnl_attribution_min_mark_interval_seconds))
        while True:
            try:
                await self.run_once()
                self.last_error = None
            except Exception as exc:  # pragma: no cover
                self.last_error = type(exc).__name__
                log.warning("engine_pnl_attribution_failed", error=type(exc).__name__)
            await asyncio.sleep(max(60, self.settings.engine_pnl_attribution_interval_seconds))

    async def run_once(self) -> dict[str, Any]:
        ts = _now_ms()
        mids = await self._safe_all_mids()
        positions = await self.repository.list_position_theses(limit=1000)
        active = [item for item in positions if item.get("position_state") in {"open", "approved", "de_risking", "trailing", "time_stop_pending"}]
        created = 0
        closed = 0
        for position in active:
            asset = str(position.get("asset") or "").upper()
            mark_px = _f(mids.get(asset))
            if mark_px <= 0:
                continue
            report = await self._first_execution_report(position)
            if report is None:
                continue
            entry_px, size, fees_usd, slippage_bps = self._execution_inputs(report)
            if entry_px <= 0 or size <= 0:
                continue
            side = str(position.get("side") or "long")
            gross = (mark_px - entry_px) * size if side == "long" else (entry_px - mark_px) * size
            notional = entry_px * size
            slippage_cost = abs(notional) * slippage_bps / 10_000
            total = gross - fees_usd - slippage_cost
            window_start = await self._window_start(position, ts)
            attribution = {
                "attribution_id": _attr_id(position, window_start, ts),
                "position_id": position.get("position_id"),
                "candidate_id": position.get("entry_candidate_id"),
                "strategy_id": str(position.get("strategy_id") or "unknown"),
                "asset": asset,
                "window_start_ms": window_start,
                "window_end_ms": ts,
                "alpha_pnl_usd": gross,
                "timing_pnl_usd": 0.0,
                "execution_pnl_usd": -slippage_cost,
                "fees_usd": fees_usd,
                "funding_usd": 0.0,
                "residual_pnl_usd": 0.0,
                "total_pnl_usd": total,
                "metrics": {
                    "entry_px": entry_px,
                    "mark_px": mark_px,
                    "size": size,
                    "side": side,
                    "unrealized_pnl_usd": gross,
                    "return_bps": (mark_px / entry_px - 1) * 10_000 * (1 if side == "long" else -1),
                    "holding_ms": ts - int(position.get("opened_at_ms") or position.get("updated_at_ms") or ts),
                    "source": self.settings.engine_pnl_attribution_mark_source,
                },
                "metadata": {"exchange_actions": []},
            }
            await self.repository.record_pnl_attribution(attribution)
            created += 1
            self.positions_marked += 1
            close_reason = self._close_reason(position, mark_px, ts)
            if close_reason:
                updated = {
                    **position,
                    "position_state": "closed",
                    "closed_at_ms": ts,
                    "updated_at_ms": ts,
                    "degradation_reasons": [*(position.get("degradation_reasons") or []), close_reason],
                }
                await self.repository.record_position_thesis(updated)
                closed += 1
        self.last_run_at_ms = ts
        self.records_created += created
        return {"positions_seen": len(active), "records_created": created, "positions_closed": closed}

    async def _safe_all_mids(self) -> dict[str, float]:
        try:
            raw = await self.hyperliquid.all_mids()
            return {str(key).upper(): _f(value) for key, value in raw.items()}
        except Exception:
            return {}

    async def _first_execution_report(self, position: dict[str, Any]) -> dict[str, Any] | None:
        report_ids = list(position.get("execution_report_ids") or [])
        reports = await self.repository.list_execution_reports(limit=1000)
        for report_id in report_ids:
            for report in reports:
                if report.get("report_id") == report_id:
                    return report
        return None

    def _execution_inputs(self, report: dict[str, Any]) -> tuple[float, float, float, float]:
        assumptions = report.get("assumptions") or {}
        would_submit = assumptions.get("would_submit") or {}
        size = _f(report.get("filled_size")) or _f(report.get("requested_size")) or _f(would_submit.get("target_size"))
        entry_px = _f(report.get("avg_fill_px")) or _f(would_submit.get("price_limit"))
        if entry_px <= 0 and size > 0:
            entry_px = _f(would_submit.get("target_notional_usd")) / size
        return entry_px, size, _f(report.get("fees_usd")), _f(report.get("slippage_bps"))

    async def _window_start(self, position: dict[str, Any], now_ms: int) -> int:
        records = await self.repository.list_pnl_attribution(limit=1000)
        position_id = position.get("position_id")
        previous = [item for item in records if item.get("position_id") == position_id]
        if previous:
            return max(int(item.get("window_end_ms") or 0) for item in previous)
        return int(position.get("opened_at_ms") or position.get("updated_at_ms") or now_ms)

    def _close_reason(self, position: dict[str, Any], mark_px: float, now_ms: int) -> str | None:
        side = str(position.get("side") or "long")
        stop = _f(position.get("stop"))
        targets = [_f(item) for item in position.get("targets") or [] if _f(item) > 0]
        if side == "long" and stop > 0 and mark_px <= stop:
            return "stop_hit"
        if side == "short" and stop > 0 and mark_px >= stop:
            return "stop_hit"
        if targets:
            first_target = targets[0]
            if side == "long" and mark_px >= first_target:
                return "target_hit"
            if side == "short" and mark_px <= first_target:
                return "target_hit"
        opened = int(position.get("opened_at_ms") or position.get("updated_at_ms") or now_ms)
        if now_ms - opened > max(1, self.settings.engine_pnl_attribution_max_position_age_hours) * 60 * 60 * 1000:
            return "max_age"
        return None


def _attr_id(position: dict[str, Any], window_start_ms: int, window_end_ms: int) -> str:
    digest = hashlib.sha1(f"{position.get('position_id')}:{window_start_ms}:{window_end_ms}".encode()).hexdigest()[:24]
    return "attr_" + digest
