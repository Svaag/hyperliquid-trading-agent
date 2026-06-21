from __future__ import annotations

import inspect

from hyperliquid_trading_agent.app.autonomy.service import AutonomousTradingLoopService
from hyperliquid_trading_agent.app.config import Settings


def test_autonomy_signals_with_engine_enabled_switch_defaults_safe() -> None:
    settings = Settings(environment="test")

    assert settings.autonomy_signals_run_with_engine_enabled is False


def test_autonomy_iteration_can_continue_to_signal_path_after_engine_run() -> None:
    source = inspect.getsource(AutonomousTradingLoopService._run_iteration)

    assert "autonomy_signals_run_with_engine_enabled" in source
    assert "await self.engine_service.run_once" in source
    assert "await self._generate_and_post_signals" in source
