from __future__ import annotations

import anyio

from hyperliquid_trading_agent.app.engine.attribution import CandidateOutcomeAttributionService, OUTCOME_WINDOWS_MS
from hyperliquid_trading_agent.app.engine.schemas import AllocationDecision, AlphaCandidate, CouncilReview, EVEstimate


class FakeOutcomeRepository:
    enabled = True

    def __init__(self):
        self.links: dict[str, dict] = {}
        self.outcomes: dict[str, dict] = {}

    async def upsert_candidate_evidence_link(self, link: dict):
        self.links[link["link_id"]] = link
        return link["link_id"]

    async def upsert_candidate_outcome_attribution(self, item: dict):
        self.outcomes[item["attribution_id"]] = item
        return item["attribution_id"]

    async def list_candidate_outcome_attributions(self, **kwargs):
        terminal_state = kwargs.get("terminal_state")
        rows = list(self.outcomes.values())
        if terminal_state:
            rows = [row for row in rows if row.get("terminal_state") == terminal_state]
        return rows[: kwargs.get("limit", 100)]


def _candidate() -> AlphaCandidate:
    return AlphaCandidate(
        candidate_id="cand_attr_1",
        strategy_id="microstructure_ofi_v2",
        strategy_version="2.0.0",
        strategy_family="microstructure_orderflow",
        asset="BTC",
        venue="hyperliquid",
        side="long",
        horizon="5m",
        proposed_entry=100.0,
        stop=99.0,
        targets=[102.0],
        thesis="test",
        invalidation_conditions=["stop"],
        feature_snapshot_id="fs_1",
        regime_snapshot_id="reg_1",
        raw_alpha_score=70,
        confidence=0.6,
        created_at_ms=1_000,
        expires_at_ms=301_000,
    )


def _ev() -> EVEstimate:
    return EVEstimate(
        estimate_id="ev_1",
        candidate_id="cand_attr_1",
        model_version_id="deterministic_fallback_v1",
        p_target=0.4,
        p_stop=0.3,
        p_timeout=0.3,
        expected_favorable_bps=40,
        expected_adverse_bps=20,
        expected_holding_ms=300_000,
        expected_fee_bps=1,
        expected_spread_cost_bps=0.5,
        expected_slippage_bps=0.5,
        expected_market_impact_bps=0,
        expected_funding_cost_bps=0,
        tail_loss_bps=20,
        net_ev_bps=10,
        risk_adjusted_utility=0.5,
        uncertainty=0.2,
        calibration_bucket="test",
        created_at_ms=1_000,
    )


def test_candidate_evidence_link_precreates_delayed_outcome_windows():
    repo = FakeOutcomeRepository()
    service = CandidateOutcomeAttributionService(repo)
    allocation = AllocationDecision(allocation_id="alloc_1", candidate_id="cand_attr_1", status="allocate", allocated_size=1, allocated_notional_usd=100, risk_usd=1, created_at_ms=1_000)
    council = CouncilReview(review_id="council_1", packet_id="packet_1", candidate_id="cand_attr_1", strategy_id="microstructure_ofi_v2", decision="allow_shadow", created_at_ms=1_000)

    async def run():
        return await service.record_candidate_evidence(
            candidate=_candidate(),
            allocation=allocation,
            ev=_ev(),
            risk_decision={"decision_id": "risk_1", "decision": "allow"},
            council_review=council,
            packet={"packet_id": "packet_1"},
            replay_context={"replay_id": "ereplay_1", "status": "passed"},
            created_at_ms=1_000,
        )

    link, outcomes = anyio.run(run)

    assert link.risk_decision_id == "risk_1"
    assert link.council_review_id == "council_1"
    assert link.replay_context_id == "ereplay_1"
    assert len(outcomes) == len(OUTCOME_WINDOWS_MS) == 5
    assert {item.outcome_window for item in outcomes} == set(OUTCOME_WINDOWS_MS)
    assert len(repo.outcomes) == 5


def test_candidate_outcomes_mature_from_mark_prices():
    repo = FakeOutcomeRepository()
    service = CandidateOutcomeAttributionService(repo)

    async def run():
        await service.record_candidate_evidence(candidate=_candidate(), allocation={"allocation_id": "alloc_1", "status": "allocate"}, ev=_ev(), created_at_ms=1_000)
        return await service.refresh_matured_outcomes(marks={"BTC": 101.0}, timestamp_ms=90_000_000)

    matured = anyio.run(run)

    assert len(matured) == 5
    assert all(item.terminal_state == "matured" for item in matured)
    assert all(item.mark_px == 101.0 for item in matured)
    assert all(item.net_return_bps > 0 for item in matured)
