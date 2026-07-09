from __future__ import annotations

import anyio

from hyperliquid_trading_agent.app.engine.alpha.wave1a import (
    FundingCarryStrategy,
    LegacySignalAdapterStrategy,
    LiquidationCascadeStrategy,
    LiquidationMeanRevertStrategy,
    MicrostructureOFIV2Strategy,
    OIBreakoutStrategy,
    RegimeDefensiveFlatStrategy,
)
from hyperliquid_trading_agent.app.engine.portfolio_allocator import PortfolioAllocator
from hyperliquid_trading_agent.app.engine.schemas import EVEstimate, FeatureSnapshot, RegimeVector
from hyperliquid_trading_agent.app.engine.strategy_registry import create_default_strategy_registry


def _snapshot(features: dict) -> FeatureSnapshot:
    return FeatureSnapshot(snapshot_id="fs_test", asset="BTC", as_of_ms=1_000, features=features)


def _regime(**overrides) -> RegimeVector:
    data = dict(
        regime_snapshot_id="reg_test",
        primary_asset="BTC",
        created_at_ms=1_000,
        as_of_ms=1_000,
        trend_state="range",
        trend_confidence=0.3,
        liquidity_state="normal",
        spread_state="tight",
        volatility_state="normal",
        funding_state="neutral",
        oi_state="flat",
        liquidation_state="calm",
        orderflow_state="balanced",
        news_state="no_event",
        correlation_state="normal",
        session_state="us",
        feature_coverage_pct=100.0,
        regime_label="test=regime",
        regime_stability_score=0.75,
    )
    data.update(overrides)
    return RegimeVector(**data)


def test_wave_1a_registry_has_active_alpha_nucleus():
    registry = create_default_strategy_registry()
    ids = {spec.strategy_id for spec in registry.specs(enabled_only=True)}
    assert {
        "microstructure_ofi_v2",
        "liquidation_cascade_v1",
        "liquidation_mean_revert_v1",
        "funding_carry_v1",
        "oi_breakout_v1",
        "legacy_signal_adapter_v1",
        "regime_defensive_flat_v1",
    } <= ids
    alpha_specs = [spec for spec in registry.alpha_breadth_specs() if not spec.strategy_id.startswith("legacy")]
    assert len(alpha_specs) >= 5
    assert registry.require_spec("legacy_signal_adapter_v1").counts_for_breadth is False
    assert registry.require_spec("regime_defensive_flat_v1").counts_for_breadth is False


def test_wave_1a_alpha_strategies_emit_with_required_metadata():
    timestamp_ms = 10_000
    cases = [
        (
            MicrostructureOFIV2Strategy(),
            _snapshot({"mid": 100.0, "spread_bps": 4.0, "top_imbalance": 0.42, "realized_vol_5m_bps": 40.0}),
            _regime(orderflow_state="buy_pressure"),
            "long",
        ),
        (
            LiquidationCascadeStrategy(),
            _snapshot({"mid": 100.0, "spread_bps": 6.0, "top_imbalance": -0.05, "liq_notional_5m": 300_000.0, "long_vs_short_liq_imbalance_5m": 220_000.0, "confirmed_only_liq_score_5m": 0.7}),
            _regime(liquidation_state="long_flush"),
            "short",
        ),
        (
            LiquidationMeanRevertStrategy(),
            _snapshot({"mid": 100.0, "spread_bps": 6.0, "top_imbalance": 0.05, "liq_notional_5m": 220_000.0, "largest_single_liq_5m": 80_000.0, "long_vs_short_liq_imbalance_5m": 180_000.0}),
            _regime(liquidation_state="long_flush"),
            "long",
        ),
        (
            FundingCarryStrategy(),
            _snapshot({"mid": 100.0, "funding_hourly": 0.0002, "realized_vol_15m_bps": 55.0}),
            _regime(funding_state="positive_extreme"),
            "short",
        ),
        (
            OIBreakoutStrategy(),
            _snapshot({"mid": 100.0, "spread_bps": 5.0, "mid_return_5m_bps": 55.0, "oi_delta_5m_pct": 4.0}),
            _regime(oi_state="expanding", trend_state="bull"),
            "long",
        ),
    ]

    for strategy, snapshot, regime, side in cases:
        candidates = strategy.generate(snapshot, regime, timestamp_ms=timestamp_ms)
        assert len(candidates) == 1, strategy.strategy_id
        candidate = candidates[0]
        assert candidate.side == side
        assert candidate.strategy_version != "unknown"
        assert candidate.strategy_family == strategy.spec.family
        assert candidate.required_features == strategy.spec.required_features
        assert candidate.feature_coverage_pct == 100.0
        assert candidate.counts_for_breadth is True
        assert candidate.risk_tags


