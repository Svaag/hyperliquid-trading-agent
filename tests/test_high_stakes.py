from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import hyperliquid_trading_agent.app.hyperliquid.sdk_info_client as sdk_info_module
from hyperliquid_trading_agent.app.agent.high_stakes.context import HighStakesContextBuilder
from hyperliquid_trading_agent.app.agent.high_stakes.features import parse_trade_setup
from hyperliquid_trading_agent.app.agent.high_stakes.formatting import format_trade_proposal
from hyperliquid_trading_agent.app.agent.high_stakes.graph import HighStakesDebateGraph
from hyperliquid_trading_agent.app.agent.high_stakes.prompts import ROLES, role_system_prompt
from hyperliquid_trading_agent.app.agent.high_stakes.roles import HighStakesRoleRunner
from hyperliquid_trading_agent.app.agent.high_stakes.routing import route_high_stakes
from hyperliquid_trading_agent.app.agent.high_stakes.schemas import (
    CritiqueResolution,
    DataCoverage,
    DataRequest,
    EndpointEvidence,
    JudgeDecision,
    RoleOpinion,
    RoleScorecard,
    TradeProposal,
    TradeProposalRequest,
    TradeProposalResponse,
    TradeSetupDraft,
)
from hyperliquid_trading_agent.app.agent.model_gateway import StructuredModelResponse
from hyperliquid_trading_agent.app.agent.runner import AgentContext, TradingAgentRunner
from hyperliquid_trading_agent.app.agent.tools import ToolResult
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.hyperliquid.sdk_info_client import SDKInfoClient
from hyperliquid_trading_agent.app.hyperliquid.validation import round_size_to_sz_decimals, validate_hyperliquid_price
from hyperliquid_trading_agent.app.main import create_app


class HighStakesFakeTools:
    async def get_market_snapshot(self, coins, intervals=None, include_l2=False):
        return ToolResult(
            tool="get_market_snapshot",
            data={
                "assets": {
                    coin: {
                        "kind": "perp",
                        "asset_id": 0,
                        "sz_decimals": 5,
                        "max_leverage": 40,
                        "mid": "100",
                        "context": {"markPx": "100", "oraclePx": "100", "funding": "0.00001", "openInterest": "1000000"},
                        "l2": {"levels": [[{"px": "99.9", "sz": "10"}], [{"px": "100.1", "sz": "8"}]]},
                    }
                    for coin in coins
                }
            },
            source="fake-hl",
            timestamp_ms=1,
            freshness="live",
        )

    async def get_candles(self, coin, interval="1h", lookback_hours=24):
        return ToolResult(
            tool="get_candles",
            data=[{"s": coin, "o": "90", "h": "101", "l": "89", "c": "95"}, {"s": coin, "o": "95", "h": "104", "l": "94", "c": "100"}],
            source="fake-hl",
            timestamp_ms=1,
            freshness="live",
        )

    async def get_funding_context(self, coin):
        return ToolResult(
            tool="get_funding_context",
            data={"coin": coin, "funding_history_48h": [{"fundingRate": "0.00001"}]},
            source="fake-hl",
            timestamp_ms=1,
            freshness="live",
        )

    async def search_hyperliquid_docs(self, query):
        return ToolResult(tool="search_hyperliquid_docs", data={"excerpt": "tick/lot docs"}, source="fake-docs", timestamp_ms=1, freshness="live")

    async def search_market_news(self, query, lookback_hours=24):
        return ToolResult(tool="search_market_news", data={"rss": [], "search": [], "x": []}, source="fake-news", timestamp_ms=1, freshness="live")

    async def get_public_user_state(self, address):
        return ToolResult(tool="get_public_user_state", data={"perps": {"marginSummary": {"accountValue": "10000"}}}, source="fake-hl", timestamp_ms=1, freshness="live")


