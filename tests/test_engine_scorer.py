from __future__ import annotations

from hyperliquid_trading_agent.app.engine.schemas import AlphaCandidate, RegimeVector
from hyperliquid_trading_agent.app.engine.scorer import STRATEGY_EDGE_PRIOR_CAP_BPS, DeterministicEVScorer


def _candidate(edge_bps: float) -> AlphaCandidate:
    return AlphaCandidate(
        candidate_id=f"cand_edge_{edge_bps}",
        strategy_id="test_strategy_v1",
        strategy_version="1.0.0",
        strategy_family="test_family",
        valid_regimes=["range"],
        required_features=["mid"],
        feature_coverage_pct=100.0,
        expected_edge_bps=edge_bps,
        risk_tags=["test"],
        asset="BTC",
        venue="hyperliquid",
        side="long",
        horizon="15m",
        proposed_entry=100.0,
        stop=99.5,
        targets=[101.0],
        thesis="test candidate",
        invalidation_conditions=["test invalidation"],
        feature_snapshot_id="fs_test",
        regime_snapshot_id="reg_test",
        raw_alpha_score=70.0,
        confidence=0.6,
        created_at_ms=1_000,
        expires_at_ms=901_000,
        metadata={"spread_bps": 2.0},
    )


def _regime() -> RegimeVector:
    return RegimeVector(regime_snapshot_id="reg_test", primary_asset="BTC", created_at_ms=1_000, as_of_ms=1_000, regime_stability_score=0.7)


def test_deterministic_scorer_adds_capped_strategy_edge_prior_with_audit_metadata():
    scorer = DeterministicEVScorer()

    no_edge = scorer.score(_candidate(0.0), _regime())
    high_edge = scorer.score(_candidate(50.0), _regime())
    negative_edge = scorer.score(_candidate(-10.0), _regime())

    assert round(high_edge.net_ev_bps - high_edge.metadata["base_net_ev_bps"], 6) == STRATEGY_EDGE_PRIOR_CAP_BPS
    assert high_edge.metadata["strategy_edge_prior_bps"] == STRATEGY_EDGE_PRIOR_CAP_BPS
    assert high_edge.metadata["strategy_edge_prior_source"] == "candidate.expected_edge_bps"
    assert high_edge.metadata["risk_gateway_required"] is True
    assert negative_edge.metadata["strategy_edge_prior_bps"] == 0.0
    assert no_edge.net_ev_bps == no_edge.metadata["base_net_ev_bps"]
