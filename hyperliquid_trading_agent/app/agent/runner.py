from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from hyperliquid_trading_agent.app.agent.guardrails import classify_request
from hyperliquid_trading_agent.app.agent.high_stakes.graph import HighStakesDebateGraph
from hyperliquid_trading_agent.app.agent.high_stakes.routing import route_high_stakes
from hyperliquid_trading_agent.app.agent.high_stakes.schemas import TradeProposalRequest
from hyperliquid_trading_agent.app.agent.model_gateway import ModelGateway, ModelGatewayError
from hyperliquid_trading_agent.app.agent.prompts import DEFAULT_RESPONSE_TEMPLATE, SYSTEM_PROMPT
from hyperliquid_trading_agent.app.agent.tools import AgentTools, ToolResult
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.db.repository import Repository
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.paper.schemas import PaperTradeRequest
from hyperliquid_trading_agent.app.security import redact_text

log = get_logger(__name__)

ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}")
NUMBER_RE = re.compile(r"(?P<label>entry|stop|sl|tp|take profit|equity|account|risk)\s*[:=]?\s*\$?(?P<value>\d+(?:\.\d+)?)", re.IGNORECASE)
COMMON_COINS = {
    "BTC",
    "ETH",
    "SOL",
    "HYPE",
    "DOGE",
    "XRP",
    "BNB",
    "AVAX",
    "SUI",
    "LINK",
    "ARB",
    "OP",
    "LTC",
    "ENA",
    "PUMP",
    "FARTCOIN",
    "PURR",
}


@dataclass(frozen=True)
class AgentContext:
    source: str = "api"
    discord_guild_id: str | None = None
    discord_channel_id: str | None = None
    discord_thread_id: str | None = None
    discord_user_id: str | None = None


@dataclass(frozen=True)
class AgentResponse:
    content: str
    refused: bool = False
    tool_results: list[ToolResult] = field(default_factory=list)
    model_used: str | None = None
    fallback_used: bool = False
    decision_run_id: str | None = None
    proposal_id: str | None = None
    high_stakes: bool = False


