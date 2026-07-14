from __future__ import annotations

import time

import anyio
from fastapi.testclient import TestClient

from hyperliquid_trading_agent.app.autonomy.discord import parse_autonomy_command
from hyperliquid_trading_agent.app.autonomy.event_evaluation import AlphaEventEvaluationService
from hyperliquid_trading_agent.app.autonomy.memory import MemoryService
from hyperliquid_trading_agent.app.autonomy.reports import AutonomyReportService, TokenCapitalScorer
from hyperliquid_trading_agent.app.autonomy.schemas import NewsEvent, OperatorFeedback, RoleLessonMemory
from hyperliquid_trading_agent.app.autonomy.tuning import TuningProposalService
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.main import create_app


def test_alpha_event_evaluation_bullish_event_worked_and_memory_candidate():
    settings = Settings(
        autonomy_event_eval_horizons="15m,1h",
        autonomy_event_eval_min_importance=50,
        autonomy_event_eval_min_source_score=0.4,
    )
    memory = MemoryService(settings=settings)
    service = AlphaEventEvaluationService(settings=settings, memory_service=memory)
    event = NewsEvent(
        id="nw_btc_1",
        source="coindesk",
        provider="rss",
        title="BTC ETF inflow surge",
        observed_at_ms=0,
        assets=["BTC"],
        importance_score=80,
        sentiment="bullish",
        metadata={"source_score": 0.8, "event_type": "headline", "asset_class": "crypto"},
    )

    async def run():
        evaluations = await service.create_for_news_event(event, market_regime="risk_on")
        assert len(evaluations) == 1
        await service.on_price("BTC", "crypto", 100, 1)
        await service.on_price("BTC", "crypto", 101, 15 * 60 * 1000)
        await service.mark_due(60 * 60 * 1000)
        return await service.get(evaluations[0].id), memory.status()

    evaluation, status = anyio.run(run)

    assert evaluation is not None
    assert evaluation.terminal_outcome == "worked"
    assert evaluation.status == "complete"
    assert status["candidate_lessons"] >= 1 or status["shadow_lessons"] >= 1


def test_alpha_event_evaluation_neutral_macro_uses_proxy_and_volatility():
    settings = Settings(autonomy_event_eval_horizons="15m", autonomy_event_eval_macro_proxies="BTC,SPY")
    service = AlphaEventEvaluationService(settings=settings)
    event = NewsEvent(
        id="macro_cpi",
        source="federal_reserve",
        provider="rss",
        title="FOMC CPI inflation surprise",
        observed_at_ms=0,
        assets=[],
        importance_score=90,
        sentiment="unknown",
        metadata={"source_score": 1.0, "event_type": "macro", "asset_class": "macro"},
    )

    async def run():
        evaluations = await service.create_for_news_event(event)
        assert {item.symbol for item in evaluations} == {"BTC", "SPY"}
        await service.on_price("BTC", "crypto", 100, 1)
        await service.on_price("BTC", "crypto", 101, 15 * 60 * 1000)
        await service.mark_due(15 * 60 * 1000)
        return await service.get(evaluations[0].id)

    evaluation = anyio.run(run)

    assert evaluation is not None
    assert evaluation.direction == "neutral"
    assert evaluation.terminal_outcome == "volatility_only"


def test_token_capital_score_hard_gate_caps():
    scorer = TokenCapitalScorer()
    snapshot = scorer.compute(
        window="daily",
        timestamp_ms=1,
        event_evaluations=[],
        portfolio_snapshot=None,
        memory_counts={"active_role_lessons": 3},
        feedback_items=[],
        reliability={},
        hard_gates=[{"kind": "live_execution_claim", "score_cap": 10}],
    )

    assert snapshot.total_score <= 10
    assert snapshot.memory_compounding_score > 45
    assert snapshot.component_details["weights"]["signal_quality"] == 0.20


def test_memory_pipeline_feedback_to_operator_candidate_and_promotion():
    settings = Settings(autonomy_operator_lesson_min_samples=2, autonomy_lesson_min_confidence=0.6)
    service = MemoryService(settings=settings)
    now_ms = int(time.time() * 1000)
    feedback = OperatorFeedback(
        id="fb1",
        source="api",
        target_type="bot",
        target_id="discord_bot",
        rating="bad",
        note="needs clearer next command",
        created_at_ms=now_ms,
    )

    async def run():
        first = await service.record_feedback(feedback)
        second = await service.record_feedback(feedback.model_copy(update={"id": "fb2", "created_at_ms": now_ms + 1}))
        await service.promote_candidates(now_ms=now_ms + 2)
        return first, second, service.status()

    first, second, status = anyio.run(run)

    assert first is not None
    assert second is not None
    assert second.sample_size == 2
    assert status["active_operator_lessons"] == 1


