from __future__ import annotations

from hyperliquid_trading_agent.app.engine.alpha.wave1c import (
    BasisReversionStrategy,
    FundingSqueezeStrategy,
    MicrostructureAbsorptionStrategy,
    NewsImpulseStrategy,
    RangeRotationStrategy,
    VolatilityCompressionBreakoutStrategy,
)
from hyperliquid_trading_agent.app.engine.schemas import FeatureSnapshot, RegimeVector
from hyperliquid_trading_agent.app.engine.strategy_registry import WAVE_1C_DETERMINISTIC_IDS, create_default_strategy_registry, planned_wave_1c_specs


def _snapshot(features: dict) -> FeatureSnapshot:
    return FeatureSnapshot(snapshot_id="fs_wave1c", asset="BTC", as_of_ms=1_000, features=features)


def _regime(**overrides) -> RegimeVector:
    data = dict(
        regime_snapshot_id="reg_wave1c",
        primary_asset="BTC",
        created_at_ms=1_000,
        as_of_ms=1_000,
        trend_state="range",
        trend_confidence=0.3,
        liquidity_state="normal",
        spread_state="tight",
        volatility_state="normal",
        funding_state="neutral",
        oi_state="expanding",
        liquidation_state="calm",
        orderflow_state="balanced",
        news_state="catalyst",
        correlation_state="normal",
        session_state="us",
        feature_coverage_pct=100.0,
        regime_label="test=wave1c",
        regime_stability_score=0.75,
    )
    data.update(overrides)
    return RegimeVector(**data)


def test_wave_1c_specs_are_registered_disabled_until_explicit_enablement():
    specs = {spec.strategy_id: spec for spec in planned_wave_1c_specs()}

    assert WAVE_1C_DETERMINISTIC_IDS <= set(specs)
    assert all(specs[strategy_id].enabled is False for strategy_id in WAVE_1C_DETERMINISTIC_IDS)
    assert specs["range_rotation_v1"].enabled is False
    assert specs["volatility_compression_breakout_v1"].enabled is False

    default_registry = create_default_strategy_registry()
    assert all(default_registry.spec(strategy_id).enabled is False for strategy_id in WAVE_1C_DETERMINISTIC_IDS)
    enabled_registry = create_default_strategy_registry(enable_wave_1c=True)
    assert WAVE_1C_DETERMINISTIC_IDS <= {strategy.strategy_id for strategy in enabled_registry.strategies(enabled_only=True)}


def test_wave_1c_active_strategies_emit_replayable_candidates_with_contract_metadata():
    cases = [
        (
            MicrostructureAbsorptionStrategy(),
            _snapshot({"mid": 100.0, "spread_bps": 4.0, "top_imbalance": 0.48, "mid_return_5m_bps": 3.0}),
            "short",
        ),
        (
            FundingSqueezeStrategy(),
            _snapshot({"mid": 100.0, "funding_hourly": 0.0003, "oi_delta_5m_pct": 4.0, "mid_return_5m_bps": -35.0, "spread_bps": 6.0}),
            "short",
        ),
        (
            BasisReversionStrategy(),
            _snapshot({"mid": 100.0, "perp_basis_bps": 30.0, "realized_vol_15m_bps": 35.0, "spread_bps": 4.0}),
            "short",
        ),
        (
            NewsImpulseStrategy(),
            _snapshot({"mid": 100.0, "catalyst_pressure": 0.8, "source_consensus_score": 0.75, "mid_return_5m_bps": 45.0, "day_volume_usd": 100_000_000.0}),
            "long",
        ),
    ]

    for strategy, snapshot, expected_side in cases:
        candidates = strategy.generate(snapshot, _regime(), timestamp_ms=10_000)
        assert len(candidates) == 1, strategy.strategy_id
        candidate = candidates[0]
        assert candidate.side == expected_side
        assert candidate.strategy_version == strategy.spec.version
        assert candidate.strategy_family == strategy.spec.family
        assert candidate.required_features == strategy.spec.required_features
        assert candidate.feature_coverage_pct == 100.0
        assert candidate.metadata["regime_label"] == "test=wave1c"


def test_wave_1c_optional_strategies_are_inert_until_replay_depth_exists():
    snapshot = _snapshot({"mid": 100.0, "range_position": 0.1, "realized_vol_15m_bps": 20.0, "spread_bps": 3.0, "top_depth_usd": 1_000_000, "mid_return_5m_bps": 30.0})

    assert RangeRotationStrategy().generate(snapshot, _regime(), timestamp_ms=10_000) == []
    assert VolatilityCompressionBreakoutStrategy().generate(snapshot, _regime(), timestamp_ms=10_000) == []
