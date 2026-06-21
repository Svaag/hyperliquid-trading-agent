from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.hip4.orderbook import parse_l2_book
from hyperliquid_trading_agent.app.hip4.paper import Hip4PaperLedger
from hyperliquid_trading_agent.app.hip4.registry import Hip4Registry
from hyperliquid_trading_agent.app.hip4.scanner import Hip4Scanner

FIXTURES = Path("tests/fixtures/hip4")


def _candidate(settings: Settings):
    payload = json.loads((FIXTURES / "outcome_meta_with_questions.json").read_text())
    registry = Hip4Registry(settings=settings)
    registry.load_raw(payload, observed_at_ms=1)
    books = {
        "#1720": parse_l2_book("#1720", json.loads((FIXTURES / "l2_book_side0.json").read_text()), source="fixture"),
        "#1721": parse_l2_book("#1721", json.loads((FIXTURES / "l2_book_side1.json").read_text()), source="fixture"),
    }
    candidates = Hip4Scanner(settings=settings).scan(outcomes={172: registry.outcomes[172]}, questions={}, books=books)
    return next(item for item in candidates if item.strategy_type == "binary_split_sell")


@pytest.mark.asyncio
async def test_paper_ledger_executes_complete_set_without_negative_inventory() -> None:
    settings = Settings(environment="test", hip4_scan_enabled=True, hip4_scan_max_book_staleness_ms=10_000_000_000_000, hip4_paper_execution_enabled=True)
    candidate = _candidate(settings)
    ledger = Hip4PaperLedger(settings=settings)

    result = await ledger.execute_candidate(candidate)

    balances = result["portfolio"]["balances"]
    assert "+1720" not in balances
    assert "+1721" not in balances
    assert Decimal(str(balances["USDC"])) == Decimal("100024.50")
    assert ledger.reconcile()["status"] == "ok"


@pytest.mark.asyncio
async def test_paper_ledger_disabled_flag_blocks_execution() -> None:
    settings = Settings(environment="test", hip4_scan_enabled=True, hip4_scan_max_book_staleness_ms=10_000_000_000_000, hip4_paper_execution_enabled=False)
    candidate = _candidate(settings)
    ledger = Hip4PaperLedger(settings=settings)

    with pytest.raises(PermissionError):
        await ledger.execute_candidate(candidate)


@pytest.mark.asyncio
async def test_paper_ledger_rejects_quote_token_mismatch() -> None:
    settings = Settings(environment="test", hip4_scan_enabled=True, hip4_scan_max_book_staleness_ms=10_000_000_000_000, hip4_paper_execution_enabled=True)
    candidate = _candidate(settings).model_copy(update={"quote_token": "USDT"})
    ledger = Hip4PaperLedger(settings=settings, quote_token="USDC")

    with pytest.raises(ValueError, match="quote token"):
        await ledger.execute_candidate(candidate)
