"""Phase 2 tests: derived-vs-confirmed reconciliation + managed-gRPC decode.

The reconciliation math is exercised against hand-built events (replayed frames),
so coverage/match-rate are pinned before any live confirmed source exists.
"""

from __future__ import annotations

from decimal import Decimal

from hyperliquid_trading_agent.app.liquidations.adapters.hyperliquid_grpc import parse_grpc_fill
from hyperliquid_trading_agent.app.liquidations.models import EventType, LiquidationEvent, SourceIntegrity
from hyperliquid_trading_agent.app.liquidations.reconcile import reconcile


def _ev(*, ts: int, symbol: str, integrity: SourceIntegrity, event_type: EventType,
        price: str, size: str, side: str = "long") -> LiquidationEvent:
    return LiquidationEvent(
        venue="hyperliquid", source="test", source_integrity=integrity, event_type=event_type,
        symbol=symbol, liquidated_side=side, price=Decimal(price), size_base=Decimal(size),
        timestamp_ms=ts, received_at_ms=ts,
    )


# ------------------------------------------------------------------- reconcile


def test_reconcile_matches_and_coverage():
    now_ms = 8_000_000
    events = [
        # BTC derived bucket 5000 (10_000) matches BTC confirmed bucket 5000 (12_000)
        _ev(ts=5_000_000, symbol="BTC", integrity=SourceIntegrity.DERIVED,
            event_type=EventType.LIQUIDATION_PRESSURE, price="100", size="100"),
        _ev(ts=5_000_500, symbol="BTC", integrity=SourceIntegrity.VENDOR,
            event_type=EventType.LIQUIDATION, price="120", size="100"),
        # ETH derived only (false positive), 5_000
        _ev(ts=6_000_000, symbol="ETH", integrity=SourceIntegrity.DERIVED,
            event_type=EventType.LIQUIDATION_PRESSURE, price="50", size="100"),
        # SOL confirmed only (false negative), 3_000
        _ev(ts=7_000_000, symbol="SOL", integrity=SourceIntegrity.VENDOR,
            event_type=EventType.LIQUIDATION, price="30", size="100"),
    ]
    report = reconcile(events, bucket_ms=1000, window_ms=10_000_000, now_ms=now_ms, confirmed_source="grpc")

    assert report["confirmed_source"] == "grpc"
    assert report["derived_buckets"] == 2 and report["confirmed_buckets"] == 2
    assert report["matched_buckets"] == 1
    assert report["derived_only_buckets"] == 1 and report["confirmed_only_buckets"] == 1
    assert report["match_rate"] == 0.5
    assert report["derived_notional_usd"] == 15000.0
    assert report["confirmed_notional_usd"] == 15000.0
    assert report["notional_delta_usd"] == 0.0
    assert report["confirmed_coverage"] == 0.75  # 15000 / (15000 confirmed + 5000 derived-only)
    assert report["by_symbol"]["BTC"]["matched_buckets"] == 1


def test_reconcile_honest_when_no_confirmed_source():
    report = reconcile(
        [_ev(ts=1_000, symbol="BTC", integrity=SourceIntegrity.DERIVED,
             event_type=EventType.LIQUIDATION_PRESSURE, price="100", size="10")],
        bucket_ms=1000, window_ms=10_000, now_ms=5_000,
    )
    assert report["confirmed_source"] == "not_configured"
    assert report["confirmed_buckets"] == 0
    assert report["match_rate"] == 0.0
    assert report["confirmed_coverage"] == 0.0


def test_reconcile_ignores_out_of_window_and_non_hl():
    now_ms = 1_000_000
    stale = _ev(ts=1, symbol="BTC", integrity=SourceIntegrity.VENDOR,
                event_type=EventType.LIQUIDATION, price="100", size="100")
    report = reconcile([stale], bucket_ms=1000, window_ms=10_000, now_ms=now_ms)
    assert report["confirmed_buckets"] == 0  # outside the trailing window


# ----------------------------------------------------------------- grpc decode


def test_parse_grpc_fill_vendor_liquidation():
    ev = parse_grpc_fill(
        {"coin": "BTC", "px": "60000", "sz": "0.5", "dir": "Close Long", "time": 1_700_000_000_000,
         "tid": 9, "block": "12345",
         "liquidation": {"liquidatedUser": "0xVICTIM", "markPx": "59000", "method": "market",
                         "liquidationId": "L1"}},
        provider="thunderhead",
    )
    assert ev is not None
    assert ev.source == "hyperliquid_grpc" and ev.source_integrity == SourceIntegrity.VENDOR
    assert ev.event_type == EventType.LIQUIDATION and ev.liquidated_side == "long"
    assert ev.symbol == "BTC" and ev.block_height == 12345 and ev.liquidation_id == "L1"
    assert ev.notional_usd == Decimal("60000") * Decimal("0.5")
    assert ev.raw["_provider"] == "thunderhead"


def test_parse_grpc_fill_backstop_and_non_liquidation():
    backstop = parse_grpc_fill(
        {"coin": "ETH", "px": "3000", "sz": "1", "dir": "Close Short", "time": 1,
         "liquidation": {"liquidatedUser": "0xX", "method": "backstop"}}
    )
    assert backstop is not None and backstop.event_type == EventType.BACKSTOP and backstop.liquidated_side == "short"
    assert parse_grpc_fill({"coin": "BTC", "px": "1", "sz": "1", "time": 1}) is None  # ordinary fill
