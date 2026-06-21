from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.hip4.capabilities import build_capability_probe
from hyperliquid_trading_agent.app.hip4.orderbook import parse_l2_book
from hyperliquid_trading_agent.app.hip4.registry import Hip4Registry
from hyperliquid_trading_agent.app.hip4.risk import Hip4RiskChecker
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
    return Hip4Scanner(settings=settings).scan(outcomes={172: registry.outcomes[172]}, questions={}, books=books)[0]


@pytest.mark.asyncio
async def test_risk_rejects_when_hip4_disabled() -> None:
    settings = Settings(environment="test", hip4_enabled=False, hip4_scan_enabled=True, hip4_scan_max_book_staleness_ms=10_000_000_000_000)
    candidate = _candidate(settings)

    decision = await Hip4RiskChecker(settings=settings).check_candidate(candidate, registry_last_refresh_at_ms=1)

    assert decision.allowed is False
    assert any(item["code"] == "hip4_disabled" for item in decision.violations)


@pytest.mark.asyncio
async def test_risk_rejects_stale_and_partial_settlement() -> None:
    settings = Settings(environment="test", hip4_enabled=True, hip4_scan_enabled=True, hip4_scan_max_book_staleness_ms=10_000_000_000_000)
    candidate = _candidate(settings)
    payload = json.loads((FIXTURES / "outcome_meta_with_questions.json").read_text())
    capabilities = build_capability_probe(payload, settings=settings, probed_at_ms=1)
    registry = Hip4Registry(settings=settings)
    registry.load_raw(payload, observed_at_ms=1)
    partial_question = registry.questions[32].model_copy(update={"status": "partial_settled"})

    risk_settings = settings.model_copy(update={"hip4_scan_max_book_staleness_ms": 1, "hip4_registry_max_staleness_ms": 10_000_000_000_000})
    decision = await Hip4RiskChecker(settings=risk_settings).check_candidate(
        candidate,
        capabilities=capabilities,
        question=partial_question,
        registry_last_refresh_at_ms=candidate.as_of_ms,
        now_ms=candidate.as_of_ms + 10_000,
    )

    codes = {item["code"] for item in decision.violations}
    assert "stale_candidate" in codes
    assert "settled_or_partial_question" in codes


@pytest.mark.asyncio
async def test_manual_ticket_rejects_unless_enabled() -> None:
    settings = Settings(environment="test", hip4_enabled=True, hip4_scan_enabled=True, hip4_scan_max_book_staleness_ms=10_000_000_000_000)
    candidate = _candidate(settings).model_copy(update={"mode": "manual_ticket"})

    decision = await Hip4RiskChecker(settings=settings).check_candidate(candidate, registry_last_refresh_at_ms=candidate.as_of_ms, manual_ticket=True)

    assert decision.allowed is False
    assert any(item["code"] == "manual_ticket_disabled" for item in decision.violations)


@pytest.mark.asyncio
async def test_edge_threshold_either_rejects_when_both_fail() -> None:
    build_settings = Settings(environment="test", hip4_enabled=True, hip4_scan_enabled=True, hip4_scan_max_book_staleness_ms=10_000_000_000_000)
    risk_settings = build_settings.model_copy(
        update={
            "hip4_mode": "paper_shadow",
            "hip4_edge_threshold_mode": "either",
            "hip4_min_edge_bps": Decimal("10000"),
            "hip4_min_edge_usd": Decimal("1000"),
        }
    )
    candidate = _candidate(build_settings)

    decision = await Hip4RiskChecker(settings=risk_settings).check_candidate(candidate, registry_last_refresh_at_ms=candidate.as_of_ms)

    assert decision.allowed is False
    assert any(item["code"] == "edge_below_minimum" for item in decision.violations)
