from __future__ import annotations

import pytest

from hyperliquid_trading_agent.app.agent.guardrails import classify_request
from hyperliquid_trading_agent.app.agent.model_gateway import ModelGateway
from hyperliquid_trading_agent.app.agent.runner import AgentContext, TradingAgentRunner, extract_coins
from hyperliquid_trading_agent.app.agent.tools import ToolResult
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.discord_bot import (
    DiscordContext,
    DiscordTradingBot,
    _chunk,
    _is_bot_thread,
    _message_prompt_without_mentions,
)
from hyperliquid_trading_agent.app.hyperliquid.client import HyperliquidClient
from hyperliquid_trading_agent.app.paper.schemas import PaperTradeRequest
from hyperliquid_trading_agent.app.paper.simulator import PaperTradeSimulator


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class FakeHttpClient:
    def __init__(self):
        self.calls = []

    async def post(self, path, json):
        self.calls.append((path, json))
        return FakeResponse({"ok": json})


class FakeTools:
    async def get_market_snapshot(self, coins, intervals=None, include_l2=False):
        return ToolResult(
            tool="get_market_snapshot",
            data={"assets": {coin: {"mid": "100", "context": {"markPx": "100", "funding": "0.00001"}} for coin in coins}},
            source="fake",
            timestamp_ms=1,
            freshness="live",
        )

    async def search_hyperliquid_docs(self, query):
        return ToolResult(tool="search_hyperliquid_docs", data={"excerpt": "docs"}, source="fake-docs", timestamp_ms=1, freshness="live")

    async def get_funding_context(self, coin):
        return ToolResult(tool="get_funding_context", data={"coin": coin}, source="fake", timestamp_ms=1, freshness="live")


class FailingModelGateway:
    async def complete(self, *args, **kwargs):
        from hyperliquid_trading_agent.app.agent.model_gateway import ModelGatewayError

        raise ModelGatewayError("no configured credentials")


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeChoice:
    def __init__(self, content):
        self.message = FakeMessage(content)


class FakeCompletion:
    def __init__(self, content):
        self.choices = [FakeChoice(content)]


def test_model_gateway_provider_mapping(monkeypatch):
    settings = Settings(
        agent_model_chain="openrouter:anthropic/claude-3-5-sonnet,openai:gpt-4o-mini,anthropic:claude-3-haiku,kimi:moonshot-v1-8k",
        openrouter_api_key="or-key",
        openai_api_key="oa-key",
        anthropic_api_key="ant-key",
        kimi_api_key="kimi-key",
    )

    attempts = ModelGateway(settings).configured_attempts()

    assert [item.provider for item in attempts] == ["openrouter", "openai", "anthropic", "kimi"]
    assert attempts[0].litellm_model == "openrouter/anthropic/claude-3-5-sonnet"
    assert attempts[-1].api_base == "https://api.moonshot.ai/v1"
    assert all(item.missing_reason is None for item in attempts)


@pytest.mark.asyncio
async def test_model_gateway_skips_empty_model_response(monkeypatch):
    import litellm

    calls = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs["model"])
        return FakeCompletion("" if len(calls) == 1 else "usable response")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    settings = Settings(agent_model_chain="openrouter:first,openrouter:second", openrouter_api_key="or-key")

    result = await ModelGateway(settings).complete("prompt", "system")

    assert calls == ["openrouter/first", "openrouter/second"]
    assert result.content == "usable response"
    assert result.model == "openrouter:second"
    assert "empty response" in result.attempts[0]


@pytest.mark.asyncio
async def test_hyperliquid_client_posts_expected_payload():
    fake_http = FakeHttpClient()
    client = HyperliquidClient(Settings(), http_client=fake_http)  # type: ignore[arg-type]

    result = await client.l2_book("BTC", n_sig_figs=5, mantissa=2)

    assert fake_http.calls == [("/info", {"type": "l2Book", "coin": "BTC", "nSigFigs": 5, "mantissa": 2})]
    assert result["ok"]["type"] == "l2Book"


@pytest.mark.asyncio
async def test_runner_uses_fallback_when_model_missing():
    runner = TradingAgentRunner(tools=FakeTools(), model_gateway=FailingModelGateway())  # type: ignore[arg-type]

    response = await runner.answer("What is your BTC trade read?", context=AgentContext(source="test"))

    assert response.fallback_used is True
    assert response.tool_results
    assert "BTC" in response.content
    assert "trade" in response.content.lower()
    assert "placed" in response.content.lower()


def test_extract_coins_guardrails_and_discord_helpers():
    assert extract_coins("Compare BTC ETH and random ABC") == ["ABC", "BTC", "ETH"]
    assert extract_coins("read on HYPE?") == ["HYPE"]
    assert classify_request("read on FOOBAR?").allowed is True
    assert _message_prompt_without_mentions("<@123> BTC plan") == "BTC plan"
    assert len(_chunk("a" * 5000, 1800)) == 3


def test_discord_authorization_channel_and_role():
    settings = Settings(discord_allowed_channel_ids="42", discord_allowed_role_ids="7")
    bot = DiscordTradingBot(settings=settings, runner=None)

    assert bot.is_authorized(DiscordContext(guild_id=1, channel_id=42, author_id=3), role_ids={7}) is True
    assert bot.is_authorized(DiscordContext(guild_id=1, channel_id=42, author_id=3), role_ids={8}) is False


class FakeUser:
    id = 123


class FakeThread:
    owner_id = 123
    parent = object()


class FakeOtherThread:
    owner_id = 456
    parent = object()


def test_discord_thread_continuation_detection():
    assert _is_bot_thread(FakeThread(), FakeUser()) is True
    assert _is_bot_thread(FakeOtherThread(), FakeUser()) is False


def test_paper_trade_simulator_plan():
    plan = PaperTradeSimulator().plan(
        PaperTradeRequest(coin="BTC", side="long", entry=100, stop=95, take_profit=115, account_equity_usd=10_000, risk_pct=1)
    )

    assert plan.risk_usd == 100
    assert plan.size_units == 20
    assert plan.notional_usd == 2000