class TradingAgentRunner:
    """Trading-support orchestration with semantic tool selection and LLM fallback."""

    def __init__(
        self,
        tools: AgentTools,
        model_gateway: ModelGateway,
        repository: Repository | None = None,
        settings: Settings | None = None,
        high_stakes_graph: HighStakesDebateGraph | None = None,
    ):
        self.tools = tools
        self.model_gateway = model_gateway
        self.repository = repository
        self.settings = settings
        self.high_stakes_graph = high_stakes_graph

    async def answer(self, prompt: str, context: AgentContext | None = None) -> AgentResponse:
        context = context or AgentContext()
        started = time.perf_counter()
        redacted_prompt = redact_text(prompt)
        guardrail = classify_request(prompt)
        if not guardrail.allowed:
            await self._audit("request_refused", context, {"category": guardrail.category, "prompt": redacted_prompt})
            return AgentResponse(content=guardrail.reason, refused=True)

        tool_results: list[ToolResult] = []
        try:
            high_stakes = await self._maybe_answer_high_stakes(prompt=redacted_prompt, context=context, started=started)
            if high_stakes is not None:
                return high_stakes
            tool_results = await self._gather_context(prompt, context)
            model_context = {
                "prompt": redacted_prompt,
                "tool_results": [result.to_dict() for result in tool_results],
                "response_template": DEFAULT_RESPONSE_TEMPLATE,
                "mvp_limits": {
                    "mainnet_execution": "disabled",
                    "paper_trading": "local simulation only",
                    "private_keys": "never accepted",
                },
            }
            try:
                model_response = await self.model_gateway.complete(redacted_prompt, SYSTEM_PROMPT, context=model_context)
                content = _ensure_non_empty(model_response.content, prompt, tool_results)
                await self._audit(
                    "request_answered",
                    context,
                    {
                        "prompt": redacted_prompt,
                        "model": model_response.model,
                        "provider": model_response.provider,
                        "tool_count": len(tool_results),
                        "latency_ms": int((time.perf_counter() - started) * 1000),
                    },
                )
                return AgentResponse(content=content, tool_results=tool_results, model_used=model_response.model)
            except ModelGatewayError as exc:
                log.warning("model_gateway_unavailable_using_fallback", error=str(exc)[:300])
                content = _fallback_answer(prompt, tool_results, model_error=str(exc))
                await self._audit(
                    "request_answered_fallback",
                    context,
                    {"prompt": redacted_prompt, "tool_count": len(tool_results), "model_error": str(exc)[:500]},
                )
                return AgentResponse(content=content, tool_results=tool_results, fallback_used=True)
        except Exception as exc:
            log.exception("agent_answer_failed", error=type(exc).__name__)
            await self._audit("request_failed", context, {"prompt": redacted_prompt, "error": type(exc).__name__})
            return AgentResponse(
                content=(
                    "I hit an infrastructure error while gathering live context. "
                    "No trade was placed. Try again or ask for a narrower market/data request."
                ),
                tool_results=tool_results,
                fallback_used=True,
            )

    async def _maybe_answer_high_stakes(self, prompt: str, context: AgentContext, started: float) -> AgentResponse | None:
        if not self.settings or not self.settings.high_stakes_debate_enabled or self.high_stakes_graph is None:
            return None
        route = route_high_stakes(
            prompt,
            activation_policy=self.settings.high_stakes_activation_policy,
            max_coins=self.settings.high_stakes_max_coins,
        )
        if not route.activate:
            return None
        result = await self.high_stakes_graph.run(
            TradeProposalRequest(prompt=prompt, force_debate=False),
            agent_context={
                "source": context.source,
                "actor": context.discord_user_id or context.source,
                "discord_guild_id": context.discord_guild_id,
                "discord_channel_id": context.discord_channel_id,
                "discord_thread_id": context.discord_thread_id,
            },
        )
        await self._audit(
            "high_stakes_request_answered",
            context,
            {
                "prompt": prompt,
                "status": result.status,
                "run_id": result.run_id,
                "proposal_id": result.proposal_id,
                "latency_ms": int((time.perf_counter() - started) * 1000),
            },
        )
        return AgentResponse(
            content=result.content,
            model_used="multi-agent-debate",
            decision_run_id=result.run_id,
            proposal_id=result.proposal_id,
            high_stakes=True,
        )

    async def _gather_context(self, prompt: str, context: AgentContext) -> list[ToolResult]:
        lowered = prompt.lower()
        results: list[ToolResult] = []
        coins = extract_coins(prompt)
        addresses = ADDRESS_RE.findall(prompt)

        include_l2 = any(term in lowered for term in ["order book", "book", "depth", "bid", "ask", "liquidity"])
        wants_market = any(term in lowered for term in ["trade", "market", "price", "read", "setup", "long", "short", "funding", "liquidation", "book"])
        wants_news = any(term in lowered for term in ["news", "macro", "fed", "fomc", "cpi", "ppi", "rates", "headline", "cycle", "economy"])
        wants_docs = any(term in lowered for term in ["hyperliquid", "api", "docs", "margin", "funding", "order", "tick", "lot", "liquidation"])
        wants_funding = "funding" in lowered
        wants_candles = any(term in lowered for term in ["chart", "candle", "trend", "1h", "4h", "daily"])
        wants_paper = any(term in lowered for term in ["paper", "simulate", "position size", "risk 1", "risk:"])

        if wants_market or coins:
            results.append(await self.tools.get_market_snapshot(coins or ["BTC", "ETH", "SOL"], include_l2=include_l2))
        if wants_funding:
            for coin in coins or ["BTC"]:
                results.append(await self.tools.get_funding_context(coin))
        if wants_candles:
            interval = _infer_interval(prompt)
            for coin in coins or ["BTC"]:
                results.append(await self.tools.get_candles(coin, interval=interval, lookback_hours=_infer_lookback_hours(interval)))
        if addresses:
            for address in addresses[:2]:
                results.append(await self.tools.get_public_user_state(address))
                if any(term in lowered for term in ["fill", "trade history", "recent trades"]):
                    results.append(await self.tools.get_recent_fills(address, lookback_hours=48))
        if wants_news:
            results.append(await self.tools.search_market_news(prompt, lookback_hours=24))
        if wants_docs:
            results.append(await self.tools.search_hyperliquid_docs(prompt))
        if wants_paper:
            request = _parse_paper_trade(prompt, coins=coins)
            if request is not None:
                market_snapshot = results[0].to_dict() if results else None
                results.append(
                    await self.tools.simulate_paper_trade(
                        request,
                        discord_user_id=context.discord_user_id,
                        market_snapshot=market_snapshot,
                    )
                )
        return results

    async def _audit(self, event_type: str, context: AgentContext, payload: dict[str, Any]) -> None:
        if not self.repository:
            return
        await self.repository.record_audit_event(
            event_type,
            actor=context.discord_user_id or context.source,
            payload={
                **payload,
                "source": context.source,
                "discord_guild_id": context.discord_guild_id,
                "discord_channel_id": context.discord_channel_id,
                "discord_thread_id": context.discord_thread_id,
            },
        )


def extract_coins(text: str) -> list[str]:
    candidates = set(re.findall(r"\b[A-Z][A-Z0-9]{1,12}\b", text.upper()))
    coins = [coin for coin in candidates if coin in COMMON_COINS or coin.startswith("@")] 
    # Also catch bitcoin/ethereum spelled out.
    lowered = text.lower()
    if "bitcoin" in lowered:
        coins.append("BTC")
    if "ethereum" in lowered:
        coins.append("ETH")
    return sorted(set(coins))


