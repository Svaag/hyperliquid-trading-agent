from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.hip4.capabilities import build_capability_probe
from hyperliquid_trading_agent.app.hip4.orderbook import parse_l2_book
from hyperliquid_trading_agent.app.hip4.registry import Hip4Registry
from hyperliquid_trading_agent.app.hip4.scanner import Hip4Scanner

FIXTURES = Path("tests/fixtures/hip4")
FRESHNESS_FOR_FIXTURES = 10_000_000_000_000


def _registry() -> Hip4Registry:
    payload = json.loads((FIXTURES / "outcome_meta_with_questions.json").read_text())
    registry = Hip4Registry(settings=Settings(environment="test"))
    registry.load_raw(payload, observed_at_ms=1)
    return registry


def _binary_books(as_of0: int | None = None, as_of1: int | None = None):
    return {
        "#1720": parse_l2_book("#1720", json.loads((FIXTURES / "l2_book_side0.json").read_text()), source="fixture", as_of_ms=as_of0),
        "#1721": parse_l2_book("#1721", json.loads((FIXTURES / "l2_book_side1.json").read_text()), source="fixture", as_of_ms=as_of1),
    }


def test_binary_split_sell_uses_executable_depth_and_proves_no_residual_inventory() -> None:
    registry = _registry()
    settings = Settings(environment="test", hip4_scan_enabled=True, hip4_scan_max_book_staleness_ms=FRESHNESS_FOR_FIXTURES)
    scanner = Hip4Scanner(settings=settings)
    books = _binary_books()
    capabilities = build_capability_probe(json.loads((FIXTURES / "outcome_meta_with_questions.json").read_text()), settings=settings, probed_at_ms=1)

    candidates = scanner.scan(outcomes={172: registry.outcomes[172]}, questions={}, books=books, capabilities=capabilities)

    split_sell = next(item for item in candidates if item.strategy_type == "binary_split_sell")
    assert split_sell.size == Decimal("500")
    assert split_sell.expected_net_edge_usd == Decimal("24.500")
    assert split_sell.proof["residual_inventory_zero"] is True
    assert split_sell.residual_inventory == {}
    assert split_sell.quote_token == "USDC"
    assert all(leg.avg_price != Decimal("0") for leg in split_sell.legs)


def test_partial_depth_rejected_for_risk_free_candidate() -> None:
    registry = _registry()
    settings = Settings(environment="test", hip4_scan_enabled=True, hip4_scan_max_book_staleness_ms=FRESHNESS_FOR_FIXTURES)
    scanner = Hip4Scanner(settings=settings)
    raw = json.loads((FIXTURES / "partial_depth_books.json").read_text())
    books = {coin: parse_l2_book(coin, payload, source="fixture") for coin, payload in raw.items()}

    candidates = scanner.scan(outcomes={172: registry.outcomes[172]}, questions={}, books=books)

    assert not [item for item in candidates if item.strategy_type == "binary_split_sell"]
    assert any(item["code"] == "partial_depth" for item in scanner.last_rejects)


def test_default_edge_threshold_requires_both_bps_and_usd() -> None:
    settings = Settings(environment="test", hip4_scan_enabled=True, hip4_scan_max_book_staleness_ms=FRESHNESS_FOR_FIXTURES, hip4_min_edge_bps=Decimal("1"), hip4_min_edge_usd=Decimal("1000"))
    scanner = Hip4Scanner(settings=settings)
    registry = _registry()
    books = _binary_books()

    candidates = scanner.scan(outcomes={172: registry.outcomes[172]}, questions={}, books=books)

    assert not candidates
    assert any(item["code"] == "edge_below_threshold" for item in scanner.last_rejects)


def test_stale_books_do_not_emit_candidates() -> None:
    settings = Settings(environment="test", hip4_scan_enabled=True, hip4_scan_max_book_staleness_ms=1)
    scanner = Hip4Scanner(settings=settings)
    registry = _registry()
    books = _binary_books(as_of0=1, as_of1=1)

    candidates = scanner.scan(outcomes={172: registry.outcomes[172]}, questions={}, books=books)

    assert candidates == []
    assert any(item["code"] == "stale_book" for item in scanner.last_rejects)


def test_candidate_as_of_uses_min_book_timestamp() -> None:
    settings = Settings(environment="test", hip4_scan_enabled=True, hip4_scan_max_book_staleness_ms=FRESHNESS_FOR_FIXTURES)
    scanner = Hip4Scanner(settings=settings)
    registry = _registry()
    books = _binary_books(as_of0=100, as_of1=200)

    candidate = next(item for item in scanner.scan(outcomes={172: registry.outcomes[172]}, questions={}, books=books) if item.strategy_type == "binary_split_sell")

    assert candidate.as_of_ms == 100
    assert candidate.proof["book_as_of_ms_by_coin"] == {"#1720": 100, "#1721": 200}


def test_missing_quote_token_rejects_candidate() -> None:
    settings = Settings(environment="test", hip4_scan_enabled=True, hip4_scan_max_book_staleness_ms=FRESHNESS_FOR_FIXTURES)
    scanner = Hip4Scanner(settings=settings)
    registry = _registry()
    outcome = registry.outcomes[172].model_copy(update={"quote_token": None})
    books = _binary_books()

    candidates = scanner.scan(outcomes={172: outcome}, questions={}, books=books)

    assert candidates == []
    assert any(item["code"] == "quote_token_missing" for item in scanner.last_rejects)
