from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, cast

from hyperliquid_trading_agent.app.db.repository import Repository
from hyperliquid_trading_agent.app.hyperliquid.asset_resolver import AssetResolver
from hyperliquid_trading_agent.app.hyperliquid.client import HyperliquidClient
from hyperliquid_trading_agent.app.hyperliquid.docs_grounding import HyperliquidDocs
from hyperliquid_trading_agent.app.markets.resolution import (
    COMMODITY_SYMBOLS,
    AssetCandidate,
    ResolutionPlan,
    parse_market_intent,
    route_market_intent,
)
from hyperliquid_trading_agent.app.metrics import TOOL_CALLS
from hyperliquid_trading_agent.app.news.service import NewsService
from hyperliquid_trading_agent.app.paper.schemas import PaperTradeRequest
from hyperliquid_trading_agent.app.paper.simulator import PaperTradeSimulator
from hyperliquid_trading_agent.app.tradfi.client import TradFiClient
from hyperliquid_trading_agent.app.tradfi.options_flow import OptionsFlowDetector


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
        tradfi: TradFiClient | None = None,
        options_flow: OptionsFlowDetector | None = None,
    ):
        self.hyperliquid = hyperliquid
        self.news = news
        self.repository = repository
        self.docs = docs or HyperliquidDocs()
        self.paper = paper or PaperTradeSimulator()
        self.tradfi = tradfi
        self.options_flow = options_flow
        self._hip3_symbol_cache: dict[str, str | None] = {}

    async def get_market_snapshot(self, coins: list[str], intervals: list[str] | None = None, include_l2: bool = False) -> ToolResult:
        async def run() -> dict[str, Any]:
            mids = await self.hyperliquid.all_mids()
            perp_meta_ctxs = await self.hyperliquid.meta_and_asset_ctxs()
            spot_meta_ctxs = await self.hyperliquid.spot_meta_and_asset_ctxs()
            resolver = AssetResolver.from_meta_and_contexts(perp_meta_ctxs, spot_meta_ctxs)
            assets: dict[str, Any] = {}
            checked_hip3_dexs: set[str] = set()
            for coin in _clean_coins(coins):
                resolved = resolver.resolve(coin)
                source_mids = mids
                if resolved is None:
                    resolved, source_mids, dex = await self._resolve_hip3_asset(coin)
                    if dex:
                        checked_hip3_dexs.add(dex)
                if resolved is None:
                    assets[coin.upper()] = {
                        "error": "asset_not_found",
                        "mid": mids.get(coin.upper()),
                        "checked_hip3_dexs": sorted(checked_hip3_dexs),
                    }
                    continue
                asset_data: dict[str, Any] = {
                    "query_symbol": coin.upper(),
                    "coin": resolved.coin,
                    "kind": resolved.kind,
                    "dex": resolved.dex,
                    "asset_id": resolved.asset_id,
                    "sz_decimals": resolved.sz_decimals,
                    "max_leverage": resolved.max_leverage,
                    "mid": _mid_for_resolved(source_mids, resolved.coin, coin),
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
                resolved, _mids, _dex = await self._resolve_hip3_asset(coin)
            if resolved is None:
                return {"coin": coin, "error": "asset_not_found"}
            return {"asset": asdict(resolved)}

        return await self._run_tool("get_asset_context", {"coin": coin}, run, "hyperliquid:/info/metaAndAssetCtxs")

    async def get_order_book(self, coin: str, n_sig_figs: int | None = None, mantissa: int | None = None) -> ToolResult:
        async def run() -> Any:
            resolved_coin = await self._canonical_market_coin(coin)
            return await self.hyperliquid.l2_book(resolved_coin, n_sig_figs=n_sig_figs, mantissa=mantissa)

        return await self._run_tool("get_order_book", {"coin": coin}, run, "hyperliquid:/info/l2Book")

    async def get_candles(self, coin: str, interval: str = "1h", lookback_hours: int = 24) -> ToolResult:
        async def run() -> Any:
            resolved_coin = await self._resolve_market_coin(coin)
            if resolved_coin is None:
                return {"coin": coin.upper(), "error": "asset_not_found", "candles": []}
            end = int(time.time() * 1000)
            start = end - lookback_hours * 60 * 60 * 1000
            return await self.hyperliquid.candle_snapshot(resolved_coin, interval, start, end)

        return await self._run_tool(
            "get_candles",
            {"coin": coin, "interval": interval, "lookback_hours": lookback_hours},
            run,
            "hyperliquid:/info/candleSnapshot",
        )

    async def get_funding_context(self, coin: str) -> ToolResult:
        async def run() -> dict[str, Any]:
            resolved_coin = await self._resolve_market_coin(coin)
            if resolved_coin is None:
                return {"coin": coin.upper(), "query_symbol": coin.upper(), "error": "asset_not_found", "funding_history_48h": [], "predicted_fundings": []}
            end = int(time.time() * 1000)
            start = end - 48 * 60 * 60 * 1000
            history = await self.hyperliquid.funding_history(resolved_coin, start, end)
            predicted = await self.hyperliquid.predicted_fundings()
            return {"coin": resolved_coin, "query_symbol": coin.upper(), "funding_history_48h": history, "predicted_fundings": _filter_predicted(predicted, resolved_coin)}

        return await self._run_tool("get_funding_context", {"coin": coin}, run, "hyperliquid:/info/funding")

    async def resolve_market_intent(self, prompt: str) -> ToolResult:
        async def run() -> dict[str, Any]:
            plan = await self._build_market_resolution_plan(prompt)
            return plan.model_dump(mode="json")

        return await self._run_tool("resolve_market_intent", {"prompt": prompt[:500]}, run, "local:intent-router")

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

    # --- TradFi tools -----------------------------------------------------------

    async def get_stock_quote(self, symbol: str) -> ToolResult:
        async def run() -> Any:
            if self.tradfi is None:
                return {"error": "tradfi_not_available"}
            quote = await self.tradfi.get_latest_quote(symbol)
            trade = await self.tradfi.get_latest_trade(symbol)
            return {
                "symbol": symbol.upper(),
                "quote": quote.model_dump(mode="json") if quote else None,
                "last_trade": trade.model_dump(mode="json") if trade else None,
            }
        return await self._run_tool("get_stock_quote", {"symbol": symbol}, run, self._tradfi_source())

    async def get_stock_bars(self, symbol: str, timeframe: str = "1d", lookback_hours: int = 120) -> ToolResult:
        async def run() -> Any:
            if self.tradfi is None:
                return {"error": "tradfi_not_available"}
            bars = await self.tradfi.get_bars(symbol, timeframe=timeframe, lookback_hours=lookback_hours)
            return {
                "symbol": symbol.upper(),
                "timeframe": timeframe,
                "bars": [b.model_dump(mode="json") for b in bars],
                "count": len(bars),
            }
        return await self._run_tool("get_stock_bars", {"symbol": symbol, "timeframe": timeframe, "lookback_hours": lookback_hours}, run, self._tradfi_source())

    async def get_options_chain(self, underlying: str, expiration: str | None = None) -> ToolResult:
        async def run() -> Any:
            if self.tradfi is None:
                return {"error": "tradfi_not_available"}
            from datetime import date
            exp_date = date.fromisoformat(expiration) if expiration else None
            chain = await self.tradfi.get_options_chain(underlying, expiration=exp_date)
            return {
                "underlying": chain.underlying,
                "underlying_price": chain.underlying_price,
                "expiration": str(chain.expiration_date) if chain.expiration_date else None,
                "contracts_count": len(chain.contracts),
                "calls": [c.model_dump(mode="json") for c in chain.calls[:20]],
                "puts": [c.model_dump(mode="json") for c in chain.puts[:20]],
            }
        return await self._run_tool("get_options_chain", {"underlying": underlying, "expiration": expiration}, run, self._tradfi_source())

    async def get_earnings_calendar(self, symbol: str | None = None) -> ToolResult:
        async def run() -> Any:
            if self.tradfi is None:
                return {"error": "tradfi_not_available"}
            if not symbol:
                return {"note": "earnings_calendar_requires_symbol", "hint": "Use get_corporate_actions or search_market_news for earnings data"}
            actions = await self.tradfi.get_corporate_actions([symbol])
            return {
                "symbol": symbol.upper(),
                "corporate_actions": {k: [a.model_dump(mode="json") for a in v] for k, v in actions.items()},
                "note": "Check for upcoming ex_dividend and earnings dates in corporate actions. For full earnings calendar, use search_market_news.",
            }
        return await self._run_tool("get_earnings_calendar", {"symbol": symbol}, run, self._tradfi_source())

    async def get_corporate_actions(self, symbol: str) -> ToolResult:
        async def run() -> Any:
            if self.tradfi is None:
                return {"error": "tradfi_not_available"}
            actions = await self.tradfi.get_corporate_actions([symbol])
            return {
                "symbol": symbol.upper(),
                "actions": [a.model_dump(mode="json") for a in actions.get(symbol.upper(), [])],
                "count": len(actions.get(symbol.upper(), [])),
            }
        return await self._run_tool("get_corporate_actions", {"symbol": symbol}, run, self._tradfi_source())

    async def get_market_snapshot_tradfi(self, symbols: list[str]) -> ToolResult:
        async def run() -> Any:
            if self.tradfi is None:
                return {"error": "tradfi_not_available"}
            snaps = await self.tradfi.get_snapshots(symbols)
            return {
                sym: snap.model_dump(mode="json") for sym, snap in snaps.items()
            }
        return await self._run_tool("get_market_snapshot_tradfi", {"symbols": symbols}, run, self._tradfi_source())

    # --- Analysis tools ---------------------------------------------------------

    async def analyze_options_flow(self, symbol: str) -> ToolResult:
        async def run() -> Any:
            if self.tradfi is None or self.options_flow is None:
                return {"error": "tradfi_or_options_flow_not_available"}
            chain = await self.tradfi.get_options_chain(symbol)
            if not chain.contracts:
                return {"symbol": symbol.upper(), "flow_events": [], "note": "No options data available"}
            events = self.options_flow.detect(chain)
            return {
                "symbol": symbol.upper(),
                "underlying_price": chain.underlying_price,
                "total_contracts_scanned": len(chain.contracts),
                "flow_events": [e.model_dump(mode="json") for e in events[:10]],
                "count": len(events),
            }
        return await self._run_tool("analyze_options_flow", {"symbol": symbol}, run, self._tradfi_source())

    async def compare_stocks(self, symbols: list[str]) -> ToolResult:
        async def run() -> Any:
            if self.tradfi is None:
                return {"error": "tradfi_not_available"}
            snaps = await self.tradfi.get_snapshots(symbols)
            comparison: dict[str, Any] = {}
            for sym in symbols:
                snap = snaps.get(sym.upper())
                if snap is None:
                    comparison[sym.upper()] = {"error": "not_found"}
                    continue
                comparison[sym.upper()] = {
                    "price": snap.daily_bar.close if snap.daily_bar else None,
                    "change_pct": snap.change_pct,
                    "volume": snap.daily_bar.volume if snap.daily_bar else None,
                    "bid": snap.latest_quote.bid_price if snap.latest_quote else None,
                    "ask": snap.latest_quote.ask_price if snap.latest_quote else None,
                }
            return comparison
        return await self._run_tool("compare_stocks", {"symbols": symbols}, run, self._tradfi_source())

    async def sector_heatmap(self, sector: str | None = None) -> ToolResult:
        async def run() -> Any:
            if self.tradfi is None:
                return {"error": "tradfi_not_available"}
            # Key sector ETFs as proxies
            sector_etfs = {
                "technology": "XLK", "financials": "XLF", "healthcare": "XLV",
                "energy": "XLE", "consumer": "XLY", "industrials": "XLI",
                "materials": "XLB", "utilities": "XLU", "real_estate": "XLRE",
                "communication": "XLC", "broad": "SPY",
            }
            to_snapshot = list(sector_etfs.values()) if sector is None else [sector_etfs.get(sector.lower(), "SPY")]
            snaps = await self.tradfi.get_snapshots(to_snapshot)
            result = {}
            for name, etf in sector_etfs.items():
                if sector and name != sector.lower():
                    continue
                snap = snaps.get(etf.upper())
                if snap:
                    result[name] = {
                        "etf": etf, "price": snap.daily_bar.close if snap.daily_bar else None,
                        "change_pct": snap.change_pct,
                    }
            return result
        return await self._run_tool("sector_heatmap", {"sector": sector}, run, self._tradfi_source())

    async def stock_screener(self, criteria: str = "") -> ToolResult:
        async def run() -> Any:
            if self.tradfi is None:
                return {"error": "tradfi_not_available"}
            # Simple screener: snapshot a set of popular tickers based on criteria keywords.
            # The LLM is expected to interpret the results, not the tool.
            # Add some common stock tickers for screening
            common_stocks = ["AAPL", "NVDA", "MSFT", "AMZN", "GOOGL", "META", "TSLA", "BRK.B", "JPM", "V", "JNJ", "WMT", "PG", "MA", "UNH", "HD", "BAC", "DIS", "ADBE", "NFLX", "CRM", "AMD", "INTC", "QCOM", "TXN"]
            # Filter by sector keywords if any
            criteria_lower = criteria.lower()
            if "tech" in criteria_lower or "semiconductor" in criteria_lower:
                to_check = ["AAPL", "NVDA", "MSFT", "AMD", "INTC", "QCOM", "TXN", "ADBE", "CRM", "NFLX"]
            elif "finance" in criteria_lower or "bank" in criteria_lower:
                to_check = ["JPM", "BAC", "V", "MA", "GS", "MS", "C", "WFC"]
            elif "health" in criteria_lower or "pharma" in criteria_lower:
                to_check = ["JNJ", "UNH", "PFE", "MRK", "ABBV", "LLY"]
            else:
                to_check = common_stocks[:10]
            snaps = await self.tradfi.get_snapshots(to_check)
            results = []
            for sym in to_check:
                snap = snaps.get(sym.upper())
                if snap is None:
                    continue
                results.append({
                    "symbol": sym,
                    "price": snap.daily_bar.close if snap.daily_bar else None,
                    "change_pct": snap.change_pct,
                    "volume": snap.daily_bar.volume if snap.daily_bar else None,
                })
            return {"criteria": criteria, "results": sorted(results, key=lambda r: r.get("volume") or 0, reverse=True)}
        return await self._run_tool("stock_screener", {"criteria": criteria}, run, self._tradfi_source())

    async def estimate_option_greeks(self, symbol: str, strike: float, expiration: str, option_type: str) -> ToolResult:
        async def run() -> Any:
            if self.tradfi is None:
                return {"error": "tradfi_not_available"}
            from datetime import date
            exp_date = date.fromisoformat(expiration)
            chain = await self.tradfi.get_options_chain(symbol, expiration=exp_date, strike_min=strike - 5, strike_max=strike + 5)
            # Find the nearest matching contract
            best = None
            for c in chain.contracts:
                if c.option_type == option_type.lower() and c.strike_price == strike:
                    best = c
                    break
            if best is None and chain.contracts:
                # Find closest strike
                best = min(chain.contracts, key=lambda c: abs(c.strike_price - strike))
            if best is None:
                return {"symbol": symbol.upper(), "strike": strike, "error": "no_matching_contract"}
            return {
                "symbol": best.symbol,
                "underlying": best.underlying,
                "strike": best.strike_price,
                "expiration": str(best.expiration_date),
                "option_type": best.option_type,
                "bid": best.bid, "ask": best.ask, "last": best.last_price,
                "delta": best.delta, "gamma": best.gamma, "theta": best.theta,
                "vega": best.vega, "rho": best.rho,
                "implied_volatility": best.implied_volatility,
            }
        return await self._run_tool("estimate_option_greeks", {"symbol": symbol, "strike": strike, "expiration": expiration, "option_type": option_type}, run, self._tradfi_source())

    async def _build_market_resolution_plan(self, prompt: str) -> ResolutionPlan:
        intent = parse_market_intent(prompt)
        candidates_by_query: dict[str, list[AssetCandidate]] = {symbol: [] for symbol in intent.symbols}
        if not intent.symbols:
            return route_market_intent(intent, candidates_by_query)

        perp_meta_ctxs = await self.hyperliquid.meta_and_asset_ctxs()
        spot_meta_ctxs = await self.hyperliquid.spot_meta_and_asset_ctxs()
        resolver = AssetResolver.from_meta_and_contexts(perp_meta_ctxs, spot_meta_ctxs)
        for query in intent.symbols:
            resolved = resolver.resolve(query)
            if resolved is not None:
                candidates_by_query[query].append(
                    AssetCandidate(
                        query=query,
                        symbol=query.split(":", 1)[-1].upper(),
                        canonical_symbol=resolved.coin,
                        display_symbol=resolved.coin,
                        asset_class="spot" if resolved.kind == "spot" else "crypto_perp",
                        provider="hyperliquid",
                        venue="hyperliquid-main",
                        source="hyperliquid_meta",
                        dex=resolved.dex,
                        liquidity_usd=_float_or_none((resolved.context or {}).get("dayNtlVlm")),
                        open_interest=_float_or_none((resolved.context or {}).get("openInterest")),
                        metadata={"kind": resolved.kind, "context": resolved.context or {}},
                    )
                )
        await self._add_hip3_candidates(intent.symbols, candidates_by_query)
        await self._add_tradfi_candidates(intent.symbols, candidates_by_query)
        return route_market_intent(intent, candidates_by_query)

    def _tradfi_source(self) -> str:
        provider = getattr(getattr(self.tradfi, "provider", None), "name", None)
        return f"tradfi:{provider or 'unavailable'}"

    async def _add_hip3_candidates(self, symbols: list[str], candidates_by_query: dict[str, list[AssetCandidate]]) -> None:
        dexs = await self._all_candidate_hip3_dexs(symbols)
        for dex in dexs:
            try:
                meta_ctxs = await self.hyperliquid.meta_and_asset_ctxs(dex=dex)
            except Exception:
                continue
            universe = meta_ctxs[0].get("universe", []) if isinstance(meta_ctxs, list) and meta_ctxs and isinstance(meta_ctxs[0], dict) else []
            ctxs = meta_ctxs[1] if isinstance(meta_ctxs, list) and len(meta_ctxs) > 1 and isinstance(meta_ctxs[1], list) else []
            for idx, item in enumerate(universe):
                if not isinstance(item, dict):
                    continue
                canonical = str(item.get("name") or "")
                base = canonical.split(":", 1)[-1].upper()
                matching_queries = [query for query in symbols if _query_matches_canonical(query, canonical)]
                if not matching_queries:
                    continue
                ctx = ctxs[idx] if idx < len(ctxs) and isinstance(ctxs[idx], dict) else {}
                asset_class = cast(Any, "commodity" if base in COMMODITY_SYMBOLS else "hip3_perp")
                for query in matching_queries:
                    candidates_by_query.setdefault(query, []).append(
                        AssetCandidate(
                            query=query,
                            symbol=base,
                            canonical_symbol=canonical,
                            display_symbol=base,
                            asset_class=asset_class,
                            provider="hyperliquid",
                            venue=f"hyperliquid-{dex}",
                            source="hyperliquid_hip3_meta",
                            dex=dex,
                            liquidity_usd=_float_or_none(ctx.get("dayNtlVlm")),
                            open_interest=_float_or_none(ctx.get("openInterest")),
                            metadata={"context": ctx, "meta": item},
                        )
                    )

    async def _add_tradfi_candidates(self, symbols: list[str], candidates_by_query: dict[str, list[AssetCandidate]]) -> None:
        if self.tradfi is None:
            return
        tradfi_symbols = [symbol.split(":", 1)[-1].upper() for symbol in symbols if ":" not in symbol]
        if not tradfi_symbols:
            return
        try:
            metadata = await self.tradfi.get_asset_metadata(tradfi_symbols)
        except Exception:
            metadata = {}
        for query in symbols:
            if ":" in query:
                continue
            asset = metadata.get(query.upper())
            if asset is None:
                continue
            asset_class = cast(Any, "etf" if asset.is_etf_like else "equity")
            candidates_by_query.setdefault(query, []).append(
                AssetCandidate(
                    query=query,
                    symbol=asset.symbol,
                    canonical_symbol=asset.symbol,
                    display_symbol=asset.symbol,
                    asset_class=asset_class,
                    provider="alpaca",
                    venue=asset.exchange or "alpaca",
                    source=f"{self._tradfi_source()}:asset_metadata",
                    active=asset.active,
                    tradable=asset.tradable,
                    metadata=asset.model_dump(mode="json"),
                )
            )

    async def _all_candidate_hip3_dexs(self, symbols: list[str]) -> list[str]:
        candidates: list[str] = []
        configured = list(getattr(self.hyperliquid.settings, "autonomy_hip3_dex_names", []))
        candidates.extend(str(dex).strip().lower() for dex in configured if str(dex).strip())
        explicit_dexs = [symbol.split(":", 1)[0].lower() for symbol in symbols if ":" in symbol]
        candidates.extend(explicit_dexs)
        wanted = {symbol.split(":", 1)[-1].upper() for symbol in symbols}
        try:
            dexs = await self.hyperliquid.perp_dexs()
        except Exception:
            dexs = []
        if isinstance(dexs, list):
            for dex_info in dexs:
                if not isinstance(dex_info, dict):
                    continue
                dex_name = str(dex_info.get("name") or "").strip().lower()
                if not dex_name:
                    continue
                assets = dex_info.get("assetToStreamingOiCap") or []
                if any(_asset_row_base(row) in wanted for row in assets):
                    candidates.append(dex_name)
        candidates.append("xyz")
        out: list[str] = []
        for dex in candidates:
            if dex and dex not in out:
                out.append(dex)
        return out

    async def canonical_market_coin(self, coin: str) -> str:
        """Return the Hyperliquid coin identifier used by market-data endpoints."""
        return await self._canonical_market_coin(coin)

    async def resolve_market_coin(self, coin: str) -> str | None:
        return await self._resolve_market_coin(coin)

    async def _canonical_market_coin(self, coin: str) -> str:
        resolved = await self._resolve_market_coin(coin)
        return resolved or coin.strip().upper()

    async def _resolve_market_coin(self, coin: str) -> str | None:
        cleaned = coin.strip()
        if not cleaned:
            return None
        if ":" in cleaned:
            return cleaned
        cached = self._hip3_symbol_cache.get(cleaned.upper())
        if cached:
            return cached
        perp_meta_ctxs = await self.hyperliquid.meta_and_asset_ctxs()
        spot_meta_ctxs = await self.hyperliquid.spot_meta_and_asset_ctxs()
        resolver = AssetResolver.from_meta_and_contexts(perp_meta_ctxs, spot_meta_ctxs)
        resolved = resolver.resolve(cleaned)
        if resolved is not None:
            return resolved.coin
        resolved, _mids, _dex = await self._resolve_hip3_asset(cleaned)
        if resolved is not None:
            self._hip3_symbol_cache[cleaned.upper()] = resolved.coin
            return resolved.coin
        return None

    async def _resolve_hip3_asset(self, symbol: str) -> tuple[Any | None, dict[str, str], str | None]:
        target = symbol.strip()
        if not target:
            return None, {}, None
        cache_key = target.upper()
        if cache_key in self._hip3_symbol_cache and self._hip3_symbol_cache[cache_key] is None:
            return None, {}, None
        cached = self._hip3_symbol_cache.get(cache_key)
        if cached:
            dex = cached.split(":", 1)[0].lower() if ":" in cached else None
            mids = await self.hyperliquid.all_mids(dex=dex or "") if dex else {}
            meta_ctxs = await self.hyperliquid.meta_and_asset_ctxs(dex=dex or "") if dex else []
            resolved = AssetResolver.from_meta_and_contexts(meta_ctxs, dex=dex).resolve(cached) if dex else None
            return resolved, mids, dex
        for dex in await self._candidate_hip3_dexs(target):
            try:
                mids = await self.hyperliquid.all_mids(dex=dex)
                meta_ctxs = await self.hyperliquid.meta_and_asset_ctxs(dex=dex)
            except Exception:
                continue
            resolver = AssetResolver.from_meta_and_contexts(meta_ctxs, dex=dex)
            resolved = resolver.resolve(target)
            if resolved is not None:
                self._hip3_symbol_cache[cache_key] = resolved.coin
                return resolved, mids, dex
        self._hip3_symbol_cache[cache_key] = None
        return None, {}, None

    async def _candidate_hip3_dexs(self, symbol: str) -> list[str]:
        configured = list(getattr(self.hyperliquid.settings, "autonomy_hip3_dex_names", []))
        candidates: list[str] = []
        if ":" in symbol:
            candidates.append(symbol.split(":", 1)[0].lower())
        candidates.extend(str(dex).strip().lower() for dex in configured if str(dex).strip())
        target = symbol.split(":", 1)[-1].strip().upper()
        try:
            dexs = await self.hyperliquid.perp_dexs()
        except Exception:
            dexs = []
        if isinstance(dexs, list):
            for dex_info in dexs:
                if not isinstance(dex_info, dict):
                    continue
                dex_name = str(dex_info.get("name") or "").strip().lower()
                if not dex_name:
                    continue
                assets = dex_info.get("assetToStreamingOiCap") or []
                for item in assets:
                    asset_name = str(item[0] if isinstance(item, (list, tuple)) and item else "").upper()
                    if asset_name == target or asset_name.endswith(f":{target}"):
                        candidates.append(dex_name)
                        break
        # TradeXYZ is currently exposed by Hyperliquid's API as dex "xyz".
        # Keep this low-cost fallback for semantic reads like "SPCX read" even
        # when AUTONOMY_HIP3_DEXS is not configured.
        candidates.append("xyz")
        out: list[str] = []
        for dex in candidates:
            if dex and dex not in out:
                out.append(dex)
        return out

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


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _query_matches_canonical(query: str, canonical: str) -> bool:
    query_upper = query.upper()
    canonical_upper = canonical.upper()
    base = canonical_upper.split(":", 1)[-1]
    return query_upper == canonical_upper or query_upper == base


def _asset_row_base(row: Any) -> str:
    if not isinstance(row, (list, tuple)) or not row:
        return ""
    return str(row[0]).split(":", 1)[-1].upper()


def _mid_for_resolved(mids: dict[str, Any], resolved_coin: str, query_symbol: str) -> Any:
    resolved_upper = resolved_coin.upper()
    query_upper = query_symbol.upper()
    if resolved_coin in mids:
        return mids[resolved_coin]
    if resolved_upper in mids:
        return mids[resolved_upper]
    if query_upper in mids:
        return mids[query_upper]
    if ":" in resolved_upper:
        dex, base = resolved_upper.split(":", 1)
        return mids.get(base) or mids.get(f"{dex.lower()}:{base}") or mids.get(f"{dex}:{base}")
    return None


def _clean_coins(coins: list[str]) -> list[str]:
    cleaned = [coin.strip().upper() for coin in coins if coin.strip()]
    return cleaned or ["BTC", "ETH", "SOL"]


def _filter_predicted(predicted: Any, coin: str) -> Any:
    target = coin.upper()
    if isinstance(predicted, list):
        return [item for item in predicted if isinstance(item, list) and item and str(item[0]).upper() == target]
    return predicted