def _parse_paper_trade(prompt: str, coins: list[str]) -> PaperTradeRequest | None:
    lowered = prompt.lower()
    side = "short" if "short" in lowered else "long" if "long" in lowered else None
    if side is None:
        return None
    values: dict[str, float] = {}
    for match in NUMBER_RE.finditer(prompt):
        label = match.group("label").lower()
        value = float(match.group("value"))
        if label in {"sl"}:
            label = "stop"
        if label == "take profit":
            label = "tp"
        if label == "account":
            label = "equity"
        values[label] = value
    if "entry" not in values or "stop" not in values:
        return None
    equity = values.get("equity", 10_000.0)
    risk_pct = values.get("risk", 1.0)
    return PaperTradeRequest(
        coin=(coins[0] if coins else "BTC"),
        side=side,
        entry=values["entry"],
        stop=values["stop"],
        take_profit=values.get("tp"),
        account_equity_usd=equity,
        risk_pct=risk_pct,
        thesis=prompt[:1000],
    )


def _infer_interval(prompt: str) -> str:
    lowered = prompt.lower()
    for interval in ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d", "1w"]:
        if interval in lowered:
            return interval
    if "daily" in lowered:
        return "1d"
    return "1h"


def _infer_lookback_hours(interval: str) -> int:
    if interval.endswith("m"):
        return 24
    if interval.endswith("h"):
        return 7 * 24
    return 60 * 24


def _ensure_non_empty(content: str, prompt: str, tool_results: list[ToolResult]) -> str:
    return content.strip() or _fallback_answer(prompt, tool_results, model_error="empty model response")


def _fallback_answer(prompt: str, tool_results: list[ToolResult], model_error: str = "") -> str:
    """Human-friendly response when model credentials are absent or all providers fail.

    This is intentionally not a fake analysis. It surfaces the useful facts we can
    determine locally, states the limitation plainly, and asks for the minimum
    extra context needed for a real trade review.
    """
    market_lines: list[str] = []
    paper_lines: list[str] = []
    other_tools: list[str] = []

    for result in tool_results:
        if result.tool == "get_market_snapshot":
            assets = result.data.get("assets", {}) if isinstance(result.data, dict) else {}
            for coin, data in list(assets.items())[:5]:
                if not isinstance(data, dict):
                    continue
                ctx = data.get("context") or {}
                details = [f"{coin} is around {data.get('mid') or 'unknown'}"]
                if ctx.get("markPx") is not None:
                    details.append(f"mark {ctx.get('markPx')}")
                if ctx.get("funding") is not None:
                    details.append(f"funding {ctx.get('funding')}")
                if ctx.get("openInterest") is not None:
                    details.append(f"OI {ctx.get('openInterest')}")
                market_lines.append("; ".join(details) + ".")
        elif result.tool == "simulate_paper_trade" and isinstance(result.data, dict):
            paper_lines.append(
                f"Paper sizing: {result.data.get('side')} {result.data.get('coin')} size "
                f"{result.data.get('size_units'):.6g}, notional ${result.data.get('notional_usd'):.2f}, "
                f"defined risk ${result.data.get('risk_usd'):.2f}."
            )
        else:
            other_tools.append(result.tool)

    lines = [
        "I can only give a lightweight data-only read right now because the reasoning model is not configured. I won't pretend this is a full trade call.",
        "",
    ]
    if market_lines:
        lines.append("Live context I can see:")
        lines.extend(f"- {line}" for line in market_lines)
    elif tool_results:
        lines.append("I pulled some live context, but not enough market structure to make even a useful data-only read.")
    else:
        lines.append("I did not pull live market data for that prompt. Mention a coin such as BTC, ETH, or SOL for a quick snapshot.")

    if paper_lines:
        lines.append("")
        lines.extend(f"- {line}" for line in paper_lines)
    if other_tools:
        lines.append("")
        lines.append("Additional context checked: " + ", ".join(sorted(set(other_tools))) + ".")

    lines.extend(
        [
            "",
            "My practical take: treat this as neutral / needs-more-context, not a long or short signal. Price, funding, and OI alone do not establish trend, invalidation, liquidity quality, or risk/reward.",
            "",
            "For a real answer, give me the coin, timeframe, proposed side, entry, stop, target, and account risk. Example: `BTC 4h long idea, entry 63750, stop 62500, target 67000, risk 1% — debate it.`",
            "",
            "No trade was placed. This service is still non-executing.",
            "",
            "Ops note: the LLM provider key is missing or unavailable, so this fallback avoided discretionary analysis.",
        ]
    )
    return "\n".join(lines)[:4000]
