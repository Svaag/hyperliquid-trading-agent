from __future__ import annotations

import pytest

from hyperliquid_trading_agent.app.autonomy.schemas import TradeSignal
from hyperliquid_trading_agent.app.autonomy.service import AutonomousTradingLoopService
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.governance.decision_context import DecisionContextRecorder
from hyperliquid_trading_agent.app.governance.schemas import CandidateConfigDiff


@pytest.mark.asyncio
async def test_startup_version_snapshots_are_redacted_and_stable_refs():
    settings = Settings(openrouter_api_key="or-secret", agent_api_bearer_token="agent-secret")
    recorder = DecisionContextRecorder(settings=settings, repository=None, code_version="test-version")

    refs = await recorder.snapshot_startup()

    assert refs["config_version_id"].startswith("cfg_runtime_settings_")
    assert refs["risk_config_version_id"].startswith("risk_risk_settings_")
    assert refs["model_route_version_id"].startswith("model_model_routes_")
    assert refs["prompt_version_ids"]
    assert recorder.config_version is not None
    assert recorder.config_version.payload["openrouter_api_key"] == "[REDACTED]"
    assert recorder.config_version.payload["agent_api_bearer_token"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_decision_context_ref_records_versions_and_selected_prompts():
    settings = Settings()
    recorder = DecisionContextRecorder(settings=settings, repository=None, code_version="test-version")
    await recorder.snapshot_startup()

    context = recorder.new_decision_context(
        run_id="run_1",
        source_type="high_stakes_proposal",
        source_id="proposal_1",
        prompt_names=[f"high_stakes.analyst.{settings.high_stakes_prompt_style}.system", "role_contract.analyst"],
        injected_memory_ids=["mem_1"],
        market_snapshot_refs=["market_map:1"],
        data_freshness={"allMids": "fresh"},
    )

    assert context.run_id == "run_1"
    assert context.config_version_id == recorder.config_version.id  # type: ignore[union-attr]
    assert context.risk_config_version_id == recorder.risk_config_version.id  # type: ignore[union-attr]
    assert len(context.prompt_version_ids) == 2
    assert context.model_route["version_id"] == recorder.model_route_version.id  # type: ignore[union-attr]
    assert context.injected_memory_ids == ["mem_1"]


@pytest.mark.asyncio
async def test_autonomy_signal_attachment_is_audit_only_metadata():
    settings = Settings(autonomy_paper_initial_equity_usd=10_000)
    recorder = DecisionContextRecorder(settings=settings, repository=None, code_version="test-version")
    await recorder.snapshot_startup()
    service = AutonomousTradingLoopService(
        settings=settings,
        repository=None,
        hyperliquid=object(),
        news=object(),
        decision_context_recorder=recorder,
    )
    signal = TradeSignal(
        id="sig_gov_1",
        symbol="BTC",
        side="long",
        signal_type="trend_continuation",
        score=80,
        confidence=0.7,
        created_at_ms=1,
        expires_at_ms=1000,
        entry=100,
        stop=95,
        take_profit=115,
        invalidation="below 95",
        thesis="up",
        risk_plan={"exchange_actions": []},
    )

    attached = await service._attach_decision_context(signal, source_type="autonomy_signal", timestamp_ms=1)

    assert attached.risk_plan["exchange_actions"] == []
    assert signal.metadata == {}
    assert attached.metadata["decision_context"]["config_version_id"] == recorder.config_version.id  # type: ignore[union-attr]
    assert attached.metadata["decision_context"]["metadata"]["paper_only"] is True


def test_candidate_config_diff_schema_blocks_auto_apply_and_requires_evidence():
    diff = CandidateConfigDiff(
        proposal_id="tp_1",
        strategy_id="autonomy_v1",
        change_type="threshold_adjustment",
        current_value={"autonomy_min_signal_score": 75},
        proposed_value={"autonomy_min_signal_score": 80},
        rationale="Paper signals underperformed below 80 score.",
        evidence=["eval_1"],
        expected_effect="Fewer lower-quality signals.",
        risk_direction="relaxes_risk",
        auto_apply_allowed=True,
        created_at_ms=1,
    )

    assert diff.requires_human_approval is True
    assert diff.auto_apply_allowed is False

    with pytest.raises(ValueError, match="must link to evidence"):
        CandidateConfigDiff(
            proposal_id="tp_2",
            strategy_id="autonomy_v1",
            change_type="threshold_adjustment",
            current_value={"autonomy_min_signal_score": 75},
            proposed_value={"autonomy_min_signal_score": 70},
            rationale="No evidence.",
            evidence=[],
            expected_effect="More trades.",
            risk_direction="increases_exposure",
            created_at_ms=1,
        )
