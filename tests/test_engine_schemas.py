from __future__ import annotations

import pytest
from pydantic import ValidationError

from hyperliquid_trading_agent.app.engine.schemas import (
    AllocationDecision,
    AlphaCandidate,
    DebateDecision,
    EVEstimate,
    EvidencePack,
    ExecutionReport,
    FeatureValue,
    ModelVersion,
    NormalizedEvent,
    OrderIntent,
    PositionThesis,
    RegimeVector,
    StrategyPermissions,
    StrategySpec,
)


def test_normalized_event_requires_received_before_computed_and_uppercases_symbols():
    event = NormalizedEvent(
        event_id="evt_1",
        event_type="all_mids",
        asset_class="crypto",
        symbols=["btc", "BTC", " eth "],
        source="hyperliquid",
        provider="ws",
        event_ts_ms=90,
        received_ts_ms=100,
        computed_ts_ms=105,
        payload={"BTC": "100"},
        quality_score=0.9,
        staleness_ms=10,
    )

    assert event.symbols == ["BTC", "ETH"]

    with pytest.raises(ValidationError):
        NormalizedEvent(
            event_id="evt_bad",
            event_type="all_mids",
            asset_class="crypto",
            symbols=["BTC"],
            source="hyperliquid",
            provider="ws",
            received_ts_ms=105,
            computed_ts_ms=100,
        )


def test_feature_value_is_point_in_time_and_uppercases_asset():
    feature = FeatureValue(
        feature_id="feat_1",
        asset="btc",
        feature_group="orderflow",
        feature_name="imbalance_10bps",
        value={"imbalance": 0.12},
        scalar_value=0.12,
        received_ts_ms=100,
        computed_ts_ms=101,
        source_event_id="evt_1",
        source="l2Book",
        version="orderflow_v1",
        quality_score=0.8,
    )

    assert feature.asset == "BTC"


def test_regime_vector_contains_permissions_and_quality_flags():
    regime = RegimeVector(
        regime_snapshot_id="reg_1",
        primary_asset="btc",
        created_at_ms=100,
        as_of_ms=99,
        trend_state="bull",
        trend_confidence=0.72,
        realized_vol_percentile=0.61,
        liquidity_state="normal",
        spread_state="tight",
        funding_stress_z=0.4,
        regime_stability_score=0.66,
        permissions=StrategyPermissions(momentum_allowed=True, news_event_allowed=True),
        feature_refs=["feat_1"],
        raw_feature_refs={"trend": "feat_1"},
        derived_labels={"risk": "constructive"},
        quality_flags=["implied_vol_unavailable"],
    )

    assert regime.primary_asset == "BTC"
    assert regime.permissions.momentum_allowed is True
    assert regime.quality_flags == ["implied_vol_unavailable"]


def test_strategy_spec_contract_normalizes_assets_and_rejects_empty_ids():
    spec = StrategySpec(
        strategy_id="example_alpha_v1",
        version="1.0.0",
        family="test_family",
        supported_assets=["btc", "BTC", " eth "],
        supported_venues=["hyperliquid"],
        supported_horizons=["15m"],
        required_features=["mid"],
        valid_regimes=["bull"],
        max_candidates_per_run=1,
        max_allocation_share_pct=45.0,
        cooldown_ms=1000,
        min_confidence=0.25,
        min_ev_bps=8.0,
        risk_tags=["test"],
    )

    assert spec.supported_assets == ["BTC", "ETH"]

    with pytest.raises(ValidationError):
        StrategySpec(strategy_id=" ", version="1", family="x")


def test_alpha_candidate_lifecycle_and_invalidation_contract():
    candidate = AlphaCandidate(
        candidate_id="cand_1",
        strategy_id="directional_momentum_v2",
        asset="eth",
        asset_class="crypto",
        venue="hyperliquid",
        side="long",
        horizon="30m",
        proposed_entry=100,
        stop=97,
        targets=[106],
        thesis="breakout with confirming orderflow",
        invalidation_conditions=["lose breakout level"],
        feature_snapshot_id="fs_1",
        regime_snapshot_id="reg_1",
        source_event_ids=["evt_1"],
        raw_alpha_score=78,
        confidence=0.64,
        created_at_ms=100,
        expires_at_ms=200,
    )

    assert candidate.asset == "ETH"
    assert candidate.strategy_version == "unknown"
    assert candidate.counts_for_breadth is True

    with pytest.raises(ValidationError):
        AlphaCandidate(
            candidate_id="cand_bad",
            strategy_id="directional_momentum_v2",
            asset="ETH",
            asset_class="crypto",
            venue="hyperliquid",
            side="long",
            horizon="30m",
            proposed_entry=100,
            stop=97,
            targets=[106],
            thesis="missing invalidation",
            feature_snapshot_id="fs_1",
            regime_snapshot_id="reg_1",
            raw_alpha_score=78,
            confidence=0.64,
            created_at_ms=100,
            expires_at_ms=200,
        )


