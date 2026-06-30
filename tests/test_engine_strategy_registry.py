from __future__ import annotations

import pytest

from hyperliquid_trading_agent.app.engine.alpha.directional import DirectionalMomentumStrategy
from hyperliquid_trading_agent.app.engine.strategy_registry import (
    StrategyRegistry,
    create_default_strategy_registry,
    planned_wave_1a_specs,
)


def test_default_strategy_registry_registers_existing_strategies_and_specs():
    registry = create_default_strategy_registry()

    assert registry.get("directional_momentum_v2") is not None
    assert registry.get("support_resistance_reversion_v2") is not None
    assert registry.get("microstructure_ofi_v1") is not None
    assert registry.get("news_event_alpha_v1") is not None
    assert registry.spec("equity_options_flow_v1") is not None

    metadata = registry.metadata()
    assert metadata["enabled_strategy_count"] >= 4
    assert "trend_following" in metadata["alpha_families"]
    assert "microstructure_orderflow" in metadata["alpha_families"]


def test_wave_1a_strategy_specs_are_valid_and_do_not_overcount_bridge_or_defensive():
    specs = {spec.strategy_id: spec for spec in planned_wave_1a_specs()}

    assert {
        "microstructure_ofi_v2",
        "liquidation_cascade_v1",
        "liquidation_mean_revert_v1",
        "funding_carry_v1",
        "oi_breakout_v1",
        "legacy_signal_adapter_v1",
        "regime_defensive_flat_v1",
    } <= set(specs)
    assert specs["legacy_signal_adapter_v1"].counts_for_breadth is False
    assert specs["regime_defensive_flat_v1"].counts_for_breadth is False
    assert specs["regime_defensive_flat_v1"].max_allocation_share_pct == 0.0
    assert all(spec.required_features for spec in specs.values())


def test_duplicate_strategy_ids_are_rejected():
    registry = StrategyRegistry()
    registry.register(DirectionalMomentumStrategy())

    with pytest.raises(ValueError, match="duplicate strategy_id"):
        registry.register(DirectionalMomentumStrategy())
