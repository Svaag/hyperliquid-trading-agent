from __future__ import annotations

from fastapi.testclient import TestClient

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.main import create_app


class FakeHip4Service:
    capabilities = None

    def __init__(self):
        self.registry = type("Registry", (), {"status": lambda self: {"stale": False}, "questions": {}})()
        self.ws_manager = type("Ws", (), {"status": lambda self: {}})()
        self.scanner = type("Scanner", (), {"last_rejects": []})()
        self.paper = type("Paper", (), {"snapshot": lambda self: {"balances": {"USDC": "100"}}, "list_actions": lambda self: []})()

    def status(self):
        return {"enabled": True, "status": "ok"}

    def list_outcomes(self):
        return [{"outcome_id": 172}]

    def list_questions(self):
        return []

    def list_books(self):
        return []

    def list_edges(self):
        return []


def test_hip4_routes_fail_closed_when_disabled() -> None:
    app = create_app(Settings(environment="test", hip4_enabled=False, position_tracking_enabled=False, autonomy_enabled=False))
    client = TestClient(app)

    assert client.get("/hip4/status").status_code == 200
    assert client.get("/hip4/outcomes").status_code == 409
    assert client.post("/hip4/manual-ticket/test").status_code == 404


def test_hip4_read_route_uses_bounded_service_when_enabled() -> None:
    app = create_app(Settings(environment="test", hip4_enabled=True, position_tracking_enabled=False, autonomy_enabled=False))
    app.state.hip4_service = FakeHip4Service()
    client = TestClient(app)

    response = client.get("/hip4/outcomes")

    assert response.status_code == 200
    assert response.json()["items"] == [{"outcome_id": 172}]


def test_hip4_paper_action_requires_auth_outside_dev() -> None:
    app = create_app(Settings(environment="prod", agent_api_bearer_token="secret", hip4_enabled=False, position_tracking_enabled=False, autonomy_enabled=False, engine_enabled=False, orchestration_wave_supervisor_enabled=False, tradfi_enabled=False, _env_file=None))
    app.state.hip4_service = FakeHip4Service()
    client = TestClient(app)

    assert client.post("/hip4/paper/execute/missing").status_code == 401
