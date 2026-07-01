from __future__ import annotations

import pytest

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.alpha.wave2 import WAVE_2_DEFERRED_IDS, wave_2_strategy_instances
from hyperliquid_trading_agent.app.engine.bandit import WAVE2_FORBIDDEN_ACTIONS, WAVE2_POLICY_ACTION_SPACE
from hyperliquid_trading_agent.app.engine.schemas import FeatureSnapshot, RegimeVector
from hyperliquid_trading_agent.app.engine.strategy_registry import create_default_strategy_registry, planned_wave_2_specs


def test_wave2_flag_remains_rejected_until_wave1d_real_evidence():
    with pytest.raises(ValueError, match="ENGINE_WAVE2_ENABLED"):
        Settings(environment="test", engine_wave2_enabled=True)


def test_wave2_specs_are_registered_disabled_and_do_not_count_for_breadth():
    specs = {spec.strategy_id: spec for spec in planned_wave_2_specs()}
    registry = create_default_strategy_registry()

    assert WAVE_2_DEFERRED_IDS <= set(specs)
    assert WAVE_2_DEFERRED_IDS <= {spec.strategy_id for spec in registry.specs(enabled_only=False)}
    assert not (WAVE_2_DEFERRED_IDS & {strategy.strategy_id for strategy in registry.strategies(enabled_only=True)})
    assert all(spec.enabled is False for spec in specs.values())
    assert all(spec.counts_for_breadth is False for spec in specs.values())
    assert all(spec.max_allocation_share_pct == 0.0 for spec in specs.values())
    assert {spec.metadata["subwave"] for spec in specs.values()} == {"2A", "2B", "2C"}


def test_wave2_strategy_instances_are_inert_until_operator_enablement():
    assert {strategy.strategy_id for strategy in wave_2_strategy_instances()} == WAVE_2_DEFERRED_IDS
    assert all(strategy.generate(None, None, timestamp_ms=1_000) == [] for strategy in wave_2_strategy_instances())


def test_shadow_full_catalog_enables_wave2_research_candidate_generation_only_via_registry():
    registry = create_default_strategy_registry(catalog_mode="shadow_full_catalog")
    strategy = registry.get("cross_venue_lead_lag_v1")
    snapshot = FeatureSnapshot(
        snapshot_id="fs_wave2",
        asset="BTC",
        as_of_ms=1_000,
        features={
            "mid": 100.0,
            "cross_venue_mid_delta_bps": 7.0,
            "cross_venue_volume_imbalance": 0.35,
            "spread_bps": 4.0,
            "top_depth_usd": 500_000.0,
        },
    )
    regime = RegimeVector(regime_snapshot_id="reg_wave2", primary_asset="BTC", created_at_ms=1_000, as_of_ms=1_000, regime_label="test=wave2")

    candidates = strategy.generate(snapshot, regime, timestamp_ms=10_000)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.strategy_id == "cross_venue_lead_lag_v1"
    assert candidate.source_integrity["activation_scope"] == "shadow_only"
    assert candidate.source_integrity["paper_eligible"] is False
    assert candidate.counts_for_breadth is True


def test_wave2d_policy_action_space_is_constrained_report_only():
    assert WAVE2_POLICY_ACTION_SPACE == [
        "strategy_weight_bucket",
        "candidate_quota_bucket",
        "min_confidence_threshold",
        "min_ev_threshold",
        "cooldown_bucket",
        "no_trade",
        "shadow_only_experiment",
    ]
    assert "place_orders" in WAVE2_FORBIDDEN_ACTIONS
    assert "bypass_RiskGateway" in WAVE2_FORBIDDEN_ACTIONS
