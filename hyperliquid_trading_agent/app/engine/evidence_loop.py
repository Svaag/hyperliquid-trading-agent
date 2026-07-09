"""Scheduled evidence refreshers for the shadow engine.

Keeps the readiness scorecard measurable without operator action: the
strategy-regime performance table is refreshed hourly and a baseline-equivalence
replay comparison is produced daily. Both are report-only writers — no engine,
risk, or execution state is touched.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.replay_compare import EngineReplayComparisonService
from hyperliquid_trading_agent.app.engine.strategy_performance import refresh_strategy_regime_performance
from hyperliquid_trading_agent.app.logging import get_logger

log = get_logger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


class EngineEvidenceRefreshLoopService:
    def __init__(self, *, settings: Settings, repository: Any):
        self.settings = settings
        self.repository = repository
        self._task: asyncio.Task | None = None
        self.last_strategy_refresh_at_ms: int | None = None
        self.last_replay_at_ms: int | None = None
        self.last_error: str | None = None
        self.strategy_refresh_count = 0
        self.replay_count = 0
        self.last_replay_status: str | None = None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "running": self._task is not None and not self._task.done(),
            "strategy_refresh_enabled": bool(self.settings.engine_strategy_regime_refresh_enabled),
            "replay_schedule_enabled": bool(self.settings.engine_replay_comparison_schedule_enabled),
            "last_strategy_refresh_at_ms": self.last_strategy_refresh_at_ms,
            "last_replay_at_ms": self.last_replay_at_ms,
            "last_replay_status": self.last_replay_status,
            "last_error": self.last_error,
            "strategy_refresh_count": self.strategy_refresh_count,
            "replay_count": self.replay_count,
        }

    @property
    def _enabled(self) -> bool:
        return bool(self.settings.engine_enabled) and (
            bool(self.settings.engine_strategy_regime_refresh_enabled) or bool(self.settings.engine_replay_comparison_schedule_enabled)
        )

    async def start(self) -> None:
        if self._task is not None or not self._enabled:
            return
        self._task = asyncio.create_task(self._run(), name="engine-evidence-refresh")
        log.info(
            "engine_evidence_refresh_started",
            strategy_interval_seconds=self.settings.engine_strategy_regime_refresh_interval_seconds,
            replay_interval_seconds=self.settings.engine_replay_comparison_interval_seconds,
        )

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
        await asyncio.sleep(120)
        while True:
            try:
                await self.run_due(_now_ms())
                self.last_error = None
            except Exception as exc:  # pragma: no cover - loop resilience
                self.last_error = type(exc).__name__
                log.warning("engine_evidence_refresh_failed", error=type(exc).__name__)
            await asyncio.sleep(60)

    async def run_due(self, now_ms: int) -> dict[str, Any]:
        result: dict[str, Any] = {"strategy_refreshed": False, "replay_ran": False}
        strategy_interval_ms = max(60, int(self.settings.engine_strategy_regime_refresh_interval_seconds)) * 1000
        if self.settings.engine_strategy_regime_refresh_enabled and (
            self.last_strategy_refresh_at_ms is None or now_ms - self.last_strategy_refresh_at_ms >= strategy_interval_ms
        ):
            result["strategy_refreshed"] = True
            result["strategy_rows"] = len(await self.run_strategy_refresh(now_ms))
        replay_interval_ms = max(3600, int(self.settings.engine_replay_comparison_interval_seconds)) * 1000
        if self.settings.engine_replay_comparison_schedule_enabled and (
            self.last_replay_at_ms is None or now_ms - self.last_replay_at_ms >= replay_interval_ms
        ):
            result["replay_ran"] = True
            result["replay_status"] = (await self.run_replay_comparison(now_ms)).get("status")
        return result

    async def run_strategy_refresh(self, now_ms: int) -> list[dict[str, Any]]:
        window_ms = max(1, int(self.settings.engine_evidence_refresh_window_hours)) * 60 * 60 * 1000
        rows = await refresh_strategy_regime_performance(
            self.repository,
            window_start_ms=now_ms - window_ms,
            window_end_ms=now_ms,
            limit=5000,
        )
        self.last_strategy_refresh_at_ms = now_ms
        self.strategy_refresh_count += 1
        log.info("engine_strategy_regime_refreshed", rows=len(rows))
        return rows

    async def run_replay_comparison(self, now_ms: int) -> dict[str, Any]:
        window_ms = max(1, int(self.settings.engine_evidence_refresh_window_hours)) * 60 * 60 * 1000
        artifact = await EngineReplayComparisonService(repository=self.repository, settings=self.settings).compare_variant(
            baseline_config={"current": True},
            candidate_config={"current": True},
            window_start_ms=now_ms - window_ms,
            window_end_ms=now_ms,
            universe=[str(symbol).upper() for symbol in self.settings.autonomy_core_symbols],
            variant_id="scheduled_baseline_equivalence",
        )
        self.last_replay_at_ms = now_ms
        self.replay_count += 1
        self.last_replay_status = str(artifact.get("status"))
        log.info("engine_scheduled_replay_completed", status=self.last_replay_status, replay_id=artifact.get("replay_id"))
        return artifact
