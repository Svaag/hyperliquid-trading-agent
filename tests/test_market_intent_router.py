from __future__ import annotations

from typing import Any

from hyperliquid_trading_agent.app.agent.runner import AgentContext, TradingAgentRunner
from hyperliquid_trading_agent.app.agent.tools import AgentTools, ToolResult
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.markets.resolution import (
    AssetCandidate,
    parse_market_intent,
    route_market_intent,
)
from hyperliquid_trading_agent.app.tradfi.schemas import StockSnapshot, TradFiAsset


def _hl(query: str, canonical: str, *, cls: str = "hip3_perp", dex: str = "xyz", liq: float = 10_000_000, oi: float = 100_000) -> AssetCandidate:
    return AssetCandidate(
        query=query,
        symbol=canonical.split(":")[-1],
        canonical_symbol=canonical,
        display_symbol=canonical.split(":")[-1],
        asset_class=cls,  # type: ignore[arg-type]
        provider="hyperliquid",
        venue=f"hyperliquid-{dex}" if dex else "hyperliquid-main",
        dex=dex or None,
        liquidity_usd=liq,
        open_interest=oi,
    )


def _eq(query: str, symbol: str, *, name: str = "", active: bool = True, tradable: bool = True, etf: bool = False) -> AssetCandidate:
    return AssetCandidate(
        query=query,
        symbol=symbol,
        canonical_symbol=symbol,
        display_symbol=symbol,
        asset_class="etf" if etf else "equity",
        provider="alpaca",
        venue="NASDAQ",
        active=active,
        tradable=tradable,
        metadata={"name": name or f"{symbol} Common Stock"},
    )


def _route(prompt: str, by_query: dict[str, list[AssetCandidate]]):
    intent = parse_market_intent(prompt)
    return route_market_intent(intent, by_query)


def test_parse_oil_market_is_commodity_not_literal_stock_only():
    intent = parse_market_intent("oil market read")

    assert intent.wants_commodity is True
    assert "oil" in intent.commodity_topics
    assert {"WTI", "CL", "BRENTOIL", "OIL", "USO"}.issubset(set(intent.symbols))


def test_parse_edgar_and_sec_forms_are_not_symbols():
    access = parse_market_intent("do you have access to SEC EDGAR?")
    filing = parse_market_intent("AAPL 10-K in EDGAR?")

    assert access.symbols == []
    assert access.wants_news is True
    assert filing.symbols == ["AAPL"]
    assert filing.wants_tradfi is True
    assert filing.wants_news is True


def test_msft_stock_prefers_nasdaq_equity_over_hip3():
    plan = _route(
        "MSFT stock read",
        {"MSFT": [_eq("MSFT", "MSFT", name="Microsoft Corporation Common Stock"), _hl("MSFT", "xyz:MSFT"), _hl("MSFT", "cash:MSFT", dex="cash", liq=1_000_000)]},
    )

    route = plan.routes[0]
    assert route.ambiguous is False
    assert route.selected[0].provider == "alpaca"
    assert plan.tradfi_symbols == ["MSFT"]
    assert plan.hyperliquid_symbols == []


def test_msft_hyperliquid_prefers_hip3_and_exact_namespace_wins():
    plan = _route(
        "MSFT on Hyperliquid",
        {"MSFT": [_eq("MSFT", "MSFT"), _hl("MSFT", "xyz:MSFT"), _hl("MSFT", "cash:MSFT", dex="cash", liq=1_000_000)]},
    )
    assert plan.routes[0].selected[0].canonical_symbol == "xyz:MSFT"
    assert plan.hyperliquid_symbols == ["xyz:MSFT"]
    assert plan.tradfi_symbols == []

    exact = _route(
        "cash:MSFT read",
        {"cash:MSFT": [_eq("cash:MSFT", "MSFT"), _hl("cash:MSFT", "xyz:MSFT"), _hl("cash:MSFT", "cash:MSFT", dex="cash", liq=1_000_000)]},
    )
    assert exact.routes[0].selected[0].canonical_symbol == "cash:MSFT"
    assert exact.routes[0].ambiguous is False