class AskFakeGraph:
    def __init__(self):
        self.prompts = []

    async def run(self, request, agent_context=None):
        assert request.force_debate is False
        self.prompts.append(request.prompt)
        return TradeProposalResponse(
            run_id="run-1",
            proposal_id="proposal-1",
            status="paper_ready",
            content="high stakes answer",
            proposal={"autonomous_execution_allowed": False, "exchange_actions": []},
            judge_decision={},
            rounds=1,
            role_count=3,
        )


class FakeTrackingService:
    def __init__(self):
        self.calls = []

    async def auto_arm(self, plan, *, proposal_id=None, run_id=None):
        self.calls.append((plan, proposal_id, run_id))
        return "tracker-1"


class FakeAsyncSDKInfo:
    async def all_mids(self, dex=""):
        return {"BTC": "100"}

    async def meta_and_asset_ctxs(self):
        return [{"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]}, [{"markPx": "100", "oraclePx": "100"}]]

    async def spot_meta_and_asset_ctxs(self):
        return [{"universe": [], "tokens": []}, []]

    async def l2_snapshot(self, coin):
        return {"coin": coin, "levels": [[{"px": "99.9", "sz": "10"}], [{"px": "100.1", "sz": "10"}]]}

    async def candles_snapshot(self, coin, interval, start_time_ms, end_time_ms):
        return []

    async def funding_history(self, coin, start_time_ms, end_time_ms=None):
        return []

    async def open_orders(self, address, dex=""):
        return []

    async def user_fills_by_time(self, address, start_time_ms, end_time_ms=None, aggregate_by_time=False):
        return []

    async def historical_orders(self, address):
        return []

    async def user_funding_history(self, address, start_time_ms, end_time_ms=None):
        return []

    async def user_fees(self, address):
        return {"feeSchedule": {}}

    async def portfolio(self, address):
        return []

    async def user_non_funding_ledger_updates(self, address, start_time_ms, end_time_ms=None):
        return []

    async def user_twap_slice_fills(self, address):
        return []

    async def user_vault_equities(self, address):
        return []

    async def user_role(self, address):
        return {"role": "user"}

    async def extra_agents(self, address):
        return []

    async def query_sub_accounts(self, address):
        return []


class StructuredFakeGateway:
    def __init__(self, *, request_escalation_once: bool = False):
        self.request_escalation_once = request_escalation_once
        self.judge_calls = 0

    async def complete_structured(self, prompt, system_prompt, response_model, **kwargs):
        if response_model is TradeSetupDraft:
            parsed = TradeSetupDraft(
                coin="BTC",
                side="long",
                entry=100,
                stop=95,
                take_profit=115,
                timeframe="1h",
                thesis="Breakout continuation with defined invalidation.",
                confidence=0.62,
                risk_pct=1,
                account_equity_usd=10_000,
                invalidation="Stop at 95",
            )
        elif response_model is RoleOpinion:
            parsed = RoleOpinion(
                role="review",
                stance="support",
                confidence=0.6,
                summary="Evidence is acceptable for a paper proposal.",
                evidence=[EndpointEvidence(endpoint="l2Book", source="fake", used_by_role="review", summary="Depth acceptable")],
                scorecard=RoleScorecard(evidence_quality=4, directional_edge=3, risk_asymmetry=3, liquidity_quality=3, execution_feasibility=3, invalidation_quality=4, final_score=20),
            )
        elif response_model is JudgeDecision:
            self.judge_calls += 1
            if self.request_escalation_once and self.judge_calls == 1:
                parsed = JudgeDecision(
                    status="needs_more_data",
                    converged=False,
                    revise=False,
                    confidence=0.3,
                    summary="Need deeper liquidity before decision.",
                    data_requests=[DataRequest(reason="Verify depth", endpoint_family="liquidity", coin="BTC", priority="high")],
                )
            else:
                parsed = JudgeDecision(
                    status="paper_ready",
                    converged=True,
                    revise=False,
                    confidence=0.7,
                    summary="Paper proposal approved; no live execution.",
                    accepted_critiques=["Execution remains manual only"],
                    critique_resolutions=[CritiqueResolution(critique="manual only", source_role="execution", severity="high", resolution="accepted", rationale="No Exchange usage")],
                )
        else:  # pragma: no cover - defensive for future schemas
            parsed = response_model()
        return StructuredModelResponse(parsed=parsed, raw_content=parsed.model_dump_json(), model="fake-model", provider="fake", attempts=[])


def test_institutional_prompt_pack_has_rubrics_and_safety():
    for role in ROLES:
        prompt = role_system_prompt(role, "standard")
        assert "endpoint/tool evidence" in prompt
        assert "scorecard" in prompt
        assert "veto" in prompt.lower()
    execution_prompt = role_system_prompt("execution", "aggressive")
    judge_prompt = role_system_prompt("judge", "standard")
    assert "no executable /exchange payload" in execution_prompt
    assert "never relax evidence" in execution_prompt
    assert "Do not average" in judge_prompt or "Do not average".lower() in judge_prompt.lower()


def test_institutional_schema_accepts_evidence_and_rubrics():
    opinion = RoleOpinion(
        role="risk",
        evidence=[EndpointEvidence(endpoint="clearinghouseState", source="hyperliquid-sdk:Info", used_by_role="risk", summary="Account value observed")],
        missing_evidence=["portfolio"],
        scorecard=RoleScorecard(evidence_quality=4, directional_edge=3, risk_asymmetry=4, liquidity_quality=3, execution_feasibility=3, invalidation_quality=5, final_score=22),
        data_requests=[DataRequest(reason="Need portfolio history", endpoint_family="portfolio", priority="high")],
    )
    decision = JudgeDecision(
        critique_resolutions=[CritiqueResolution(critique="thin depth", source_role="adversary", severity="high", resolution="accepted", rationale="L2 top depth is shallow")],
        data_coverage=DataCoverage(required_endpoints=["l2Book"], used_endpoints=["l2Book"], coverage_score=1.0),
    )

    assert opinion.scorecard.final_score == 22
    assert decision.critique_resolutions[0].resolution == "accepted"


def test_high_stakes_route_is_risk_routed_not_every_market_read():
    ordinary = route_high_stakes("What is your BTC market read?")
    setup = route_high_stakes("Plan a paper long BTC entry 100 stop 95 tp 115 equity 10000 risk 1")
    position = route_high_stakes("I entered VVV two days ago at 16.40 and have a stop loss at 15.50; hold or exit?")

    assert ordinary.activate is False
    assert setup.activate is True
    assert {"analyst", "quant", "risk", "adversary", "judge"}.issubset(set(setup.selected_roles))
    assert "VVV" in position.coins
    assert "execution" not in position.selected_roles


def test_parse_trade_setup_handles_position_hold_language():
    setup = parse_trade_setup("I entered VVV two days ago at 16.40 and I have a stop loss at 15.50; hold or exit?")

    assert setup["side"] == "long"
    assert setup["entry"] == 16.40
    assert setup["stop"] == 15.50


def test_general_proposal_format_omits_empty_sections():
    proposal = TradeProposal(
        status="no_trade",
        judge_summary="No setup was supplied.",
        role_summaries={"treasury": "Role not activated for this route.", "adversary": "No adversary summary available."},
    )
    decision = JudgeDecision(status="no_trade", confidence=0.8, summary="No setup was supplied.")

    content = format_trade_proposal(proposal, decision)

    assert "**Decision:**" in content
    assert "Accepted critiques" not in content
    assert "Deferred critiques" not in content
    assert "Treasury" not in content
    assert "Adversary" not in content
    assert "No endpoint coverage summary" not in content


def test_compact_position_review_hides_model_fallback_noise():
    proposal = TradeProposal(
        status="manual_review_required",
        coin="VVV",
        side="long",
        entry=16.4,
        stop=15.5,
        rationale=["Position context: VVV long is -1% from entry."],
        risks=["Model fallback for quant: TimeoutError"],
        warnings=["judge_model_fallback:TimeoutError"],
    )
    decision = JudgeDecision(status="manual_review_required", confidence=0.35, summary="Deterministic high-stakes review completed")

    content = format_trade_proposal(proposal, decision)

    assert "VVV position review" in content
    assert "TimeoutError" not in content
    assert "Deterministic" not in content
    assert "Accepted critiques" not in content


@pytest.mark.asyncio
async def test_high_stakes_graph_produces_non_executing_proposal():
    settings = Settings(high_stakes_debate_enabled=True, high_stakes_max_rounds=2, agent_model_chain="openai:gpt-test")
    graph = HighStakesDebateGraph(
        settings=settings,
        context_builder=HighStakesContextBuilder(HighStakesFakeTools(), settings),  # type: ignore[arg-type]
        role_runner=HighStakesRoleRunner(StructuredFakeGateway(), settings),  # type: ignore[arg-type]
        repository=None,
    )

    response = await graph.run(TradeProposalRequest(prompt="Autonomous long BTC entry 100 stop 95 tp 115 equity 10000 risk 1"))

    assert response.status == "not_executable"
    assert response.proposal["autonomous_execution_allowed"] is False
    assert response.proposal["exchange_actions"] == []
    assert response.proposal["coin"] == "BTC"


@pytest.mark.asyncio
async def test_high_stakes_graph_auto_arms_tracking_plan():
    settings = Settings(high_stakes_debate_enabled=True, high_stakes_max_rounds=1, agent_model_chain="openai:gpt-test")
    tracking = FakeTrackingService()
    graph = HighStakesDebateGraph(
        settings=settings,
        context_builder=HighStakesContextBuilder(HighStakesFakeTools(), settings),  # type: ignore[arg-type]
        role_runner=HighStakesRoleRunner(StructuredFakeGateway(), settings),  # type: ignore[arg-type]
        repository=None,
        tracking_service=tracking,  # type: ignore[arg-type]
    )

    response = await graph.run(TradeProposalRequest(prompt="Long BTC entry 100 stop 95 tp 115 equity 10000 risk 1"))

    assert tracking.calls
    assert response.proposal["tracking_plan"]["metadata"]["auto_arm_status"] == "armed"
    assert response.proposal["tracking_plan"]["metadata"]["tracker_id"] == "tracker-1"


@pytest.mark.asyncio
async def test_high_stakes_graph_escalates_data_once():
    settings = Settings(high_stakes_debate_enabled=True, high_stakes_max_rounds=2, high_stakes_max_data_escalations=1, agent_model_chain="openai:gpt-test")
    gateway = StructuredFakeGateway(request_escalation_once=True)
    graph = HighStakesDebateGraph(
        settings=settings,
        context_builder=HighStakesContextBuilder(HighStakesFakeTools(), settings),  # type: ignore[arg-type]
        role_runner=HighStakesRoleRunner(gateway, settings),  # type: ignore[arg-type]
        repository=None,
    )

    response = await graph.run(TradeProposalRequest(prompt="Long BTC entry 100 stop 95 tp 115 equity 10000 risk 1"))

    assert gateway.judge_calls == 2
    assert response.rounds == 2
    assert "data_escalation:1" in response.warnings


@pytest.mark.asyncio
async def test_context_builder_uses_sdk_data_profiles_and_coverage():
    address = "0x" + "1" * 40
    settings = Settings(high_stakes_info_provider="sdk_preferred", high_stakes_smart_money_addresses=address)
    route = route_high_stakes(f"Review long BTC entry 100 stop 95 for account {address}", forced=True)
    context = await HighStakesContextBuilder(HighStakesFakeTools(), settings, sdk_info=FakeAsyncSDKInfo()).gather(  # type: ignore[arg-type]
        TradeProposalRequest(prompt="Review long BTC entry 100 stop 95", account_address=address),
        route,
    )

    assert "market_baseline" in context.data_profiles
    assert "account_deep" in context.data_profiles
    assert "smart_money_watchlist" in context.data_profiles
    assert "userFees" in context.data_coverage.used_endpoints
    assert context.data_coverage.coverage_score > 0.5


@pytest.mark.asyncio
async def test_sdk_info_client_wraps_official_info_without_exchange():
    class FakeInfo:
        def __init__(self, base_url, skip_ws):
            self.base_url = base_url
            self.skip_ws = skip_ws
            self.ws_manager = None

        def all_mids(self, dex=""):
            return {"BTC": "100", "dex": dex}

        def user_fees(self, address):
            return {"user": address, "feeSchedule": {}}

    client = SDKInfoClient(Settings(), info_factory=FakeInfo)

    assert not hasattr(sdk_info_module, "Exchange")
    assert await client.all_mids() == {"BTC": "100", "dex": ""}
    assert (await client.user_fees("0x" + "A" * 40))["user"] == "0x" + "a" * 40


@pytest.mark.asyncio
async def test_runner_routes_high_stakes_ask_path():
    graph = AskFakeGraph()
    runner = TradingAgentRunner(
        tools=object(),  # type: ignore[arg-type]
        model_gateway=object(),  # type: ignore[arg-type]
        settings=Settings(high_stakes_debate_enabled=True),
        high_stakes_graph=graph,  # type: ignore[arg-type]
    )

    response = await runner.answer("Plan a long BTC entry 100 stop 95 risk 1", context=AgentContext(source="test"))

    assert response.high_stakes is True
    assert response.decision_run_id == "run-1"
    assert response.proposal_id == "proposal-1"
    assert response.content == "high stakes answer"


@pytest.mark.asyncio
async def test_runner_injects_discord_thread_context_for_followup_position_questions():
    graph = AskFakeGraph()
    runner = TradingAgentRunner(
        tools=object(),  # type: ignore[arg-type]
        model_gateway=object(),  # type: ignore[arg-type]
        settings=Settings(high_stakes_debate_enabled=True),
        high_stakes_graph=graph,  # type: ignore[arg-type]
    )

    response = await runner.answer(
        "What about moving stop loss up to above my entry?",
        context=AgentContext(
            source="discord",
            conversation_context="Previous bot post: VVV position review — long from 16.4, stop 15.5. Levels to watch: hard stop 15.5.",
        ),
    )

    assert response.high_stakes is True
    assert graph.prompts
    assert "Prior Discord thread context" in graph.prompts[0]
    assert "VVV position review" in graph.prompts[0]


def test_hyperliquid_validation_helpers():
    assert str(round_size_to_sz_decimals("1.234567", 3)) == "1.234"
    assert validate_hyperliquid_price("12345", sz_decimals=5).valid is True
    assert validate_hyperliquid_price("123456", sz_decimals=5).valid is False


def test_trade_proposal_api_requires_token_outside_dev():
    app = create_app(Settings(environment="prod", high_stakes_debate_enabled=True, agent_api_bearer_token="", discord_bot_token="", position_tracking_enabled=False))
    with TestClient(app) as client:
        response = client.post("/trade/proposals", json={"prompt": "long BTC entry 100 stop 95"})

    assert response.status_code == 503


def test_trade_proposal_api_reports_disabled_in_dev():
    app = create_app(Settings(environment="dev", high_stakes_debate_enabled=False, discord_bot_token="", position_tracking_enabled=False))
    with TestClient(app) as client:
        response = client.post("/trade/proposals", json={"prompt": "long BTC entry 100 stop 95"})

    assert response.status_code == 409


def test_tracking_api_requires_token_outside_dev():
    app = create_app(Settings(environment="prod", agent_api_bearer_token="", discord_bot_token="", position_tracking_enabled=False))
    with TestClient(app) as client:
        response = client.get("/tracking/positions")

    assert response.status_code == 503


def test_health_config_reports_tracking_status():
    app = create_app(Settings(environment="dev", discord_bot_token="", position_tracking_enabled=False))
    with TestClient(app) as client:
        response = client.get("/health/config")

    assert response.status_code == 200
    assert response.json()["position_tracking"]["enabled"] is False