def test_ev_estimate_probability_contract():
    estimate = EVEstimate(
        estimate_id="ev_1",
        candidate_id="cand_1",
        model_version_id="deterministic_fallback_v1",
        p_target=0.45,
        p_stop=0.30,
        p_timeout=0.25,
        expected_favorable_bps=80,
        expected_adverse_bps=45,
        expected_holding_ms=1_800_000,
        expected_fee_bps=4.5,
        expected_spread_cost_bps=1.0,
        expected_slippage_bps=2.0,
        expected_market_impact_bps=0.5,
        expected_funding_cost_bps=0.1,
        tail_loss_bps=60,
        net_ev_bps=11.4,
        risk_adjusted_utility=0.19,
        uncertainty=0.65,
        calibration_bucket="fallback:momentum:crypto",
        created_at_ms=100,
    )

    assert estimate.net_ev_bps == 11.4

    with pytest.raises(ValidationError):
        EVEstimate(
            estimate_id="ev_bad",
            candidate_id="cand_1",
            model_version_id="deterministic_fallback_v1",
            p_target=0.9,
            p_stop=0.9,
            p_timeout=0.1,
            expected_favorable_bps=80,
            expected_adverse_bps=45,
            expected_holding_ms=1,
            expected_fee_bps=1,
            expected_spread_cost_bps=1,
            expected_slippage_bps=1,
            expected_market_impact_bps=1,
            expected_funding_cost_bps=0,
            tail_loss_bps=1,
            net_ev_bps=1,
            risk_adjusted_utility=1,
            uncertainty=0.5,
            calibration_bucket="bad",
            created_at_ms=100,
        )


def test_evidence_pack_and_debate_decision_enforce_no_execution_authority():
    pack = EvidencePack(
        evidence_pack_id="ep_1",
        candidate_id="cand_1",
        strategy_id="directional_momentum_v2",
        asset="btc",
        side="long",
        horizon="30m",
        feature_snapshot_id="fs_1",
        proposed_trade_plan={"entry": 100, "stop": 97, "exchange_actions": []},
        invalidation_conditions=["below 97"],
        created_at_ms=100,
    )
    assert pack.asset == "BTC"

    with pytest.raises(ValidationError):
        EvidencePack(
            evidence_pack_id="ep_bad",
            candidate_id="cand_1",
            strategy_id="directional_momentum_v2",
            asset="BTC",
            side="long",
            horizon="30m",
            feature_snapshot_id="fs_1",
            proposed_trade_plan={"exchange_actions": [{"type": "order"}]},
            created_at_ms=100,
        )

    blocked = DebateDecision(
        debate_decision_id="dd_1",
        evidence_pack_id="ep_1",
        candidate_id="cand_1",
        decision="block",
        confidence_adjustment=-0.5,
        max_size_multiplier=0,
        reason_codes=["stale_orderflow"],
        audit_summary="Blocked by stale orderflow.",
        created_at_ms=101,
    )
    assert blocked.max_size_multiplier == 0

    with pytest.raises(ValidationError):
        DebateDecision(
            debate_decision_id="dd_bad",
            evidence_pack_id="ep_1",
            candidate_id="cand_1",
            decision="block",
            confidence_adjustment=-0.5,
            max_size_multiplier=0.5,
            audit_summary="Invalid block size.",
            created_at_ms=101,
        )


def test_allocation_order_execution_and_position_contracts_are_paper_shadow_only():
    skipped = AllocationDecision(
        allocation_id="alloc_1",
        candidate_id="cand_1",
        status="skip",
        created_at_ms=100,
    )
    assert skipped.allocated_size == 0

    with pytest.raises(ValidationError):
        AllocationDecision(
            allocation_id="alloc_bad",
            candidate_id="cand_1",
            status="skip",
            allocated_size=1,
            created_at_ms=100,
        )

    intent = OrderIntent(
        intent_id="intent_1",
        parent_candidate_id="cand_1",
        portfolio_decision_id="alloc_2",
        asset="hype",
        venue="hyperliquid",
        side="buy",
        order_type="marketable_limit",
        time_in_force="ioc",
        target_size=10,
        target_notional_usd=1000,
        max_slippage_bps=5,
        price_limit=101,
        reduce_only=False,
        post_only=False,
        deadline_ts_ms=200,
        strategy_id="directional_momentum_v2",
        model_version_id="deterministic_fallback_v1",
        config_version_id="cfg_1",
        risk_budget_id="risk_1",
        execution_mode="paper",
        created_at_ms=100,
    )
    assert intent.asset == "HYPE"

    report = ExecutionReport(
        report_id="er_1",
        intent_id="intent_1",
        execution_mode="paper",
        status="filled",
        requested_size=10,
        filled_size=10,
        avg_fill_px=100.1,
        fees_usd=0.45,
        slippage_bps=2,
        adapter="paper",
        assumptions={"fill_model": "instant"},
        created_at_ms=105,
    )
    assert report.status == "filled"

    with pytest.raises(ValidationError):
        ExecutionReport(
            report_id="er_bad",
            intent_id="intent_1",
            execution_mode="paper",
            status="filled",
            requested_size=10,
            filled_size=11,
            avg_fill_px=100.1,
            adapter="paper",
            created_at_ms=105,
        )

    thesis = PositionThesis(
        position_id="pos_1",
        entry_candidate_id="cand_1",
        strategy_id="directional_momentum_v2",
        asset="hype",
        venue="hyperliquid",
        side="long",
        entry_reason="EV positive after allocator and risk checks.",
        expected_horizon="30m",
        stop=97,
        targets=[106],
        invalidation_rules=["lose 97"],
        position_state="open",
        execution_report_ids=["er_1"],
        opened_at_ms=105,
        updated_at_ms=106,
    )
    assert thesis.asset == "HYPE"


def test_model_version_requires_human_approval_metadata_when_approved():
    with pytest.raises(ValidationError):
        ModelVersion(
            model_version_id="model_1",
            model_type="meta_label_classifier",
            artifact_uri="file:///tmp/model.joblib",
            training_data_hash="datahash",
            feature_schema_hash="schemahash",
            status="approved",
            created_at_ms=100,
        )

    approved = ModelVersion(
        model_version_id="model_1",
        model_type="meta_label_classifier",
        artifact_uri="file:///tmp/model.joblib",
        training_data_hash="datahash",
        feature_schema_hash="schemahash",
        status="approved",
        approved_by="human-reviewer",
        approved_at_ms=101,
        created_at_ms=100,
    )
    assert approved.status == "approved"