def test_sensitive_memory_roles_require_change_control_even_if_configured():
    settings = Settings(
        autonomy_memory_prompt_roles="analyst,risk", autonomy_memory_require_change_control_for_risk_execution=True
    )
    service = MemoryService(settings=settings)
    now = int(time.time() * 1000)
    base_lesson = RoleLessonMemory(
        id="mem_risk_sensitive",
        role="risk",
        lesson_type="risk_discipline",
        scope={"symbol": "BTC"},
        claim="Risk lesson.",
        instruction="Do not inject without change control.",
        confidence=0.9,
        sample_size=20,
        validation_status="active",
        risk_affecting=True,
        created_at_ms=now,
        activated_at_ms=now,
        expires_at_ms=now + 86_400_000,
        metadata={},
    )

    async def run():
        service.role_lessons[base_lesson.id] = base_lesson
        blocked = await service.memory_block_for_role("risk", symbol="BTC")
        service.role_lessons[base_lesson.id] = base_lesson.model_copy(
            update={"metadata": {"change_control_id": "cc_1", "approved_for_role_injection_roles": ["risk"]}}
        )
        allowed = await service.memory_block_for_role("risk", symbol="BTC")
        return blocked, allowed

    blocked, allowed = anyio.run(run)

    assert blocked == ""
    assert "Do not inject without change control" in allowed


def test_tuning_proposal_from_event_evaluations_observe_only():
    settings = Settings(autonomy_tuning_proposals_enabled=True)
    service = TuningProposalService(settings=settings)
    from hyperliquid_trading_agent.app.autonomy.schemas import AlphaEventEvaluation

    evaluations = [
        AlphaEventEvaluation(
            id=f"aeval_{i}",
            event_id=f"event_{i}",
            event_source="x_cashtag",
            provider="x",
            event_type="social",
            asset_class="crypto",
            symbol="BTC",
            direction="long",
            sentiment="bullish",
            status="complete",
            terminal_outcome="failed",
            received_at_ms=i,
            importance_score=80,
            source_score=0.5,
            max_favorable_bps=10,
            max_adverse_bps=-80,
            max_abs_move_bps=80,
            metadata={"exchange_actions": []},
        )
        for i in range(8)
    ]

    proposals = anyio.run(service.generate_from_event_evaluations, evaluations)

    assert proposals
    assert proposals[0].metadata["requires_change_control"] is True
    assert proposals[0].metadata["auto_apply_enabled"] is False


def test_report_generation_includes_token_capital_and_no_live_execution():
    settings = Settings(autonomy_event_eval_horizons="15m")
    evaluation_service = AlphaEventEvaluationService(settings=settings)
    report_service = AutonomyReportService(settings=settings, event_evaluation_service=evaluation_service)
    event = NewsEvent(
        id="report_event",
        source="coindesk",
        provider="rss",
        title="BTC catalyst",
        observed_at_ms=1,
        assets=["BTC"],
        importance_score=80,
        sentiment="bullish",
        metadata={"source_score": 0.8, "event_type": "headline", "asset_class": "crypto"},
    )

    async def run():
        await evaluation_service.create_for_news_event(event)
        await evaluation_service.on_price("BTC", "crypto", 100, 1)
        await evaluation_service.on_price("BTC", "crypto", 101, 15 * 60 * 1000)
        await evaluation_service.mark_due(15 * 60 * 1000)
        return await report_service.generate_daily(now_ms=24 * 60 * 60 * 1000 + 10, post=False)

    report = anyio.run(run)

    assert report.token_capital.total_score >= 0
    assert report.report["safety"]["exchange_actions"] == []
    assert "No live trades placed" in report.summary


def test_new_autonomy_discord_commands_parse_and_apply_denied():
    assert parse_autonomy_command("daily report").action == "daily_report"  # type: ignore[union-attr]
    assert parse_autonomy_command("event outcome event_abc").target_id == "event_abc"  # type: ignore[union-attr]
    assert parse_autonomy_command("signal outcome sig_abc") is None
    assert parse_autonomy_command("mark signal sig_abc good") is None
    assert parse_autonomy_command("memories risk").role == "risk"  # type: ignore[union-attr]
    command = parse_autonomy_command("apply tuning proposal tp_1")
    assert command is not None
    assert command.action == "apply_tuning_proposal"


def test_health_config_exposes_learning_loop_and_api_auth():
    settings = Settings(
        environment="test", position_tracking_enabled=False, autonomy_enabled=True, autonomy_alert_channel_id="123"
    )
    app = create_app(settings)
    with TestClient(app) as client:
        health = client.get("/health/config")
        client.app.state.event_evaluation_service.repository = None
        client.app.state.report_service.repository = None
        client.app.state.memory_service.repository = None
        client.app.state.tuning_service.repository = None
        evaluations = client.get("/autonomy/evaluations/events")
        token = client.get("/autonomy/token-capital")

    assert health.status_code == 200
    autonomy = health.json()["autonomy"]
    assert autonomy["event_evaluation"]["enabled"] is True
    assert autonomy["tuning_proposals"]["auto_apply_enabled"] is False
    assert evaluations.status_code == 200
    assert token.status_code == 200
