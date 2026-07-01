from __future__ import annotations

from typing import Any

from hyperliquid_trading_agent.app.config import ServiceRole, Settings
from hyperliquid_trading_agent.app.workers.base import BaseWorker


class SchedulerWorker(BaseWorker):
    role = ServiceRole.SCHEDULER
    lock_name = "service:scheduler"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.command_count = 0

    async def run(self) -> None:
        await self.command_loop(
            {
                "orchestration_wave_run_once": self._accepted_noop,
                "autonomy_evaluations_run": self._accepted_noop,
                "autonomy_evaluations_backfill": self._accepted_noop,
                "autonomy_event_evaluations_backfill": self._accepted_noop,
                "autonomy_daily_report_run": self._accepted_noop,
                "autonomy_weekly_report_run": self._accepted_noop,
            }
        )

    async def _accepted_noop(self, command: dict[str, Any]) -> dict[str, Any]:
        self.command_count += 1
        return {"accepted_by": self.instance_id, "command_type": command.get("command_type"), "note": "scheduler command accepted"}

    def heartbeat_metadata(self) -> dict[str, Any]:
        return {"scheduler": {"command_count": self.command_count}}
