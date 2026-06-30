"""Phase 0 tests for the liquidation flow monitor.

Covers the contract (derivation + honesty invariant), venue-aware dedupe, and a
DB-free replay smoke that drives the full pipeline (adapter -> service -> dedupe
-> aggregator -> recent tape -> bus) and asserts the public surface.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from pydantic import ValidationError

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.liquidations.adapters.replay import ReplayAdapter
from hyperliquid_trading_agent.app.liquidations.models import (
    EventType,
    LiquidationEvent,
    SourceIntegrity,
)
from hyperliquid_trading_agent.app.liquidations.service import LiquidationService


def _event(**overrides) -> LiquidationEvent:
    base = dict(
        venue="lighter",
        source="lighter_ws",
        source_integrity=SourceIntegrity.CONFIRMED,
        event_type=EventType.LIQUIDATION,
        symbol="BTC",
        liquidated_side="long",
        price=Decimal("60000"),
        size_base=Decimal("0.5"),
        timestamp_ms=1_000_000,
        received_at_ms=1_000_050,
        trade_id="t-1",
    )
    base.update(overrides)
    return LiquidationEvent(**base)


# --------------------------------------------------------------------- contract


def test_notional_auto_computed_from_price_and_size():
    ev = _event()
    assert ev.notional_usd == Decimal("30000.0")
    assert ev.is_execution is True


def test_event_id_is_assigned_and_deterministic():
    a = _event()
    b = _event()  # identical inputs
    assert a.event_id == b.event_id
    assert a.event_id.startswith("lighter:")


def test_pressure_must_not_be_confirmed():
    with pytest.raises(ValidationError):
        _event(event_type=EventType.LIQUIDATION_PRESSURE, source_integrity=SourceIntegrity.CONFIRMED)
    # but pressure with a derived source is fine, and is not an execution
    ev = _event(event_type=EventType.LIQUIDATION_PRESSURE, source_integrity=SourceIntegrity.DERIVED)
    assert ev.is_execution is False


def test_public_view_redacts_and_drops_raw():
    ev = _event(liquidated_user="0xABCDEF1234567890", raw={"secret": 1})
    view = ev.public_view()
    assert "raw" not in view
    assert view["liquidated_user"] != "0xABCDEF1234567890"
    assert view["liquidated_user"].startswith("0xABCD")
    # numeric fields must serialize as JSON numbers (consistent with the DB
    # projection + chart-friendly), not Decimal strings
    assert isinstance(view["notional_usd"], float)
    assert isinstance(view["price"], float)


# ----------------------------------------------------------------------- dedupe


def test_dedupe_same_underlying_event_collapses():
    a = _event(trade_id="abc")
    b = _event(trade_id="abc")
    assert a.event_id == b.event_id


def test_dedupe_distinct_events_differ():
    a = _event(trade_id="abc")
    b = _event(trade_id="def")
    assert a.event_id != b.event_id


def test_hyperliquid_derived_id_buckets_within_a_second():
    # Same coin/side/price/size within the same second -> same derived id.
    common = dict(
        venue="hyperliquid",
        source="hyperliquid_public_ws",
        source_integrity=SourceIntegrity.DERIVED,
        event_type=EventType.LIQUIDATION_PRESSURE,
        raw={"k": "v"},
    )
    a = _event(timestamp_ms=1_700_000_000_100, trade_id=None, **common)
    b = _event(timestamp_ms=1_700_000_000_900, trade_id=None, **common)
    assert a.event_id == b.event_id


# ------------------------------------------------------------------ replay smoke


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def test_replay_smoke_drives_full_pipeline():
    events = [
        _event(symbol="BTC", liquidated_side="long", price=Decimal("60000"), size_base=Decimal("1"), trade_id="a"),
        _event(symbol="ETH", liquidated_side="short", price=Decimal("3000"), size_base=Decimal("10"), trade_id="b"),
        _event(
            symbol="BTC",
            liquidated_side="short",
            price=Decimal("60000"),
            size_base=Decimal("0.5"),
            trade_id="c",
            event_type=EventType.LIQUIDATION_PRESSURE,
            source_integrity=SourceIntegrity.DERIVED,
        ),
        # exact duplicate of the first -> must be deduped
        _event(symbol="BTC", liquidated_side="long", price=Decimal("60000"), size_base=Decimal("1"), trade_id="a"),
    ]
    # sessionmaker=None -> store disabled, whole pipeline runs in memory.
    service = LiquidationService(Settings(), None, adapters=[ReplayAdapter(events)])

    received: list = []

    async def consume():
        async with service.bus.subscribe() as stream:
            async for ev in stream:
                received.append(ev)

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0)  # let the subscriber register before publishes
    await service.start()
    try:
        # 3 unique events (4th is a duplicate of the 1st).
        await _wait_for(lambda: len(service._recent) >= 3)
        await asyncio.sleep(0.05)
    finally:
        await service.stop()
        consumer.cancel()

    # dedupe collapsed the duplicate
    assert len(service._recent) == 3

    # recent tape (public projection), newest first
    recent = service.recent(limit=10)
    assert len(recent) == 3
    assert "raw" not in recent[0]

    # bus delivered the unique events to the live subscriber
    assert len(received) == 3

    # aggregator counts confirmed executions only (pressure excluded from totals)
    summary = service.summary(now_ms=1_000_100)
    win = summary["windows"]["1m"]
    assert win["count"] == 2  # two executions, the pressure event is excluded
    # 60000*1 (BTC long) + 3000*10 (ETH short) = 90000
    assert win["total_notional_usd"] == pytest.approx(90000.0)
    assert win["long_notional_usd"] == pytest.approx(60000.0)
    assert win["short_notional_usd"] == pytest.approx(30000.0)
    assert win["by_venue"].get("lighter") == pytest.approx(90000.0)


async def test_venues_reports_adapter_health():
    service = LiquidationService(Settings(), None, adapters=[ReplayAdapter([_event()], loop=True)])
    await service.start()
    try:
        await _wait_for(lambda: service._recent)
        venues = service.venues(now_ms=1_000_100)
        assert venues and venues[0]["adapter"] == "replay"
        assert venues[0]["events_total"] >= 1
    finally:
        await service.stop()
