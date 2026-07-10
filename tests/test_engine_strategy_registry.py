from __future__ import annotations

import pytest

from hyperliquid_trading_agent.app.engine.alpha.directional import DirectionalMomentumStrategy
from hyperliquid_trading_agent.app.engine.strategy_registry import (
    SHADOW_FULL_CATALOG_ACTIVE_IDS,
    WAVE_1A_NUCLEUS_IDS,
    StrategyRegistry,
    create_default_strategy_registry,
    planned_wave_1a_specs,
)


def test_default_strategy_registry_locks_wave_1a_nucleus_only():
    registry = create_default_strategy_registry()

    runtime_ids = {strategy.strategy_id for strategy in registry.strategies(enabled_only=True)}
    assert runtime_ids == WAVE_1A_NUCLEUS_IDS
    assert registry.get("directional_momentum_v2") is None
    assert registry.spec("directional_momentum_v2") is not None
    assert registry.spec("directional_momentum_v2").enabled is False
    assert registry.spec("directional_momentum_v2").counts_for_breadth is False
    assert registry.spec("equity_options_flow_v1") is not None
    assert registry.spec("equity_options_flow_v1").enabled is False

    metadata = registry.metadata()
    assert metadata["enabled_strategy_count"] == len(WAVE_1A_NUCLEUS_IDS)
    assert metadata["alpha_breadth_count"] == 5
    assert "liquidation_pressure" in metadata["alpha_families"]
    assert "microstructure_orderflow" in metadata["alpha_families"]


def test_shadow_full_catalog_registers_runtime_shadow_only_breadth_without_paper_eligibility():
    registry = create_default_strategy_registry(catalog_mode="shadow_full_catalog")
    catalog = registry.catalog_summary()

    runtime_ids = {strategy.strategy_id for strategy in registry.strategies(enabled_only=True)}
    assert SHADOW_FULL_CATALOG_ACTIVE_IDS <= runtime_ids
    assert catalog["mode"] == "shadow_full_catalog"
    assert catalog["total_specs"] == 30
    assert catalog["runtime_enabled"] == len(SHADOW_FULL_CATALOG_ACTIVE_IDS)
    assert catalog["paper_eligible"] == 0
    assert catalog["shadow_only"] == len(SHADOW_FULL_CATALOG_ACTIVE_IDS)
    assert catalog["spec_only_ids"] == ["equity_options_flow_v1", "legacy_signal_adapter_v1"]
    assert registry.require_spec("cross_venue_lead_lag_v1").enabled is True
    assert registry.require_spec("cross_venue_lead_lag_v1").counts_for_breadth is True
    assert registry.require_spec("cross_venue_lead_lag_v1").metadata["paper_eligible"] is False


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
