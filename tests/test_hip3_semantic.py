from __future__ import annotations

from typing import Any

from hyperliquid_trading_agent.app.agent.high_stakes.context import HighStakesContextBuilder
from hyperliquid_trading_agent.app.agent.high_stakes.routing import route_high_stakes
from hyperliquid_trading_agent.app.agent.high_stakes.schemas import TradeProposalRequest
from hyperliquid_trading_agent.app.agent.tools import AgentTools
from hyperliquid_trading_agent.app.config import Settings


class FakeHyperliquidHip3:
    def __init__(self):
        self.settings = Settings(autonomy_hip3_dexs="")
        self.l2_coins: list[str] = []
        self.candle_coins: list[str] = []
        self.funding_coins: list[str] = []

    async def all_mids(self, dex: str = "") -> dict[str, str]:
        if dex == "xyz":
            return {"xyz:SPCX": "202.84"}
        return {"BTC": "100"}

    async def meta_and_asset_ctxs(self, dex: str = "") -> list[Any]:
        if dex == "xyz":
            return [
                {"universe": [{"name": "xyz:SPCX", "szDecimals": 2, "maxLeverage": 10}]},
                [
                    {
                        "coin": "xyz:SPCX",
                        "funding": "0.0000474762",
                        "openInterest": "1322473.64",
                        "dayNtlVlm": "1213881653.42",
                        "markPx": "202.93",
                        "midPx": "202.985",
                    }
                ],
            ]
        return [{"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50}]}, [{"coin": "BTC", "markPx": "100"}]]

    async def spot_meta_and_asset_ctxs(self) -> list[Any]:
        return [{"universe": []}, []]

    async def perp_dexs(self) -> list[Any]:
        return [None, {"name": "xyz", "fullName": "XYZ", "assetToStreamingOiCap": [["xyz:SPCX", "100000000.0"]]}]

    async def l2_book(self, coin: str, n_sig_figs: int | None = None, mantissa: int | None = None) -> dict[str, Any]:
        self.l2_coins.append(coin)
        return {"coin": coin, "levels": []}

    async def candle_snapshot(self, coin: str, interval: str, start_time_ms: int, end_time_ms: int) -> list[dict[str, Any]]:
        self.candle_coins.append(coin)
        return [{"T": end_time_ms, "c": "202.84", "i": interval}]

    async def funding_history(self, coin: str, start_time_ms: int, end_time_ms: int) -> list[dict[str, Any]]:
        self.funding_coins.append(coin)
        return [{"coin": coin, "fundingRate": "0.0000474762"}]

    async def predicted_fundings(self) -> list[Any]:
        return [["xyz:SPCX", [["HlPerp", {"fundingRate": "0.00005"}]]]]


class FakeNews:
    pass


class FakeSDKInfoHip3Verifier:
    def __init__(self):
        self.l2_coins: list[str] = []
        self.candle_coins: list[str] = []
        self.funding_coins: list[str] = []

    async def all_mids(self, dex: str = "") -> dict[str, str]:
        return {"BTC": "100"}

    async def meta_and_asset_ctxs(self):
        return [{"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50}]}, [{"coin": "BTC", "markPx": "100"}]]

    async def spot_meta_and_asset_ctxs(self):
        return [{"universe": []}, []]

    async def l2_snapshot(self, coin: str) -> dict[str, Any]:
        if coin == "SPCX":
            raise KeyError(coin)
        self.l2_coins.append(coin)
        return {"coin": coin, "levels": []}

    async def candles_snapshot(self, coin: str, interval: str, start_time_ms: int, end_time_ms: int) -> list[dict[str, Any]]:
        if coin == "SPCX":
            raise KeyError(coin)
        self.candle_coins.append(coin)
        return [{"s": coin, "c": "202.84", "i": interval}]

    async def funding_history(self, coin: str, start_time_ms: int, end_time_ms: int | None = None) -> list[dict[str, Any]]:
        if coin == "SPCX":
            raise KeyError(coin)
        self.funding_coins.append(coin)
        return [{"coin": coin, "fundingRate": "0.0000474762"}]


async def test_semantic_market_snapshot_resolves_bare_hip3_symbol():
    hyperliquid = FakeHyperliquidHip3()
    tools = AgentTools(hyperliquid=hyperliquid, news=FakeNews())  # type: ignore[arg-type]

    result = await tools.get_market_snapshot(["SPCX"], include_l2=True)

    assets = result.data["assets"]
    assert "xyz:SPCX" in assets
    assert assets["xyz:SPCX"]["query_symbol"] == "SPCX"
    assert assets["xyz:SPCX"]["kind"] == "hip3_index"
    assert assets["xyz:SPCX"]["dex"] == "xyz"
    assert assets["xyz:SPCX"]["mid"] == "202.84"
    assert assets["xyz:SPCX"]["context"]["openInterest"] == "1322473.64"
    assert hyperliquid.l2_coins == ["xyz:SPCX"]


async def test_semantic_candles_and_funding_canonicalize_bare_hip3_symbol():
    hyperliquid = FakeHyperliquidHip3()
    tools = AgentTools(hyperliquid=hyperliquid, news=FakeNews())  # type: ignore[arg-type]

    candles = await tools.get_candles("SPCX", interval="1h", lookback_hours=1)
    funding = await tools.get_funding_context("SPCX")

    assert candles.data[0]["c"] == "202.84"
    assert hyperliquid.candle_coins == ["xyz:SPCX"]
    assert funding.data["coin"] == "xyz:SPCX"
    assert funding.data["query_symbol"] == "SPCX"
    assert hyperliquid.funding_coins == ["xyz:SPCX"]


async def test_candles_and_funding_do_not_call_api_for_unknown_symbol():
    hyperliquid = FakeHyperliquidHip3()
    tools = AgentTools(hyperliquid=hyperliquid, news=FakeNews())  # type: ignore[arg-type]

    candles = await tools.get_candles("EOF", interval="1h", lookback_hours=1)
    funding = await tools.get_funding_context("EOF")

    assert candles.data["error"] == "asset_not_found"
    assert funding.data["error"] == "asset_not_found"
    assert hyperliquid.candle_coins == []
    assert hyperliquid.funding_coins == []


async def test_high_stakes_sdk_verification_canonicalizes_hip3_symbol():
    hyperliquid = FakeHyperliquidHip3()
    tools = AgentTools(hyperliquid=hyperliquid, news=FakeNews())  # type: ignore[arg-type]
    sdk_info = FakeSDKInfoHip3Verifier()
    settings = Settings(high_stakes_info_provider="sdk_preferred")
    route = route_high_stakes("Review long SPCX entry 202 stop 190", forced=True)

    context = await HighStakesContextBuilder(tools, settings, sdk_info=sdk_info).gather(
        TradeProposalRequest(prompt="Review long SPCX entry 202 stop 190"),
        route,
    )

    assert sdk_info.l2_coins == ["xyz:SPCX"]
    assert sdk_info.candle_coins == ["xyz:SPCX"]
    assert sdk_info.funding_coins == ["xyz:SPCX"]
    assert not any(warning.startswith("tool_error:sdk_l2_SPCX") for warning in context.warnings)
    assert not any(warning.startswith("tool_error:sdk_candles_SPCX") for warning in context.warnings)
