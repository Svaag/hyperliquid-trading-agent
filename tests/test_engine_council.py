from __future__ import annotations

from hyperliquid_trading_agent.app.engine.council import (
    DeterministicCouncil,
    build_candidate_trade_packet,
    council_allows_execution,
)
from hyperliquid_trading_agent.app.engine.schemas import (
    AllocationDecision,
    AlphaCandidate,
    EVEstimate,
    OrderIntent,
    RegimeVector,
)


def _candidate() -> AlphaCandidate:
    return AlphaCandidate(
        candidate_id="cand_1",
        strategy_id="microstructure_ofi_v2",
        strategy_version="2.0.0",
        strategy_family="microstructure_orderflow",
        valid_regimes=["buy_pressure"],
        required_features=["mid"],
        feature_coverage_pct=100,
        risk_tags=["microstructure"],
        asset="BTC",
        asset_class="crypto",
        venue="hyperliquid",
        side="long",
        horizon="5m",
        proposed_entry=100,
        stop=99,
        targets=[102],
        thesis="test",
        invalidation_conditions=["stop"],
        feature_snapshot_id="fs_1",
        regime_snapshot_id="reg_1",
        raw_alpha_score=80,
        confidence=0.6,
        created_at_ms=1_000,
        expires_at_ms=120_000,
    )


def _ev(candidate_id: str = "cand_1") -> EVEstimate:
    return EVEstimate(
        estimate_id="ev_1",
        candidate_id=candidate_id,
        model_version_id="deterministic_fallback_v1",
        p_target=0.4,
        p_stop=0.3,
        p_timeout=0.3,
        expected_favorable_bps=100,
        expected_adverse_bps=50,
        expected_holding_ms=300_000,
        expected_fee_bps=4,
        expected_spread_cost_bps=1,
        expected_slippage_bps=1,
        expected_market_impact_bps=0,
        expected_funding_cost_bps=0,
        tail_loss_bps=60,
        net_ev_bps=20,
        risk_adjusted_utility=0.3,
        uncertainty=0.2,
        calibration_bucket="test",
        created_at_ms=1_000,
    )


def _allocation(candidate_id: str = "cand_1") -> AllocationDecision:
    return AllocationDecision(
        allocation_id="alloc_1",
        candidate_id=candidate_id,
        status="allocate",
        allocated_size=1,
        allocated_notional_usd=100,
        risk_usd=1,
        created_at_ms=1_000,
        metadata={"strategy_id": "microstructure_ofi_v2", "strategy_family": "microstructure_orderflow", "asset": "BTC"},
    )


def _intent(mode: str = "shadow") -> OrderIntent:
    return OrderIntent(
        intent_id="intent_1",
        parent_candidate_id="cand_1",
        portfolio_decision_id="alloc_1",
        asset="BTC",
        asset_class="crypto",
        venue="hyperliquid",
        side="buy",
        order_type="marketable_limit",
        time_in_force="ioc",
        target_size=1,
        target_notional_usd=100,
        max_slippage_bps=5,
        price_limit=100,
        strategy_id="microstructure_ofi_v2",
        model_version_id="deterministic_fallback_v1",
        config_version_id="test",
        risk_budget_id="default",
        execution_mode=mode,  # type: ignore[arg-type]
        deadline_ts_ms=120_000,
        created_at_ms=1_000,
    )


def _regime(**overrides) -> RegimeVector:
    data = dict(
        regime_snapshot_id="reg_1",
        primary_asset="BTC",
        created_at_ms=1_000,
        as_of_ms=1_000,
        trend_state="range",
        liquidity_state="normal",
        spread_state="tight",
        orderflow_state="buy_pressure",
        regime_label="orderflow=buy_pressure",
        feature_coverage_pct=100,
    )
    data.update(overrides)
    return RegimeVector(**data)


def test_candidate_trade_packet_and_shadow_council_review_allow_missing_replay_for_observation():
    candidate = _candidate()
    packet = build_candidate_trade_packet(candidate=candidate, ev=_ev(), allocation=_allocation(), order_intent=_intent("shadow"), risk_decision={"decision": "allow", "violations": []}, created_at_ms=1_000)

    review = DeterministicCouncil().review(packet, _regime())

    assert packet.strategy_version == "2.0.0"
    assert review.decision == "allow_shadow"
    assert "latest_replay_pass_or_advisory_pass" in review.required_evidence
    assert council_allows_execution(review, execution_mode="shadow") is True


def test_paper_council_rejects_missing_replay():
    candidate = _candidate()
    packet = build_candidate_trade_packet(candidate=candidate, ev=_ev(), allocation=_allocation(), order_intent=_intent("paper"), risk_decision={"decision": "allow", "violations": []}, created_at_ms=1_000)

    review = DeterministicCouncil().review(packet, _regime())

    assert review.decision == "reject"
    assert "latest_replay_missing_or_failed" in review.vetoes
    assert council_allows_execution(review, execution_mode="paper") is False


def test_council_hard_vetoes_risk_reject_and_regime_mismatch():
    candidate = _candidate().model_copy(update={"valid_regimes": ["sell_pressure"]})
    packet = build_candidate_trade_packet(
        candidate=candidate,
        ev=_ev(),
        allocation=_allocation(),
        order_intent=_intent("shadow"),
        risk_decision={"decision": "reject", "violations": [{"code": "spread_too_wide"}]},
        created_at_ms=1_000,
    )

    review = DeterministicCouncil().review(packet, _regime(orderflow_state="buy_pressure"))

    assert review.decision == "reject"
    assert "risk_gateway_reject" in review.vetoes
    assert "strategy_invalid_for_current_regime" in review.vetoes
