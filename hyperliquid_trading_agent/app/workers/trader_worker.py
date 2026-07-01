from __future__ import annotations

from typing import Any

from hyperliquid_trading_agent.app.config import ServiceRole, Settings
from hyperliquid_trading_agent.app.workers.base import BaseWorker


class TraderWorker(BaseWorker):
    role = ServiceRole.TRADER
    lock_name = "service:trader"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.command_count = 0

    async def run(self) -> None:
        await self.command_loop(
            {
                "engine_strategy_regime_refresh": self._accepted_noop,
                "engine_bandit_run": self._accepted_noop,
                "engine_replay_comparison_run": self._accepted_noop,
                "hip4_loop_run_once": self._accepted_noop,
                "hip4_scan_run": self._accepted_noop,
                "hip4_paper_execute": self._accepted_noop,
                "hip4_reconcile_run": self._accepted_noop,
                "autonomy_pause": self._accepted_noop,
                "autonomy_resume": self._accepted_noop,
                "autonomy_signal_approve": self._accepted_noop,
                "autonomy_signal_reject": self._accepted_noop,
                "autonomy_signal_expire": self._accepted_noop,
                "autonomy_equity_signal_approve": self._accepted_noop,
                "autonomy_equity_signal_reject": self._accepted_noop,
                "admin_debug_seed_flip_demo": self._accepted_noop,
            }
        )

    async def _accepted_noop(self, command: dict[str, Any]) -> dict[str, Any]:
        self.command_count += 1
        return {
            "accepted_by": self.instance_id,
            "note": "trader worker command boundary accepted; direct loop execution is disabled until the dedicated trader runtime is promoted",
            "command_type": command.get("command_type"),
        }

    def heartbeat_metadata(self) -> dict[str, Any]:
        return {"trader": {"command_count": self.command_count, "execution_authority": "settings-gated"}}
