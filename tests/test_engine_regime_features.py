from __future__ import annotations

from datetime import UTC, datetime

from hyperliquid_trading_agent.app.engine.feature_store import derive_features, derive_rolling_features
from hyperliquid_trading_agent.app.engine.regime import RegimeEngine
from hyperliquid_trading_agent.app.engine.schemas import FeatureValue, NormalizedEvent


def _event(event_type: str, payload: dict, *, symbols: list[str] | None = None) -> NormalizedEvent:
    return NormalizedEvent(
        event_id=f"evt_{event_type}",
        event_type=event_type,
        asset_class="crypto",
        symbols=symbols or ["BTC"],
        source="test",
        provider="test",
        received_ts_ms=1_000,
        computed_ts_ms=1_000,
        payload=payload,
    )


def _feature(name: str, value: float, ts: int = 1_000, asset: str = "BTC") -> FeatureValue:
    return FeatureValue(
        feature_id=f"feat_{asset}_{name}_{ts}_{value}",
        asset=asset,
        feature_group="test",
        feature_name=name,
        value={name: value},
        scalar_value=value,
        received_ts_ms=ts,
        computed_ts_ms=ts,
        source="test",
        version="test_v1",
    )


def test_meta_and_liquidation_feature_derivation():
    meta_event = _event(
        "meta_and_asset_ctxs",
        {
            "meta": {"universe": [{"name": "BTC"}]},
            "asset_ctxs": [{"funding": "0.0003", "openInterest": "12345", "dayNtlVlm": "987654"}],
        },
    )

    features = {feature.feature_name: feature.scalar_value for feature in derive_features(meta_event)}
    assert features["funding_hourly"] == 0.0003
    assert features["open_interest"] == 12345
    assert features["day_volume_usd"] == 987654

    liq_event = _event(
        "liquidation_signal",
        {
            "symbol": "BTC",
            "liq_notional_1m": 50_000,
            "liq_notional_5m": 250_000,
            "long_vs_short_liq_imbalance_5m": 180_000,
            "largest_single_liq_5m": 90_000,
            "confirmed_only_liq_score_5m": 0.8,
            "event_count_5m": 7,
            "source_mix_5m": {"confirmed": 1.0},
        },
    )
    names = {feature.feature_name for feature in derive_features(liq_event)}
    assert "liq_event_count_5m" in names
    assert "source_mix_5m" in names


def test_rolling_features_are_deterministic():
    points = [
        _feature("mid", 100, ts=0),
        _feature("mid", 101, ts=60_000),
        _feature("mid", 103, ts=300_000),
        _feature("open_interest", 1_000, ts=0),
        _feature("open_interest", 1_050, ts=120_000),
        _feature("open_interest", 1_200, ts=300_000),
        _feature("open_interest", 1_500, ts=360_000),
    ]

    rollups = {feature.feature_name: feature.scalar_value for feature in derive_rolling_features(asset="BTC", features=points, as_of_ms=360_000)}
    assert round(rollups["mid_return_5m_bps"], 2) == 300.0
    assert round(rollups["oi_delta_5m_pct"], 2) == 50.0
    assert "oi_velocity_z" in rollups


def test_regime_engine_emits_expanded_deterministic_labels():
    as_of_ms = int(datetime(2026, 6, 30, 15, 0, tzinfo=UTC).timestamp() * 1000)
    features = [
        *[_feature("mid", value, ts=as_of_ms - (5 - idx) * 60_000) for idx, value in enumerate([100, 101, 99, 102, 101])],
        _feature("spread_bps", 3.0, ts=as_of_ms),
        _feature("top_depth_usd", 500_000, ts=as_of_ms),
        _feature("top_imbalance", 0.35, ts=as_of_ms),
        *[_feature("funding_hourly", value, ts=as_of_ms - (5 - idx) * 60_000) for idx, value in enumerate([0, 0, 0, 0, 10])],
        *[_feature("open_interest", value, ts=as_of_ms - (5 - idx) * 60_000) for idx, value in enumerate([100, 100, 100, 100, 200])],
        _feature("liq_notional_5m", 500_000, ts=as_of_ms),
        _feature("long_vs_short_liq_imbalance_5m", 400_000, ts=as_of_ms),
        _feature("liq_event_count_5m", 5, ts=as_of_ms),
        _feature("catalyst_pressure", 0.5, ts=as_of_ms),
    ]

    regime = RegimeEngine().compute(features, primary_asset="BTC", as_of_ms=as_of_ms)

    assert regime.liquidation_state == "long_flush"
    assert regime.orderflow_state == "buy_pressure"
    assert regime.news_state == "catalyst"
    assert regime.session_state == "us"
    assert regime.feature_coverage_pct == 100.0
    assert "liquidation=long_flush" in regime.regime_label
    assert regime.derived_labels["regime_label"] == regime.regime_label
