"""Phase 1 golden-payload tests for the venue adapters.

Each adapter's decode is a pure function over a recorded venue frame, so these
pin the exact field mapping (side, notional, integrity, event_type) without any
network. Side mappings encode documented assumptions; changing them here should
be a deliberate act.
"""

from __future__ import annotations

from decimal import Decimal

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.liquidations.adapters import (
    hyperliquid_public_ws as hlp,
)
from hyperliquid_trading_agent.app.liquidations.adapters import (
    hyperliquid_user_events as hlu,
)
from hyperliquid_trading_agent.app.liquidations.adapters import (
    lighter_ws as lighter,
)
from hyperliquid_trading_agent.app.liquidations.adapters._ws import to_ms
from hyperliquid_trading_agent.app.liquidations.adapters.aster_ws import normalize_symbol, parse_force_order
from hyperliquid_trading_agent.app.liquidations.adapters.hyperliquid_grpc import HyperliquidGrpcAdapter
from hyperliquid_trading_agent.app.liquidations.models import EventType, SourceIntegrity
from hyperliquid_trading_agent.app.liquidations.service import LiquidationService

# ------------------------------------------------------------------------ _ws


def test_to_ms_normalizes_magnitudes():
    assert to_ms(1_700_000_000) == 1_700_000_000_000  # seconds
    assert to_ms(1_700_000_000_000) == 1_700_000_000_000  # ms
    assert to_ms(1_700_000_000_000_000) == 1_700_000_000_000  # µs
    assert to_ms(1_700_000_000_000_000_000) == 1_700_000_000_000  # ns
    assert to_ms(0) == 0
    assert to_ms("nope") == 0


# ----------------------------------------------------------------------- aster


def _aster_frame(side: str) -> dict:
    return {
        "e": "forceOrder",
        "E": 1568014460893,
        "o": {
            "s": "BTCUSDT", "S": side, "o": "LIMIT", "f": "IOC",
            "q": "0.014", "p": "9910", "ap": "9920", "X": "FILLED",
            "l": "0.014", "z": "0.014", "T": 1568014460893,
        },
    }


def test_aster_sell_is_liquidated_long():
    ev = parse_force_order(_aster_frame("SELL"))
    assert ev is not None
    assert ev.venue == "aster" and ev.symbol == "BTC"
    assert ev.source_integrity == SourceIntegrity.SNAPSHOT_THROTTLED
    assert ev.event_type == EventType.LIQUIDATION
    assert ev.liquidated_side == "long"
    assert ev.size_base == Decimal("0.014") and ev.avg_price == Decimal("9920")
    assert ev.notional_usd == Decimal("0.014") * Decimal("9920")
    assert ev.timestamp_ms == 1568014460893
    assert ev.event_id.startswith("aster:")


def test_aster_buy_is_liquidated_short_and_combined_envelope():
    wrapped = {"stream": "!forceOrder@arr", "data": _aster_frame("BUY")}
    ev = parse_force_order(wrapped)
    assert ev is not None and ev.liquidated_side == "short"


def test_aster_ignores_non_force_order():
    assert parse_force_order({"e": "trade"}) is None


def test_normalize_symbol():
    assert normalize_symbol("BTCUSDT") == "BTC"
    assert normalize_symbol("ETHUSDC") == "ETH"
    assert normalize_symbol("HYPE") == "HYPE"


# --------------------------------------------------------------------- lighter


def _lighter_message(channel: str = "trade:1") -> dict:
    return {
        "channel": channel,
        "type": "update/trade",
        "nonce": 5,
        "trades": [
            {"trade_id": "100", "price": "3000", "size": "2", "usd_amount": "6000",
             "timestamp": 1_700_000_000_000, "is_maker_ask": True, "type": "trade"},
            {"trade_id": "101", "price": "3001", "size": "1", "usd_amount": "3001",
             "timestamp": 1_700_000_000_001, "is_maker_ask": False, "type": "deleverage"},
        ],
        "liquidation_trades": [
            {"trade_id": "200", "price": "60000", "size": "0.5", "usd_amount": "30000",
             "timestamp": 1_700_000_000_002, "is_maker_ask": True, "type": "liquidation"},
        ],
    }