def test_plain_msft_is_ambiguous_and_routes_both_equity_and_top_hip3():
    plan = _route(
        "MSFT read",
        {"MSFT": [_eq("MSFT", "MSFT"), _hl("MSFT", "xyz:MSFT", liq=12_000_000), _hl("MSFT", "cash:MSFT", dex="cash", liq=3_000_000)]},
    )

    route = plan.routes[0]
    assert route.ambiguous is True
    assert "MSFT" in plan.ambiguous_queries
    assert "MSFT" in plan.tradfi_symbols
    assert "xyz:MSFT" in plan.hyperliquid_symbols
    assert any(candidate.dex == "cash" for candidate in route.candidates)


def test_duplicate_hip3_symbol_ranks_by_liquidity_but_keeps_ambiguity():
    plan = _route(
        "NVDA perp read",
        {"NVDA": [_hl("NVDA", "xyz:NVDA", dex="xyz", liq=50_000_000), _hl("NVDA", "cash:NVDA", dex="cash", liq=3_000_000), _hl("NVDA", "flx:NVDA", dex="flx", liq=1_000)]},
    )

    assert plan.routes[0].selected[0].canonical_symbol == "xyz:NVDA"
    assert plan.hyperliquid_symbols[0] == "xyz:NVDA"
    assert [candidate.canonical_symbol for candidate in plan.routes[0].candidates[:3]] == ["xyz:NVDA", "cash:NVDA", "flx:NVDA"]


def test_crypto_symbol_prefers_main_hyperliquid_unless_etf_intent():
    btc_candidates = {
        "BTC": [
            _hl("BTC", "BTC", cls="crypto_perp", dex="", liq=1_000_000_000),
            _hl("BTC", "hyna:BTC", dex="hyna", liq=1_000_000),
            _eq("BTC", "BTC", name="Grayscale Bitcoin Mini Trust ETF", etf=True),
        ]
    }

    crypto = _route("BTC read", btc_candidates)
    assert crypto.routes[0].selected[0].canonical_symbol == "BTC"
    assert crypto.tradfi_symbols == []

    etf = _route("BTC ETF read", btc_candidates)
    assert etf.routes[0].selected[0].provider == "alpaca"
    assert etf.tradfi_symbols == ["BTC"]


def test_oil_and_wti_prefer_commodity_candidates_over_inactive_or_name_collision_equity():
    oil = _route(
        "OIL read",
        {"OIL": [_eq("OIL", "OIL", name="iPath Pure Beta Crude Oil ETN", active=False, tradable=False, etf=True), _hl("OIL", "flx:OIL", cls="commodity", dex="flx", liq=95_000)]},
    )
    assert oil.routes[0].selected[0].canonical_symbol == "flx:OIL"
    assert oil.tradfi_symbols == []

    wti = _route(
        "WTI read",
        {"WTI": [_eq("WTI", "WTI", name="W&T Offshore, Inc."), _hl("WTI", "cash:WTI", cls="commodity", dex="cash", liq=5_800_000)]},
    )
    assert wti.routes[0].selected[0].canonical_symbol == "cash:WTI"


def test_uso_read_prefers_tradfi_etf_when_no_commodity_word():
    plan = _route("USO read", {"USO": [_eq("USO", "USO", name="United States Oil Fund, LP", etf=True)]})

    assert plan.routes[0].selected[0].provider == "alpaca"
    assert plan.tradfi_symbols == ["USO"]


