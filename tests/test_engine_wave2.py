from __future__ import annotations

import pytest

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.alpha.wave2 import WAVE_2_IDS, wave_2_strategy_instances
from hyperliquid_trading_agent.app.engine.bandit import WAVE2_FORBIDDEN_ACTIONS, WAVE2_POLICY_ACTION_SPACE
from hyperliquid_trading_agent.app.engine.schemas import FeatureSnapshot, RegimeVector
from hyperliquid_trading_agent.app.engine.strategy_registry import (
    create_default_strategy_registry,
    planned_wave_2_specs,
)


def test_wave2_defaults_to_integrated_catalog_without_enabling_execution():
    settings = Settings(environment="test", engine_wave2_enabled=True, _env_file=None)
    assert settings.engine_alpha_catalog_mode == "integrated"
    assert settings.engine_shadow_enabled is True
    assert settings.engine_paper_enabled is False
    assert settings.engine_live_enabled is False

    promoted = Settings(
        environment="test",
        engine_wave2_enabled=True,
        engine_paper_enabled=True,
        engine_execution_modes="paper,shadow",
        _env_file=None,
    )
    assert promoted.engine_alpha_catalog_mode == "integrated"
    assert promoted.engine_paper_enabled is True


@pytest.mark.parametrize("legacy_mode", ["wave2_early_shadow", "shadow_full_catalog"])
def test_legacy_wave2_research_catalog_modes_are_removed(legacy_mode: str):
    with pytest.raises(ValueError):
        Settings(
            environment="test",
            engine_alpha_catalog_mode=legacy_mode,
            engine_wave2_enabled=True,
            _env_file=None,
        )


def test_nonintegrated_catalog_registers_first_class_wave2_as_inactive_specs():
    specs = {spec.strategy_id: spec for spec in planned_wave_2_specs()}
    registry = create_default_strategy_registry()

    assert WAVE_2_IDS <= set(specs)
    assert WAVE_2_IDS <= {spec.strategy_id for spec in registry.specs(enabled_only=False)}
    assert not (WAVE_2_IDS & {strategy.strategy_id for strategy in registry.strategies(enabled_only=True)})
    assert all(spec.enabled is False for spec in specs.values())
    assert all(spec.counts_for_breadth is False for spec in specs.values())
    assert all(spec.max_allocation_share_pct == 25.0 for spec in specs.values())
    assert all(spec.metadata["paper_eligible"] is True for spec in specs.values())
    assert all("deferred" not in spec.metadata for spec in specs.values())
    assert {spec.metadata["subwave"] for spec in specs.values()} == {"2A", "2B", "2C"}


def test_wave2_strategy_instances_are_first_class_by_definition():
    strategies = wave_2_strategy_instances()

    assert {strategy.strategy_id for strategy in strategies} == WAVE_2_IDS
    assert all(strategy.spec.enabled and strategy.spec.counts_for_breadth for strategy in strategies)
    assert all(strategy.spec.metadata["activation_scope"] == "paper_shadow" for strategy in strategies)
    assert all(strategy.spec.metadata["paper_eligible"] is True for strategy in strategies)
    assert all(strategy.spec.metadata["integration_status"] == "first_class" for strategy in strategies)
    assert all(strategy.generate(None, None, timestamp_ms=1_000) == [] for strategy in wave_2_strategy_instances())


def test_integrated_catalog_makes_every_wave2_strategy_first_class_and_paper_eligible():
    registry = create_default_strategy_registry(catalog_mode="integrated")
    catalog = registry.catalog_summary()
    wave2_specs = [registry.require_spec(strategy_id) for strategy_id in WAVE_2_IDS]

    assert WAVE_2_IDS <= {strategy.strategy_id for strategy in registry.strategies(enabled_only=True)}
    assert WAVE_2_IDS <= set(catalog["paper_eligible_ids"])
    assert not (WAVE_2_IDS & set(catalog["shadow_only_ids"]))
    assert all(spec.enabled and spec.counts_for_breadth for spec in wave2_specs)
    assert all(spec.metadata["activation_scope"] == "paper_shadow" for spec in wave2_specs)
    assert all(spec.metadata["paper_eligible"] is True for spec in wave2_specs)
    assert all(spec.metadata["operator_promotion_required"] is False for spec in wave2_specs)
    assert all("deferred" not in spec.metadata for spec in wave2_specs)


def test_integrated_wave2_candidate_uses_the_standard_engine_contract():
    registry = create_default_strategy_registry(catalog_mode="integrated")
    strategy = registry.get("cross_venue_lead_lag_v1")
    snapshot = FeatureSnapshot(
        snapshot_id="fs_wave2_integrated",
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
    regime = RegimeVector(
        regime_snapshot_id="reg_wave2_integrated",
        primary_asset="BTC",
        created_at_ms=1_000,
        as_of_ms=1_000,
        regime_label="test=wave2",
    )

    candidate = strategy.generate(snapshot, regime, timestamp_ms=10_000)[0]

    assert candidate.source_integrity == {
        "spec_version": strategy.spec.version,
        "registry_contract": "strategy_spec_v1",
        "activation_scope": "paper_shadow",
        "paper_eligible": True,
        "operator_promotion_required": False,
    }
    assert candidate.metadata["activation_scope"] == "paper_shadow"


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


def test_wave2_candidate_preserves_hip3_equity_identity():
    registry = create_default_strategy_registry(catalog_mode="integrated")
    strategy = registry.get("cross_venue_lead_lag_v1")
    snapshot = FeatureSnapshot(
        snapshot_id="fs_wave2_msft",
        asset="MSFT",
        instrument_id="ins_hip3_msft",
        underlying_id="EQUITY:MSFT",
        venue_id="hyperliquid:xyz",
        provider_symbol="xyz:MSFT",
        as_of_ms=1_000,
        features={
            "mid": 500.0,
            "cross_venue_mid_delta_bps": 8.0,
            "cross_venue_volume_imbalance": 0.25,
            "spread_bps": 4.0,
            "top_depth_usd": 750_000.0,
        },
        metadata={"asset_class": "equity"},
    )
    regime = RegimeVector(
        regime_snapshot_id="reg_wave2_msft",
        primary_asset="MSFT",
        created_at_ms=1_000,
        as_of_ms=1_000,
        regime_label="test=wave2",
    )

    candidate = strategy.generate(snapshot, regime, timestamp_ms=10_000)[0]

    assert strategy.spec.supported_assets == ["*"]
    assert candidate.asset_class == "equity"
    assert candidate.instrument_id == "ins_hip3_msft"
    assert candidate.venue_id == "hyperliquid:xyz"
    assert candidate.provider_symbol == "xyz:MSFT"
