from __future__ import annotations

import pytest

from hyperliquid_trading_agent.app.autonomy.schemas import RoleLessonMemory, TradeSignal
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.governance.policy import MemoryPolicyEngine
from hyperliquid_trading_agent.app.governance.review import ReviewWorkflowService
from hyperliquid_trading_agent.app.governance.risk_gateway import RiskGateway
from hyperliquid_trading_agent.app.governance.shadow import ShadowComparisonService


def _lesson(**updates):
    base = dict(
        id="mem_1",
        role="research",
        lesson_type="data_quality",
        scope={},
        claim="Research lesson",
        instruction="Use only sourced catalysts.",
        confidence=0.8,
        sample_size=10,
        validation_status="active",
        created_at_ms=1,
        expires_at_ms=9999999999999,
    )
    base.update(updates)
    return RoleLessonMemory(**base)


def test_memory_policy_blocks_candidate_and_execution_contexts():
    policy = MemoryPolicyEngine()
    candidate = _lesson(memory_status="candidate")
    advisory = _lesson(memory_status="validated_advisory")

    assert not policy.can_inject(candidate, role="research").allowed
    assert policy.can_inject(advisory, role="research").allowed
    assert not policy.can_inject(advisory, role="execution").allowed


@pytest.mark.asyncio
async def test_risk_gateway_rejects_exchange_actions_and_disabled_live():
    signal = TradeSignal(
        id="sig_bad",
        symbol="BTC",
        side="long",
        signal_type="trend",
        score=80,
        confidence=0.7,
        created_at_ms=1,
        expires_at_ms=9999999999999,
        entry=100,
        stop=95,
        invalidation="below 95",
        thesis="up",
        risk_plan={"exchange_actions": [{"type": "order"}]},
    )

    decision = await RiskGateway(settings=Settings()).check_signal(signal, mode="live", asset_class="crypto")

    codes = {item["code"] for item in decision.violations}
    assert decision.decision == "reject"
    assert "exchange_actions_present" in codes
    assert "live_crypto_disabled" in codes


@pytest.mark.asyncio
async def test_shadow_comparison_classifies_tighter_candidate():
    service = ShadowComparisonService(repository=None)
    # With no repository diff, service stays insufficient-data even if metrics are supplied.
    result = await service.compare_candidate_diff("tp_missing", baseline_metrics={"avg_r": 0.1}, candidate_metrics={"avg_r": 0.2})

    assert result.status == "insufficient_data"
    assert result.recommendation == "needs_more_evidence"


@pytest.mark.asyncio
async def test_same_actor_cannot_approve_own_change():
    service = ReviewWorkflowService(repository=None)

    with pytest.raises(ValueError, match="same actor"):
        await service.record_promotion_decision(
            proposal_id="tp_1",
            reviewer="bot",
            decision="approved",
            rationale="self approval should fail",
            proposer_actor="bot",
            approver_actor="bot",
            change_control_id="CC-1",
            rollback_plan_id="rollback_1",
        )
