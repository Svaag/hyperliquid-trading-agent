from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any

from hyperliquid_trading_agent.app.db.repository import Repository
from hyperliquid_trading_agent.app.hyperliquid.asset_resolver import AssetResolver
from hyperliquid_trading_agent.app.hyperliquid.client import HyperliquidClient
from hyperliquid_trading_agent.app.hyperliquid.docs_grounding import HyperliquidDocs
from hyperliquid_trading_agent.app.metrics import TOOL_CALLS
from hyperliquid_trading_agent.app.news.service import NewsService
from hyperliquid_trading_agent.app.paper.schemas import PaperTradeRequest
from hyperliquid_trading_agent.app.paper.simulator import PaperTradeSimulator


@dataclass(frozen=True)
class ToolResult:
    tool: str
    data: Any
    source: str
    timestamp_ms: int
    freshness: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AgentTools:
    """Semantic tool registry for the trading agent."""

    def __init__(
        self,
        hyperliquid: HyperliquidClient,
        news: NewsService,
        repository: Repository | None = None,
        docs: HyperliquidDocs | None = None,
        paper: PaperTradeSimulator | None = None,
    ):
        self.hyperliquid = hyperliquid
        self.news = news
        self.repository = repository
        self.docs = docs or HyperliquidDocs()
        self.paper = paper or PaperTradeSimulator()

    async def get_market_snapshot(self, coins: list[str], intervals: list[str] | None = None, include_l2: bool = False) -> ToolResult:
        async def run() -> dict[str, Any]:
            mids = await self.hyperliquid.all_mids()
            perp_meta_ctxs = await self.hyperliquid.meta_and_asset_ctxs()
            spot_meta_ctxs = await self.hyperliquid.spot_meta_and_asset_ctxs()
            resolver = AssetResolver.from_meta_and_contexts(perp_meta_ctxs, spot_meta_ctxs)
            assets: dict[str, Any] = {}
            for coin in _clean_coins(coins):
                resolved = resolver.resolve(coin)
                if resolved is None:
                    assets[coin.upper()] = {"error": "asset_not_found", "mid": mids.get(coin.upper())}
                    continue
                asset_data: dict[str, Any] = {
                    "coin": resolved.coin,
                    "kind": resolved.kind,
                    "asset_id": resolved.asset_id,
                    "sz_decimals": resolved.sz_decimals,
                    "max_leverage": resolved.max_leverage,
                    "mid": mids.get(resolved.coin) or mids.get(coin.upper()),
                    "context": resolved.context,
                }
                if include_l2:
                    asset_data["l2"] = await self.hyperliquid.l2_book(resolved.coin)
                assets[resolved.coin] = asset_data
            return {"network": self.hyperliquid.settings.hyperliquid_network, "assets": assets}

        return await self._run_tool("get_market_snapshot", {"coins": coins, "include_l2": include_l2}, run, "hyperliquid:/info")

    async def get_asset_context(self, coin: str) -> ToolResult:
        async def run() -> dict[str, Any]:
            perp_meta_ctxs = await self.hyperliquid.meta_and_asset_ctxs()
            spot_meta_ctxs = await self.hyperliquid.spot_meta_and_asset_ctxs()
            resolver = AssetResolver.from_meta_and_contexts(perp_meta_ctxs, spot_meta_ctxs)
            resolved = resolver.resolve(coin)
            if resolved is None:
                return {"coin": coin, "error": "asset_not_found"}
            return {"asset": asdict(resolved)}

        return await self._run_tool("get_asset_context", {"coin": coin}, run, "hyperliquid:/info/metaAndAssetCtxs")

    async def get_order_book(self, coin: str, n_sig_figs: int | None = None, mantissa: int | None = None) -> ToolResult:
        async def run() -> Any:
            return await self.hyperliquid.l2_book(coin.upper(), n_sig_figs=n_sig_figs, mantissa=mantissa)

        return await self._run_tool("get_order_book", {"coin": coin}, run, "hyperliquid:/info/l2Book")

    async def get_candles(self, coin: str, interval: str = "1h", lookback_hours: int = 24) -> ToolResult:
        async def run() -> Any:
            end = int(time.time() * 1000)
            start = end - lookback_hours * 60 * 60 * 1000
            return await self.hyperliquid.candle_snapshot(coin.upper(), interval, start, end)

        return await self._run_tool(
            "get_candles",
            {"coin": coin, "interval": interval, "lookback_hours": lookback_hours},
            run,
            "hyperliquid:/info/candleSnapshot",
        )

    async def get_funding_context(self, coin: str) -> ToolResult:
        async def run() -> dict[str, Any]:
            end = int(time.time() * 1000)
            start = end - 48 * 60 * 60 * 1000
            history = await self.hyperliquid.funding_history(coin.upper(), start, end)
            predicted = await self.hyperliquid.predicted_fundings()
            return {"coin": coin.upper(), "funding_history_48h": history, "predicted_fundings": _filter_predicted(predicted, coin)}

        return await self._run_tool("get_funding_context", {"coin": coin}, run, "hyperliquid:/info/funding")

    async def get_public_user_state(self, address: str) -> ToolResult:
        async def run() -> dict[str, Any]:
            return {
                "perps": await self.hyperliquid.user_state(address),
                "spot": await self.hyperliquid.spot_user_state(address),
                "open_orders": await self.hyperliquid.frontend_open_orders(address),
                "rate_limit": await self.hyperliquid.user_rate_limit(address),
                "note": "Hyperliquid docs require the actual master/subaccount address for account data, not an API wallet address.",
            }

        return await self._run_tool("get_public_user_state", {"address": address}, run, "hyperliquid:/info/user")

    async def get_recent_fills(self, address: str, lookback_hours: int = 24) -> ToolResult:
        async def run() -> Any:
            end = int(time.time() * 1000)
            start = end - lookback_hours * 60 * 60 * 1000
            return await self.hyperliquid.user_fills_by_time(address, start, end)

        return await self._run_tool("get_recent_fills", {"address": address, "lookback_hours": lookback_hours}, run, "hyperliquid:/info/userFillsByTime")

    async def search_hyperliquid_docs(self, query: str) -> ToolResult:
        async def run() -> dict[str, Any]:
            answer = await self.docs.ask(query)
            return asdict(answer)

        return await self._run_tool("search_hyperliquid_docs", {"query": query}, run, "hyperliquid-docs:gitbook")

    async def search_market_news(self, query: str, lookback_hours: int = 24) -> ToolResult:
        async def run() -> dict[str, Any]:
            return (await self.news.current_context(query, lookback_hours=lookback_hours)).to_dict()

        return await self._run_tool("search_market_news", {"query": query, "lookback_hours": lookback_hours}, run, "rss+optional-search+x")

    async def simulate_paper_trade(
        self,
        request: PaperTradeRequest,
        discord_user_id: str | None = None,
        market_snapshot: dict[str, Any] | None = None,
    ) -> ToolResult:
        async def run() -> dict[str, Any]:
            plan = self.paper.plan(request)
            plan_dict = plan.model_dump()
            if self.repository:
                idea_id = await self.repository.record_paper_trade(
                    discord_user_id=discord_user_id,
                    coin=plan.coin,
                    side=plan.side,
                    thesis=request.thesis,
                    plan=plan_dict,
                    market_snapshot=market_snapshot,
                )
                plan_dict["paper_trade_id"] = idea_id
            return plan_dict

        return await self._run_tool("simulate_paper_trade", request.model_dump(), run, "local:paper-simulator")

    async def _run_tool(self, name: str, input_json: dict[str, Any], func, source: str) -> ToolResult:
        started = time.perf_counter()
        timestamp_ms = int(time.time() * 1000)
        try:
            data = await func()
            result = ToolResult(tool=name, data=data, source=source, timestamp_ms=timestamp_ms, freshness="live_or_recent_cache")
            TOOL_CALLS.labels(tool=name, result="ok").inc()
            if self.repository:
                await self.repository.record_tool_call(name, "ok", input_json=input_json, output_json=result.to_dict(), latency_ms=int((time.perf_counter() - started) * 1000))
            return result
        except Exception as exc:
            TOOL_CALLS.labels(tool=name, result="error").inc()
            if self.repository:
                await self.repository.record_tool_call(name, "error", input_json=input_json, output_json={"error": type(exc).__name__}, latency_ms=int((time.perf_counter() - started) * 1000))
            raise


def _clean_coins(coins: list[str]) -> list[str]:
    cleaned = [coin.strip().upper() for coin in coins if coin.strip()]
    return cleaned or ["BTC", "ETH", "SOL"]


def _filter_predicted(predicted: Any, coin: str) -> Any:
    target = coin.upper()
    if isinstance(predicted, list):
        return [item for item in predicted if isinstance(item, list) and item and str(item[0]).upper() == target]
    return predicted
