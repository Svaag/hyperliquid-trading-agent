from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.hip4.orderbook import parse_l2_book
from hyperliquid_trading_agent.app.hip4.registry import Hip4Registry
from hyperliquid_trading_agent.app.hip4.risk import Hip4RiskChecker
from hyperliquid_trading_agent.app.hip4.scanner import Hip4Scanner
from hyperliquid_trading_agent.app.main import create_app

FIXTURES = Path("tests/fixtures/hip4")


class FakeHip4ModeService:
    def __init__(self, *, supports_paper: bool = True, supports_manual: bool = False):
        self.capabilities = SimpleNamespace(supports_native_action_modeling=supports_paper, supports_manual_ticket_export=supports_manual)
        self.scanner = SimpleNamespace(last_rejects=[])
        self.paper = SimpleNamespace(snapshot=lambda: {"balances": {"USDC": "100"}}, list_actions=lambda: [])

    def status(self):
        return {"enabled": True, "status": "ok"}

    async def run_scan(self):
        return []

    async def execute_paper_candidate(self, candidate_id: str):
        return {"candidate_id": candidate_id}

    async def reconcile_paper(self):
        return {"status": "ok"}

    async def manual_ticket(self, candidate_id: str):
        return {"candidate_id": candidate_id, "non_executable": True}


def _candidate(settings: Settings):
    payload = json.loads((FIXTURES / "outcome_meta_with_questions.json").read_text())
    registry = Hip4Registry(settings=settings)
    registry.load_raw(payload, observed_at_ms=1)
    books = {
        "#1720": parse_l2_book("#1720", json.loads((FIXTURES / "l2_book_side0.json").read_text()), source="fixture"),
        "#1721": parse_l2_book("#1721", json.loads((FIXTURES / "l2_book_side1.json").read_text()), source="fixture"),
    }
    return Hip4Scanner(settings=settings).scan(outcomes={172: registry.outcomes[172]}, questions={}, books=books)[0]


def test_read_only_mode_rejects_scan_route_even_if_scan_flag_enabled() -> None:
    app = create_app(Settings(environment="test", hip4_enabled=True, hip4_mode="read_only", hip4_scan_enabled=True, position_tracking_enabled=False, autonomy_enabled=False))
    app.state.hip4_service = FakeHip4ModeService()
    client = TestClient(app)

    response = client.post("/hip4/scan/run")

    assert response.status_code == 409
    assert "does not allow scanning" in response.json()["detail"]


def test_shadow_mode_rejects_paper_route_even_if_paper_flag_enabled() -> None:
    app = create_app(Settings(environment="test", hip4_enabled=True, hip4_mode="shadow", hip4_paper_execution_enabled=True, position_tracking_enabled=False, autonomy_enabled=False))
    app.state.hip4_service = FakeHip4ModeService()
    client = TestClient(app)

    response = client.post("/hip4/paper/execute/candidate")

    assert response.status_code == 409
    assert "does not allow paper" in response.json()["detail"]


def test_paper_shadow_mode_still_requires_paper_flag() -> None:
    app = create_app(Settings(environment="test", hip4_enabled=True, hip4_mode="paper_shadow", hip4_paper_execution_enabled=False, position_tracking_enabled=False, autonomy_enabled=False))
    app.state.hip4_service = FakeHip4ModeService()
    client = TestClient(app)

    response = client.post("/hip4/paper/execute/candidate")

    assert response.status_code == 409
    assert "paper execution is disabled" in response.json()["detail"]


def test_paper_route_requires_capability_probe() -> None:
    app = create_app(Settings(environment="test", hip4_enabled=True, hip4_mode="paper_shadow", hip4_paper_execution_enabled=True, position_tracking_enabled=False, autonomy_enabled=False))
    service = FakeHip4ModeService()
    service.capabilities = None
    app.state.hip4_service = service
    client = TestClient(app)

    response = client.post("/hip4/paper/execute/candidate")

    assert response.status_code == 403
    assert "capability probe" in response.json()["detail"]


def test_manual_ticket_route_requires_mode_and_capability() -> None:
    app = create_app(Settings(environment="test", hip4_enabled=True, hip4_mode="paper_shadow", hip4_manual_ticket_export_enabled=True, position_tracking_enabled=False, autonomy_enabled=False))
    app.state.hip4_service = FakeHip4ModeService(supports_manual=False)
    client = TestClient(app)

    response = client.post("/hip4/manual-ticket/candidate")

    assert response.status_code == 403
    assert "capabilities" in response.json()["detail"]


@pytest.mark.asyncio
async def test_risk_rejects_paper_when_mode_disallows_it() -> None:
    settings = Settings(environment="test", hip4_enabled=True, hip4_mode="shadow", hip4_scan_enabled=True, hip4_scan_max_book_staleness_ms=10_000_000_000_000)
    candidate = _candidate(settings).model_copy(update={"mode": "paper"})

    decision = await Hip4RiskChecker(settings=settings).check_candidate(candidate, registry_last_refresh_at_ms=candidate.as_of_ms)

    assert decision.allowed is False
    assert any(item["code"] == "mode_disallows_paper" for item in decision.violations)


@pytest.mark.asyncio
async def test_risk_rejects_paper_without_capabilities() -> None:
    settings = Settings(environment="test", hip4_enabled=True, hip4_mode="paper_shadow", hip4_scan_enabled=True, hip4_scan_max_book_staleness_ms=10_000_000_000_000)
    candidate = _candidate(settings).model_copy(update={"mode": "paper"})

    decision = await Hip4RiskChecker(settings=settings).check_candidate(candidate, registry_last_refresh_at_ms=candidate.as_of_ms)

    assert decision.allowed is False
    assert any(item["code"] == "capability_probe_missing" for item in decision.violations)
