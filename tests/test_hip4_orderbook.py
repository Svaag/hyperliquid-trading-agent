from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from hyperliquid_trading_agent.app.hip4.orderbook import book_is_fresh, executable_vwap, parse_l2_book

FIXTURES = Path("tests/fixtures/hip4")


def test_l2_book_parse_and_executable_vwap() -> None:
    payload = json.loads((FIXTURES / "l2_book_side0.json").read_text())
    book = parse_l2_book("#1720", payload, source="fixture")

    assert book.coin == "#1720"
    assert book.outcome_id == 172
    assert book.side == 0
    assert book.bids[0].px == Decimal("0.62")
    filled, avg = executable_vwap(book.bids, Decimal("400"))
    assert filled == Decimal("400")
    assert avg == Decimal("0.62")


def test_stale_book_marked_unusable_by_freshness_check() -> None:
    payload = json.loads((FIXTURES / "stale_book_snapshot.json").read_text())
    book = parse_l2_book("#1720", payload, source="fixture")

    assert book_is_fresh(book, now_ms=100_000, max_staleness_ms=10_000) is False
