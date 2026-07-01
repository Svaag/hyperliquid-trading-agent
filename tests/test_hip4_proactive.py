from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.hip4.capabilities import build_capability_probe
from hyperliquid_trading_agent.app.hip4.orderbook import parse_l2_book
from hyperliquid_trading_agent.app.hip4.service import Hip4Service

FIXTURES = Path("tests/fixtures/hip4")
STALE_OK_MS = 10_000_000_000_000


def _seed_service(settings: Settings) -> Hip4Service:
    payload = json.loads((FIXTURES / "outcome_meta_with_questions.json").read_text())
    service = Hip4Service(settings=settings)
    service.capabilities = build_capability_probe(payload, settings=settings, probed_at_ms=1)
    service.registry.load_raw(payload, observed_at_ms=int(time.time() * 1000))
    service.ws_manager.books = {
        "#1720": parse_l2_book("#1720", json.loads((FIXTURES / "l2_book_side0.json").read_text()), source="fixture"),
        "#1721": parse_l2_book("#1721", json.loads((FIXTURES / "l2_book_side1.json").read_text()), source="fixture"),
    }
    return service


@pytest.mark.asyncio
async def test_proactive_cycle_paper_executes_and_tracks_pnl_inventory_and_learning() -> None:
    settings = Settings(
        environment="test",
        hip4_enabled=True,
        hip4_mode="paper_shadow",
        hip4_scan_enabled=True,
        hip4_paper_execution_enabled=True,
        hip4_proactive_paper_execution_enabled=True,
        hip4_proactive_max_paper_executions_per_cycle=1,
        hip4_scan_max_book_staleness_ms=STALE_OK_MS,
        hip4_paper_execution_max_book_staleness_ms=STALE_OK_MS,
        hip4_registry_max_staleness_ms=STALE_OK_MS,
        hip4_allow_inferred_lot_size_for_paper=True,
        hip4_min_edge_usd=1,
        hip4_min_edge_bps=1,
        hip4_discord_digest_enabled=False,
    )
    service = _seed_service(settings)

    summary = await service.run_proactive_cycle(manual=True)

    assert summary["status"] == "ok"
    assert summary["candidate_count"] >= 1
    assert summary["paper_execution_count"] == 1
    assert service.paper.snapshot()["realized_pnl"] != "0"
    assert service.paper.list_fills()
    assert service.paper.snapshot()["balances"]["USDC"] > "100000"
    learning = service.learning_status()
    assert learning["cycles"] == 1
    assert learning["strategy_stats"]["binary_split_sell"]["paper_executed"] == 1


@pytest.mark.asyncio
async def test_proactive_loop_is_disabled_by_default() -> None:
    service = Hip4Service(settings=Settings(environment="test", hip4_enabled=True, hip4_scan_enabled=True, hip4_proactive_loop_enabled=False, _env_file=None))

    await service.start_proactive_loop()

    assert service.proactive_loop_status()["enabled"] is False
    assert service.proactive_loop_status()["task_active"] is False


def test_registry_refresh_due_uses_refresh_cadence_and_staleness() -> None:
    settings = Settings(
        environment="test",
        hip4_enabled=True,
        hip4_scan_enabled=True,
        hip4_outcome_meta_refresh_seconds=60,
        hip4_registry_max_staleness_ms=300_000,
    )
    service = Hip4Service(settings=settings)
    service.registry.last_refresh_at_ms = int(time.time() * 1000)

    assert service._registry_refresh_due() is False

    service.registry.last_refresh_at_ms = int(time.time() * 1000) - 61_000

    assert service._registry_refresh_due() is True