class FakeHyperliquidCollisionCatalog:
    def __init__(self):
        self.settings = Settings(autonomy_hip3_dexs="")

    async def all_mids(self, dex: str = "") -> dict[str, str]:
        if dex == "xyz":
            return {"xyz:MSFT": "397.33", "xyz:SPCX": "202.84"}
        if dex == "cash":
            return {"cash:MSFT": "397.35"}
        if dex == "flx":
            return {"flx:OIL": "76.62"}
        return {"BTC": "100000"}

    async def meta_and_asset_ctxs(self, dex: str = "") -> list[Any]:
        if dex == "xyz":
            return [
                {"universe": [{"name": "xyz:MSFT", "szDecimals": 2, "maxLeverage": 10}, {"name": "xyz:SPCX", "szDecimals": 2, "maxLeverage": 10}]},
                [{"coin": "xyz:MSFT", "dayNtlVlm": "12000000", "openInterest": "110000"}, {"coin": "xyz:SPCX", "dayNtlVlm": "1000000000", "openInterest": "1322473"}],
            ]
        if dex == "cash":
            return [{"universe": [{"name": "cash:MSFT", "szDecimals": 2, "maxLeverage": 10}]}, [{"coin": "cash:MSFT", "dayNtlVlm": "3000000", "openInterest": "2000"}]]
        if dex == "flx":
            return [{"universe": [{"name": "flx:OIL", "szDecimals": 3, "maxLeverage": 10}]}, [{"coin": "flx:OIL", "dayNtlVlm": "95000", "openInterest": "3800"}]]
        return [{"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50}]}, [{"coin": "BTC", "dayNtlVlm": "1000000000", "openInterest": "100000"}]]

    async def spot_meta_and_asset_ctxs(self) -> list[Any]:
        return [{"universe": []}, []]

    async def perp_dexs(self) -> list[Any]:
        return [
            {"name": "xyz", "fullName": "XYZ", "assetToStreamingOiCap": [["xyz:MSFT", "125000000"], ["xyz:SPCX", "500000000"]]},
            {"name": "cash", "fullName": "dreamcash", "assetToStreamingOiCap": [["cash:MSFT", "20000000"]]},
            {"name": "flx", "fullName": "Felix", "assetToStreamingOiCap": [["flx:OIL", "22500000"]]},
        ]


class FakeTradFiCollisionCatalog:
    async def get_asset_metadata(self, symbols: list[str]) -> dict[str, TradFiAsset]:
        assets = {
            "MSFT": TradFiAsset(symbol="MSFT", name="Microsoft Corporation Common Stock", exchange="NASDAQ", status="active", tradable=True),
            "SPCX": TradFiAsset(symbol="SPCX", name="Space Exploration Technologies Corp. Class A Common Stock", exchange="NASDAQ", status="active", tradable=True),
            "OIL": TradFiAsset(symbol="OIL", name="iPath Pure Beta Crude Oil ETN", exchange="ARCA", status="inactive", tradable=False),
        }
        return {symbol: assets[symbol] for symbol in symbols if symbol in assets}

    async def get_snapshots(self, symbols: list[str]) -> dict[str, StockSnapshot]:
        return {}


class FakeNews:
    pass


async def test_agent_tool_intent_router_returns_ambiguous_msft_and_commodity_oil():
    tools = AgentTools(hyperliquid=FakeHyperliquidCollisionCatalog(), news=FakeNews(), tradfi=FakeTradFiCollisionCatalog())  # type: ignore[arg-type]

    msft = await tools.resolve_market_intent("MSFT read")
    oil = await tools.resolve_market_intent("oil market read")

    assert "MSFT" in msft.data["ambiguous_queries"]
    assert "MSFT" in msft.data["tradfi_symbols"]
    assert "xyz:MSFT" in msft.data["hyperliquid_symbols"]
    assert "cash:MSFT" in [candidate["canonical_symbol"] for candidate in msft.data["routes"][0]["candidates"]]

    assert "flx:OIL" in oil.data["hyperliquid_symbols"]
    assert "OIL" not in oil.data["tradfi_symbols"]


class FailingGateway:
    async def complete(self, *args, **kwargs):
        from hyperliquid_trading_agent.app.agent.model_gateway import ModelGatewayError

        raise ModelGatewayError("offline")


class RoutedFakeTools:
    tradfi = object()

    def __init__(self):
        self.calls: list[str] = []

    async def resolve_market_intent(self, prompt: str) -> ToolResult:
        self.calls.append("resolve_market_intent")
        if "stock" in prompt.lower() or "BTC ETF" in prompt:
            tradfi_symbols = ["BTC"] if "BTC" in prompt else ["MSFT"]
            hl_symbols: list[str] = []
            ambiguous: list[str] = []
        elif "hyperliquid" in prompt.lower() or "hl" in prompt.lower():
            tradfi_symbols = []
            hl_symbols = ["xyz:MSFT"]
            ambiguous = []
        else:
            tradfi_symbols = ["MSFT"]
            hl_symbols = ["xyz:MSFT"]
            ambiguous = ["MSFT"]
        return ToolResult(
            tool="resolve_market_intent",
            data={
                "hyperliquid_symbols": hl_symbols,
                "tradfi_symbols": tradfi_symbols,
                "ambiguous_queries": ambiguous,
                "routes": [],
            },
            source="fake",
            timestamp_ms=1,
            freshness="live",
        )

    async def get_market_snapshot(self, coins, intervals=None, include_l2=False):
        self.calls.append(f"get_market_snapshot:{','.join(coins)}")
        return ToolResult(tool="get_market_snapshot", data={"assets": {coin: {"mid": "1", "context": {}} for coin in coins}}, source="fake", timestamp_ms=1, freshness="live")

    async def get_market_snapshot_tradfi(self, symbols):
        self.calls.append(f"get_market_snapshot_tradfi:{','.join(symbols)}")
        return ToolResult(tool="get_market_snapshot_tradfi", data={symbol: {"daily_bar": {"close": 1}, "change_pct": 0} for symbol in symbols}, source="fake", timestamp_ms=1, freshness="live")

    async def search_hyperliquid_docs(self, query):
        self.calls.append("search_hyperliquid_docs")
        return ToolResult(tool="search_hyperliquid_docs", data={}, source="fake", timestamp_ms=1, freshness="live")

    async def search_market_news(self, query, lookback_hours=24):
        self.calls.append("search_market_news")
        return ToolResult(tool="search_market_news", data={}, source="fake", timestamp_ms=1, freshness="live")

    async def get_funding_context(self, coin):
        self.calls.append(f"get_funding_context:{coin}")
        return ToolResult(tool="get_funding_context", data={"coin": coin}, source="fake", timestamp_ms=1, freshness="live")

    async def get_candles(self, coin, interval="1h", lookback_hours=24):
        self.calls.append(f"get_candles:{coin}")
        return ToolResult(tool="get_candles", data=[], source="fake", timestamp_ms=1, freshness="live")


async def test_runner_uses_intent_router_for_stock_vs_hyperliquid_collision():
    tools = RoutedFakeTools()
    runner = TradingAgentRunner(tools=tools, model_gateway=FailingGateway(), settings=Settings(tradfi_enabled=True))  # type: ignore[arg-type]

    stock_response = await runner.answer("MSFT stock read", context=AgentContext(source="test"))
    stock_tool_names = [item.tool for item in stock_response.tool_results]
    assert "resolve_market_intent" in stock_tool_names
    assert "get_market_snapshot_tradfi" in stock_tool_names
    assert not any(call.startswith("get_market_snapshot:xyz:MSFT") for call in tools.calls)

    tools.calls.clear()
    hl_response = await runner.answer("MSFT on Hyperliquid read", context=AgentContext(source="test"))
    hl_tool_names = [item.tool for item in hl_response.tool_results]
    assert "get_market_snapshot" in hl_tool_names
    assert "get_market_snapshot_tradfi" not in hl_tool_names
    assert "get_market_snapshot:xyz:MSFT" in tools.calls


async def test_runner_uses_tradfi_for_btc_etf_not_crypto_perp():
    tools = RoutedFakeTools()
    runner = TradingAgentRunner(tools=tools, model_gateway=FailingGateway(), settings=Settings(tradfi_enabled=True))  # type: ignore[arg-type]

    response = await runner.answer("BTC ETF read", context=AgentContext(source="test"))

    names = [item.tool for item in response.tool_results]
    assert "get_market_snapshot_tradfi" in names
    assert "get_market_snapshot" not in names
    assert "get_market_snapshot_tradfi:BTC" in tools.calls