def test_lighter_emits_liquidations_and_skips_normal_trades():
    events = lighter.iter_liquidations(_lighter_message(), {1: "ETH"})
    assert len(events) == 2  # deleverage (from trades) + liquidation_trade; normal trade excluded
    by_type = {e.event_type: e for e in events}
    liq = by_type[EventType.LIQUIDATION]
    assert liq.symbol == "ETH" and liq.venue_market_id == "1" and liq.trade_id == "200"
    assert liq.source_integrity == SourceIntegrity.CONFIRMED
    assert liq.liquidated_side == "short"  # is_maker_ask True -> taker buys to close short
    assert liq.notional_usd == Decimal("30000")
    delev = by_type[EventType.DELEVERAGE]
    assert delev.liquidated_side == "long"  # is_maker_ask False


def test_lighter_unknown_market_falls_back_to_label():
    events = lighter.iter_liquidations(_lighter_message(channel="trade:9"), {})
    assert events and all(e.symbol == "MKT9" for e in events)


# ----------------------------------------------------------- hyperliquid public


def test_hl_public_large_sell_is_long_pressure():
    ev = hlp.parse_trade(
        {"coin": "BTC", "side": "A", "px": "60000", "sz": "1", "time": 1_700_000_000_000, "tid": 123},
        min_notional=50_000,
    )
    assert ev is not None
    assert ev.event_type == EventType.LIQUIDATION_PRESSURE
    assert ev.source_integrity == SourceIntegrity.DERIVED  # never "confirmed"
    assert ev.liquidated_side == "long" and ev.symbol == "BTC"
    assert ev.notional_usd == Decimal("60000")


def test_hl_public_below_threshold_skipped():
    assert hlp.parse_trade(
        {"coin": "BTC", "side": "B", "px": "60000", "sz": "0.1", "time": 1, "tid": 1}, min_notional=50_000
    ) is None


def test_hl_public_large_buy_is_short_pressure():
    ev = hlp.parse_trade(
        {"coin": "ETH", "side": "B", "px": "3000", "sz": "100", "time": 1, "tid": 2}, min_notional=50_000
    )
    assert ev is not None and ev.liquidated_side == "short"


# ------------------------------------------------------------- hyperliquid user


def test_hl_user_parses_liquidation_fill():
    ev = hlu.parse_fill(
        {"coin": "BTC", "px": "60000", "sz": "0.5", "side": "A", "time": 1_700_000_000_000, "tid": 7,
         "dir": "Close Long",
         "liquidation": {"liquidatedUser": "0xVICTIM", "markPx": "59000", "method": "market"}}
    )
    assert ev is not None
    assert ev.event_type == EventType.LIQUIDATION
    assert ev.source_integrity == SourceIntegrity.ACCOUNT_PRIVATE
    assert ev.liquidated_side == "long"
    assert ev.liquidated_user == "0xVICTIM" and ev.mark_price == Decimal("59000") and ev.method == "market"


def test_hl_user_backstop_method():
    ev = hlu.parse_fill(
        {"coin": "ETH", "px": "3000", "sz": "1", "side": "B", "time": 1, "tid": 8, "dir": "Close Short",
         "liquidation": {"liquidatedUser": "0xX", "method": "backstop"}}
    )
    assert ev is not None and ev.event_type == EventType.BACKSTOP and ev.liquidated_side == "short"


def test_hl_user_non_liquidation_fill_ignored():
    assert hlu.parse_fill({"coin": "BTC", "px": "1", "sz": "1", "time": 1}) is None


def test_hl_user_decode_skips_snapshot():
    adapter = hlu.HyperliquidUserEventsAdapter(Settings())
    snapshot = {"channel": "userFills", "data": {"isSnapshot": True, "user": "0xabc", "fills": []}}
    assert adapter._decode(snapshot) == []


# -------------------------------------------------------------- hyperliquid grpc


async def test_hl_grpc_stub_stays_dark():
    adapter = HyperliquidGrpcAdapter(Settings())
    events = [event async for event in adapter.run()]
    assert events == []
    assert adapter.health()["error"] == "not_configured"


# --------------------------------------------------------------- service wiring


def test_build_adapters_respects_flags():
    settings = Settings(
        liquidations_aster_enabled=True,
        liquidations_lighter_enabled=True,
        liquidations_hl_public_enabled=True,
        liquidations_hl_user_enabled=True,
    )
    service = LiquidationService(settings, None)
    assert {a.source for a in service.adapters} == {
        "aster_ws", "lighter_ws", "hyperliquid_public_ws", "hyperliquid_user_events",
    }