def test_wave_1a_missing_required_features_prevents_emission():
    assert MicrostructureOFIV2Strategy().generate(_snapshot({"mid": 100.0}), _regime(orderflow_state="buy_pressure"), timestamp_ms=10_000) == []
    assert FundingCarryStrategy().generate(_snapshot({"mid": 100.0, "funding_hourly": 0.0003}), _regime(), timestamp_ms=10_000) == []


def test_legacy_adapter_dedupes_contract_and_does_not_count_for_breadth():
    signal = {
        "id": "sig_1",
        "symbol": "BTC",
        "side": "long",
        "signal_type": "breakout",
        "score": 72,
        "confidence": 0.66,
        "entry": 100,
        "stop": 97,
        "take_profit": 106,
        "thesis": "legacy breakout",
        "invalidation": "lose level",
        "expires_at_ms": 60_000,
        "metadata": {"horizon": "30m"},
    }
    strategy = LegacySignalAdapterStrategy(signals=[signal, dict(signal)])

    candidates = strategy.generate(_snapshot({"mid": 100.0}), _regime(), timestamp_ms=10_000)

    assert [candidate.candidate_id for candidate in candidates] == [candidates[0].candidate_id]
    assert candidates[0].counts_for_breadth is False
    assert candidates[0].source_integrity["legacy_signal_id"] == "sig_1"


def test_defensive_flat_never_allocates_intent_size():
    candidate = RegimeDefensiveFlatStrategy().generate(
        _snapshot({"mid": 100.0}),
        _regime(volatility_state="extreme", regime_label="volatility=extreme"),
        timestamp_ms=10_000,
    )[0]

    assert candidate.side == "flat"
    assert candidate.counts_for_breadth is False

    async def run():
        ev = EVEstimate(
            estimate_id="ev_flat",
            candidate_id=candidate.candidate_id,
            model_version_id="test",
            p_target=0.3,
            p_stop=0.3,
            p_timeout=0.4,
            expected_favorable_bps=0,
            expected_adverse_bps=1,
            expected_holding_ms=60_000,
            expected_fee_bps=0,
            expected_spread_cost_bps=0,
            expected_slippage_bps=0,
            expected_market_impact_bps=0,
            expected_funding_cost_bps=0,
            tail_loss_bps=1,
            net_ev_bps=100,
            risk_adjusted_utility=1,
            uncertainty=0.1,
            calibration_bucket="test",
            created_at_ms=10_000,
        )
        allocation = await PortfolioAllocator(min_net_ev_bps=-999, min_risk_adjusted_utility=-999).allocate(candidate, ev, regime=_regime(), portfolio_state={"equity_usd": 100_000})
        assert allocation.status == "skip"
        assert "defensive_flat_no_trade" in allocation.reason_codes
        assert allocation.allocated_notional_usd == 0.0

    anyio.run(run)

def test_funding_carry_v1_1_uses_relative_p90_gate_with_cold_start_fallback():
    base = {"mid": 100.0, "realized_vol_15m_bps": 55.0}
    regime = _regime(funding_state="positive_extreme")

    # With p90 evidence: fires when |funding| clears max(floor, p90).
    fired = FundingCarryStrategy().generate(
        _snapshot({**base, "funding_hourly": 6e-5, "funding_abs_p90_24h": 5.5e-5}), regime, timestamp_ms=10_000
    )
    assert len(fired) == 1
    assert fired[0].side == "short"
    assert fired[0].metadata["funding_gate"] == 5.5e-5

    # Below the floor even when p90 is tiny: stays silent.
    assert (
        FundingCarryStrategy().generate(
            _snapshot({**base, "funding_hourly": 2e-5, "funding_abs_p90_24h": 1e-5}), regime, timestamp_ms=10_000
        )
        == []
    )

    # Cold start (no p90 feature yet): legacy 1e-4 gate is unchanged.
    assert FundingCarryStrategy().generate(_snapshot({**base, "funding_hourly": 6e-5}), regime, timestamp_ms=10_000) == []
    assert len(FundingCarryStrategy().generate(_snapshot({**base, "funding_hourly": 2e-4}), regime, timestamp_ms=10_000)) == 1
