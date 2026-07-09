from __future__ import annotations

from datetime import UTC, datetime

import anyio

from hyperliquid_trading_agent.app.engine.feature_store import FeatureStore, derive_features, derive_rolling_features
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
            "asset_ctxs": [{"funding": "0.0003", "openInterest": "12345", "dayNtlVlm": "987654", "markPx": "101", "oraclePx": "100"}],
        },
    )

    features = {feature.feature_name: feature.scalar_value for feature in derive_features(meta_event)}
    assert features["funding_hourly"] == 0.0003
    assert features["open_interest"] == 12345
    assert features["day_volume_usd"] == 987654
    assert round(features["perp_basis_bps"], 4) == 100.0

    news_event = _event(
        "newswire",
        {"importance_score": 80, "sentiment": "bullish", "confidence": 0.9, "source_score": 0.7},
    )
    news_features = {feature.feature_name: feature.scalar_value for feature in derive_features(news_event)}
    assert news_features["source_consensus_score"] == 0.7

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


def test_store_rollups_match_list_based_rollups():
    points = [
        _feature("mid", 100, ts=0),
        _feature("mid", 101, ts=60_000),
        _feature("mid", 99, ts=180_000),
        _feature("mid", 103, ts=300_000),
        _feature("open_interest", 1_000, ts=0),
        _feature("open_interest", 1_050, ts=120_000),
        _feature("open_interest", 1_200, ts=300_000),
        _feature("top_depth_usd", 1_000_000, ts=0),
        _feature("top_depth_usd", 800_000, ts=300_000),
        _feature("spread_bps", 4, ts=0),
        _feature("spread_bps", 6, ts=300_000),
        _feature("perp_basis_bps", 10, ts=0),
        _feature("perp_basis_bps", 16, ts=300_000),
        _feature("funding_hourly", 0.0001, ts=0),
        _feature("funding_hourly", 0.00013, ts=300_000),
        _feature("day_volume_usd", 100_000_000, ts=300_000),
    ]
    expected = {feature.feature_name: feature.scalar_value for feature in derive_rolling_features(asset="BTC", features=points, as_of_ms=300_000)}

    async def run():
        store = FeatureStore()
        for point in points:
            await store.record(point)
        return {feature.feature_name: feature.scalar_value for feature in store._rollups_for("BTC", as_of_ms=300_000)}

    assert anyio.run(run) == expected


def test_funding_abs_p90_rollup_requires_full_trailing_sample():
    hourly = [_feature("funding_hourly", (idx + 1) * 1e-5 * (-1 if idx % 2 else 1), ts=idx * 3_600_000) for idx in range(25)]
    cutoff = 24 * 3_600_000
    rollups = {feature.feature_name: feature.scalar_value for feature in derive_rolling_features(asset="BTC", features=hourly, as_of_ms=cutoff)}
    assert abs(rollups["funding_abs_p90_24h"] - 23e-5) < 1e-12

    sparse = {feature.feature_name for feature in derive_rolling_features(asset="BTC", features=hourly[:20], as_of_ms=cutoff)}
    assert "funding_abs_p90_24h" not in sparse


def test_wave_catalog_rollups_and_cross_venue_features_are_deterministic_and_gated():
    points = []
    for idx, mid in enumerate([100, 101, 99, 102, 98, 103]):
        ts = idx * 60_000
        points.extend(
            [
                _feature("mid", mid, ts=ts),
                _feature("top_depth_usd", 1_000_000 - idx * 100_000, ts=ts),
                _feature("spread_bps", 4 + idx, ts=ts),
                _feature("perp_basis_bps", 10 + idx * 3, ts=ts),
                _feature("funding_hourly", 0.0001 + idx * 0.00001, ts=ts),
                _feature("day_volume_usd", 100_000_000, ts=ts),
            ]
        )
    rollups = {feature.feature_name: feature.scalar_value for feature in derive_rolling_features(asset="BTC", features=points, as_of_ms=300_000)}

    assert {"range_position", "depth_thinning_5m_pct", "basis_delta_15m_bps", "basis_zscore", "spread_velocity_5m_bps", "funding_change_15m", "volume_liquidity_score"} <= set(rollups)
    assert 0.0 <= rollups["range_position"] <= 1.0
    assert rollups["depth_thinning_5m_pct"] >= 0

    event = _event(
        "cross_venue_market",
        {
            "mid": 100.0,
            "day_volume_usd": 1_000_000,
            "venues": {"aster": {"mid": 101.0, "volume_usd": 2_000_000, "liq_imbalance": 50_000}, "hyperliquid": {"liq_imbalance": 10_000}},
        },
    )
    assert derive_features(event) == []
    cross = {feature.feature_name: feature.scalar_value for feature in derive_features(event, cross_venue_dexes=["aster"])}
    assert {"cross_venue_mid_delta_bps", "cross_venue_volume_imbalance", "cross_venue_liq_imbalance"} <= set(cross)


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
