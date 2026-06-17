from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from hyperliquid_trading_agent.app.agent.high_stakes.features import build_deterministic_features
from hyperliquid_trading_agent.app.agent.high_stakes.schemas import (
    DataCoverage,
    DataRequest,
    HighStakesRoute,
    MarketContextBundle,
    TradeProposalRequest,
)
from hyperliquid_trading_agent.app.agent.tools import AgentTools, ToolResult
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.hyperliquid.sdk_info_client import SDKInfoClient

ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}")
NEWS_TERMS = {"news", "macro", "fed", "fomc", "cpi", "ppi", "rates", "catalyst", "headline", "swing"}


class DataProfile:
    MARKET_BASELINE = "market_baseline"
    MARKET_DEEP = "market_deep"
    ACCOUNT_BASELINE = "account_baseline"
    ACCOUNT_DEEP = "account_deep"
    EXECUTION_READINESS = "execution_readiness"
    SMART_MONEY_WATCHLIST = "smart_money_watchlist"
    RESEARCH = "research"


@dataclass(frozen=True)
class PlannedCall:
    name: str
    endpoints: list[str]
    profile: str
    call: Callable[[], Awaitable[ToolResult]]


class HighStakesContextBuilder:
    def __init__(self, tools: AgentTools, settings: Settings, sdk_info: SDKInfoClient | None = None):
        self.tools = tools
        self.settings = settings
        self.sdk_info = sdk_info

    async def gather(
        self,
        request: TradeProposalRequest,
        route: HighStakesRoute,
        data_requests: list[DataRequest] | None = None,
    ) -> MarketContextBundle:
        prompt = request.prompt
        warnings: list[str] = []
        results: list[ToolResult] = []
        used_endpoints: list[str] = []
        failed_endpoints: list[str] = []
        profiles: list[str] = []
        coins = (route.coins or ["BTC"])[: max(1, self.settings.high_stakes_max_coins)]
        planned_calls = self._planned_calls(prompt, route, coins, request, data_requests=data_requests)
        required_endpoints = _dedupe(endpoint for planned in planned_calls for endpoint in planned.endpoints)
        for planned in planned_calls:
            profiles.append(planned.profile)
            try:
                result = await planned.call()
                results.append(result)
                used_endpoints.extend(planned.endpoints)
            except Exception as exc:
                failed_endpoints.extend(planned.endpoints)
                warnings.append(f"tool_error:{planned.name}:{type(exc).__name__}")
        used_unique = _dedupe(used_endpoints)
        failed_unique = _dedupe(failed_endpoints)
        missing = [endpoint for endpoint in required_endpoints if endpoint not in used_unique]
        coverage = DataCoverage(
            required_endpoints=required_endpoints,
            used_endpoints=used_unique,
            missing_endpoints=missing,
            stale_or_failed_endpoints=failed_unique,
            coverage_score=(len(used_unique) / len(required_endpoints)) if required_endpoints else 1.0,
        )
        tool_dicts = [result.to_dict() for result in results]
        features = build_deterministic_features(
            prompt,
            tool_dicts,
            request_overrides={
                "account_address": request.account_address,
                "account_equity_usd": request.account_equity_usd,
                "risk_pct": request.risk_pct,
                "data_coverage": coverage.model_dump(mode="json"),
            },
        )
        features["data_coverage"] = coverage.model_dump(mode="json")
        return MarketContextBundle(
            prompt=prompt,
            route=route,
            tool_results=tool_dicts,
            features=features,
            data_profiles=_dedupe(profiles),
            data_coverage=coverage,
            warnings=warnings,
        )

    def merge_contexts(
        self,
        first: MarketContextBundle,
        second: MarketContextBundle,
        request: TradeProposalRequest,
    ) -> MarketContextBundle:
        tool_results = first.tool_results + second.tool_results
        required = _dedupe(first.data_coverage.required_endpoints + second.data_coverage.required_endpoints)
        used = _dedupe(first.data_coverage.used_endpoints + second.data_coverage.used_endpoints)
        failed = _dedupe(first.data_coverage.stale_or_failed_endpoints + second.data_coverage.stale_or_failed_endpoints)
        missing = [endpoint for endpoint in required if endpoint not in used]
        coverage = DataCoverage(
            required_endpoints=required,
            used_endpoints=used,
            missing_endpoints=missing,
            stale_or_failed_endpoints=failed,
            coverage_score=(len(used) / len(required)) if required else 1.0,
        )
        features = build_deterministic_features(
            request.prompt,
            tool_results,
            request_overrides={
                "account_address": request.account_address,
                "account_equity_usd": request.account_equity_usd,
                "risk_pct": request.risk_pct,
                "data_coverage": coverage.model_dump(mode="json"),
            },
        )
        features["data_coverage"] = coverage.model_dump(mode="json")
        return MarketContextBundle(
            prompt=request.prompt,
            route=first.route,
            tool_results=tool_results,
            features=features,
            data_profiles=_dedupe(first.data_profiles + second.data_profiles),
            data_coverage=coverage,
            warnings=first.warnings + second.warnings,
        )

    def _planned_calls(
        self,
        prompt: str,
        route: HighStakesRoute,
        coins: list[str],
        request: TradeProposalRequest,
        data_requests: list[DataRequest] | None = None,
    ) -> list[PlannedCall]:
        if data_requests:
            return self._escalation_plan(data_requests, coins, request, route)
        lowered = prompt.lower()
        plan: list[PlannedCall] = []
        plan.append(
            PlannedCall(
                "market_snapshot",
                ["allMids", "metaAndAssetCtxs", "spotMetaAndAssetCtxs", "l2Book"],
                DataProfile.MARKET_BASELINE,
                self._market_snapshot_call(coins),
            )
        )
        interval = _infer_interval(prompt)
        for coin in coins:
            plan.append(
                PlannedCall(
                    f"candles_{coin}",
                    ["candleSnapshot"],
                    DataProfile.MARKET_BASELINE,
                    self._candles_call(coin, interval),
                )
            )
            plan.append(
                PlannedCall(
                    f"funding_{coin}",
                    ["fundingHistory", "predictedFundings"],
                    DataProfile.MARKET_BASELINE,
                    self._funding_call(coin),
                )
            )
            if self.sdk_info and self.settings.high_stakes_info_provider != "rest_only":
                plan.extend(self._sdk_market_verification_calls(coin, interval))
        if any(term in lowered for term in NEWS_TERMS) or "research" in route.selected_roles:
            plan.append(
                PlannedCall(
                    "research_news",
                    ["rss", "search", "x_recent_search"],
                    DataProfile.RESEARCH,
                    self._news_call(prompt, 24),
                )
            )
        if "execution" in route.selected_roles or "risk" in route.selected_roles:
            plan.append(
                PlannedCall(
                    "hyperliquid_docs",
                    ["hyperliquid_docs_tick_lot_margin_funding_orders"],
                    DataProfile.EXECUTION_READINESS,
                    self._docs_call(f"Hyperliquid margin funding order tick lot liquidation docs for: {prompt}"),
                )
            )
        for address in self._allowed_addresses(request, route):
            plan.extend(self._account_calls(address, deep=True))
        for address in self.settings.smart_money_addresses[:3]:
            if ADDRESS_RE.fullmatch(address):
                plan.extend(self._smart_money_calls(address))
        return plan

    def _escalation_plan(
        self,
        data_requests: list[DataRequest],
        coins: list[str],
        request: TradeProposalRequest,
        route: HighStakesRoute,
    ) -> list[PlannedCall]:
        plan: list[PlannedCall] = []
        addresses = self._allowed_addresses(request, route)
        for data_request in data_requests:
            family = data_request.endpoint_family.lower()
            target_coin = data_request.coin or (coins[0] if coins else "BTC")
            if any(term in family for term in ["account", "portfolio", "treasury", "fills", "orders", "fees"]):
                target_addresses = [data_request.address.lower()] if data_request.address and ADDRESS_RE.fullmatch(data_request.address) else addresses
                for address in target_addresses:
                    plan.extend(self._account_calls(address, deep=True))
            elif any(term in family for term in ["research", "news", "social", "macro"]):
                plan.append(
                    PlannedCall(
                        "escalated_research_news",
                        ["rss", "search", "x_recent_search"],
                        DataProfile.RESEARCH,
                        self._news_call(request.prompt, 48),
                    )
                )
            elif any(term in family for term in ["execution", "l2", "liquidity", "book", "market"]):
                plan.append(
                    PlannedCall(
                        f"escalated_market_snapshot_{target_coin}",
                        ["allMids", "metaAndAssetCtxs", "spotMetaAndAssetCtxs", "l2Book"],
                        DataProfile.MARKET_DEEP,
                        self._market_snapshot_call([target_coin]),
                    )
                )
                if self.sdk_info and self.settings.high_stakes_info_provider != "rest_only":
                    plan.extend(self._sdk_market_verification_calls(target_coin, data_request.interval or _infer_interval(request.prompt)))
            elif "funding" in family:
                plan.append(
                    PlannedCall(
                        f"escalated_funding_{target_coin}",
                        ["fundingHistory", "predictedFundings"],
                        DataProfile.MARKET_DEEP,
                        self._funding_call(target_coin),
                    )
                )
            else:
                plan.append(
                    PlannedCall(
                        f"escalated_candles_{target_coin}",
                        ["candleSnapshot"],
                        DataProfile.MARKET_DEEP,
                        self._candles_call(target_coin, data_request.interval or _infer_interval(request.prompt)),
                    )
                )
        return plan

    def _market_snapshot_call(self, coins: list[str]) -> Callable[[], Awaitable[ToolResult]]:
        async def call() -> ToolResult:
            return await self.tools.get_market_snapshot(coins, include_l2=True)

        return call

    def _candles_call(self, coin: str, interval: str) -> Callable[[], Awaitable[ToolResult]]:
        async def call() -> ToolResult:
            return await self.tools.get_candles(coin, interval=interval, lookback_hours=_infer_lookback_hours(interval))

        return call

    def _funding_call(self, coin: str) -> Callable[[], Awaitable[ToolResult]]:
        async def call() -> ToolResult:
            return await self.tools.get_funding_context(coin)

        return call

    def _news_call(self, prompt: str, lookback_hours: int) -> Callable[[], Awaitable[ToolResult]]:
        async def call() -> ToolResult:
            return await self.tools.search_market_news(prompt, lookback_hours=lookback_hours)

        return call

    def _docs_call(self, query: str) -> Callable[[], Awaitable[ToolResult]]:
        async def call() -> ToolResult:
            return await self.tools.search_hyperliquid_docs(query)

        return call

    def _public_user_state_call(self, address: str) -> Callable[[], Awaitable[ToolResult]]:
        async def call() -> ToolResult:
            return await self.tools.get_public_user_state(address)

        return call

    def _sdk_market_verification_calls(self, coin: str, interval: str) -> list[PlannedCall]:
        if self.sdk_info is None:
            return []
        end = int(time.time() * 1000)
        start = end - _infer_lookback_hours(interval) * 60 * 60 * 1000
        return [
            self._sdk_planned("sdk_all_mids", ["allMids"], DataProfile.MARKET_DEEP, self.sdk_info.all_mids),
            self._sdk_planned("sdk_meta_and_asset_ctxs", ["metaAndAssetCtxs"], DataProfile.MARKET_DEEP, self.sdk_info.meta_and_asset_ctxs),
            self._sdk_planned("sdk_spot_meta_and_asset_ctxs", ["spotMetaAndAssetCtxs"], DataProfile.MARKET_DEEP, self.sdk_info.spot_meta_and_asset_ctxs),
            self._sdk_coin_planned(f"sdk_l2_{coin}", ["l2Book"], DataProfile.MARKET_DEEP, self.sdk_info.l2_snapshot, coin),
            self._sdk_coin_planned(f"sdk_candles_{coin}", ["candleSnapshot"], DataProfile.MARKET_DEEP, self.sdk_info.candles_snapshot, coin, interval, start, end),
            self._sdk_coin_planned(f"sdk_funding_{coin}", ["fundingHistory"], DataProfile.MARKET_DEEP, self.sdk_info.funding_history, coin, end - 48 * 60 * 60 * 1000, end),
        ]

    def _account_calls(self, address: str, *, deep: bool) -> list[PlannedCall]:
        calls = [
            PlannedCall(
                f"public_user_state_{address}",
                ["clearinghouseState", "spotClearinghouseState", "frontendOpenOrders", "userRateLimit"],
                DataProfile.ACCOUNT_BASELINE,
                self._public_user_state_call(address),
            )
        ]
        if not deep:
            return calls
        end = int(time.time() * 1000)
        start = end - 7 * 24 * 60 * 60 * 1000
        if self.sdk_info and self.settings.high_stakes_info_provider != "rest_only":
            calls.extend(
                [
                    self._sdk_planned(f"sdk_open_orders_{address}", ["openOrders"], DataProfile.ACCOUNT_DEEP, self.sdk_info.open_orders, address),
                    self._sdk_planned(f"sdk_user_fills_{address}", ["userFillsByTime"], DataProfile.ACCOUNT_DEEP, self.sdk_info.user_fills_by_time, address, start, end, True),
                    self._sdk_planned(f"sdk_historical_orders_{address}", ["historicalOrders"], DataProfile.ACCOUNT_DEEP, self.sdk_info.historical_orders, address),
                    self._sdk_planned(f"sdk_user_funding_{address}", ["userFunding"], DataProfile.ACCOUNT_DEEP, self.sdk_info.user_funding_history, address, start, end),
                    self._sdk_planned(f"sdk_user_fees_{address}", ["userFees"], DataProfile.ACCOUNT_DEEP, self.sdk_info.user_fees, address),
                    self._sdk_planned(f"sdk_portfolio_{address}", ["portfolio"], DataProfile.ACCOUNT_DEEP, self.sdk_info.portfolio, address),
                    self._sdk_planned(
                        f"sdk_ledger_{address}",
                        ["userNonFundingLedgerUpdates"],
                        DataProfile.ACCOUNT_DEEP,
                        self.sdk_info.user_non_funding_ledger_updates,
                        address,
                        start,
                        end,
                    ),
                    self._sdk_planned(f"sdk_twap_fills_{address}", ["userTwapSliceFills"], DataProfile.ACCOUNT_DEEP, self.sdk_info.user_twap_slice_fills, address),
                    self._sdk_planned(f"sdk_vault_equities_{address}", ["userVaultEquities"], DataProfile.ACCOUNT_DEEP, self.sdk_info.user_vault_equities, address),
                    self._sdk_planned(f"sdk_user_role_{address}", ["userRole"], DataProfile.ACCOUNT_DEEP, self.sdk_info.user_role, address),
                    self._sdk_planned(f"sdk_extra_agents_{address}", ["extraAgents"], DataProfile.ACCOUNT_DEEP, self.sdk_info.extra_agents, address),
                    self._sdk_planned(f"sdk_sub_accounts_{address}", ["subAccounts"], DataProfile.ACCOUNT_DEEP, self.sdk_info.query_sub_accounts, address),
                ]
            )
        elif self.settings.high_stakes_info_provider != "sdk_only" and hasattr(self.tools, "hyperliquid"):
            calls.extend(self._rest_account_deep_calls(address, start, end))
        return calls

    def _smart_money_calls(self, address: str) -> list[PlannedCall]:
        calls = self._account_calls(address, deep=True)[:6]
        return [PlannedCall(call.name, call.endpoints, DataProfile.SMART_MONEY_WATCHLIST, call.call) for call in calls]

    def _rest_account_deep_calls(self, address: str, start: int, end: int) -> list[PlannedCall]:
        hyperliquid = self.tools.hyperliquid
        return [
            PlannedCall(f"rest_open_orders_{address}", ["openOrders"], DataProfile.ACCOUNT_DEEP, lambda: _tool_result("rest_open_orders", hyperliquid.open_orders(address), "hyperliquid:/info/openOrders")),
            PlannedCall(
                f"rest_user_fills_{address}",
                ["userFillsByTime"],
                DataProfile.ACCOUNT_DEEP,
                lambda: _tool_result("rest_user_fills_by_time", hyperliquid.user_fills_by_time(address, start, end), "hyperliquid:/info/userFillsByTime"),
            ),
            PlannedCall(
                f"rest_historical_orders_{address}",
                ["historicalOrders"],
                DataProfile.ACCOUNT_DEEP,
                lambda: _tool_result("rest_historical_orders", hyperliquid.historical_orders(address), "hyperliquid:/info/historicalOrders"),
            ),
            PlannedCall(
                f"rest_user_funding_{address}",
                ["userFunding"],
                DataProfile.ACCOUNT_DEEP,
                lambda: _tool_result("rest_user_funding", hyperliquid.user_funding(address, start, end), "hyperliquid:/info/userFunding"),
            ),
            PlannedCall(f"rest_user_fees_{address}", ["userFees"], DataProfile.ACCOUNT_DEEP, lambda: _tool_result("rest_user_fees", hyperliquid.user_fees(address), "hyperliquid:/info/userFees")),
            PlannedCall(f"rest_portfolio_{address}", ["portfolio"], DataProfile.ACCOUNT_DEEP, lambda: _tool_result("rest_portfolio", hyperliquid.portfolio(address), "hyperliquid:/info/portfolio")),
            PlannedCall(
                f"rest_ledger_{address}",
                ["userNonFundingLedgerUpdates"],
                DataProfile.ACCOUNT_DEEP,
                lambda: _tool_result("rest_user_non_funding_ledger_updates", hyperliquid.user_non_funding_ledger_updates(address, start, end), "hyperliquid:/info/userNonFundingLedgerUpdates"),
            ),
            PlannedCall(f"rest_user_rate_limit_{address}", ["userRateLimit"], DataProfile.ACCOUNT_DEEP, lambda: _tool_result("rest_user_rate_limit", hyperliquid.user_rate_limit(address), "hyperliquid:/info/userRateLimit")),
            PlannedCall(f"rest_user_role_{address}", ["userRole"], DataProfile.ACCOUNT_DEEP, lambda: _tool_result("rest_user_role", hyperliquid.user_role(address), "hyperliquid:/info/userRole")),
            PlannedCall(f"rest_vault_equities_{address}", ["userVaultEquities"], DataProfile.ACCOUNT_DEEP, lambda: _tool_result("rest_user_vault_equities", hyperliquid.user_vault_equities(address), "hyperliquid:/info/userVaultEquities")),
            PlannedCall(f"rest_extra_agents_{address}", ["extraAgents"], DataProfile.ACCOUNT_DEEP, lambda: _tool_result("rest_extra_agents", hyperliquid.extra_agents(address), "hyperliquid:/info/extraAgents")),
            PlannedCall(f"rest_sub_accounts_{address}", ["subAccounts"], DataProfile.ACCOUNT_DEEP, lambda: _tool_result("rest_sub_accounts", hyperliquid.sub_accounts(address), "hyperliquid:/info/subAccounts")),
        ]

    def _sdk_planned(self, name: str, endpoints: list[str], profile: str, func: Callable[..., Awaitable[Any]], *args: Any) -> PlannedCall:
        async def call() -> ToolResult:
            data = await func(*args)
            return ToolResult(tool=name, data=data, source=f"hyperliquid-sdk:Info/{','.join(endpoints)}", timestamp_ms=int(time.time() * 1000), freshness="live_or_recent_cache")

        return PlannedCall(name, endpoints, profile, call)

    def _sdk_coin_planned(self, name: str, endpoints: list[str], profile: str, func: Callable[..., Awaitable[Any]], coin: str, *args: Any) -> PlannedCall:
        async def call() -> ToolResult:
            resolved_coin = await self._canonical_market_coin(coin)
            data = await func(resolved_coin, *args)
            return ToolResult(tool=name, data=data, source=f"hyperliquid-sdk:Info/{','.join(endpoints)}", timestamp_ms=int(time.time() * 1000), freshness="live_or_recent_cache")

        return PlannedCall(name, endpoints, profile, call)

    async def _canonical_market_coin(self, coin: str) -> str:
        public_resolver = getattr(self.tools, "canonical_market_coin", None)
        if callable(public_resolver):
            return await public_resolver(coin)
        private_resolver = getattr(self.tools, "_canonical_market_coin", None)
        if callable(private_resolver):
            return await private_resolver(coin)
        return coin

    def _allowed_addresses(self, request: TradeProposalRequest, route: HighStakesRoute) -> list[str]:
        candidates: list[str] = []
        if request.account_address and ADDRESS_RE.fullmatch(request.account_address):
            candidates.append(request.account_address.lower())
        candidates.extend(route.addresses)
        allowlist = self.settings.account_allowlist
        out: list[str] = []
        for address in dict.fromkeys(candidates):
            if allowlist and address.lower() not in allowlist:
                continue
            out.append(address.lower())
        return out[:2]


async def _tool_result(tool: str, awaitable: Awaitable[Any], source: str) -> ToolResult:
    data = await awaitable
    return ToolResult(tool=tool, data=data, source=source, timestamp_ms=int(time.time() * 1000), freshness="live_or_recent_cache")


def summarize_tools(tool_results: list[dict[str, Any]]) -> list[str]:
    summary: list[str] = []
    for result in tool_results:
        summary.append(f"{result.get('tool')} from {result.get('source')} ({result.get('freshness')})")
    return summary


def _dedupe(items) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        value = str(item)
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _infer_interval(prompt: str) -> str:
    lowered = prompt.lower()
    for interval in ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d", "1w"]:
        if interval in lowered:
            return interval
    if "daily" in lowered:
        return "1d"
    if "swing" in lowered:
        return "4h"
    if "scalp" in lowered:
        return "15m"
    return "1h"


def _infer_lookback_hours(interval: str) -> int:
    if interval.endswith("m"):
        return 24
    if interval.endswith("h"):
        return 7 * 24
    return 60 * 24
